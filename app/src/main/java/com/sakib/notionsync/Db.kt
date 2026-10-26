package com.sakib.notionsync

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * Remembers every file already sent to Notion (by its path, size and
 * modified time) and which Notion page stands for each phone folder.
 * This is what prevents duplicates.
 */
class Db private constructor(ctx: Context) : SQLiteOpenHelper(ctx, "sync.db", null, 1) {

    data class Rec(
        val size: Long,
        val mtime: Long,
        val status: String,
        val blockId: String?,
        val attempts: Int
    )

    companion object {
        @Volatile
        private var instance: Db? = null

        fun get(ctx: Context): Db = instance ?: synchronized(this) {
            instance ?: Db(ctx.applicationContext).also { instance = it }
        }
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, " +
                "status TEXT, block_id TEXT, error TEXT, attempts INTEGER DEFAULT 0, updated INTEGER)"
        )
        db.execSQL("CREATE TABLE folders(path TEXT PRIMARY KEY, page_id TEXT)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}

    fun loadAll(): HashMap<String, Rec> {
        val map = HashMap<String, Rec>()
        readableDatabase.rawQuery(
            "SELECT path, size, mtime, status, block_id, attempts FROM files", null
        ).use { c ->
            while (c.moveToNext()) {
                map[c.getString(0)] = Rec(c.getLong(1), c.getLong(2), c.getString(3), c.getString(4), c.getInt(5))
            }
        }
        return map
    }

    fun save(path: String, size: Long, mtime: Long, status: String, blockId: String?, error: String?, attempts: Int) {
        val v = ContentValues()
        v.put("path", path)
        v.put("size", size)
        v.put("mtime", mtime)
        v.put("status", status)
        v.put("block_id", blockId)
        v.put("error", error)
        v.put("attempts", attempts)
        v.put("updated", System.currentTimeMillis())
        writableDatabase.insertWithOnConflict("files", null, v, SQLiteDatabase.CONFLICT_REPLACE)
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
}
