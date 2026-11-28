package com.sakib.notionsync

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.text.Html
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import java.util.Calendar
import java.util.zip.ZipEntry
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream
import kotlin.math.exp
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.roundToLong

/**
 * Flashcards: decks, cards and spaced repetition with FSRS (the modern scheduler Anki also uses).
 * Same rules and the same .apkg format as the laptop app, so decks move freely between them,
 * Anki and AnkiDroid. Times are seconds since 1970 (as a Double), like on the laptop.
 */
object Fsrs {
    const val NEW = 0; const val LEARNING = 1; const val REVIEW = 2; const val RELEARNING = 3
    const val AGAIN = 1; const val HARD = 2; const val GOOD = 3; const val EASY = 4
    const val DAY_STARTS_HOUR = 4
    const val LEARN_AHEAD = 20 * 60.0
    private val W = doubleArrayOf(0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031, 1.6474, 0.1367,
        1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755)
    private const val DECAY = -0.5
    private const val FACTOR = 19.0 / 81.0
    private const val MAX_IVL = 36500
    private val LEARN_STEPS = intArrayOf(1, 10)
    private val RELEARN_STEPS = intArrayOf(10)

    private fun clamp(x: Double, lo: Double, hi: Double) = max(lo, min(hi, x))
    fun retrievability(elapsedDays: Double, s: Double) = if (s <= 0) 0.0 else (1 + FACTOR * max(0.0, elapsedDays) / s).pow(DECAY)
    fun intervalDays(s: Double, retention: Double): Int =
        clamp((s / FACTOR * (retention.pow(1 / DECAY) - 1)).roundToInt().toDouble(), 1.0, MAX_IVL.toDouble()).toInt()
    private fun initS(g: Int) = max(0.1, W[g - 1])
    private fun initD(g: Int) = clamp(W[4] - (g - 3) * W[5], 1.0, 10.0)
    private fun nextD(d: Double, g: Int): Double {
        val nd = d - W[6] * (g - 3)
        return clamp(W[7] * initD(GOOD) + (1 - W[7]) * nd, 1.0, 10.0)
    }
    private fun recallS(d: Double, s: Double, r: Double, g: Int): Double {
        val hard = if (g == HARD) W[15] else 1.0
        val easy = if (g == EASY) W[16] else 1.0
        return s * (1 + exp(W[8]) * (11 - d) * s.pow(-W[9]) * (exp(W[10] * (1 - r)) - 1) * hard * easy)
    }
    private fun forgetS(d: Double, s: Double, r: Double) =
        max(0.1, min(s, W[11] * d.pow(-W[12]) * ((s + 1).pow(W[13]) - 1) * exp(W[14] * (1 - r))))

    fun dayStart(t: Double = now()): Double {
        val c = Calendar.getInstance()
        c.timeInMillis = ((t - DAY_STARTS_HOUR * 3600) * 1000).toLong()
        c.set(Calendar.HOUR_OF_DAY, 0); c.set(Calendar.MINUTE, 0); c.set(Calendar.SECOND, 0); c.set(Calendar.MILLISECOND, 0)
        return c.timeInMillis / 1000.0 + DAY_STARTS_HOUR * 3600
    }

    fun now() = System.currentTimeMillis() / 1000.0

    /** What happens to the card if answered with grade g. Returns a changed copy. */
    fun schedule(card: Card, g: Int, now: Double = now(), retention: Double = 0.9, fuzz: Boolean = true): Card {
        val c = card.copy()
        val s = c.stability; val d = c.difficulty
        val elapsed = if (c.lastReview > 0) max(0.0, (now - c.lastReview) / 86400) else 0.0
        c.reps += 1
        c.lastReview = now
        fun toReview(stab: Double) {
            var ivl = intervalDays(stab, retention)
            if (fuzz && ivl > 2) {
                val spread = max(1, (ivl * 0.05).roundToInt())
                ivl = clamp((ivl + (-spread..spread).random()).toDouble(), 2.0, MAX_IVL.toDouble()).toInt()
            }
            c.state = REVIEW; c.step = 0
            c.due = dayStart(now) + ivl * 86400.0
            c.ivl = ivl
        }
        fun learnStep(steps: IntArray, i: Int) { c.step = i; c.due = now + steps[i] * 60.0; c.ivl = 0 }

        when (card.state) {
            NEW -> {
                c.stability = initS(g); c.difficulty = initD(g)
                if (g == EASY) toReview(c.stability) else {
                    c.state = LEARNING
                    when (g) {
                        AGAIN -> learnStep(LEARN_STEPS, 0)
                        HARD -> { c.step = 0; c.due = now + (LEARN_STEPS[0] + LEARN_STEPS[min(1, LEARN_STEPS.size - 1)]) / 2.0 * 60; c.ivl = 0 }
                        else -> if (LEARN_STEPS.size > 1) learnStep(LEARN_STEPS, 1) else toReview(c.stability)
                    }
                }
            }
            LEARNING, RELEARNING -> {
                val steps = if (card.state == LEARNING) LEARN_STEPS else RELEARN_STEPS
                val step = c.step
                c.difficulty = nextD(if (d > 0) d else initD(GOOD), g)
                when (g) {
                    AGAIN -> { c.stability = max(0.1, (if (s > 0) s else initS(AGAIN)) * 0.8); learnStep(steps, 0) }
                    HARD -> { c.due = now + steps[min(step, steps.size - 1)] * 60.0 * (if (step == 0) 1.5 else 1.0); c.ivl = 0 }
                    GOOD -> if (step + 1 < steps.size) learnStep(steps, step + 1) else {
                        c.stability = max(if (s > 0) s else initS(GOOD), if (card.state == LEARNING) initS(GOOD) else s)
                        toReview(c.stability)
                    }
                    else -> {
                        c.stability = max((if (s > 0) s else initS(EASY)) * 1.5, if (card.state == LEARNING) initS(EASY) else s)
                        toReview(c.stability)
                    }
                }
            }
            else -> {
                val r = retrievability(elapsed, s)
                c.difficulty = nextD(d, g)
                if (g == AGAIN) {
                    c.stability = forgetS(d, s, r); c.lapses += 1; c.state = RELEARNING; learnStep(RELEARN_STEPS, 0)
                } else {
                    c.stability = recallS(d, s, r, g); toReview(c.stability)
                }
            }
        }
        return c
    }

    fun human(seconds: Double): String {
        if (seconds < 60) return "<1m"
        val m = seconds / 60
        if (m < 60) return "${m.roundToInt()}m"
        val h = m / 60
        if (h < 24) return "${h.roundToInt()}h"
        val d = h / 24
        if (d < 30) return "${d.roundToInt()}d"
        if (d < 365) return String.format("%.1fmo", d / 30).replace(".0mo", "mo")
        return String.format("%.1fy", d / 365).replace(".0y", "y")
    }
}

data class Card(
    val id: Long, val noteId: Long, val ord: Int, val deckId: Long,
    var state: Int, var step: Int, var due: Double, var ivl: Int, var stability: Double, var difficulty: Double,
    var reps: Int, var lapses: Int, var lastReview: Double, val suspended: Boolean,
    val front: String = "", val back: String = "", var revlogId: Long = 0
) {
    val question get() = if (ord == 0) front else back
    val answer get() = if (ord == 0) back else front
}

data class Deck(val id: Long, val name: String, val newPerDay: Int, val reviewsPerDay: Int, val retention: Double)
data class Note(val id: Long, val deckId: Long, val front: String, val back: String, val tags: String, val reverse: Boolean)
data class NoteRow(val id: Long, val front: String, val back: String, val tags: String, val deck: String,
                   val due: Double, val ivl: Int, val state: Int, val suspended: Boolean)

object FlashText {
    val IMG = Regex("\\[img:([^\\]]+)\\]")
    private val CLOZE = Regex("\\{\\{c(\\d+)::(.*?)(?:::(.*?))?\\}\\}", RegexOption.DOT_MATCHES_ALL)

    fun htmlToText(s: String?): String {
        if (s.isNullOrEmpty()) return ""
        var t = s
        t = Regex("<img[^>]*?src\\s*=\\s*(?:\"([^\"]*)\"|'([^']*)'|([^\\s>]+))[^>]*>", RegexOption.IGNORE_CASE)
            .replace(t) { m -> "[img:" + unescape(m.groupValues[1].ifEmpty { m.groupValues[2] }.ifEmpty { m.groupValues[3] }) + "]" }
        t = Regex("\\[sound:[^\\]]*\\]").replace(t, "")
        t = Regex("<br\\s*/?>", RegexOption.IGNORE_CASE).replace(t, "\n")
        t = Regex("</?(div|p|tr|h\\d)\\b[^>]*>", RegexOption.IGNORE_CASE).replace(t, "\n")
        t = Regex("</li>", RegexOption.IGNORE_CASE).replace(t, "\n")
        t = Regex("<li[^>]*>", RegexOption.IGNORE_CASE).replace(t, "• ")
        t = Regex("<style.*?</style>|<script.*?</script>", setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL)).replace(t, "")
        t = Regex("<[^>]+>").replace(t, "")
        t = unescape(t).replace('\u00a0', ' ')
        t = Regex("[ \\t]+\\n").replace(t, "\n")
        t = Regex("\\n{3,}").replace(t, "\n\n")
        return t.trim()
    }

    private fun unescape(s: String): String =
        if ('&' in s) Html.fromHtml(s.replace("<", "&lt;").replace("\n", "&#10;"), Html.FROM_HTML_MODE_LEGACY).toString() else s

    private fun esc(s: String) = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;")

    fun textToHtml(s: String?): String {
        val text = s ?: ""
        val sb = StringBuilder()
        var pos = 0
        for (m in IMG.findAll(text)) {
            sb.append(esc(text.substring(pos, m.range.first)).replace("\n", "<br>"))
            sb.append("<img src=\"").append(esc(m.groupValues[1])).append("\">")
            pos = m.range.last + 1
        }
        sb.append(esc(text.substring(pos)).replace("\n", "<br>"))
        return sb.toString()
    }

    fun images(s: String?) = IMG.findAll(s ?: "").map { it.groupValues[1] }.toList()

    fun hasCloze(s: String) = CLOZE.containsMatchIn(s)

    fun clozeCards(text: String): List<Pair<String, String>> {
        val nums = CLOZE.findAll(text).map { it.groupValues[1].toInt() }.toSortedSet()
        return nums.map { n ->
            val f = CLOZE.replace(text) { m ->
                if (m.groupValues[1].toInt() == n) (if (m.groupValues[3].isNotEmpty()) "[${m.groupValues[3]}]" else "[…]") else m.groupValues[2]
            }
            val b = CLOZE.replace(text) { m -> if (m.groupValues[1].toInt() == n) "【${m.groupValues[2]}】" else m.groupValues[2] }
            f to b
        }
    }

    fun guid(): String {
        val chars = ('a'..'z') + ('A'..'Z') + ('0'..'9') + "!#$%&()*+,-./:;<=>?@[]^_`{|}~".toList()
        return (1..10).map { chars.random() }.joinToString("")
    }
}

class FlashDb private constructor(val ctx: Context) : SQLiteOpenHelper(ctx, "flashcards.db", null, 2) {
    companion object {
        @Volatile private var inst: FlashDb? = null
        fun get(ctx: Context) = inst ?: synchronized(this) { inst ?: FlashDb(ctx.applicationContext).also { inst = it } }
    }

    val mediaDir: File get() = File(ctx.filesDir, "flashcards_media").also { it.mkdirs() }
    fun mediaFile(name: String) = File(mediaDir, File(name).name)

    override fun onConfigure(db: SQLiteDatabase) {
        db.enableWriteAheadLogging()
        db.setForeignKeyConstraintsEnabled(true)
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE decks(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, new_per_day INTEGER DEFAULT 20, " +
                "reviews_per_day INTEGER DEFAULT 200, retention REAL DEFAULT 0.9, created REAL, mod REAL)")
        db.execSQL("CREATE TABLE notes(id INTEGER PRIMARY KEY, guid TEXT UNIQUE, deck_id INTEGER NOT NULL REFERENCES decks(id) " +
                "ON DELETE CASCADE, front TEXT NOT NULL, back TEXT NOT NULL, tags TEXT DEFAULT '', reverse INTEGER DEFAULT 0, " +
                "created REAL, mod REAL)")
        db.execSQL("CREATE TABLE cards(id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE, " +
                "ord INTEGER DEFAULT 0, deck_id INTEGER NOT NULL, state INTEGER DEFAULT 0, step INTEGER DEFAULT 0, due REAL DEFAULT 0, " +
                "ivl INTEGER DEFAULT 0, stability REAL DEFAULT 0, difficulty REAL DEFAULT 0, reps INTEGER DEFAULT 0, " +
                "lapses INTEGER DEFAULT 0, last_review REAL DEFAULT 0, suspended INTEGER DEFAULT 0, position INTEGER DEFAULT 0, mod REAL)")
        db.execSQL("CREATE INDEX cards_deck_due ON cards(deck_id, state, due)")
        db.execSQL("CREATE INDEX cards_note ON cards(note_id)")
        db.execSQL("CREATE TABLE revlog(id INTEGER PRIMARY KEY, card_id INTEGER, grade INTEGER, state INTEGER, ivl INTEGER, took REAL, time REAL)")
        db.execSQL("CREATE INDEX revlog_time ON revlog(time)")
        onUpgrade(db, 1, 2)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) {        // sync between devices
            db.execSQL("ALTER TABLE decks ADD COLUMN guid TEXT")
            db.execSQL("CREATE TABLE IF NOT EXISTS graves(kind TEXT, guid TEXT, time REAL, PRIMARY KEY(kind, guid))")
            db.execSQL("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            db.rawQuery("SELECT id FROM decks", null).use { c ->
                while (c.moveToNext()) db.execSQL("UPDATE decks SET guid=? WHERE id=?", arrayOf(FlashText.guid() + FlashText.guid(), c.getLong(0)))
            }
        }
    }

    private fun long1(sql: String, vararg a: Any?): Long =
        readableDatabase.rawQuery(sql, a.map { it?.toString() }.toTypedArray()).use { if (it.moveToFirst() && !it.isNull(0)) it.getLong(0) else 0L }

    private fun double1(sql: String, vararg a: Any?): Double? =
        readableDatabase.rawQuery(sql, a.map { it?.toString() }.toTypedArray()).use { if (it.moveToFirst() && !it.isNull(0)) it.getDouble(0) else null }

    // ---- decks ----

    fun decks(): List<Deck> = readableDatabase.rawQuery("SELECT id, name, new_per_day, reviews_per_day, retention FROM decks ORDER BY name COLLATE NOCASE", null).use { c ->
        generateSequence { if (c.moveToNext()) Deck(c.getLong(0), c.getString(1), c.getInt(2), c.getInt(3), c.getDouble(4)) else null }.toList()
    }

    fun deck(id: Long): Deck? = decks().firstOrNull { it.id == id }

    fun deckByName(name: String?, create: Boolean = true): Long {
        val n = (name ?: "").trim().ifEmpty { "Default" }
        val found = long1("SELECT id FROM decks WHERE name=? COLLATE NOCASE", n)
        if (found != 0L || !create) return found
        val v = ContentValues().apply { put("name", n); put("created", Fsrs.now()); put("mod", Fsrs.now()); put("guid", FlashText.guid() + FlashText.guid()) }
        return writableDatabase.insert("decks", null, v)
    }

    fun renameDeck(id: Long, name: String) {
        writableDatabase.execSQL("UPDATE decks SET name=?, mod=? WHERE id=?", arrayOf(name.trim(), Fsrs.now(), id))
    }

    fun setOptions(id: Long, newPerDay: Int, reviewsPerDay: Int, retention: Double) {
        writableDatabase.execSQL("UPDATE decks SET new_per_day=?, reviews_per_day=?, retention=?, mod=? WHERE id=?",
            arrayOf(newPerDay, reviewsPerDay, retention.coerceIn(0.7, 0.99), Fsrs.now(), id))
    }

    private fun grave(kind: String, guid: String?) {
        if (!guid.isNullOrEmpty()) writableDatabase.execSQL("INSERT OR REPLACE INTO graves VALUES(?,?,?)", arrayOf(kind, guid, Fsrs.now()))
    }

    private fun str1(sql: String, vararg a: Any?): String? =
        readableDatabase.rawQuery(sql, a.map { it?.toString() }.toTypedArray()).use { if (it.moveToFirst() && !it.isNull(0)) it.getString(0) else null }

    fun deleteDeck(id: Long) {
        val db = writableDatabase
        db.beginTransaction()
        try {
            grave("deck", str1("SELECT guid FROM decks WHERE id=?", id))
            db.rawQuery("SELECT guid FROM notes WHERE deck_id=?", arrayOf(id.toString())).use { c -> while (c.moveToNext()) grave("note", c.getString(0)) }
            db.execSQL("DELETE FROM cards WHERE deck_id=?", arrayOf(id))
            db.execSQL("DELETE FROM notes WHERE deck_id=?", arrayOf(id))
            db.execSQL("DELETE FROM decks WHERE id=?", arrayOf(id))
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
    }

    // ---- notes & cards ----

    fun addNote(deckId: Long, front: String, back: String, tags: String = "", reverse: Boolean = false, guid: String? = null): Long {
        val db = writableDatabase
        val now = Fsrs.now()
        val nid = db.insertOrThrow("notes", null, ContentValues().apply {
            put("guid", guid ?: FlashText.guid()); put("deck_id", deckId); put("front", front); put("back", back)
            put("tags", tags.trim()); put("reverse", if (reverse) 1 else 0); put("created", now); put("mod", now)
        })
        val pos = long1("SELECT COALESCE(MAX(position),0) FROM cards") + 1
        for (o in if (reverse) listOf(0, 1) else listOf(0)) {
            db.insert("cards", null, ContentValues().apply {
                put("note_id", nid); put("ord", o); put("deck_id", deckId); put("position", pos + o); put("mod", now)
            })
        }
        return nid
    }

    fun updateNote(id: Long, deckId: Long, front: String, back: String, tags: String, reverse: Boolean) {
        val db = writableDatabase
        val now = Fsrs.now()
        db.execSQL("UPDATE notes SET deck_id=?, front=?, back=?, tags=?, reverse=?, mod=? WHERE id=?",
            arrayOf(deckId, front, back, tags.trim(), if (reverse) 1 else 0, now, id))
        db.execSQL("UPDATE cards SET deck_id=? WHERE note_id=?", arrayOf(deckId, id))   // study progress unchanged
        val hasRev = long1("SELECT COUNT(*) FROM cards WHERE note_id=? AND ord=1", id) > 0
        if (reverse && !hasRev) db.insert("cards", null, ContentValues().apply {
            put("note_id", id); put("ord", 1); put("deck_id", deckId); put("position", now.toLong()); put("mod", now)
        })
        else if (!reverse && hasRev) db.execSQL("DELETE FROM cards WHERE note_id=? AND ord=1", arrayOf(id))
    }

    fun note(id: Long): Note? = readableDatabase.rawQuery("SELECT id, deck_id, front, back, tags, reverse FROM notes WHERE id=?", arrayOf(id.toString())).use { c ->
        if (c.moveToFirst()) Note(c.getLong(0), c.getLong(1), c.getString(2), c.getString(3), c.getString(4) ?: "", c.getInt(5) == 1) else null
    }

    fun deleteNote(id: Long) {
        grave("note", str1("SELECT guid FROM notes WHERE id=?", id))
        writableDatabase.execSQL("DELETE FROM cards WHERE note_id=?", arrayOf(id))
        writableDatabase.execSQL("DELETE FROM notes WHERE id=?", arrayOf(id))
    }

    fun moveNote(id: Long, deckId: Long) {
        writableDatabase.execSQL("UPDATE notes SET deck_id=?, mod=? WHERE id=?", arrayOf(deckId, Fsrs.now(), id))
        writableDatabase.execSQL("UPDATE cards SET deck_id=? WHERE note_id=?", arrayOf(deckId, id))
    }

    fun resetNote(id: Long) {
        writableDatabase.execSQL("UPDATE cards SET state=0, step=0, due=0, ivl=0, stability=0, difficulty=0, reps=0, lapses=0, " +
                "last_review=0, mod=? WHERE note_id=?", arrayOf(Fsrs.now(), id))
    }

    fun toggleSuspendNote(id: Long) {
        writableDatabase.execSQL("UPDATE cards SET suspended=1-suspended, mod=? WHERE note_id=?", arrayOf(Fsrs.now(), id))
    }

    fun suspendCard(id: Long) {
        writableDatabase.execSQL("UPDATE cards SET suspended=1, mod=? WHERE id=?", arrayOf(Fsrs.now(), id))
    }

    fun card(id: Long): Card? = readableDatabase.rawQuery(
        "SELECT c.id, c.note_id, c.ord, c.deck_id, c.state, c.step, c.due, c.ivl, c.stability, c.difficulty, c.reps, c.lapses, " +
                "c.last_review, c.suspended, n.front, n.back FROM cards c JOIN notes n ON n.id=c.note_id WHERE c.id=?", arrayOf(id.toString())
    ).use { c ->
        if (!c.moveToFirst()) null else Card(c.getLong(0), c.getLong(1), c.getInt(2), c.getLong(3), c.getInt(4), c.getInt(5),
            c.getDouble(6), c.getInt(7), c.getDouble(8), c.getDouble(9), c.getInt(10), c.getInt(11), c.getDouble(12),
            c.getInt(13) == 1, c.getString(14), c.getString(15))
    }

    fun total(deckId: Long) = long1("SELECT COUNT(*) FROM cards WHERE deck_id=?", deckId).toInt()

    fun search(query: String, deckId: Long? = null, limit: Int = 2000): List<NoteRow> {
        val sb = StringBuilder("SELECT n.id, n.front, n.back, n.tags, d.name, MIN(c.due), MAX(c.ivl), MIN(c.state), MAX(c.suspended) " +
                "FROM notes n JOIN decks d ON d.id=n.deck_id LEFT JOIN cards c ON c.note_id=n.id WHERE 1=1")
        val args = mutableListOf<String>()
        if (deckId != null) { sb.append(" AND n.deck_id=?"); args.add(deckId.toString()) }
        for (w in query.split(Regex("\\s+")).filter { it.isNotBlank() }) {
            if (w.lowercase().startsWith("tag:")) { sb.append(" AND (' ' || n.tags || ' ') LIKE ?"); args.add("% ${w.substring(4)} %") }
            else { sb.append(" AND (n.front LIKE ? OR n.back LIKE ? OR n.tags LIKE ?)"); repeat(3) { args.add("%$w%") } }
        }
        sb.append(" GROUP BY n.id ORDER BY n.id DESC LIMIT $limit")
        return readableDatabase.rawQuery(sb.toString(), args.toTypedArray()).use { c ->
            generateSequence {
                if (c.moveToNext()) NoteRow(c.getLong(0), c.getString(1), c.getString(2), c.getString(3) ?: "", c.getString(4),
                    if (c.isNull(5)) 0.0 else c.getDouble(5), if (c.isNull(6)) 0 else c.getInt(6), if (c.isNull(7)) 0 else c.getInt(7),
                    !c.isNull(8) && c.getInt(8) == 1) else null
            }.toList()
        }
    }

    // ---- studying ----

    /** (new, learning, due) to study now, after the daily limits. */
    fun counts(deckId: Long, now: Double = Fsrs.now()): Triple<Int, Int, Int> {
        val start = Fsrs.dayStart(now)
        val d = deck(deckId) ?: return Triple(0, 0, 0)
        val newDone = long1("SELECT COUNT(DISTINCT r.card_id) FROM revlog r JOIN cards c ON c.id=r.card_id WHERE c.deck_id=? AND r.time>=? AND r.state=0", deckId, start)
        val revDone = long1("SELECT COUNT(*) FROM revlog r JOIN cards c ON c.id=r.card_id WHERE c.deck_id=? AND r.time>=? AND r.state=2", deckId, start)
        val new = long1("SELECT COUNT(*) FROM cards WHERE deck_id=? AND state=0 AND suspended=0", deckId)
        val lrn = long1("SELECT COUNT(*) FROM cards WHERE deck_id=? AND state IN (1,3) AND suspended=0 AND due<=?", deckId, now + Fsrs.LEARN_AHEAD)
        val rev = long1("SELECT COUNT(*) FROM cards WHERE deck_id=? AND state=2 AND suspended=0 AND due<?", deckId, start + 86400)
        return Triple(max(0, min(new, d.newPerDay - newDone).toInt()), lrn.toInt(), max(0, min(rev, d.reviewsPerDay - revDone).toInt()))
    }

    fun nextCard(deckId: Long, now: Double = Fsrs.now()): Long? {
        val (newLeft, _, revLeft) = counts(deckId, now)
        val start = Fsrs.dayStart(now)
        val lrn = long1("SELECT id FROM cards WHERE deck_id=? AND state IN (1,3) AND suspended=0 AND due<=? ORDER BY due LIMIT 1", deckId, now)
        if (lrn != 0L) return lrn
        val rev = if (revLeft > 0) long1("SELECT id FROM cards WHERE deck_id=? AND state=2 AND suspended=0 AND due<? ORDER BY due LIMIT 1", deckId, start + 86400) else 0L
        val new = if (newLeft > 0) long1("SELECT id FROM cards WHERE deck_id=? AND state=0 AND suspended=0 ORDER BY position, id LIMIT 1", deckId) else 0L
        if (rev != 0L && new != 0L) return if (Math.random() < newLeft.toDouble() / (newLeft + revLeft)) new else rev
        if (rev != 0L) return rev
        if (new != 0L) return new
        val ahead = long1("SELECT id FROM cards WHERE deck_id=? AND state IN (1,3) AND suspended=0 AND due<=? ORDER BY due LIMIT 1", deckId, now + Fsrs.LEARN_AHEAD)
        return if (ahead != 0L) ahead else null
    }

    fun nextLearningDue(deckId: Long): Double? =
        double1("SELECT MIN(due) FROM cards WHERE deck_id=? AND state IN (1,3) AND suspended=0", deckId)

    fun preview(cardId: Long, now: Double = Fsrs.now()): Map<Int, Double> {
        val c = card(cardId) ?: return emptyMap()
        val ret = deck(c.deckId)?.retention ?: 0.9
        return (1..4).associateWith { g -> max(0.0, Fsrs.schedule(c, g, now, ret, fuzz = false).due - now) }
    }

    /** Records an answer; returns the card as it was before (for Undo). */
    fun answer(cardId: Long, g: Int, took: Double): Card? {
        val c = card(cardId) ?: return null
        val now = Fsrs.now()
        val n = Fsrs.schedule(c, g, now, deck(c.deckId)?.retention ?: 0.9)
        val db = writableDatabase
        db.execSQL("UPDATE cards SET state=?, step=?, due=?, ivl=?, stability=?, difficulty=?, reps=?, lapses=?, last_review=?, mod=? WHERE id=?",
            arrayOf(n.state, n.step, n.due, n.ivl, n.stability, n.difficulty, n.reps, n.lapses, n.lastReview, now, cardId))
        c.revlogId = db.insert("revlog", null, ContentValues().apply {
            put("card_id", cardId); put("grade", g); put("state", c.state); put("ivl", n.ivl); put("took", took); put("time", now)
        })
        return c
    }

    fun undo(b: Card) {
        writableDatabase.execSQL("UPDATE cards SET state=?, step=?, due=?, ivl=?, stability=?, difficulty=?, reps=?, lapses=?, last_review=?, mod=? WHERE id=?",
            arrayOf(b.state, b.step, b.due, b.ivl, b.stability, b.difficulty, b.reps, b.lapses, b.lastReview, Fsrs.now(), b.id))
        if (b.revlogId > 0) writableDatabase.execSQL("DELETE FROM revlog WHERE id=?", arrayOf(b.revlogId))
    }

    // ---- statistics ----

    fun todayCount() = long1("SELECT COUNT(*) FROM revlog WHERE time>=?", Fsrs.dayStart()).toInt()
    fun todayMinutes() = ((double1("SELECT SUM(took) FROM revlog WHERE time>=?", Fsrs.dayStart()) ?: 0.0) / 60).toInt()
    fun todayCorrect(): Int? {
        val all = long1("SELECT COUNT(*) FROM revlog WHERE time>=? AND state=2", Fsrs.dayStart())
        if (all == 0L) return null
        return (long1("SELECT COUNT(*) FROM revlog WHERE time>=? AND state=2 AND grade>1", Fsrs.dayStart()) * 100 / all).toInt()
    }

    fun perDay(days: Int): Map<Int, Int> {
        val start = Fsrs.dayStart()
        val out = HashMap<Int, Int>()
        readableDatabase.rawQuery("SELECT time FROM revlog WHERE time>=?", arrayOf((start - (days - 1) * 86400.0).toString())).use { c ->
            while (c.moveToNext()) {
                val i = ((start - Fsrs.dayStart(c.getDouble(0))) / 86400 + 0.5).toInt()
                out[i] = (out[i] ?: 0) + 1
            }
        }
        return out
    }

    fun streak(): Int {
        val per = perDay(400)
        var i = if ((per[0] ?: 0) > 0) 0 else 1
        var n = 0
        while ((per[i] ?: 0) > 0) { n++; i++ }
        return n
    }

    fun forecast(days: Int): IntArray {
        val start = Fsrs.dayStart()
        val out = IntArray(days)
        readableDatabase.rawQuery("SELECT due FROM cards WHERE state=2 AND suspended=0 AND due<?", arrayOf((start + days * 86400.0).toString())).use { c ->
            while (c.moveToNext()) out[((c.getDouble(0) - start) / 86400).toInt().coerceIn(0, days - 1)]++
        }
        return out
    }

    // ---- media ----

    fun storeMedia(name: String, data: ByteArray): String {
        var n = File(name).name.ifEmpty { "file" }
        var f = File(mediaDir, n)
        if (f.exists()) {
            if (f.readBytes().contentEquals(data)) return n
            val dot = n.lastIndexOf('.')
            val stem = if (dot > 0) n.substring(0, dot) else n
            val ext = if (dot > 0) n.substring(dot) else ""
            n = "$stem-${sha1(data).take(8)}$ext"
            f = File(mediaDir, n)
        }
        f.writeBytes(data)
        return n
    }

    private fun sha1(b: ByteArray) = MessageDigest.getInstance("SHA-1").digest(b).joinToString("") { "%02x".format(it) }

    // ---- sync between devices (same format as the laptop) ----

    fun signature(): String = listOf(
        "SELECT COUNT(*), COALESCE(MAX(mod),0) FROM decks", "SELECT COUNT(*), COALESCE(MAX(mod),0) FROM notes",
        "SELECT COUNT(*), COALESCE(MAX(mod),0) FROM cards", "SELECT COUNT(*), COALESCE(MAX(time),0) FROM revlog",
        "SELECT COUNT(*), COALESCE(MAX(time),0) FROM graves"
    ).joinToString("|") { q -> readableDatabase.rawQuery(q, null).use { it.moveToFirst(); "${it.getLong(0)}:${String.format(java.util.Locale.US, "%.3f", it.getDouble(1))}" } }

    fun getMeta(k: String): String? = str1("SELECT v FROM meta WHERE k=?", k)
    fun setMeta(k: String, v: String) = writableDatabase.execSQL("INSERT OR REPLACE INTO meta VALUES(?,?)", arrayOf(k, v))

    private fun rows(sql: String): JSONArray {
        val out = JSONArray()
        readableDatabase.rawQuery(sql, null).use { c ->
            while (c.moveToNext()) {
                val o = JSONObject()
                for (i in 0 until c.columnCount) {
                    when (c.getType(i)) {
                        Cursor.FIELD_TYPE_INTEGER -> o.put(c.getColumnName(i), c.getLong(i))
                        Cursor.FIELD_TYPE_FLOAT -> o.put(c.getColumnName(i), c.getDouble(i))
                        Cursor.FIELD_TYPE_NULL -> o.put(c.getColumnName(i), JSONObject.NULL)
                        else -> o.put(c.getColumnName(i), c.getString(i))
                    }
                }
                out.put(o)
            }
        }
        return out
    }

    fun exportSync(device: String): JSONObject {
        val notes = rows("SELECT n.guid, d.guid AS deck, n.front, n.back, n.tags, n.reverse, n.created, n.mod FROM notes n JOIN decks d ON d.id=n.deck_id")
        val media = sortedSetOf<String>()
        for (i in 0 until notes.length()) { val n = notes.getJSONObject(i); media.addAll(FlashText.images(n.optString("front"))); media.addAll(FlashText.images(n.optString("back"))) }
        return JSONObject().put("v", 1).put("device", device).put("time", Fsrs.now())
            .put("decks", rows("SELECT guid, name, new_per_day, reviews_per_day, retention, mod FROM decks"))
            .put("notes", notes)
            .put("cards", rows("SELECT n.guid AS note, c.ord, c.state, c.step, c.due, c.ivl, c.stability, c.difficulty, c.reps, c.lapses, c.last_review, c.suspended, c.position, c.mod FROM cards c JOIN notes n ON n.id=c.note_id"))
            .put("revlog", rows("SELECT n.guid AS note, c.ord, r.grade, r.state, r.ivl, r.took, r.time FROM revlog r JOIN cards c ON c.id=r.card_id JOIN notes n ON n.id=c.note_id"))
            .put("graves", rows("SELECT kind, guid, time FROM graves"))
            .put("media", JSONArray(media.toList()))
    }

    /** Merges another device's collection in (newest change wins). Returns a short summary. */
    fun mergeSync(data: JSONObject): String {
        if (data.optInt("v", 0) > 1) return "skipped (made by a newer app version — update this app)"
        var added = 0; var updated = 0; var removed = 0
        val db = writableDatabase
        fun arr(k: String) = data.optJSONArray(k) ?: JSONArray()
        fun JSONObject.d(k: String) = optDouble(k, 0.0).let { if (it.isNaN()) 0.0 else it }
        db.beginTransaction()
        try {
            val gr = arr("graves")
            for (i in 0 until gr.length()) {
                val g = gr.getJSONObject(i); val kind = g.getString("kind"); val guid = g.getString("guid"); val t = g.d("time")
                val old = double1("SELECT time FROM graves WHERE kind=? AND guid=?", kind, guid)
                if (old == null || old < t) db.execSQL("INSERT OR REPLACE INTO graves VALUES(?,?,?)", arrayOf(kind, guid, t))
                if (kind == "note") {
                    db.rawQuery("SELECT id, mod FROM notes WHERE guid=?", arrayOf(guid)).use { c ->
                        if (c.moveToFirst() && c.getDouble(1) <= t) {
                            db.execSQL("DELETE FROM cards WHERE note_id=?", arrayOf(c.getLong(0))); db.execSQL("DELETE FROM notes WHERE id=?", arrayOf(c.getLong(0))); removed++
                        }
                    }
                } else if (kind == "deck") {
                    db.rawQuery("SELECT id, mod FROM decks WHERE guid=?", arrayOf(guid)).use { c ->
                        if (c.moveToFirst() && c.getDouble(1) <= t && long1("SELECT COUNT(*) FROM notes WHERE deck_id=? AND mod>?", c.getLong(0), t) == 0L) {
                            val id = c.getLong(0)
                            db.execSQL("DELETE FROM cards WHERE deck_id=?", arrayOf(id)); db.execSQL("DELETE FROM notes WHERE deck_id=?", arrayOf(id)); db.execSQL("DELETE FROM decks WHERE id=?", arrayOf(id))
                        }
                    }
                }
            }
            val graves = HashSet<String>()
            db.rawQuery("SELECT kind, guid FROM graves", null).use { c -> while (c.moveToNext()) graves.add(c.getString(0) + "|" + c.getString(1)) }
            val notesArr = arr("notes")
            val deckWithNotes = HashSet<String>()
            for (i in 0 until notesArr.length()) deckWithNotes.add(notesArr.getJSONObject(i).optString("deck"))
            val deckId = HashMap<String, Long>()
            val ds = arr("decks")
            for (i in 0 until ds.length()) {
                val d = ds.getJSONObject(i); val guid = d.getString("guid"); val name = d.getString("name")
                if ("deck|$guid" in graves && guid !in deckWithNotes) continue
                var row = db.rawQuery("SELECT id, guid, mod, name FROM decks WHERE guid=?", arrayOf(guid)).use { c -> if (c.moveToFirst()) arrayOf<Any?>(c.getLong(0), c.getString(1), c.getDouble(2), c.getString(3)) else null }
                if (row == null) row = db.rawQuery("SELECT id, guid, mod, name FROM decks WHERE name=? COLLATE NOCASE", arrayOf(name)).use { c -> if (c.moveToFirst()) arrayOf<Any?>(c.getLong(0), c.getString(1), c.getDouble(2), c.getString(3)) else null }
                val id: Long
                if (row != null) {
                    id = row[0] as Long
                    val lg = row[1] as String? ?: "~"
                    if (lg != guid && guid < lg) db.execSQL("UPDATE decks SET guid=? WHERE id=?", arrayOf(guid, id))
                    if (d.d("mod") > (row[2] as Double)) {
                        val clash = long1("SELECT COUNT(*) FROM decks WHERE name=? COLLATE NOCASE AND id!=?", name, id) > 0
                        db.execSQL("UPDATE decks SET name=?, new_per_day=?, reviews_per_day=?, retention=?, mod=? WHERE id=?",
                            arrayOf(if (clash) row[3] else name, d.optInt("new_per_day", 20), d.optInt("reviews_per_day", 200), d.optDouble("retention", 0.9), d.d("mod"), id))
                    }
                } else {
                    id = db.insert("decks", null, ContentValues().apply {
                        put("guid", guid); put("name", name); put("new_per_day", d.optInt("new_per_day", 20)); put("reviews_per_day", d.optInt("reviews_per_day", 200))
                        put("retention", d.optDouble("retention", 0.9)); put("created", Fsrs.now()); put("mod", d.d("mod"))
                    })
                }
                deckId[guid] = id
            }
            val noteId = HashMap<String, Long>()
            for (i in 0 until notesArr.length()) {
                val n = notesArr.getJSONObject(i); val guid = n.getString("guid")
                if ("note|$guid" in graves) continue
                val did = deckId[n.optString("deck")] ?: continue
                val tags = if (n.isNull("tags")) "" else n.optString("tags")
                val existing = db.rawQuery("SELECT id, mod FROM notes WHERE guid=?", arrayOf(guid)).use { c -> if (c.moveToFirst()) c.getLong(0) to c.getDouble(1) else null }
                val nid: Long
                if (existing == null) {
                    nid = db.insert("notes", null, ContentValues().apply {
                        put("guid", guid); put("deck_id", did); put("front", n.optString("front")); put("back", n.optString("back")); put("tags", tags)
                        put("reverse", n.optInt("reverse")); put("created", n.d("created")); put("mod", n.d("mod"))
                    }); added++
                } else {
                    nid = existing.first
                    if (n.d("mod") > existing.second) {
                        db.execSQL("UPDATE notes SET deck_id=?, front=?, back=?, tags=?, reverse=?, mod=? WHERE id=?", arrayOf(did, n.optString("front"), n.optString("back"), tags, n.optInt("reverse"), n.d("mod"), nid))
                        db.execSQL("UPDATE cards SET deck_id=? WHERE note_id=?", arrayOf(did, nid))
                        if (n.optInt("reverse") == 0) db.execSQL("DELETE FROM cards WHERE note_id=? AND ord=1", arrayOf(nid))
                        updated++
                    }
                }
                noteId[guid] = nid
            }
            val cs = arr("cards")
            for (i in 0 until cs.length()) {
                val k = cs.getJSONObject(i)
                val nid = noteId[k.optString("note")] ?: continue
                val ord = k.optInt("ord")
                val existing = db.rawQuery("SELECT id, mod FROM cards WHERE note_id=? AND ord=?", arrayOf(nid.toString(), ord.toString())).use { c -> if (c.moveToFirst()) c.getLong(0) to c.getDouble(1) else null }
                val vals = arrayOf<Any?>(k.optInt("state"), k.optInt("step"), k.d("due"), k.optInt("ivl"), k.d("stability"), k.d("difficulty"), k.optInt("reps"), k.optInt("lapses"), k.d("last_review"), k.optInt("suspended"), k.d("mod"))
                if (existing == null) {
                    val deck = long1("SELECT deck_id FROM notes WHERE id=?", nid)
                    db.execSQL("INSERT INTO cards(note_id, ord, deck_id, state, step, due, ivl, stability, difficulty, reps, lapses, last_review, suspended, mod, position) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (listOf<Any?>(nid, ord, deck) + vals.toList() + listOf<Any?>(k.optLong("position"))).toTypedArray())
                } else if (k.d("mod") > existing.second) {
                    db.execSQL("UPDATE cards SET state=?, step=?, due=?, ivl=?, stability=?, difficulty=?, reps=?, lapses=?, last_review=?, suspended=?, mod=? WHERE id=?", (vals.toList() + listOf<Any?>(existing.first)).toTypedArray())
                }
            }
            val have = HashSet<String>()
            db.rawQuery("SELECT n.guid, c.ord, r.time FROM revlog r JOIN cards c ON c.id=r.card_id JOIN notes n ON n.id=c.note_id", null).use { c ->
                while (c.moveToNext()) have.add(c.getString(0) + "|" + c.getInt(1) + "|" + String.format(java.util.Locale.US, "%.3f", c.getDouble(2)))
            }
            val rl = arr("revlog")
            for (i in 0 until rl.length()) {
                val r = rl.getJSONObject(i)
                val ng = r.optString("note"); val ord = r.optInt("ord")
                val key = ng + "|" + ord + "|" + String.format(java.util.Locale.US, "%.3f", r.d("time"))
                val nid = noteId[ng] ?: continue
                if (key in have) continue
                val cid = long1("SELECT id FROM cards WHERE note_id=? AND ord=?", nid, ord)
                if (cid != 0L) {
                    db.execSQL("INSERT INTO revlog(card_id, grade, state, ivl, took, time) VALUES(?,?,?,?,?,?)", arrayOf(cid, r.optInt("grade"), r.optInt("state"), r.optInt("ivl"), r.d("took"), r.d("time")))
                    have.add(key)
                }
            }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
        return "$added new, $updated changed, $removed deleted"
    }

    // ---- import ----

    /** Imports an Anki package or a text file. Returns (cards added, deck names). */
    fun importStream(input: InputStream, fileName: String, progress: (String) -> Unit = {}): Pair<Int, List<String>> {
        val lower = fileName.lowercase()
        val tmp = File.createTempFile("import", ".bin", ctx.cacheDir)
        try {
            input.use { inp -> tmp.outputStream().use { inp.copyTo(it) } }
            val isZip = tmp.inputStream().use { val h = ByteArray(2); it.read(h) == 2 && h[0] == 'P'.code.toByte() && h[1] == 'K'.code.toByte() }
            return if (isZip || lower.endsWith(".apkg") || lower.endsWith(".colpkg")) importApkg(tmp, progress)
            else importText(tmp.readText().removePrefix("\uFEFF"), fileName.substringBeforeLast('.'))
        } finally { tmp.delete() }
    }

    fun importText(text: String, deckName: String): Pair<Int, List<String>> {
        var sep: Char? = null; var deck = deckName; var tagCol: Int? = null; var deckCol: Int? = null; var isHtml: Boolean? = null
        val body = mutableListOf<String>()
        for (line in text.lines()) {
            if (line.startsWith("#") && ':' in line) {
                val k = line.substring(1).substringBefore(':').trim().lowercase(); val v = line.substringAfter(':').trim()
                when (k) {
                    "separator" -> sep = mapOf("tab" to '\t', "comma" to ',', "semicolon" to ';', "space" to ' ', "pipe" to '|', "colon" to ':')[v.lowercase()] ?: v.firstOrNull()
                    "deck" -> deck = v
                    "tags column" -> tagCol = v.toIntOrNull()?.minus(1)
                    "deck column" -> deckCol = v.toIntOrNull()?.minus(1)
                    "html" -> isHtml = v.lowercase() == "true"
                }
                continue
            }
            if (line.isNotBlank()) body.add(line)
        }
        if (body.isEmpty()) return 0 to listOf(deck)
        val sample = body.take(20).joinToString("\n")
        val s = sep ?: if ('\t' in sample) '\t' else if (sample.count { it == ';' } > sample.count { it == ',' }) ';' else ','
        val rows = Csv.parse(body.joinToString("\n"), s)
        val html = isHtml ?: rows.take(50).any { r -> r.any { Regex("<(br|div|b|i|img|span)\\b", RegexOption.IGNORE_CASE).containsMatchIn(it) } }
        var n = 0
        val ids = HashMap<String, Long>()
        val db = writableDatabase
        db.beginTransaction()
        try {
            for (r in rows) {
                if (r.all { it.isBlank() }) continue
                val fields = r.filterIndexed { i, _ -> i != tagCol && i != deckCol }
                var front = fields.getOrElse(0) { "" }; var back = fields.getOrElse(1) { "" }
                val tags = if (tagCol != null) r.getOrElse(tagCol!!) { "" } else fields.getOrElse(2) { "" }
                val dname = if (deckCol != null) r.getOrElse(deckCol!!) { deck } else deck
                if (html) { front = FlashText.htmlToText(front); back = FlashText.htmlToText(back) }
                val pairs = if (FlashText.hasCloze(front)) FlashText.clozeCards(front).map { (f, b) -> f to (if (back.isNotBlank()) "$b\n\n$back" else b) } else listOf(front to back)
                val did = ids.getOrPut(dname) { deckByName(dname) }
                for ((f, b) in pairs) { addNote(did, f.trim(), b.trim(), tags); n++ }
            }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
        return n to ids.keys.toList()
    }

    private fun importApkg(file: File, progress: (String) -> Unit): Pair<Int, List<String>> {
        val entries = HashMap<String, File>()
        val dir = File(ctx.cacheDir, "apkg-" + System.nanoTime()).also { it.mkdirs() }
        try {
            ZipInputStream(file.inputStream().buffered()).use { z ->
                var e: ZipEntry? = z.nextEntry
                while (e != null) {
                    if (!e.isDirectory) {
                        val safe = File(dir, "e" + entries.size)
                        safe.outputStream().use { z.copyTo(it) }
                        entries[e.name] = safe
                    }
                    e = z.nextEntry
                }
            }
            var zstd = false
            val colFile = when {
                entries.containsKey("collection.anki21b") -> { zstd = true; entries["collection.anki21b"]!! }
                entries.containsKey("collection.anki21") -> entries["collection.anki21"]!!
                entries.containsKey("collection.anki2") -> entries["collection.anki2"]!!
                else -> throw IllegalArgumentException("This isn't an Anki deck file (no collection inside).")
            }
            val dbFile = File(dir, "col.db")
            if (zstd) dbFile.writeBytes(Zstd.decompress(colFile.readBytes())) else colFile.copyTo(dbFile, true)
            val media = HashMap<String, String>()
            entries["media"]?.let { mf ->
                val raw = mf.readBytes()
                try {
                    val js = JSONObject(String(raw, Charsets.UTF_8))
                    for (k in js.keys()) media[k] = js.getString(k)
                } catch (_: Exception) {
                    try {
                        Proto.mediaNames(Zstd.decompress(raw)).forEachIndexed { i, n -> media[i.toString()] = n }
                        zstd = true
                    } catch (_: Exception) {}
                }
            }
            val renamed = HashMap<String, String>()
            var i = 0
            for ((k, name) in media) {
                val f = entries[k] ?: continue
                if (name.isEmpty()) continue
                var data = f.readBytes()
                if (zstd) data = try { Zstd.decompress(data) } catch (_: Exception) { data }
                val used = storeMedia(name, data)
                if (used != name) renamed[name] = used
                if (++i % 50 == 0) progress("Copying pictures… $i/${media.size}")
            }
            return importCollection(dbFile, renamed, progress)
        } finally { dir.deleteRecursively() }
    }

    private fun importCollection(f: File, renamed: Map<String, String>, progress: (String) -> Unit): Pair<Int, List<String>> {
        val src = SQLiteDatabase.openDatabase(f.path, null, SQLiteDatabase.OPEN_READONLY)
        try {
            val tables = src.rawQuery("SELECT name FROM sqlite_master WHERE type='table'", null).use { c -> generateSequence { if (c.moveToNext()) c.getString(0) else null }.toSet() }
            fun count(t: String) = src.rawQuery("SELECT COUNT(*) FROM $t", null).use { it.moveToFirst(); it.getLong(0) }
            val decks = HashMap<Long, String>()
            val models = HashMap<Long, Pair<Int, List<String>>>()  // type (1 = cloze), field names
            val tmplCount = HashMap<Long, Int>()
            if ("decks" in tables && count("decks") > 0) {
                src.rawQuery("SELECT id, name FROM decks", null).use { c -> while (c.moveToNext()) decks[c.getLong(0)] = c.getString(1).replace("\u001f", "::") }
            } else src.rawQuery("SELECT decks FROM col", null).use { c ->
                if (c.moveToFirst()) { val js = JSONObject(c.getString(0) ?: "{}"); for (k in js.keys()) decks[k.toLong()] = js.getJSONObject(k).optString("name", "Default") }
            }
            if ("notetypes" in tables && count("notetypes") > 0) {
                src.rawQuery("SELECT id, config FROM notetypes", null).use { c ->
                    while (c.moveToNext()) {
                        val id = c.getLong(0)
                        val kind = if (Proto.varintField(c.getBlob(1), 1) == 1L) 1 else 0
                        val flds = src.rawQuery("SELECT name FROM fields WHERE ntid=? ORDER BY ord", arrayOf(id.toString())).use { x -> generateSequence { if (x.moveToNext()) x.getString(0) else null }.toList() }
                        tmplCount[id] = src.rawQuery("SELECT COUNT(*) FROM templates WHERE ntid=?", arrayOf(id.toString())).use { x -> x.moveToFirst(); x.getInt(0) }
                        models[id] = kind to flds
                    }
                }
            } else src.rawQuery("SELECT models FROM col", null).use { c ->
                if (c.moveToFirst()) {
                    val js = JSONObject(c.getString(0) ?: "{}")
                    for (k in js.keys()) {
                        val m = js.getJSONObject(k)
                        val fa = m.optJSONArray("flds") ?: JSONArray()
                        models[k.toLong()] = m.optInt("type", 0) to (0 until fa.length()).map { fa.getJSONObject(it).optString("name") }
                        tmplCount[k.toLong()] = m.optJSONArray("tmpls")?.length() ?: 1
                    }
                }
            }
            var crt = src.rawQuery("SELECT crt FROM col", null).use { if (it.moveToFirst()) it.getLong(0) else 0L }
            if (crt > 100_000_000_000L) crt /= 1000
            class C(val ord: Int, val did: Long, val type: Int, val queue: Int, val due: Long, val ivl: Int, val factor: Int, val reps: Int, val lapses: Int)
            val cardsByNote = HashMap<Long, MutableList<C>>()
            src.rawQuery("SELECT nid, ord, did, type, queue, due, ivl, factor, reps, lapses FROM cards", null).use { c ->
                while (c.moveToNext()) cardsByNote.getOrPut(c.getLong(0)) { mutableListOf() }
                    .add(C(c.getInt(1), c.getLong(2), c.getInt(3), c.getInt(4), c.getLong(5), c.getInt(6), c.getInt(7), c.getInt(8), c.getInt(9)))
            }
            val existing = readableDatabase.rawQuery("SELECT guid FROM notes", null).use { c -> generateSequence { if (c.moveToNext()) c.getString(0) else null }.toHashSet() }
            val deckIds = HashMap<String, Long>()
            var added = 0
            val now = Fsrs.now()
            val today = Fsrs.dayStart(now)
            val db = writableDatabase
            db.beginTransaction()
            try {
                src.rawQuery("SELECT id, guid, mid, flds, tags FROM notes", null).use { c ->
                    val total = c.count
                    var i = 0
                    while (c.moveToNext()) {
                        if (i++ % 200 == 0) progress("Importing cards… $i/$total")
                        val nid = c.getLong(0); val guid = c.getString(1); val mid = c.getLong(2)
                        var fields = (c.getString(3) ?: "").split('\u001f')
                        if (renamed.isNotEmpty()) fields = fields.map { var x = it; for ((o, n) in renamed) x = x.replace(o, n); x }
                        val tags = (c.getString(4) ?: "").trim()
                        val (kind, fnames) = models[mid] ?: (0 to emptyList())
                        val cards = (cardsByNote[nid] ?: mutableListOf()).sortedBy { it.ord }
                        val dname = decks[cards.firstOrNull()?.did ?: 1L] ?: "Imported"
                        val deckId = deckIds.getOrPut(dname) { deckByName(dname) }
                        if (kind == 1) {
                            val text = FlashText.htmlToText(fields[0])
                            val extra = if (fields.size > 1) FlashText.htmlToText(fields[1]) else ""
                            FlashText.clozeCards(text).forEachIndexed { k, (f, b) ->
                                val g = "$guid#c${k + 1}"
                                if (g !in existing) { addNote(deckId, f, if (extra.isNotEmpty()) "$b\n\n$extra" else b, tags, guid = g); added++ }
                            }
                            continue
                        }
                        if (guid in existing) continue
                        val front = FlashText.htmlToText(fields.getOrElse(0) { "" })
                        val back = StringBuilder(FlashText.htmlToText(fields.getOrElse(1) { "" }))
                        for (j in 2 until fields.size) {
                            val t = FlashText.htmlToText(fields[j])
                            if (t.isNotEmpty()) { val label = fnames.getOrNull(j) ?: ""; back.append("\n\n").append(if (label.isNotEmpty()) "$label: " else "").append(t) }
                        }
                        val newId = addNote(deckId, front, back.toString().trim(), tags, reverse = cards.size > 1, guid = guid)
                        added++
                        val mine = readableDatabase.rawQuery("SELECT id, ord FROM cards WHERE note_id=?", arrayOf(newId.toString())).use { x -> generateSequence { if (x.moveToNext()) x.getLong(0) to x.getInt(1) else null }.toList() }
                        for (k in cards) {
                            val target = mine.firstOrNull { it.second == min(k.ord, 1) }?.first ?: continue
                            if (k.type == 0) { if (k.queue == -1) suspendCard(target); continue }
                            val ivl = if (k.type == 2) max(1, k.ivl) else 0
                            val state: Int; val due: Double
                            if (k.type == 2) {
                                val d0 = if (k.due < 100_000_000L) crt + k.due * 86400.0 else k.due.toDouble()
                                due = if (d0 > 0) Fsrs.dayStart(d0) else today; state = Fsrs.REVIEW
                            } else {
                                due = if (k.due > 100_000_000L) k.due.toDouble() else now; state = if (k.type == 3) Fsrs.RELEARNING else Fsrs.LEARNING
                            }
                            val diff = (11 - (if (k.factor > 0) k.factor else 2500) / 1000.0 * 2.4).coerceIn(1.0, 10.0)
                            db.execSQL("UPDATE cards SET state=?, due=?, ivl=?, stability=?, difficulty=?, reps=?, lapses=?, last_review=?, suspended=? WHERE id=?",
                                arrayOf(state, due, ivl, max(ivl.toDouble(), 0.5), diff, k.reps, k.lapses,
                                    if (state == Fsrs.REVIEW) due - ivl * 86400.0 else now, if (k.queue == -1) 1 else 0, target))
                        }
                    }
                }
                db.setTransactionSuccessful()
            } finally { db.endTransaction() }
            return added to deckIds.keys.sorted()
        } finally { src.close() }
    }

    // ---- export ----

    fun exportText(out: OutputStream, deckId: Long?): Int {
        val rows = search("", deckId, Int.MAX_VALUE).reversed()
        out.bufferedWriter(Charsets.UTF_8).use { w ->
            w.write("#separator:tab\n#html:true\n#tags column:3\n")
            for (r in rows) w.write(listOf(FlashText.textToHtml(r.front), FlashText.textToHtml(r.back), r.tags).joinToString("\t") { Csv.quote(it, '\t') } + "\n")
        }
        return rows.size
    }

    /** Writes an Anki package (opens in Anki, AnkiDroid and the laptop app). */
    fun exportApkg(out: OutputStream, deckId: Long?, withProgress: Boolean = true): Int {
        val decks = if (deckId != null) listOfNotNull(deck(deckId)) else decks()
        require(decks.isNotEmpty()) { "No deck to export." }
        val ids = decks.joinToString(",") { it.id.toString() }
        val now = (Fsrs.now()).toLong()
        val crt = Fsrs.dayStart().toLong()
        val base = System.currentTimeMillis()
        val mBasic = base; val mRev = base + 1
        val ankiDeck = decks.mapIndexed { i, d -> d.id to base + 10 + i }.toMap()
        fun model(id: Long, name: String, reverse: Boolean): JSONObject {
            val tmpls = JSONArray().put(JSONObject().put("name", "Card 1").put("ord", 0).put("qfmt", "{{Front}}")
                .put("afmt", "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}").put("did", JSONObject.NULL).put("bqfmt", "").put("bafmt", ""))
            if (reverse) tmpls.put(JSONObject().put("name", "Card 2").put("ord", 1).put("qfmt", "{{Back}}")
                .put("afmt", "{{FrontSide}}\n\n<hr id=answer>\n\n{{Front}}").put("did", JSONObject.NULL).put("bqfmt", "").put("bafmt", ""))
            val flds = JSONArray()
            listOf("Front", "Back").forEachIndexed { i, n ->
                flds.put(JSONObject().put("name", n).put("ord", i).put("sticky", false).put("rtl", false).put("font", "Arial").put("size", 20).put("media", JSONArray()))
            }
            val req = JSONArray().put(JSONArray().put(0).put("any").put(JSONArray().put(0)))
            if (reverse) req.put(JSONArray().put(1).put("any").put(JSONArray().put(1)))
            return JSONObject().put("id", id).put("name", name).put("type", 0).put("mod", now).put("usn", -1).put("sortf", 0)
                .put("did", ankiDeck[decks[0].id]).put("tmpls", tmpls).put("flds", flds)
                .put("css", ".card {\n font-family: arial;\n font-size: 20px;\n text-align: center;\n color: black;\n background-color: white;\n}\n")
                .put("latexPre", "\\documentclass[12pt]{article}\n\\special{papersize=3in,5in}\n\\usepackage[utf8]{inputenc}\n\\usepackage{amssymb,amsmath}\n\\pagestyle{empty}\n\\setlength{\\parindent}{0in}\n\\begin{document}\n")
                .put("latexPost", "\\end{document}").put("latexsvg", false).put("req", req).put("tags", JSONArray()).put("vers", JSONArray())
        }
        fun ankiDeckJson(id: Long, name: String) = JSONObject().put("id", id).put("name", name).put("mod", now).put("usn", -1)
            .put("lrnToday", JSONArray("[0,0]")).put("revToday", JSONArray("[0,0]")).put("newToday", JSONArray("[0,0]"))
            .put("timeToday", JSONArray("[0,0]")).put("collapsed", false).put("browserCollapsed", false).put("desc", "")
            .put("dyn", 0).put("conf", 1).put("extendNew", 0).put("extendRev", 0)
        val models = JSONObject().put(mBasic.toString(), model(mBasic, "Basic (Notion Sync)", false)).put(mRev.toString(), model(mRev, "Basic and reversed (Notion Sync)", true))
        val dj = JSONObject().put("1", ankiDeckJson(1, "Default"))
        for (d in decks) dj.put(ankiDeck[d.id].toString(), ankiDeckJson(ankiDeck[d.id]!!, d.name))
        val dconf = JSONObject().put("1", JSONObject("""{"id":1,"name":"Default","mod":$now,"usn":-1,"maxTaken":60,"autoplay":true,"timer":0,"replayq":true,"dyn":false,
            "new":{"bury":false,"delays":[1.0,10.0],"initialFactor":2500,"ints":[1,4,0],"order":1,"perDay":20},
            "lapse":{"delays":[10.0],"leechAction":1,"leechFails":8,"minInt":1,"mult":0.0},
            "rev":{"bury":false,"ease4":1.3,"ivlFct":1.0,"maxIvl":36500,"perDay":200,"hardFactor":1.2}}"""))
        val conf = JSONObject("""{"nextPos":1,"estTimes":true,"activeDecks":[1],"sortType":"noteFld","timeLim":0,"sortBackwards":false,
            "addToCur":true,"curDeck":1,"newBury":true,"newSpread":0,"dueCounts":true,"curModel":"$mBasic","collapseTime":1200}""")

        val tmp = File(ctx.cacheDir, "export-${System.nanoTime()}.anki2")
        val db = SQLiteDatabase.openOrCreateDatabase(tmp, null)
        val media = LinkedHashSet<String>()
        var nNotes = 0
        try {
            for (stmt in ANKI_SCHEMA.split(";").map { it.trim() }.filter { it.isNotEmpty() }) db.execSQL(stmt)
            db.execSQL("INSERT INTO col VALUES(1,?,?,?,11,0,0,0,?,?,?,?,?)", arrayOf(crt, now * 1000, now * 1000, conf.toString(), models.toString(), dj.toString(), dconf.toString(), "{}"))
            val noteMap = HashMap<Long, Long>()
            readableDatabase.rawQuery("SELECT id, guid, front, back, tags, reverse, mod FROM notes WHERE deck_id IN ($ids)", null).use { c ->
                var i = 0
                while (c.moveToNext()) {
                    val nid = base + 100 + i++
                    noteMap[c.getLong(0)] = nid
                    val front = FlashText.textToHtml(c.getString(2)); val back = FlashText.textToHtml(c.getString(3))
                    media.addAll(FlashText.images(c.getString(2))); media.addAll(FlashText.images(c.getString(3)))
                    val sfld = FlashText.htmlToText(front)
                    val csum = MessageDigest.getInstance("SHA-1").digest(sfld.toByteArray()).take(4).fold(0L) { a, b -> (a shl 8) or (b.toLong() and 0xff) }
                    val tags = (c.getString(4) ?: "").trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
                    db.execSQL("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?,?)", arrayOf(nid, c.getString(1) ?: FlashText.guid(),
                        if (c.getInt(5) == 1) mRev else mBasic, c.getDouble(6).toLong(), -1, if (tags.isNotEmpty()) " $tags " else "",
                        front + "\u001f" + back, sfld, csum, 0, ""))
                }
                nNotes = i
            }
            readableDatabase.rawQuery("SELECT note_id, ord, deck_id, state, due, ivl, stability, difficulty, reps, lapses, suspended, position, mod FROM cards WHERE deck_id IN ($ids)", null).use { c ->
                var i = 0
                while (c.moveToNext()) {
                    val cid = base + 100 + nNotes + i++
                    var type = 0; var queue = 0; var due = c.getLong(11).takeIf { it > 0 } ?: i.toLong(); var ivl = 0; var factor = 2500
                    val st = c.getInt(3)
                    if (withProgress && st != Fsrs.NEW) {
                        if (st == Fsrs.REVIEW) { type = 2; queue = 2; due = ((Fsrs.dayStart(c.getDouble(4)) - crt) / 86400).roundToLong(); ivl = max(1, if (c.getInt(5) > 0) c.getInt(5) else c.getDouble(6).roundToInt()) }
                        else { type = if (st == Fsrs.LEARNING) 1 else 3; queue = 1; due = c.getDouble(4).toLong() }
                        factor = (1300 + (10 - c.getDouble(7)) * 300).toInt().coerceIn(1300, 4000)
                    }
                    if (c.getInt(10) == 1) queue = -1
                    db.execSQL("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", arrayOf(cid, noteMap[c.getLong(0)], ankiDeck[c.getLong(2)],
                        c.getInt(1), c.getDouble(12).toLong(), -1, type, queue, due, ivl, factor, if (withProgress) c.getInt(8) else 0,
                        if (withProgress) c.getInt(9) else 0, 0, 0, 0, 0, ""))
                }
            }
        } finally { db.close() }
        try {
            ZipOutputStream(out.buffered()).use { z ->
                z.putNextEntry(ZipEntry("collection.anki2")); tmp.inputStream().use { it.copyTo(z) }; z.closeEntry()
                val index = JSONObject()
                var k = 0
                for (name in media) {
                    val f = mediaFile(name)
                    if (!f.isFile) continue
                    z.putNextEntry(ZipEntry(k.toString())); f.inputStream().use { it.copyTo(z) }; z.closeEntry()
                    index.put(k.toString(), name); k++
                }
                z.putNextEntry(ZipEntry("media")); z.write(index.toString().toByteArray()); z.closeEntry()
            }
        } finally { tmp.delete(); File(tmp.path + "-journal").delete() }
        return nNotes
    }

    private val ANKI_SCHEMA = """
        CREATE TABLE col (id integer primary key, crt integer not null, mod integer not null, scm integer not null, ver integer not null, dty integer not null, usn integer not null, ls integer not null, conf text not null, models text not null, decks text not null, dconf text not null, tags text not null);
        CREATE TABLE notes (id integer primary key, guid text not null, mid integer not null, mod integer not null, usn integer not null, tags text not null, flds text not null, sfld integer not null, csum integer not null, flags integer not null, data text not null);
        CREATE TABLE cards (id integer primary key, nid integer not null, did integer not null, ord integer not null, mod integer not null, usn integer not null, type integer not null, queue integer not null, due integer not null, ivl integer not null, factor integer not null, reps integer not null, lapses integer not null, left integer not null, odue integer not null, odid integer not null, flags integer not null, data text not null);
        CREATE TABLE revlog (id integer primary key, cid integer not null, usn integer not null, ease integer not null, ivl integer not null, lastIvl integer not null, factor integer not null, time integer not null, type integer not null);
        CREATE TABLE graves (usn integer not null, oid integer not null, type integer not null);
        CREATE INDEX ix_notes_usn on notes (usn);
        CREATE INDEX ix_cards_usn on cards (usn);
        CREATE INDEX ix_revlog_usn on revlog (usn);
        CREATE INDEX ix_cards_nid on cards (nid);
        CREATE INDEX ix_cards_sched on cards (did, queue, due);
        CREATE INDEX ix_revlog_cid on revlog (cid);
        CREATE INDEX ix_notes_csum on notes (csum)
    """
}

/** Small CSV reader/writer (quotes, doubled quotes, separators inside quotes). */
object Csv {
    fun parse(text: String, sep: Char): List<List<String>> {
        val rows = mutableListOf<List<String>>()
        var row = mutableListOf<String>()
        val cell = StringBuilder()
        var q = false
        var i = 0
        while (i < text.length) {
            val ch = text[i]
            if (q) {
                if (ch == '"') { if (i + 1 < text.length && text[i + 1] == '"') { cell.append('"'); i++ } else q = false }
                else cell.append(ch)
            } else when (ch) {
                '"' -> if (cell.isEmpty()) q = true else cell.append(ch)
                sep -> { row.add(cell.toString()); cell.clear() }
                '\n' -> { row.add(cell.toString()); cell.clear(); rows.add(row); row = mutableListOf() }
                '\r' -> {}
                else -> cell.append(ch)
            }
            i++
        }
        if (cell.isNotEmpty() || row.isNotEmpty()) { row.add(cell.toString()); rows.add(row) }
        return rows
    }

    fun quote(s: String, sep: Char) = if (s.any { it == sep || it == '"' || it == '\n' }) "\"" + s.replace("\"", "\"\"") + "\"" else s
}

/** Reading the newer Anki package format (zstd compressed, protobuf lists). */
object Zstd {
    fun decompress(b: ByteArray): ByteArray = try {
        com.github.luben.zstd.ZstdInputStream(b.inputStream()).use { it.readBytes() }
    } catch (e: Throwable) {
        throw IllegalArgumentException("This deck was saved by a newer Anki in a compressed format that couldn't be read. " +
                "In Anki, export it again and tick “Support older Anki versions”.")
    }
}

object Proto {
    private fun varint(b: ByteArray, start: Int): Pair<Long, Int> {
        var i = start; var shift = 0; var r = 0L
        while (true) { val x = b[i++].toInt() and 0xff; r = r or ((x and 0x7f).toLong() shl shift); if (x and 0x80 == 0) return r to i; shift += 7 }
    }

    private fun fields(b: ByteArray): List<Triple<Int, Int, Any>> {
        val out = mutableListOf<Triple<Int, Int, Any>>()
        var i = 0
        while (i < b.size) {
            val (key, j) = varint(b, i); i = j
            val fn = (key shr 3).toInt(); val wt = (key and 7).toInt()
            when (wt) {
                0 -> { val (v, k) = varint(b, i); i = k; out.add(Triple(fn, wt, v)) }
                2 -> { val (len, k) = varint(b, i); i = k; out.add(Triple(fn, wt, b.copyOfRange(i, i + len.toInt()))); i += len.toInt() }
                5 -> i += 4
                1 -> i += 8
                else -> return out
            }
        }
        return out
    }

    fun varintField(b: ByteArray?, field: Int): Long = try {
        fields(b ?: ByteArray(0)).firstOrNull { it.first == field && it.second == 0 }?.third as? Long ?: 0L
    } catch (_: Exception) { 0L }

    fun mediaNames(b: ByteArray): List<String> = fields(b).filter { it.first == 1 && it.second == 2 }.map { e ->
        fields(e.third as ByteArray).firstOrNull { it.first == 1 && it.second == 2 }?.let { String(it.third as ByteArray, Charsets.UTF_8) } ?: ""
    }
}
