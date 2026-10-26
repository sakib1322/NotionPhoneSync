package com.sakib.notionsync

import android.content.Context
import android.media.MediaScannerConnection
import android.os.Environment
import java.io.File

/**
 * Keeps the app small and frees space on the phone:
 *  - downloads (files opened from the cloud) and previews are kept within a limit;
 *  - files already safely in the cloud can be removed from the phone ("offloaded").
 */
object LocalSpace {

    const val THUMB_LIMIT = 300L * 1024 * 1024

    fun downloadsDir(ctx: Context) = File(ctx.cacheDir, "open")
    fun thumbsDir(ctx: Context) = File(ctx.cacheDir, "thumbs")

    fun sizeOf(dir: File): Long = dir.walkBottomUp().filter { it.isFile }.sumOf { it.length() }

    data class AppUsage(val downloads: Long, val previews: Long, val pendingUploads: Long, val other: Long) {
        val total get() = downloads + previews + pendingUploads + other
    }

    fun appUsage(ctx: Context): AppUsage {
        val dl = sizeOf(downloadsDir(ctx))
        val th = sizeOf(thumbsDir(ctx))
        val out = sizeOf(UploadWorker.outboxDir(ctx))
        val all = sizeOf(ctx.cacheDir) + sizeOf(ctx.filesDir) +
            (ctx.getDatabasePath("sync.db").parentFile?.let { sizeOf(it) } ?: 0)
        return AppUsage(dl, th, out, (all - dl - th - out).coerceAtLeast(0))
    }

    /** Removes the least recently used files in [dir] until it is under [limit] bytes. */
    @Synchronized
    fun trim(dir: File, limit: Long) {
        // Files used in the last 10 minutes are kept (they may be open right now).
        val recent = System.currentTimeMillis() - 10 * 60_000
        val files = dir.walkBottomUp().filter { it.isFile && !it.name.endsWith(".part") }.toMutableList()
        var total = files.sumOf { it.length() }
        if (total <= limit) return
        files.sortBy { it.lastModified() }
        for (f in files) {
            if (total <= limit) break
            if (f.lastModified() > recent) continue
            total -= f.length()
            f.delete()
            f.parentFile?.takeIf { it != dir && (it.list()?.isEmpty() == true) }?.delete()
        }
    }

    fun trimAll(ctx: Context) {
        trim(downloadsDir(ctx), Prefs(ctx).cacheLimitMb * 1024L * 1024)
        trim(thumbsDir(ctx), THUMB_LIMIT)
    }

    fun clearDownloads(ctx: Context) {
        downloadsDir(ctx).deleteRecursively()
    }

    fun clearPreviews(ctx: Context) {
        thumbsDir(ctx).deleteRecursively()
    }

    /** The biggest downloaded files, for the "what's downloaded" list. */
    fun downloads(ctx: Context): List<File> =
        downloadsDir(ctx).walkBottomUp().filter { it.isFile && !it.name.endsWith(".part") }
            .sortedByDescending { it.length() }.toList()

    // ---- freeing space on the phone ---------------------------------------------------------

    data class Candidate(val rel: String, val file: File, val size: Long, val block: String, val page: String)

    data class Filter(val photosVideosOnly: Boolean, val olderThanDays: Int, val folders: Set<String>?)

    private val media = setOf("Photo", "Video")

    /**
     * Phone files that are backed up and unchanged since (so the cloud copy is identical).
     * Runs on a background thread (it checks each file on the phone).
     */
    fun candidates(ctx: Context, filter: Filter): List<Candidate> {
        val db = Db.get(ctx)
        val root = Environment.getExternalStorageDirectory()
        val cutoff = if (filter.olderThanDays > 0) System.currentTimeMillis() - filter.olderThanDays * 86_400_000L else Long.MAX_VALUE
        val out = ArrayList<Candidate>()
        for (r in db.offloadable()) {
            if (filter.photosVideosOnly && CloudIndex.kind(r.path.substringAfterLast('/')) !in media) continue
            if (filter.folders != null && r.path.substringBefore('/') !in filter.folders) continue
            if (r.mtime > cutoff) continue
            val f = File(root, r.path)
            if (!f.isFile || f.length() != r.size || f.lastModified() != r.mtime) continue
            val relDir = r.path.substringBeforeLast('/', "")
            val page = if (relDir.isEmpty()) Prefs(ctx).rootPageId else (db.folderPage(relDir) ?: continue)
            out.add(Candidate(r.path, f, r.size, r.block, page))
        }
        return out
    }

    /** (files, bytes) that "Free up space" could remove right now, by top-level folder. */
    fun summary(cands: List<Candidate>): Map<String, Pair<Int, Long>> {
        val m = LinkedHashMap<String, Pair<Int, Long>>()
        for (c in cands.sortedByDescending { it.size }) {
            val top = c.rel.substringBefore('/').ifEmpty { "(top level)" }
            val (n, b) = m[top] ?: (0 to 0L)
            m[top] = (n + 1) to (b + c.size)
        }
        return m.toList().sortedByDescending { it.second.second }.toMap()
    }

    /**
     * Confirms each file is really in Notion (by listing its folder page), then removes
     * the phone copy. Returns (files removed, bytes freed).
     */
    fun offload(ctx: Context, cands: List<Candidate>, isStopped: () -> Boolean, progress: (Int, Int, String) -> Unit): Pair<Int, Long> {
        val db = Db.get(ctx)
        val client = NotionClient(Prefs(ctx).token)
        var removed = 0
        var bytes = 0L
        val done = ArrayList<String>()
        val byPage = cands.groupBy { it.page }
        var checked = 0
        for ((page, list) in byPage) {
            if (isStopped()) break
            // Every block on this page, to confirm the uploads are still there.
            val present = HashSet<String>()
            var cursor: String? = null
            do {
                val res = client.children(page, cursor)
                res.results.forEach { present.add(it.optString("id")) }
                cursor = res.next
                progress(checked, cands.size, "Checking the cloud copies…")
            } while (cursor != null && !isStopped())
            for (c in list) {
                if (isStopped()) break
                checked++
                progress(checked, cands.size, c.file.name)
                if (c.block !in present) continue
                if (!c.file.isFile || c.file.length() != c.size) continue
                if (c.file.delete()) {
                    db.markOffloaded(c.rel)
                    removed++
                    bytes += c.size
                    done.add(c.file.path)
                }
            }
        }
        // Let the Gallery and other apps know the files are gone from the phone.
        if (done.isNotEmpty()) {
            done.chunked(500).forEach { MediaScannerConnection.scanFile(ctx, it.toTypedArray(), null, null) }
        }
        Logger.log(ctx, "Freed ${SpeedMeter.size(bytes)} on the phone: removed $removed backed-up file(s). They stay in the cloud.")
        return removed to bytes
    }

    /** Removes one backed-up file from the phone after confirming its cloud copy exists. */
    fun offloadOne(ctx: Context, rel: String, block: String): Boolean {
        val f = File(Environment.getExternalStorageDirectory(), rel)
        val rec = Db.get(ctx).loadAll()[rel] ?: return false
        if (rec.status != "done" || rec.blockId != block) return false
        if (!f.isFile || f.length() != rec.size || f.lastModified() != rec.mtime) return false
        val b = NotionClient(Prefs(ctx).token).block(block)
        if (b.optBoolean("archived") || b.optBoolean("in_trash")) return false
        if (!f.delete()) return false
        Db.get(ctx).markOffloaded(rel)
        MediaScannerConnection.scanFile(ctx, arrayOf(f.path), null, null)
        Logger.log(ctx, "Removed from the phone (kept in the cloud): $rel")
        return true
    }
}
