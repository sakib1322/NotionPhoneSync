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
        if (inCall && rec == null && p.callRecord && p.signedIn.isNotEmpty() && allowed(ctx)) {
            begin(if (mode == AudioManager.MODE_IN_CALL) "Phone" else appName(foreground))
        } else if (!inCall && rec != null) {
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

    private fun begin(appLabel: String) {
        app = appLabel
        showBadge()
        val stamp = SimpleDateFormat("yyyy-MM-dd HH.mm", Locale.US).format(Date())
        val f = File(folder(), "$app call $stamp.m4a")
        // Sources most likely to keep working while another app is on a call, best first.
        val sources = listOf(MediaRecorder.AudioSource.VOICE_RECOGNITION, MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.VOICE_COMMUNICATION, MediaRecorder.AudioSource.CAMCORDER)
        for (src in sources) {
            val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(ctx) else @Suppress("DEPRECATION") MediaRecorder()
            try {
                r.setAudioSource(src)
                r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                r.setAudioSamplingRate(44100)
                r.setAudioEncodingBitRate(96_000)
                r.setAudioChannels(1)
                r.setOutputFile(f.absolutePath)
                r.prepare()
                r.start()
                rec = r
                file = f
                started = System.currentTimeMillis()
                maxAmp = 0
                main.postDelayed(meter, 500)
                Logger.log(ctx, "Recording the $app call…")
                return
            } catch (e: Exception) {
                try { r.release() } catch (x: Exception) { }
                f.delete()
            }
        }
        hideBadge()
        tell("Couldn't record this call", "The phone wouldn't let the app use the microphone during this $app call.")
    }

    private val meter = object : Runnable {
        override fun run() {
            val r = rec ?: return
            try { maxAmp = maxOf(maxAmp, r.maxAmplitude) } catch (e: Exception) { }
            badge?.text = "● REC  " + dur((System.currentTimeMillis() - started) / 1000)
            main.postDelayed(this, 500)
        }
    }

    private fun finish() {
        main.removeCallbacks(meter)
        val r = rec ?: run { hideBadge(); return }
        rec = null
        try { maxAmp = maxOf(maxAmp, r.maxAmplitude) } catch (e: Exception) { }
        try { r.stop() } catch (e: Exception) { }
        try { r.release() } catch (e: Exception) { }
        hideBadge()
        val f = file ?: return
        val secs = (System.currentTimeMillis() - started) / 1000
        if (maxAmp < 50 || f.length() < 2000) {
            f.delete()
            tell("This $app call wasn't recorded",
                "Your phone gave the app only silence during this call (Samsung does this for some calls). Nothing was saved.")
            Logger.log(ctx, "The $app call couldn't be recorded: the phone gave only silence.")
            return
        }
        MediaScannerConnection.scanFile(ctx, arrayOf(f.path), arrayOf("audio/mp4"), null)
        tell("$app call recorded (${dur(secs)})", "Saved in Recordings › Calls: ${f.name}. It'll be backed up to Notion.")
        Logger.log(ctx, "Recorded the $app call (${dur(secs)}): Recordings/Calls/${f.name}")
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
