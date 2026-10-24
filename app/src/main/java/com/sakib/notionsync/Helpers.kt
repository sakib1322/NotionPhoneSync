package com.sakib.notionsync

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Environment
import android.os.PowerManager
import android.webkit.MimeTypeMap
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

object FileTypes {
    private val images = setOf("jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "svg", "tif", "tiff", "ico", "bmp")
    private val videos = setOf("mp4", "mov", "m4v", "webm", "avi", "mkv", "3gp", "wmv", "flv", "mpeg", "mpg", "qt")
    private val audio = setOf("mp3", "m4a", "aac", "wav", "ogg", "oga", "wma", "mid", "midi", "flac", "amr", "m4b", "mpga")

    // Content types matched to what Notion accepts.
    private val known = mapOf(
        "jpg" to "image/jpeg", "jpeg" to "image/jpeg", "png" to "image/png", "gif" to "image/gif",
        "webp" to "image/webp", "heic" to "image/heic", "heif" to "image/heif", "svg" to "image/svg+xml",
        "tif" to "image/tiff", "tiff" to "image/tiff", "ico" to "image/vnd.microsoft.icon",
        "mp4" to "video/mp4", "m4v" to "video/mp4", "mov" to "video/quicktime", "webm" to "video/webm",
        "avi" to "video/x-msvideo", "wmv" to "video/x-ms-wmv",
        "mp3" to "audio/mpeg", "m4a" to "audio/mp4", "m4b" to "audio/mp4", "aac" to "audio/aac",
        "wav" to "audio/wav", "ogg" to "audio/ogg", "oga" to "audio/ogg", "wma" to "audio/x-ms-wma",
        "mid" to "audio/midi", "midi" to "audio/midi",
        "pdf" to "application/pdf", "txt" to "text/plain", "csv" to "text/csv", "json" to "application/json",
        "doc" to "application/msword",
        "docx" to "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls" to "application/vnd.ms-excel",
        "xlsx" to "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ppt" to "application/vnd.ms-powerpoint",
        "pptx" to "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )

    fun ext(name: String) = name.substringAfterLast('.', "").lowercase(Locale.ROOT)

    /** WhatsApp voice notes (.opus) are Ogg audio; Notion accepts them as .ogg. */
    fun uploadName(name: String): String = if (ext(name) == "opus") "$name.ogg" else name

    fun mime(name: String): String {
        val e = ext(name)
        return known[e] ?: MimeTypeMap.getSingleton().getMimeTypeFromExtension(e) ?: "application/octet-stream"
    }

    fun blockType(name: String): String {
        val e = ext(name)
        return when {
            e in images -> "image"
            e in videos -> "video"
            e in audio -> "audio"
            e == "pdf" -> "pdf"
            else -> "file"
        }
    }
}

object Perms {
    fun hasStorage(ctx: Context): Boolean =
        if (Build.VERSION.SDK_INT >= 30) Environment.isExternalStorageManager()
        else ctx.checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED

    fun notifications(ctx: Context): Boolean =
        if (Build.VERSION.SDK_INT >= 33)
            ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        else true

    fun battery(ctx: Context): Boolean =
        (ctx.getSystemService(Context.POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(ctx.packageName)
}

object Notif {
    const val CHANNEL = "sync"
    const val ID = 42

    fun ensureChannel(ctx: Context) {
        val nm = ctx.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL, "Sync progress", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    fun build(ctx: Context, text: String, done: Int, total: Int): Notification {
        ensureChannel(ctx)
        val pi = PendingIntent.getActivity(
            ctx, 0, Intent(ctx, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val b = NotificationCompat.Builder(ctx, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Syncing to Notion")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pi)
        if (total > 0) b.setProgress(total, done, false)
        return b.build()
    }

    fun update(ctx: Context, text: String, done: Int, total: Int) {
        try {
            NotificationManagerCompat.from(ctx).notify(ID, build(ctx, text, done, total))
        } catch (e: SecurityException) {
            // Notifications not allowed; sync continues without them.
        }
    }

    fun cancel(ctx: Context) {
        NotificationManagerCompat.from(ctx).cancel(ID)
    }
}

object Logger {
    private const val FILE = "sync.log"
    private val fmt = SimpleDateFormat("dd MMM HH:mm:ss", Locale.US)

    @Synchronized
    fun log(ctx: Context, msg: String) {
        val f = File(ctx.filesDir, FILE)
        f.appendText("${fmt.format(Date())}  $msg\n")
        if (f.length() > 300_000) {
            val keep = f.readText().takeLast(150_000).substringAfter('\n')
            f.writeText(keep)
        }
    }

    @Synchronized
    fun tail(ctx: Context, lines: Int): String {
        val f = File(ctx.filesDir, FILE)
        if (!f.exists()) return "Nothing yet."
        return f.readLines().takeLast(lines).reversed().joinToString("\n")
    }
}

object Scheduler {
    private const val AUTO = "auto-sync"
    private const val NOW = "sync-now"

    fun apply(ctx: Context) {
        val p = Prefs(ctx)
        val wm = WorkManager.getInstance(ctx)
        if (!p.autoSync) {
            wm.cancelUniqueWork(AUTO)
            return
        }
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(if (p.wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
            .setRequiresCharging(p.chargingOnly)
            .build()
        val req = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(constraints)
            .build()
        wm.enqueueUniquePeriodicWork(AUTO, ExistingPeriodicWorkPolicy.UPDATE, req)
    }

    fun syncNow(ctx: Context) {
        val req = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .build()
        WorkManager.getInstance(ctx).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, req)
    }
}
