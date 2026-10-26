package com.sakib.notionsync

import android.content.ActivityNotFoundException
import android.content.ContentValues
import android.content.Intent
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.text.Editable
import android.text.TextWatcher
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.button.MaterialButton
import com.google.android.material.color.MaterialColors
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.textfield.TextInputEditText
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * The Files tab: browse what's in Notion like a Drive folder, with previews,
 * and open, share or save any file.
 */
class FilesPage(
    private val act: AppCompatActivity,
    root: View,
    private val onExcludedChanged: () -> Unit
) {
    sealed class Item(val id: String, val name: String)
    class Folder(id: String, name: String) : Item(id, name)
    class FileItem(
        id: String, name: String, val date: String, val type: String,
        var url: String?, var fetchedAt: Long, val pageId: String
    ) : Item(id, name)

    private val prefs = Prefs(act)
    private val thumbs = Thumbs(act)
    private val main = Handler(Looper.getMainLooper())
    private val storageRoot = Environment.getExternalStorageDirectory()
    private val http = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    private val btnSource: MaterialButton = root.findViewById(R.id.btn_source)
    private val btnView: MaterialButton = root.findViewById(R.id.btn_view)
    private val searchBox: View = root.findViewById(R.id.search_box)
    private val etSearch: TextInputEditText = root.findViewById(R.id.et_search)
    private val crumbs: LinearLayout = root.findViewById(R.id.crumbs)
    private val loadingBar: LinearProgressIndicator = root.findViewById(R.id.files_loading)
    private val list: RecyclerView = root.findViewById(R.id.files_list)
    private val empty: TextView = root.findViewById(R.id.files_empty)

    private val stack = ArrayList<Pair<String, String>>() // folder id, title
    private val items = ArrayList<Item>()
    private var shown: List<Item> = emptyList()
    private var nextCursor: String? = null
    private var hasMore = false
    private var loading = false
    private var generation = 0
    private var query = ""
    private var error: String? = null
    private var blockPaths: Map<String, String> = emptyMap()
    private var discovered = false
    private val adapter = Adapter()

    /** Called when the folder changes (MainActivity uses it for the Back button). */
    var onStackChanged: (() -> Unit)? = null
    private val layout = GridLayoutManager(act, 3)

    init {
        list.layoutManager = layout
        list.adapter = adapter
        applyViewMode()
        list.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(rv: RecyclerView, dx: Int, dy: Int) {
                if (layout.findLastVisibleItemPosition() >= shown.size - 15) loadMore()
            }
        })
        btnView.setOnClickListener {
            prefs.filesGrid = !prefs.filesGrid
            applyViewMode()
        }
        root.findViewById<MaterialButton>(R.id.btn_search).setOnClickListener {
            val show = searchBox.visibility != View.VISIBLE
            searchBox.visibility = if (show) View.VISIBLE else View.GONE
            if (show) etSearch.requestFocus() else etSearch.setText("")
        }
        etSearch.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun onTextChanged(s: CharSequence?, a: Int, b: Int, c: Int) {}
            override fun afterTextChanged(s: Editable?) {
                query = s?.toString()?.trim() ?: ""
                applyFilter()
                if (query.isNotEmpty()) loadMore()
            }
        })
        btnSource.setOnClickListener { pickSource() }
        empty.setOnClickListener { if (error != null) reload() }
    }

    // ---- public -----------------------------------------------------------------------

    fun onShown() {
        if (prefs.token.isBlank() || prefs.rootPageId.isBlank()) {
            stack.clear()
            items.clear()
            applyFilter()
            showEmpty("Connect Notion in Settings first, then your backups appear here.")
            return
        }
        if (stack.isEmpty()) {
            Thread { blockPaths = Db.get(act).blockPaths() }.start()
            openSource(prefs.rootPageId, prefs.rootTitle.ifBlank { "Phone Backup" })
        }
        if (!discovered) discover()
    }

    fun canGoUp() = stack.size > 1

    fun goUp() {
        if (stack.size > 1) {
            stack.removeAt(stack.size - 1)
            reload()
        }
    }

    // ---- sources (Phone Backup, Laptop Backup…) ---------------------------------------------

    private fun sources(): List<Pair<String, String>> {
        val out = LinkedHashMap<String, String>()
        if (prefs.rootPageId.isNotBlank()) out[prefs.rootPageId] = prefs.rootTitle.ifBlank { "Phone Backup" }
        for (line in prefs.extraSources.lines()) {
            val id = line.substringBefore('|').trim()
            if (id.isNotEmpty() && id !in out) out[id] = line.substringAfter('|', "Backup").trim()
        }
        return out.toList()
    }

    private fun addSource(id: String, title: String) {
        if (sources().any { it.first == id }) return
        prefs.extraSources = (prefs.extraSources.lines().filter { it.isNotBlank() } + "$id|$title").joinToString("\n")
    }

    /** Finds other top-level pages the integration can see, e.g. Laptop Backup. */
    private fun discover() {
        discovered = true
        val token = prefs.token
        Thread {
            try {
                val found = NotionClient(token).topLevelPages()
                main.post { found.forEach { (id, t) -> addSource(id, t) } }
            } catch (e: Exception) {
                discovered = false
            }
        }.start()
    }

    private fun pickSource() {
        val src = sources()
        val labels = src.map { it.second } + "+ Add a page by link…"
        MaterialAlertDialogBuilder(act)
            .setTitle("Show backup")
            .setItems(labels.toTypedArray()) { _, i ->
                if (i < src.size) openSource(src[i].first, src[i].second) else addByLink()
            }
            .show()
    }

    private fun addByLink() {
        val input = EditText(act).apply {
            hint = "Paste a Notion page link"
            setSingleLine()
        }
        val box = FrameLayout(act).apply {
            val p = (20 * resources.displayMetrics.density).toInt()
            setPadding(p, p / 2, p, 0)
            addView(input)
        }
        MaterialAlertDialogBuilder(act)
            .setTitle("Add a backup page")
            .setMessage("For example your Laptop Backup page. It must be connected to your integration in Notion.")
            .setView(box)
            .setPositiveButton("Add") { _, _ ->
                val id = NotionClient.parsePageId(input.text.toString())
                if (id == null) {
                    toast("That doesn't look like a Notion page link.")
                    return@setPositiveButton
                }
                val token = prefs.token
                Thread {
                    try {
                        val t = NotionClient(token).pageTitle(id)
                        main.post {
                            addSource(id, t)
                            openSource(id, t)
                        }
                    } catch (e: Exception) {
                        main.post { toast("Can't open that page. Connect it to your integration in Notion first.") }
                    }
                }.start()
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun openSource(id: String, title: String) {
        stack.clear()
        stack.add(id to title)
        btnSource.text = title
        etSearch.setText("")
        reload()
    }

    private fun isPhoneSource() = stack.firstOrNull()?.first == prefs.rootPageId

    // ---- loading --------------------------------------------------------------------------

    private fun reload() {
        generation++
        items.clear()
        nextCursor = null
        hasMore = true
        loading = false
        error = null
        renderCrumbs()
        applyFilter()
        list.scrollToPosition(0)
        loadMore()
    }

    private fun loadMore() {
        if (loading || !hasMore || stack.isEmpty()) return
        loading = true
        loadingBar.visibility = View.VISIBLE
        val g = generation
        val pageId = stack.last().first
        val cursor = nextCursor
        val token = prefs.token
        Thread {
            try {
                val res = NotionClient(token).children(pageId, cursor)
                val parsed = res.results.mapNotNull { parse(it, pageId) }
                main.post {
                    if (g != generation) return@post
                    items.addAll(parsed)
                    nextCursor = res.next
                    hasMore = res.next != null
                    loading = false
                    loadingBar.visibility = if (hasMore && query.isNotEmpty()) View.VISIBLE else View.INVISIBLE
                    applyFilter()
                    // Keep going while searching, or while the screen isn't full yet.
                    if (hasMore && (query.isNotEmpty() || shown.size < 30)) loadMore()
                }
            } catch (e: Exception) {
                main.post {
                    if (g != generation) return@post
                    loading = false
                    loadingBar.visibility = View.INVISIBLE
                    error = when {
                        e is NotionException && e.status == 404 -> "Can't open this page. Is it connected to your integration?"
                        e.message?.contains("resolve host") == true -> "No internet connection."
                        else -> "Couldn't load: ${e.message ?: "error"}"
                    }
                    applyFilter()
                }
            }
        }.start()
    }

    private fun parse(b: JSONObject, pageId: String): Item? {
        val id = b.optString("id")
        return when (val type = b.optString("type")) {
            "child_page" -> Folder(id, b.getJSONObject("child_page").optString("title").ifBlank { "Untitled" })
            "image", "video", "audio", "pdf", "file" -> {
                val inner = b.optJSONObject(type) ?: return null
                val capArr = inner.optJSONArray("caption")
                val caption = if (capArr == null) "" else
                    (0 until capArr.length()).joinToString("") { capArr.getJSONObject(it).optString("plain_text") }
                val url = inner.optJSONObject("file")?.optString("url")?.takeIf { it.isNotEmpty() }
                    ?: inner.optJSONObject("external")?.optString("url")?.takeIf { it.isNotEmpty() }
                val name = caption.substringBefore(" · ").trim()
                    .ifEmpty { inner.optString("name") }
                    .ifEmpty { url?.substringBefore('?')?.substringAfterLast('/') ?: "File" }
                val date = if (caption.contains(" · ")) caption.substringAfter(" · ").trim() else ""
                FileItem(id, name, date, type, url, System.currentTimeMillis(), pageId)
            }
            else -> null
        }
    }

    private fun applyFilter() {
        val q = query.lowercase()
        val filtered = if (q.isEmpty()) items else items.filter { it.name.lowercase().contains(q) }
        shown = filtered.filterIsInstance<Folder>() + filtered.filterIsInstance<FileItem>()
        adapter.notifyDataSetChanged()
        when {
            error != null && shown.isEmpty() -> showEmpty("$error\n\nTap to try again.")
            shown.isEmpty() && !hasMore && q.isNotEmpty() -> showEmpty("Nothing called “$query” in this folder.")
            shown.isEmpty() && !hasMore && stack.isNotEmpty() -> showEmpty("This folder is empty.")
            else -> empty.visibility = View.GONE
        }
    }

    private fun showEmpty(text: String) {
        empty.text = text
        empty.visibility = View.VISIBLE
    }

    private fun renderCrumbs() {
        crumbs.removeAllViews()
        val primary = MaterialColors.getColor(crumbs, androidx.appcompat.R.attr.colorPrimary)
        val muted = MaterialColors.getColor(crumbs, com.google.android.material.R.attr.colorOnSurfaceVariant)
        stack.forEachIndexed { i, (_, title) ->
            if (i > 0) crumbs.addView(TextView(act).apply {
                text = "  ›  "
                setTextColor(muted)
            })
            crumbs.addView(TextView(act).apply {
                text = title
                textSize = 14f
                setTextColor(if (i == stack.size - 1) MaterialColors.getColor(crumbs, com.google.android.material.R.attr.colorOnSurface) else primary)
                if (i == stack.size - 1) setTypeface(typeface, android.graphics.Typeface.BOLD)
                setOnClickListener {
                    while (stack.size > i + 1) stack.removeAt(stack.size - 1)
                    reload()
                }
            })
        }
        onStackChanged?.invoke()
        (crumbs.parent as View).post { (crumbs.parent as android.widget.HorizontalScrollView).fullScroll(View.FOCUS_RIGHT) }
    }

    private fun applyViewMode() {
        layout.spanCount = if (prefs.filesGrid) 3 else 1
        btnView.setIconResource(if (prefs.filesGrid) R.drawable.ic_view_list else R.drawable.ic_grid)
        adapter.notifyDataSetChanged()
    }

    // ---- file actions ---------------------------------------------------------------------

    private fun kindIcon(it: Item): Int = when {
        it is Folder -> R.drawable.ic_folder
        (it as FileItem).type == "image" -> R.drawable.ic_image
        it.type == "video" -> R.drawable.ic_video
        it.type == "audio" -> R.drawable.ic_audio
        else -> R.drawable.ic_file
    }

    private fun kindLabel(f: FileItem): String {
        val ext = f.name.substringAfterLast('.', "").uppercase()
        return when (f.type) {
            "image" -> "Photo"
            "video" -> "Video"
            "audio" -> "Audio"
            "pdf" -> "PDF"
            else -> if (ext.isNotEmpty() && ext.length <= 5) "$ext file" else "File"
        }
    }

    private fun localFile(f: FileItem): File? {
        if (!isPhoneSource()) return null
        val rel = blockPaths[f.id] ?: return null
        return File(storageRoot, rel).takeIf { it.exists() }
    }

    private fun actions(f: FileItem) {
        MaterialAlertDialogBuilder(act)
            .setTitle(f.name)
            .setItems(arrayOf("Open", "Share", "Save to phone", "Open in Notion")) { _, i ->
                when (i) {
                    0 -> obtain(f) { open(f, it) }
                    1 -> obtain(f) { share(f, it) }
                    2 -> obtain(f) { saveToPhone(f, it) }
                    3 -> openInNotion(f)
                }
            }
            .show()
    }

    /** Gets the file: the phone copy if it's still there, otherwise downloads it from Notion. */
    private fun obtain(f: FileItem, then: (File) -> Unit) {
        localFile(f)?.let {
            then(it)
            return
        }
        val safe = f.name.replace(Regex("[\\\\/:*?\"<>|]"), "_")
        val dest = File(act.cacheDir, "open/${f.id}/$safe")
        if (dest.exists() && dest.length() > 0) {
            then(dest)
            return
        }
        var call: Call? = null
        var cancelled = false
        val dialog = MaterialAlertDialogBuilder(act)
            .setTitle("Getting file from Notion")
            .setMessage("Starting…")
            .setNegativeButton("Cancel") { _, _ ->
                cancelled = true
                call?.cancel()
            }
            .setCancelable(false)
            .show()
        val token = prefs.token
        Thread {
            try {
                var url = f.url
                // Notion's file links expire after an hour: get a fresh one if needed.
                if (url == null || System.currentTimeMillis() - f.fetchedAt > 50 * 60_000) {
                    val fresh = parse(NotionClient(token).block(f.id), f.pageId) as? FileItem
                    url = fresh?.url
                    f.url = url
                    f.fetchedAt = System.currentTimeMillis()
                }
                if (url == null) throw IllegalStateException("No download link")
                dest.parentFile?.mkdirs()
                val part = File(dest.path + ".part")
                val c = http.newCall(Request.Builder().url(url).build())
                call = c
                c.execute().use { r ->
                    if (!r.isSuccessful) throw IllegalStateException("Download failed (${r.code})")
                    val body = r.body ?: throw IllegalStateException("Empty download")
                    val total = body.contentLength()
                    body.byteStream().use { input ->
                        part.outputStream().use { out ->
                            val buf = ByteArray(64 * 1024)
                            var done = 0L
                            var lastPct = -1
                            while (true) {
                                val n = input.read(buf)
                                if (n < 0) break
                                out.write(buf, 0, n)
                                done += n
                                val pct = if (total > 0) (done * 100 / total).toInt() else -1
                                if (pct != lastPct) {
                                    lastPct = pct
                                    val msg = if (pct >= 0) "$pct% of ${SpeedMeter.size(total)}" else SpeedMeter.size(done)
                                    main.post { dialog.setMessage(msg) }
                                }
                            }
                        }
                    }
                }
                part.renameTo(dest)
                main.post {
                    dialog.dismiss()
                    then(dest)
                }
            } catch (e: Exception) {
                main.post {
                    dialog.dismiss()
                    if (!cancelled) toast("Couldn't get the file: ${e.message ?: "no internet?"}")
                }
            }
        }.start()
    }

    private fun uriFor(file: File): Uri = FileProvider.getUriForFile(act, "${act.packageName}.files", file)

    private fun open(f: FileItem, file: File) {
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(uriFor(file), FileTypes.mime(f.name))
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        try {
            act.startActivity(Intent.createChooser(intent, "Open with"))
        } catch (e: ActivityNotFoundException) {
            toast("No app on this phone can open this file type.")
        }
    }

    private fun share(f: FileItem, file: File) {
        val intent = Intent(Intent.ACTION_SEND)
            .setType(FileTypes.mime(f.name))
            .putExtra(Intent.EXTRA_STREAM, uriFor(file))
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        act.startActivity(Intent.createChooser(intent, "Share"))
    }

    private fun saveToPhone(f: FileItem, file: File) {
        // Keep restored files out of the backup so they aren't uploaded again.
        if (prefs.excludedList().none { it.equals(RESTORE_DIR, true) }) {
            prefs.excluded = (prefs.excluded.trimEnd() + "\n" + RESTORE_DIR).trim()
            onExcludedChanged()
        }
        Thread {
            val ok = try {
                if (Build.VERSION.SDK_INT >= 29) {
                    val values = ContentValues().apply {
                        put(MediaStore.Downloads.DISPLAY_NAME, f.name)
                        put(MediaStore.Downloads.MIME_TYPE, FileTypes.mime(f.name))
                        put(MediaStore.Downloads.RELATIVE_PATH, RESTORE_DIR)
                    }
                    val uri = act.contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                        ?: throw IllegalStateException("No space")
                    act.contentResolver.openOutputStream(uri)?.use { out -> file.inputStream().use { it.copyTo(out) } }
                } else {
                    val dir = File(storageRoot, RESTORE_DIR).apply { mkdirs() }
                    file.copyTo(File(dir, f.name), overwrite = true)
                }
                true
            } catch (e: Exception) {
                false
            }
            main.post {
                toast(if (ok) "Saved to $RESTORE_DIR" else "Couldn't save here. Use Share instead.")
            }
        }.start()
    }

    private fun openInNotion(f: FileItem) {
        val url = "https://www.notion.so/${f.pageId.replace("-", "")}#${f.id.replace("-", "")}"
        try {
            act.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (e: ActivityNotFoundException) {
            toast("No browser found.")
        }
    }

    private fun toast(t: String) = Toast.makeText(act, t, Toast.LENGTH_LONG).show()

    // ---- list ---------------------------------------------------------------------------

    private class Holder(v: View) : RecyclerView.ViewHolder(v) {
        val thumbBox: FrameLayout = v.findViewById(R.id.thumb_box)
        val thumb: ImageView = v.findViewById(R.id.thumb)
        val badge: ImageView = v.findViewById(R.id.badge)
        val name: TextView = v.findViewById(R.id.name)
        val meta: TextView = v.findViewById(R.id.meta)
    }

    private inner class Adapter : RecyclerView.Adapter<Holder>() {
        override fun getItemCount() = shown.size
        override fun getItemViewType(position: Int) = if (prefs.filesGrid) 1 else 0

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val inf = LayoutInflater.from(parent.context)
            val v = inf.inflate(if (viewType == 1) R.layout.item_grid else R.layout.item_row, parent, false)
            val h = Holder(v)
            if (viewType == 1) {
                val d = parent.resources.displayMetrics.density
                val w = (if (parent.width > 0) parent.width else parent.resources.displayMetrics.widthPixels) -
                    parent.paddingLeft - parent.paddingRight
                h.thumbBox.layoutParams.height = w / 3 - (10 * d).toInt()
            }
            h.badge.imageTintList = ColorStateList.valueOf(0xFFFFFFFF.toInt())
            return h
        }

        override fun onBindViewHolder(h: Holder, position: Int) {
            val it = shown[position]
            val d = h.itemView.resources.displayMetrics.density
            val grid = prefs.filesGrid
            h.name.text = it.name
            h.thumb.tag = null
            h.thumb.setImageResource(kindIcon(it))
            h.thumb.scaleType = ImageView.ScaleType.CENTER_INSIDE
            val pad = ((if (grid) 30 else 12) * d).toInt()
            h.thumb.setPadding(pad, pad, pad, pad)
            h.thumb.imageTintList = ColorStateList.valueOf(
                if (it is Folder) MaterialColors.getColor(h.thumb, androidx.appcompat.R.attr.colorPrimary)
                else MaterialColors.getColor(h.thumb, com.google.android.material.R.attr.colorOnSurfaceVariant)
            )
            h.badge.visibility = View.GONE
            when (it) {
                is Folder -> {
                    h.meta.text = "Folder"
                    h.itemView.setOnClickListener { _ ->
                        stack.add(it.id to it.name)
                        etSearch.setText("")
                        reload()
                    }
                    h.itemView.setOnLongClickListener(null)
                }
                is FileItem -> {
                    h.meta.text = if (grid) it.date.substringBefore(',').ifEmpty { kindLabel(it) }
                    else listOf(kindLabel(it), it.date).filter { s -> s.isNotEmpty() }.joinToString(" · ")
                    if (it.type == "image" || it.type == "video") {
                        val allowDownload = !(prefs.wifiOnly && Perms.onMeteredNetwork(act))
                        thumbs.load(it.id, localFile(it), it.url, it.type == "video", allowDownload, h.thumb)
                        if (it.type == "video") h.badge.visibility = View.VISIBLE
                    }
                    h.itemView.setOnClickListener { _ -> obtain(it) { file -> open(it, file) } }
                    h.itemView.setOnLongClickListener { _ ->
                        actions(it)
                        true
                    }
                }
            }
        }
    }

    companion object {
        const val RESTORE_DIR = "Download/Notion Restore"
    }
}
