"""Settings, the sync database, the activity log and file-type rules."""
import json
import mimetypes
import os
import sqlite3
import threading
import time
from datetime import datetime

APP = "NotionLaptopSync"
DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP)
os.makedirs(DATA_DIR, exist_ok=True)


def norm(path: str) -> str:
    """Windows paths ignore case, so compare them in one form."""
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


class Settings:
    DEFAULTS = {
        "token": "",
        "root_page": "",
        "root_title": "",
        "folders": None,          # list of folder paths; None = not set up yet
        "labels": {},             # norm(folder) -> Notion page title for that folder
        "excluded": [],           # folder paths to skip
        "skip_hidden": True,
        "skip_cloud_only": True,  # don't download OneDrive "online-only" files
        "plugged_only": False,
        "auto_sync": False,
        "interval": 60,           # minutes
        "startup": True,
        "last_run": 0.0,
        "check_notion": False,
        "closed_to_tray_hint": False,
    }

    def __init__(self):
        self.path = os.path.join(DATA_DIR, "settings.json")
        self.lock = threading.Lock()
        self.data = dict(self.DEFAULTS)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def get(self, key):
        with self.lock:
            v = self.data.get(key, self.DEFAULTS.get(key))
            return list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)

    def set(self, key, value):
        with self.lock:
            self.data[key] = value
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)


class Rec:
    __slots__ = ("size", "mtime", "status", "block_id", "attempts", "index_id")

    def __init__(self, size, mtime, status, block_id, attempts, index_id=None):
        self.size, self.mtime, self.status, self.block_id, self.attempts = size, mtime, status, block_id, attempts
        self.index_id = index_id


class Db:
    """Remembers every file already sent to Notion. This is what prevents duplicates."""

    def __init__(self):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(os.path.join(DATA_DIR, "sync.db"), check_same_thread=False)
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, shown TEXT, size INTEGER, mtime INTEGER, "
                "status TEXT, block_id TEXT, error TEXT, attempts INTEGER DEFAULT 0, updated REAL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS folders(key TEXT PRIMARY KEY, page_id TEXT)")
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(files)").fetchall()]
            if "index_id" not in cols:
                # Adds to the existing data; nothing already recorded is changed.
                self.conn.execute("ALTER TABLE files ADD COLUMN index_id TEXT")
            if "key" not in cols:
                self.conn.execute("ALTER TABLE files ADD COLUMN key TEXT")
            self.conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            self.conn.commit()

    def load_all(self):
        with self.lock:
            rows = self.conn.execute("SELECT path, size, mtime, status, block_id, attempts, index_id FROM files").fetchall()
        return {r[0]: Rec(r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows}

    def save(self, path, size, mtime, status, block_id, error, attempts, key=None):
        """Records a file's sync state, keeping its index link."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE files SET shown=?, size=?, mtime=?, status=?, block_id=?, error=?, attempts=?, updated=?, "
                "key=COALESCE(?, key) WHERE path=?",
                (path, size, mtime, status, block_id, error, attempts, time.time(), key, norm(path)))
            if cur.rowcount == 0:
                self.conn.execute(
                    "INSERT INTO files(path, shown, size, mtime, status, block_id, error, attempts, updated, key) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (norm(path), path, size, mtime, status, block_id, error, attempts, time.time(), key))
            self.conn.commit()

    def set_index_id(self, path, index_id):
        with self.lock:
            self.conn.execute("UPDATE files SET index_id=? WHERE path=?", (index_id, norm(path)))
            self.conn.commit()

    def pending_index(self, limit):
        """Uploaded files that aren't in the cloud index yet (from before the index existed)."""
        with self.lock:
            return self.conn.execute(
                "SELECT shown, size, mtime, block_id, key FROM files WHERE status='done' AND block_id IS NOT NULL "
                "AND index_id IS NULL LIMIT ?", (limit,)).fetchall()

    def count_pending_index(self):
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM files WHERE status='done' AND block_id IS NOT NULL AND index_id IS NULL").fetchone()[0]

    def get_meta(self, k):
        with self.lock:
            r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def set_meta(self, k, v):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
            self.conn.commit()

    def folder_page(self, key):
        with self.lock:
            r = self.conn.execute("SELECT page_id FROM folders WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_folder_page(self, key, page_id):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO folders VALUES(?,?)", (key, page_id))
            self.conn.commit()

    def clear_folders(self):
        with self.lock:
            self.conn.execute("DELETE FROM folders")
            self.conn.commit()

    def reset_all(self):
        with self.lock:
            self.conn.execute("DELETE FROM files")
            self.conn.execute("DELETE FROM folders")
            self.conn.commit()

    def reset_problems(self):
        with self.lock:
            self.conn.execute("DELETE FROM files WHERE status IN ('skipped','failed','retry')")
            self.conn.commit()

    def counts(self):
        with self.lock:
            rows = self.conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()
        return dict(rows)

    def problems(self):
        with self.lock:
            return self.conn.execute(
                "SELECT shown, status, error FROM files WHERE status IN ('skipped','failed','retry') "
                "ORDER BY updated DESC LIMIT 300").fetchall()


class Log:
    def __init__(self):
        self.path = os.path.join(DATA_DIR, "sync.log")
        self.lock = threading.Lock()

    def __call__(self, msg):
        line = f"{datetime.now().strftime('%d %b %H:%M:%S')}  {msg}\n"
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                if os.path.getsize(self.path) > 500_000:
                    with open(self.path, "r", encoding="utf-8") as f:
                        keep = f.read()[-250_000:]
                    keep = keep[keep.find("\n") + 1:]
                    with open(self.path, "w", encoding="utf-8") as f:
                        f.write(keep)
            except OSError:
                pass

    def tail(self, lines=200):
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    all_lines = f.read().splitlines()
            except OSError:
                return "Nothing yet."
        return "\n".join(reversed(all_lines[-lines:])) or "Nothing yet."


# ---- file types -------------------------------------------------------------

IMAGES = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "svg", "tif", "tiff", "ico", "bmp"}
VIDEOS = {"mp4", "mov", "m4v", "webm", "avi", "mkv", "3gp", "wmv", "flv", "mpeg", "mpg", "qt"}
AUDIO = {"mp3", "m4a", "aac", "wav", "ogg", "oga", "wma", "mid", "midi", "flac", "amr", "m4b", "mpga"}

KNOWN_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    "heic": "image/heic", "heif": "image/heif", "svg": "image/svg+xml", "tif": "image/tiff", "tiff": "image/tiff",
    "ico": "image/vnd.microsoft.icon",
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
    "avi": "video/x-msvideo", "wmv": "video/x-ms-wmv",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "m4b": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav",
    "ogg": "audio/ogg", "oga": "audio/ogg", "wma": "audio/x-ms-wma", "mid": "audio/midi", "midi": "audio/midi",
    "pdf": "application/pdf", "txt": "text/plain", "csv": "text/csv", "json": "application/json",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# Temporary and system files that are never worth backing up.
JUNK_NAMES = {"desktop.ini", "thumbs.db", "ehthumbs.db", ".ds_store", "ntuser.dat"}
JUNK_EXT = {"tmp", "crdownload", "part", "partial", "download", "lock", "lnk"}


def ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_junk(name):
    low = name.lower()
    return low in JUNK_NAMES or low.startswith("~$") or ext(name) in JUNK_EXT


def upload_name(name):
    """Voice notes (.opus) are Ogg audio; Notion accepts them as .ogg."""
    return name + ".ogg" if ext(name) == "opus" else name


def mime(name):
    e = ext(name)
    return KNOWN_MIME.get(e) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def block_type(name):
    e = ext(name)
    if e in IMAGES:
        return "image"
    if e in VIDEOS:
        return "video"
    if e in AUDIO:
        return "audio"
    if e == "pdf":
        return "pdf"
    return "file"


INDEX_TITLE = "File index (used by Notion Sync apps - please don't edit)"
DOC_EXT = {"doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "odt", "ods", "odp", "md",
           "pages", "key", "numbers", "epub", "json", "xml", "html", "htm"}


def kind(name):
    e = ext(name)
    if e == "pdf":
        return "PDF"
    if e in DOC_EXT:
        return "Document"
    return {"image": "Photo", "video": "Video", "audio": "Audio"}.get(block_type(upload_name(name)), "Other")


def iso(mtime_ms):
    return datetime.fromtimestamp(mtime_ms / 1000).astimezone().isoformat()


def caption_time(mtime_ms):
    d = datetime.fromtimestamp(mtime_ms / 1000)
    return f"{d.day} {d.strftime('%b %Y, %H:%M')}"


def human_size(n):
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024:.0f} KB"


def human_rate(bps):
    if bps >= 1024 ** 2:
        return f"{bps / 1024 ** 2:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def interval_label(minutes):
    if minutes % 1440 == 0:
        return "day" if minutes == 1440 else f"{minutes // 1440} days"
    if minutes % 60 == 0:
        return "hour" if minutes == 60 else f"{minutes // 60} hours"
    return f"{minutes} minutes"
