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

    /** How often auto-sync runs, in minutes (Android's minimum is 15). */
    var intervalMinutes: Int
        get() = sp.getInt("interval_minutes", 15).coerceAtLeast(15)
        set(v) { sp.edit().putInt("interval_minutes", v.coerceIn(15, 10080)).apply() }

    var running: Boolean
        get() = sp.getBoolean("running", false)
        set(v) { sp.edit().putBoolean("running", v).apply() }

    var lastRun: Long
        get() = sp.getLong("last_run", 0L)
        set(v) { sp.edit().putLong("last_run", v).apply() }

    /** Last moment the sync showed signs of life; used to tell a real run from a stale flag. */
    var heartbeat: Long
        get() = sp.getLong("heartbeat", 0L)
        set(v) { sp.edit().putLong("heartbeat", v).apply() }

    /** True after a fresh install: check Notion for files that are already uploaded. */
    var checkNotion: Boolean
        get() = sp.getBoolean("check_notion", false)
        set(v) { sp.edit().putBoolean("check_notion", v).apply() }

    /** What the sync is doing right now (shown on the dashboard). */
    var currentText: String
        get() = sp.getString("current_text", "") ?: ""
        set(v) { sp.edit().putString("current_text", v).apply() }

    /** new, old, name or size */
    var filesSort: String
        get() = sp.getString("files_sort", "new") ?: "new"
        set(v) { sp.edit().putString("files_sort", v).apply() }

    var filesGrid: Boolean
        get() = sp.getBoolean("files_grid", true)
        set(v) { sp.edit().putBoolean("files_grid", v).apply() }

    /** Extra Notion pages to browse in Files, saved as "id|title" lines. */
    var extraSources: String
        get() = sp.getString("extra_sources", "") ?: ""
        set(v) { sp.edit().putString("extra_sources", v).apply() }

    var lastPending: Int
        get() = sp.getInt("last_pending", 0)
        set(v) { sp.edit().putInt("last_pending", v).apply() }

    fun excludedList(): List<String> =
        excluded.lines().map { it.trim().trim('/') }.filter { it.isNotEmpty() }
}
