package com.sakib.notionsync

import android.Manifest
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.text.style.RelativeSizeSpan
import android.text.style.StyleSpan
import android.view.View
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.google.android.material.bottomnavigation.BottomNavigationView
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import com.google.android.material.chip.Chip
import com.google.android.material.chip.ChipDrawable
import com.google.android.material.chip.ChipGroup
import com.google.android.material.color.MaterialColors
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.progressindicator.CircularProgressIndicator
import com.google.android.material.textfield.TextInputEditText
import java.text.NumberFormat
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {

    private lateinit var prefs: Prefs
    private val meter = SpeedMeter()
    private var tick = 0
    private var updating = false
    private var tab = 0
    private val num: NumberFormat = NumberFormat.getIntegerInstance()
    private val timeFmt = SimpleDateFormat("d MMM, HH:mm", Locale.US)
    private val handler = Handler(Looper.getMainLooper())

    // header
    private lateinit var title: TextView
    private lateinit var subtitle: TextView
    private lateinit var pill: TextView
    private lateinit var pages: List<View>

    // home
    private lateinit var ring: CircularProgressIndicator
    private lateinit var percent: TextView
    private lateinit var statusTitle: TextView
    private lateinit var statusDetail: TextView
    private lateinit var currentFile: TextView
    private lateinit var nDone: TextView
    private lateinit var nLeft: TextView
    private lateinit var nIssues: TextView
    private lateinit var speedUp: TextView
    private lateinit var speedDown: TextView
    private lateinit var speedTotals: TextView
    private lateinit var spark: SparkView
    private lateinit var swAuto: MaterialSwitch
    private lateinit var autoDetail: TextView
    private lateinit var autoTimes: TextView
    private lateinit var issuesCard: MaterialCardView
    private lateinit var issuesDetail: TextView

    // settings
    private lateinit var notionStatus: TextView
    private lateinit var etToken: TextInputEditText
    private lateinit var etLink: TextInputEditText
    private lateinit var permFiles: MaterialButton
    private lateinit var permNotif: MaterialButton
    private lateinit var permBattery: MaterialButton
    private lateinit var intervalNow: TextView
    private lateinit var chips: ChipGroup
    private lateinit var etMinutes: TextInputEditText
    private lateinit var etExcluded: TextInputEditText
    private lateinit var updateText: TextView

    // activity
    private lateinit var logView: TextView

    // files
    private lateinit var files: FilesPage
    private val backCallback = object : OnBackPressedCallback(false) {
        override fun handleOnBackPressed() {
            files.goUp()
            isEnabled = files.canGoUp()
        }
    }

    private val colGood by lazy { ContextCompat.getColor(this, R.color.good) }
    private val colWarn by lazy { ContextCompat.getColor(this, R.color.warn) }
    private val colMuted by lazy { MaterialColors.getColor(logViewOrRoot(), com.google.android.material.R.attr.colorOnSurfaceVariant) }
    private val colPrimary by lazy { MaterialColors.getColor(logViewOrRoot(), androidx.appcompat.R.attr.colorPrimary) }

    private fun logViewOrRoot(): View = findViewById(android.R.id.content)

    private val ticker = object : Runnable {
        override fun run() {
            updateSpeed()
            if (tick++ % 2 == 0) refresh()
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        Notif.ensureChannel(this)
        setContentView(R.layout.activity_main)
        bindViews()
        setupHome()
        setupSettings()
        files = FilesPage(this, findViewById(R.id.page_files)) { etExcluded.setText(prefs.excluded) }
        files.onStackChanged = { backCallback.isEnabled = tab == TAB_FILES && files.canGoUp() }
        onBackPressedDispatcher.addCallback(this, backCallback)

        val nav = findViewById<BottomNavigationView>(R.id.nav)
        nav.setOnItemSelectedListener { item ->
            showTab(
                when (item.itemId) {
                    R.id.nav_files -> TAB_FILES
                    R.id.nav_activity -> TAB_ACTIVITY
                    R.id.nav_settings -> TAB_SETTINGS
                    else -> 0
                }
            )
            true
        }
        val start = savedInstanceState?.getInt("tab") ?: if (prefs.token.isBlank()) TAB_SETTINGS else TAB_HOME
        nav.selectedItemId = NAV_IDS[start]
        showTab(start)
        checkUpdate(install = false)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putInt("tab", tab)
    }

    override fun onResume() {
        super.onResume()
        handler.post(ticker)
    }

    override fun onPause() {
        super.onPause()
        prefs.excluded = etExcluded.text.toString()
        handler.removeCallbacks(ticker)
    }

    private fun bindViews() {
        title = findViewById(R.id.title)
        subtitle = findViewById(R.id.subtitle)
        pill = findViewById(R.id.state_pill)
        pages = listOf(
            findViewById(R.id.page_home), findViewById(R.id.page_files),
            findViewById(R.id.page_activity), findViewById(R.id.page_settings)
        )

        ring = findViewById(R.id.ring)
        percent = findViewById(R.id.percent)
        statusTitle = findViewById(R.id.status_title)
        statusDetail = findViewById(R.id.status_detail)
        currentFile = findViewById(R.id.current_file)
        nDone = findViewById(R.id.n_done)
        nLeft = findViewById(R.id.n_left)
        nIssues = findViewById(R.id.n_issues)
        speedUp = findViewById(R.id.speed_up)
        speedDown = findViewById(R.id.speed_down)
        speedTotals = findViewById(R.id.speed_totals)
        spark = findViewById(R.id.spark)
        swAuto = findViewById(R.id.sw_auto)
        autoDetail = findViewById(R.id.auto_detail)
        autoTimes = findViewById(R.id.auto_times)
        issuesCard = findViewById(R.id.issues_card)
        issuesDetail = findViewById(R.id.issues_detail)

        notionStatus = findViewById(R.id.notion_status)
        etToken = findViewById(R.id.et_token)
        etLink = findViewById(R.id.et_link)
        permFiles = findViewById(R.id.perm_files)
        permNotif = findViewById(R.id.perm_notif)
        permBattery = findViewById(R.id.perm_battery)
        intervalNow = findViewById(R.id.interval_now)
        chips = findViewById(R.id.chips)
        etMinutes = findViewById(R.id.et_minutes)
        etExcluded = findViewById(R.id.et_excluded)
        updateText = findViewById(R.id.update_text)

        logView = findViewById(R.id.log)
    }

    private fun showTab(i: Int) {
        tab = i
        pages.forEachIndexed { idx, v -> v.visibility = if (idx == i) View.VISIBLE else View.GONE }
        title.text = listOf("Backup", "Files", "Activity", "Settings")[i]
        if (i == TAB_FILES) files.onShown()
        backCallback.isEnabled = i == TAB_FILES && files.canGoUp()
        refresh()
    }

    // ---- home ---------------------------------------------------------------

    private fun setupHome() {
        findViewById<MaterialButton>(R.id.btn_sync).setOnClickListener {
            if (!ready()) {
                toast("Finish setup in Settings first.")
                findViewById<BottomNavigationView>(R.id.nav).selectedItemId = R.id.nav_settings
                return@setOnClickListener
            }
            prefs.excluded = etExcluded.text.toString()
            Scheduler.syncNow(this)
            toast(if (prefs.wifiOnly && Perms.onMeteredNetwork(this)) "Waiting for Wi-Fi (\"Only on Wi-Fi\" is on)." else "Sync started.")
            refresh()
        }
        findViewById<MaterialButton>(R.id.btn_stop).setOnClickListener {
            Scheduler.stop(this)
            toast(
                if (prefs.autoSync) "Stopped. Auto-sync will try again in ${Scheduler.intervalLabel(prefs.intervalMinutes)}."
                else "Stopped."
            )
            refresh()
        }
        swAuto.isChecked = prefs.autoSync
        swAuto.setOnCheckedChangeListener { _, on ->
            if (on == prefs.autoSync) return@setOnCheckedChangeListener
            if (on && !ready()) {
                toast("Finish setup in Settings first.")
                swAuto.isChecked = false
                return@setOnCheckedChangeListener
            }
            prefs.excluded = etExcluded.text.toString()
            prefs.autoSync = on
            if (on) {
                Scheduler.apply(this)
                Scheduler.syncNow(this)
            } else {
                Scheduler.stop(this)
                toast("Auto-sync off. Any running sync is stopping.")
            }
            refresh()
        }
        findViewById<MaterialButton>(R.id.btn_issues).setOnClickListener { showProblems() }
        findViewById<MaterialCardView>(R.id.issues_tile).setOnClickListener { showProblems() }
        findViewById<MaterialButton>(R.id.btn_retry).setOnClickListener {
            Db.get(this).resetProblems()
            toast("They'll be tried again on the next sync.")
            refresh()
        }
    }

    private fun updateSpeed() {
        val r = meter.sample()
        if (r == null) {
            speedUp.text = "n/a"
            speedDown.text = "n/a"
            speedTotals.text = "This phone doesn't report app speeds."
            return
        }
        speedUp.text = SpeedMeter.rate(r.upBps)
        speedDown.text = SpeedMeter.rate(r.downBps)
        spark.push(r.upBps)
        speedTotals.text = "This app only · sent ${SpeedMeter.size(r.totalUp)}, " +
            "received ${SpeedMeter.size(r.totalDown)} since the phone restarted"
    }

    private fun active() = prefs.running && System.currentTimeMillis() - prefs.heartbeat < 10 * 60_000

    private fun ready() = prefs.token.isNotBlank() && prefs.rootPageId.isNotBlank() && Perms.hasStorage(this)

    private fun setPill(text: String, bg: Int, fg: Int) {
        pill.text = text
        pill.setTextColor(fg)
        pill.background = GradientDrawable().apply {
            cornerRadius = 100f * resources.displayMetrics.density
            setColor(bg)
        }
    }

    private fun refresh() {
        val c = Db.get(this).counts()
        val done = c["done"] ?: 0
        val left = prefs.lastPending
        val issues = (c["skipped"] ?: 0) + (c["failed"] ?: 0) + (c["retry"] ?: 0)
        val total = done + left
        val pct = if (total == 0) (if (done > 0) 1000 else 0) else (done * 1000L / total).toInt()
        val running = active()
        val waitingWifi = prefs.wifiOnly && Perms.onMeteredNetwork(this)

        subtitle.text = if (prefs.rootTitle.isNotEmpty()) "Syncing to “${prefs.rootTitle}” in Notion" else "Not connected to Notion yet"

        // Status pill
        val green = ContextCompat.getColor(this, R.color.good_container)
        val amber = ContextCompat.getColor(this, R.color.warn_container)
        val neutral = MaterialColors.getColor(pill, com.google.android.material.R.attr.colorSurfaceContainerHighest)
        val onSurface = MaterialColors.getColor(pill, com.google.android.material.R.attr.colorOnSurface)
        when {
            !ready() -> setPill("Setup needed", amber, onSurface)
            running -> setPill("● Syncing", ContextCompat.getColor(this, R.color.primary_container),
                ContextCompat.getColor(this, R.color.on_primary_container))
            left > 0 && waitingWifi -> setPill("Waiting for Wi-Fi", amber, onSurface)
            left > 0 -> setPill("Paused", neutral, onSurface)
            else -> setPill("✓ Up to date", green, onSurface)
        }

        if (tab == TAB_HOME) {
            ring.setProgressCompat(pct, true)
            percent.text = if (total == 0 && done == 0) "–" else "${pct / 10}%"
            statusTitle.text = when {
                !ready() -> "Finish setup"
                running -> "Backing up…"
                left > 0 && waitingWifi -> "Waiting for Wi-Fi"
                left > 0 && prefs.autoSync -> "Paused for now"
                left > 0 -> "Paused"
                done > 0 -> "All backed up"
                else -> "Ready"
            }
            statusDetail.text = when {
                !ready() -> "Add your Notion details and allow file access in Settings."
                total > 0 && left > 0 -> "${num.format(done)} of ${num.format(total)} files are in Notion. " +
                    (if (running) "Uploading the newest first." else if (prefs.autoSync) "It continues on the next auto-sync." else "Tap Sync now to continue.")
                done > 0 -> "${num.format(done)} files are safely in Notion."
                else -> "Tap Sync now to start your first backup."
            }
            val cur = prefs.currentText
            currentFile.visibility = if (running && cur.isNotEmpty()) View.VISIBLE else View.GONE
            currentFile.text = cur
            nDone.text = num.format(done)
            nLeft.text = num.format(left)
            nIssues.text = num.format(issues)
            nIssues.setTextColor(if (issues > 0) colWarn else onSurface)

            if (swAuto.isChecked != prefs.autoSync) swAuto.isChecked = prefs.autoSync
            val rules = mutableListOf("every ${Scheduler.intervalLabel(prefs.intervalMinutes)}")
            if (prefs.wifiOnly) rules.add("Wi-Fi only")
            if (prefs.chargingOnly) rules.add("while charging")
            autoDetail.text = if (prefs.autoSync) rules.joinToString(" · ").replaceFirstChar { it.uppercase() }
            else "Off. Turn on to back up new files automatically."
            val last = if (prefs.lastRun == 0L) "never" else timeFmt.format(Date(prefs.lastRun))
            val next = if (prefs.autoSync && prefs.lastRun > 0 && !running)
                " · next ≈ ${SimpleDateFormat("HH:mm", Locale.US).format(Date(prefs.lastRun + prefs.intervalMinutes * 60_000L))}" else ""
            autoTimes.text = "Last run $last$next"

            issuesCard.visibility = if (issues > 0) View.VISIBLE else View.GONE
            val parts = mutableListOf<String>()
            (c["skipped"] ?: 0).let { if (it > 0) parts.add("${num.format(it)} skipped (Notion doesn't accept that file type, or it's too big)") }
            (c["retry"] ?: 0).let { if (it > 0) parts.add("${num.format(it)} will be retried automatically") }
            (c["failed"] ?: 0).let { if (it > 0) parts.add("${num.format(it)} failed 5 times") }
            issuesDetail.text = parts.joinToString("\n")
        }

        if (tab == TAB_SETTINGS) refreshSettings()
        if (tab == TAB_ACTIVITY) renderLog()
        if (tab == TAB_FILES) backCallback.isEnabled = files.canGoUp()
    }

    // ---- settings -------------------------------------------------------------

    private fun setupSettings() {
        etToken.setText(prefs.token)
        etLink.setText(prefs.rootPageId)
        etExcluded.setText(prefs.excluded)
        etMinutes.setText(prefs.intervalMinutes.toString())
        notionStatus.text = if (prefs.rootTitle.isEmpty()) "Not connected yet." else "✓ Connected to “${prefs.rootTitle}”"

        findViewById<MaterialButton>(R.id.btn_save_notion).setOnClickListener {
            saveNotion(etToken.text.toString().trim(), etLink.text.toString())
        }
        permFiles.setOnClickListener { askStorage() }
        permBattery.setOnClickListener { askBattery() }
        if (Build.VERSION.SDK_INT >= 33) {
            permNotif.setOnClickListener {
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2)
            }
        } else {
            findViewById<View>(R.id.row_notif).visibility = View.GONE
        }

        val presets = listOf(15 to "15 min", 30 to "30 min", 60 to "1 hour", 120 to "2 hours",
            360 to "6 hours", 720 to "12 hours", 1440 to "1 day")
        for ((m, label) in presets) {
            val chip = Chip(this).apply {
                setChipDrawable(
                    ChipDrawable.createFromAttributes(
                        this@MainActivity, null, 0, com.google.android.material.R.style.Widget_Material3_Chip_Filter
                    )
                )
                text = label
                isCheckable = true
                tag = m
                id = View.generateViewId()
            }
            chips.addView(chip)
            chip.isChecked = prefs.intervalMinutes == m
            chip.setOnClickListener { if (chip.isChecked) setInterval(m) }
        }
        findViewById<MaterialButton>(R.id.btn_minutes).setOnClickListener {
            setInterval(etMinutes.text.toString().toIntOrNull() ?: 0)
        }

        bindSwitch(R.id.sw_wifi, prefs.wifiOnly) {
            prefs.wifiOnly = it
            if (it && Perms.onMeteredNetwork(this)) {
                Scheduler.stop(this)
                toast("Paused until the phone is on Wi-Fi.")
            } else {
                Scheduler.apply(this)
            }
        }
        bindSwitch(R.id.sw_charging, prefs.chargingOnly) {
            prefs.chargingOnly = it
            Scheduler.apply(this)
        }
        bindSwitch(R.id.sw_hidden, prefs.skipHidden) { prefs.skipHidden = it }

        findViewById<MaterialButton>(R.id.btn_update).setOnClickListener { checkUpdate(install = true) }
        updateText.text = "Version: build ${Updater.currentBuild(this)}"
    }

    private fun bindSwitch(id: Int, value: Boolean, onChange: (Boolean) -> Unit) {
        val sw = findViewById<MaterialSwitch>(id)
        sw.isChecked = value
        sw.setOnCheckedChangeListener { _, on -> onChange(on); refresh() }
    }

    private fun setInterval(minutes: Int) {
        if (minutes < 15) {
            toast("Android allows 15 minutes at the shortest.")
            etMinutes.setText(prefs.intervalMinutes.toString())
            return
        }
        prefs.intervalMinutes = minutes
        etMinutes.setText(prefs.intervalMinutes.toString())
        Scheduler.apply(this, delayFirstRun = true)
        toast("Auto-sync set to every ${Scheduler.intervalLabel(prefs.intervalMinutes)}.")
        refresh()
    }

    private fun refreshSettings() {
        fun perm(b: MaterialButton, ok: Boolean) {
            b.isEnabled = !ok
            b.text = if (ok) "✓ Allowed" else "Allow"
        }
        perm(permFiles, Perms.hasStorage(this))
        perm(permNotif, Perms.notifications(this))
        perm(permBattery, Perms.battery(this))
        intervalNow.text = "Every ${Scheduler.intervalLabel(prefs.intervalMinutes)}" +
            if (prefs.autoSync) "" else " (auto-sync is off)"
        for (i in 0 until chips.childCount) {
            val chip = chips.getChildAt(i) as Chip
            val should = chip.tag == prefs.intervalMinutes
            if (chip.isChecked != should) chip.isChecked = should
        }
    }

    private fun saveNotion(token: String, link: String) {
        val id = NotionClient.parsePageId(link)
        if (token.isEmpty()) {
            notionStatus.text = "Paste the integration secret first."
            return
        }
        if (id == null) {
            notionStatus.text = "That doesn't look like a Notion page link."
            return
        }
        notionStatus.text = "Checking…"
        Thread {
            val msg = try {
                val t = NotionClient(token).pageTitle(id)
                if (prefs.rootPageId.isNotEmpty() && prefs.rootPageId != id) Db.get(this).resetAll()
                prefs.token = token
                prefs.rootPageId = id
                prefs.rootTitle = t
                "✓ Connected to “$t”"
            } catch (e: NotionException) {
                when (e.status) {
                    401 -> "Notion didn't accept that secret. Copy it again."
                    400, 404 -> "Can't open that page. In Notion, open the page → ••• → Connections → add your integration."
                    else -> "Notion error: ${e.message}"
                }
            } catch (e: Exception) {
                "Couldn't reach Notion: ${e.message}"
            }
            runOnUiThread {
                notionStatus.text = msg
                refresh()
            }
        }.start()
    }

    private fun checkUpdate(install: Boolean) {
        if (updating) return
        updating = true
        updateText.text = "Checking for updates…"
        fun show(t: String) = runOnUiThread { updateText.text = t }
        Thread {
            try {
                val current = Updater.currentBuild(this)
                val rel = Updater.latest()
                if (rel == null || rel.build <= current) {
                    show("Version: build $current · You have the latest version.")
                } else if (!install) {
                    show("Version: build $current · Update available: build ${rel.build}")
                    runOnUiThread {
                        findViewById<MaterialButton>(R.id.btn_update).text = "Install build ${rel.build}"
                        findViewById<BottomNavigationView>(R.id.nav).getOrCreateBadge(R.id.nav_settings)
                    }
                } else if (!packageManager.canRequestPackageInstalls()) {
                    show("Allow \"Install unknown apps\" for this app, come back, and tap the button again.")
                    runOnUiThread {
                        startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
                    }
                } else {
                    val apk = Updater.download(this, rel) { pct -> show("Downloading build ${rel.build}… $pct%") }
                    show("Installing build ${rel.build}… Tap Update when Android asks.")
                    runOnUiThread { Updater.install(this, apk) }
                }
            } catch (e: Exception) {
                show("Couldn't check for updates: ${e.message ?: "no internet?"}")
            } finally {
                updating = false
            }
        }.start()
    }

    private fun askStorage() {
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                startActivity(Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName")))
            } catch (e: Exception) {
                startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            }
        } else {
            requestPermissions(arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE), 1)
        }
    }

    private fun askBattery() {
        try {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
        } catch (e: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    // ---- activity log ------------------------------------------------------------

    private fun pretty(path: String): String = path
        .replace(Regex("^Android/media/com\\.whatsapp/WhatsApp/(accounts/\\d+/)?Media/"), "WhatsApp/")
        .replace(Regex("^Android/media/com\\.whatsapp/WhatsApp/(accounts/\\d+/)?"), "WhatsApp/")

    private fun friendlyReason(r: String): String = when {
        r.contains("Unable to resolve host") || r.contains("UnknownHost") -> "no internet"
        r.contains("timeout", true) -> "connection too slow"
        r.contains("Sync stopped") -> "stopped"
        else -> r
    }

    private class Entry(val icon: String, val color: Int, val main: String, val sub: String)

    private fun describe(msg: String, uploaded: Regex, already: Regex, couldnt: Regex, skipped: Regex): Entry {
        uploaded.find(msg)?.let { m ->
            val p = pretty(m.groupValues[1])
            return Entry("✓", colGood, p.substringAfterLast('/'), p.substringBeforeLast('/', "phone storage"))
        }
        already.find(msg)?.let { m ->
            val p = pretty(m.groupValues[1])
            return Entry("✓", colMuted, p.substringAfterLast('/'), "already in Notion")
        }
        couldnt.find(msg)?.let { m ->
            val p = pretty(m.groupValues[1])
            return Entry("!", colWarn, p.substringAfterLast('/'),
                "not uploaded: ${friendlyReason(m.groupValues[2])} · ${m.groupValues[3].lowercase().removeSuffix(".")}")
        }
        skipped.find(msg)?.let { m ->
            val p = pretty(m.groupValues[1])
            return Entry("⊘", colMuted, p.substringAfterLast('/'), "skipped: ${m.groupValues[2]}")
        }
        return when {
            msg.startsWith("Sync finished") || msg.startsWith("Everything is up to date") ->
                Entry("✓", colPrimary, msg, "")
            msg.startsWith("Connection problem") || msg.startsWith("Notion refused") ||
                msg.startsWith("Sync stopped:") || msg.startsWith("Paused") ->
                Entry("!", colWarn, friendlyReason(msg), "")
            else -> Entry("•", colMuted, msg, "")
        }
    }

    private fun renderLog() {
        val lines = Logger.tail(this, 80)
        if (lines == "Nothing yet.") {
            logView.text = "Nothing yet. Tap Sync now on the Backup tab."
            return
        }
        val sb = SpannableStringBuilder()
        val uploaded = Regex("^Uploaded (.+)$")
        val already = Regex("^Already in Notion: (.+)$")
        val couldnt = Regex("^Couldn't upload (.+?) \\((.*)\\)\\. (.*)$")
        val skipped = Regex("^Skipped (.+?): (.*)$")
        for (raw in lines.lines()) {
            if (raw.length < 17) continue
            val time = raw.substring(0, 15).trim()
            val msg = raw.substring(15).trim()
            val e = describe(msg, uploaded, already, couldnt, skipped)
            val icon = e.icon
            val iconColor = e.color
            val main = e.main
            val sub = e.sub
            val s = sb.length
            sb.append(icon).append("  ")
            sb.setSpan(ForegroundColorSpan(iconColor), s, s + icon.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            sb.setSpan(StyleSpan(Typeface.BOLD), s, s + icon.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            sb.append(main).append('\n')
            val s2 = sb.length
            sb.append("     ").append(time)
            if (sub.isNotEmpty()) sb.append(" · ").append(sub)
            sb.append("\n\n")
            sb.setSpan(ForegroundColorSpan(colMuted), s2, sb.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            sb.setSpan(RelativeSizeSpan(0.82f), s2, sb.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
        }
        logView.text = sb
    }

    // ---- misc --------------------------------------------------------------------------

    private fun showProblems() {
        val items = Db.get(this).problems()
        val text = if (items.isEmpty()) "Nothing skipped or failed."
        else items.joinToString("\n\n") { "${pretty(it.first)}\n→ ${friendlyReason(it.second)}" }
        MaterialAlertDialogBuilder(this)
            .setTitle("Files that didn't upload")
            .setMessage(text)
            .setPositiveButton("OK", null)
            .setNeutralButton("Try again") { _, _ ->
                Db.get(this).resetProblems()
                toast("They'll be tried again on the next sync.")
            }
            .show()
    }

    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_LONG).show()

    @Suppress("unused")
    private fun tint(c: Int) = ColorStateList.valueOf(c)

    companion object {
        const val TAB_HOME = 0
        const val TAB_FILES = 1
        const val TAB_ACTIVITY = 2
        const val TAB_SETTINGS = 3
        val NAV_IDS = listOf(R.id.nav_home, R.id.nav_files, R.id.nav_activity, R.id.nav_settings)
    }
}
