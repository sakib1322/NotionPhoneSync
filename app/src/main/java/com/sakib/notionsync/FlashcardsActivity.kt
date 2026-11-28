package com.sakib.notionsync

import android.content.Intent
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.util.TypedValue
import android.view.Gravity
import android.view.HapticFeedbackConstants
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.AutoCompleteTextView
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.google.android.material.color.MaterialColors
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import java.util.Calendar
import java.util.concurrent.Executors

/** Flashcards: decks, studying with FSRS, adding/editing, browsing, Anki import and export. */
class FlashcardsActivity : AppCompatActivity() {
    private lateinit var db: FlashDb
    private lateinit var root: FrameLayout
    private val main = Handler(Looper.getMainLooper())
    private val bg = Executors.newSingleThreadExecutor()
    private var mode = "home"
    private var deckId = 0L
    private var cardId: Long? = null
    private var revealed = false
    private var shownAt = 0L
    private val undoStack = ArrayDeque<Card>()
    private var waitJob: Runnable? = null
    private var exportDeck: Long? = null
    private var exportFmt = "apkg"
    private var exportProgress = true
    private var pictureTarget: EditText? = null

    private val pickImport = registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri -> uri?.let { doImport(it) } }
    private val pickExport = registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { uri -> uri?.let { doExport(it) } }
    private val pickPicture = registerForActivityResult(ActivityResultContracts.GetContent()) { uri -> uri?.let { addPicture(it) } }

    private fun c(attr: Int) = MaterialColors.getColor(root, attr)
    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()
    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_LONG).show()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        db = FlashDb.get(this)
        root = FrameLayout(this)
        setContentView(root)
        root.setBackgroundColor(MaterialColors.getColor(this, android.R.attr.colorBackground, Color.WHITE))
        root.fitsSystemWindows = true
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() { if (mode == "study") showHome() else finish() }
        })
        showHome()
    }

    override fun onResume() {
        super.onResume()
        if (mode == "home") showHome()
        if (FlashSync.needed(db, 120)) syncNow(quiet = true)
    }

    override fun onPause() {
        super.onPause()
        if (db.signature() != db.getMeta("sync_sig")) syncNow(quiet = true)     // send what you just studied
    }

    private var syncStatus: TextView? = null

    private fun syncNow(quiet: Boolean = false) {
        if (Prefs(this).token.isBlank()) { if (!quiet) toast("Connect Notion first (Settings) to sync with your laptop."); return }
        if (FlashSync.busy) return
        syncStatus?.text = "Syncing with your other devices…"
        val app = applicationContext
        Thread {
            val res = try { FlashSync.run(app) } catch (e: Exception) { null }
            main.post {
                if (isFinishing || isDestroyed) return@post
                syncStatus?.text = FlashSync.status(db)
                if (res != null && res != "nothing new") { if (mode == "home") showHome() else if (mode == "study" && !revealed) updateCounts() }
                if (!quiet && res == null) toast("Couldn't sync: ${FlashSync.lastError}")
            }
        }.start()
    }

    override fun onDestroy() {
        super.onDestroy()
        waitJob?.let { main.removeCallbacks(it) }
        bg.shutdown()
    }

    // ---- small view helpers ----

    private fun text(s: String, size: Float = 15f, bold: Boolean = false, color: Int? = null) = TextView(this).apply {
        text = s; setTextSize(TypedValue.COMPLEX_UNIT_SP, size)
        if (bold) typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
        setTextColor(color ?: c(com.google.android.material.R.attr.colorOnSurface))
    }

    private fun button(s: String, filled: Boolean = false, onClick: () -> Unit) =
        MaterialButton(this, null, if (filled) com.google.android.material.R.attr.materialButtonStyle else com.google.android.material.R.attr.materialButtonOutlinedStyle).apply {
            text = s; isAllCaps = false; setOnClickListener { onClick() }
        }

    private fun card(): MaterialCardView = MaterialCardView(this).apply {
        radius = dp(22).toFloat(); cardElevation = 0f; strokeWidth = 0
        setCardBackgroundColor(c(com.google.android.material.R.attr.colorSurfaceContainerLow))
    }

    private fun vbox(pad: Int = 0) = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(pad, pad, pad, pad) }
    private fun hbox() = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
    private fun lp(w: Int = ViewGroup.LayoutParams.MATCH_PARENT, h: Int = ViewGroup.LayoutParams.WRAP_CONTENT, top: Int = 0, weight: Float = 0f) =
        LinearLayout.LayoutParams(w, h, weight).apply { topMargin = dp(top) }

    // ---- home ----

    private fun showHome() {
        waitJob?.let { main.removeCallbacks(it) }
        mode = "home"
        root.removeAllViews()
        val scroll = ScrollView(this)
        val col = vbox(dp(18))
        scroll.addView(col)
        root.addView(scroll)

        val top = hbox()
        top.addView(text("Flashcards", 26f, true), lp(0, weight = 1f))
        top.addView(button("Import") { importMenu() })
        top.addView(button("Export") { exportDialog(null) }, lp(ViewGroup.LayoutParams.WRAP_CONTENT).apply { leftMargin = dp(6) })
        col.addView(top)
        val srow = hbox()
        syncStatus = text(FlashSync.status(db), 12f, color = c(com.google.android.material.R.attr.colorOnSurfaceVariant))
        srow.addView(syncStatus, lp(0, weight = 1f))
        srow.addView(button("Sync now") { syncNow() })
        col.addView(srow, lp(top = 6))

        // today
        val stats = hbox()
        val today = db.todayCount(); val streak = db.streak(); val correct = db.todayCorrect(); val fc = db.forecast(7)
        for ((big, small) in listOf("$today" to "today", "$streak" to "day streak",
            (correct?.let { "$it%" } ?: "–") to "correct", "${fc.sum()}" to "this week")) {
            val box = vbox(dp(12))
            box.background = GradientDrawable().apply { cornerRadius = dp(16).toFloat(); setColor(c(com.google.android.material.R.attr.colorSurfaceContainerLow)) }
            box.addView(text(big, 20f, true))
            box.addView(text(small, 12f, color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)))
            stats.addView(box, lp(0, weight = 1f).apply { rightMargin = dp(6) })
        }
        col.addView(stats, lp(top = 16))
        col.addView(heatmap(), lp(h = dp(7 * 12 + 6), top = 12))

        val add = button("+  Add cards", filled = true) { editDialog(null, db.decks().firstOrNull()?.id) }
        col.addView(add, lp(top = 14))

        val decks = db.decks()
        if (decks.isEmpty()) {
            col.addView(text("No decks yet.\nTap “Add cards” to make your first card, or Import an Anki deck (.apkg) or a text file.",
                15f, color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)), lp(top = 24))
        }
        for (d in decks) {
            val (n, l, r) = db.counts(d.id)
            val cv = card()
            val row = hbox()
            row.setPadding(dp(18), dp(16), dp(10), dp(16))
            val left = vbox()
            left.addView(text(d.name.replace("::", " › "), 17f, true))
            val counts = hbox()
            for ((v, colr, cap) in listOf(Triple(n, 0xFF1A73E8.toInt(), "new"), Triple(l, 0xFFD93025.toInt(), "learning"), Triple(r, 0xFF188038.toInt(), "due"))) {
                counts.addView(text("$v", 14f, true, if (v > 0) colr else c(com.google.android.material.R.attr.colorOnSurfaceVariant)))
                counts.addView(text(" $cap   ", 13f, color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)))
            }
            counts.addView(text("· ${db.total(d.id)} cards", 13f, color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)))
            left.addView(counts, lp(top = 4))
            row.addView(left, lp(0, weight = 1f))
            val more = ImageView(this).apply {
                setImageResource(R.drawable.ic_more); setPadding(dp(10), dp(10), dp(10), dp(10))
                setColorFilter(c(com.google.android.material.R.attr.colorOnSurfaceVariant))
                setOnClickListener { deckMenu(d) }
            }
            row.addView(more)
            cv.addView(row)
            cv.setOnClickListener { startStudy(d.id) }
            cv.setOnLongClickListener { deckMenu(d); true }
            col.addView(cv, lp(top = 10))
        }
        col.addView(button("New deck") { newDeck() }, lp(top = 14))
        col.addView(button("Browse all cards") { browse(null) }, lp(top = 4))
        col.addView(text("Tap a deck to study. Tap the card (or “Show answer”), then Again · Hard · Good · Easy. " +
                "The app works out when you'll need to see each card again, like Anki.", 12f,
            color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)), lp(top = 14))
    }

    private fun heatmap(): View = object : View(this) {
        val per = db.perDay(7 * 26)
        val p = android.graphics.Paint(android.graphics.Paint.ANTI_ALIAS_FLAG)
        override fun onDraw(cv: android.graphics.Canvas) {
            val weeks = 26
            val gap = dp(2).toFloat()
            val sz = minOf((width - gap * weeks) / weeks, (height - gap * 7) / 7f)
            val top = per.values.maxOrNull() ?: 0
            val cal = Calendar.getInstance(); cal.timeInMillis = (Fsrs.dayStart() * 1000).toLong()
            val todayWd = cal.get(Calendar.DAY_OF_WEEK) - 1
            for (i in 0 until weeks * 7) {
                val colI = weeks - 1 - (i + (6 - todayWd)) / 7
                val row = ((todayWd - i) % 7 + 7) % 7
                if (colI < 0) continue
                val n = per[i] ?: 0
                val k = if (top > 0) n.toFloat() / top else 0f
                p.color = when {
                    n == 0 -> c(com.google.android.material.R.attr.colorSurfaceContainerHighest)
                    k < 0.25 -> 0xFF9BE9A8.toInt(); k < 0.5 -> 0xFF40C463.toInt(); k < 0.75 -> 0xFF30A14E.toInt(); else -> 0xFF216E39.toInt()
                }
                val x = colI * (sz + gap); val y = row * (sz + gap)
                cv.drawRoundRect(x, y, x + sz, y + sz, dp(2).toFloat(), dp(2).toFloat(), p)
            }
        }
    }

    private fun deckMenu(d: Deck) {
        val items = arrayOf("Study", "Add cards", "Browse", "Options", "Rename", "Export", "Delete")
        MaterialAlertDialogBuilder(this).setTitle(d.name).setItems(items) { _, i ->
            when (i) {
                0 -> startStudy(d.id); 1 -> editDialog(null, d.id); 2 -> browse(d.id); 3 -> optionsDialog(d)
                4 -> ask("Rename deck", d.name) { name -> if (name.isNotBlank()) { db.renameDeck(d.id, name); showHome() } }
                5 -> exportDialog(d.id)
                6 -> MaterialAlertDialogBuilder(this).setTitle("Delete “${d.name}”?")
                    .setMessage("Its ${db.total(d.id)} card(s) will be deleted. This can't be undone. (Tip: Export it first.)")
                    .setPositiveButton("Delete") { _, _ -> db.deleteDeck(d.id); showHome() }.setNegativeButton("Cancel", null).show()
            }
        }.show()
    }

    private fun ask(title: String, initial: String = "", hint: String = "", done: (String) -> Unit) {
        val til = TextInputLayout(this, null, com.google.android.material.R.attr.textInputOutlinedStyle)
        val et = TextInputEditText(til.context).apply { setText(initial); setSingleLine() }
        til.hint = hint.ifEmpty { title }
        til.addView(et)
        val box = FrameLayout(this).apply { setPadding(dp(22), dp(8), dp(22), 0); addView(til) }
        MaterialAlertDialogBuilder(this).setTitle(title).setView(box)
            .setPositiveButton("OK") { _, _ -> done(et.text.toString().trim()) }.setNegativeButton("Cancel", null).show()
        et.requestFocus()
    }

    private fun newDeck() = ask("New deck", hint = "Deck name (use :: for a sub-deck)") { name ->
        if (name.isNotBlank()) { db.deckByName(name); showHome() }
    }

    private fun optionsDialog(d: Deck) {
        val box = vbox(dp(22))
        fun field(label: String, v: Int): EditText {
            val til = TextInputLayout(this, null, com.google.android.material.R.attr.textInputOutlinedStyle); til.hint = label
            val et = TextInputEditText(til.context).apply { setText(v.toString()); inputType = InputType.TYPE_CLASS_NUMBER }
            til.addView(et); box.addView(til, lp(top = 6)); return et
        }
        val newEt = field("New cards per day", d.newPerDay)
        val revEt = field("Maximum reviews per day", d.reviewsPerDay)
        val retLbl = text("How much to remember: ${(d.retention * 100).toInt()}%", 14f)
        box.addView(retLbl, lp(top = 14))
        val sb = SeekBar(this).apply { max = 17; progress = (d.retention * 100).toInt() - 80 }
        sb.setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(s: SeekBar?, p: Int, u: Boolean) { retLbl.text = "How much to remember: ${80 + p}%" }
            override fun onStartTrackingTouch(s: SeekBar?) {}
            override fun onStopTrackingTouch(s: SeekBar?) {}
        })
        box.addView(sb)
        box.addView(text("90% is a good balance. Higher = you remember more but see cards more often.", 12f,
            color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)))
        MaterialAlertDialogBuilder(this).setTitle("Options — ${d.name}").setView(box)
            .setPositiveButton("Save") { _, _ ->
                db.setOptions(d.id, newEt.text.toString().toIntOrNull() ?: d.newPerDay, revEt.text.toString().toIntOrNull() ?: d.reviewsPerDay, (80 + sb.progress) / 100.0)
                showHome()
            }.setNegativeButton("Cancel", null).show()
    }

    // ---- study ----

    private lateinit var qView: LinearLayout
    private lateinit var bottom: LinearLayout
    private lateinit var countsView: TextView

    private fun startStudy(id: Long) {
        deckId = id
        mode = "study"
        undoStack.clear()
        root.removeAllViews()
        val col = vbox(dp(14))
        val top = hbox()
        top.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_back); setPadding(dp(8), dp(8), dp(8), dp(8))
            setColorFilter(c(com.google.android.material.R.attr.colorOnSurface)); setOnClickListener { showHome() }
        })
        val titles = vbox()
        titles.addView(text(db.deck(id)?.name?.replace("::", " › ") ?: "", 17f, true))
        countsView = text("", 13f)
        titles.addView(countsView)
        top.addView(titles, lp(0, weight = 1f).apply { leftMargin = dp(6) })
        for ((label, step) in listOf("A−" to -1, "A+" to 1)) top.addView(text(label, 16f, true).apply {
            setPadding(dp(10), dp(8), dp(10), dp(8)); setOnClickListener { textSize(step) }
        })
        top.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_more); setPadding(dp(10), dp(10), dp(10), dp(10))
            setColorFilter(c(com.google.android.material.R.attr.colorOnSurfaceVariant)); setOnClickListener { studyMenu() }
        })
        col.addView(top)
        val cv = card()
        val scroll = ScrollView(this)
        qView = vbox(dp(24)).apply { gravity = Gravity.CENTER_HORIZONTAL }
        scroll.addView(qView)
        scroll.isFillViewport = true
        cv.addView(scroll)
        cv.setOnClickListener { if (!revealed) reveal() }
        qView.setOnClickListener { if (!revealed) reveal() }
        col.addView(cv, lp(h = 0, top = 12, weight = 1f))
        bottom = hbox()
        col.addView(bottom, lp(top = 12))
        root.addView(col)
        nextCard()
    }

    private fun studyMenu() {
        val items = arrayOf("Undo last answer", "Edit this card", "Suspend this card", "Delete this card")
        MaterialAlertDialogBuilder(this).setItems(items) { _, i ->
            when (i) {
                0 -> undo()
                1 -> cardId?.let { id -> db.card(id)?.let { editDialog(it.noteId, null) { redrawCurrent() } } }
                2 -> cardId?.let { db.suspendCard(it); nextCard() }
                3 -> cardId?.let { id -> db.card(id)?.let { cd ->
                    MaterialAlertDialogBuilder(this).setTitle("Delete this card?").setPositiveButton("Delete") { _, _ ->
                        db.deleteNote(cd.noteId); undoStack.removeAll { it.noteId == cd.noteId }; nextCard()
                    }.setNegativeButton("Cancel", null).show()
                } }
            }
        }.show()
    }

    private fun updateCounts() {
        val (n, l, r) = db.counts(deckId)
        val sp = android.text.SpannableStringBuilder()
        fun add(v: Int, col: Int, cap: String) {
            val s = sp.length; sp.append("$v")
            sp.setSpan(android.text.style.ForegroundColorSpan(col), s, sp.length, 0)
            sp.setSpan(android.text.style.StyleSpan(Typeface.BOLD), s, sp.length, 0)
            sp.append(" $cap   ")
        }
        add(n, 0xFF1A73E8.toInt(), "new"); add(l, 0xFFD93025.toInt(), "learning"); add(r, 0xFF188038.toInt(), "due")
        countsView.text = sp
    }

    private fun nextCard() {
        waitJob?.let { main.removeCallbacks(it) }
        updateCounts()
        val id = db.nextCard(deckId)
        cardId = id
        revealed = false
        if (id == null) return finished()
        val cd = db.card(id) ?: return finished()
        render(cd.question, null)
        shownAt = System.currentTimeMillis()
        bottom.removeAllViews()
        bottom.addView(button("Show answer", filled = true) { reveal() }.apply { minHeight = dp(56) }, lp(0, weight = 1f))
    }

    private fun finished() {
        qView.removeAllViews()
        qView.addView(text("🎉", 48f).apply { gravity = Gravity.CENTER })
        qView.addView(text("You're done with this deck for now.", 18f, true).apply { gravity = Gravity.CENTER }, lp(top = 8))
        val nxt = db.nextLearningDue(deckId)
        val now = Fsrs.now()
        if (nxt != null && nxt - now < 6 * 3600) {
            val wait = maxOf(0.0, nxt - now)
            qView.addView(text("A card you're learning comes back in ${Fsrs.human(wait)}. Stay here and it shows up by itself.", 14f,
                color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)).apply { gravity = Gravity.CENTER }, lp(top = 12))
            waitJob = Runnable { if (mode == "study") nextCard() }.also { main.postDelayed(it, (minOf(wait, 3600.0) * 1000).toLong() + 500) }
        } else {
            val fc = db.forecast(2)
            qView.addView(text("Tomorrow: ${fc.getOrElse(1) { 0 }} card(s) to review.", 14f,
                color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)).apply { gravity = Gravity.CENTER }, lp(top = 12))
        }
        bottom.removeAllViews()
        bottom.addView(button("Back to decks", filled = true) { showHome() }, lp(0, weight = 1f))
    }

    private var lastQ = ""
    private var lastA: String? = null

    /** Short cards big and centred; long ones smaller and left-aligned (easier to read) + your A−/A+ choice. */
    private fun render(q0: String, a0: String?) {
        val q = FlashText.tidy(q0); val a = a0?.let { FlashText.tidy(it) }
        lastQ = q0; lastA = a0
        qView.removeAllViews()
        val n = FlashText.IMG.replace(q + (a ?: ""), "").length
        val adj = db.getMeta("text_adj")?.toIntOrNull() ?: 0
        val (qs, asz, center) = when { n < 120 -> Triple(22f, 19f, true); n < 350 -> Triple(18f, 16f, true); else -> Triple(15f, 14f, false) }
        rich(q, maxOf(10f, qs + adj), center, bold = n < 120)
        if (a != null) {
            qView.addView(View(this).apply { setBackgroundColor(c(com.google.android.material.R.attr.colorOutlineVariant)) },
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1)).apply { topMargin = dp(16); bottomMargin = dp(16) })
            rich(a, maxOf(10f, asz + adj), center)
        }
    }

    private fun textSize(step: Int) {
        val v = ((db.getMeta("text_adj")?.toIntOrNull() ?: 0) + step).coerceIn(-6, 10)
        db.setMeta("text_adj", v.toString())
        if (mode == "study" && cardId != null) render(lastQ, lastA)
    }

    private fun rich(s: String, size: Float, center: Boolean = true, bold: Boolean = false) {
        var pos = 0
        fun addText(t: String) {
            if (t.isBlank()) return
            qView.addView(text(t.trim('\n'), size, bold).apply {
                gravity = if (center) Gravity.CENTER else Gravity.START; setLineSpacing(0f, 1.2f); setTextIsSelectable(false)
            }, lp())
        }
        for (m in FlashText.IMG.findAll(s)) {
            addText(s.substring(pos, m.range.first))
            val f = db.mediaFile(m.groupValues[1])
            val bmp = if (f.isFile) try {
                val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }; BitmapFactory.decodeFile(f.path, o)
                var ss = 1; while (o.outWidth / ss > 1400) ss *= 2
                BitmapFactory.decodeFile(f.path, BitmapFactory.Options().apply { inSampleSize = ss })
            } catch (_: Throwable) { null } else null
            if (bmp != null) qView.addView(ImageView(this).apply { setImageBitmap(bmp); adjustViewBounds = true; maxHeight = dp(260) }, lp(top = 8))
            else qView.addView(text("[picture: ${m.groupValues[1]}]", 13f, color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)))
            pos = m.range.last + 1
        }
        addText(s.substring(pos))
    }

    private fun reveal() {
        val id = cardId ?: return
        if (revealed) return
        val cd = db.card(id) ?: return nextCard()
        revealed = true
        root.performHapticFeedback(HapticFeedbackConstants.CLOCK_TICK)
        render(cd.question, cd.answer)
        val pv = db.preview(id)
        bottom.removeAllViews()
        for ((g, name, fg, bgc) in listOf(Quad(1, "Again", 0xFFD93025.toInt(), 0xFFFCE8E6.toInt()), Quad(2, "Hard", 0xFFB06000.toInt(), 0xFFFEF7E0.toInt()),
            Quad(3, "Good", 0xFF188038.toInt(), 0xFFE6F4EA.toInt()), Quad(4, "Easy", 0xFF1A73E8.toInt(), 0xFFE8F0FE.toInt()))) {
            val b = MaterialButton(this).apply {
                text = "$name\n${Fsrs.human(pv[g] ?: 0.0)}"; isAllCaps = false; setTextColor(fg)
                backgroundTintList = android.content.res.ColorStateList.valueOf(bgc); minHeight = dp(60)
                insetTop = 0; insetBottom = 0; cornerRadius = dp(16)
                setOnClickListener { grade(g) }
            }
            bottom.addView(b, lp(0, weight = 1f).apply { leftMargin = dp(3); rightMargin = dp(3) })
        }
    }

    private data class Quad(val a: Int, val b: String, val c: Int, val d: Int)

    private fun grade(g: Int) {
        val id = cardId ?: return
        if (!revealed) return
        val took = minOf(60.0, (System.currentTimeMillis() - shownAt) / 1000.0)
        db.answer(id, g, took)?.let { undoStack.addLast(it); while (undoStack.size > 20) undoStack.removeFirst() }
        nextCard()
    }

    private fun undo() {
        val b = undoStack.removeLastOrNull() ?: return toast("Nothing to undo.")
        db.undo(b)
        waitJob?.let { main.removeCallbacks(it) }
        cardId = b.id
        revealed = false
        updateCounts()
        db.card(b.id)?.let { render(it.question, null) }
        shownAt = System.currentTimeMillis()
        bottom.removeAllViews()
        bottom.addView(button("Show answer", filled = true) { reveal() }.apply { minHeight = dp(56) }, lp(0, weight = 1f))
    }

    private fun redrawCurrent() {
        val id = cardId ?: return
        val cd = db.card(id) ?: return nextCard()
        render(cd.question, if (revealed) cd.answer else null)
    }

    // ---- add / edit ----

    private fun editDialog(noteId: Long?, deck: Long?, after: (() -> Unit)? = null) {
        val note = noteId?.let { db.note(it) }
        val box = vbox(dp(20))
        val names = db.decks().map { it.name }
        val deckTil = TextInputLayout(this, null, com.google.android.material.R.attr.textInputOutlinedExposedDropdownMenuStyle).apply { hint = "Deck" }
        val deckEt = AutoCompleteTextView(deckTil.context).apply {
            setText(note?.let { db.deck(it.deckId)?.name } ?: deck?.let { db.deck(it)?.name } ?: names.firstOrNull() ?: "My first deck")
            setAdapter(ArrayAdapter(this@FlashcardsActivity, android.R.layout.simple_list_item_1, names)); threshold = 0
            setPadding(dp(14), dp(16), dp(14), dp(16))
        }
        deckTil.addView(deckEt)
        box.addView(deckTil)
        fun area(label: String, v: String): EditText {
            val til = TextInputLayout(this, null, com.google.android.material.R.attr.textInputOutlinedStyle); til.hint = label
            val et = TextInputEditText(til.context).apply { setText(v); minLines = 3; gravity = Gravity.TOP
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES }
            et.setOnFocusChangeListener { _, f -> if (f) pictureTarget = et }
            til.addView(et); box.addView(til, lp(top = 10)); return et
        }
        val front = area("Front (the question)", note?.front ?: "")
        val back = area("Back (the answer)", note?.back ?: "")
        pictureTarget = front
        val tagsTil = TextInputLayout(this, null, com.google.android.material.R.attr.textInputOutlinedStyle).apply { hint = "Tags (optional)" }
        val tags = TextInputEditText(tagsTil.context).apply { setText(note?.tags ?: ""); setSingleLine() }
        tagsTil.addView(tags); box.addView(tagsTil, lp(top = 10))
        val rev = CheckBox(this).apply { text = "Also make a reverse card (asks the back, answer is the front)"; isChecked = note?.reverse ?: false }
        box.addView(rev, lp(top = 6))
        box.addView(button("Add picture") { pickPicture.launch("image/*") }, lp(ViewGroup.LayoutParams.WRAP_CONTENT, top = 4))
        val status = text("", 13f, color = 0xFF188038.toInt())
        box.addView(status)
        val scroll = ScrollView(this).apply { addView(box) }
        val dlg = MaterialAlertDialogBuilder(this).setTitle(if (note != null) "Edit card" else "Add cards").setView(scroll)
            .setPositiveButton(if (note != null) "Save" else "Add", null).setNegativeButton("Close", null).create()
        dlg.setOnShowListener {
            dlg.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val f = front.text.toString().trim(); val b = back.text.toString().trim()
                if (f.isEmpty()) { status.setTextColor(0xFFD93025.toInt()); status.text = "Type something on the front first."; return@setOnClickListener }
                val did = db.deckByName(deckEt.text.toString())
                if (note != null) {
                    db.updateNote(note.id, did, f, b, tags.text.toString(), rev.isChecked)
                    dlg.dismiss(); after?.invoke()
                } else {
                    db.addNote(did, f, b, tags.text.toString(), rev.isChecked)
                    front.setText(""); back.setText(""); front.requestFocus()
                    status.setTextColor(0xFF188038.toInt())
                    status.text = "✓ Added (${db.total(did)} cards in ${deckEt.text}). Add another, or Close."
                }
                if (mode == "home") showHome()
            }
        }
        dlg.show()
        front.requestFocus()
    }

    private fun addPicture(uri: Uri) {
        val target = pictureTarget ?: return
        bg.execute {
            try {
                val name = queryName(uri) ?: "picture.jpg"
                val data = contentResolver.openInputStream(uri)!!.use { it.readBytes() }
                val used = db.storeMedia(name, data)
                main.post { target.text.insert(maxOf(0, target.selectionStart), "[img:$used]") }
            } catch (e: Exception) { main.post { toast("Couldn't add the picture: ${e.message}") } }
        }
    }

    private fun queryName(uri: Uri): String? = try {
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { if (it.moveToFirst()) it.getString(0) else null }
    } catch (_: Exception) { null }

    // ---- browse ----

    private fun browse(deck: Long?) {
        val box = vbox(dp(16))
        val til = TextInputLayout(this, null, com.google.android.material.R.attr.textInputOutlinedStyle).apply { hint = "Search (words, or tag:name)" }
        val q = TextInputEditText(til.context).apply { setSingleLine() }
        til.addView(q); box.addView(til)
        val countV = text("", 12f, color = c(com.google.android.material.R.attr.colorOnSurfaceVariant))
        box.addView(countV, lp(top = 4))
        val list = vbox()
        box.addView(list)
        val scroll = ScrollView(this).apply { addView(box) }
        val dlg = MaterialAlertDialogBuilder(this).setTitle(deck?.let { db.deck(it)?.name } ?: "All cards").setView(scroll)
            .setPositiveButton("Close", null).create()
        fun line(s: String) = FlashText.IMG.replace(s.replace('\n', ' '), "🖼").let { if (it.length > 90) it.take(89) + "…" else it }
        lateinit var fill: () -> Unit
        fill = {
            list.removeAllViews()
            val rows = db.search(q.text.toString(), deck, 300)
            countV.text = "${rows.size} card(s)" + if (rows.size >= 300) " (first 300 — search to narrow)" else ""
            val now = Fsrs.now()
            for (r in rows) {
                val item = vbox(dp(10))
                item.addView(text(line(r.front), 15f, true, if (r.suspended) c(com.google.android.material.R.attr.colorOnSurfaceVariant) else null))
                item.addView(text(line(r.back), 13f, color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)))
                val nxt = if (r.state == Fsrs.NEW) "new" else if (r.due <= now) "due now" else "next: " + Fsrs.human(r.due - now)
                item.addView(text("${r.deck} · $nxt" + (if (r.suspended) " · suspended" else "") + (if (r.tags.isNotBlank()) " · ${r.tags}" else ""), 11f,
                    color = c(com.google.android.material.R.attr.colorOnSurfaceVariant)))
                item.setOnClickListener {
                    MaterialAlertDialogBuilder(this).setItems(arrayOf("Edit", "Move to deck…", "Suspend / unsuspend", "Forget progress", "Delete")) { _, i ->
                        when (i) {
                            0 -> editDialog(r.id, null) { fill() }
                            1 -> ask("Move to deck", hint = "Deck name (new or existing)") { n -> if (n.isNotBlank()) { db.moveNote(r.id, db.deckByName(n)); fill() } }
                            2 -> { db.toggleSuspendNote(r.id); fill() }
                            3 -> { db.resetNote(r.id); fill() }
                            4 -> MaterialAlertDialogBuilder(this).setTitle("Delete this card?").setPositiveButton("Delete") { _, _ -> db.deleteNote(r.id); fill() }
                                .setNegativeButton("Cancel", null).show()
                        }
                    }.show()
                }
                list.addView(item)
                list.addView(View(this).apply { setBackgroundColor(c(com.google.android.material.R.attr.colorOutlineVariant)) },
                    LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 1))
            }
        }
        var pending: Runnable? = null
        q.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun afterTextChanged(s: Editable?) { pending?.let { main.removeCallbacks(it) }; pending = Runnable { fill() }.also { main.postDelayed(it, 250) } }
        })
        dlg.setOnDismissListener { if (mode == "home") showHome() }
        fill()
        dlg.show()
    }

    // ---- import / export ----

    private fun importMenu() {
        MaterialAlertDialogBuilder(this).setTitle("Import flashcards")
            .setMessage("Choose an Anki deck (.apkg / .colpkg) or a text file (.txt / .csv: front, back, tags per line). " +
                    "Cards you already have are skipped.")
            .setPositiveButton("Choose file") { _, _ -> pickImport.launch(arrayOf("*/*")) }
            .setNegativeButton("Cancel", null).show()
    }

    private fun doImport(uri: Uri) {
        val name = queryName(uri) ?: uri.lastPathSegment ?: "import"
        val prog = text("Reading the file…", 15f).apply { setPadding(dp(24), dp(20), dp(24), dp(8)) }
        val dlg = MaterialAlertDialogBuilder(this).setTitle("Importing").setView(prog).setCancelable(false).show()
        bg.execute {
            val res = try {
                contentResolver.openInputStream(uri)!!.let { db.importStream(it, name) { m -> main.post { prog.text = m } } }
            } catch (e: Throwable) { e }
            main.post {
                dlg.dismiss()
                if (res is Throwable) {
                    MaterialAlertDialogBuilder(this).setTitle("Couldn't import").setMessage(res.message ?: res.toString()).setPositiveButton("OK", null).show()
                } else {
                    val pair = res as Pair<*, *>
                    val n = pair.first as Int
                    val decks = (pair.second as List<*>).map { it.toString() }
                    Logger.log(this, "Flashcards: imported $n card(s) from $name.")
                    MaterialAlertDialogBuilder(this).setTitle("Imported").setMessage(
                        "Added $n card(s)" + (if (decks.isNotEmpty()) " to: ${decks.take(6).joinToString(", ")}" else "") +
                                (if (n == 0) ".\n(Cards you already have are skipped.)" else ".")).setPositiveButton("OK", null).show()
                }
                if (mode == "home") showHome()
            }
        }
    }

    private fun exportDialog(deck: Long?) {
        val decks = db.decks()
        if (decks.isEmpty()) return toast("There are no decks to export yet.")
        val names = arrayOf("All decks") + decks.map { it.name }.toTypedArray()
        var pick = deck?.let { d -> decks.indexOfFirst { it.id == d } + 1 } ?: 0
        val box = vbox(dp(20))
        val dTil = TextInputLayout(this, null, com.google.android.material.R.attr.textInputOutlinedExposedDropdownMenuStyle).apply { hint = "Deck" }
        val dEt = AutoCompleteTextView(dTil.context).apply {
            setText(names[pick]); setAdapter(ArrayAdapter(this@FlashcardsActivity, android.R.layout.simple_list_item_1, names))
            inputType = InputType.TYPE_NULL; setPadding(dp(14), dp(16), dp(14), dp(16))
            setOnItemClickListener { _, _, i, _ -> pick = i }
        }
        dTil.addView(dEt); box.addView(dTil)
        val group = android.widget.RadioGroup(this)
        val r1 = android.widget.RadioButton(this).apply { text = "Anki deck (.apkg) — opens in Anki, AnkiDroid and the laptop app"; id = View.generateViewId(); isChecked = true }
        val r2 = android.widget.RadioButton(this).apply { text = "Text (.txt) — front, back and tags separated by tabs"; id = View.generateViewId() }
        group.addView(r1); group.addView(r2)
        box.addView(group, lp(top = 10))
        val prog = CheckBox(this).apply { text = "Include study progress (Anki deck only)"; isChecked = true }
        box.addView(prog, lp(top = 4))
        MaterialAlertDialogBuilder(this).setTitle("Export flashcards").setView(box)
            .setPositiveButton("Export") { _, _ ->
                exportDeck = if (pick == 0) null else decks[pick - 1].id
                exportFmt = if (r2.isChecked) "txt" else "apkg"
                exportProgress = prog.isChecked
                val base = (if (pick == 0) "Flashcards" else decks[pick - 1].name).replace("::", " - ").replace(Regex("[\\\\/:*?\"<>|]"), "_")
                pickExport.launch("$base.$exportFmt")
            }.setNegativeButton("Cancel", null).show()
    }

    private fun doExport(uri: Uri) {
        bg.execute {
            val res = try {
                contentResolver.openOutputStream(uri)!!.use { out ->
                    if (exportFmt == "apkg") db.exportApkg(out, exportDeck, exportProgress) else db.exportText(out, exportDeck)
                }
            } catch (e: Throwable) { e }
            main.post {
                if (res is Throwable) toast("Couldn't export: ${res.message}")
                else { toast("Saved $res card(s).") ; Logger.log(this, "Flashcards: exported $res card(s).") }
            }
        }
    }

    companion object {
        fun open(ctx: android.content.Context) = ctx.startActivity(Intent(ctx, FlashcardsActivity::class.java))
    }
}
