package com.sakib.notionsync

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.provider.Settings
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject

/**
 * Hides repeated notifications: when the same message arrives again within a few minutes — from the
 * same app or another one (e.g. an SMS and the bank's app, WhatsApp and WhatsApp Business, Gmail and
 * Outlook) — the repeat is removed. Updates of a notification that's already showing aren't touched,
 * and nothing ongoing (calls, music, navigation, downloads) is ever removed.
 */
class DedupService : NotificationListenerService() {

    companion object {
        fun granted(ctx: Context): Boolean {
            val on = Settings.Secure.getString(ctx.contentResolver, "enabled_notification_listeners") ?: return false
            val me = ComponentName(ctx, DedupService::class.java)
            return on.split(':').any { ComponentName.unflattenFromString(it) == me }
        }

        private val recent = LinkedHashMap<String, Pair<Long, String>>()   // message → (time, notification key)

        fun normalise(s: String): String = s.lowercase().replace(Regex("\\s+"), " ").replace(Regex("[\\u200B-\\u200F\\uFEFF]"), "").trim()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        try { check(sbn ?: return) } catch (t: Throwable) { }
    }

    private fun check(sbn: StatusBarNotification) {
        val p = Prefs(this)
        if (!p.dedupOn || sbn.packageName == packageName || sbn.isOngoing) return
        val n = sbn.notification ?: return
        if (n.flags and Notification.FLAG_GROUP_SUMMARY != 0) return
        if (n.flags and Notification.FLAG_FOREGROUND_SERVICE != 0) return
        if (n.category in setOf(Notification.CATEGORY_CALL, Notification.CATEGORY_ALARM, Notification.CATEGORY_TRANSPORT,
                Notification.CATEGORY_NAVIGATION, Notification.CATEGORY_PROGRESS, Notification.CATEGORY_STOPWATCH)) return
        val ex = n.extras ?: return
        val title = ex.getCharSequence(Notification.EXTRA_TITLE)?.toString().orEmpty()
        val text = (ex.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: ex.getCharSequence(Notification.EXTRA_TEXT))?.toString().orEmpty()
        if (text.isBlank()) return
        // A long message is matched on its words alone (different apps title it differently);
        // a short one together with its title.
        val sig = if (text.length >= 20) normalise(text) else normalise("$title|$text")
        val now = System.currentTimeMillis()
        val window = p.dedupMinutes * 60_000L
        synchronized(recent) {
            recent.entries.removeAll { now - it.value.first > window }
            val prev = recent[sig]
            if (prev != null && prev.second != sbn.key) {
                cancelNotification(sbn.key)
                record(p, sbn.packageName, title, text)
                return
            }
            recent[sig] = now to sbn.key
            while (recent.size > 300) recent.remove(recent.keys.first())
        }
    }

    private fun record(p: Prefs, pkg: String, title: String, text: String) {
        p.dedupCount = p.dedupCount + 1
        val app = try { packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString() } catch (e: Exception) { pkg }
        val arr = try { JSONArray(p.dedupLog) } catch (e: Exception) { JSONArray() }
        val out = JSONArray().put(JSONObject().put("t", System.currentTimeMillis()).put("app", app)
            .put("title", title.take(60)).put("text", text.take(80)))
        for (i in 0 until minOf(arr.length(), 29)) out.put(arr.get(i))
        p.dedupLog = out.toString()
    }
}
