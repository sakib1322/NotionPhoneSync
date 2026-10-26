package com.sakib.notionsync

import android.app.Activity
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.TextView
import android.widget.Toast
import com.google.android.material.button.MaterialButton
import com.google.android.material.chip.Chip
import com.google.android.material.chip.ChipDrawable
import com.google.android.material.chip.ChipGroup
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import java.text.NumberFormat

/** The Space card (Backup tab): what the app stores, and its downloaded copies of cloud files. */
class SpaceTools(private val act: Activity) {
    private val prefs = Prefs(act)
    private val main = Handler(Looper.getMainLooper())
    private val num: NumberFormat = NumberFormat.getIntegerInstance()

    // ---- the card --------------------------------------------------------------------------

    private var lastCheck = 0L
    private var checking = false

    fun bindCard(root: View) {
        val chips = root.findViewById<ChipGroup>(R.id.space_limit)
        for ((mb, label) in listOf(250 to "250 MB", 500 to "500 MB", 1024 to "1 GB", 2048 to "2 GB", 5120 to "5 GB")) {
            val chip = Chip(act).apply {
                setChipDrawable(ChipDrawable.createFromAttributes(act, null, 0, com.google.android.material.R.style.Widget_Material3_Chip_Filter))
                text = label
                isCheckable = true
                id = View.generateViewId()
            }
            chips.addView(chip)
            chip.isChecked = prefs.cacheLimitMb == mb
            chip.setOnClickListener {
                prefs.cacheLimitMb = mb
                Thread { LocalSpace.trimAll(act) }.start()
                lastCheck = 0
                refreshCard(root, force = true)
            }
        }
        root.findViewById<com.google.android.material.materialswitch.MaterialSwitch>(R.id.sw_cloud_previews).apply {
            isChecked = prefs.cloudPreviews
            setOnCheckedChangeListener { _, on -> prefs.cloudPreviews = on }
        }
        root.findViewById<MaterialButton>(R.id.space_see).setOnClickListener { showDownloads { refreshCard(root, true) } }
        root.findViewById<MaterialButton>(R.id.space_clear).setOnClickListener {
            MaterialAlertDialogBuilder(act)
                .setTitle("Clear downloaded files?")
                .setMessage("Removes the copies of cloud files you opened on this phone, and the photo previews. " +
                    "Nothing is removed from the cloud, and files open again whenever you need them.")
                .setPositiveButton("Clear") { _, _ ->
                    Thread {
                        LocalSpace.clearDownloads(act)
                        LocalSpace.clearPreviews(act)
                        main.post { refreshCard(root, true) }
                    }.start()
                }
                .setNegativeButton("Cancel", null)
                .show()
        }
    }

    /** Updates the numbers (at most every 5 minutes unless forced). */
    fun refreshCard(root: View, force: Boolean = false) {
        if (checking || (!force && System.currentTimeMillis() - lastCheck < 300_000)) return
        checking = true
        lastCheck = System.currentTimeMillis()
        Thread {
            val usage = LocalSpace.appUsage(act)
            main.post {
                checking = false
                root.findViewById<TextView>(R.id.space_app).text = SpeedMeter.size(usage.total)
                root.findViewById<TextView>(R.id.space_app_detail).text =
                    "Downloaded from the cloud ${SpeedMeter.size(usage.downloads)} (limit ${limitLabel()}) · previews ${SpeedMeter.size(usage.previews)}" +
                        (if (usage.pendingUploads > 0) " · waiting uploads ${SpeedMeter.size(usage.pendingUploads)}" else "") +
                        " · app data ${SpeedMeter.size(usage.other)}"
            }
        }.start()
    }

    private fun limitLabel() = if (prefs.cacheLimitMb >= 1024) "${prefs.cacheLimitMb / 1024} GB" else "${prefs.cacheLimitMb} MB"

    private fun showDownloads(after: () -> Unit) {
        Thread {
            val files = LocalSpace.downloads(act)
            main.post {
                val total = files.sumOf { it.length() }
                val text = if (files.isEmpty()) "Nothing downloaded right now."
                else "${num.format(files.size)} files · ${SpeedMeter.size(total)}\n\n" +
                    files.take(60).joinToString("\n") { "${SpeedMeter.size(it.length())}   ${it.name}" } +
                    (if (files.size > 60) "\n…and ${num.format(files.size - 60)} more" else "")
                MaterialAlertDialogBuilder(act)
                    .setTitle("Downloaded on this phone")
                    .setMessage(text)
                    .setPositiveButton("OK") { _, _ -> after() }
                    .setNeutralButton("Clear all") { _, _ ->
                        Thread {
                            LocalSpace.clearDownloads(act)
                            main.post(after)
                        }.start()
                    }
                    .show()
            }
        }.start()
    }

    private fun toast(t: String) = Toast.makeText(act, t, Toast.LENGTH_LONG).show()
}
