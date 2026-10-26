package com.sakib.notionsync

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * Remembers every file already sent to Notion (by its path, size and
 * modified time) and which Notion page stands for each phone folder.
 * This is what prevents duplicates.
 *
 * Since version 2 it also keeps a local copy of the cloud file index
 * (table idx), which makes browsing, search and storage totals instant.
 */
class Db private constructor(ctx: Context) : SQLiteOpenHelper(ctx, "sync.db", null, 3) {

    data class Rec(
        val size: Long,
        val mtime: Long,
        val status: String,
        val blockId: String?,
        val attempts: Int,
        val indexId: String? = null
    )

    /** One file (or folder marker) in the cloud index. */
    data class IdxRow(
        val rowId: String,
        val root: String,
        val name: String,
        val path: String,
        val size: Long,
        val mtime: Long,
        val kind: String,
        val block: String,
        val page: String,
        val device: String,
        val source: String
    )

    data class FolderSummary(val name: String, val files: Int, val bytes: Long)

    companion object {
        @Volatile
        private var instance: Db? = null

        fun get(ctx: Context): Db = instance ?: synchronized(this) {
            instance ?: Db(ctx.applicationContext).also { instance = it }
        }

        private const val IDX_COLS = "row_id, root, name, path, size, mtime, kind, block, page, device, source"
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, " +
                "status TEXT, block_id TEXT, error TEXT, attempts INTEGER DEFAULT 0, updated INTEGER, index_id TEXT)"
        )
        db.execSQL("CREATE TABLE folders(path TEXT PRIMARY KEY, page_id TEXT)")
        createIndexTables(db)
        createOutbox(db)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) {
            // Adds to the existing data; nothing already recorded is changed.
            db.execSQL("ALTER TABLE files ADD COLUMN index_id TEXT")
            createIndexTables(db)
        }
        if (oldVersion < 3) createOutbox(db)
    }

    private fun createOutbox(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT, name TEXT, " +
                "size INTEGER, root TEXT, path TEXT, status TEXT, error TEXT, created INTEGER)"
        )
    }

    // ---- uploads made in the app (queue) ---------------------------------------------------

    data class OutItem(val id: Long, val file: String, val name: String, val size: Long, val root: String, val path: String)

    fun queueUpload(file: String, name: String, size: Long, root: String, path: String) {
        val v = ContentValues()
        v.put("file", file)
        v.put("name", name)
        v.put("size", size)
        v.put("root", root)
        v.put("path", path)
        v.put("status", "queued")
        v.put("created", System.currentTimeMillis())
        writableDatabase.insert("outbox", null, v)
    }

    fun queuedUploads(): List<OutItem> {
        val out = ArrayList<OutItem>()
        readableDatabase.rawQuery(
            "SELECT id, file, name, size, root, path FROM outbox WHERE status = 'queued' ORDER BY id", null
        ).use { c -> while (c.moveToNext()) out.add(OutItem(c.getLong(0), c.getString(1), c.getString(2), c.getLong(3), c.getString(4), c.getString(5))) }
        return out
    }

    fun finishUpload(id: Long, status: String, error: String?) {
        val v = ContentValues()
        v.put("status", status)
        v.put("error", error)
        writableDatabase.update("outbox", v, "id = ?", arrayOf(id.toString()))
    }

    fun countQueuedUploads(): Int =
        readableDatabase.rawQuery("SELECT COUNT(*) FROM outbox WHERE status = 'queued'", null)
            .use { c -> if (c.moveToFirst()) c.getInt(0) else 0 }

    /** Every index row in a folder and everything below it (files and folder markers). */
    fun rowsUnder(root: String, path: String): List<IdxRow> {
        val out = ArrayList<IdxRow>()
        val prefix = "$path/"
        readableDatabase.rawQuery(
            "SELECT $IDX_COLS FROM idx WHERE root = ? AND (path = ? OR substr(path, 1, ${prefix.length}) = ?)",
            arrayOf(root, path, prefix)
        ).use { c -> while (c.moveToNext()) out.add(rowFrom(c)) }
        // The marker for the folder itself lives in its parent.
        val parent = path.substringBeforeLast('/', "")
        val name = path.substringAfterLast('/')
        readableDatabase.rawQuery(
            "SELECT $IDX_COLS FROM idx WHERE root = ? AND kind = 'Folder' AND path = ? AND name = ?",
            arrayOf(root, parent, name)
        ).use { c -> while (c.moveToNext()) out.add(rowFrom(c)) }
        return out
    }

    private fun createIndexTables(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE IF NOT EXISTS idx(row_id TEXT PRIMARY KEY, root TEXT, name TEXT, path TEXT, size INTEGER, " +
                "mtime INTEGER, kind TEXT, block TEXT, page TEXT, device TEXT, source TEXT)"
        )
        db.execSQL("CREATE INDEX IF NOT EXISTS idx_root_path ON idx(root, path)")
        db.execSQL("CREATE INDEX IF NOT EXISTS idx_block ON idx(block)")
        db.execSQL(
            "CREATE TABLE IF NOT EXISTS idx_meta(root TEXT PRIMARY KEY, db_id TEXT, last_pull INTEGER DEFAULT 0, " +
                "last_full INTEGER DEFAULT 0)"
        )
    }

    // ---- sync records -------------------------------------------------------------

    fun loadAll(): HashMap<String, Rec> {
        val map = HashMap<String, Rec>()
        readableDatabase.rawQuery(
            "SELECT path, size, mtime, status, block_id, attempts, index_id FROM files", null
        ).use { c ->
            while (c.moveToNext()) {
                map[c.getString(0)] = Rec(c.getLong(1), c.getLong(2), c.getString(3), c.getString(4), c.getInt(5), c.getString(6))
            }
        }
        return map
    }

    /** Records a file's sync state, keeping its index link. */
    fun save(path: String, size: Long, mtime: Long, status: String, blockId: String?, error: String?, attempts: Int) {
        val v = ContentValues()
        v.put("size", size)
        v.put("mtime", mtime)
        v.put("status", status)
        v.put("block_id", blockId)
        v.put("error", error)
        v.put("attempts", attempts)
        v.put("updated", System.currentTimeMillis())
        val w = writableDatabase
        if (w.update("files", v, "path = ?", arrayOf(path)) == 0) {
            v.put("path", path)
            w.insertWithOnConflict("files", null, v, SQLiteDatabase.CONFLICT_REPLACE)
        }
    }

    fun setIndexId(path: String, indexId: String?) {
        val v = ContentValues()
        v.put("index_id", indexId)
        writableDatabase.update("files", v, "path = ?", arrayOf(path))
    }

    /** Uploaded files that aren't in the cloud index yet (from before the index existed). */
    fun pendingIndex(limit: Int): List<Pair<String, Rec>> {
        val out = ArrayList<Pair<String, Rec>>()
        readableDatabase.rawQuery(
            "SELECT path, size, mtime, status, block_id, attempts FROM files " +
                "WHERE status = 'done' AND block_id IS NOT NULL AND index_id IS NULL LIMIT $limit", null
        ).use { c ->
            while (c.moveToNext()) {
                out.add(c.getString(0) to Rec(c.getLong(1), c.getLong(2), c.getString(3), c.getString(4), c.getInt(5)))
            }
        }
        return out
    }

    fun countPendingIndex(): Int =
        readableDatabase.rawQuery(
            "SELECT COUNT(*) FROM files WHERE status = 'done' AND block_id IS NOT NULL AND index_id IS NULL", null
        ).use { c -> if (c.moveToFirst()) c.getInt(0) else 0 }

    /** Marks a file removed in the app, so it isn't uploaded again unless it changes on the phone. */
    fun markRemovedByBlock(blockId: String) {
        writableDatabase.execSQL("UPDATE files SET status = 'removed', index_id = NULL WHERE block_id = ?", arrayOf(blockId))
    }

    fun folderPage(path: String): String? {
        readableDatabase.rawQuery("SELECT page_id FROM folders WHERE path = ?", arrayOf(path)).use { c ->
            return if (c.moveToFirst()) c.getString(0) else null
        }
    }

    fun setFolderPage(path: String, pageId: String) {
        val v = ContentValues()
        v.put("path", path)
        v.put("page_id", pageId)
        writableDatabase.insertWithOnConflict("folders", null, v, SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun clearFolders() {
        writableDatabase.execSQL("DELETE FROM folders")
    }

    fun resetAll() {
        writableDatabase.execSQL("DELETE FROM files")
        writableDatabase.execSQL("DELETE FROM folders")
    }

    fun resetProblems() {
        writableDatabase.execSQL("DELETE FROM files WHERE status IN ('skipped', 'failed', 'retry')")
    }

    /** Notion block ID -> phone path, for showing previews from files still on the phone. */
    fun blockPaths(): HashMap<String, String> {
        val map = HashMap<String, String>()
        readableDatabase.rawQuery(
            "SELECT block_id, path FROM files WHERE status = 'done' AND block_id IS NOT NULL", null
        ).use { c ->
            while (c.moveToNext()) map[c.getString(0)] = c.getString(1)
        }
        return map
    }

    fun counts(): Map<String, Int> {
        val m = HashMap<String, Int>()
        readableDatabase.rawQuery("SELECT status, COUNT(*) FROM files GROUP BY status", null).use { c ->
            while (c.moveToNext()) m[c.getString(0)] = c.getInt(1)
        }
        return m
    }

    fun problems(): List<Pair<String, String>> {
        val out = ArrayList<Pair<String, String>>()
        readableDatabase.rawQuery(
            "SELECT path, status, error FROM files WHERE status IN ('skipped', 'failed', 'retry') " +
                "ORDER BY updated DESC LIMIT 300", null
        ).use { c ->
            while (c.moveToNext()) {
                out.add(c.getString(0) to "${c.getString(1)}: ${c.getString(2) ?: ""}")
            }
        }
        return out
    }

    // ---- cloud index (local copy) ------------------------------------------------------

    fun indexDbId(root: String): String? =
        readableDatabase.rawQuery("SELECT db_id FROM idx_meta WHERE root = ?", arrayOf(root))
            .use { c -> if (c.moveToFirst()) c.getString(0) else null }

    fun setIndexDbId(root: String, dbId: String) {
        val w = writableDatabase
        val v = ContentValues()
        v.put("db_id", dbId)
        if (w.update("idx_meta", v, "root = ?", arrayOf(root)) == 0) {
            v.put("root", root)
            w.insert("idx_meta", null, v)
        }
    }

    /** (last incremental pull, last full pull) in milliseconds. */
    fun pullTimes(root: String): Pair<Long, Long> =
        readableDatabase.rawQuery("SELECT last_pull, last_full FROM idx_meta WHERE root = ?", arrayOf(root))
            .use { c -> if (c.moveToFirst()) c.getLong(0) to c.getLong(1) else 0L to 0L }

    fun setPullTimes(root: String, lastPull: Long, lastFull: Long?) {
        val v = ContentValues()
        v.put("last_pull", lastPull)
        if (lastFull != null) v.put("last_full", lastFull)
        writableDatabase.update("idx_meta", v, "root = ?", arrayOf(root))
    }

    fun upsertIdx(r: IdxRow) {
        val v = ContentValues()
        v.put("row_id", r.rowId)
        v.put("root", r.root)
        v.put("name", r.name)
        v.put("path", r.path)
        v.put("size", r.size)
        v.put("mtime", r.mtime)
        v.put("kind", r.kind)
        v.put("block", r.block)
        v.put("page", r.page)
        v.put("device", r.device)
        v.put("source", r.source)
        writableDatabase.insertWithOnConflict("idx", null, v, SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun upsertIdxBatch(rows: List<IdxRow>) {
        val w = writableDatabase
        w.beginTransaction()
        try {
            rows.forEach { upsertIdx(it) }
            w.setTransactionSuccessful()
        } finally {
            w.endTransaction()
        }
    }

    fun deleteIdx(rowId: String) {
        writableDatabase.delete("idx", "row_id = ?", arrayOf(rowId))
    }

    /** After a full refresh: removes local rows that no longer exist in the cloud. */
    fun pruneIdx(root: String, keep: Set<String>) {
        val stale = ArrayList<String>()
        readableDatabase.rawQuery("SELECT row_id FROM idx WHERE root = ?", arrayOf(root)).use { c ->
            while (c.moveToNext()) if (c.getString(0) !in keep) stale.add(c.getString(0))
        }
        val w = writableDatabase
        w.beginTransaction()
        try {
            stale.forEach { w.delete("idx", "row_id = ?", arrayOf(it)) }
            w.setTransactionSuccessful()
        } finally {
            w.endTransaction()
        }
    }

    private fun rowFrom(c: android.database.Cursor) = IdxRow(
        c.getString(0), c.getString(1), c.getString(2) ?: "", c.getString(3) ?: "", c.getLong(4), c.getLong(5),
        c.getString(6) ?: "Other", c.getString(7) ?: "", c.getString(8) ?: "", c.getString(9) ?: "", c.getString(10) ?: ""
    )

    fun idxCount(root: String): Int =
        readableDatabase.rawQuery("SELECT COUNT(*) FROM idx WHERE root = ? AND kind != 'Folder'", arrayOf(root))
            .use { c -> if (c.moveToFirst()) c.getInt(0) else 0 }

    /** Sub-folders directly inside [path] with their file counts and sizes (including everything below). */
    fun subFolders(root: String, path: String): List<FolderSummary> {
        val prefix = if (path.isEmpty()) "" else "$path/"
        val agg = LinkedHashMap<String, LongArray>()
        val sql = if (path.isEmpty())
            "SELECT path, SUM(CASE WHEN kind = 'Folder' THEN 0 ELSE 1 END), SUM(size) FROM idx WHERE root = ? AND path != '' GROUP BY path"
        else
            "SELECT path, SUM(CASE WHEN kind = 'Folder' THEN 0 ELSE 1 END), SUM(size) FROM idx WHERE root = ? " +
                "AND substr(path, 1, ${prefix.length}) = ? GROUP BY path"
        val args = if (path.isEmpty()) arrayOf(root) else arrayOf(root, prefix)
        readableDatabase.rawQuery(sql, args).use { c ->
            while (c.moveToNext()) {
                val p = c.getString(0) ?: continue
                val rest = p.removePrefix(prefix)
                if (rest.isEmpty()) continue
                val name = rest.substringBefore('/')
                val a = agg.getOrPut(name) { LongArray(2) }
                a[0] += c.getLong(1)
                a[1] += c.getLong(2)
            }
        }
        // Folder markers (empty folders made in the app) live at their own path.
        readableDatabase.rawQuery(
            "SELECT name FROM idx WHERE root = ? AND kind = 'Folder' AND path = ?", arrayOf(root, path)
        ).use { c -> while (c.moveToNext()) agg.getOrPut(c.getString(0)) { LongArray(2) } }
        return agg.map { (n, a) -> FolderSummary(n, a[0].toInt(), a[1]) }.sortedBy { it.name.lowercase() }
    }

    fun filesIn(root: String, path: String, order: String): List<IdxRow> {
        val out = ArrayList<IdxRow>()
        readableDatabase.rawQuery(
            "SELECT $IDX_COLS FROM idx WHERE root = ? AND path = ? AND kind != 'Folder' ORDER BY $order",
            arrayOf(root, path)
        ).use { c -> while (c.moveToNext()) out.add(rowFrom(c)) }
        return out
    }

    fun search(roots: List<String>, query: String, limit: Int = 500): List<IdxRow> {
        if (roots.isEmpty()) return emptyList()
        val out = ArrayList<IdxRow>()
        val like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        val marks = roots.joinToString(",") { "?" }
        readableDatabase.rawQuery(
            "SELECT $IDX_COLS FROM idx WHERE root IN ($marks) AND kind != 'Folder' AND name LIKE ? ESCAPE '\\' " +
                "ORDER BY mtime DESC LIMIT $limit",
            (roots + like).toTypedArray()
        ).use { c -> while (c.moveToNext()) out.add(rowFrom(c)) }
        return out
    }

    /** Totals for the storage screen: (files, bytes) per kind. */
    fun storageByKind(root: String): List<Triple<String, Int, Long>> {
        val out = ArrayList<Triple<String, Int, Long>>()
        readableDatabase.rawQuery(
            "SELECT kind, COUNT(*), SUM(size) FROM idx WHERE root = ? AND kind != 'Folder' GROUP BY kind ORDER BY SUM(size) DESC",
            arrayOf(root)
        ).use { c -> while (c.moveToNext()) out.add(Triple(c.getString(0) ?: "Other", c.getInt(1), c.getLong(2))) }
        return out
    }

    fun storageTotal(root: String): Pair<Int, Long> =
        readableDatabase.rawQuery(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM idx WHERE root = ? AND kind != 'Folder'", arrayOf(root)
        ).use { c -> if (c.moveToFirst()) c.getInt(0) to c.getLong(1) else 0 to 0L }

    fun rowByBlock(block: String): IdxRow? =
        readableDatabase.rawQuery("SELECT $IDX_COLS FROM idx WHERE block = ? LIMIT 1", arrayOf(block))
            .use { c -> if (c.moveToFirst()) rowFrom(c) else null }

    fun rowById(rowId: String): IdxRow? =
        readableDatabase.rawQuery("SELECT $IDX_COLS FROM idx WHERE row_id = ?", arrayOf(rowId))
            .use { c -> if (c.moveToFirst()) rowFrom(c) else null }

    /** Photos and videos, newest first, for the timeline. */
    fun media(root: String, limit: Int = 50000): List<IdxRow> {
        val out = ArrayList<IdxRow>()
        readableDatabase.rawQuery(
            "SELECT $IDX_COLS FROM idx WHERE root = ? AND kind IN ('Photo', 'Video') ORDER BY mtime DESC LIMIT $limit",
            arrayOf(root)
        ).use { c -> while (c.moveToNext()) out.add(rowFrom(c)) }
        return out
    }

    data class Offloadable(val path: String, val size: Long, val mtime: Long, val block: String)

    /** Backed-up files (status done) that may still be on the phone. */
    fun offloadable(): List<Offloadable> {
        val out = ArrayList<Offloadable>()
        readableDatabase.rawQuery(
            "SELECT path, size, mtime, block_id FROM files WHERE status = 'done' AND block_id IS NOT NULL", null
        ).use { c -> while (c.moveToNext()) out.add(Offloadable(c.getString(0), c.getLong(1), c.getLong(2), c.getString(3))) }
        return out
    }

    /** The phone copy was removed to save space; the cloud copy stays. */
    fun markOffloaded(path: String) {
        writableDatabase.execSQL("UPDATE files SET status = 'offloaded' WHERE path = ?", arrayOf(path))
    }

    fun offloadedTotals(): Pair<Int, Long> =
        readableDatabase.rawQuery("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE status = 'offloaded'", null)
            .use { c -> if (c.moveToFirst()) c.getInt(0) to c.getLong(1) else 0 to 0L }
}
