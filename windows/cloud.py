"""The cloud drive behind the Files tab: reading the file index (a Notion
database in each backup page), and uploading, downloading, renaming,
moving and deleting files."""
import os
import threading
import time
from datetime import datetime

import requests

import core
from notion import TRAFFIC, NotionClient, NotionError, Stopped

CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or core.DATA_DIR, "NotionLaptopSync", "cache")


def _text(v):
    return {"rich_text": [{"text": {"content": (v or "")[:1900]}}]}


def _plain(props, name):
    p = props.get(name) or {}
    return "".join(t.get("plain_text", "") for t in p.get(p.get("type", ""), []) or [])


def _parse_time(s):
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


class Row:
    """One file (or folder marker) in the index."""
    __slots__ = ("row_id", "root", "name", "path", "size", "mtime", "kind", "block", "page", "device", "source")

    def __init__(self, t):
        (self.row_id, self.root, self.name, self.path, self.size, self.mtime, self.kind,
         self.block, self.page, self.device, self.source) = t

    def tuple(self):
        return (self.row_id, self.root, self.name, self.path, self.size, self.mtime, self.kind,
                self.block, self.page, self.device, self.source)


class Cloud:
    def __init__(self, settings, db, stop=None):
        self.settings = settings
        self.db = db
        self.stop = stop or threading.Event()
        self.client = NotionClient(settings.get("token"), self.stop)
        self.page_cache = {}

    # ---- sources (Laptop Backup, Phone Backup…) ---------------------------------

    def sources(self):
        out = {}
        if self.settings.get("root_page"):
            out[self.settings.get("root_page")] = self.settings.get("root_title") or "Laptop Backup"
        for line in (self.settings.get("extra_sources") or []):
            sid, _, title = line.partition("|")
            if sid and sid not in out:
                out[sid] = title or "Backup"
        return list(out.items())

    def discover(self):
        """Adds other top-level pages the integration can see (e.g. Phone Backup)."""
        known = {sid for sid, _ in self.sources()}
        extra = list(self.settings.get("extra_sources") or [])
        body = {"filter": {"property": "object", "value": "page"}, "page_size": 100}
        res = self.client._call("POST", "/search", json_body=body)
        for p in res.get("results", []):
            if (p.get("parent") or {}).get("type") != "workspace" or p["id"] in known:
                continue
            title = "Untitled"
            for prop in (p.get("properties") or {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", [])) or "Untitled"
            extra.append(f"{p['id']}|{title}")
            known.add(p["id"])
        self.settings.set("extra_sources", extra)

    # ---- index ------------------------------------------------------------------

    def index_db(self, root, create=False):
        k = "index_db:" + root
        dbid = self.db.get_meta(k)
        if dbid:
            return dbid
        dbid = self.client.find_child_database(root, core.INDEX_TITLE)
        if not dbid and create:
            dbid = self.client.create_index_database(root, core.INDEX_TITLE)
        if dbid:
            self.db.set_meta(k, dbid)
        return dbid

    def pull(self, root, full=False, progress=None):
        """Copies new and changed rows into the local copy; a full pull also drops deleted ones."""
        dbid = self.index_db(root)
        if not dbid:
            return 0
        last = float(self.db.get_meta("pull:" + root) or 0)
        last_full = float(self.db.get_meta("pullfull:" + root) or 0)
        if not last_full or time.time() - last_full > 24 * 3600:
            full = True
        started = time.time()
        seen, count, cursor = set(), 0, None
        while True:
            body = {"page_size": 100, "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}]}
            if not full and last:
                since = datetime.fromtimestamp(last - 300).astimezone().isoformat()
                body["filter"] = {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": since}}
            if cursor:
                body["start_cursor"] = cursor
            res = self.client.query_database(dbid, body)
            rows = []
            for p in res.get("results", []):
                if p.get("archived") or p.get("in_trash"):
                    self.db.delete_idx(p["id"])
                    continue
                pr = p.get("properties") or {}
                rows.append((
                    p["id"], root, _plain(pr, "Name"), _plain(pr, "Path"),
                    int((pr.get("Size") or {}).get("number") or 0),
                    _parse_time(((pr.get("Modified") or {}).get("date") or {}).get("start")),
                    ((pr.get("Kind") or {}).get("select") or {}).get("name") or "Other",
                    _plain(pr, "Block"), _plain(pr, "Page"),
                    ((pr.get("Device") or {}).get("select") or {}).get("name") or "",
                    _plain(pr, "Source")))
                seen.add(p["id"])
            self.db.upsert_idx(rows)
            count += len(rows)
            if progress:
                progress(count)
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        if full:
            self.db.prune_idx(root, seen)
            self.db.set_meta("pullfull:" + root, str(started))
        self.db.set_meta("pull:" + root, str(started))
        return count

    def _add_row(self, root, name, path, size, mtime, kind, block, page, device, source):
        dbid = self.index_db(root, create=(root == self.settings.get("root_page")))
        if not dbid:
            raise RuntimeError("This backup has no file index yet")
        props = {
            "Name": {"title": [{"text": {"content": name[:1900]}}]}, "Path": _text(path),
            "Size": {"number": size}, "Modified": {"date": {"start": core.iso(mtime)}},
            "Kind": {"select": {"name": kind}}, "Block": _text(block), "Page": _text(page),
            "Device": {"select": {"name": device}}, "Source": _text(source),
        }
        rid = self.client.create_row(dbid, props)
        self.db.upsert_idx([(rid, root, name, path, size, mtime, kind, block, page, device, source)])
        return rid

    # ---- file management ---------------------------------------------------------------

    def rename(self, row, new_name):
        self.client.update_row(row.row_id, {"Name": {"title": [{"text": {"content": new_name[:1900]}}]}})
        row.name = new_name
        self.db.upsert_idx([row.tuple()])

    def move(self, row, new_path):
        self.client.update_row(row.row_id, {"Path": _text(new_path)})
        row.path = new_path
        self.db.upsert_idx([row.tuple()])

    def delete(self, row):
        if row.kind != "Folder" and row.block:
            try:
                self.client.delete_block(row.block)
            except NotionError as e:
                if e.status != 404:
                    raise
            # A laptop file removed here isn't uploaded again unless it changes.
            self.db.mark_removed_by_block(row.block)
        try:
            self.client.archive_page(row.row_id)
        except NotionError as e:
            if e.status != 404:
                raise
        self.db.delete_idx(row.row_id)

    def new_folder(self, root, parent, name):
        self._add_row(root, name, parent, 0, int(time.time() * 1000), "Folder", "", "", "App", "")

    def rename_folder(self, root, path, new_name, progress=None):
        parent, _, old = path.rpartition("/")
        new_path = f"{parent}/{new_name}" if parent else new_name
        rows = [Row(t) for t in self.db.rows_under(root, path)]
        for i, r in enumerate(rows):
            if progress:
                progress(i, len(rows))
            if r.kind == "Folder" and r.path == parent and r.name == old:
                self.rename(r, new_name)
            elif r.path == path:
                self.move(r, new_path)
            else:
                self.move(r, new_path + r.path[len(path):])

    def delete_folder(self, root, path, progress=None):
        rows = [Row(t) for t in self.db.rows_under(root, path)]
        for i, r in enumerate(rows):
            if progress:
                progress(i, len(rows))
            self.delete(r)

    def page_for_path(self, root, path):
        if not path:
            return root
        if root == self.settings.get("root_page"):
            pid = self.db.folder_page(path)
            if pid:
                return pid
        key = root + "|" + path
        if key in self.page_cache:
            return self.page_cache[key]
        parent_path, _, name = path.rpartition("/")
        parent = self.page_for_path(root, parent_path)
        pid = self.client.child_pages(parent).get(name) or self.client.create_page(parent, name)
        self.page_cache[key] = pid
        if root == self.settings.get("root_page"):
            self.db.set_folder_page(path, pid)
        return pid

    def upload(self, local_path, root, path):
        name = os.path.basename(local_path)
        size = os.path.getsize(local_path)
        up = core.upload_name(name)
        btype = core.block_type(up)
        fid = self.client.upload_file(local_path, up, core.mime(up), size)
        page = self.page_for_path(root, path)
        caption = f"{name} · uploaded {core.caption_time(int(time.time() * 1000))}"
        try:
            block = self.client.attach(page, fid, btype, caption)
        except NotionError as e:
            if e.status == 400 and btype != "file":
                block = self.client.attach(page, fid, "file", caption)
            else:
                raise
        self._add_row(root, name, path, size, int(time.time() * 1000), core.kind(name), block, page, "Upload", "")

    # ---- getting files -------------------------------------------------------------------

    def local_copy(self, row):
        """The laptop's own copy of a file it uploaded, if it's still there and unchanged."""
        if row.device == "Laptop" and row.source and os.path.isfile(row.source):
            return row.source
        return None

    def fresh_url(self, row):
        b = self.client.block(row.block)
        inner = b.get(b.get("type"), {}) or {}
        return (inner.get("file") or {}).get("url") or (inner.get("external") or {}).get("url")

    def download(self, row, progress=None):
        """Returns a path to the file on this laptop, downloading it if needed."""
        local = self.local_copy(row)
        if local:
            return local
        safe = "".join("_" if c in '\\/:*?"<>|' else c for c in row.name)
        dest = os.path.join(CACHE_DIR, row.block, safe)
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            os.utime(dest)  # recently used: kept longest
            return dest
        url = self.fresh_url(row)
        if not url:
            raise RuntimeError("No download link")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        with requests.get(url, stream=True, timeout=(20, 120)) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as out:
                for chunk in r.iter_content(256 * 1024):
                    if self.stop.is_set():
                        raise Stopped()
                    out.write(chunk)
                    done += len(chunk)
                    TRAFFIC.add_down(len(chunk))
                    if progress:
                        progress(done, total)
        os.replace(part, dest)
        try:
            import space
            space.trim_all(self.settings)
        except Exception:
            pass
        return dest
