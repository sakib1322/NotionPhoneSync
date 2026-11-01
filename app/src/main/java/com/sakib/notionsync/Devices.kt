package com.sakib.notionsync

import android.content.Context
import android.os.Build
import android.provider.Settings
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant

/**
 * Your devices: every phone and laptop signed in to the same account, each with its own
 * name and backup page. Kept in a small Notion database (the "Devices" list, same as the
 * laptop's devices.py) so every device can see the others.
 */
object Devices {
    const val TITLE = "Devices (used by Notion Sync apps - please don't edit)"
    const val HOME_ID = "home"

    data class Device(
        val row: String, val id: String, val name: String, val type: String,
        val page: String, val pageTitle: String, val seen: String
    ) {
        fun toJson(): JSONObject = JSONObject().put("row", row).put("id", id).put("name", name).put("type", type)
            .put("page", page).put("page_title", pageTitle).put("seen", seen)

        fun seenAgo(): String {
            val t = try { Instant.parse(seen).toEpochMilli() } catch (e: Exception) { return "never" }
            val s = (System.currentTimeMillis() - t) / 1000
            return when {
                s < 120 -> "just now"
                s < 3600 -> "${s / 60} min ago"
                s < 86400 -> "${s / 3600} h ago"
                else -> "${s / 86400} days ago"
            }
        }

        companion object {
            fun fromJson(o: JSONObject) = Device(
                o.optString("row"), o.optString("id"), o.optString("name"), o.optString("type"),
                o.optString("page"), o.optString("page_title"), o.optString("seen")
            )
        }
    }

    fun defaultName(ctx: Context): String {
        val n = try { Settings.Global.getString(ctx.contentResolver, "device_name") } catch (e: Exception) { null }
        return (n?.takeIf { it.isNotBlank() } ?: "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}").trim().take(60)
    }

    fun myName(ctx: Context): String = Prefs(ctx).deviceName.ifBlank { defaultName(ctx) }

    fun cached(ctx: Context): List<Device> {
        val arr = try { JSONArray(Prefs(ctx).devicesCache) } catch (e: Exception) { JSONArray() }
        return (0 until arr.length()).map { Device.fromJson(arr.getJSONObject(it)) }
    }

    /** Ids of the other devices seen in the last [days] days. */
    fun othersRecent(ctx: Context, days: Int = 30): Set<String> {
        val me = Prefs(ctx).deviceId
        val cutoff = System.currentTimeMillis() - days * 86_400_000L
        return cached(ctx).filter { d ->
            d.id.isNotEmpty() && d.id != me &&
                (try { Instant.parse(d.seen).toEpochMilli() } catch (e: Exception) { System.currentTimeMillis() }) >= cutoff
        }.map { it.id }.toSet()
    }

    fun nameOf(ctx: Context, id: String): String? = cached(ctx).firstOrNull { it.id == id }?.name

    /** Notion is needed unless every other recent device already got it over Wi-Fi. */
    fun needNotion(ctx: Context, reached: Set<String>): Boolean {
        val others = othersRecent(ctx)
        if (others.isEmpty()) return reached.isEmpty()
        return (others - reached).isNotEmpty()
    }
}

class Registry(private val ctx: Context) {
    private val prefs = Prefs(ctx)
    private val client = NotionClient(prefs.token)

    private fun rt(v: String) = JSONObject().put("rich_text", JSONArray().put(JSONObject().put("text", JSONObject().put("content", v.take(1900)))))

    private fun plain(pr: JSONObject, name: String): String {
        val p = pr.optJSONObject(name) ?: return ""
        val arr = p.optJSONArray(p.optString("type")) ?: p.optJSONArray("rich_text") ?: p.optJSONArray("title") ?: return ""
        return (0 until arr.length()).joinToString("") { arr.getJSONObject(it).optString("plain_text") }
    }

    fun dbId(parent: String? = null): String? {
        prefs.devicesDb.takeIf { it.isNotEmpty() }?.let { return it }
        val found = ArrayList<Pair<String, String>>()
        var cursor: String? = null
        do {
            val body = JSONObject().put("query", "Devices (used by Notion Sync").put("page_size", 50)
                .put("filter", JSONObject().put("property", "object").put("value", "database"))
            if (cursor != null) body.put("start_cursor", cursor)
            val res = client.search(body)
            val arr = res.optJSONArray("results") ?: JSONArray()
            for (i in 0 until arr.length()) {
                val d = arr.getJSONObject(i)
                val t = d.optJSONArray("title")
                val title = if (t == null) "" else (0 until t.length()).joinToString("") { t.getJSONObject(it).optString("plain_text") }
                if (title == Devices.TITLE && !d.optBoolean("archived") && !d.optBoolean("in_trash")) {
                    found.add(d.optString("created_time") to d.getString("id"))
                }
            }
            cursor = if (res.optBoolean("has_more")) res.optString("next_cursor").takeIf { it.isNotEmpty() && it != "null" } else null
        } while (cursor != null)
        val id = found.minByOrNull { it.first }?.second ?: run {
            val where = parent ?: prefs.rootPageId.ifEmpty { return null }
            fun empty() = JSONObject()
            val props = JSONObject()
                .put("Name", JSONObject().put("title", empty())).put("Id", JSONObject().put("rich_text", empty()))
                .put("Type", JSONObject().put("rich_text", empty())).put("Page", JSONObject().put("rich_text", empty()))
                .put("Page title", JSONObject().put("rich_text", empty())).put("Seen", JSONObject().put("date", empty()))
            client.createDatabase(
                JSONObject().put("parent", JSONObject().put("type", "page_id").put("page_id", where))
                    .put("title", JSONArray().put(JSONObject().put("type", "text").put("text", JSONObject().put("content", Devices.TITLE))))
                    .put("properties", props)
            )
        }
        prefs.devicesDb = id
        return id
    }

    private fun rows(): List<Devices.Device> {
        val db = dbId() ?: return emptyList()
        val out = ArrayList<Devices.Device>()
        var cursor: String? = null
        do {
            val body = JSONObject().put("page_size", 100)
            if (cursor != null) body.put("start_cursor", cursor)
            val res = try {
                client.queryDatabase(db, body)
            } catch (e: NotionException) {
                if (e.status == 404) { prefs.devicesDb = ""; return rows() }
                throw e
            }
            val arr = res.optJSONArray("results") ?: JSONArray()
            for (i in 0 until arr.length()) {
                val p = arr.getJSONObject(i)
                val pr = p.optJSONObject("properties") ?: continue
                val seen = pr.optJSONObject("Seen")?.optJSONObject("date")?.optString("start").orEmpty()
                out.add(Devices.Device(p.getString("id"), plain(pr, "Id"), plain(pr, "Name"), plain(pr, "Type"),
                    plain(pr, "Page"), plain(pr, "Page title"), seen))
            }
            cursor = if (res.optBoolean("has_more")) res.optString("next_cursor").takeIf { it.isNotEmpty() && it != "null" } else null
        } while (cursor != null)
        return out
    }

    /** Every device (one per id), newest-seen first; also caches them. */
    fun all(): List<Devices.Device> {
        val byId = LinkedHashMap<String, Devices.Device>()
        for (d in rows()) {
            if (d.id.isEmpty() || d.id == Devices.HOME_ID) continue
            val cur = byId[d.id]
            if (cur == null || d.seen > cur.seen) byId[d.id] = d
        }
        val devs = byId.values.sortedByDescending { it.seen }
        prefs.devicesCache = JSONArray().apply { devs.forEach { put(it.toJson()) } }.toString()
        return devs
    }

    fun home(): String? = rows().firstOrNull { it.id == Devices.HOME_ID && it.page.isNotEmpty() }?.page

    fun setHome(page: String) = upsert(Devices.HOME_ID, "(where new devices' backup pages go)", "home", page, "", false)

    private fun upsert(id: String, name: String, type: String, page: String, pageTitle: String, seen: Boolean = true): String? {
        val db = dbId() ?: return null
        val props = JSONObject()
            .put("Name", JSONObject().put("title", JSONArray().put(JSONObject().put("text", JSONObject().put("content", name.ifBlank { "Device" }.take(200))))))
            .put("Id", rt(id)).put("Type", rt(type)).put("Page", rt(page)).put("Page title", rt(pageTitle))
        if (seen) props.put("Seen", JSONObject().put("date", JSONObject().put("start", Instant.now().toString())))
        val res = client.queryDatabase(db, JSONObject().put("page_size", 5)
            .put("filter", JSONObject().put("property", "Id").put("rich_text", JSONObject().put("equals", id))))
        val arr = res.optJSONArray("results") ?: JSONArray()
        if (arr.length() > 0) {
            val row = arr.getJSONObject(0).getString("id")
            client.updateRow(row, props)
            return row
        }
        return client.createRow(db, props)
    }

    fun registerMe() {
        upsert(prefs.deviceId, Devices.myName(ctx), "phone", prefs.rootPageId, prefs.rootTitle)
        prefs.devicesSeenAt = System.currentTimeMillis()
    }

    fun renameMe(newName: String) {
        val old = Devices.myName(ctx)
        val name = newName.trim().take(60)
        prefs.deviceName = name
        // A page the app named after the phone is renamed with it.
        if (prefs.rootPageId.isNotEmpty() && prefs.rootTitle == "$old Backup") {
            val title = "$name Backup"
            try {
                client.patchPage(prefs.rootPageId, JSONObject().put("properties", JSONObject().put("title",
                    JSONObject().put("title", JSONArray().put(JSONObject().put("text", JSONObject().put("content", title)))))))
                prefs.rootTitle = title
            } catch (e: Exception) { }
        }
        registerMe()
    }

    /** Pages a new device's backup page can go inside: the remembered spot first, then
     *  top-level pages that aren't another device's backup. */
    fun candidateParents(): List<Pair<String, String>> {
        val devicePages = all().map { it.page }.filter { it.isNotEmpty() }.toSet()
        val out = ArrayList<Pair<String, String>>()
        val home = home()
        if (home != null) try { out.add(home to client.pageTitle(home)) } catch (e: Exception) { }
        for ((id, t) in client.topLevelPages()) if (id !in devicePages && id != home) out.add(id to t)
        return out
    }

    fun createPage(parent: String, title: String): String {
        val id = client.createPage(parent, title)
        setHome(parent)
        return id
    }
}
