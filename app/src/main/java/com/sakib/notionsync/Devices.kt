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
        val page: String, val pageTitle: String, val seen: String, val status: String = ""
    ) {
        fun toJson(): JSONObject = JSONObject().put("row", row).put("id", id).put("name", name).put("type", type)
            .put("page", page).put("page_title", pageTitle).put("seen", seen).put("status", status)

        /** What the device last reported (files, pending, syncing…). */
        fun info(): JSONObject = try { JSONObject(status.ifEmpty { "{}" }) } catch (e: Exception) { JSONObject() }

        fun online(): Boolean {
            val t = try { Instant.parse(seen).toEpochMilli() } catch (e: Exception) {
                try { java.time.OffsetDateTime.parse(seen).toInstant().toEpochMilli() } catch (x: Exception) { return false }
            }
            return System.currentTimeMillis() - t < 12 * 60_000L
        }

        fun seenAgo(): String {
            val t = try { Instant.parse(seen).toEpochMilli() } catch (e: Exception) {
                try { java.time.OffsetDateTime.parse(seen).toInstant().toEpochMilli() } catch (x: Exception) { return "never" }
            }
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
                o.optString("page"), o.optString("page_title"), o.optString("seen"), o.optString("status")
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

    /** What this phone reports to your other devices. */
    fun myStatus(ctx: Context): JSONObject {
        val p = Prefs(ctx)
        val db = Db.get(ctx)
        val syncing = p.running && System.currentTimeMillis() - p.heartbeat < 3 * 60_000L
        val (n, b) = try { db.storageTotal(p.rootPageId) } catch (e: Exception) { 0 to 0L }
        val c = try { db.counts() } catch (e: Exception) { emptyMap() }
        return JSONObject().put("platform", "android").put("version", Updater.currentBuild(ctx))
            .put("syncing", syncing).put("now", if (syncing) p.currentText else "")
            .put("pending", p.lastPending).put("uploaded", c["done"] ?: 0)
            .put("failed", (c["failed"] ?: 0) + (c["retry"] ?: 0))
            .put("cloud_files", n).put("cloud_bytes", b).put("last_sync", p.lastRun)
            .put("auto", p.autoSync).put("clipboard", p.clipboardSync)
            .put("wifi", JSONArray(ClipService.lan?.connectedNames() ?: emptyList<String>()))
    }

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

    // Notion's search is slow to notice new databases, so two devices signing in close together can
    // each make a devices list. We read every one we find (searching again every few minutes),
    // always write to the oldest, and merge by device id, so they always meet.

    private fun search(): List<Pair<String, String>> {
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
        return found
    }

    private val sp = ctx.applicationContext.getSharedPreferences("devices", Context.MODE_PRIVATE)

    private fun known(): MutableList<Pair<String, String>> {
        val out = sp.getString("dbs", "").orEmpty().split(",").filter { it.contains("|") }
            .map { it.substringBefore("|") to it.substringAfter("|") }.toMutableList()
        val old = prefs.devicesDb
        if (old.isNotEmpty() && out.none { it.second == old }) out.add("" to old)
        return out
    }

    private fun save(list: Collection<Pair<String, String>>) {
        val byId = LinkedHashMap<String, String>()
        for ((c, i) in list) if (i !in byId || (c.isNotEmpty() && byId[i].isNullOrEmpty())) byId[i] = c
        val sorted = byId.entries.map { it.value to it.key }.sortedWith(compareBy({ it.first.isEmpty() }, { it.first }))
        sp.edit().putString("dbs", sorted.joinToString(",") { "${it.first}|${it.second}" }).apply()
        prefs.devicesDb = sorted.firstOrNull()?.second ?: ""
    }

    fun dbs(parent: String? = null, refresh: Boolean = false): List<String> {
        var list: MutableCollection<Pair<String, String>> = known()
        if (refresh || list.isEmpty() || System.currentTimeMillis() - sp.getLong("search_at", 0) > 300_000) {
            try {
                list = (list + search()).toMutableSet()
                sp.edit().putLong("search_at", System.currentTimeMillis()).apply()
            } catch (e: Exception) {
                if (list.isEmpty()) throw e
            }
        }
        if (list.isEmpty()) {
            val where = parent ?: prefs.rootPageId.ifEmpty { return emptyList() }
            fun empty() = JSONObject()
            val props = JSONObject()
                .put("Name", JSONObject().put("title", empty())).put("Id", JSONObject().put("rich_text", empty()))
                .put("Type", JSONObject().put("rich_text", empty())).put("Page", JSONObject().put("rich_text", empty()))
                .put("Page title", JSONObject().put("rich_text", empty())).put("Seen", JSONObject().put("date", empty()))
                .put("Status", JSONObject().put("rich_text", empty()))
            val d = client.post("/databases",
                JSONObject().put("parent", JSONObject().put("type", "page_id").put("page_id", where))
                    .put("title", JSONArray().put(JSONObject().put("type", "text").put("text", JSONObject().put("content", Devices.TITLE))))
                    .put("properties", props))
            list = mutableListOf(d.optString("created_time") to d.getString("id"))
            sp.edit().putString("status_col", d.getString("id")).apply()
        }
        save(list)
        return known().map { it.second }.let { ids -> listOf(prefs.devicesDb).filter { it.isNotEmpty() } + ids.filter { it != prefs.devicesDb } }
    }

    fun dbId(parent: String? = null): String? = dbs(parent).firstOrNull()

    private fun forget(id: String) = save(known().filter { it.second != id })

    private fun rows(): List<Devices.Device> {
        val out = ArrayList<Devices.Device>()
        for (db in dbs()) {
            var cursor: String? = null
            do {
                val body = JSONObject().put("page_size", 100)
                if (cursor != null) body.put("start_cursor", cursor)
                val res = try {
                    client.queryDatabase(db, body)
                } catch (e: NotionException) {
                    if (e.status == 404) { forget(db); break }
                    throw e
                }
                val arr = res.optJSONArray("results") ?: JSONArray()
                for (i in 0 until arr.length()) {
                    val p = arr.getJSONObject(i)
                    val pr = p.optJSONObject("properties") ?: continue
                    val seen = pr.optJSONObject("Seen")?.optJSONObject("date")?.optString("start").orEmpty()
                    out.add(Devices.Device(p.getString("id"), plain(pr, "Id"), plain(pr, "Name"), plain(pr, "Type"),
                        plain(pr, "Page"), plain(pr, "Page title"), seen, plain(pr, "Status")))
                }
                cursor = if (res.optBoolean("has_more")) res.optString("next_cursor").takeIf { it.isNotEmpty() && it != "null" } else null
            } while (cursor != null)
        }
        return out
    }

    private fun ensureStatus(db: String): Boolean {
        if (sp.getString("status_col", "") == db) return true
        return try {
            client.patchDatabase(db, JSONObject().put("properties", JSONObject().put("Status", JSONObject().put("rich_text", JSONObject()))))
            sp.edit().putString("status_col", db).apply()
            true
        } catch (e: Exception) { false }
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

    private fun upsert(id: String, name: String, type: String, page: String, pageTitle: String, seen: Boolean = true,
                       status: String? = null): String? {
        val db = dbId() ?: return null
        val props = JSONObject()
            .put("Name", JSONObject().put("title", JSONArray().put(JSONObject().put("text", JSONObject().put("content", name.ifBlank { "Device" }.take(200))))))
            .put("Id", rt(id)).put("Type", rt(type)).put("Page", rt(page)).put("Page title", rt(pageTitle))
        if (seen) props.put("Seen", JSONObject().put("date", JSONObject().put("start", Instant.now().toString())))
        if (status != null && ensureStatus(db)) props.put("Status", rt(status))
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

    fun registerMe(status: JSONObject? = null) {
        namePage(null)
        upsert(prefs.deviceId, Devices.myName(ctx), "phone", prefs.rootPageId, prefs.rootTitle, status = status?.toString())
        prefs.devicesSeenAt = System.currentTimeMillis()
    }

    /** Keeps this phone's backup page named after it ("<name> Backup") while it still has a
     *  default name (Phone Backup) or was named after the old device name. */
    private fun namePage(old: String?) {
        val name = prefs.deviceName
        if (name.isBlank() || prefs.rootPageId.isEmpty()) return
        val want = "$name Backup"
        val title = prefs.rootTitle
        if (title == want) return
        if (title !in setOf("Phone Backup", "Laptop Backup", "") && (old == null || title != "$old Backup")) return
        try {
            client.patchPage(prefs.rootPageId, JSONObject().put("properties", JSONObject().put("title",
                JSONObject().put("title", JSONArray().put(JSONObject().put("text", JSONObject().put("content", want)))))))
            prefs.rootTitle = want
        } catch (e: Exception) { }
    }

    fun renameMe(newName: String) {
        val old = Devices.myName(ctx)
        prefs.deviceName = newName.trim().take(60)
        namePage(old)
        registerMe(Devices.myStatus(ctx))
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
