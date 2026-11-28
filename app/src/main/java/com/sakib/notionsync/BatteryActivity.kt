package com.sakib.notionsync

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.button.MaterialButton
import com.google.android.material.color.MaterialColors
import com.google.android.material.materialswitch.MaterialSwitch

/** Battery: live status, health, a 48-hour chart and unplug / plug-in reminders (like the laptop app). */
class BatteryActivity : AppCompatActivity() {
    private lateinit var root: FrameLayout
    private val main = Handler(Looper.getMainLooper())
    private val tick = object : Runnable { override fun run() { refresh(); main.postDelayed(this, 2000) } }
    private lateinit var bigPct: TextView
    private lateinit var stateLine: TextView
    private lateinit var ring: Ring
    private val values = HashMap<String, Pair<TextView, TextView>>()
    private lateinit var chart: Chart
    private lateinit var healthNote: TextView

    private fun c(attr: Int) = MaterialColors.getColor(root, attr)
    private fun dp(v: Int) = TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v.toFloat(), resources.displayMetrics).toInt()
    private val onSurface get() = c(com.google.android.material.R.attr.colorOnSurface)
    private val muted get() = c(com.google.android.material.R.attr.colorOnSurfaceVariant)
    private val surface2 get() = c(com.google.android.material.R.attr.colorSurfaceContainerLow)

    private fun text(s: String, size: Float, bold: Boolean = false, color: Int? = null) = TextView(this).apply {
        text = s; setTextSize(TypedValue.COMPLEX_UNIT_SP, size); setTextColor(color ?: onSurface)
        if (bold) typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
    }
    private fun lp(w: Int = ViewGroup.LayoutParams.MATCH_PARENT, h: Int = ViewGroup.LayoutParams.WRAP_CONTENT, top: Int = 0, weight: Float = 0f) =
        LinearLayout.LayoutParams(w, h, weight).apply { topMargin = dp(top) }
    private fun box(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL; setPadding(dp(16), dp(14), dp(16), dp(14))
        background = GradientDrawable().apply { cornerRadius = dp(20).toFloat(); setColor(surface2) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        root = FrameLayout(this)
        root.fitsSystemWindows = true
        setContentView(root)
        root.setBackgroundColor(MaterialColors.getColor(this, android.R.attr.colorBackground, Color.WHITE))
        Battery.attach(this)
        build()
    }

    override fun onResume() { super.onResume(); main.post(tick) }
    override fun onPause() { super.onPause(); main.removeCallbacks(tick) }

    private fun build() {
        val scroll = ScrollView(this)
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(18), dp(10), dp(18), dp(24)) }
        scroll.addView(col)
        root.addView(scroll)

        val top = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL }
        top.addView(ImageView(this).apply {
            setImageResource(R.drawable.ic_back); setPadding(dp(8), dp(8), dp(8), dp(8)); setColorFilter(onSurface); setOnClickListener { finish() }
        })
        top.addView(text("Battery", 26f, true), lp(0, weight = 1f).apply { leftMargin = dp(6) })
        col.addView(top)

        // big level ring
        val hero = FrameLayout(this)
        ring = Ring()
        hero.addView(ring, FrameLayout.LayoutParams(dp(190), dp(190), Gravity.CENTER))
        val mid = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER }
        bigPct = text("–", 44f, true).apply { gravity = Gravity.CENTER }
        stateLine = text("", 14f, color = muted).apply { gravity = Gravity.CENTER }
        mid.addView(bigPct); mid.addView(stateLine)
        hero.addView(mid, FrameLayout.LayoutParams(dp(170), ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.CENTER))
        col.addView(hero, lp(h = dp(210), top = 8))

        // details grid
        val keys = listOf("health" to "Health", "temp" to "Temperature", "current" to "Current", "volts" to "Voltage",
            "capacity" to "Holds now", "cycles" to "Charge cycles")
        for (row in keys.chunked(2)) {
            val r = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
            for ((k, label) in row) {
                val b = box()
                b.addView(text(label, 12f, color = muted))
                val v = text("–", 18f, true); val s = text("", 11f, color = muted)
                b.addView(v); b.addView(s)
                values[k] = v to s
                r.addView(b, lp(0, weight = 1f).apply { rightMargin = dp(8) })
            }
            col.addView(r, lp(top = 8))
        }
        healthNote = text("", 12f, color = muted)
        col.addView(healthNote, lp(top = 8))

        // chart
        val cb = box()
        cb.addView(text("Last 48 hours", 14f, true))
        chart = Chart()
        cb.addView(chart, lp(h = dp(130), top = 8))
        cb.addView(text("Green = charging. The chart fills in while the app runs (it keeps a point every 5 minutes).", 11f, color = muted), lp(top = 6))
        col.addView(cb, lp(top = 14))

        // reminders
        val rb = box()
        val sw = MaterialSwitch(this).apply { text = "Remind me to unplug and plug in"; isChecked = Battery.remindOn(this@BatteryActivity); textSize = 15f }
        rb.addView(sw)
        val unLbl = text("", 13f); val plLbl = text("", 13f)
        val un = SeekBar(this).apply { max = 50; progress = Battery.unplugAt(this@BatteryActivity) - 50 }
        val pl = SeekBar(this).apply { max = 40; progress = Battery.plugAt(this@BatteryActivity) - 5 }
        fun save() {
            unLbl.text = "Unplug at ${50 + un.progress}%"; plLbl.text = "Plug in at ${5 + pl.progress}%"
            Battery.setReminders(this, sw.isChecked, 50 + un.progress, 5 + pl.progress)
        }
        val l = object : SeekBar.OnSeekBarChangeListener {
            override fun onProgressChanged(s: SeekBar?, p: Int, u: Boolean) { save() }
            override fun onStartTrackingTouch(s: SeekBar?) {}
            override fun onStopTrackingTouch(s: SeekBar?) {}
        }
        un.setOnSeekBarChangeListener(l); pl.setOnSeekBarChangeListener(l)
        sw.setOnCheckedChangeListener { _, on ->
            save()
            if (on && Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 5)
        }
        rb.addView(unLbl, lp(top = 10)); rb.addView(un)
        rb.addView(plLbl, lp(top = 6)); rb.addView(pl)
        save()
        rb.addView(text("Keeping the battery between about 20% and 80% makes it last years longer. Reminders work while " +
                "the shared clipboard or auto-sync keeps the app running.", 11f, color = muted), lp(top = 6))
        val test = MaterialButton(this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle).apply {
            text = "Show a test reminder"; isAllCaps = false
            setOnClickListener { Battery.notify(this@BatteryActivity, 7103, "🔋 Test reminder", "This is how the unplug / plug-in reminders look.") }
        }
        rb.addView(test, lp(ViewGroup.LayoutParams.WRAP_CONTENT, top = 6))
        col.addView(rb, lp(top = 14))

        // Android's own pages
        for ((label, action) in listOf("Battery use by app (Android)" to Intent.ACTION_POWER_USAGE_SUMMARY,
            "Battery saver" to Settings.ACTION_BATTERY_SAVER_SETTINGS)) {
            col.addView(MaterialButton(this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle).apply {
                text = label; isAllCaps = false
                setOnClickListener { try { startActivity(Intent(action)) } catch (_: Exception) { Toast.makeText(this@BatteryActivity, "This phone doesn't have that page.", Toast.LENGTH_SHORT).show() } }
            }, lp(top = 8))
        }
    }

    private fun refresh() {
        val b = try { Battery.read(this) } catch (_: Exception) { return }
        bigPct.text = "${b.level}%"
        ring.set(b.level, b.charging || b.full)
        stateLine.text = when {
            b.full -> "Full · on ${b.source.ifEmpty { "charger" }}"
            b.charging -> "Charging (${b.source})" + (b.minutesToFull?.let { " · full in " + (if (it >= 60) "${it / 60} h ${it % 60} min" else "$it min") } ?: "")
            else -> "On battery"
        }
        fun set(k: String, v: String, s: String = "") { values[k]?.let { it.first.text = v; it.second.text = s } }
        set("health", b.healthPct?.let { "$it%" } ?: b.health, if (b.healthPct != null) b.health else "")
        set("temp", b.tempC?.let { "%.1f °C".format(it) } ?: "–", b.tempC?.let { if (it >= 42) "hot — let it cool" else if (it >= 38) "warm" else "normal" } ?: "")
        set("current", b.currentMa?.let { "${if (it > 0) "+" else ""}$it mA" } ?: "–", b.currentMa?.let { if (it > 0) "going in" else "being used" } ?: "")
        set("volts", b.volts?.let { "%.2f V".format(it) } ?: "–", b.technology)
        set("capacity", b.estFullMah?.let { "$it mAh" } ?: "–", b.designMah?.let { "new: $it mAh" } ?: "when full (estimate)")
        set("cycles", b.cycles?.toString() ?: "–", if (b.cycles == null) "not shown by this phone" else "")
        healthNote.text = b.healthPct?.let { "Health is an estimate: what the battery holds now compared with when it was new. " +
                "It's most accurate when the phone is charged above 50%." } ?: ""
        chart.points = Battery.history(this)
        chart.invalidate()
    }

    inner class Ring : View(this@BatteryActivity) {
        private var level = 0; private var charging = false
        private val p = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND }
        fun set(l: Int, c: Boolean) { level = l; charging = c; invalidate() }
        override fun onDraw(cv: Canvas) {
            val w = dp(14).toFloat()
            p.strokeWidth = w
            val r = RectF(w, w, width - w, height - w)
            p.color = c(com.google.android.material.R.attr.colorSurfaceContainerHighest)
            cv.drawArc(r, 135f, 270f, false, p)
            p.color = when { charging -> 0xFF188038.toInt(); level <= 20 -> 0xFFD93025.toInt(); level <= 40 -> 0xFFE37400.toInt(); else -> c(androidx.appcompat.R.attr.colorPrimary) }
            cv.drawArc(r, 135f, 270f * level / 100f, false, p)
        }
    }

    inner class Chart : View(this@BatteryActivity) {
        var points: List<Triple<Long, Int, Boolean>> = emptyList()
        private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = dp(2).toFloat() }
        private val grid = Paint().apply { strokeWidth = 1f }
        private val label = Paint(Paint.ANTI_ALIAS_FLAG).apply { textSize = dp(10).toFloat() }
        override fun onDraw(cv: Canvas) {
            grid.color = c(com.google.android.material.R.attr.colorOutlineVariant); label.color = muted
            for (pct in listOf(0, 50, 100)) {
                val y = height - height * pct / 100f
                cv.drawLine(0f, y.coerceIn(1f, height - 1f), width.toFloat(), y.coerceIn(1f, height - 1f), grid)
                cv.drawText("$pct%", 2f, (y - 3).coerceIn(label.textSize, height - 3f), label)
            }
            if (points.size < 2) { cv.drawText("Not enough data yet", width / 3f, height / 2f, label); return }
            val now = System.currentTimeMillis() / 1000
            val start = now - 48 * 3600
            fun x(t: Long) = ((t - start).toFloat() / (48 * 3600)) * width
            fun y(l: Int) = height - height * l / 100f
            for (i in 1 until points.size) {
                val a = points[i - 1]; val b = points[i]
                if (b.first - a.first > 30 * 60) continue          // a gap: the app wasn't running
                line.color = if (b.third) 0xFF188038.toInt() else c(androidx.appcompat.R.attr.colorPrimary)
                cv.drawLine(x(a.first), y(a.second), x(b.first), y(b.second), line)
            }
        }
    }
}
