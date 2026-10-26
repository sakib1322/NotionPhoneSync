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

/** The Space card (Backup tab) and the "Free up space" steps. */
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
        root.findViewById<MaterialButton>(R.id.space_free_btn).setOnClickListener { freeUp(null) { refreshCard(root, true) } }
    }

    /** Updates the numbers (the phone-wide check runs at most every 5 minutes). */
    fun refreshCard(root: View, force: Boolean = false) {
        if (checking || (!force && System.currentTimeMillis() - lastCheck < 300_000)) return
        checking = true
        lastCheck = System.currentTimeMillis()
        Thread {
            val usage = LocalSpace.appUsage(act)
            val (offN, offB) = Db.get(act).offloadedTotals()
            val cands = try {
                if (Perms.hasStorage(act)) LocalSpace.candidates(act, LocalSpace.Filter(false, 0, null)) else emptyList()
            } catch (e: Exception) {
                emptyList()
            }
            main.post {
                checking = false
                root.findViewById<TextView>(R.id.space_app).text = SpeedMeter.size(usage.total)
                root.findViewById<TextView>(R.id.space_app_detail).text =
                    "Downloaded files ${SpeedMeter.size(usage.downloads)} (limit ${limitLabel()}) · previews ${SpeedMeter.size(usage.previews)}" +
                        (if (usage.pendingUploads > 0) " · waiting uploads ${SpeedMeter.size(usage.pendingUploads)}" else "") +
                        " · app data ${SpeedMeter.size(usage.other)}"
                root.findViewById<TextView>(R.id.space_free).text =
                    if (cands.isEmpty()) "Nothing to free right now."
                    else "${num.format(cands.size)} backed-up files on this phone use ${SpeedMeter.size(cands.sumOf { it.size })}. " +
                        "You can remove them here and keep them in the cloud."
                root.findViewById<TextView>(R.id.space_freed).text =
                    if (offN > 0) "Freed so far: ${num.format(offN)} files · ${SpeedMeter.size(offB)}" else ""
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

    // ---- free up space ---------------------------------------------------------------------

    /** Step by step: what to remove, how old, which folders, confirm, then remove (with checks). */
    fun freeUp(onlyFolder: String?, after: () -> Unit) {
        if (!Perms.hasStorage(act)) {
            toast("Allow access to all files in Settings first.")
            return
        }
        val kinds = arrayOf("All backed-up files", "Only photos and videos")
        var kind = 1
        MaterialAlertDialogBuilder(act)
            .setTitle(if (onlyFolder != null) "Free up space in “${onlyFolder.substringAfterLast('/')}”" else "Free up space")
            .setSingleChoiceItems(kinds, kind) { _, i -> kind = i }
            .setPositiveButton("Next") { _, _ -> pickAge(kind == 1, onlyFolder, after) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun pickAge(media: Boolean, onlyFolder: String?, after: () -> Unit) {
        val labels = arrayOf("Any age", "Older than 30 days", "Older than 90 days", "Older than 1 year")
        val days = intArrayOf(0, 30, 90, 365)
        var pick = 1
        MaterialAlertDialogBuilder(act)
            .setTitle("Which files?")
            .setSingleChoiceItems(labels, pick) { _, i -> pick = i }
            .setPositiveButton("Next") { _, _ -> findCandidates(LocalSpace.Filter(media, days[pick], null), onlyFolder, after) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun findCandidates(filter: LocalSpace.Filter, onlyFolder: String?, after: () -> Unit) {
        val wait = MaterialAlertDialogBuilder(act).setTitle("Looking for files…").setMessage("Checking which files are safely backed up.")
            .setCancelable(false).show()
        Thread {
            val all = try { LocalSpace.candidates(act, filter) } catch (e: Exception) { emptyList() }
            val cands = if (onlyFolder == null) all else all.filter { it.rel.startsWith("$onlyFolder/") }
            main.post {
                wait.dismiss()
                if (cands.isEmpty()) {
                    MaterialAlertDialogBuilder(act).setTitle("Nothing to remove")
                        .setMessage("No backed-up files match. Files only count once they are uploaded and unchanged since.")
                        .setPositiveButton("OK", null).show()
                    return@post
                }
                if (onlyFolder != null) confirm(cands, after) else pickFolders(cands, after)
            }
        }.start()
    }

    private fun pickFolders(cands: List<LocalSpace.Candidate>, after: () -> Unit) {
        val groups = LocalSpace.summary(cands).toList()
        val labels = groups.map { (name, v) -> "$name  ·  ${num.format(v.first)} files, ${SpeedMeter.size(v.second)}" }.toTypedArray()
        val checked = BooleanArray(groups.size) { true }
        MaterialAlertDialogBuilder(act)
            .setTitle("From these folders")
            .setMultiChoiceItems(labels, checked) { _, i, on -> checked[i] = on }
            .setPositiveButton("Next") { _, _ ->
                val chosen = groups.filterIndexed { i, _ -> checked[i] }.map { it.first }.toSet()
                val pick = cands.filter { it.rel.substringBefore('/').ifEmpty { "(top level)" } in chosen }
                if (pick.isEmpty()) toast("No folders chosen.") else confirm(pick, after)
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun confirm(cands: List<LocalSpace.Candidate>, after: () -> Unit) {
        val bytes = cands.sumOf { it.size }
        MaterialAlertDialogBuilder(act)
            .setTitle("Remove ${num.format(cands.size)} files from this phone?")
            .setMessage(
                "This frees ${SpeedMeter.size(bytes)}.\n\n" +
                    "• The files stay in your cloud and open from the Files tab.\n" +
                    "• Before removing each file, the app checks its cloud copy is there; any that can't be confirmed are kept.\n" +
                    "• They disappear from your Gallery and other apps on this phone."
            )
            .setPositiveButton("Remove") { _, _ -> run(cands, after) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun run(cands: List<LocalSpace.Candidate>, after: () -> Unit) {
        var stop = false
        val dialog = MaterialAlertDialogBuilder(act)
            .setTitle("Freeing up space…")
            .setMessage("Starting…")
            .setCancelable(false)
            .setNegativeButton("Stop") { _, _ -> stop = true }
            .show()
        Thread {
            val result = try {
                LocalSpace.offload(act, cands, { stop }) { i, n, what ->
                    main.post { dialog.setMessage("${num.format(i)} of ${num.format(n)}\n$what") }
                }
            } catch (e: Exception) {
                main.post { toast("Stopped: ${e.message ?: "no internet?"}") }
                null
            }
            main.post {
                dialog.dismiss()
                if (result != null) {
                    val (n, b) = result
                    val skipped = cands.size - n
                    MaterialAlertDialogBuilder(act)
                        .setTitle("Freed ${SpeedMeter.size(b)}")
                        .setMessage("Removed ${num.format(n)} files from this phone. They're still in your cloud." +
                            if (skipped > 0 && !stop) "\n\n${num.format(skipped)} were kept because their cloud copy couldn't be confirmed." else "")
                        .setPositiveButton("OK", null)
                        .show()
                }
                after()
            }
        }.start()
    }

    private fun toast(t: String) = Toast.makeText(act, t, Toast.LENGTH_LONG).show()
}
