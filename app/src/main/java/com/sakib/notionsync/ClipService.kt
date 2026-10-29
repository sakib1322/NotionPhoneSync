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
        const val CHANNEL_IN = "clipboard_in"
        const val ID = 77
        @Volatile var lastFp: String = ""
        /** The direct Wi-Fi link to the laptop (null when the service isn't running). */
        @Volatile var lan: LanServer? = null

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
    private var multicast: android.net.wifi.WifiManager.MulticastLock? = null
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
        if (lan == null) {
            val p = Prefs(this)
            val app = applicationContext
            lan = LanServer({ p.token }, { p.clipboardSync && p.token.isNotEmpty() }, ::lanItem, { Logger.log(app, it) }).also { it.start() }
            try {
                val wm = applicationContext.getSystemService(android.net.wifi.WifiManager::class.java)
                multicast = wm.createMulticastLock("notionsync-clip").apply { setReferenceCounted(false); acquire() }
            } catch (e: Exception) { }
        }
        if (thread?.isAlive != true) {
            alive = true
            thread = Thread({ loop() }, "clip-receive").also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        lan?.stop()
        lan = null
        try { multicast?.release() } catch (e: Exception) { }
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
        var lastFast = false
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
                val fast = lan?.connected == true
                if (fast != lastFast) {
                    lastFast = fast
                    status(this, if (fast) "Fast mode: connected to the laptop over Wi-Fi." else "Copy on the laptop → paste here. Tap to send what you copied here.")
                }
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
                pm.isInteractive -> 1_500L
                else -> 30_000L
            }
            try { Thread.sleep(wait) } catch (e: InterruptedException) { break }
        }
        main.post { stopSelf() }
    }

    private fun now() = java.text.SimpleDateFormat("h:mm a").format(java.util.Date())

    private fun putText(text: String, via: String) {
        val cm = getSystemService(ClipboardManager::class.java)
        lastFp = ClipRelay.fingerprint("text", text, null)
        main.post {
            cm.setPrimaryClip(ClipData.newPlainText("From laptop", text))
            toast("Copied from laptop: ${text.trim().take(40)}")
        }
        status(this, "Got text from the laptop — paste it anywhere.")
        Prefs(this).clipStatus = "✓ Working$via — last received text at ${now()}"
        Logger.log(this, "Shared clipboard: got text from the laptop$via.")
    }

    /** Free file names in Download › From laptop. */
    private fun targets(names: List<String>): List<File> {
        val dir = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "From laptop")
        dir.mkdirs()
        val out = ArrayList<File>()
        for (name0 in names) {
            val name = name0.substringAfterLast('/').replace(Regex("[\\\\/:*?\"<>|]"), "_").ifEmpty { "file" }
            var f = File(dir, name)
            var n = 1
            while (f.exists() || f in out) {
                f = File(dir, name.substringBeforeLast('.', name) + " ($n)" + (if (name.contains('.')) "." + name.substringAfterLast('.') else ""))
                n++
            }
            out.add(f)
        }
        return out
    }

    private fun putFiles(files: List<File>, via: String) {
        if (files.isEmpty()) return
        val cm = getSystemService(ClipboardManager::class.java)
        lastFp = ClipRelay.fingerprint("files", null, files)
        // Let the gallery and file apps see them.
        android.media.MediaScannerConnection.scanFile(this, files.map { it.path }.toTypedArray(), null, null)
        val uris = files.map { FileProvider.getUriForFile(this, "$packageName.files", it) }
        val mimes = files.map { FileTypes.mime(it.name) }
        val clip = ClipData(
            android.content.ClipDescription(files.first().name, mimes.distinct().toTypedArray()),
            ClipData.Item(uris.first())
        )
        for (u in uris.drop(1)) clip.addItem(ClipData.Item(u))
        main.post {
            cm.setPrimaryClip(clip)
            toast(if (files.size == 1) "Copied from laptop: ${files.first().name}" else "Copied ${files.size} files from the laptop")
        }
        fileNotification(files, uris, mimes, clip)
        status(this, "Got ${files.joinToString(", ") { it.name }.take(60)} — paste it, or find it in Downloads › From laptop.")
        Logger.log(this, "Shared clipboard: ${files.size} file(s) from the laptop$via (also in Download/From laptop).")
        Prefs(this).clipStatus = "✓ Working$via — last received ${files.size} file(s) at ${now()}"
    }

    /**
     * A pop-up for files that arrive, with Share (e.g. to WhatsApp) and Open. Many apps,
     * WhatsApp included, only accept pasted text and pictures, not documents like PDFs,
     * so sharing is the one-tap way to send those on.
     */
    private fun fileNotification(files: List<File>, uris: List<Uri>, mimes: List<String>, clip: ClipData) {
        try {
            val nm = getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(CHANNEL_IN) == null) {
                nm.createNotificationChannel(NotificationChannel(CHANNEL_IN, "Files from the laptop", NotificationManager.IMPORTANCE_HIGH))
            }
            val type = if (mimes.distinct().size == 1) mimes[0] else "*/*"
            val send = if (uris.size == 1) {
                Intent(Intent.ACTION_SEND).setType(type).putExtra(Intent.EXTRA_STREAM, uris[0])
            } else {
                Intent(Intent.ACTION_SEND_MULTIPLE).setType(type).putParcelableArrayListExtra(Intent.EXTRA_STREAM, ArrayList(uris))
            }
            send.clipData = clip
            send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            val chooser = Intent.createChooser(send, "Send to…").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_GRANT_READ_URI_PERMISSION)
            val nid = 1000 + (System.currentTimeMillis() % 100000).toInt()
            val sharePi = PendingIntent.getActivity(this, nid, chooser, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val b = NotificationCompat.Builder(this, CHANNEL_IN)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle(if (files.size == 1) "From laptop: ${files[0].name}" else "${files.size} files from the laptop")
                .setContentText("Copied. Tap Share to send it on (e.g. WhatsApp), or paste it.")
                .setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .addAction(0, "Share", sharePi)
            if (uris.size == 1) {
                val view = Intent(Intent.ACTION_VIEW).setDataAndType(uris[0], mimes[0])
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                val openPi = PendingIntent.getActivity(this, nid + 1, Intent.createChooser(view, "Open with").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                b.addAction(0, "Open", openPi).setContentIntent(openPi)
            } else {
                b.setContentIntent(sharePi)
            }
            NotificationManagerCompat.from(this).notify(nid, b.build())
        } catch (e: SecurityException) { }
    }

    /** An item the laptop sent over Wi-Fi. */
    private fun lanItem(meta: org.json.JSONObject, link: LanLink): Boolean {
        if (meta.optString("kind") == "text") {
            putText(meta.optString("text"), " (Wi-Fi)")
            return true
        }
        val arr = meta.optJSONArray("files") ?: return true
        val names = (0 until arr.length()).map { arr.getJSONObject(it).optString("name", "file") }
        if (names.isNotEmpty()) main.post { toast(if (names.size == 1) "Receiving ${names[0]} from the laptop…" else "Receiving ${names.size} files from the laptop…") }
        val dest = targets(names)
        var i = 0
        val files = link.recvFiles(arr) { dest[i++] }
        putFiles(files, " (Wi-Fi)")
        return true
    }

    private fun receive(relay: ClipRelay, item: ClipRelay.Item) {
        if (item.kind == "text") { putText(item.text, ""); return }
        val links = relay.filesOf(item)
        if (links.isEmpty()) return
        main.post { toast(if (links.size == 1) "Receiving ${links[0].first} from the laptop…" else "Receiving ${links.size} files from the laptop…") }
        val dest = targets(links.map { it.first })
        // Download all files side by side.
        val pool = java.util.concurrent.Executors.newFixedThreadPool(minOf(4, links.size))
        val files = try {
            links.indices.map { i ->
                pool.submit<File> {
                    val f = dest[i]
                    http.newCall(Request.Builder().url(links[i].second).build()).execute().use { r ->
                        if (!r.isSuccessful) throw RuntimeException("download failed (${r.code})")
                        f.outputStream().use { out -> r.body!!.byteStream().copyTo(out, 1 shl 20) }
                    }
                    f
                }
            }.map { try { it.get() } catch (e: java.util.concurrent.ExecutionException) { throw (e.cause ?: e) } }
        } finally {
            pool.shutdown()
        }
        putFiles(files, "")
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
            val limit = if (ClipService.lan?.connected == true) Lan.MAX_FILE else ClipRelay.MAX_FILE
            if (f.length() > limit) {
                f.delete()
                throw IllegalArgumentException(if (limit == ClipRelay.MAX_FILE) "${f.name} is over 100 MB; big files can only be sent when the laptop is on the same Wi-Fi" else "${f.name} is too big")
            }
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
                    val kind = if (images) "image" else "files"
                    if (ClipService.lan?.send(kind, null, files) == true) {
                        ClipService.lastFp = fp
                        files.forEach { it.delete() }
                        toast("✓ On the laptop's clipboard — press Ctrl+V")
                        Logger.log(app, "Shared clipboard: sent ${files.size} file(s) to the laptop over Wi-Fi.")
                        p.clipStatus = "✓ Working (Wi-Fi) — last sent ${files.size} file(s) at " + java.text.SimpleDateFormat("h:mm a").format(java.util.Date())
                        return@Thread
                    }
                    if (files.any { it.length() > ClipRelay.MAX_FILE }) {
                        toast("Files over 100 MB can only be sent when the laptop is on the same Wi-Fi.")
                        return@Thread
                    }
                    toast(if (files.size == 1) "Sending ${files.first().name} to the laptop…" else "Sending ${files.size} files to the laptop…")
                    ClipRelay(app).send(kind, null, files)
                    ClipService.lastFp = fp
                    files.forEach { it.delete() }
                    toast("✓ On the laptop's clipboard — press Ctrl+V")
                    Logger.log(app, "Shared clipboard: sent ${files.size} file(s) to the laptop.")
                    p.clipStatus = "✓ Working — last sent ${files.size} file(s) at " + java.text.SimpleDateFormat("h:mm a").format(java.util.Date())
                } else if (!text.isNullOrEmpty()) {
                    if (ClipService.lan?.send("text", text, emptyList()) == true) {
                        ClipService.lastFp = ClipRelay.fingerprint("text", text, null)
                        toast("✓ On the laptop's clipboard — press Ctrl+V")
                        Logger.log(app, "Shared clipboard: sent text to the laptop over Wi-Fi.")
                        p.clipStatus = "✓ Working (Wi-Fi) — last sent text at " + java.text.SimpleDateFormat("h:mm a").format(java.util.Date())
                        return@Thread
                    }
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
