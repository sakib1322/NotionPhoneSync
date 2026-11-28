package com.sakib.notionsync

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import org.json.JSONArray
import kotlin.math.abs
import kotlin.math.roundToInt

/** Everything the phone tells about its battery right now. */
data class BatteryNow(
    val level: Int, val charging: Boolean, val full: Boolean, val source: String, val health: String,
    val tempC: Double?, val volts: Double?, val currentMa: Int?, val chargeMah: Int?, val estFullMah: Int?,
    val designMah: Int?, val cycles: Int?, val minutesToFull: Long?, val technology: String
) {
    /** Estimated battery health: what it holds now vs. when new. */
    val healthPct: Int? get() {
        if (estFullMah == null || designMah == null || designMah <= 0) return null
        val pct = estFullMah * 100 / designMah
        return if (pct in 40..115) pct.coerceAtMost(100) else null     // outside that, the phone's numbers can't be trusted
    }
}

object Battery {
    private const val CHANNEL = "battery"

    fun read(ctx: Context): BatteryNow {
        val i = ctx.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val lvl = i?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = i?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val level = if (lvl >= 0 && scale > 0) lvl * 100 / scale else bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
        val status = i?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
        val plugged = i?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
        val source = when (plugged) {
            BatteryManager.BATTERY_PLUGGED_AC -> "charger"; BatteryManager.BATTERY_PLUGGED_USB -> "USB"
            BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless charger"; else -> if (plugged != 0) "charger" else ""
        }
        val health = when (i?.getIntExtra(BatteryManager.EXTRA_HEALTH, 0)) {
            BatteryManager.BATTERY_HEALTH_GOOD -> "Good"; BatteryManager.BATTERY_HEALTH_OVERHEAT -> "Too hot"
            BatteryManager.BATTERY_HEALTH_DEAD -> "Worn out"; BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE -> "Over voltage"
            BatteryManager.BATTERY_HEALTH_COLD -> "Too cold"; BatteryManager.BATTERY_HEALTH_UNSPECIFIED_FAILURE -> "Problem"
            else -> "Unknown"
        }
        val temp = i?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE)?.takeIf { it != Int.MIN_VALUE }?.let { it / 10.0 }
        val volts = i?.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1)?.takeIf { it > 0 }?.let { if (it > 1000) it / 1000.0 else it.toDouble() }
        // current: phones report µA (most) or mA (some); positive or negative depending on the maker
        val cur = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CURRENT_NOW).takeIf { it != Int.MIN_VALUE && it != 0 }?.let {
            val ma = if (abs(it) > 20_000) it / 1000 else it
            if (status == BatteryManager.BATTERY_STATUS_CHARGING) abs(ma) else -abs(ma)
        }
        val counter = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CHARGE_COUNTER).takeIf { it > 0 && it != Int.MIN_VALUE }
            ?.let { if (it > 50_000) it / 1000 else it }        // µAh -> mAh
        val estFull = if (counter != null && level in 5..100) counter * 100 / level else null
        val cycles = if (Build.VERSION.SDK_INT >= 34) i?.getIntExtra(BatteryManager.EXTRA_CYCLE_COUNT, -1)?.takeIf { it >= 0 } else null
        val toFull = if (Build.VERSION.SDK_INT >= 28) bm.computeChargeTimeRemaining().takeIf { it > 0 }?.let { it / 60000 } else null
        return BatteryNow(level, status == BatteryManager.BATTERY_STATUS_CHARGING, status == BatteryManager.BATTERY_STATUS_FULL,
            source, health, temp, volts, cur, counter, estFull, designCapacity(ctx), cycles, toFull,
            i?.getStringExtra(BatteryManager.EXTRA_TECHNOLOGY) ?: "")
    }

    /** The battery's size when new, as the phone maker wrote it into Android (not on every phone). */
    private fun designCapacity(ctx: Context): Int? = try {
        val cls = Class.forName("com.android.internal.os.PowerProfile")
        val pp = cls.getConstructor(Context::class.java).newInstance(ctx)
        (cls.getMethod("getBatteryCapacity").invoke(pp) as Double).roundToInt().takeIf { it in 500..20000 }
    } catch (_: Throwable) { null }

    // ---- reminders and history (in the background) ----

    @Volatile private var attached = false

    /** Starts watching the battery while the app is running (the shared clipboard, sync or the screen keep it running). */
    fun attach(ctx: Context) {
        if (attached) return
        synchronized(this) {
            if (attached) return
            try {
                ContextCompat.registerReceiver(ctx.applicationContext, object : BroadcastReceiver() {
                    override fun onReceive(c: Context, intent: Intent) = check(c)
                }, IntentFilter(Intent.ACTION_BATTERY_CHANGED), ContextCompat.RECEIVER_NOT_EXPORTED)
                attached = true
            } catch (_: Exception) {}
        }
    }

    private fun prefs(ctx: Context) = ctx.applicationContext.getSharedPreferences("battery", Context.MODE_PRIVATE)
    fun remindOn(ctx: Context) = prefs(ctx).getBoolean("remind", false)
    fun unplugAt(ctx: Context) = prefs(ctx).getInt("unplug_at", 80)
    fun plugAt(ctx: Context) = prefs(ctx).getInt("plug_at", 20)
    fun setReminders(ctx: Context, on: Boolean, unplug: Int, plug: Int) {
        prefs(ctx).edit().putBoolean("remind", on).putInt("unplug_at", unplug).putInt("plug_at", plug).apply()
        if (on) attach(ctx)
    }

    /** Called on every battery change (and from the sync job): reminders + a small history for the chart. */
    @Synchronized
    fun check(ctx: Context) {
        val p = prefs(ctx)
        val now = try { read(ctx) } catch (_: Exception) { return }
        val t = System.currentTimeMillis()
        // history: one point every 5 minutes, 48 hours
        if (t - p.getLong("last_point", 0) > 5 * 60_000) {
            val arr = try { JSONArray(p.getString("history", "[]")) } catch (_: Exception) { JSONArray() }
            arr.put(JSONArray().put(t / 1000).put(now.level).put(if (now.charging) 1 else 0))
            val keep = JSONArray()
            for (k in 0 until arr.length()) { val e = arr.getJSONArray(k); if (e.getLong(0) > t / 1000 - 48 * 3600) keep.put(e) }
            p.edit().putString("history", keep.toString()).putLong("last_point", t).apply()
        }
        if (!p.getBoolean("remind", false)) return
        val plugged = now.charging || now.full || now.source.isNotEmpty()
        val wasPlugged = p.getBoolean("was_plugged", plugged)
        if (plugged != wasPlugged) p.edit().putBoolean("was_plugged", plugged).putBoolean("told_unplug", false).putBoolean("told_plug", false).apply()
        if (plugged && now.level >= unplugAt(ctx) && !p.getBoolean("told_unplug", false)) {
            notify(ctx, 7101, "🔋 ${now.level}% — unplug the charger", "Keeping it between ${plugAt(ctx)}% and ${unplugAt(ctx)}% makes the battery last years longer.")
            p.edit().putBoolean("told_unplug", true).apply()
        } else if (!plugged && now.level <= plugAt(ctx) && !p.getBoolean("told_plug", false)) {
            notify(ctx, 7102, "🪫 ${now.level}% — time to plug in", "Charging before it gets very low is kinder to the battery.")
            p.edit().putBoolean("told_plug", true).apply()
        }
    }

    fun history(ctx: Context): List<Triple<Long, Int, Boolean>> {
        val arr = try { JSONArray(prefs(ctx).getString("history", "[]")) } catch (_: Exception) { JSONArray() }
        return (0 until arr.length()).map { val e = arr.getJSONArray(it); Triple(e.getLong(0), e.getInt(1), e.getInt(2) == 1) }
    }

    fun notify(ctx: Context, id: Int, title: String, text: String) {
        val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (nm.getNotificationChannel(CHANNEL) == null)
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Battery reminders", NotificationManager.IMPORTANCE_HIGH))
        val open = PendingIntent.getActivity(ctx, 0, Intent(ctx, BatteryActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        try {
            nm.notify(id, NotificationCompat.Builder(ctx, CHANNEL).setSmallIcon(R.drawable.ic_stat).setContentTitle(title)
                .setContentText(text).setStyle(NotificationCompat.BigTextStyle().bigText(text)).setContentIntent(open).setAutoCancel(true)
                .setPriority(NotificationCompat.PRIORITY_HIGH).build())
        } catch (_: SecurityException) {}
    }
}
