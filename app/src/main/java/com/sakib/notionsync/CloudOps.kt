package com.sakib.notionsync

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * File management in the cloud. Names and folders live in the file index,
 * so renaming and moving only change the index (instant, no re-upload).
 * Deleting moves the file and its index row to Notion's trash.
 */
class CloudOps(ctx: Context) {
    private val app = ctx.applicationContext
    private val db = Db.get(app)
    private val prefs = Prefs(app)
    private val client = NotionClient(prefs.token)
    private val index = CloudIndex(app, client)
    private val pageCache = HashMap<String, String>()

    private fun text(s: String) = JSONObject().put(
        "rich_text", JSONArray().put(JSONObject().put("text", JSONObject().put("content", s.take(1900))))
    )

    private fun title(s: String) = JSONObject().put(
        "title", JSONArray().put(JSONObject().put("text", JSONObject().put("content", s.take(1900))))
    )

    fun rename(row: Db.IdxRow, newName: String) {
        client.updateRow(row.rowId, JSONObject().put("Name", title(newName)))
        db.upsertIdx(row.copy(name = newName))
    }

    fun move(row: Db.IdxRow, newPath: String) {
        client.updateRow(row.rowId, JSONObject().put("Path", text(newPath)))
        db.upsertIdx(row.copy(path = newPath))
    }

    fun delete(row: Db.IdxRow) {
        if (row.kind != "Folder" && row.block.isNotEmpty()) {
            try {
                client.deleteBlock(row.block)
            } catch (e: NotionException) {
                if (e.status != 404) throw e
            }
            // A phone file removed here isn't uploaded again unless it changes on the phone.
            db.markRemovedByBlock(row.block)
        }
        try {
            client.archivePage(row.rowId)
        } catch (e: NotionException) {
            if (e.status != 404) throw e
        }
        db.deleteIdx(row.rowId)
    }

    fun newFolder(root: String, parentPath: String, name: String) {
        val dbId = index.dbIdFor(root, create = root == prefs.rootPageId)
            ?: throw IllegalStateException("This backup has no file index yet")
        index.add(root, dbId, CloudIndex.Entry(name, parentPath, 0, System.currentTimeMillis(), "Folder", "", "", "App", ""))
    }

    /** Renames a folder by updating the folder path of everything inside it. */
    fun renameFolder(root: String, path: String, newName: String, progress: (Int, Int) -> Unit) {
        val parent = path.substringBeforeLast('/', "")
        val newPath = if (parent.isEmpty()) newName else "$parent/$newName"
        val rows = db.rowsUnder(root, path)
        rows.forEachIndexed { i, r ->
            progress(i, rows.size)
            when {
                r.kind == "Folder" && r.path == parent && r.name == path.substringAfterLast('/') -> rename(r, newName)
                r.path == path -> move(r, newPath)
                else -> move(r, newPath + r.path.removePrefix(path))
            }
        }
    }

    fun deleteFolder(root: String, path: String, progress: (Int, Int) -> Unit) {
        val rows = db.rowsUnder(root, path)
        rows.forEachIndexed { i, r ->
            progress(i, rows.size)
            delete(r)
        }
    }

    /** The Notion page that holds files for a folder path (found or created). */
    fun pageForPath(root: String, path: String): String {
        if (path.isEmpty()) return root
        if (root == prefs.rootPageId) db.folderPage(path)?.let { return it }
        pageCache["$root|$path"]?.let { return it }
        val parent = pageForPath(root, path.substringBeforeLast('/', ""))
        val name = path.substringAfterLast('/')
        val id = client.childPages(parent)[name] ?: client.createPage(parent, name)
        pageCache["$root|$path"] = id
        if (root == prefs.rootPageId) db.setFolderPage(path, id)
        return id
    }

    /** Uploads a file into a cloud folder and adds it to the index. */
    fun upload(file: java.io.File, name: String, root: String, path: String, onPart: () -> Unit) {
        val upName = FileTypes.uploadName(name)
        val mime = FileTypes.mime(upName)
        val type = FileTypes.blockType(upName)
        val size = file.length()
        val uploadId = client.uploadFile(file, upName, mime, size, onPart)
        val page = pageForPath(root, path)
        val caption = "$name · uploaded ${java.text.SimpleDateFormat("d MMM yyyy, HH:mm", java.util.Locale.US).format(java.util.Date())}"
        val block = try {
            client.attach(page, uploadId, type, caption)
        } catch (e: NotionException) {
            if (e.status == 400 && type != "file") client.attach(page, uploadId, "file", caption) else throw e
        }
        val dbId = index.dbIdFor(root, create = root == prefs.rootPageId)
        if (dbId != null) {
            index.add(root, dbId, CloudIndex.Entry(name, path, size, System.currentTimeMillis(), CloudIndex.kind(name), block, page, "Upload", ""))
        }
    }

    fun cancel() = client.cancel()
}
