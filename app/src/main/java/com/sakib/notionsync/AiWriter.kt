package com.sakib.notionsync

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * AI writing helper (like SwiftSlate): finish what you typed in any app with a command such as /im
 * and it's rewritten in place by the AI you chose. Same providers and commands as the laptop app.
 */
object AiWriter {
    data class Provider(val id: String, val label: String, val anthropic: Boolean, val base: String,
                        val model: String, val keyUrl: String)

    val PROVIDERS = listOf(
        Provider("gemini", "Google Gemini", false, "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash", "aistudio.google.com/apikey"),
        Provider("claude", "Claude (Anthropic)", true, "https://api.anthropic.com/v1", "claude-haiku-5-5", "console.anthropic.com"),
        Provider("openai", "ChatGPT (OpenAI)", false, "https://api.openai.com/v1", "gpt-4.1-mini", "platform.openai.com/api-keys"),
        Provider("deepseek", "DeepSeek", false, "https://api.deepseek.com/v1", "deepseek-chat", "platform.deepseek.com"),
        Provider("qwen", "Qwen (Alibaba)", false, "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen-plus", "bailian.console.alibabacloud.com"),
        Provider("grok", "Grok (xAI)", false, "https://api.x.ai/v1", "grok-3-mini", "console.x.ai"),
        Provider("groq", "Groq (fast, free tier)", false, "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "console.groq.com/keys"),
        Provider("mistral", "Mistral", false, "https://api.mistral.ai/v1", "mistral-small-latest", "console.mistral.ai"),
        Provider("openrouter", "OpenRouter (hundreds of models)", false, "https://openrouter.ai/api/v1", "openrouter/auto", "openrouter.ai/keys"),
        Provider("custom", "Other (OpenAI-compatible)", false, "", "", ""),
    )

    val COMMANDS = linkedMapOf(
        "/im" to "Fix the grammar, spelling and punctuation and improve the wording so it reads naturally and clearly. Keep the same meaning, tone and language.",
        "/fix" to "Fix only grammar, spelling and punctuation. Change nothing else.",
        "/formal" to "Rewrite it to sound formal and professional.",
        "/friendly" to "Rewrite it to sound warm, friendly and casual.",
        "/short" to "Make it shorter and more concise, keeping the key points.",
        "/long" to "Expand it with a little more detail, keeping the same tone.",
        "/en" to "Translate it into natural English.",
        "/bn" to "Translate it into natural Bangla (Bangladeshi style; English terms may be written in Bangla script where that's how people normally say them).",
        "/reply" to "Write a good reply to this message (the text is a message someone sent me).",
        "/emoji" to "Add a few fitting emojis, keep the text otherwise the same.",
    )

    private const val SYSTEM = "You are a writing assistant inside the user's keyboard. You receive text the user typed and an " +
        "instruction. Return ONLY the resulting text — no quotes, no explanations, no preface, no markdown. " +
        "Keep line breaks where they make sense. Unless the instruction is to translate, answer in the same " +
        "language as the text."

    private val http = OkHttpClient.Builder().connectTimeout(20, TimeUnit.SECONDS).readTimeout(60, TimeUnit.SECONDS).build()
    private val JSON = "application/json".toMediaType()

    fun provider(id: String) = PROVIDERS.firstOrNull { it.id == id } ?: PROVIDERS[0]

    fun commands(p: Prefs): Map<String, String> {
        val out = LinkedHashMap(COMMANDS)
        try {
            val c = JSONObject(p.writerCustom)
            c.keys().forEach { k -> val v = c.optString(k); if (k.startsWith("/") && v.isNotBlank()) out[k] = v }
        } catch (e: Exception) { }
        return out
    }

    /** The command at the end of the text (with a space or nothing before it), or null. */
    fun findTrigger(p: Prefs, text: String): String? {
        for (t in commands(p).keys.sortedByDescending { it.length }) {
            if (text.endsWith(t)) {
                val before = text.dropLast(t.length)
                if (before.isEmpty() || before.last().isWhitespace()) return t
            }
        }
        return null
    }

    fun key(p: Prefs, id: String = p.writerProvider): String = try { JSONObject(p.writerKeys).optString(id) } catch (e: Exception) { "" }
    fun model(p: Prefs, id: String = p.writerProvider): String =
        (try { JSONObject(p.writerModels).optString(id) } catch (e: Exception) { "" }).ifBlank { provider(id).model }

    fun setKey(p: Prefs, id: String, v: String) { val o = try { JSONObject(p.writerKeys) } catch (e: Exception) { JSONObject() }; o.put(id, v); p.writerKeys = o.toString() }
    fun setModel(p: Prefs, id: String, v: String) { val o = try { JSONObject(p.writerModels) } catch (e: Exception) { JSONObject() }; o.put(id, v); p.writerModels = o.toString() }

    class ModelGone(msg: String, val suggestion: String?) : IOException(msg)

    private val FAMILY = mapOf("gemini" to "flash", "claude" to "haiku", "openai" to "mini", "deepseek" to "chat",
        "qwen" to "plus", "grok" to "mini", "groq" to "llama", "mistral" to "small")
    private val BAD = listOf("embed", "tts", "image", "audio", "vision", "live", "transcribe", "realtime", "search",
        "moderation", "guard", "whisper", "dall", "veo", "imagen", "lite", "nano", "thinking", "learnlm", "gemma")

    /** The newest model of the usual quick family from what the provider offers. */
    fun pickModel(prov: String, names: List<String>): String? {
        val fam = FAMILY[prov] ?: ""
        val good = names.filter { n -> BAD.none { n.lowercase().contains(it) } }
        val pool = good.filter { fam.isNotEmpty() && it.lowercase().contains(fam) }.ifEmpty { good.ifEmpty { names } }
        val stable = pool.filter { !it.lowercase().contains("preview") && !it.lowercase().contains("exp") }.ifEmpty { pool }
        fun ver(n: String) = Regex("\\d+").findAll(n).take(3).map { it.value.toInt() }.toList()
        val cmp = Comparator<String> { a, b ->
            val va = ver(a); val vb = ver(b)
            for (i in 0 until maxOf(va.size, vb.size)) {
                val d = (va.getOrNull(i) ?: -1).compareTo(vb.getOrNull(i) ?: -1)
                if (d != 0) return@Comparator d
            }
            b.length.compareTo(a.length)
        }
        return stable.maxWithOrNull(cmp)
    }

    fun listModels(p: Prefs, prov: String, key: String): List<String> {
        val pr = provider(prov)
        val base = (if (prov == "custom") p.writerBase else pr.base).trimEnd('/')
        val rb = Request.Builder().url("$base/models")
        if (pr.anthropic) rb.header("x-api-key", key).header("anthropic-version", "2023-06-01") else rb.header("Authorization", "Bearer $key")
        return http.newCall(rb.build()).execute().use { r ->
            val s = r.body?.string() ?: ""
            if (!r.isSuccessful) throw IOException(errorText(r.code, s))
            val j = JSONObject(s)
            val arr = j.optJSONArray("data") ?: j.optJSONArray("models") ?: JSONArray()
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.getJSONObject(i)
                o.optString("id").ifBlank { o.optString("name") }.removePrefix("models/").ifBlank { null }
            }.distinct().sorted()
        }
    }

    /** Asks the AI. Runs on a background thread. If the model was retired, switches to a current one. */
    fun rewrite(p: Prefs, instruction: String, text: String): String = try {
        rewriteOnce(p, instruction, text)
    } catch (e: ModelGone) {
        val prov = p.writerProvider
        val old = model(p, prov)
        val new = e.suggestion ?: try { pickModel(prov, listModels(p, prov, key(p, prov))) } catch (x: Exception) { null }
        if (new == null || new == old) throw IOException("The model “$old” isn't available. Type another model name in settings.")
        setModel(p, prov, new)
        rewriteOnce(p, instruction, text)
    }

    private fun rewriteOnce(p: Prefs, instruction: String, text: String): String {
        val prov = provider(p.writerProvider)
        val key = key(p, prov.id)
        if (key.isBlank()) throw IOException("No API key yet: add one in Notion Sync › Settings › Writing helper.")
        val base = (if (prov.id == "custom") p.writerBase else prov.base).trimEnd('/')
        if (base.isBlank()) throw IOException("No address set for this AI.")
        val user = "Instruction: $instruction\n\nText:\n$text"
        val model = model(p, prov.id)
        var out: String
        if (prov.anthropic) {
            val body = JSONObject().put("model", model).put("max_tokens", 2048).put("system", SYSTEM)
                .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", user)))
            val req = Request.Builder().url("$base/messages").post(body.toString().toRequestBody(JSON))
                .header("x-api-key", key).header("anthropic-version", "2023-06-01").build()
            out = http.newCall(req).execute().use { r ->
                val s = r.body?.string() ?: ""
                if (!r.isSuccessful) throw IOException(errorText(r.code, s))
                val arr = JSONObject(s).optJSONArray("content") ?: JSONArray()
                buildString { for (i in 0 until arr.length()) arr.getJSONObject(i).let { if (it.optString("type") == "text") append(it.optString("text")) } }
            }
        } else {
            val body = JSONObject().put("model", model).put("temperature", 0.3)
                .put("messages", JSONArray()
                    .put(JSONObject().put("role", "system").put("content", SYSTEM))
                    .put(JSONObject().put("role", "user").put("content", user)))
            val rb = Request.Builder().url("$base/chat/completions").post(body.toString().toRequestBody(JSON))
                .header("Authorization", "Bearer $key")
            if (prov.id == "openrouter") rb.header("HTTP-Referer", "https://github.com/sakib1322/NotionPhoneSync").header("X-Title", "Notion Sync")
            out = http.newCall(rb.build()).execute().use { r ->
                val s = r.body?.string() ?: ""
                if (!r.isSuccessful) throw IOException(errorText(r.code, s))
                JSONObject(s).getJSONArray("choices").getJSONObject(0).getJSONObject("message").optString("content")
            }
        }
        out = out.trim()
        if (out.length >= 2 && out.first() == out.last() && out.first() in "\"'“”") out = out.substring(1, out.length - 1).trim()
        if (out.isEmpty()) throw IOException("The AI sent back nothing.")
        return out
    }

    private fun errorText(code: Int, body: String): String {
        val msg = try {
            val t = body.trim()
            val j = if (t.startsWith("[")) JSONArray(t).getJSONObject(0) else JSONObject(t)
            j.optJSONObject("error")?.optString("message") ?: j.optString("error").ifBlank { j.optString("message") }
        } catch (e: Exception) { body.take(150) }
        val low = msg.lowercase()
        if (code == 404 || (low.contains("model") && (low.contains("not found") || low.contains("no longer available")
                || low.contains("does not exist") || low.contains("not supported")))) {
            throw ModelGone(msg, Regex("use (?:models/)?([\\w.\\-]+)").find(msg)?.groupValues?.get(1))
        }
        return when (code) {
            401, 403 -> "The API key was refused ($msg)."
            429 -> "Too many requests or out of credit for this AI right now."
            else -> "The AI said: ${msg.ifBlank { code.toString() }}"
        }
    }
}
