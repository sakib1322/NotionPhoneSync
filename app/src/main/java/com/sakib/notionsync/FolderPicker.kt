package com.sakib.notionsync

import android.app.Activity
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.Toast
import com.google.android.material.dialog.MaterialAlertDialogBuilder

/** The backup pages the app knows about: your phone backup plus others (e.g. Laptop Backup). */
object Sources {
    fun list(prefs: Prefs): List<Pair<String, String>> {
        val out = LinkedHashMap<String, String>()
        if (prefs.rootPageId.isNotBlank()) out[prefs.rootPageId] = prefs.rootTitle.ifBlank { "Phone Backup" }
        for (line in prefs.extraSources.lines()) {
            val id = line.substringBefore('|').trim()
            if (id.isNotEmpty() && id !in out) out[id] = line.substringAfter('|', "Backup").trim()
        }
        return out.toList()
    }

    fun title(prefs: Prefs, root: String) = list(prefs).firstOrNull { it.first == root }?.second ?: "Backup"
}

/**
 * A dialog for choosing a cloud folder: tap folders to go in, "Up" to go back,
 * and the main button to choose the folder you're in.
 */
class FolderPicker(
    private val act: Activity,
    private val title: String,
    private val action: String,
    private val onPick: (root: String, path: String) -> Unit,
    private val onCancel: () -> Unit = {}
) {
    private val prefs = Prefs(act)
    private val db = Db.get(act)

    fun show(root: String, path: String) {
        val folders = db.subFolders(root, path)
        val labels = ArrayList<String>()
        labels.add("+ New folder here")
        if (Sources.list(prefs).count { db.indexDbId(it.first) != null } > 1) labels.add("⇄ Another backup…")
        val first = labels.size
        folders.forEach { labels.add("📁  ${it.name}") }
        val where = Sources.title(prefs, root) + if (path.isEmpty()) "" else " › " + path.replace("/", " › ")
        val b = MaterialAlertDialogBuilder(act)
            .setTitle("$title\n$where")
            .setItems(labels.toTypedArray()) { _, i ->
                when {
                    i >= first -> {
                        val name = folders[i - first].name
                        show(root, if (path.isEmpty()) name else "$path/$name")
                    }
                    labels[i].startsWith("+") -> newFolder(root, path)
                    else -> pickSource(path)
                }
            }
            .setPositiveButton(action) { _, _ -> onPick(root, path) }
            .setOnCancelListener { onCancel() }
        if (path.isNotEmpty()) b.setNeutralButton("Up") { _, _ -> show(root, path.substringBeforeLast('/', "")) }
        else b.setNegativeButton("Cancel") { _, _ -> onCancel() }
        b.show()
    }

    private fun pickSource(path: String) {
        val src = Sources.list(prefs).filter { db.indexDbId(it.first) != null }
        MaterialAlertDialogBuilder(act)
            .setTitle("Choose a backup")
            .setItems(src.map { it.second }.toTypedArray()) { _, i -> show(src[i].first, "") }
            .setOnCancelListener { show(src.firstOrNull()?.first ?: prefs.rootPageId, path) }
            .show()
    }

    private fun newFolder(root: String, path: String) {
        val input = EditText(act).apply {
            hint = "Folder name"
            setSingleLine()
        }
        val box = FrameLayout(act).apply {
            val p = (20 * resources.displayMetrics.density).toInt()
            setPadding(p, p / 2, p, 0)
            addView(input)
        }
        MaterialAlertDialogBuilder(act)
            .setTitle("New folder")
            .setView(box)
            .setPositiveButton("Create") { _, _ ->
                val name = input.text.toString().trim().replace("/", "-")
                if (name.isEmpty()) {
                    show(root, path)
                    return@setPositiveButton
                }
                Thread {
                    val ok = try {
                        CloudOps(act).newFolder(root, path, name)
                        true
                    } catch (e: Exception) {
                        false
                    }
                    act.runOnUiThread {
                        if (!ok) Toast.makeText(act, "Couldn't create the folder. Check your internet.", Toast.LENGTH_LONG).show()
                        show(root, if (ok) (if (path.isEmpty()) name else "$path/$name") else path)
                    }
                }.start()
            }
            .setNegativeButton("Cancel") { _, _ -> show(root, path) }
            .show()
    }
}
