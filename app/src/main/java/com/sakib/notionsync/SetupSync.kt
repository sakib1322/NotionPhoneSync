package com.sakib.notionsync

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import android.util.Base64
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Your setup (Notion connection, backup pages, options) so a new phone or laptop
 * can pick up where you left off: saved in a hidden app folder in your own Google
 * Drive, or moved with a transfer code / QR. Your files themselves stay in Notion.
 */
object SetupSync {
    private const val FILE = "notion-sync-setup.json"
    private const val PREFIX = "NSYNC1:"
    private val JSON = "application/json; charset=utf-8".toMediaType()
    private val http = OkHttpClient.Builder().connectTimeout(20, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS).build()

    // ---- what is saved ---------------------------------------------------------------

    /** This phone's part of the setup, merged into [base] (which may hold the laptop's part). */
    fun payload(ctx: Context, base: JSONObject = JSONObject()): JSONObject {
        val p = Prefs(ctx)
        val phone = JSONObject()
            .put("root_page", p.rootPageId).put("root_title", p.rootTitle)
            .put("wifi_only", p.wifiOnly).put("charging_only", p.chargingOnly).put("skip_hidden", p.skipHidden)
            .put("excluded", p.excluded).put("auto_sync", p.autoSync).put("interval", p.intervalMinutes)
            .put("cache_limit_mb", p.cacheLimitMb).put("cloud_previews", p.cloudPreviews)
        val sources = JSONArray()
        Sources.list(p).forEach { (id, t) -> sources.put(JSONObject().put("id", id).put("title", t)) }
        return base.put("version", 1)
            .put("notion_token", p.token)
            .put("phone", phone)
            .put("sources", mergeSources(base.optJSONArray("sources"), sources))
            .put("updated", System.currentTimeMillis())
    }

    private fun mergeSources(a: JSONArray?, b: JSONArray): JSONArray {
        val seen = LinkedHashMap<String, String>()
        for (arr in listOfNotNull(a, b)) for (i in 0 until arr.length()) {
            val o = arr.getJSONObject(i)
            seen.putIfAbsent(o.optString("id"), o.optString("title"))
        }
        val out = JSONArray()
        seen.forEach { (id, t) -> if (id.isNotEmpty()) out.put(JSONObject().put("id", id).put("title", t)) }
        return out
    }

    /** Backup pages mentioned in a setup, as (id, title). */
    fun sourcesIn(json: JSONObject): List<Pair<String, String>> {
        val out = ArrayList<Pair<String, String>>()
        json.optJSONObject("phone")?.let { if (it.optString("root_page").isNotEmpty()) out.add(it.optString("root_page") to it.optString("root_title", "Phone Backup")) }
        json.optJSONObject("laptop")?.let { if (it.optString("root_page").isNotEmpty()) out.add(it.optString("root_page") to it.optString("root_title", "Laptop Backup")) }
        json.optJSONArray("sources")?.let { arr ->
            for (i in 0 until arr.length()) arr.getJSONObject(i).let { out.add(it.optString("id") to it.optString("title")) }
        }
        return out.distinctBy { it.first }.filter { it.first.isNotEmpty() }
    }

    /**
     * Uses a saved setup on this phone. Returns false when it has no phone backup page
     * (e.g. a code made on the laptop): then the app asks which page to use.
     */
    fun apply(ctx: Context, json: JSONObject): Boolean {
        val p = Prefs(ctx)
        val token = json.optString("notion_token")
        if (token.isNotEmpty()) p.token = token
        // Other backups (e.g. the laptop's) become browsable in Files.
        val extra = p.extraSources.lines().filter { it.isNotBlank() }.toMutableList()
        for ((id, t) in sourcesIn(json)) if (extra.none { it.startsWith("$id|") }) extra.add("$id|$t")
        p.extraSources = extra.joinToString("\n")
        val phone = json.optJSONObject("phone") ?: return p.rootPageId.isNotEmpty()
        val root = phone.optString("root_page")
        if (root.isEmpty()) return p.rootPageId.isNotEmpty()
        if (p.rootPageId.isNotEmpty() && p.rootPageId != root) Db.get(ctx).resetAll()
        p.rootPageId = root
        p.rootTitle = phone.optString("root_title", "Phone Backup")
        p.wifiOnly = phone.optBoolean("wifi_only", p.wifiOnly)
        p.chargingOnly = phone.optBoolean("charging_only", p.chargingOnly)
        p.skipHidden = phone.optBoolean("skip_hidden", p.skipHidden)
        p.excluded = phone.optString("excluded", p.excluded)
        p.intervalMinutes = phone.optInt("interval", p.intervalMinutes)
        p.cacheLimitMb = phone.optInt("cache_limit_mb", p.cacheLimitMb)
        p.cloudPreviews = phone.optBoolean("cloud_previews", p.cloudPreviews)
        p.autoSync = phone.optBoolean("auto_sync", p.autoSync)
        return true
    }

    // ---- transfer code / QR ---------------------------------------------------------------

    fun transferCode(ctx: Context): String {
        val bytes = payload(ctx).toString().toByteArray()
        return PREFIX + Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
    }

    fun readCode(text: String): JSONObject? {
        val t = text.trim()
        if (!t.startsWith(PREFIX)) return null
        return try {
            JSONObject(String(Base64.decode(t.removePrefix(PREFIX), Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)))
        } catch (e: Exception) {
            null
        }
    }

    fun qr(text: String, size: Int): Bitmap {
        val m = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, size, size, mapOf(EncodeHintType.MARGIN to 1))
        val bmp = Bitmap.createBitmap(size, size, Bitmap.Config.RGB_565)
        for (x in 0 until size) for (y in 0 until size) bmp.setPixel(x, y, if (m[x, y]) Color.BLACK else Color.WHITE)
        return bmp
    }

    // ---- Google Drive (hidden app folder) ---------------------------------------------------

    private fun findFile(token: String): String? {
        val url = "https://www.googleapis.com/drive/v3/files?spaces=appDataFolder&fields=files(id,modifiedTime)&q=name%3D%27$FILE%27"
        http.newCall(Request.Builder().url(url).header("Authorization", "Bearer $token").build()).execute().use { r ->
            if (!r.isSuccessful) throw IOException("Google Drive answered ${r.code}")
            val files = JSONObject(r.body?.string() ?: "{}").optJSONArray("files") ?: return null
            return if (files.length() > 0) files.getJSONObject(0).getString("id") else null
        }
    }

    /** The setup saved in your Google account, or null if there isn't one yet. */
    fun download(token: String): JSONObject? {
        val id = findFile(token) ?: return null
        http.newCall(
            Request.Builder().url("https://www.googleapis.com/drive/v3/files/$id?alt=media")
                .header("Authorization", "Bearer $token").build()
        ).execute().use { r ->
            if (!r.isSuccessful) throw IOException("Google Drive answered ${r.code}")
            return JSONObject(r.body?.string() ?: "{}")
        }
    }

    /** Saves this phone's setup to your Google account (keeping the laptop's part). */
    fun upload(ctx: Context, token: String) {
        val id = findFile(token)
        val base = if (id != null) (download(token) ?: JSONObject()) else JSONObject()
        val body = payload(ctx, base).toString().toRequestBody(JSON)
        val req = if (id == null) {
            val meta = JSONObject().put("name", FILE).put("parents", JSONArray().put("appDataFolder")).toString().toRequestBody(JSON)
            val multipart = MultipartBody.Builder().setType("multipart/related".toMediaType())
                .addPart(meta).addPart(body).build()
            Request.Builder().url("https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart").post(multipart)
        } else {
            Request.Builder().url("https://www.googleapis.com/upload/drive/v3/files/$id?uploadType=media").patch(body)
        }
        http.newCall(req.header("Authorization", "Bearer $token").build()).execute().use { r ->
            if (!r.isSuccessful) throw IOException("Google Drive answered ${r.code}: ${r.body?.string()?.take(200)}")
        }
        Prefs(ctx).googleSavedAt = System.currentTimeMillis()
    }
}
