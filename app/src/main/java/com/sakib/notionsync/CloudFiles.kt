package com.sakib.notionsync

import android.content.Context
import android.os.Environment
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.util.concurrent.TimeUnit

/** Getting a cloud file's content: the phone copy if it exists, otherwise from Notion. */
object CloudFiles {
    private val http = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    fun local(f: FilesPage.FileItem): File? {
        val rel = f.localPath ?: return null
        return File(Environment.getExternalStorageDirectory(), rel).takeIf { it.exists() }
    }

    fun cacheFile(ctx: Context, f: FilesPage.FileItem): File {
        val safe = f.name.replace(Regex("[\\\\/:*?\"<>|]"), "_")
        return File(ctx.cacheDir, "open/${f.block}/$safe")
    }

    /** A working download link (Notion's links expire after an hour). */
    fun freshUrl(ctx: Context, f: FilesPage.FileItem): String? {
        if (f.url != null && System.currentTimeMillis() - f.fetchedAt < 50 * 60_000) return f.url
        val b = NotionClient(Prefs(ctx).token).block(f.block)
        val inner = b.optJSONObject(b.optString("type"))
        f.url = inner?.optJSONObject("file")?.optString("url")?.takeIf { !it.isNullOrEmpty() }
            ?: inner?.optJSONObject("external")?.optString("url")?.takeIf { !it.isNullOrEmpty() }
        f.fetchedAt = System.currentTimeMillis()
        return f.url
    }

    /** Returns the file on the phone, downloading it if needed. Runs on a background thread. */
    fun obtain(ctx: Context, f: FilesPage.FileItem, progress: (Long, Long) -> Unit = { _, _ -> }): File {
        local(f)?.let { return it }
        val dest = cacheFile(ctx, f)
        if (dest.exists() && dest.length() > 0) return dest
        val url = freshUrl(ctx, f) ?: throw IllegalStateException("No download link")
        dest.parentFile?.mkdirs()
        val part = File(dest.path + ".part")
        http.newCall(Request.Builder().url(url).build()).execute().use { r ->
            if (!r.isSuccessful) throw IllegalStateException("Download failed (${r.code})")
            val body = r.body ?: throw IllegalStateException("Empty download")
            val total = body.contentLength()
            body.byteStream().use { input ->
                part.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    var done = 0L
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        progress(done, total)
                    }
                }
            }
        }
        part.renameTo(dest)
        return dest
    }
}
