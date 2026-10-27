package com.sakib.notionsync

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.service.quicksettings.TileService
import android.widget.Toast
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.time.Instant
import java.util.concurrent.TimeUnit

/**
 * Shared clipboard. Keeps running quietly: anything copied on the laptop lands on the
 * phone's clipboard within seconds. Android doesn't let apps read the clipboard in the
 * background, so sending the other way is one tap: "Send copied" in this notification,
 * the Quick Settings tile, or Share → "Copy to laptop".
 */
class ClipService : Service() {

    companion object {
        const val CHANNEL = "clipboard"
        const val ID = 77
        @Volatile var lastFp: String = ""

        fun start(ctx: Context) {
            val p = Prefs(ctx)
            if (!p.clipboardSync || p.token.isEmpty() || p.rootPageId.isEmpty()) return
            try {
                ContextCompat.startForegroundService(ctx, Intent(ctx, ClipService::class.java))
            } catch (e: Exception) {
                Logger.log(ctx, "Shared clipboard couldn't start: ${e.message}")
            }
        }

        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, ClipService::class.java))

        fun ensureChannel(ctx: Context) {
            val nm = ctx.getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(CHANNEL) == null) {
                nm.createNotificationChannel(
                    NotificationChannel(CHANNEL, "Shared clipboard", NotificationManager.IMPORTANCE_LOW).apply {
                        setShowBadge(false)
                    }
                )
            }
        }

        fun notification(ctx: Context, text: String): Notification {
            ensureChannel(ctx)
            val send = PendingIntent.getActivity(
                ctx, 7, Intent(ctx, ClipSendActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
            )
            val open = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
            return NotificationCompat.Builder(ctx, CHANNEL)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("Shared clipboard is on")
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setSilent(true)
                .setContentIntent(send)
                .addAction(0, "Send copied to laptop", send)
                .addAction(0, "Open app", open)
                .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
                .build()
        }

        fun status(ctx: Context, text: String) {
            try {
                NotificationManagerCompat.from(ctx).notify(ID, notification(ctx, text))
            } catch (e: SecurityException) { }
        }
    }

    @Volatile private var alive = false
    private var thread: Thread? = null
    private val main = Handler(Looper.getMainLooper())
    private val http = OkHttpClient.Builder().connectTimeout(30, TimeUnit.SECONDS).readTimeout(120, TimeUnit.SECONDS).build()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val n = notification(this, "Copy on the laptop → paste here. Tap to send what you copied here.")
        try {
            ServiceCompat.startForeground(
                this, ID, n,
                if (Build.VERSION.SDK_INT >= 34) ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE else 0
            )
        } catch (e: Exception) {
            Logger.log(this, "Shared clipboard couldn't start: ${e.message}")
            stopSelf()
            return START_NOT_STICKY
        }
        if (!Prefs(this).clipboardSync) { stopSelf(); return START_NOT_STICKY }
        if (thread?.isAlive != true) {
            alive = true
            thread = Thread({ loop() }, "clip-receive").also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        alive = false
        thread?.interrupt()
        super.onDestroy()
    }

    private fun loop() {
        val prefs = Prefs(this)
        val sp = getSharedPreferences("clip", MODE_PRIVATE)
        var since = sp.getLong("since", 0L).let { if (it == 0L || System.currentTimeMillis() - it > 3600_000) System.currentTimeMillis() else it }
        val seen = LinkedHashSet<String>(sp.getStringSet("seen", emptySet()) ?: emptySet())
        var lastCleanup = 0L
        var failures = 0
        val pm = getSystemService(PowerManager::class.java)
        while (alive) {
            try {
                if (prefs.token.isEmpty() || !prefs.clipboardSync) break
                val relay = ClipRelay(this)
                for (item in relay.poll(since)) {
                    if (item.id in seen) continue
                    receive(relay, item)
                    seen.add(item.id)
                    while (seen.size > 200) seen.remove(seen.first())
                    try { since = maxOf(since, Instant.parse(item.created).toEpochMilli()) } catch (e: Exception) { }
                    sp.edit().putLong("since", since).putStringSet("seen", HashSet(seen)).apply()
                }
                if (System.currentTimeMillis() - lastCleanup > 3600_000) {
                    lastCleanup = System.currentTimeMillis()
                    try { relay.cleanup() } catch (e: Exception) { }
                }
                if (failures > 0 || !prefs.clipStatus.startsWith("✓")) prefs.clipStatus = "✓ Working — waiting for things copied on the laptop."
                failures = 0
            } catch (e: InterruptedException) {
                break
            } catch (e: Exception) {
                failures++
                val msg = "⚠ Can't reach the clipboard mailbox in Notion: ${e.message}"
                if (prefs.clipStatus != msg) {
                    prefs.clipStatus = msg
                    Logger.log(this, "Shared clipboard: can't reach Notion (${e.message}). Will keep trying.")
                }
            }
            // Quick while the screen is on; gentle on the battery while it's off.
            val wait = when {
                failures > 0 -> minOf(60_000L, 5_000L * failures)
                pm.isInteractive -> 3_000L
                else -> 30_000L
            }
            try { Thread.sleep(wait) } catch (e: InterruptedException) { break }
        }
        main.post { stopSelf() }
    }

    private fun receive(relay: ClipRelay, item: ClipRelay.Item) {
        val cm = getSystemService(ClipboardManager::class.java)
        if (item.kind == "text") {
            lastFp = ClipRelay.fingerprint("text", item.text, null)
            main.post {
                cm.setPrimaryClip(ClipData.newPlainText("From laptop", item.text))
                toast("Copied from laptop: ${item.text.trim().take(40)}")
            }
            status(this, "Got text from the laptop — paste it anywhere.")
            Prefs(this).clipStatus = "✓ Working — last received text at " + java.text.SimpleDateFormat("h:mm a").format(java.util.Date())
            Logger.log(this, "Shared clipboard: got text from the laptop.")
            return
        }
        val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "From laptop")
        dir.mkdirs()
        val files = ArrayList<File>()
        for ((name0, url) in relay.filesOf(item)) {
            val name = name0.replace(Regex("[\\\\/:*?\"<>|]"), "_").ifEmpty { "file" }
            var f = File(dir, name)
            var n = 1
            while (f.exists()) {
                f = File(dir, name.substringBeforeLast('.', name) + " ($n)" + (if (name.contains('.')) "." + name.substringAfterLast('.') else ""))
                n++
            }
            http.newCall(Request.Builder().url(url).build()).execute().use { r ->
                if (!r.isSuccessful) throw RuntimeException("download failed (${r.code})")
                f.outputStream().use { out -> r.body!!.byteStream().copyTo(out) }
            }
            files.add(f)
        }
        if (files.isEmpty()) return
        lastFp = ClipRelay.fingerprint("files", null, files)
        // Let the gallery and file apps see them.
        android.media.MediaScannerConnection.scanFile(this, files.map { it.path }.toTypedArray(), null, null)
        val uris = files.map { FileProvider.getUriForFile(this, "$packageName.files", it) }
        main.post {
            val clip = ClipData.newUri(contentResolver, files.first().name, uris.first())
            for (u in uris.drop(1)) clip.addItem(ClipData.Item(u))
            cm.setPrimaryClip(clip)
            toast(if (files.size == 1) "Copied from laptop: ${files.first().name}" else "Copied ${files.size} files from the laptop")
        }
        status(this, "Got ${files.joinToString(", ") { it.name }.take(60)} — paste it, or find it in Downloads › From laptop.")
        Logger.log(this, "Shared clipboard: ${files.size} file(s) from the laptop (also in Download/From laptop).")
        Prefs(this).clipStatus = "✓ Working — last received ${files.size} file(s) at " + java.text.SimpleDateFormat("h:mm a").format(java.util.Date())
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
}

/** Starts the shared clipboard when the phone turns on. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_LOCKED_BOOT_COMPLETED) {
            ClipService.start(ctx)
        }
    }
}

/** Quick Settings tile: one tap sends what you copied on the phone to the laptop. */
class ClipTile : TileService() {
    override fun onStartListening() {
        super.onStartListening()
        qsTile?.let {
            it.state = if (Prefs(this).clipboardSync) android.service.quicksettings.Tile.STATE_ACTIVE
            else android.service.quicksettings.Tile.STATE_INACTIVE
            it.label = "Send clipboard"
            it.updateTile()
        }
    }

    @Suppress("DEPRECATION")
    override fun onClick() {
        super.onClick()
        val i = Intent(this, ClipSendActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(PendingIntent.getActivity(this, 8, i, PendingIntent.FLAG_IMMUTABLE))
        } else {
            startActivityAndCollapse(i)
        }
    }
}

/** Helper used by both the send screen and the share target. */
object ClipSend {
    /** Copies shared/copied content into the app's cache so it can be uploaded. */
    fun copyIn(ctx: Context, uris: List<Uri>): List<File> {
        val dir = File(ctx.cacheDir, "clipout").apply { mkdirs() }
        dir.listFiles()?.forEach { if (System.currentTimeMillis() - it.lastModified() > 3600_000) it.delete() }
        val out = ArrayList<File>()
        for (u in uris) {
            var name = "file"
            try {
                ctx.contentResolver.query(u, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                    if (c.moveToFirst()) c.getString(0)?.let { name = it }
                }
            } catch (e: Exception) { }
            if (name == "file" && u.scheme == "file") name = File(u.path ?: "file").name
            if (!name.contains('.')) {
                val ext = android.webkit.MimeTypeMap.getSingleton().getExtensionFromMimeType(ctx.contentResolver.getType(u) ?: "")
                if (ext != null) name = "$name.$ext"
            }
            val sub = File(dir, System.nanoTime().toString()).apply { mkdirs() }
            val f = File(sub, name.replace(Regex("[\\\\/:*?\"<>|]"), "_"))
            ctx.contentResolver.openInputStream(u)?.use { inp -> f.outputStream().use { inp.copyTo(it) } } ?: continue
            if (f.length() > ClipRelay.MAX_FILE) { f.delete(); throw IllegalArgumentException("${f.name} is over 100 MB, too big for the clipboard") }
            out.add(f)
        }
        return out
    }

    /** Sends in the background; reports with toasts. */
    fun send(ctx: Context, text: String?, uris: List<Uri>) {
        val app = ctx.applicationContext
        val main = Handler(Looper.getMainLooper())
        fun toast(s: String) = main.post { Toast.makeText(app, s, Toast.LENGTH_SHORT).show() }
        val p = Prefs(app)
        if (p.token.isEmpty() || p.rootPageId.isEmpty()) { toast("Connect Notion in the app first."); return }
        Thread {
            try {
                if (uris.isNotEmpty()) {
                    val files = copyIn(app, uris)
                    if (files.isEmpty()) { toast("Couldn't read what you copied."); return@Thread }
                    val fp = ClipRelay.fingerprint("files", null, files)
                    val images = files.all { FileTypes.blockType(it.name) == "image" }
                    toast(if (files.size == 1) "Sending ${files.first().name} to the laptop…" else "Sending ${files.size} files to the laptop…")
                    ClipRelay(app).send(if (images) "image" else "files", null, files)
                    ClipService.lastFp = fp
                    files.forEach { it.delete() }
                    toast("✓ On the laptop's clipboard — press Ctrl+V")
                    Logger.log(app, "Shared clipboard: sent ${files.size} file(s) to the laptop.")
                    p.clipStatus = "✓ Working — last sent ${files.size} file(s) at " + java.text.SimpleDateFormat("h:mm a").format(java.util.Date())
                } else if (!text.isNullOrEmpty()) {
                    ClipRelay(app).send("text", text, emptyList())
                    ClipService.lastFp = ClipRelay.fingerprint("text", text, null)
                    toast("✓ On the laptop's clipboard — press Ctrl+V")
                    Logger.log(app, "Shared clipboard: sent text to the laptop.")
                    p.clipStatus = "✓ Working — last sent text at " + java.text.SimpleDateFormat("h:mm a").format(java.util.Date())
                } else {
                    toast("Nothing copied yet.")
                }
            } catch (e: Exception) {
                toast("Couldn't send: ${e.message}")
                p.clipStatus = "⚠ Couldn't send: ${e.message}"
                Logger.log(app, "Shared clipboard: couldn't send (${e.message}).")
            }
        }.start()
    }
}
