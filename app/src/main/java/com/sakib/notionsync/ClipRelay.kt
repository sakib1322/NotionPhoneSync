package com.sakib.notionsync

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.time.Instant

/**
 * The shared clipboard's mailbox: a small Notion database the phone and laptop use to
 * hand copied items to each other. Same design as the laptop's clip_relay.py.
 */
class ClipRelay(ctx: Context, private val device: String = "Phone") {
    private val prefs = Prefs(ctx)
    private val client = NotionClient(prefs.token)

    data class Item(val id: String, val kind: String, val text: String, val created: String)

    companion object {
        const val TITLE = "Clipboard (used by Notion Sync apps - please don't edit)"
        const val MAX_FILE = 100L * 1024 * 1024

        fun fingerprint(kind: String, text: String?, files: List<File>?): String {
            val md = MessageDigest.getInstance("SHA-1")
            md.update(kind.toByteArray())
            if (text != null) md.update(text.toByteArray())
            files?.forEach { md.update(it.name.toByteArray()); md.update(it.length().toString().toByteArray()) }
            return md.digest().joinToString("") { "%02x".format(it) }
        }

        private fun chunks(text: String): JSONArray {
            val t = text.take(2000 * 100)
            val arr = JSONArray()
            var i = 0
            while (i < t.length) {
                arr.put(JSONObject().put("type", "text").put("text", JSONObject().put("content", t.substring(i, minOf(i + 2000, t.length)))))
                i += 2000
            }
            if (arr.length() == 0) arr.put(JSONObject().put("type", "text").put("text", JSONObject().put("content", "")))
            return arr
        }
    }

    fun dbId(): String {
        prefs.clipDbId.takeIf { it.isNotEmpty() }?.let { return it }
        val res = client.search(
            JSONObject().put("query", "Clipboard (used by Notion Sync").put("page_size", 25)
                .put("filter", JSONObject().put("property", "object").put("value", "database"))
        )
        val arr = res.optJSONArray("results") ?: JSONArray()
        var best: Pair<String, String>? = null
        for (i in 0 until arr.length()) {
            val d = arr.getJSONObject(i)
            val t = d.optJSONArray("title")
            val title = if (t == null) "" else (0 until t.length()).joinToString("") { t.getJSONObject(it).optString("plain_text") }
            if (title != TITLE || d.optBoolean("archived") || d.optBoolean("in_trash")) continue
            val c = d.optString("created_time")
            if (best == null || c < best.first) best = c to d.getString("id")
        }
        val id = best?.second ?: run {
            fun empty() = JSONObject()
            val props = JSONObject()
                .put("Name", JSONObject().put("title", empty())).put("Kind", JSONObject().put("select", empty()))
                .put("From", JSONObject().put("select", empty())).put("Text", JSONObject().put("rich_text", empty()))
                .put("Ready", JSONObject().put("checkbox", empty())).put("Files", JSONObject().put("number", empty()))
            client.createDatabase(
                JSONObject().put("parent", JSONObject().put("type", "page_id").put("page_id", prefs.rootPageId))
                    .put("title", JSONArray().put(JSONObject().put("type", "text").put("text", JSONObject().put("content", TITLE))))
                    .put("properties", props)
            )
        }
        prefs.clipDbId = id
        return id
    }

    /** Sends text, or files (kind "image" or "files"). */
    fun send(kind: String, text: String?, files: List<File>) {
        val db = dbId()
        val name = if (kind == "text") (text ?: "").trim().lineSequence().firstOrNull()?.take(80).orEmpty().ifEmpty { "Text" }
        else files.joinToString(", ") { it.name }.take(200)
        val props = JSONObject()
            .put("Name", JSONObject().put("title", JSONArray().put(JSONObject().put("text", JSONObject().put("content", name)))))
            .put("Kind", JSONObject().put("select", JSONObject().put("name", kind)))
            .put("From", JSONObject().put("select", JSONObject().put("name", device)))
            .put("Ready", JSONObject().put("checkbox", false))
            .put("Files", JSONObject().put("number", files.size))
        if (kind == "text") props.put("Text", JSONObject().put("rich_text", chunks(text ?: "")))
        val row = try {
            client.createRow(db, props)
        } catch (e: NotionException) {
            if (e.status == 404) {
                prefs.clipDbId = ""
                client.createRow(dbId(), props)
            } else throw e
        }
        for (f in files) {
            val up = FileTypes.uploadName(f.name)
            val type = FileTypes.blockType(up)
            val id = client.uploadFile(f, up, FileTypes.mime(up), f.length())
            try {
                client.attach(row, id, type, f.name)
            } catch (e: NotionException) {
                if (e.status == 400 && type != "file") client.attach(row, id, "file", f.name) else throw e
            }
        }
        // Only now can the laptop pick it up (so it never sees half an item).
        client.updateRow(row, JSONObject().put("Ready", JSONObject().put("checkbox", true)))
    }

    fun poll(sinceMs: Long): List<Item> {
        val body = JSONObject().put("page_size", 20)
            .put("sorts", JSONArray().put(JSONObject().put("timestamp", "created_time").put("direction", "ascending")))
            .put(
                "filter", JSONObject().put(
                    "and", JSONArray()
                        .put(JSONObject().put("property", "From").put("select", JSONObject().put("does_not_equal", device)))
                        .put(JSONObject().put("property", "Ready").put("checkbox", JSONObject().put("equals", true)))
                        .put(
                            JSONObject().put("timestamp", "created_time")
                                .put("created_time", JSONObject().put("on_or_after", Instant.ofEpochMilli(sinceMs - 60_000).toString()))
                        )
                )
            )
        val res = try {
            client.queryDatabase(dbId(), body)
        } catch (e: NotionException) {
            if (e.status == 404) prefs.clipDbId = ""
            throw e
        }
        val arr = res.optJSONArray("results") ?: return emptyList()
        val out = ArrayList<Item>()
        for (i in 0 until arr.length()) {
            val p = arr.getJSONObject(i)
            val pr = p.optJSONObject("properties") ?: continue
            if (pr.optJSONObject("From")?.optJSONObject("select")?.optString("name") == device) continue
            if (pr.optJSONObject("Ready")?.optBoolean("checkbox") != true) continue
            val kind = pr.optJSONObject("Kind")?.optJSONObject("select")?.optString("name") ?: "text"
            val t = pr.optJSONObject("Text")?.optJSONArray("rich_text")
            val text = if (t == null) "" else (0 until t.length()).joinToString("") { t.getJSONObject(it).optString("plain_text") }
            out.add(Item(p.getString("id"), kind, text, p.optString("created_time")))
        }
        return out
    }

    /** (name, url) of an item's files. */
    fun filesOf(item: Item): List<Pair<String, String>> {
        val out = ArrayList<Pair<String, String>>()
        var cursor: String? = null
        do {
            val res = client.children(item.id, cursor)
            for (b in res.results) {
                val type = b.optString("type")
                if (type !in setOf("image", "video", "audio", "pdf", "file")) continue
                val inner = b.optJSONObject(type) ?: continue
                val url = inner.optJSONObject("file")?.optString("url")?.takeIf { !it.isNullOrEmpty() }
                    ?: inner.optJSONObject("external")?.optString("url")?.takeIf { !it.isNullOrEmpty() } ?: continue
                val cap = inner.optJSONArray("caption")
                val name = if (cap == null) "" else (0 until cap.length()).joinToString("") { cap.getJSONObject(it).optString("plain_text") }
                out.add(name.ifEmpty { inner.optString("name", "file") } to url)
            }
            cursor = res.next
        } while (cursor != null)
        return out
    }

    /** Removes this phone's items older than a day. */
    fun cleanup() {
        val cutoff = Instant.now().minusSeconds(24 * 3600)
        val res = client.queryDatabase(
            dbId(), JSONObject().put("page_size", 50).put(
                "filter", JSONObject().put(
                    "and", JSONArray()
                        .put(JSONObject().put("property", "From").put("select", JSONObject().put("equals", device)))
                        .put(JSONObject().put("timestamp", "created_time").put("created_time", JSONObject().put("before", cutoff.toString())))
                )
            )
        )
        val arr = res.optJSONArray("results") ?: return
        for (i in 0 until arr.length()) {
            val p = arr.getJSONObject(i)
            val created = try { Instant.parse(p.optString("created_time")) } catch (e: Exception) { continue }
            if (created.isAfter(cutoff)) continue
            if (p.optJSONObject("properties")?.optJSONObject("From")?.optJSONObject("select")?.optString("name") != device) continue
            try { client.archivePage(p.getString("id")) } catch (e: Exception) { }
        }
    }
}
