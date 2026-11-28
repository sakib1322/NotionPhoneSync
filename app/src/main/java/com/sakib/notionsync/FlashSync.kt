package com.sakib.notionsync

import android.content.Context
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Keeps flashcards the same on all your devices, through Notion (exactly like the laptop app):
 * a page "Flashcards (Notion Sync)" holds the newest copy (flashcards-sync|device|time) and the
 * pictures (flashcards-media|name). Download newest -> merge -> upload merged -> remove older copies.
 */
object FlashSync {
    private const val PAGE_TITLE = "Flashcards (Notion Sync)"
    private const val SYNC_PREFIX = "flashcards-sync|"
    private const val MEDIA_PREFIX = "flashcards-media|"
    @Volatile var busy = false
    @Volatile var lastError = ""
    private val http = OkHttpClient.Builder().connectTimeout(20, TimeUnit.SECONDS).readTimeout(120, TimeUnit.SECONDS).build()

    private fun caption(b: JSONObject): String {
        val inner = b.optJSONObject(b.optString("type")) ?: return ""
        val arr = inner.optJSONArray("caption") ?: return ""
        return (0 until arr.length()).joinToString("") { arr.getJSONObject(it).optString("plain_text") }
    }

    private fun fileUrl(b: JSONObject): String? {
        val inner = b.optJSONObject(b.optString("type")) ?: return null
        return inner.optJSONObject("file")?.optString("url")?.takeIf { it.isNotEmpty() }
            ?: inner.optJSONObject("external")?.optString("url")?.takeIf { it.isNotEmpty() }
    }

    private fun children(client: NotionClient, page: String): List<JSONObject> {
        val out = mutableListOf<JSONObject>()
        var cursor: String? = null
        do {
            val p = client.children(page, cursor)
            out.addAll(p.results)
            cursor = p.next
        } while (cursor != null)
        return out
    }

    private fun page(ctx: Context, db: FlashDb, client: NotionClient): String {
        db.getMeta("sync_page")?.takeIf { it.isNotEmpty() }?.let { return it }
        val prefs = Prefs(ctx)
        val home = (try { Registry(ctx).home() } catch (_: Exception) { null }) ?: prefs.rootPageId.ifEmpty { null }
            ?: throw IllegalStateException("connect Notion first")
        val existing = children(client, home).filter { it.optString("type") == "child_page" && it.optJSONObject("child_page")?.optString("title") == PAGE_TITLE }
            .minByOrNull { it.optString("created_time") }
        val id = existing?.getString("id") ?: client.createPage(home, PAGE_TITLE)
        db.setMeta("sync_page", id)
        return id
    }

    fun status(db: FlashDb): String {
        if (busy) return "Syncing with your other devices…"
        if (lastError.isNotEmpty()) return "Sync: $lastError"
        val at = db.getMeta("sync_at")?.toDoubleOrNull() ?: return "Not synced yet."
        val mins = ((Fsrs.now() - at) / 60).toInt()
        return "Synced with your other devices " + when {
            mins < 1 -> "just now"; mins < 60 -> "$mins min ago"
            else -> java.text.SimpleDateFormat("'at' HH:mm, d MMM", java.util.Locale.getDefault()).format(java.util.Date((at * 1000).toLong()))
        }
    }

    fun needed(db: FlashDb, maxAge: Int = 600): Boolean {
        val at = db.getMeta("sync_at")?.toDoubleOrNull() ?: 0.0
        return db.signature() != db.getMeta("sync_sig") || Fsrs.now() - at > maxAge
    }

    private fun download(url: String): ByteArray =
        http.newCall(Request.Builder().url(url).build()).execute().use { r ->
            if (!r.isSuccessful) throw java.io.IOException("Download failed (${r.code})")
            r.body!!.bytes()
        }

    /** One full sync (call from a background thread). Returns a summary. */
    @Synchronized
    fun run(ctx: Context): String {
        val prefs = Prefs(ctx)
        if (prefs.token.isBlank()) throw IllegalStateException("connect Notion first")
        val db = FlashDb.get(ctx)
        busy = true
        try {
            val client = NotionClient(prefs.token)
            var pg = page(ctx, db, client)
            val blocks = try { children(client, pg) } catch (e: NotionException) {
                if (e.status == 404 || e.status == 400) { db.setMeta("sync_page", ""); pg = page(ctx, db, client); children(client, pg) } else throw e
            }
            val copies = mutableListOf<Pair<Double, JSONObject>>()
            val media = HashMap<String, JSONObject>()
            for (b in blocks) {
                val cap = caption(b)
                if (cap.startsWith(SYNC_PREFIX)) copies.add((cap.substringAfterLast('|').toDoubleOrNull() ?: 0.0) to b)
                else if (cap.startsWith(MEDIA_PREFIX)) media[cap.removePrefix(MEDIA_PREFIX)] = b
            }
            copies.sortBy { it.first }
            var summary = "nothing new"
            var newestT = 0.0
            copies.lastOrNull()?.let { (t, b) ->
                newestT = t
                if (fmt(t) != db.getMeta("sync_seen")) {
                    val url = fileUrl(client.block(b.getString("id"))) ?: throw java.io.IOException("No link to the synced file")
                    summary = db.mergeSync(JSONObject(String(download(url), Charsets.UTF_8)))
                }
            }
            val device = Devices.myName(ctx).replace("|", "/")
            var mine = db.exportSync(device)
            val names = mine.getJSONArray("media")
            for (i in 0 until names.length()) {
                val n = names.getString(i)
                val f = db.mediaFile(n)
                val b = media[n]
                if (b != null && !f.isFile) fileUrl(client.block(b.getString("id")))?.let { u -> try { db.storeMedia(n, download(u)) } catch (_: Exception) {} }
            }
            mine = db.exportSync(device)
            val names2 = mine.getJSONArray("media")
            for (i in 0 until names2.length()) {
                val n = names2.getString(i)
                val f = db.mediaFile(n)
                if (!media.containsKey(n) && f.isFile) {
                    val up = client.uploadFile(f, FileTypes.uploadName(n), FileTypes.mime(n), f.length())
                    client.attach(pg, up, "file", MEDIA_PREFIX + n)
                }
            }
            val t = Math.round(Fsrs.now() * 1000) / 1000.0
            val tmp = File(ctx.cacheDir, "flashcards-sync.txt")
            tmp.writeText(mine.toString(), Charsets.UTF_8)
            try {
                val up = client.uploadFile(tmp, "flashcards-sync.txt", "text/plain", tmp.length())
                client.attach(pg, up, "file", "$SYNC_PREFIX$device|${fmt(t)}")
            } finally { tmp.delete() }
            for ((ct, b) in copies) if (ct <= newestT) try { client.deleteBlock(b.getString("id")) } catch (_: Exception) {}
            db.setMeta("sync_seen", fmt(t))
            db.setMeta("sync_sig", db.signature())
            db.setMeta("sync_at", Fsrs.now().toString())
            lastError = ""
            if (summary != "nothing new") Logger.log(ctx, "Flashcards synced with your other devices: $summary.")
            return summary
        } catch (e: Exception) {
            lastError = (e.message ?: e.toString()).take(160)
            throw e
        } finally { busy = false }
    }

    /** Same text for a time as the laptop writes (e.g. 1764301234.567). */
    private fun fmt(t: Double): String {
        val s = String.format(java.util.Locale.US, "%.3f", t).trimEnd('0').trimEnd('.')
        return if ('.' in s) s else "$s.0"
    }
}
