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
        self.client = NotionClient(settings.get("token"), stop)

    # ---- the mailbox ---------------------------------------------------------------

    def db_id(self):
        cached = self.db.get_meta("clip_db")
        if cached:
            return cached
        # Look for one the other device made (so both use the same mailbox).
        res = self.client._call("POST", "/search", json_body={
            "query": "Clipboard (used by Notion Sync", "page_size": 25,
            "filter": {"property": "object", "value": "database"}})
        found = []
        for d in res.get("results", []):
            title = "".join(t.get("plain_text", "") for t in d.get("title", []))
            if title == TITLE and not d.get("archived") and not d.get("in_trash"):
                found.append((d.get("created_time", ""), d["id"]))
        if found:
            dbid = sorted(found)[0][1]
        else:
            root = self.settings.get("root_page")
            props = {"Name": {"title": {}}, "Kind": {"select": {}}, "From": {"select": {}},
                     "Text": {"rich_text": {}}, "Ready": {"checkbox": {}}, "Files": {"number": {}}}
            dbid = self.client._call("POST", "/databases", json_body={
                "parent": {"type": "page_id", "page_id": root},
                "title": [{"type": "text", "text": {"content": TITLE}}], "properties": props})["id"]
        self.db.set_meta("clip_db", dbid)
        return dbid

    # ---- sending -------------------------------------------------------------------------

    def send(self, kind, payload):
        """kind 'text' (payload: str) or 'image'/'files' (payload: list of file paths)."""
        dbid = self.db_id()
        if kind == "text":
            name = payload.strip().splitlines()[0][:80] if payload.strip() else "Text"
        else:
            name = ", ".join(os.path.basename(p) for p in payload)[:200]
        props = {"Name": {"title": [{"text": {"content": name or "Clipboard"}}]},
                 "Kind": {"select": {"name": kind}}, "From": {"select": {"name": self.device}},
                 "Ready": {"checkbox": False}, "Files": {"number": 0 if kind == "text" else len(payload)}}
        if kind == "text":
            props["Text"] = {"rich_text": _text_chunks(payload)}
        try:
            row = self.client.create_row(dbid, props)
        except NotionError as e:
            if e.status == 404:  # mailbox was deleted: make or find a new one
                self.db.set_meta("clip_db", "")
                return self.send(kind, payload)
            raise
        if kind != "text":
            for p in payload:
                name = os.path.basename(p)
                up = core.upload_name(name)
                btype = core.block_type(up)
                fid = self.client.upload_file(p, up, core.mime(up), os.path.getsize(p))
                try:
                    self.client.attach(row, fid, btype, name)
                except NotionError as e:
                    if e.status == 400 and btype != "file":
                        self.client.attach(row, fid, "file", name)
                    else:
                        raise
        # Only now can the other device pick it up (so it never sees half an item).
        self.client.update_row(row, {"Ready": {"checkbox": True}})
        return row

    # ---- receiving ----------------------------------------------------------------------

    def poll(self, since):
        """New ready items from the other device since `since` (datetime): list of dicts, oldest first."""
        dbid = self.db_id()
        body = {"page_size": 20,
                "sorts": [{"timestamp": "created_time", "direction": "ascending"}],
                "filter": {"and": [
                    {"property": "From", "select": {"does_not_equal": self.device}},
                    {"property": "Ready", "checkbox": {"equals": True}},
                    {"timestamp": "created_time", "created_time": {"on_or_after": _iso(since - timedelta(minutes=1))}},
                ]}}
        try:
            res = self.client.query_database(dbid, body)
        except NotionError as e:
            if e.status == 404:
                self.db.set_meta("clip_db", "")
            raise
        out = []
        for p in res.get("results", []):
            pr = p.get("properties") or {}
            if (((pr.get("From") or {}).get("select") or {}).get("name")) == self.device:
                continue
            if not (pr.get("Ready") or {}).get("checkbox"):
                continue
            kind = ((pr.get("Kind") or {}).get("select") or {}).get("name") or "text"
            text = "".join(t.get("plain_text", "") for t in (pr.get("Text") or {}).get("rich_text", []))
            out.append({"id": p["id"], "kind": kind, "text": text, "created": p.get("created_time", "")})
        return out

    def files_of(self, item):
        """(name, url) of the files attached to an item."""
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
        dbid = self.db_id()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=KEEP_HOURS)
        res = self.client.query_database(dbid, {"page_size": 50, "filter": {"and": [
            {"property": "From", "select": {"equals": self.device}},
            {"timestamp": "created_time", "created_time": {"before": _iso(cutoff)}}]}})
        for p in res.get("results", []):
            try:
                created = datetime.fromisoformat(p.get("created_time", "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if created >= cutoff or (((p.get("properties") or {}).get("From") or {}).get("select") or {}).get("name") != self.device:
                continue
            try:
                self.client.archive_page(p["id"])
            except NotionError:
                pass
