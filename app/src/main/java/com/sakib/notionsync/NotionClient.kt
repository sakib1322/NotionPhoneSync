package com.sakib.notionsync

import okhttp3.Call
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.util.concurrent.TimeUnit

class NotionException(val status: Int, val code: String, message: String) : Exception(message)

/** Talks to the Notion API: pages, file uploads and blocks. */
class NotionClient(private val token: String, private val priority: Boolean = false) {

    companion object {
        private const val BASE = "https://api.notion.com/v1"
        private const val VERSION = "2022-06-28"
        private const val SINGLE_PART_MAX = 20L * 1024 * 1024
        private const val PART_SIZE = 10L * 1024 * 1024
        private val JSON_TYPE = "application/json; charset=utf-8".toMediaType()

        private val http = OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(180, TimeUnit.SECONDS)
            .writeTimeout(300, TimeUnit.SECONDS)
            .build()

        // Notion allows about 3 requests per second.
        private val gate = Object()
        private var lastCall = 0L

        @Volatile private var priorityUntil = 0L

        /** Shared-clipboard requests ("priority") go straight through and the backup waits
         *  behind them, so copy and paste never queues behind uploads. */
        private fun throttle(priority: Boolean = false) {
            if (priority) {
                val now = System.currentTimeMillis()
                priorityUntil = now + 1000
                lastCall = maxOf(lastCall, now)
                return
            }
            while (System.currentTimeMillis() < priorityUntil) Thread.sleep(100)
            synchronized(gate) {
                val wait = lastCall + 350 - System.currentTimeMillis()
                if (wait > 0) Thread.sleep(wait)
                lastCall = System.currentTimeMillis()
            }
        }

        /** Pulls the page ID out of a Notion link (or accepts a bare ID). */
        fun parsePageId(input: String): String? {
            val clean = input.trim().substringBefore('?').substringBefore('#')
            val plain = Regex("[0-9a-fA-F]{32}").findAll(clean).lastOrNull()?.value
            val dashed = Regex("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
                .findAll(clean).lastOrNull()?.value
            val h = (plain ?: dashed ?: return null).replace("-", "").lowercase()
            return "${h.substring(0, 8)}-${h.substring(8, 12)}-${h.substring(12, 16)}-${h.substring(16, 20)}-${h.substring(20)}"
        }
    }

    @Volatile private var inFlight: Call? = null
    @Volatile private var cancelled = false

    /** Aborts the request in progress and refuses new ones (used when the sync is stopped). */
    fun cancel() {
        cancelled = true
        inFlight?.cancel()
    }

    private fun req(path: String): Request.Builder = Request.Builder()
        .url(BASE + path)
        .header("Authorization", "Bearer $token")
        .header("Notion-Version", VERSION)

    private fun call(build: () -> Request): JSONObject {
        var attempt = 0
        while (true) {
            attempt++
            if (cancelled) throw IOException("Sync stopped")
            throttle(priority)
            var status = 0
            var body = ""
            var retryAfter: String? = null
            try {
                val c = http.newCall(build())
                inFlight = c
                if (cancelled) c.cancel()
                c.execute().use { r ->
                    status = r.code
                    body = r.body?.string() ?: ""
                    retryAfter = r.header("Retry-After")
                }
            } catch (e: IOException) {
                if (cancelled || attempt >= 4) throw e
                Thread.sleep((2000L shl attempt).coerceAtMost(30_000L))
                continue
            }
            if (status in 200..299) return if (body.isBlank()) JSONObject() else JSONObject(body)

            val json = try { JSONObject(body) } catch (e: Exception) { null }
            val code = json?.optString("code") ?: ""
            val msg = json?.optString("message")?.takeIf { it.isNotEmpty() } ?: body.take(300)
            val retriable = status == 429 || status == 409 || status >= 500
            if (retriable && attempt < 6) {
                val seconds = retryAfter?.toLongOrNull() ?: (1L shl attempt)
                Thread.sleep((seconds * 1000).coerceAtMost(60_000L))
                continue
            }
            throw NotionException(status, code, msg)
        }
    }

    private fun get(path: String) = call { req(path).get().build() }
    fun post(path: String, json: JSONObject) =
        call { req(path).post(json.toString().toRequestBody(JSON_TYPE)).build() }
    private fun patch(path: String, json: JSONObject) =
        call { req(path).patch(json.toString().toRequestBody(JSON_TYPE)).build() }

    fun pageTitle(pageId: String): String {
        val props = get("/pages/$pageId").optJSONObject("properties") ?: return "Untitled"
        for (key in props.keys()) {
            val p = props.getJSONObject(key)
            if (p.optString("type") == "title") {
                val arr = p.getJSONArray("title")
                val t = (0 until arr.length()).joinToString("") { arr.getJSONObject(it).optString("plain_text") }
                return t.ifBlank { "Untitled" }
            }
        }
        return "Untitled"
    }

    class ChildPage(val results: List<JSONObject>, val next: String?)

    /** One page (up to 100) of the blocks inside a page, for the Files browser. */
    fun children(pageId: String, cursor: String?): ChildPage {
        val q = "/blocks/$pageId/children?page_size=100" + (cursor?.let { "&start_cursor=$it" } ?: "")
        val res = get(q)
        val arr = res.getJSONArray("results")
        val list = (0 until arr.length()).map { arr.getJSONObject(it) }
        val next = if (res.optBoolean("has_more")) {
            res.optString("next_cursor").takeIf { it.isNotEmpty() && it != "null" }
        } else null
        return ChildPage(list, next)
    }

    fun block(blockId: String): JSONObject = get("/blocks/$blockId")

    // ---- index database ------------------------------------------------------------------

    /** Finds a database directly inside a page by its title. */
    fun findChildDatabase(pageId: String, title: String): String? {
        var cursor: String? = null
        do {
            val res = children(pageId, cursor)
            for (b in res.results) {
                if (b.optString("type") == "child_database" &&
                    b.optJSONObject("child_database")?.optString("title") == title
                ) return b.getString("id")
            }
            cursor = res.next
        } while (cursor != null)
        return null
    }

    fun createIndexDatabase(parentId: String, title: String): String {
        fun empty() = JSONObject()
        val props = JSONObject()
            .put("Name", JSONObject().put("title", empty()))
            .put("Path", JSONObject().put("rich_text", empty()))
            .put("Size", JSONObject().put("number", JSONObject().put("format", "number")))
            .put("Modified", JSONObject().put("date", empty()))
            .put("Kind", JSONObject().put("select", empty()))
            .put("Block", JSONObject().put("rich_text", empty()))
            .put("Page", JSONObject().put("rich_text", empty()))
            .put("Device", JSONObject().put("select", empty()))
            .put("Source", JSONObject().put("rich_text", empty()))
        val body = JSONObject()
            .put("parent", JSONObject().put("type", "page_id").put("page_id", parentId))
            .put("title", JSONArray().put(JSONObject().put("type", "text").put("text", JSONObject().put("content", title))))
            .put("properties", props)
        return post("/databases", body).getString("id")
    }

    fun search(body: JSONObject): JSONObject = post("/search", body)

    fun patchDatabase(dbId: String, body: JSONObject): JSONObject = patch("/databases/$dbId", body)

    fun createDatabase(body: JSONObject): String = post("/databases", body).getString("id")

    fun createRow(dbId: String, properties: JSONObject): String =
        post("/pages", JSONObject().put("parent", JSONObject().put("database_id", dbId)).put("properties", properties))
            .getString("id")

    fun updateRow(rowId: String, properties: JSONObject) {
        patch("/pages/$rowId", JSONObject().put("properties", properties))
    }

    /** Moves a page (or index row) to Notion's trash. */
    fun archivePage(pageId: String) {
        patch("/pages/$pageId", JSONObject().put("archived", true))
    }

    fun queryDatabase(dbId: String, body: JSONObject): JSONObject = post("/databases/$dbId/query", body)

    fun updateCaption(blockId: String, type: String, caption: String) {
        val cap = JSONArray().put(JSONObject().put("type", "text").put("text", JSONObject().put("content", caption.take(1900))))
        patch("/blocks/$blockId", JSONObject().put(type, JSONObject().put("caption", cap)))
    }

    /** Top-level pages this integration can see (e.g. Phone Backup, Laptop Backup) as ID -> title. */
    fun topLevelPages(): List<Pair<String, String>> {
        val out = ArrayList<Pair<String, String>>()
        var cursor: String? = null
        var rounds = 0
        do {
            val body = JSONObject()
                .put("filter", JSONObject().put("property", "object").put("value", "page"))
                .put("page_size", 100)
            if (cursor != null) body.put("start_cursor", cursor)
            val res = post("/search", body)
            val arr = res.getJSONArray("results")
            for (i in 0 until arr.length()) {
                val p = arr.getJSONObject(i)
                if (p.optJSONObject("parent")?.optString("type") != "workspace") continue
                val props = p.optJSONObject("properties") ?: continue
                var title = "Untitled"
                for (k in props.keys()) {
                    val prop = props.getJSONObject(k)
                    if (prop.optString("type") == "title") {
                        val t = prop.getJSONArray("title")
                        title = (0 until t.length()).joinToString("") { t.getJSONObject(it).optString("plain_text") }
                            .ifBlank { "Untitled" }
                    }
                }
                out.add(p.getString("id") to title)
            }
            cursor = if (res.optBoolean("has_more")) res.optString("next_cursor").takeIf { it.isNotEmpty() && it != "null" } else null
            rounds++
        } while (cursor != null && rounds < 3)
        return out
    }

    /** Sub-pages directly inside a page, as title -> page ID. */
    fun childPages(pageId: String): MutableMap<String, String> {
        val out = HashMap<String, String>()
        var cursor: String? = null
        do {
            val q = "/blocks/$pageId/children?page_size=100" + (cursor?.let { "&start_cursor=$it" } ?: "")
            val res = get(q)
            val arr = res.getJSONArray("results")
            for (i in 0 until arr.length()) {
                val b = arr.getJSONObject(i)
                if (b.optString("type") == "child_page") {
                    out.putIfAbsent(b.getJSONObject("child_page").optString("title"), b.getString("id"))
                }
            }
            cursor = if (res.optBoolean("has_more")) {
                res.optString("next_cursor").takeIf { it.isNotEmpty() && it != "null" }
            } else null
        } while (cursor != null)
        return out
    }

    /** Captions of the files already on a page, as caption -> block ID. */
    fun mediaCaptions(pageId: String): Map<String, String> {
        val types = setOf("image", "video", "audio", "pdf", "file")
        val out = HashMap<String, String>()
        var cursor: String? = null
        do {
            val q = "/blocks/$pageId/children?page_size=100" + (cursor?.let { "&start_cursor=$it" } ?: "")
            val res = get(q)
            val arr = res.getJSONArray("results")
            for (i in 0 until arr.length()) {
                val b = arr.getJSONObject(i)
                val type = b.optString("type")
                if (type !in types) continue
                val cap = b.optJSONObject(type)?.optJSONArray("caption") ?: continue
                val text = (0 until cap.length()).joinToString("") { cap.getJSONObject(it).optString("plain_text") }
                if (text.isNotEmpty()) out.putIfAbsent(text, b.getString("id"))
            }
            cursor = if (res.optBoolean("has_more")) {
                res.optString("next_cursor").takeIf { it.isNotEmpty() && it != "null" }
            } else null
        } while (cursor != null)
        return out
    }

    fun createPage(parentId: String, title: String): String {
        val titleArr = JSONArray().put(JSONObject().put("text", JSONObject().put("content", title.take(1900))))
        val body = JSONObject()
            .put("parent", JSONObject().put("page_id", parentId))
            .put("properties", JSONObject().put("title", JSONObject().put("title", titleArr)))
        return post("/pages", body).getString("id")
    }

    /** Uploads a file (in 10 MB parts when it is over 20 MB) and returns the upload ID. */
    fun uploadFile(file: File, uploadName: String, mime: String, size: Long, onPart: () -> Unit = {}): String {
        return if (size <= SINGLE_PART_MAX) {
            val id = post(
                "/file_uploads",
                JSONObject().put("mode", "single_part").put("filename", uploadName).put("content_type", mime)
            ).getString("id")
            sendPart(id, file, uploadName, 0, size, mime, null)
            id
        } else {
            val parts = ((size + PART_SIZE - 1) / PART_SIZE).toInt()
            val id = post(
                "/file_uploads",
                JSONObject().put("mode", "multi_part").put("number_of_parts", parts)
                    .put("filename", uploadName).put("content_type", mime)
            ).getString("id")
            for (i in 0 until parts) {
                val offset = i * PART_SIZE
                sendPart(id, file, uploadName, offset, minOf(PART_SIZE, size - offset), mime, i + 1)
                onPart()
            }
            post("/file_uploads/$id/complete", JSONObject())
            id
        }
    }

    private fun sendPart(id: String, file: File, name: String, offset: Long, length: Long, mime: String, part: Int?) {
        call {
            val fileBody = RangeBody(file, offset, length, mime.toMediaTypeOrNull())
            val mb = MultipartBody.Builder().setType(MultipartBody.FORM)
                .addFormDataPart("file", name, fileBody)
            if (part != null) mb.addFormDataPart("part_number", part.toString())
            req("/file_uploads/$id/send").post(mb.build()).build()
        }
    }

    /** Adds the uploaded file to the end of a page and returns the new block's ID. */
    fun attach(pageId: String, uploadId: String, blockType: String, caption: String): String {
        val captionArr = JSONArray().put(
            JSONObject().put("type", "text").put("text", JSONObject().put("content", caption.take(1900)))
        )
        val inner = JSONObject()
            .put("type", "file_upload")
            .put("file_upload", JSONObject().put("id", uploadId))
            .put("caption", captionArr)
        val block = JSONObject().put("object", "block").put("type", blockType).put(blockType, inner)
        val res = patch("/blocks/$pageId/children", JSONObject().put("children", JSONArray().put(block)))
        val results = res.getJSONArray("results")
        return results.getJSONObject(results.length() - 1).getString("id")
    }

    fun deleteBlock(blockId: String) {
        call { req("/blocks/$blockId").delete().build() }
    }

    /** Streams one slice of a file without loading it into memory. */
    private class RangeBody(
        private val file: File,
        private val offset: Long,
        private val length: Long,
        private val type: MediaType?
    ) : RequestBody() {
        override fun contentType(): MediaType? = type
        override fun contentLength(): Long = length
        override fun writeTo(sink: BufferedSink) {
            RandomAccessFile(file, "r").use { raf ->
                raf.seek(offset)
                val buf = ByteArray(64 * 1024)
                var left = length
                while (left > 0) {
                    val n = raf.read(buf, 0, minOf(buf.size.toLong(), left).toInt())
                    if (n < 0) throw IOException("File changed while uploading")
                    sink.write(buf, 0, n)
                    left -= n
                }
            }
        }
    }
}
