"""Your devices: every phone and laptop signed in to the same account, each with its own
name and its own backup page. Kept in a small Notion database (the "Devices" list) so every
device can see the others, and in this laptop's settings (its own id and name)."""
import os
import socket
import time
from datetime import datetime, timezone

from notion import NotionClient, NotionError

TITLE = "Devices (used by Notion Sync apps - please don't edit)"
HOME_ID = "home"  # the row that remembers where new devices' backup pages go


def my_id(settings):
    did = settings.get("device_id")
    if not did:
        did = os.urandom(6).hex()
        settings.set("device_id", did)
    return did


def default_name():
    try:
        n = os.environ.get("COMPUTERNAME") or socket.gethostname()
    except OSError:
        n = ""
    return (n or "Laptop").strip()[:60]


def my_name(settings):
    return settings.get("device_name") or default_name()


def _rt(v):
    return {"rich_text": [{"text": {"content": (v or "")[:1900]}}]}


def _plain(props, name):
    p = props.get(name) or {}
    return "".join(t.get("plain_text", "") for t in p.get(p.get("type", ""), []) or [])


class Device:
    __slots__ = ("row", "id", "name", "type", "page", "page_title", "seen")

    def __init__(self, row, id, name, type, page, page_title, seen):
        self.row, self.id, self.name, self.type, self.page, self.page_title, self.seen = \
            row, id, name, type, page, page_title, seen

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @staticmethod
    def from_dict(d):
        return Device(*(d.get(k) for k in Device.__slots__))

    def seen_ago(self):
        if not self.seen:
            return "never"
        try:
            t = datetime.fromisoformat(self.seen.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return ""
        s = time.time() - t
        if s < 120:
            return "just now"
        if s < 3600:
            return f"{int(s // 60)} min ago"
        if s < 86400:
            return f"{int(s // 3600)} h ago"
        return f"{int(s // 86400)} days ago"


class Registry:
    def __init__(self, settings, db, stop=None):
        self.settings = settings
        self.db = db
        self.client = NotionClient(settings.get("token"), stop)

    # ---- the database ------------------------------------------------------------

    def db_id(self, parent=None):
        cached = self.db.get_meta("devices_db")
        if cached:
            return cached
        found, cursor = [], None
        while True:
            body = {"query": "Devices (used by Notion Sync", "page_size": 50,
                    "filter": {"property": "object", "value": "database"}}
            if cursor:
                body["start_cursor"] = cursor
            res = self.client._call("POST", "/search", json_body=body)
            for d in res.get("results", []):
                title = "".join(t.get("plain_text", "") for t in d.get("title", []))
                if title == TITLE and not d.get("archived") and not d.get("in_trash"):
                    found.append((d.get("created_time", ""), d["id"]))
            cursor = res.get("next_cursor") if res.get("has_more") else None
            if not cursor:
                break
        if found:
            dbid = sorted(found)[0][1]
        else:
            parent = parent or self.settings.get("root_page")
            if not parent:
                return None
            props = {"Name": {"title": {}}, "Id": {"rich_text": {}}, "Type": {"rich_text": {}},
                     "Page": {"rich_text": {}}, "Page title": {"rich_text": {}}, "Seen": {"date": {}}}
            dbid = self.client._call("POST", "/databases", json_body={
                "parent": {"type": "page_id", "page_id": parent},
                "title": [{"type": "text", "text": {"content": TITLE}}], "properties": props})["id"]
        self.db.set_meta("devices_db", dbid)
        return dbid

    def _rows(self):
        dbid = self.db_id()
        if not dbid:
            return []
        out, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            try:
                res = self.client.query_database(dbid, body)
            except NotionError as e:
                if e.status == 404:
                    self.db.set_meta("devices_db", "")
                    return self._rows()
                raise
            for p in res.get("results", []):
                pr = p.get("properties") or {}
                seen = ((pr.get("Seen") or {}).get("date") or {}).get("start") or ""
                out.append(Device(p["id"], _plain(pr, "Id"), _plain(pr, "Name"), _plain(pr, "Type"),
                                  _plain(pr, "Page"), _plain(pr, "Page title"), seen))
            cursor = res.get("next_cursor") if res.get("has_more") else None
            if not cursor:
                return out

    def all(self):
        """Every device (one per id), newest-seen first; also caches them in settings."""
        by_id = {}
        for d in self._rows():
            if d.id and d.id != HOME_ID:
                if d.id not in by_id or (d.seen or "") > (by_id[d.id].seen or ""):
                    by_id[d.id] = d
        devs = sorted(by_id.values(), key=lambda d: d.seen or "", reverse=True)
        self.settings.set("devices_cache", [d.as_dict() for d in devs])
        return devs

    def home(self):
        for d in self._rows():
            if d.id == HOME_ID and d.page:
                return d.page
        return None

    def set_home(self, page_id):
        self._upsert(HOME_ID, "(where new devices' backup pages go)", "home", page_id, "", seen=False)

    def _upsert(self, did, name, typ, page, page_title, seen=True):
        dbid = self.db_id()
        if not dbid:
            return None
        props = {"Name": {"title": [{"text": {"content": (name or "Device")[:200]}}]}, "Id": _rt(did),
                 "Type": _rt(typ), "Page": _rt(page), "Page title": _rt(page_title)}
        if seen:
            props["Seen"] = {"date": {"start": datetime.now(timezone.utc).isoformat()}}
        res = self.client.query_database(dbid, {"page_size": 5, "filter": {"property": "Id", "rich_text": {"equals": did}}})
        rows = res.get("results", [])
        if rows:
            self.client.update_row(rows[0]["id"], props)
            return rows[0]["id"]
        return self.client.create_row(dbid, props)

    def register_me(self):
        """Adds or refreshes this laptop in the list."""
        self._upsert(my_id(self.settings), my_name(self.settings), "laptop",
                     self.settings.get("root_page"), self.settings.get("root_title"))
        self.settings.set("devices_seen_at", time.time())

    def rename_me(self, new_name):
        old = my_name(self.settings)
        new_name = new_name.strip()[:60]
        self.settings.set("device_name", new_name)
        # A page the app named after the device is renamed with it.
        if self.settings.get("root_page") and self.settings.get("root_title") == f"{old} Backup":
            title = f"{new_name} Backup"
            try:
                self.client._call("PATCH", f"/pages/{self.settings.get('root_page')}", json_body={
                    "properties": {"title": {"title": [{"text": {"content": title}}]}}})
                self.settings.set("root_title", title)
            except NotionError:
                pass
        self.register_me()

    # ---- pages ------------------------------------------------------------------------

    def candidate_parents(self):
        """Pages a new device's backup page can go inside: the remembered spot first, then
        top-level pages the integration can see that aren't another device's backup."""
        devices = {d.page for d in self.all() if d.page}
        out = []
        home = self.home()
        if home:
            try:
                out.append((home, self.client.page_title(home)))
            except NotionError:
                pass
        res = self.client._call("POST", "/search", json_body={
            "filter": {"property": "object", "value": "page"}, "page_size": 100})
        for p in res.get("results", []):
            if (p.get("parent") or {}).get("type") != "workspace" or p["id"] in devices or p["id"] == home:
                continue
            title = "Untitled"
            for prop in (p.get("properties") or {}).values():
                if prop.get("type") == "title":
                    title = "".join(t.get("plain_text", "") for t in prop.get("title", [])) or "Untitled"
            out.append((p["id"], title))
        return out

    def create_page(self, parent, title):
        pid = self.client.create_page(parent, title)
        self.set_home(parent)
        return pid


def cached(settings):
    return [Device.from_dict(d) for d in (settings.get("devices_cache") or [])]


def others_recent(settings, days=30):
    """Ids of the other devices seen in the last `days` days (from the cached list)."""
    me = my_id(settings)
    cutoff = time.time() - days * 86400
    out = set()
    for d in cached(settings):
        if d.id == me or not d.id:
            continue
        try:
            t = datetime.fromisoformat((d.seen or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            t = time.time()
        if t >= cutoff:
            out.add(d.id)
    return out


def name_of(settings, did):
    for d in cached(settings):
        if d.id == did:
            return d.name
    return None
