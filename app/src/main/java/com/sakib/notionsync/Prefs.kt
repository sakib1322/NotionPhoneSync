package com.sakib.notionsync

import android.content.Context

class Prefs(ctx: Context) {
    private val sp = ctx.applicationContext.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) { sp.edit().putString("token", v).apply() }

    var rootPageId: String
        get() = sp.getString("root_page", "") ?: ""
        set(v) { sp.edit().putString("root_page", v).apply() }

    var rootTitle: String
        get() = sp.getString("root_title", "") ?: ""
        set(v) { sp.edit().putString("root_title", v).apply() }

    var wifiOnly: Boolean
        get() = sp.getBoolean("wifi_only", true)
        set(v) { sp.edit().putBoolean("wifi_only", v).apply() }

    var chargingOnly: Boolean
        get() = sp.getBoolean("charging_only", false)
        set(v) { sp.edit().putBoolean("charging_only", v).apply() }

    var skipHidden: Boolean
        get() = sp.getBoolean("skip_hidden", true)
        set(v) { sp.edit().putBoolean("skip_hidden", v).apply() }

    var excluded: String
        get() = sp.getString("excluded", "Android/data\nAndroid/obb") ?: ""
        set(v) { sp.edit().putString("excluded", v).apply() }

    var autoSync: Boolean
        get() = sp.getBoolean("auto_sync", false)
        set(v) { sp.edit().putBoolean("auto_sync", v).apply() }

    var running: Boolean
        get() = sp.getBoolean("running", false)
        set(v) { sp.edit().putBoolean("running", v).apply() }

    var lastRun: Long
        get() = sp.getLong("last_run", 0L)
        set(v) { sp.edit().putLong("last_run", v).apply() }

    var lastPending: Int
        get() = sp.getInt("last_pending", 0)
        set(v) { sp.edit().putInt("last_pending", v).apply() }

    fun excludedList(): List<String> =
        excluded.lines().map { it.trim().trim('/') }.filter { it.isNotEmpty() }
}
