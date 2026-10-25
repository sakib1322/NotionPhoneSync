package com.sakib.notionsync

import android.net.TrafficStats
import android.os.Process
import android.os.SystemClock
import java.util.Locale

/**
 * Measures this app's own internet traffic (not the whole phone's):
 * upload = files going to Notion, download = Notion's replies and app updates.
 */
class SpeedMeter {
    private val uid = Process.myUid()
    private var lastTx = -1L
    private var lastRx = -1L
    private var lastTime = 0L
    private var upAvg = 0.0
    private var downAvg = 0.0

    data class Reading(val upBps: Double, val downBps: Double, val totalUp: Long, val totalDown: Long)

    /** Call about once a second. Returns null if Android doesn't report traffic on this phone. */
    fun sample(): Reading? {
        val tx = TrafficStats.getUidTxBytes(uid)
        val rx = TrafficStats.getUidRxBytes(uid)
        if (tx == TrafficStats.UNSUPPORTED.toLong() || rx == TrafficStats.UNSUPPORTED.toLong()) return null
        val now = SystemClock.elapsedRealtime()
        if (lastTime > 0 && now > lastTime) {
            val secs = (now - lastTime) / 1000.0
            val up = ((tx - lastTx).coerceAtLeast(0)) / secs
            val down = ((rx - lastRx).coerceAtLeast(0)) / secs
            // Light smoothing so the numbers don't jump around.
            upAvg = upAvg * 0.4 + up * 0.6
            downAvg = downAvg * 0.4 + down * 0.6
        }
        lastTx = tx
        lastRx = rx
        lastTime = now
        return Reading(upAvg, downAvg, tx, rx)
    }

    companion object {
        fun rate(bps: Double): String = when {
            bps >= 1024 * 1024 -> String.format(Locale.US, "%.1f MB/s", bps / (1024 * 1024))
            bps >= 1024 -> String.format(Locale.US, "%.0f KB/s", bps / 1024)
            else -> String.format(Locale.US, "%.0f B/s", bps)
        }

        fun size(bytes: Long): String = when {
            bytes >= 1024L * 1024 * 1024 -> String.format(Locale.US, "%.2f GB", bytes / (1024.0 * 1024 * 1024))
            bytes >= 1024L * 1024 -> String.format(Locale.US, "%.1f MB", bytes / (1024.0 * 1024))
            else -> String.format(Locale.US, "%.0f KB", bytes / 1024.0)
        }
    }
}
