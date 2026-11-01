"""Each device backs up to its own Notion page. If this laptop finds it has been backing up
into a page that belongs to another device (e.g. it was set up from that device's saved
setup), it moves to a page of its own:

1. it notes which files *it* uploaded into the other device's page (index rows it created
   after it started, matched with its own upload records),
2. switches to its own page and backs everything up there,
3. once that backup has finished, removes its copies from the other device's page.

Only this laptop's own uploads are ever removed, and only after they're safely in its page."""
import json
import os
import time
from datetime import datetime, timezone

import core
import devices
from notion import NotionClient, NotionError

STATE = os.path.join(core.DATA_DIR, "moved_from.json")


def owner_of(devs, page, claims):
    """Who owns a backup page several devices point at: the one you chose; otherwise the one
    that has uploaded the most files (the page's real owner), then the one registered first."""
    same = [d for d in devs if d.page == page and d.id]
    if len(same) < 2:
        return same[0] if same else None
    chosen = (claims or {}).get(page)
    for d in same:
        if d.id == chosen:
            return d

    def uploaded(d):
        v = d.info().get("uploaded")
        return v if isinstance(v, int) else -1
    return sorted(same, key=lambda d: (-uploaded(d), d.created or "9999", d.id))[0]


def conflict(settings):
    """The device that owns this laptop's page, if it isn't this laptop."""
    me = devices.my_id(settings)
    page = settings.get("root_page")
    if not page:
        return None
    devs = devices.cached(settings)
    if not any(d.id == me and d.page == page for d in devs):
        return None
    owner = owner_of(devs, page, settings.get("page_claims"))
    return None if owner is None or owner.id == me else owner


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def my_uploads(settings, db, client, root):
    """(block id, index row id) of files this laptop uploaded into `root` itself (not ones it
    recognised as already there): index rows created after it started that it recorded."""
    with db.lock:
        r = db.conn.execute("SELECT MIN(updated) FROM files").fetchone()
        mine = {row[1]: (row[0], row[2]) for row in db.conn.execute(
            "SELECT block_id, index_id, path FROM files WHERE status='done' AND index_id IS NOT NULL AND block_id IS NOT NULL")}
    started = r[0] if r and r[0] else None
    if not started:
        return []
    index_db = client.find_child_database(root, core.INDEX_TITLE)
    if not index_db:
        return []
    created_after, cursor = {}, None
    while True:
        # Notion rounds created times down to the minute, hence the minute of slack.
        body = {"page_size": 100, "filter": {"timestamp": "created_time",
                                             "created_time": {"on_or_after": _iso(started - 60)}}}
        if cursor:
            body["start_cursor"] = cursor
        res = client.query_database(index_db, body)
        for p in res.get("results", []):
            src = "".join(t.get("plain_text", "") for t in ((p.get("properties") or {}).get("Source") or {}).get("rich_text", []))
            created_after[p["id"]] = src
        cursor = res.get("next_cursor") if res.get("has_more") else None
        if not cursor:
            break
    # Created after this laptop started, recorded by it, and pointing at one of its own files.
    return [(mine[rid][0], rid) for rid, src in created_after.items()
            if rid in mine and core.norm(src) == core.norm(mine[rid][1])]


def move_to(settings, db, new_page, title, log):
    """Switches this laptop to its own page (remembering its uploads in the old one)."""
    old = settings.get("root_page")
    client = NotionClient(settings.get("token"))
    items = []
    try:
        items = my_uploads(settings, db, client, old)
    except NotionError as e:
        log(f"Couldn't list this laptop's uploads in the old page ({e}); they'll stay there.")
    if items:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"old_root": old, "items": items, "since": time.time()}, f)
    settings.set("root_page", new_page)
    settings.set("root_title", title)
    db.reset_all()
    log(f"This laptop now backs up to its own page “{title}”. "
        f"{len(items):,} file(s) it had put in the other device's page will be removed from there "
        "once they're backed up here.")
    return len(items)


def pending_cleanup():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cleanup(settings, db, log, stop):
    """After a finished backup into the new page: removes this laptop's copies from the old one."""
    st = pending_cleanup()
    if not st or st.get("old_root") == settings.get("root_page"):
        return
    c = db.counts()
    if c.get("done", 0) == 0:
        return  # nothing backed up in the new page yet
    client = NotionClient(settings.get("token"), stop)
    items = st.get("items") or []
    log(f"Removing {len(items):,} file(s) this laptop had put in the other device's page…")
    done = 0
    for block, row in items:
        if stop.is_set():
            break
        for fn, arg in ((client.delete_block, block), (client.archive_page, row)):
            try:
                fn(arg)
            except NotionError as e:
                if e.status not in (400, 404):
                    raise
        done += 1
    left = items[done:]
    if left:
        st["items"] = left
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    else:
        os.remove(STATE)
        log("Done: the other device's page has only its own files again.")
