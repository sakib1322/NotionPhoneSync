package com.sakib.notionsync

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.time.ZoneId

/**
 * The cloud file index: a Notion database inside each backup page with one
 * row per file (name, folder, size, date, kind, where the file lives).
 * The phone and laptop apps both write to it; the app keeps a local copy for
 * instant browsing and search.
 */
class CloudIndex(ctx: Context, private val client: NotionClient) {
    private val db = Db.get(ctx)

    data class Entry(
        val name: String, val path: String, val size: Long, val mtime: Long, val kind: String,
        val block: String, val page: String, val device: String, val source: String
    )

    companion object {
        const val TITLE = "File index (used by Notion Sync apps - please don't edit)"

        private val docs = setOf(
            "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "odt", "ods", "odp", "md",
            "pages", "key", "numbers", "epub", "json", "xml", "html", "htm"
        )

        fun kind(name: String): String {
            val ext = name.substringAfterLast('.', "").lowercase()
            return when {
                ext == "pdf" -> "PDF"
                ext in docs -> "Document"
                else -> when (FileTypes.blockType(FileTypes.uploadName(name))) {
                    "image" -> "Photo"
                    "video" -> "Video"
                    "audio" -> "Audio"
                    else -> "Other"
                }
            }
        }

        fun iso(ms: Long): String = OffsetDateTime.ofInstant(Instant.ofEpochMilli(ms), ZoneId.systemDefault()).toString()

        fun parseTime(s: String?): Long {
            if (s.isNullOrBlank()) return 0
            return try {
                OffsetDateTime.parse(s).toInstant().toEpochMilli()
            } catch (e: Exception) {
                try {
                    LocalDate.parse(s.take(10)).atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli()
                } catch (e2: Exception) {
                    0
                }
            }
        }

        private fun text(s: String) = JSONObject().put(
            "rich_text", JSONArray().put(JSONObject().put("text", JSONObject().put("content", s.take(1900))))
        )

        fun properties(e: Entry): JSONObject = JSONObject()
            .put("Name", JSONObject().put("title", JSONArray().put(JSONObject().put("text", JSONObject().put("content", e.name.take(1900))))))
            .put("Path", text(e.path))
            .put("Size", JSONObject().put("number", e.size))
            .put("Modified", JSONObject().put("date", JSONObject().put("start", iso(e.mtime))))
            .put("Kind", JSONObject().put("select", JSONObject().put("name", e.kind)))
            .put("Block", text(e.block))
            .put("Page", text(e.page))
            .put("Device", JSONObject().put("select", JSONObject().put("name", e.device)))
            .put("Source", text(e.source))

        private fun plain(props: JSONObject, name: String): String {
            val p = props.optJSONObject(name) ?: return ""
            val arr = p.optJSONArray(p.optString("type")) ?: return ""
            return (0 until arr.length()).joinToString("") { arr.getJSONObject(it).optString("plain_text") }
        }

        fun rowFromPage(root: String, page: JSONObject): Db.IdxRow? {
            val props = page.optJSONObject("properties") ?: return null
            return Db.IdxRow(
                rowId = page.getString("id"),
                root = root,
                name = plain(props, "Name"),
                path = plain(props, "Path"),
                size = props.optJSONObject("Size")?.optLong("number", 0) ?: 0,
                mtime = parseTime(props.optJSONObject("Modified")?.optJSONObject("date")?.optString("start")),
                kind = props.optJSONObject("Kind")?.optJSONObject("select")?.optString("name") ?: "Other",
                block = plain(props, "Block"),
                page = plain(props, "Page"),
                device = props.optJSONObject("Device")?.optJSONObject("select")?.optString("name") ?: "",
                source = plain(props, "Source")
            )
        }
    }

    /** The index database for a backup page; created when [create] is true and none exists yet. */
    fun dbIdFor(root: String, create: Boolean): String? {
        db.indexDbId(root)?.let { return it }
        val found = client.findChildDatabase(root, TITLE)
            ?: if (create) client.createIndexDatabase(root, TITLE) else null
        found?.let { db.setIndexDbId(root, it) }
        return found
    }

    fun add(root: String, dbId: String, e: Entry): String {
        val rowId = client.createRow(dbId, properties(e))
        db.upsertIdx(Db.IdxRow(rowId, root, e.name, e.path, e.size, e.mtime, e.kind, e.block, e.page, e.device, e.source))
        return rowId
    }

    /** A file's content changed: points its row at the new copy. Keeps the name and folder (they may have been changed in the app). */
    fun updateFile(root: String, dbId: String, rowId: String, e: Entry): String {
        val props = JSONObject()
            .put("Size", JSONObject().put("number", e.size))
            .put("Modified", JSONObject().put("date", JSONObject().put("start", iso(e.mtime))))
            .put("Block", properties(e).getJSONObject("Block"))
            .put("Page", properties(e).getJSONObject("Page"))
        return try {
            client.updateRow(rowId, props)
            val old = db.rowById(rowId)
            db.upsertIdx(
                Db.IdxRow(rowId, root, old?.name ?: e.name, old?.path ?: e.path, e.size, e.mtime, e.kind,
                    e.block, e.page, e.device, e.source)
            )
            rowId
        } catch (ex: NotionException) {
            if (ex.status == 404 || ex.status == 400) add(root, dbId, e) else throw ex
        }
    }

    /**
     * Copies new and changed rows from Notion into the phone's local copy.
     * A full refresh also removes rows deleted elsewhere.
     */
    fun pull(root: String, dbId: String, full: Boolean, progress: (Int) -> Unit = {}): Int {
        val (lastPull, _) = db.pullTimes(root)
        val started = System.currentTimeMillis()
        val seen = HashSet<String>()
        var cursor: String? = null
        var count = 0
        do {
            val body = JSONObject().put("page_size", 100)
                .put("sorts", JSONArray().put(JSONObject().put("timestamp", "last_edited_time").put("direction", "ascending")))
            if (!full && lastPull > 0) {
                body.put(
                    "filter", JSONObject().put("timestamp", "last_edited_time")
                        .put("last_edited_time", JSONObject().put("on_or_after", iso(lastPull - 5 * 60_000)))
                )
            }
            if (cursor != null) body.put("start_cursor", cursor)
            val res = client.queryDatabase(dbId, body)
            val arr = res.getJSONArray("results")
            val rows = ArrayList<Db.IdxRow>()
            for (i in 0 until arr.length()) {
                val p = arr.getJSONObject(i)
                if (p.optBoolean("archived") || p.optBoolean("in_trash")) {
                    db.deleteIdx(p.getString("id"))
                    continue
                }
                rowFromPage(root, p)?.let {
                    rows.add(it)
                    seen.add(it.rowId)
                }
            }
            db.upsertIdxBatch(rows)
            count += rows.size
            progress(count)
            cursor = if (res.optBoolean("has_more")) res.optString("next_cursor").takeIf { it.isNotEmpty() && it != "null" } else null
        } while (cursor != null)
        if (full) db.pruneIdx(root, seen)
        db.setPullTimes(root, started, if (full) started else null)
        return count
    }
}
