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
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
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
import java.text.NumberFormat
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * The Files tab: your cloud drive. Browses the file index (instant, works
 * offline once loaded) and falls back to reading Notion pages directly for
 * backups that have no index yet.
 */
class FilesPage(
    private val act: AppCompatActivity,
    root: View,
    private val onExcludedChanged: () -> Unit
) {
    sealed class Item(val key: String, val name: String)
    class Folder(key: String, name: String, val files: Int = -1, val bytes: Long = 0) : Item(key, name)
    class Header(label: String) : Item("header:$label", label)
    class FileItem(
        val block: String, name: String, val mtime: Long, val dateText: String, val type: String,
        var url: String?, var fetchedAt: Long, val pageId: String, val size: Long,
        val localPath: String?, val where: String?, val root: String?, val folder: String?,
        val row: Db.IdxRow? = null
    ) : Item(block, name)

    private val prefs = Prefs(act)
    private val db = Db.get(act)
    private val thumbs = Thumbs(act)
    private val main = Handler(Looper.getMainLooper())
    private val storageRoot = Environment.getExternalStorageDirectory()
    private val num: NumberFormat = NumberFormat.getIntegerInstance()
    private val dateFmt = SimpleDateFormat("d MMM yyyy", Locale.US)
    private val http = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    private val btnSource: MaterialButton = root.findViewById(R.id.btn_source)
    private val btnView: MaterialButton = root.findViewById(R.id.btn_view)
    private val searchBox: View = root.findViewById(R.id.search_box)
    private val etSearch: TextInputEditText = root.findViewById(R.id.et_search)
    private val crumbs: LinearLayout = root.findViewById(R.id.crumbs)
    private val summary: TextView = root.findViewById(R.id.files_summary)
    private val banner: TextView = root.findViewById(R.id.files_banner)
    private val loadingBar: LinearProgressIndicator = root.findViewById(R.id.files_loading)
    private val refresher: SwipeRefreshLayout = root.findViewById(R.id.files_refresh)
    private val list: RecyclerView = root.findViewById(R.id.files_list)
    private val empty: TextView = root.findViewById(R.id.files_empty)

    // Where we are
    private var sourceId = ""
    private var sourceTitle = ""
    private var useIndex = false
    private var path = ""                                         // index mode
    private val stack = ArrayList<Pair<String, String>>()        // Notion mode: page id, title

    // What's shown
    private val items = ArrayList<Item>()
    private var shown: List<Item> = emptyList()
    private var query = ""
    private var error: String? = null

    // Notion-mode paging
    private var nextCursor: String? = null
    private var hasMore = false
    private var loading = false
    private var generation = 0

    // Index refresh
    private var refreshing = false
    private var discovered = false
    private var lastAutoRefresh = 0L

    private val adapter = Adapter()
    private val layout = GridLayoutManager(act, 3)

    /** Called when the folder changes (MainActivity uses it for the Back button). */
    var onStackChanged: (() -> Unit)? = null

    /** Opens the phone's file picker (set by MainActivity). */
    var pickFiles: (() -> Unit)? = null

    private val fab: View = root.findViewById(R.id.fab_new)
    private val modeToggle: com.google.android.material.button.MaterialButtonToggleGroup = root.findViewById(R.id.mode_toggle)
    private var photosMode = false
    private val monthFmt = SimpleDateFormat("MMMM yyyy", Locale.US)
    private var lastQueued = -1

    init {
        list.layoutManager = layout
        list.adapter = adapter
        applyViewMode()
        list.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(rv: RecyclerView, dx: Int, dy: Int) {
                if (!useIndex && layout.findLastVisibleItemPosition() >= shown.size - 15) loadMore()
            }
        })
        btnView.setOnClickListener {
            prefs.filesGrid = !prefs.filesGrid
            applyViewMode()
        }
        root.findViewById<MaterialButton>(R.id.btn_sort).setOnClickListener { pickSort() }
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
                if (useIndex) showIndex() else {
                    applyFilter()
                    if (query.isNotEmpty()) loadMore()
                }
            }
        })
        btnSource.setOnClickListener { pickSource() }
        summary.setOnClickListener { showStorage() }
        empty.setOnClickListener { if (error != null) reload() }
        refresher.setOnRefreshListener { refreshIndex(full = true) }
        layout.spanSizeLookup = object : GridLayoutManager.SpanSizeLookup() {
            override fun getSpanSize(position: Int) =
                if (shown.getOrNull(position) is Header) layout.spanCount else 1
        }
        modeToggle.addOnButtonCheckedListener { _, id, checked ->
            if (!checked) return@addOnButtonCheckedListener
            photosMode = id == R.id.mode_photos
            applyViewMode()
            if (useIndex) showIndex()
        }
        fab.setOnClickListener {
            MaterialAlertDialogBuilder(act)
                .setItems(arrayOf("Upload files here", "New folder here")) { _, i ->
                    if (i == 0) pickFiles?.invoke() else newFolderHere()
                }
                .show()
        }
    }

    /** Called every couple of seconds while the Files tab is open. */
    fun tick() {
        val q = db.countQueuedUploads()
        if (q != lastQueued) {
            lastQueued = q
            if (useIndex) showIndex() else updateBanner()
        }
    }

    /** Files chosen in the phone's picker: copy and upload them to the folder being shown. */
    fun onPicked(uris: List<android.net.Uri>) {
        if (uris.isEmpty() || !useIndex) return
        val root = sourceId
        val dest = path
        val dialog = MaterialAlertDialogBuilder(act)
            .setTitle("Preparing ${uris.size} file(s)…")
            .setMessage("Copying into the app")
            .setCancelable(false)
            .show()
        Thread {
            val n = UploadWorker.queue(act, uris, root, dest) { done ->
                main.post { dialog.setMessage("Copying $done of ${uris.size}") }
            }
            main.post {
                dialog.dismiss()
                toast(if (n > 0) "Uploading $n file(s)…" else "Couldn't read those files.")
                tick()
            }
        }.start()
    }

    private fun ask(title: String, initial: String, ok: String, then: (String) -> Unit) {
        val input = EditText(act).apply {
            setText(initial)
            setSingleLine()
            setSelection(0, initial.substringBeforeLast('.', initial).length)
        }
        val box = FrameLayout(act).apply {
            val p = (20 * resources.displayMetrics.density).toInt()
            setPadding(p, p / 2, p, 0)
            addView(input)
        }
        MaterialAlertDialogBuilder(act)
            .setTitle(title)
            .setView(box)
            .setPositiveButton(ok) { _, _ ->
                val v = input.text.toString().trim().replace("/", "-")
                if (v.isNotEmpty()) then(v)
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** Runs a cloud change in the background, then refreshes the list. */
    private fun runOp(label: String, op: (CloudOps, (Int, Int) -> Unit) -> Unit) {
        val dialog = MaterialAlertDialogBuilder(act).setTitle(label).setMessage("Working…").setCancelable(false).show()
        Thread {
            val err = try {
                op(CloudOps(act)) { i, n -> if (n > 1) main.post { dialog.setMessage("${i + 1} of $n") } }
                null
            } catch (e: Exception) {
                friendly(e.message)
            }
            main.post {
                dialog.dismiss()
                if (err != null) toast("Couldn't finish: $err")
                showIndex()
            }
        }.start()
    }

    private fun newFolderHere() {
        ask("New folder", "", "Create") { name ->
            val root = sourceId
            val p = path
            runOp("Creating folder") { ops, _ -> ops.newFolder(root, p, name) }
        }
    }

    private fun folderActions(f: Folder) {
        val root = sourceId
        MaterialAlertDialogBuilder(act)
            .setTitle(f.name)
            .setItems(
                if (root == prefs.rootPageId) arrayOf("Rename folder", "Delete folder", "Free up space on the phone")
                else arrayOf("Rename folder", "Delete folder")
            ) { _, i ->
                if (i == 2) {
                    SpaceTools(act).freeUp(onlyFolder = f.key) { showIndex() }
                    return@setItems
                }
                if (i == 0) ask("Rename folder", f.name, "Rename") { name ->
                    runOp("Renaming") { ops, prog -> ops.renameFolder(root, f.key, name, prog) }
                } else MaterialAlertDialogBuilder(act)
                    .setTitle("Delete “${f.name}”?")
                    .setMessage("${if (f.files >= 0) num.format(f.files) + " files" else "Everything"} in this folder will be moved to Notion's trash " +
                        "(recoverable there for 30 days). Files on your phone and laptop are not touched.")
                    .setPositiveButton("Delete") { _, _ -> runOp("Deleting") { ops, prog -> ops.deleteFolder(root, f.key, prog) } }
                    .setNegativeButton("Cancel", null)
                    .show()
            }
            .show()
    }

    // ---- public -----------------------------------------------------------------------

    fun onShown() {
        if (prefs.token.isBlank() || prefs.rootPageId.isBlank()) {
            items.clear()
            applyFilter()
            showEmpty("Connect Notion in Settings first, then your files appear here.")
            return
        }
        if (sourceId.isEmpty()) openSource(prefs.rootPageId, prefs.rootTitle.ifBlank { "Phone Backup" })
        else if (useIndex) showIndex()
        if (!discovered) discover()
        // Quietly pick up new files (at most once a minute).
        if (System.currentTimeMillis() - lastAutoRefresh > 60_000) refreshIndex(full = false)
    }

    fun canGoUp() = if (useIndex) (!photosMode && path.isNotEmpty()) || query.isNotEmpty() else stack.size > 1

    fun goUp() {
        if (useIndex) {
            if (query.isNotEmpty()) {
                etSearch.setText("")
                return
            }
            path = path.substringBeforeLast('/', "")
            showIndex()
        } else if (stack.size > 1) {
            stack.removeAt(stack.size - 1)
            reload()
        }
    }

    // ---- sources --------------------------------------------------------------------------

    private fun sources(): List<Pair<String, String>> {
        val out = LinkedHashMap<String, String>()
        if (prefs.rootPageId.isNotBlank()) out[prefs.rootPageId] = prefs.rootTitle.ifBlank { "Phone Backup" }
        for (line in prefs.extraSources.lines()) {
            val id = line.substringBefore('|').trim()
            if (id.isNotEmpty() && id !in out) out[id] = line.substringAfter('|', "Backup").trim()
        }
        return out.toList()
    }

    private fun titleOf(root: String) = sources().firstOrNull { it.first == root }?.second ?: "Backup"

    private fun addSource(id: String, title: String) {
        if (sources().any { it.first == id }) return
        prefs.extraSources = (prefs.extraSources.lines().filter { it.isNotBlank() } + "$id|$title").joinToString("\n")
    }

    private fun discover() {
        discovered = true
        val token = prefs.token
        Thread {
            try {
                val found = NotionClient(token).topLevelPages()
                main.post {
                    val before = sources().size
                    found.forEach { (id, t) -> addSource(id, t) }
                    if (sources().size > before) refreshIndex(full = false)
                }
            } catch (e: Exception) {
                discovered = false
            }
        }.start()
    }

    private fun pickSource() {
        val src = sources()
        val labels = src.map { (id, t) ->
            val (n, b) = db.storageTotal(id)
            if (n > 0) "$t  ·  ${num.format(n)} files, ${SpeedMeter.size(b)}" else t
        } + "+ Add a page by link…" + "Browse Notion pages directly"
        MaterialAlertDialogBuilder(act)
            .setTitle("Show")
            .setItems(labels.toTypedArray()) { _, i ->
                when {
                    i < src.size -> openSource(src[i].first, src[i].second)
                    i == src.size -> addByLink()
                    else -> {
                        useIndex = false
                        stack.clear()
                        stack.add(sourceId to sourceTitle)
                        reload()
                    }
                }
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
                            refreshIndex(full = false)
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
        sourceId = id
        sourceTitle = title
        btnSource.text = title
        path = ""
        etSearch.setText("")
        useIndex = db.indexDbId(id) != null && db.idxCount(id) > 0
        if (useIndex) showIndex() else {
            stack.clear()
            stack.add(id to title)
            reload()
        }
    }

    // ---- the index ---------------------------------------------------------------------------

    /** Brings the local copy of every backup's index up to date. */
    private fun refreshIndex(full: Boolean) {
        if (refreshing) {
            refresher.isRefreshing = false
            return
        }
        refreshing = true
        lastAutoRefresh = System.currentTimeMillis()
        val token = prefs.token
        val all = sources()
        Thread {
            var anyError: String? = null
            for ((root, title) in all) {
                try {
                    val idx = CloudIndex(act, NotionClient(token))
                    val dbId = idx.dbIdFor(root, create = false) ?: continue
                    val (_, lastFull) = db.pullTimes(root)
                    // A full refresh catches files removed elsewhere; do one at least daily.
                    val doFull = full || lastFull == 0L || System.currentTimeMillis() - lastFull > 24 * 3600_000L
                    idx.pull(root, dbId, doFull) { n ->
                        if (doFull) main.post { setBanner("Loading the file list for $title… ${num.format(n)} files") }
                    }
                } catch (e: Exception) {
                    anyError = e.message
                }
            }
            main.post {
                refreshing = false
                refresher.isRefreshing = false
                if (anyError != null && full) toast("Couldn't refresh everything: ${friendly(anyError)}")
                val nowIndexed = db.indexDbId(sourceId) != null && db.idxCount(sourceId) > 0
                if (nowIndexed && !useIndex && stack.size <= 1) {
                    useIndex = true
                    path = ""
                }
                if (useIndex) showIndex() else updateBanner()
            }
        }.start()
    }

    private fun setBanner(text: String?) {
        banner.text = text ?: ""
        banner.visibility = if (text.isNullOrEmpty()) View.GONE else View.VISIBLE
    }

    private fun updateBanner() {
        val queued = db.countQueuedUploads()
        if (queued > 0) {
            setBanner("Uploading ${num.format(queued)} file(s) you added… They appear here when done.")
            return
        }
        when {
            refreshing && !useIndex -> setBanner("Loading the file list…")
            !useIndex && db.indexDbId(sourceId) == null ->
                setBanner(
                    if (sourceId == prefs.rootPageId) "Search and folder sizes appear after the next sync builds the file index."
                    else "This backup has no file index yet. Update the app that uploads to it. Showing Notion pages directly."
                )
            sourceId == prefs.rootPageId -> {
                val pending = db.countPendingIndex()
                setBanner(
                    if (pending > 0) "Adding earlier uploads to the file index: ${num.format(pending)} left. " +
                        "They appear here as each sync adds them." else null
                )
            }
            else -> setBanner(null)
        }
    }

    private fun sortSql() = when (prefs.filesSort) {
        "old" -> "mtime ASC"
        "name" -> "name COLLATE NOCASE ASC"
        "size" -> "size DESC"
        else -> "mtime DESC"
    }

    private fun fileFrom(r: Db.IdxRow, withWhere: Boolean): FileItem {
        val local = if (r.device == "Phone" && r.root == prefs.rootPageId && r.source.isNotEmpty()) r.source else null
        val type = when (r.kind) {
            "Photo" -> "image"
            "Video" -> "video"
            "Audio" -> "audio"
            "PDF" -> "pdf"
            else -> "file"
        }
        val where = if (withWhere) titleOf(r.root) + (if (r.path.isNotEmpty()) " › " + r.path.replace("/", " › ") else "") else null
        return FileItem(
            r.block, r.name, r.mtime, if (r.mtime > 0) dateFmt.format(Date(r.mtime)) else "", type,
            null, 0, r.page, r.size, local, where, r.root, r.path, r
        )
    }

    /** Shows the current folder (or search results) from the local index. */
    private fun showIndex() {
        error = null
        val q = query
        val result = ArrayList<Item>()
        if (q.isNotEmpty()) {
            val roots = sources().map { it.first }.filter { db.indexDbId(it) != null }
            db.search(roots, q).forEach { result.add(fileFrom(it, withWhere = true)) }
        } else if (photosMode) {
            var month = ""
            for (r in db.media(sourceId)) {
                val m = if (r.mtime > 0) monthFmt.format(Date(r.mtime)) else "Unknown date"
                if (m != month) {
                    month = m
                    result.add(Header(m))
                }
                result.add(fileFrom(r, withWhere = false))
            }
        } else {
            val folders = db.subFolders(sourceId, path)
            val sortedFolders = when (prefs.filesSort) {
                "size" -> folders.sortedByDescending { it.bytes }
                else -> folders
            }
            sortedFolders.forEach {
                result.add(Folder(if (path.isEmpty()) it.name else "$path/${it.name}", it.name, it.files, it.bytes))
            }
            db.filesIn(sourceId, path, sortSql()).forEach { result.add(fileFrom(it, withWhere = false)) }
        }
        items.clear()
        items.addAll(result)
        shown = result
        adapter.notifyDataSetChanged()
        renderCrumbs()
        val (n, b) = db.storageTotal(sourceId)
        summary.text = "${num.format(n)} files · ${SpeedMeter.size(b)} in $sourceTitle  ·  Storage ›"
        summary.visibility = View.VISIBLE
        modeToggle.visibility = View.VISIBLE
        fab.visibility = if (q.isEmpty() && !photosMode) View.VISIBLE else View.GONE
        updateBanner()
        when {
            q.isNotEmpty() && result.isEmpty() -> showEmpty("Nothing called “$q” in your backups.")
            photosMode && result.isEmpty() -> showEmpty("No photos or videos in this backup's index yet.")
            result.isEmpty() -> showEmpty("This folder is empty.")
            else -> empty.visibility = View.GONE
        }
    }

    private fun pickSort() {
        val keys = listOf("new", "old", "name", "size")
        val labels = arrayOf("Newest first", "Oldest first", "Name (A–Z)", "Largest first")
        MaterialAlertDialogBuilder(act)
            .setTitle("Sort by")
            .setSingleChoiceItems(labels, keys.indexOf(prefs.filesSort).coerceAtLeast(0)) { d, i ->
                prefs.filesSort = keys[i]
                d.dismiss()
                if (useIndex) showIndex() else toast("Sorting needs the file index (Notion view shows upload order).")
            }
            .show()
    }

    private fun showStorage() {
        val sb = StringBuilder()
        for ((id, t) in sources()) {
            val (n, b) = db.storageTotal(id)
            if (n == 0) continue
            sb.append("$t\n").append("${num.format(n)} files · ${SpeedMeter.size(b)}\n")
            for ((kind, c, bytes) in db.storageByKind(id)) {
                sb.append("   • ").append(plural(kind)).append(": ").append(num.format(c))
                    .append(" · ").append(SpeedMeter.size(bytes)).append('\n')
            }
            val top = db.subFolders(id, "").sortedByDescending { it.bytes }.take(5)
            if (top.isNotEmpty()) {
                sb.append("   Biggest folders:\n")
                top.forEach { sb.append("      ${it.name}: ${SpeedMeter.size(it.bytes)}\n") }
            }
            sb.append('\n')
        }
        MaterialAlertDialogBuilder(act)
            .setTitle("Storage")
            .setMessage(sb.toString().trim().ifEmpty { "Nothing in the file index yet." })
            .setPositiveButton("OK", null)
            .show()
    }

    private fun plural(kind: String) = when (kind) {
        "Photo" -> "Photos"
        "Video" -> "Videos"
        "Document" -> "Documents"
        "PDF" -> "PDFs"
        "Other" -> "Other files"
        else -> kind
    }

    // ---- Notion pages directly (fallback) -------------------------------------------------------------

    private fun reload() {
        generation++
        items.clear()
        nextCursor = null
        hasMore = true
        loading = false
        error = null
        summary.visibility = View.GONE
        fab.visibility = View.GONE
        modeToggle.visibility = View.GONE
        photosMode = false
        renderCrumbs()
        applyFilter()
        updateBanner()
        list.scrollToPosition(0)
        loadMore()
    }

    private fun loadMore() {
        if (useIndex || loading || !hasMore || stack.isEmpty()) return
        loading = true
        loadingBar.visibility = View.VISIBLE
        val g = generation
        val pageId = stack.last().first
        val cursor = nextCursor
        val token = prefs.token
        val blockPaths = if (sourceId == prefs.rootPageId) db.blockPaths() else emptyMap()
        Thread {
            try {
                val res = NotionClient(token).children(pageId, cursor)
                val parsed = res.results.mapNotNull { parseBlock(it, pageId, blockPaths) }
                main.post {
                    if (g != generation) return@post
                    items.addAll(parsed)
                    nextCursor = res.next
                    hasMore = res.next != null
                    loading = false
                    loadingBar.visibility = if (hasMore && query.isNotEmpty()) View.VISIBLE else View.INVISIBLE
                    applyFilter()
                    if (hasMore && (query.isNotEmpty() || shown.size < 30)) loadMore()
                }
            } catch (e: Exception) {
                main.post {
                    if (g != generation) return@post
                    loading = false
                    loadingBar.visibility = View.INVISIBLE
                    error = if (e is NotionException && e.status == 404) "Can't open this page. Is it connected to your integration?"
                    else friendly(e.message)
                    applyFilter()
                }
            }
        }.start()
    }

    private fun parseBlock(b: JSONObject, pageId: String, blockPaths: Map<String, String>): Item? {
        val id = b.optString("id")
        return when (val type = b.optString("type")) {
            "child_page" -> Folder(id, b.getJSONObject("child_page").optString("title").ifBlank { "Untitled" })
            "image", "video", "audio", "pdf", "file" -> {
                val f = parseFile(b, pageId) ?: return null
                FileItem(f.block, f.name, 0, f.dateText, type, f.url, f.fetchedAt, pageId, 0, blockPaths[id], null, null, null)
            }
            else -> null
        }
    }

    private fun parseFile(b: JSONObject, pageId: String): FileItem? {
        val type = b.optString("type")
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
        return FileItem(b.optString("id"), name, 0, date, type, url, System.currentTimeMillis(), pageId, 0, null, null, null, null)
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

    // ---- shared UI ------------------------------------------------------------------------------

    private fun showEmpty(text: String) {
        empty.text = text
        empty.visibility = View.VISIBLE
    }

    private fun friendly(msg: String?): String = when {
        msg == null -> "error"
        msg.contains("resolve host") || msg.contains("Unable to resolve") -> "No internet connection."
        else -> msg
    }

    private fun renderCrumbs() {
        crumbs.removeAllViews()
        val primary = MaterialColors.getColor(crumbs, androidx.appcompat.R.attr.colorPrimary)
        val muted = MaterialColors.getColor(crumbs, com.google.android.material.R.attr.colorOnSurfaceVariant)
        val strong = MaterialColors.getColor(crumbs, com.google.android.material.R.attr.colorOnSurface)
        val parts: List<Pair<String, () -> Unit>> = when {
            useIndex && query.isNotEmpty() -> listOf("Search results" to {})
            useIndex && photosMode -> listOf("$sourceTitle · all photos and videos" to {})
            useIndex -> {
                val segs = if (path.isEmpty()) emptyList() else path.split('/')
                listOf<Pair<String, () -> Unit>>(sourceTitle to { path = ""; showIndex() }) +
                    segs.mapIndexed { i, s -> s to { path = segs.take(i + 1).joinToString("/"); showIndex() } }
            }
            else -> stack.mapIndexed { i, (_, title) ->
                title to {
                    while (stack.size > i + 1) stack.removeAt(stack.size - 1)
                    reload()
                }
            }
        }
        parts.forEachIndexed { i, (title, go) ->
            if (i > 0) crumbs.addView(TextView(act).apply {
                text = "  ›  "
                setTextColor(muted)
            })
            crumbs.addView(TextView(act).apply {
                text = title
                textSize = 14f
                val last = i == parts.size - 1
                setTextColor(if (last) strong else primary)
                if (last) setTypeface(typeface, android.graphics.Typeface.BOLD)
                setOnClickListener { go() }
            })
        }
        onStackChanged?.invoke()
        (crumbs.parent as View).post { (crumbs.parent as HorizontalScrollView).fullScroll(View.FOCUS_RIGHT) }
    }

    private fun applyViewMode() {
        layout.spanCount = if (photosMode) 4 else if (prefs.filesGrid) 3 else 1
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
        val rel = f.localPath ?: return null
        return File(storageRoot, rel).takeIf { it.exists() }
    }

    private fun freshUrl(f: FileItem): String? {
        if (f.url != null && System.currentTimeMillis() - f.fetchedAt < 50 * 60_000) return f.url
        val fresh = parseFile(NotionClient(prefs.token).block(f.block), f.pageId)
        f.url = fresh?.url
        f.fetchedAt = System.currentTimeMillis()
        return f.url
    }

    private fun actions(f: FileItem) {
        val opts = mutableListOf("Open", "Share", "Save to phone")
        if (f.row != null) opts.addAll(listOf("Rename", "Move", "Delete"))
        if (f.row?.device == "Phone" && localFile(f) != null) opts.add("Remove from this phone")
        if (f.where != null) opts.add("Show in folder")
        opts.add("Open in Notion")
        MaterialAlertDialogBuilder(act)
            .setTitle(f.name)
            .setItems(opts.toTypedArray()) { _, i ->
                when (opts[i]) {
                    "Open" -> obtain(f) { open(f, it) }
                    "Share" -> obtain(f) { share(f, it) }
                    "Save to phone" -> obtain(f) { saveToPhone(f, it) }
                    "Open in Notion" -> openInNotion(f)
                    "Remove from this phone" -> MaterialAlertDialogBuilder(act)
                        .setTitle("Remove from this phone?")
                        .setMessage("Frees ${SpeedMeter.size(f.size)}. The file stays in the cloud and opens from here whenever you need it.")
                        .setPositiveButton("Remove") { _, _ ->
                            val rel = f.localPath ?: return@setPositiveButton
                            Thread {
                                val ok = try { LocalSpace.offloadOne(act, rel, f.block) } catch (e: Exception) { false }
                                main.post {
                                    toast(if (ok) "Removed from the phone. Still in the cloud." else "Not removed: couldn't confirm the cloud copy (or the file changed).")
                                    showIndex()
                                }
                            }.start()
                        }
                        .setNegativeButton("Cancel", null)
                        .show()
                    "Rename" -> ask("Rename", f.name, "Rename") { n -> runOp("Renaming") { ops, _ -> ops.rename(f.row!!, n) } }
                    "Move" -> FolderPicker(act, "Move “${f.name}” to", "Move here", { root, p ->
                        if (root != f.row!!.root) toast("Files can only move within the same backup.")
                        else runOp("Moving") { ops, _ -> ops.move(f.row, p) }
                    }).show(f.row!!.root, f.row.path)
                    "Delete" -> MaterialAlertDialogBuilder(act)
                        .setTitle("Delete “${f.name}”?")
                        .setMessage("It moves to Notion's trash (recoverable there for 30 days). The copy on your phone or laptop is not touched.")
                        .setPositiveButton("Delete") { _, _ -> runOp("Deleting") { ops, _ -> ops.delete(f.row!!) } }
                        .setNegativeButton("Cancel", null)
                        .show()
                    "Show in folder" -> {
                        val r = f.root ?: return@setItems
                        etSearch.setText("")
                        searchBox.visibility = View.GONE
                        if (r != sourceId) openSource(r, titleOf(r))
                        path = f.folder ?: ""
                        showIndex()
                    }
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
        val dest = File(act.cacheDir, "open/${f.block}/$safe")
        if (dest.exists() && dest.length() > 0) {
            dest.setLastModified(System.currentTimeMillis())
            then(dest)
            return
        }
        var call: Call? = null
        var cancelled = false
        val dialog = MaterialAlertDialogBuilder(act)
            .setTitle("Getting file from the cloud")
            .setMessage("Starting…")
            .setNegativeButton("Cancel") { _, _ ->
                cancelled = true
                call?.cancel()
            }
            .setCancelable(false)
            .show()
        Thread {
            try {
                val url = freshUrl(f) ?: throw IllegalStateException("No download link")
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
                LocalSpace.trimAll(act)
                main.post {
                    dialog.dismiss()
                    then(dest)
                }
            } catch (e: Exception) {
                main.post {
                    dialog.dismiss()
                    if (!cancelled) toast("Couldn't get the file: ${friendly(e.message)}")
                }
            }
        }.start()
    }

    /** Opens the full-screen viewer, letting you swipe through the photos/videos/audio around this one. */
    private fun openViewer(f: FileItem) {
        val media = shown.filterIsInstance<FileItem>().filter { it.type == "image" || it.type == "video" || it.type == "audio" }
        // Very long lists: keep a window around the tapped item.
        val i = media.indexOf(f).coerceAtLeast(0)
        val from = (i - 2000).coerceAtLeast(0)
        val window = media.subList(from, (i + 2000).coerceAtMost(media.size))
        ViewerData.items = window
        ViewerData.start = i - from
        act.startActivity(Intent(act, ViewerActivity::class.java))
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
        val url = "https://www.notion.so/${f.pageId.replace("-", "")}#${f.block.replace("-", "")}"
        try {
            act.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (e: ActivityNotFoundException) {
            toast("No browser found.")
        }
    }

    private fun toast(t: String) = Toast.makeText(act, t, Toast.LENGTH_LONG).show()

    // ---- list ---------------------------------------------------------------------------

    private class Holder(v: View) : RecyclerView.ViewHolder(v) {
        val thumbBox: FrameLayout? = v.findViewById(R.id.thumb_box)
        val thumb: ImageView? = v.findViewById(R.id.thumb)
        val badge: ImageView? = v.findViewById(R.id.badge)
        val name: TextView = v.findViewById(R.id.name)
        val meta: TextView = v.findViewById(R.id.meta)
    }

    private inner class Adapter : RecyclerView.Adapter<Holder>() {
        override fun getItemCount() = shown.size
        override fun getItemViewType(position: Int) = when {
            shown[position] is Header -> 2
            photosMode -> 3
            prefs.filesGrid -> 1
            else -> 0
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val inf = LayoutInflater.from(parent.context)
            val v = inf.inflate(
                when (viewType) {
                    2 -> R.layout.item_header
                    0 -> R.layout.item_row
                    else -> R.layout.item_grid
                }, parent, false
            )
            val h = Holder(v)
            if (viewType == 1 || viewType == 3) {
                val cols = if (viewType == 3) 4 else 3
                val d = parent.resources.displayMetrics.density
                val w = (if (parent.width > 0) parent.width else parent.resources.displayMetrics.widthPixels) -
                    parent.paddingLeft - parent.paddingRight
                h.thumbBox?.layoutParams?.height = w / cols - (10 * d).toInt()
                if (viewType == 3) {
                    h.name.visibility = View.GONE
                    h.meta.visibility = View.GONE
                }
            }
            h.badge?.imageTintList = ColorStateList.valueOf(0xFFFFFFFF.toInt())
            return h
        }

        override fun onBindViewHolder(h: Holder, position: Int) {
            val it = shown[position]
            if (it is Header) {
                h.name.text = it.name
                return
            }
            val thumb = h.thumb ?: return
            val badge = h.badge ?: return
            val d = h.itemView.resources.displayMetrics.density
            val grid = prefs.filesGrid
            h.name.text = it.name
            thumb.tag = null
            thumb.setImageResource(kindIcon(it))
            thumb.scaleType = ImageView.ScaleType.CENTER_INSIDE
            val pad = ((if (grid) 30 else 12) * d).toInt()
            thumb.setPadding(pad, pad, pad, pad)
            thumb.imageTintList = ColorStateList.valueOf(
                if (it is Folder) MaterialColors.getColor(thumb, androidx.appcompat.R.attr.colorPrimary)
                else MaterialColors.getColor(thumb, com.google.android.material.R.attr.colorOnSurfaceVariant)
            )
            badge.visibility = View.GONE
            when (it) {
                is Header -> {}
                is Folder -> {
                    h.meta.text = if (it.files >= 0) "${num.format(it.files)} files · ${SpeedMeter.size(it.bytes)}" else "Folder"
                    h.itemView.setOnClickListener { _ ->
                        if (useIndex) {
                            path = it.key
                            showIndex()
                        } else {
                            stack.add(it.key to it.name)
                            etSearch.setText("")
                            reload()
                        }
                    }
                    if (useIndex) h.itemView.setOnLongClickListener { _ ->
                        folderActions(it)
                        true
                    } else h.itemView.setOnLongClickListener(null)
                }
                is FileItem -> {
                    val sizeText = if (it.size > 0) SpeedMeter.size(it.size) else ""
                    h.meta.text = when {
                        it.where != null -> it.where
                        grid -> listOf(it.dateText, sizeText).firstOrNull { s -> s.isNotEmpty() } ?: kindLabel(it)
                        else -> listOf(sizeText, it.dateText, if (sizeText.isEmpty()) kindLabel(it) else "")
                            .filter { s -> s.isNotEmpty() }.joinToString(" · ")
                    }
                    if (it.type == "image" || it.type == "video") {
                        val allowDownload = !(prefs.wifiOnly && Perms.onMeteredNetwork(act))
                        val item = it
                        thumbs.load(it.block, localFile(it), it.url, it.type == "video", allowDownload, thumb) {
                            freshUrl(item)
                        }
                        if (it.type == "video") badge.visibility = View.VISIBLE
                    }
                    h.itemView.setOnClickListener { _ ->
                        if (it.type == "image" || it.type == "video" || it.type == "audio") openViewer(it)
                        else obtain(it) { file -> open(it, file) }
                    }
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
