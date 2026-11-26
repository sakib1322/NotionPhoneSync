package com.sakib.notionsync

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.Icon
import android.net.TrafficStats
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.content.ContextCompat

/**
 * Internet speed in the status bar (like the laptop's taskbar read-out): the icon shows the current
 * download speed; pull down to see ↓ download, ↑ upload and today's data (mobile / Wi-Fi).
 */
class SpeedService : Service() {

    companion object {
        private const val CHANNEL = "speed"
        private const val ID = 4207

        fun start(ctx: Context) {
            if (!Prefs(ctx).speedMeter) return
            try { ContextCompat.startForegroundService(ctx, Intent(ctx, SpeedService::class.java)) } catch (e: Exception) { }
        }

        fun stop(ctx: Context) = ctx.stopService(Intent(ctx, SpeedService::class.java))

        fun rate(bps: Double): String = when {
            bps < 1024 -> "${bps.toInt()} B/s"
            bps < 1024 * 1024 -> String.format("%.1f KB/s", bps / 1024)
            else -> String.format("%.2f MB/s", bps / 1024 / 1024)
        }

        fun size(b: Long): String = when {
            b < 1024 * 1024 -> String.format("%.0f KB", b / 1024.0)
            b < 1024L * 1024 * 1024 -> String.format("%.1f MB", b / 1024.0 / 1024)
            else -> String.format("%.2f GB", b / 1024.0 / 1024 / 1024)
        }
    }

    private val main = Handler(Looper.getMainLooper())
    private var lastRx = 0L
    private var lastTx = 0L
    private var lastMob = 0L
    private var lastT = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Internet speed", NotificationManager.IMPORTANCE_LOW).apply {
                setShowBadge(false); setSound(null, null); enableVibration(false)
            })
        }
        try {
            if (Build.VERSION.SDK_INT >= 34) startForeground(ID, build(0.0, 0.0), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            else startForeground(ID, build(0.0, 0.0))
        } catch (e: Exception) { stopSelf(); return START_NOT_STICKY }
        if (!Prefs(this).speedMeter) { stopSelf(); return START_NOT_STICKY }
        lastRx = TrafficStats.getTotalRxBytes(); lastTx = TrafficStats.getTotalTxBytes()
        lastMob = TrafficStats.getMobileRxBytes() + TrafficStats.getMobileTxBytes()
        lastT = System.currentTimeMillis()
        main.removeCallbacks(tick)
        main.postDelayed(tick, 1000)
        return START_STICKY
    }

    private val tick = object : Runnable {
        override fun run() {
            val p = Prefs(this@SpeedService)
            if (!p.speedMeter) { stopSelf(); return }
            val now = System.currentTimeMillis()
            val rx = TrafficStats.getTotalRxBytes(); val tx = TrafficStats.getTotalTxBytes()
            val mob = TrafficStats.getMobileRxBytes() + TrafficStats.getMobileTxBytes()
            val dt = ((now - lastT).coerceAtLeast(200)) / 1000.0
            val dRx = (rx - lastRx).coerceAtLeast(0); val dTx = (tx - lastTx).coerceAtLeast(0)
            val dMob = (mob - lastMob).coerceAtLeast(0)
            lastRx = rx; lastTx = tx; lastMob = mob; lastT = now
            // today's totals (reset at midnight)
            val day = java.text.SimpleDateFormat("yyyyMMdd", java.util.Locale.US).format(java.util.Date())
            if (p.speedDay != day) { p.speedDay = day; p.speedToday = 0; p.speedTodayMobile = 0 }
            p.speedToday = p.speedToday + dRx + dTx
            p.speedTodayMobile = p.speedTodayMobile + dMob.coerceAtMost(dRx + dTx)
            try {
                getSystemService(NotificationManager::class.java).notify(ID, build(dRx / dt, dTx / dt))
            } catch (e: Exception) { }
            main.postDelayed(this, 1000)
        }
    }

    override fun onDestroy() {
        main.removeCallbacks(tick)
        super.onDestroy()
    }

    private fun build(down: Double, up: Double): Notification {
        val p = Prefs(this)
        val open = PendingIntent.getActivity(this, 11, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val mobile = p.speedTodayMobile
        val wifi = (p.speedToday - mobile).coerceAtLeast(0)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(Icon.createWithBitmap(icon(down + up)))
            .setContentTitle("↓ ${rate(down)}     ↑ ${rate(up)}")
            .setContentText("Today: ${size(p.speedToday)}  (mobile ${size(mobile)} · Wi-Fi ${size(wifi)})")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setContentIntent(open)
            .setCategory(Notification.CATEGORY_STATUS)
            .build()
    }

    /** The status bar icon: the speed number over its unit, drawn in white (Android tints it). */
    private fun icon(bps: Double): Bitmap {
        val s = 96
        val bmp = Bitmap.createBitmap(s, s, Bitmap.Config.ARGB_8888)
        val c = Canvas(bmp)
        val (num, unit) = when {
            bps < 1024 -> "0" to "KB/s"
            bps < 1024 * 1024 -> (if (bps < 10 * 1024) String.format("%.1f", bps / 1024) else (bps / 1024).toInt().toString()) to "KB/s"
            else -> (if (bps < 10.0 * 1024 * 1024) String.format("%.1f", bps / 1024 / 1024) else (bps / 1024 / 1024).toInt().toString()) to "MB/s"
        }
        val pn = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD); textAlign = Paint.Align.CENTER }
        pn.textSize = if (num.length <= 2) 62f else if (num.length == 3) 54f else 44f
        c.drawText(num, s / 2f, 58f, pn)
        val pu = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textAlign = Paint.Align.CENTER; textSize = 34f
            typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD) }
        c.drawText(unit, s / 2f, 92f, pu)
        return bmp
    }
}
