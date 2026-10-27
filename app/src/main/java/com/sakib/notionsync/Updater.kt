package com.sakib.notionsync

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import androidx.core.content.pm.PackageInfoCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Checks GitHub for a newer build of this app, downloads it and opens the installer. */
object Updater {
    private const val REPO = "sakib1322/NotionPhoneSync"

    data class Release(val build: Int, val url: String, val size: Long)

    private val http = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    fun currentBuild(ctx: Context): Int =
        PackageInfoCompat.getLongVersionCode(ctx.packageManager.getPackageInfo(ctx.packageName, 0)).toInt()

    fun latest(): Release? {
        val req = Request.Builder()
            .url("https://api.github.com/repos/$REPO/releases/latest")
            .header("Accept", "application/vnd.github+json")
            .build()
        http.newCall(req).execute().use { r ->
            if (r.code == 404) return null
            if (!r.isSuccessful) throw IOException("GitHub answered ${r.code}")
            val j = JSONObject(r.body?.string() ?: "{}")
            val build = j.optString("tag_name").removePrefix("build-").toIntOrNull() ?: return null
            val assets = j.optJSONArray("assets") ?: return null
            for (i in 0 until assets.length()) {
                val a = assets.getJSONObject(i)
                if (a.optString("name").endsWith(".apk")) {
                    return Release(build, a.getString("browser_download_url"), a.optLong("size"))
                }
            }
            return null
        }
    }

    fun download(ctx: Context, rel: Release, onProgress: (Int) -> Unit): File {
        val dir = File(ctx.cacheDir, "updates")
        dir.mkdirs()
        val out = File(dir, "update.apk")
        http.newCall(Request.Builder().url(rel.url).build()).execute().use { r ->
            if (!r.isSuccessful) throw IOException("Download failed (${r.code})")
            val body = r.body ?: throw IOException("Empty download")
            val total = body.contentLength().takeIf { it > 0 } ?: rel.size
            body.byteStream().use { input ->
                out.outputStream().use { output ->
                    val buf = ByteArray(64 * 1024)
                    var done = 0L
                    var lastPct = -1
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        output.write(buf, 0, n)
                        done += n
                        if (total > 0) {
                            val pct = (done * 100 / total).toInt()
                            if (pct != lastPct) {
                                lastPct = pct
                                onProgress(pct)
                            }
                        }
                    }
                }
            }
        }
        return out
    }

    fun install(activity: Activity, apk: File) {
        val uri = FileProvider.getUriForFile(activity, "${activity.packageName}.files", apk)
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        activity.startActivity(intent)
    }
}

/** Runs right after the app is updated: picks the backup straight back up. */
class UpdateReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val p = Prefs(ctx)
        p.running = false
        Logger.log(ctx, "App updated to build ${Updater.currentBuild(ctx)}. Resuming backup.")
        ClipService.start(ctx)
        if (p.autoSync) {
            Scheduler.apply(ctx)
            Scheduler.syncNow(ctx)
        }
    }
}
