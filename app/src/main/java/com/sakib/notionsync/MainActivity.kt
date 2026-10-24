package com.sakib.notionsync

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.text.DateFormat
import java.util.Date

class MainActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var permText: TextView
    private lateinit var notionText: TextView
    private lateinit var statusText: TextView
    private lateinit var logText: TextView
    private lateinit var excludedEt: EditText
    private lateinit var autoCb: CheckBox
    private lateinit var updateText: TextView
    private var updating = false

    private val handler = Handler(Looper.getMainLooper())
    private val ticker = object : Runnable {
        override fun run() {
            refresh()
            handler.postDelayed(this, 2000)
        }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        Notif.ensureChannel(this)

        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(12), dp(16), dp(40))
        }
        val scroll = ScrollView(this)
        scroll.addView(col)
        setContentView(scroll)

        // 1. Permissions
        col.addView(header("1. Permissions"))
        permText = body("")
        col.addView(permText)
        col.addView(button("Allow access to all files") { askStorage() })
        if (Build.VERSION.SDK_INT >= 33) {
            col.addView(button("Allow notifications") {
                requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 2)
            })
        }
        col.addView(button("Let it run in the background") { askBattery() })

        // 2. Notion
        col.addView(header("2. Notion"))
        col.addView(body("Integration secret (from notion.so/profile/integrations):"))
        val tokenEt = edit("ntn_…", prefs.token, password = true)
        col.addView(tokenEt)
        col.addView(body("Link to your Notion backup page:"))
        val linkEt = edit("https://www.notion.so/…", prefs.rootPageId, password = false)
        col.addView(linkEt)
        notionText = body(
            if (prefs.rootTitle.isEmpty()) "Not connected yet." else "✓ Connected to “${prefs.rootTitle}”"
        )
        col.addView(notionText)
        col.addView(button("Save & test connection") {
            saveNotion(tokenEt.text.toString().trim(), linkEt.text.toString())
        })

        // 3. Options
        col.addView(header("3. Options"))
        col.addView(checkbox("Upload only on Wi-Fi (all syncs)", prefs.wifiOnly) {
            prefs.wifiOnly = it
            if (it && Perms.onMeteredNetwork(this)) {
                // On mobile data right now: stop the current upload too.
                Scheduler.stop(this)
                toast("Paused until the phone is on Wi-Fi.")
            } else {
                Scheduler.apply(this)
            }
        })
        col.addView(checkbox("Auto-sync only while charging", prefs.chargingOnly) {
            prefs.chargingOnly = it
            Scheduler.apply(this)
        })
        col.addView(checkbox("Skip hidden folders and files (names starting with a dot)", prefs.skipHidden) {
            prefs.skipHidden = it
        })
        col.addView(body("Folders to skip, one per line (paths from phone storage, e.g. Android/data):"))
        excludedEt = edit("", prefs.excluded, password = false, multiline = true)
        col.addView(excludedEt)

        // 4. Sync
        col.addView(header("4. Sync"))
        autoCb = checkbox("Auto-sync every 15 minutes", prefs.autoSync) { on ->
            if (on && !ready()) {
                toast("Finish steps 1 and 2 first.")
                autoCb.isChecked = false
                return@checkbox
            }
            prefs.excluded = excludedEt.text.toString()
            prefs.autoSync = on
            if (on) {
                Scheduler.apply(this)
                Scheduler.syncNow(this)
            } else {
                // Turning auto-sync off also stops a sync that is already running.
                Scheduler.stop(this)
                toast("Auto-sync off. Any running sync is stopping.")
            }
        }
        col.addView(autoCb)
        col.addView(button("Sync now") {
            if (!ready()) {
                toast("Finish steps 1 and 2 first.")
            } else {
                prefs.excluded = excludedEt.text.toString()
                Scheduler.syncNow(this)
                if (prefs.wifiOnly && Perms.onMeteredNetwork(this)) {
                    toast("Waiting for Wi-Fi (\"Upload only on Wi-Fi\" is on).")
                } else {
                    toast("Sync started.")
                }
            }
        })
        col.addView(button("Stop syncing") {
            Scheduler.stop(this)
            toast(if (prefs.autoSync) "Stopped. Auto-sync will try again in 15 minutes." else "Stopped.")
        })
        col.addView(button("Show skipped & failed files") { showProblems() })
        col.addView(button("Try skipped & failed files again") {
            Db.get(this).resetProblems()
            toast("They'll be retried on the next sync.")
        })

        statusText = body("")
        statusText.setPadding(0, dp(12), 0, dp(8))
        col.addView(statusText)

        // 5. Updates
        col.addView(header("5. App updates"))
        updateText = body("Version: build ${Updater.currentBuild(this)}")
        col.addView(updateText)
        col.addView(button("Check for update & install") { checkUpdate(install = true) })
        col.addView(body("Updating keeps your backup progress. The sync picks up where it left off."))
        checkUpdate(install = false)

        col.addView(header("Activity"))
        logText = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 11f
            setTextIsSelectable(true)
        }
        col.addView(logText)
    }

    override fun onResume() {
        super.onResume()
        handler.post(ticker)
    }

    override fun onPause() {
        super.onPause()
        prefs.excluded = excludedEt.text.toString()
        handler.removeCallbacks(ticker)
    }

    private fun ready() = prefs.token.isNotBlank() && prefs.rootPageId.isNotBlank() && Perms.hasStorage(this)

    private fun refresh() {
        fun mark(ok: Boolean) = if (ok) "✓" else "✗"
        permText.text = "${mark(Perms.hasStorage(this))} Access to all files\n" +
            "${mark(Perms.notifications(this))} Notifications\n" +
            "${mark(Perms.battery(this))} Allowed to run in the background"
        val c = Db.get(this).counts()
        val last = if (prefs.lastRun == 0L) "never"
        else DateFormat.getDateTimeInstance(DateFormat.SHORT, DateFormat.SHORT).format(Date(prefs.lastRun))
        val active = prefs.running && System.currentTimeMillis() - prefs.heartbeat < 10 * 60_000
        statusText.text = (if (active) "● Syncing now" else "○ Idle") +
            "\nUploaded: ${c["done"] ?: 0}    Waiting: ${prefs.lastPending}" +
            "\nSkipped: ${c["skipped"] ?: 0}    Failed: ${c["failed"] ?: 0}    Will retry: ${c["retry"] ?: 0}" +
            "\nLast run: $last"
        logText.text = Logger.tail(this, 40)
    }

    private fun saveNotion(token: String, link: String) {
        val id = NotionClient.parsePageId(link)
        if (token.isEmpty()) {
            notionText.text = "Paste the integration secret first."
            return
        }
        if (id == null) {
            notionText.text = "That doesn't look like a Notion page link."
            return
        }
        notionText.text = "Checking…"
        Thread {
            val msg = try {
                val title = NotionClient(token).pageTitle(id)
                if (prefs.rootPageId.isNotEmpty() && prefs.rootPageId != id) Db.get(this).resetAll()
                prefs.token = token
                prefs.rootPageId = id
                prefs.rootTitle = title
                "✓ Connected to “$title”"
            } catch (e: NotionException) {
                when (e.status) {
                    401 -> "Notion didn't accept that secret. Copy it again."
                    400, 404 -> "Can't open that page. In Notion, open the page → ••• → Connections → add your integration."
                    else -> "Notion error: ${e.message}"
                }
            } catch (e: Exception) {
                "Couldn't reach Notion: ${e.message}"
            }
            runOnUiThread { notionText.text = msg }
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
                    show("Version: build $current. You have the latest version.")
                } else if (!install) {
                    show("Version: build $current. Update available: build ${rel.build}. Tap the button below to install it.")
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
                startActivity(
                    Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:$packageName"))
                )
            } catch (e: Exception) {
                startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            }
        } else {
            requestPermissions(arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE), 1)
        }
    }

    private fun askBattery() {
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
            )
        } catch (e: Exception) {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
    }

    private fun showProblems() {
        val items = Db.get(this).problems()
        val text = if (items.isEmpty()) "Nothing skipped or failed."
        else items.joinToString("\n\n") { "${it.first}\n→ ${it.second}" }
        AlertDialog.Builder(this)
            .setTitle("Skipped & failed files")
            .setMessage(text)
            .setPositiveButton("OK", null)
            .show()
    }

    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_LONG).show()

    private fun header(t: String) = TextView(this).apply {
        text = t
        textSize = 18f
        setTypeface(typeface, Typeface.BOLD)
        setPadding(0, dp(20), 0, dp(6))
    }

    private fun body(t: String) = TextView(this).apply {
        text = t
        textSize = 14f
        setPadding(0, dp(4), 0, dp(4))
    }

    private fun button(t: String, onClick: () -> Unit) = Button(this).apply {
        text = t
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    private fun edit(hint: String, value: String, password: Boolean, multiline: Boolean = false) =
        EditText(this).apply {
            this.hint = hint
            setText(value)
            inputType = when {
                password -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
                multiline -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
                else -> InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            }
            if (multiline) minLines = 3
            textSize = 14f
        }

    private fun checkbox(t: String, value: Boolean, onChange: (Boolean) -> Unit) = CheckBox(this).apply {
        text = t
        isChecked = value
        setOnCheckedChangeListener { _, checked -> onChange(checked) }
    }
}
