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
    __slots__ = ("row", "id", "name", "type", "page", "page_title", "seen", "status", "created")

    def __init__(self, row, id, name, type, page, page_title, seen, status="", created=""):
        self.row, self.id, self.name, self.type, self.page, self.page_title, self.seen = \
            row, id, name, type, page, page_title, seen
        self.status = status or ""
        self.created = created or ""  # when the device first registered

    def info(self):
        """What the device last reported (files, pending, syncing…)."""
        import json
        try:
            return json.loads(self.status) if self.status else {}
        except ValueError:
            return {}

    def online(self):
        try:
            t = datetime.fromisoformat((self.seen or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return time.time() - t < 12 * 60

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @staticmethod
    def from_dict(d):
        return Device(*(d.get(k) for k in Device.__slots__[:7]), status=d.get("status") or "",
                      created=d.get("created") or "")

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

    # Notion's search is slow to notice new databases, so two devices signing in close together
    # can each make a devices list. We read every one we find (searching again every few
    # minutes), always write to the oldest, and merge by device id, so they always meet.

    def _search(self):
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
                return found

    def dbs(self, parent=None, refresh=False):
        known = [tuple(x.split("|", 1)) for x in (self.db.get_meta("devices_dbs") or "").split(",") if "|" in x]
        old = self.db.get_meta("devices_db")
        if old and old not in [k[1] for k in known]:
            known.append(("", old))
        last = float(self.db.get_meta("devices_search_at") or 0)
        if refresh or not known or time.time() - last > 300:
            try:
                known = list(set(known) | set(self._search()))
                self.db.set_meta("devices_search_at", str(time.time()))
            except NotionError:
                if not known:
                    raise
        if not known:
            parent = parent or self.settings.get("root_page")
            if not parent:
                return []
            props = {"Name": {"title": {}}, "Id": {"rich_text": {}}, "Type": {"rich_text": {}},
                     "Page": {"rich_text": {}}, "Page title": {"rich_text": {}}, "Seen": {"date": {}},
                     "Status": {"rich_text": {}}}
            d = self.client._call("POST", "/databases", json_body={
                "parent": {"type": "page_id", "page_id": parent},
                "title": [{"type": "text", "text": {"content": TITLE}}], "properties": props})
            known = [(d.get("created_time", ""), d["id"])]
            self.db.set_meta("devices_status_col", d["id"])
        # Same id seen with and without a date: keep the dated one.
        by_id = {}
        for c, i in known:
            if i not in by_id or (c and not by_id[i]):
                by_id[i] = c
        known = sorted(((c, i) for i, c in by_id.items()), key=lambda x: (x[0] == "", x[0]))
        self.db.set_meta("devices_dbs", ",".join(f"{c}|{i}" for c, i in known))
        self.db.set_meta("devices_db", known[0][1])
        return [i for _, i in known]

    def db_id(self, parent=None):
        ids = self.dbs(parent)
        return ids[0] if ids else None

    def _forget(self, dbid):
        known = [x for x in (self.db.get_meta("devices_dbs") or "").split(",") if "|" in x and not x.endswith("|" + dbid)]
        self.db.set_meta("devices_dbs", ",".join(known))
        if self.db.get_meta("devices_db") == dbid:
            self.db.set_meta("devices_db", "")

    def _rows(self):
        out = []
        for dbid in self.dbs():
            cursor = None
            while True:
                body = {"page_size": 100}
                if cursor:
                    body["start_cursor"] = cursor
                try:
                    res = self.client.query_database(dbid, body)
                except NotionError as e:
                    if e.status == 404:
                        self._forget(dbid)
                        break
                    raise
                for p in res.get("results", []):
                    pr = p.get("properties") or {}
                    seen = ((pr.get("Seen") or {}).get("date") or {}).get("start") or ""
                    d = Device(p["id"], _plain(pr, "Id"), _plain(pr, "Name"), _plain(pr, "Type"),
                               _plain(pr, "Page"), _plain(pr, "Page title"), seen)
                    d.status = _plain(pr, "Status")
                    d.created = p.get("created_time", "")
                    out.append(d)
                cursor = res.get("next_cursor") if res.get("has_more") else None
                if not cursor:
                    break
        return out

    def all(self):
        """Every device (one per id), newest-seen first; also caches them in settings."""
        by_id = {}
        claims = {}
        for d in self._rows():
            if d.id.startswith("owner:"):
                claims[d.id[6:]] = (d.page, d.seen or d.created or "")
                continue
            if d.id and d.id != HOME_ID:
                first = min([x for x in (d.created, by_id[d.id].created if d.id in by_id else "") if x] or [""])
                if d.id not in by_id or (d.seen or "") > (by_id[d.id].seen or ""):
                    by_id[d.id] = d
                by_id[d.id].created = first
        devs = sorted(by_id.values(), key=lambda d: d.seen or "", reverse=True)
        self.settings.set("devices_cache", [d.as_dict() for d in devs])
        self.settings.set("page_claims", {pg: owner for pg, (owner, _) in claims.items()})
        return devs

    def home(self):
        for d in self._rows():
            if d.id == HOME_ID and d.page:
                return d.page
        return None

    def set_home(self, page_id):
        self._upsert(HOME_ID, "(where new devices' backup pages go)", "home", page_id, "", seen=False)

    def _ensure_status(self, dbid):
        if self.db.get_meta("devices_status_col") == dbid:
            return True
        try:
            self.client._call("PATCH", f"/databases/{dbid}", json_body={"properties": {"Status": {"rich_text": {}}}})
        except NotionError:
            return False
        self.db.set_meta("devices_status_col", dbid)
        return True

    def _upsert(self, did, name, typ, page, page_title, seen=True, status=None):
        dbid = self.db_id()
        if not dbid:
            return None
        props = {"Name": {"title": [{"text": {"content": (name or "Device")[:200]}}]}, "Id": _rt(did),
                 "Type": _rt(typ), "Page": _rt(page), "Page title": _rt(page_title)}
        if seen:
            props["Seen"] = {"date": {"start": datetime.now(timezone.utc).isoformat()}}
        if status is not None and self._ensure_status(dbid):
            props["Status"] = _rt(status)
        res = self.client.query_database(dbid, {"page_size": 5, "filter": {"property": "Id", "rich_text": {"equals": did}}})
        rows = res.get("results", [])
        if rows:
            self.client.update_row(rows[0]["id"], props)
            return rows[0]["id"]
        return self.client.create_row(dbid, props)

    def claim_page(self, page=None):
        """Records that this laptop owns its backup page (you said so), for all your devices."""
        page = page or self.settings.get("root_page")
        self._upsert("owner:" + page, "(owner of a backup page)", "claim", my_id(self.settings), page)
        claims = dict(self.settings.get("page_claims") or {})
        claims[page] = my_id(self.settings)
        self.settings.set("page_claims", claims)

    def register_me(self, status=None):
        """Adds or refreshes this laptop in the list (with what it's doing, if given)."""
        self._name_page()
        import json
        self._upsert(my_id(self.settings), my_name(self.settings), "laptop",
                     self.settings.get("root_page"), self.settings.get("root_title"),
                     status=json.dumps(status) if status is not None else None)
        self.settings.set("devices_seen_at", time.time())

    def _name_page(self, old=None):
        """Keeps this laptop's backup page named after it ("<name> Backup") when it still has a
        default name (Laptop Backup) or was named after the old device name."""
        name = self.settings.get("device_name")
        title = self.settings.get("root_title") or ""
        page = self.settings.get("root_page")
        if not name or not page:
            return
        want = f"{name} Backup"
        if title == want or title not in ("Laptop Backup", "Phone Backup", f"{old} Backup" if old else want):
            return
        try:
            self.client._call("PATCH", f"/pages/{page}", json_body={
                "properties": {"title": {"title": [{"text": {"content": want}}]}}})
            self.settings.set("root_title", want)
        except NotionError:
            pass

    def rename_me(self, new_name, status=None):
        old = my_name(self.settings)
        self.settings.set("device_name", new_name.strip()[:60])
        self._name_page(old)
        self.register_me(status)

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
