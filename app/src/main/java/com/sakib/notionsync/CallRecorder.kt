package com.sakib.notionsync

import android.Manifest
import android.accessibilityservice.AccessibilityService
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.media.AudioManager
import android.media.MediaCodec
import android.media.MediaExtractor
import android.media.MediaFormat
import android.media.MediaMuxer
import android.media.MediaRecorder
import android.media.MediaScannerConnection
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.WindowManager
import android.widget.TextView
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import java.io.File
import java.nio.ByteBuffer
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executor

/**
 * Records calls (WhatsApp, Meet, Messenger, normal phone calls…) by itself, from inside the
 * Accessibility helper.
 *
 * Android lets an Accessibility helper keep recording the microphone during a call only while
 * something of its own is visible on screen, so a small "● REC" badge shows while recording.
 * Your voice is always picked up; the other person only as the microphone hears them (clear on
 * speakerphone). If the phone hands the app nothing but silence, the empty file is deleted and
 * you're told.
 */
class CallRecorder(private val svc: AccessibilityService) {
    private val ctx: Context = svc
    private val main = Handler(Looper.getMainLooper())
    private val am = ctx.getSystemService(AudioManager::class.java)
    private var rec: MediaRecorder? = null
    private var file: File? = null
    private var started = 0L
    private var app = ""
    private var maxAmp = 0
    private var badge: TextView? = null
    private var listener: Any? = null
    @Volatile var foreground = ""   // the app on screen (from the helper's window events)

    companion object {
        const val CHANNEL = "call_recordings"

        fun allowed(ctx: Context) =
            ctx.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

        fun folder(): File = File(Environment.getExternalStorageDirectory(), "Recordings/Calls").apply { mkdirs() }
    }

    @Volatile private var stopped = false
    // After a "stuck" call mode ended a recording, wait for a real change before recording again.
    private var ignoreUntilNormal = false

    fun start() {
        if (Build.VERSION.SDK_INT >= 31) {
            val l = AudioManager.OnModeChangedListener { onMode(it) }
            am.addOnModeChangedListener(Executor { main.post(it) }, l)
            listener = l
        }
        // A second check every 2 seconds, in case a call app or the phone doesn't announce the
        // change (so a recording always stops when the call ends).
        main.post(object : Runnable {
            override fun run() {
                if (stopped) return
                onMode(am.mode)
                main.postDelayed(this, 2000)
            }
        })
    }

    fun stop() {
        stopped = true
        if (Build.VERSION.SDK_INT >= 31) (listener as? AudioManager.OnModeChangedListener)?.let { am.removeOnModeChangedListener(it) }
        finish()
    }

    private fun onMode(mode: Int) {
        val inCall = mode == AudioManager.MODE_IN_CALL || mode == AudioManager.MODE_IN_COMMUNICATION
        val p = Prefs(ctx)
        if (!inCall || (ignoreUntilNormal && callAlive())) ignoreUntilNormal = false
        // `file` is set for the whole call, even between pieces.
        if (inCall && file == null && !ignoreUntilNormal && p.callRecord && p.signedIn.isNotEmpty() && allowed(ctx)) {
            begin(if (mode == AudioManager.MODE_IN_CALL) "Phone" else appName(foreground))
        } else if (!inCall && file != null) {
            finish()
        }
    }

    private fun appName(pkg: String): String = when {
        pkg.startsWith("com.whatsapp.w4b") -> "WhatsApp Business"
        pkg.startsWith("com.whatsapp") -> "WhatsApp"
        pkg.contains("meetings") || pkg.contains("apps.meet") || pkg.contains("tachyon") -> "Meet"
        pkg.contains("messenger") || pkg.contains("orca") -> "Messenger"
        pkg.contains("telegram") -> "Telegram"
        pkg.contains("viber") -> "Viber"
        pkg.contains("imo") -> "imo"
        pkg.contains("zoom") -> "Zoom"
        pkg.contains("skype") -> "Skype"
        pkg.contains("teams") -> "Teams"
        pkg.contains("signal") -> "Signal"
        pkg.isEmpty() -> "Call"
        else -> try {
            ctx.packageManager.getApplicationLabel(ctx.packageManager.getApplicationInfo(pkg, 0)).toString()
        } catch (e: Exception) { "Call" }
    }

    /** Microphone sources, best for a call recording first. The raw ones skip the phone's echo
     *  filter, which otherwise scrubs the other person's voice (from the speaker) out of the
     *  recording. If one gives silence, the next is tried automatically. */
    private fun sources(): List<Int> {
        val out = ArrayList<Int>()
        val raw = try { am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED) == "true" } catch (e: Exception) { false }
        if (raw) out.add(MediaRecorder.AudioSource.UNPROCESSED)
        out.add(MediaRecorder.AudioSource.MIC)
        out.add(MediaRecorder.AudioSource.CAMCORDER)
        out.add(MediaRecorder.AudioSource.VOICE_RECOGNITION)   // filtered, but most often allowed during calls
        out.add(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
        return out
    }

    private var srcList: List<Int> = emptyList()
    private var srcIdx = 0
    private var segStart = 0L
    private var callStart = 0L
    private var boosted = false
    private var boostRefused = false
    private var boostAt = 0L
    // A call is recorded in pieces: when the phone cuts the app off mid-call (e.g. you switch to
    // speaker), a new piece starts. At the end, the pieces with sound are joined into one file.
    private val pieces = ArrayList<Pair<File, Int>>()   // (file, loudest level)
    private var piece: File? = null
    private var quietSince = 0L                          // when true silence started in this piece

    private fun begin(appLabel: String) {
        app = appLabel
        showBadge()
        val stamp = SimpleDateFormat("yyyy-MM-dd HH.mm", Locale.US).format(Date())
        file = File(folder(), "$app call $stamp.m4a")
        srcList = sources()
        pieces.clear()
        callStart = System.currentTimeMillis()
        everAlive = false
        idleSince = 0L
        if (openFrom(0)) {
            main.postDelayed(meter, 500)
            Logger.log(ctx, "Recording the $app call…")
            return
        }
        // Couldn't open the microphone at all yet: keep trying through the call.
        main.postDelayed(meter, 1000)
        Logger.log(ctx, "Recording the $app call: waiting for the microphone…")
    }

    /** Starts a new piece with the first source from `from` that opens. */
    private fun openFrom(from: Int): Boolean {
        val dir = File(ctx.cacheDir, "callparts").apply { mkdirs() }
        for (i in from until srcList.size) {
            val f = File(dir, "part${System.nanoTime()}.m4a")
            val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(ctx) else @Suppress("DEPRECATION") MediaRecorder()
            try {
                r.setAudioSource(srcList[i])
                r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                r.setAudioSamplingRate(44100)
                r.setAudioEncodingBitRate(96_000)
                r.setAudioChannels(1)
                r.setOutputFile(f.absolutePath)
                r.prepare()
                r.start()
                rec = r
                piece = f
                srcIdx = i
                started = System.currentTimeMillis()
                segStart = started
                quietSince = 0L
                maxAmp = 0
                return true
            } catch (e: Exception) {
                try { r.release() } catch (x: Exception) { }
                f.delete()
            }
        }
        return false
    }

    /** Ends the current piece (kept if it has sound). */
    private fun closePiece() {
        val r = rec ?: return
        rec = null
        try { maxAmp = maxOf(maxAmp, r.maxAmplitude) } catch (e: Exception) { }
        try { r.stop() } catch (e: Exception) { }
        try { r.release() } catch (e: Exception) { }
        val f = piece ?: return
        piece = null
        if (maxAmp >= 50 && f.length() > 2000) pieces.add(f to maxAmp) else f.delete()
    }

    private var idleSince = 0L
    private var everAlive = false

    /** Is a call really going on? Some apps (e.g. InDrive after a failed call) leave the phone in
     *  "call mode" with no call. A real internet call has the call app recording the microphone
     *  for the call, or the other person's voice playing. */
    private fun callAlive(): Boolean {
        if (am.mode == AudioManager.MODE_IN_CALL) return true   // phone calls end reliably
        return try {
            val mine = if (rec != null && srcList.getOrNull(srcIdx) == MediaRecorder.AudioSource.VOICE_COMMUNICATION) 1 else 0
            val callMic = am.activeRecordingConfigurations.count { it.clientAudioSource == MediaRecorder.AudioSource.VOICE_COMMUNICATION } > mine
            val callSound = am.activePlaybackConfigurations.any {
                it.audioAttributes.usage == android.media.AudioAttributes.USAGE_VOICE_COMMUNICATION
            }
            callMic || callSound
        } catch (e: Exception) { true }
    }

    private val meter = object : Runnable {
        override fun run() {
            // The call is over even if the call app didn't say so: stop and save.
            val now0 = System.currentTimeMillis()
            val alive = callAlive()
            if (alive) everAlive = true
            // (While it's still ringing there's no call audio yet: only judge once it connected,
            // or after a minute.)
            if (alive || !(everAlive || now0 - callStart > 60_000)) idleSince = 0L
            else if (idleSince == 0L) idleSince = now0
            if (idleSince != 0L && now0 - idleSince >= 5000) {
                Logger.log(ctx, "Call recording: the call is over (the $app app didn't say so); saving.")
                idleSince = 0L
                finish()
                ignoreUntilNormal = true
                return
            }
            val r = rec
            if (r == null) {
                // Between pieces: keep trying to get the microphone back.
                if (openFrom(0)) Logger.log(ctx, "Call recording: microphone back, recording again.")
                main.postDelayed(this, 1000)
                return
            }
            val amp = try { r.maxAmplitude } catch (e: Exception) { 0 }
            maxAmp = maxOf(maxAmp, amp)
            val now = System.currentTimeMillis()
            badge?.text = "● REC  " + dur((now - callStart) / 1000)
            // A blocked microphone gives exactly nothing; a quiet room still gives a little.
            if (amp < 5) { if (quietSince == 0L) quietSince = now } else quietSince = 0L
            val silentFor = if (quietSince == 0L) 0L else (now - quietSince) / 1000

            if (maxAmp < 50) {
                // Silent from the start of this piece.
                val secs = (now - segStart) / 1000
                if (!boosted && !boostRefused && secs >= 3) boost("the phone gave silence")
                val since = (now - maxOf(segStart, boostAt)) / 1000
                if ((boosted || boostRefused) && since >= 3 && srcIdx + 1 < srcList.size) {
                    closePiece()
                    if (openFrom(srcIdx + 1)) Logger.log(ctx, "Call recording: trying another microphone source.")
                }
            } else if (silentFor >= 3) {
                // Had sound, then the phone cut the app off (e.g. you switched to speaker):
                // keep what we have and start a new piece, trying every source again.
                Logger.log(ctx, "Call recording: the phone cut the microphone off mid-call (speaker switch?); starting again.")
                closePiece()
                if (!boosted && !boostRefused) boost("cut off mid-call")
                openFrom(0)
            }
            // Never at the cost of the call: if the call app itself gets silenced, back off at once.
            if (boosted && callAppSilenced()) {
                CallTopActivity.hide()
                boosted = false
                boostRefused = true
                boostAt = now
                Logger.log(ctx, "Call recording: backed off so the other person can still hear you.")
            }
            main.postDelayed(this, 500)
        }
    }

    /** Put the see-through screen on top so this app counts as "on screen" and may record. */
    private fun boost(why: String) {
        boosted = true
        boostAt = System.currentTimeMillis()
        CallTopActivity.show(ctx)
        Logger.log(ctx, "Call recording: $why; trying with the see-through screen on top.")
    }

    /** Is any other app's recording (the call app's microphone) being silenced right now? */
    private fun callAppSilenced(): Boolean = try {
        am.activeRecordingConfigurations.any { c ->
            c.isClientSilenced && c.clientAudioSource != MediaRecorder.AudioSource.VOICE_RECOGNITION &&
                c.clientAudioSource != MediaRecorder.AudioSource.MIC &&
                c.clientAudioSource != MediaRecorder.AudioSource.UNPROCESSED &&
                c.clientAudioSource != MediaRecorder.AudioSource.CAMCORDER
        }
    } catch (e: Exception) { false }

    private fun finish() {
        main.removeCallbacks(meter)
        if (boosted) CallTopActivity.hide()
        boosted = false
        boostRefused = false
        boostAt = 0L
        val wasRecording = rec != null || pieces.isNotEmpty()
        closePiece()
        hideBadge()
        val f = file ?: return
        file = null
        if (!wasRecording && pieces.isEmpty()) return
        val secs = (System.currentTimeMillis() - callStart) / 1000
        val parts = pieces.map { it.first }
        pieces.clear()
        if (parts.isEmpty()) {
            tell("This $app call wasn't recorded",
                "Your phone gave the app only silence during this call, even with the app on top. " +
                    "Android on this phone reserves the microphone for the call app, so this kind of call can't be recorded by an app. Nothing was saved.")
            Logger.log(ctx, "The $app call couldn't be recorded: the phone gave only silence.")
            return
        }
        val ok = if (parts.size == 1) parts[0].renameTo(f) || run { parts[0].copyTo(f, true); true } else join(parts, f)
        parts.forEach { it.delete() }
        if (!ok) {
            tell("Couldn't save the $app call recording", "Joining the pieces of the recording failed.")
            return
        }
        MediaScannerConnection.scanFile(ctx, arrayOf(f.path), arrayOf("audio/mp4"), null)
        val note = if (parts.size > 1) " (the phone cut the app off ${parts.size - 1} time(s); those moments are missing)" else ""
        tell("$app call recorded (${dur(secs)})", "Saved in Recordings › Calls: ${f.name}$note. It'll be backed up to Notion.")
        Logger.log(ctx, "Recorded the $app call (${dur(secs)}, ${parts.size} piece(s)): Recordings/Calls/${f.name}")
    }

    /** Joins recordings (same format) one after another into one .m4a, without re-encoding. */
    private fun join(parts: List<File>, out: File): Boolean {
        val mux = try { MediaMuxer(out.path, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4) } catch (e: Exception) { return false }
        var track = -1
        var offset = 0L
        val buf = ByteBuffer.allocate(1 shl 20)
        val info = MediaCodec.BufferInfo()
        try {
            for (p in parts) {
                val ex = MediaExtractor()
                try {
                    ex.setDataSource(p.path)
                    var t = -1
                    for (i in 0 until ex.trackCount) {
                        if (ex.getTrackFormat(i).getString(MediaFormat.KEY_MIME)?.startsWith("audio/") == true) { t = i; break }
                    }
                    if (t < 0) continue
                    ex.selectTrack(t)
                    if (track < 0) { track = mux.addTrack(ex.getTrackFormat(t)); mux.start() }
                    var last = offset
                    while (true) {
                        val n = ex.readSampleData(buf, 0)
                        if (n < 0) break
                        info.set(0, n, offset + ex.sampleTime,
                            if (ex.sampleFlags and MediaExtractor.SAMPLE_FLAG_SYNC != 0) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0)
                        mux.writeSampleData(track, buf, info)
                        last = info.presentationTimeUs
                        ex.advance()
                    }
                    offset = last + 23_220   // one AAC frame later
                } catch (e: Exception) {
                } finally {
                    ex.release()
                }
            }
            if (track < 0) return false
            mux.stop()
            return true
        } catch (e: Exception) {
            out.delete()
            return false
        } finally {
            try { mux.release() } catch (e: Exception) { }
        }
    }

    private fun dur(s: Long) = if (s >= 3600) "%d:%02d:%02d".format(s / 3600, s / 60 % 60, s % 60) else "%d:%02d".format(s / 60, s % 60)

    /** The small "● REC" badge (it also keeps Android letting us record during the call). */
    private fun showBadge() {
        if (badge != null) return
        val d = ctx.resources.displayMetrics.density
        val v = TextView(ctx).apply {
            text = "● REC"
            setTextColor(Color.WHITE)
            textSize = 12f
            setPadding((10 * d).toInt(), (4 * d).toInt(), (10 * d).toInt(), (4 * d).toInt())
            background = GradientDrawable().apply { cornerRadius = 40 * d; setColor(0xCCD32F2F.toInt()) }
        }
        val lp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
            PixelFormat.TRANSLUCENT
        ).apply {
            gravity = Gravity.TOP or Gravity.END
            x = (12 * d).toInt()
            y = (48 * d).toInt()
        }
        try {
            ctx.getSystemService(WindowManager::class.java).addView(v, lp)
            badge = v
        } catch (e: Exception) { }
    }

    private fun hideBadge() {
        val v = badge ?: return
        badge = null
        try { ctx.getSystemService(WindowManager::class.java).removeView(v) } catch (e: Exception) { }
    }

    private fun tell(title: String, text: String) {
        try {
            val nm = ctx.getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(CHANNEL) == null) {
                nm.createNotificationChannel(NotificationChannel(CHANNEL, "Call recordings", NotificationManager.IMPORTANCE_DEFAULT))
            }
            NotificationManagerCompat.from(ctx).notify(
                (System.currentTimeMillis() % 100000).toInt() + 3000,
                NotificationCompat.Builder(ctx, CHANNEL).setSmallIcon(R.drawable.ic_stat)
                    .setContentTitle(title).setContentText(text)
                    .setStyle(NotificationCompat.BigTextStyle().bigText(text)).setAutoCancel(true).build()
            )
        } catch (e: SecurityException) { }
    }
}
