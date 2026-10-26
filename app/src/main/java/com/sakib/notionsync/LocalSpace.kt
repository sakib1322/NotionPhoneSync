package com.sakib.notionsync

import android.content.Context
import java.io.File

/**
 * Keeps the app small: copies of cloud files you opened (and photo previews) are kept
 * within a limit and can be cleared. Your own files on the phone are never touched.
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

    /** Removes the downloaded copy of one cloud file (the cloud copy and your own files are untouched). */
    fun removeDownload(ctx: Context, block: String): Boolean {
        val d = File(downloadsDir(ctx), block)
        return d.exists() && d.deleteRecursively()
    }

    fun isDownloaded(ctx: Context, block: String): Boolean =
        File(downloadsDir(ctx), block).listFiles()?.any { it.isFile && !it.name.endsWith(".part") } == true
}
