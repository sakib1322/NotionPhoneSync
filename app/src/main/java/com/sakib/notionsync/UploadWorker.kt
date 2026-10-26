package com.sakib.notionsync

import android.content.Context
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.provider.OpenableColumns
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException

/** Uploads files added in the app or through the Share menu. */
class UploadWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {

    companion object {
        private const val NAME = "manual-uploads"
        private const val NOTIF_ID = 43

        fun outboxDir(ctx: Context) = File(ctx.filesDir, "outbox").apply { mkdirs() }

        /** Copies the chosen files into the app (so they can be uploaded later) and queues them. */
        fun queue(ctx: Context, uris: List<Uri>, root: String, path: String, progress: (Int) -> Unit = {}): Int {
            val db = Db.get(ctx)
            var n = 0
            for (uri in uris) {
                try {
                    var name = "file"
                    ctx.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                        if (c.moveToFirst()) name = c.getString(0) ?: name
                    }
                    if (name == "file") name = uri.lastPathSegment?.substringAfterLast('/') ?: name
                    val dest = File(outboxDir(ctx), "${System.nanoTime()}_${name.replace(Regex("[\\\\/:*?\"<>|]"), "_")}")
                    ctx.contentResolver.openInputStream(uri)?.use { input -> dest.outputStream().use { input.copyTo(it) } }
                        ?: continue
                    db.queueUpload(dest.path, name, dest.length(), root, path)
                    n++
                    progress(n)
                } catch (e: Exception) {
                    Logger.log(ctx, "Couldn't add ${uri.lastPathSegment} for upload: ${e.message}")
                }
            }
            if (n > 0) start(ctx)
            return n
        }

        fun start(ctx: Context) {
            val req = OneTimeWorkRequestBuilder<UploadWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                .build()
            WorkManager.getInstance(ctx).enqueueUniqueWork(NAME, ExistingWorkPolicy.APPEND_OR_REPLACE, req)
        }
    }

    private fun fg(text: String): ForegroundInfo {
        val n = Notif.build(applicationContext, text, 0, 0)
        return if (Build.VERSION.SDK_INT >= 29) ForegroundInfo(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        else ForegroundInfo(NOTIF_ID, n)
    }

    override suspend fun getForegroundInfo(): ForegroundInfo = fg("Uploading your files…")

    override suspend fun doWork(): Result {
        val ctx = applicationContext
        val db = Db.get(ctx)
        try {
            setForeground(fg("Uploading your files…"))
        } catch (e: Exception) {
        }
        var failures = 0
        withContext(Dispatchers.IO) {
            val ops = CloudOps(ctx)
            val items = db.queuedUploads()
            for ((i, it) in items.withIndex()) {
                if (isStopped) break
                val f = File(it.file)
                if (!f.exists()) {
                    db.finishUpload(it.id, "failed", "File missing")
                    continue
                }
                try {
                    setForeground(fg("Uploading ${i + 1} of ${items.size}: ${it.name}"))
                } catch (e: Exception) {
                }
                try {
                    ops.upload(f, it.name, it.root, it.path) {}
                    db.finishUpload(it.id, "done", null)
                    f.delete()
                    Logger.log(ctx, "Uploaded from the app: ${it.name}")
                } catch (e: NotionException) {
                    if (e.status in 400..499) {
                        db.finishUpload(it.id, "failed", e.message)
                        f.delete()
                        Logger.log(ctx, "Skipped ${it.name}: ${e.message}")
                    } else failures++
                } catch (e: IOException) {
                    failures++
                    Logger.log(ctx, "Couldn't upload ${it.name} (${e.message}). Will retry.")
                    break
                }
            }
        }
        return if (failures > 0 && db.countQueuedUploads() > 0) Result.retry() else Result.success()
    }
}
