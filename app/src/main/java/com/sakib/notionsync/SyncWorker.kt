package com.sakib.notionsync

import android.content.Context
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Environment
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import java.nio.file.FileVisitResult
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.SimpleFileVisitor
import java.nio.file.attribute.BasicFileAttributes
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.coroutines.cancellation.CancellationException

/**
 * One sync run: scan the phone, find files not yet in Notion (or changed
 * since), and upload them into pages that mirror the phone's folders.
 */
class SyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    companion object {
        private val oneAtATime = Mutex()
        private const val MAX_SIZE = 5L * 1024 * 1024 * 1024
    }

    private val ctx: Context get() = applicationContext
    private val folderCache = HashMap<String, String>()
    private val childCache = HashMap<String, MutableMap<String, String>>()
    private val dateFmt = SimpleDateFormat("d MMM yyyy, HH:mm", Locale.US)
    private lateinit var client: NotionClient
    @Volatile private var activeClient: NotionClient? = null
    private lateinit var db: Db
    private lateinit var rootId: String
    private lateinit var prefs: Prefs
    private val captionCache = HashMap<String, Map<String, String>>()

    private fun beat() {
        if (::prefs.isInitialized) prefs.heartbeat = System.currentTimeMillis()
    }

    private fun log(msg: String) {
        Logger.log(ctx, msg)
        beat()
    }

    private fun progress(text: String, done: Int, total: Int) {
        Notif.update(ctx, text, done, total)
        beat()
    }

    override suspend fun getForegroundInfo(): ForegroundInfo = fgInfo("Getting ready…")

    private fun fgInfo(text: String): ForegroundInfo {
        val n = Notif.build(ctx, text, 0, 0)
        return if (Build.VERSION.SDK_INT >= 29) {
            ForegroundInfo(Notif.ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            ForegroundInfo(Notif.ID, n)
        }
    }

    override suspend fun doWork(): Result {
        if (!oneAtATime.tryLock()) return Result.success()
        prefs = Prefs(ctx)
        prefs.running = true
        beat()
        try {
            try {
                setForeground(fgInfo("Scanning your phone…"))
            } catch (e: Exception) {
                log("Running without a progress notification. Tap \"Let it run in the background\" for long uploads.")
            }
            coroutineScope {
                // When the sync is stopped, abort the upload in progress right away
                // instead of waiting for the current file to finish.
                val watcher = launch {
                    try {
                        awaitCancellation()
                    } finally {
                        activeClient?.cancel()
                    }
                }
                withContext(Dispatchers.IO) { sync(prefs) }
                watcher.cancel()
            }
        } catch (e: CancellationException) {
            log("Sync stopped.")
            throw e
        } catch (e: Exception) {
            log("Sync stopped: ${e.message ?: e.javaClass.simpleName}")
        } finally {
            prefs.running = false
            prefs.lastRun = System.currentTimeMillis()
            Notif.cancel(ctx)
            oneAtATime.unlock()
        }
        return Result.success()
    }

    private fun relPath(rootPath: String, f: File) = f.absolutePath.removePrefix(rootPath).trimStart('/')

    private fun sync(p: Prefs) {
        if (p.token.isBlank() || p.rootPageId.isBlank()) {
            log("Not set up yet: add your Notion secret and page link.")
            return
        }
        if (!Perms.hasStorage(ctx)) {
            log("No permission to read files. Tap \"Allow access to all files\".")
            return
        }
        log("Starting sync. Checking the Notion page…")
        client = NotionClient(p.token)
        activeClient = client
        if (isStopped) client.cancel()
        db = Db.get(ctx)
        rootId = p.rootPageId
        try {
            client.pageTitle(rootId)
        } catch (e: NotionException) {
            log(
                when (e.status) {
                    401 -> "Notion rejected the secret. Paste it again in step 2."
                    404 -> "Can't open your Notion page. Make sure it's connected to your integration."
                    else -> "Notion error: ${e.message}"
                }
            )
            return
        }

        // 1. Scan the phone for new or changed files.
        val storage = Environment.getExternalStorageDirectory()
        val rootPath = storage.absolutePath
        val excluded = p.excludedList()
        val known = db.loadAll()
        // Nothing recorded yet (fresh install or reinstall): check Notion for
        // files that are already there, so nothing is uploaded twice.
        if (known.isEmpty()) p.checkNotion = true
        val now = System.currentTimeMillis()
        val found = ArrayList<Pair<File, Long>>()
        var checked = 0
        log("Scanning your phone for files…")
        progress("Scanning your phone…", 0, 0)

        // walkFileTree reads each file's details in one call, which is much
        // faster than asking the phone separately for size, date and type.
        Files.walkFileTree(storage.toPath(), object : SimpleFileVisitor<Path>() {
            override fun preVisitDirectory(dir: Path, attrs: BasicFileAttributes): FileVisitResult {
                if (isStopped) return FileVisitResult.TERMINATE
                val rel = relPath(rootPath, dir.toFile())
                if (rel.isEmpty()) return FileVisitResult.CONTINUE
                val name = dir.fileName?.toString() ?: ""
                if (p.skipHidden && name.startsWith(".")) return FileVisitResult.SKIP_SUBTREE
                if (excluded.any { rel.equals(it, true) || rel.startsWith("$it/", true) }) {
                    return FileVisitResult.SKIP_SUBTREE
                }
                if (rel.count { it == '/' } >= 40) return FileVisitResult.SKIP_SUBTREE
                return FileVisitResult.CONTINUE
            }

            override fun visitFile(file: Path, attrs: BasicFileAttributes): FileVisitResult {
                if (!attrs.isRegularFile) return FileVisitResult.CONTINUE
                val name = file.fileName?.toString() ?: return FileVisitResult.CONTINUE
                if (p.skipHidden && name.startsWith(".")) return FileVisitResult.CONTINUE
                checked++
                if (checked % 2000 == 0) {
                    progress("Scanning… $checked files checked", 0, 0)
                    if (checked % 20000 == 0) log("Checked $checked files so far…")
                    if (isStopped) return FileVisitResult.TERMINATE
                }
                val size = attrs.size()
                val mtime = attrs.lastModifiedTime().toMillis()
                // Skip empty files and files still being written.
                if (size == 0L || now - mtime < 60_000) return FileVisitResult.CONTINUE
                val f = file.toFile()
                val r = known[relPath(rootPath, f)]
                if (r == null || r.status == "retry" || r.size != size || r.mtime != mtime) found.add(f to mtime)
                return FileVisitResult.CONTINUE
            }

            override fun visitFileFailed(file: Path, exc: IOException): FileVisitResult = FileVisitResult.CONTINUE
        })
        if (isStopped) return
        log("Scan done: checked $checked files.")

        val todo = ArrayList(found.sortedByDescending { it.second }.map { it.first })
        p.lastPending = todo.size
        if (todo.isEmpty()) {
            p.checkNotion = false
            log("Everything is up to date.")
            return
        }
        log("Found ${todo.size} new or changed file(s).")

        // 2. Upload them, newest first.
        var networkFailures = 0
        for ((i, f) in todo.withIndex()) {
            if (isStopped) return
            if (p.wifiOnly && Perms.onMeteredNetwork(ctx)) {
                log("Paused: \"Upload only on Wi-Fi\" is on and the phone isn't on Wi-Fi.")
                return
            }
            val rel = relPath(rootPath, f)
            if (!f.exists()) continue
            val size = f.length()
            val mtime = f.lastModified()
            val old = known[rel]
            progress("${i + 1} of ${todo.size}: ${f.name}", i, todo.size)
            if (old == null && p.checkNotion) {
                val existing = try { alreadyInNotion(rel, f.name, mtime) } catch (e: Exception) { null }
                if (existing != null) {
                    db.save(rel, size, mtime, "done", existing, null, 0)
                    log("Already in Notion: $rel")
                    p.lastPending = todo.size - i - 1
                    continue
                }
            }
            try {
                uploadOne(f, rel, size, mtime, old)
                networkFailures = 0
                log("Uploaded $rel")
            } catch (e: NotionException) {
                if (e.status == 401 || e.status == 403) {
                    log("Notion refused access (${e.message}). Stopping.")
                    return
                }
                if (e.status in 400..499) {
                    db.save(rel, size, mtime, "skipped", old?.blockId, e.message, 0)
                    log("Skipped $rel: ${e.message}")
                } else {
                    retryLater(rel, size, mtime, old, e.message)
                }
            } catch (e: IOException) {
                // Stopped by you or by Android: not the file's fault, so don't count it as a failure.
                if (isStopped) return
                if (!f.exists()) continue
                retryLater(rel, size, mtime, old, e.message)
                networkFailures++
                if (networkFailures >= 3) {
                    log("Connection problem. Will try again on the next run.")
                    return
                }
            }
            p.lastPending = todo.size - i - 1
        }
        p.checkNotion = false
        log("Sync finished.")
    }

    /** If this file is already on its Notion folder page, returns that block's ID. */
    private fun alreadyInNotion(rel: String, name: String, mtime: Long): String? {
        val pageId = existingFolderPage(rel.substringBeforeLast('/', "")) ?: return null
        val captions = captionCache.getOrPut(pageId) { client.mediaCaptions(pageId) }
        return captions["$name · ${dateFmt.format(Date(mtime))}"]
    }

    /** Like folderPage(), but only finds pages; never creates them. */
    private fun existingFolderPage(relDir: String): String? {
        if (relDir.isEmpty()) return rootId
        folderCache[relDir]?.let { return it }
        db.folderPage(relDir)?.let {
            folderCache[relDir] = it
            return it
        }
        val parentId = existingFolderPage(relDir.substringBeforeLast('/', "")) ?: return null
        val kids = childCache.getOrPut(parentId) { client.childPages(parentId) }
        val id = kids[relDir.substringAfterLast('/')] ?: return null
        db.setFolderPage(relDir, id)
        folderCache[relDir] = id
        return id
    }

    private fun retryLater(rel: String, size: Long, mtime: Long, old: Db.Rec?, msg: String?) {
        val attempts = (old?.attempts ?: 0) + 1
        val status = if (attempts >= 5) "failed" else "retry"
        db.save(rel, size, mtime, status, old?.blockId, msg, attempts)
        log("Couldn't upload $rel (${msg ?: "error"}). ${if (status == "failed") "Gave up after 5 tries." else "Will retry."}")
    }

    private fun uploadOne(f: File, rel: String, size: Long, mtime: Long, old: Db.Rec?) {
        if (size > MAX_SIZE) throw NotionException(413, "too_large", "Larger than Notion's 5 GB limit")
        val relDir = rel.substringBeforeLast('/', "")
        val upName = FileTypes.uploadName(f.name)
        val mime = FileTypes.mime(upName)
        val type = FileTypes.blockType(upName)
        val uploadId = client.uploadFile(f, upName, mime, size) { beat() }
        val caption = "${f.name} · ${dateFmt.format(Date(mtime))}"
        val blockId = attach(relDir, uploadId, type, caption)
        // If this file was uploaded before and has since changed, remove the old copy.
        old?.blockId?.let { oldBlock ->
            try { client.deleteBlock(oldBlock) } catch (e: Exception) { }
        }
        db.save(rel, size, mtime, "done", blockId, null, 0)
    }

    private fun attach(relDir: String, uploadId: String, type: String, caption: String): String {
        val pageId = folderPage(relDir)
        return try {
            client.attach(pageId, uploadId, type, caption)
        } catch (e: NotionException) {
            when {
                e.status == 404 -> {
                    // A folder page was deleted in Notion: rebuild the folder map and try again.
                    db.clearFolders()
                    folderCache.clear()
                    childCache.clear()
                    client.attach(folderPage(relDir), uploadId, type, caption)
                }
                e.status == 400 && type != "file" -> client.attach(pageId, uploadId, "file", caption)
                else -> throw e
            }
        }
    }

    /** Finds or creates the Notion page for a phone folder, mirroring the full folder path. */
    private fun folderPage(relDir: String): String {
        if (relDir.isEmpty()) return rootId
        folderCache[relDir]?.let { return it }
        db.folderPage(relDir)?.let {
            folderCache[relDir] = it
            return it
        }
        val parentId = folderPage(relDir.substringBeforeLast('/', ""))
        val name = relDir.substringAfterLast('/')
        val kids = childCache.getOrPut(parentId) { client.childPages(parentId) }
        val id = kids[name] ?: client.createPage(parentId, name).also { kids[name] = it }
        db.setFolderPage(relDir, id)
        folderCache[relDir] = id
        return id
    }
}
