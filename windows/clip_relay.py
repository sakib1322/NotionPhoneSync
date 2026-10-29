"""The shared clipboard's mailbox: a small Notion database both apps use to hand
copied items (text, images, files) to the other device. Items are removed after a day."""
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone

import core
from notion import NotionClient, NotionError

TITLE = "Clipboard (used by Notion Sync apps - please don't edit)"
KEEP_HOURS = 24
MAX_FILE = 100 * 1024 * 1024


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text_chunks(text):
    """Notion text fields hold 2,000 characters per piece and up to 100 pieces."""
    text = text[:2000 * 100]
    return [{"type": "text", "text": {"content": text[i:i + 2000]}} for i in range(0, len(text), 2000)] or \
        [{"type": "text", "text": {"content": ""}}]


def fingerprint(kind, payload):
    h = hashlib.sha1(kind.encode())
    if kind == "text":
        h.update(payload.encode("utf-8", "replace"))
    else:
        for p in payload:
            h.update(os.path.basename(p).encode())
            try:
                h.update(str(os.path.getsize(p)).encode())
            except OSError:
                pass
    return h.hexdigest()


class Relay:
    def __init__(self, settings, db, device="Laptop", stop=None):
        self.settings = settings
        self.db = db
        self.device = device
        # Sending and fetching go first; the background checks queue normally.
        self.client = NotionClient(settings.get("token"), stop, priority=True)
        self.bg = NotionClient(settings.get("token"), stop)

    # ---- the mailbox ---------------------------------------------------------------

    def _search(self):
        """All mailboxes Notion's search can see, oldest first: [(created, id)]."""
        found, cursor = [], None
        while True:
            body = {"query": "Clipboard (used by Notion Sync", "page_size": 50,
                    "filter": {"property": "object", "value": "database"}}
            if cursor:
                body["start_cursor"] = cursor
            res = self.bg._call("POST", "/search", json_body=body)
            for d in res.get("results", []):
                title = "".join(t.get("plain_text", "") for t in d.get("title", []))
                if title == TITLE and not d.get("archived") and not d.get("in_trash"):
                    found.append((d.get("created_time", ""), d["id"]))
            cursor = res.get("next_cursor") if res.get("has_more") else None
            if not cursor:
                return sorted(set(found))

    def mailboxes(self, refresh=False):
        """Every mailbox to read from, oldest first. Notion's search is slow to notice new
        databases, so the phone and laptop can each make one before seeing the other's;
        we re-search every couple of minutes and read from all of them, and always send
        to the oldest, so the two apps always meet."""
        known = [tuple(x.split("|", 1)) for x in (self.db.get_meta("clip_dbs") or "").split(",") if "|" in x]
        last = float(self.db.get_meta("clip_search_at") or 0)
        if refresh or not known or time.time() - last > 120:
            try:
                found = self._search()
                self.db.set_meta("clip_search_at", str(time.time()))
                known = sorted(set(known) | set(found))
            except NotionError:
                if not known:
                    raise
        if not known:
            root = self.settings.get("root_page")
            props = {"Name": {"title": {}}, "Kind": {"select": {}}, "From": {"select": {}},
                     "Text": {"rich_text": {}}, "Ready": {"checkbox": {}}, "Files": {"number": {}}}
            d = self.client._call("POST", "/databases", json_body={
                "parent": {"type": "page_id", "page_id": root},
                "title": [{"type": "text", "text": {"content": TITLE}}], "properties": props})
            known = [(d.get("created_time", ""), d["id"])]
        self._save(known)
        return [i for _, i in known]

    def _save(self, known):
        self.db.set_meta("clip_dbs", ",".join(f"{c}|{i}" for c, i in sorted(set(known))))
        self.db.set_meta("clip_db", known[0][1] if known else "")

    def _forget(self, dbid):
        known = [tuple(x.split("|", 1)) for x in (self.db.get_meta("clip_dbs") or "").split(",") if "|" in x]
        self._save([k for k in known if k[1] != dbid])

    def db_id(self):
        return self.mailboxes()[0]

    # ---- sending -------------------------------------------------------------------------

    def _ensure_attachments(self, dbid):
        """Adds the 'Attachments' files column (one request per mailbox, ever)."""
        if self.db.get_meta("clip_att") == dbid:
            return True
        try:
            self.client._call("PATCH", f"/databases/{dbid}", json_body={"properties": {"Attachments": {"files": {}}}})
        except NotionError:
            return False
        self.db.set_meta("clip_att", dbid)
        return True

    def _upload_all(self, paths):
        """Uploads files side by side; returns [(name, upload id)] in order."""
        from concurrent.futures import ThreadPoolExecutor

        def one(p):
            name = os.path.basename(p)
            up = core.upload_name(name)
            return name, self.client.upload_file(p, up, core.mime(up), os.path.getsize(p))
        with ThreadPoolExecutor(max_workers=min(3, len(paths))) as ex:
            return list(ex.map(one, paths))

    NOTE_KINDS = ("text", "offer", "request", "failed")

    def send(self, kind, payload, note=None):
        """kind 'text' (payload: str), 'image'/'files' (payload: list of file paths), or one of the
        small notes 'offer' / 'request' / 'failed' (payload: str). `note` rides along with files
        (the offer they answer). Everything goes in ONE request: the item appears complete and
        ready, or not at all."""
        dbid = self.db_id()
        textual = kind in self.NOTE_KINDS
        if kind == "text":
            name = payload.strip().splitlines()[0][:80] if payload.strip() else "Text"
        elif textual:
            name = kind
        else:
            name = ", ".join(os.path.basename(p) for p in payload)[:200]
        props = {"Name": {"title": [{"text": {"content": name or "Clipboard"}}]},
                 "Kind": {"select": {"name": kind}}, "From": {"select": {"name": self.device}},
                 "Ready": {"checkbox": True}, "Files": {"number": 0 if textual else len(payload)}}
        uploads = []
        if textual:
            props["Text"] = {"rich_text": _text_chunks(payload)}
        else:
            if note:
                props["Text"] = {"rich_text": _text_chunks(note)}
            uploads = self._upload_all(payload)
            if self._ensure_attachments(dbid):
                props["Attachments"] = {"files": [{"type": "file_upload", "file_upload": {"id": fid}, "name": n[:100]}
                                                  for n, fid in uploads]}
        if textual or "Attachments" in props:
            try:
                return self.client.create_row(dbid, props)
            except NotionError as e:
                if e.status == 404:  # mailbox was deleted: make or find a new one
                    self._forget(dbid)
                    return self.send(kind, payload, note)
                if e.status != 400 or "Attachments" not in props:
                    raise
                self.db.set_meta("clip_att", "")
        # Older way (if the files column can't be used): files as blocks, then mark ready.
        props.pop("Attachments", None)
        props["Ready"] = {"checkbox": False}
        row = self.client.create_row(dbid, props)
        for n, fid in uploads:
            btype = core.block_type(core.upload_name(n))
            try:
                self.client.attach(row, fid, btype, n)
            except NotionError as e:
                if e.status == 400 and btype != "file":
                    self.client.attach(row, fid, "file", n)
                else:
                    raise
        self.client.update_row(row, {"Ready": {"checkbox": True}})
        return row

    # ---- receiving ----------------------------------------------------------------------

    def poll(self, since):
        """New ready items from the other device since `since` (datetime): list of dicts, oldest first."""
        # No "From" filter: Notion rejects one naming a device that hasn't sent anything yet.
        # This device's own items are skipped below instead.
        body = {"page_size": 50,
                "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
                "filter": {"and": [
                    {"property": "Ready", "checkbox": {"equals": True}},
                    {"timestamp": "created_time", "created_time": {"on_or_after": _iso(since - timedelta(minutes=1))}},
                ]}}
        out = []
        for dbid in self.mailboxes():
            try:
                res = self.bg.query_database(dbid, body)
            except NotionError as e:
                if e.status == 404:
                    self._forget(dbid)
                    continue
                raise
            for p in res.get("results", []):
                pr = p.get("properties") or {}
                if (((pr.get("From") or {}).get("select") or {}).get("name")) == self.device:
                    continue
                if not (pr.get("Ready") or {}).get("checkbox"):
                    continue
                kind = ((pr.get("Kind") or {}).get("select") or {}).get("name") or "text"
                text = "".join(t.get("plain_text", "") for t in (pr.get("Text") or {}).get("rich_text", []))
                files = []
                for f in (pr.get("Attachments") or {}).get("files") or []:
                    url = (f.get("file") or {}).get("url") or (f.get("external") or {}).get("url")
                    if url:
                        files.append((f.get("name") or "file", url))
                count = (pr.get("Files") or {}).get("number") or 0
                out.append({"id": p["id"], "kind": kind, "text": text, "created": p.get("created_time", ""),
                            "files": files if len(files) >= count else None})
        out.sort(key=lambda x: x["created"])
        return out

    def files_of(self, item):
        """(name, url) of the files attached to an item."""
        if item.get("files"):
            return item["files"]
        out = []
        for b in self.client._children(item["id"]):
            t = b.get("type")
            if t not in ("image", "video", "audio", "pdf", "file"):
                continue
            inner = b.get(t, {})
            url = (inner.get("file") or {}).get("url") or (inner.get("external") or {}).get("url")
            name = "".join(c.get("plain_text", "") for c in inner.get("caption", [])) or inner.get("name") or "file"
            if url:
                out.append((name, url))
        return out

    def cleanup(self):
        """Removes this device's items older than a day."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)
        for dbid in self.mailboxes():
            self._cleanup_one(dbid, cutoff)

    def _cleanup_one(self, dbid, cutoff):
        res = self.bg.query_database(dbid, {"page_size": 50, "filter": {"and": [
            {"timestamp": "created_time", "created_time": {"before": _iso(cutoff)}}]}})
        for p in res.get("results", []):
            try:
                created = datetime.fromisoformat(p.get("created_time", "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if created >= cutoff or (((p.get("properties") or {}).get("From") or {}).get("select") or {}).get("name") != self.device:
                continue
            try:
                self.bg.archive_page(p["id"])
            except NotionError:
                pass
