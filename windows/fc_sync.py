"""Keeps flashcards the same on all your devices, through Notion.

A page called "Flashcards (Notion Sync)" in your shared space holds the newest copy of everything as a
file (flashcards-sync|<device>|<time>), plus the pictures (flashcards-media|<name>). Each sync:
download the newest copy -> merge it here -> upload the merged copy -> remove older copies.
The phone app does exactly the same.
"""
import json
import os
import tempfile
import time

import requests

from notion import NotionClient, NotionError

PAGE_TITLE = "Flashcards (Notion Sync)"
SYNC_PREFIX = "flashcards-sync|"
MEDIA_PREFIX = "flashcards-media|"


def _caption(b):
    t = b.get("type")
    return "".join(c.get("plain_text", "") for c in (b.get(t) or {}).get("caption", []))


def _file_url(b):
    t = b.get("type")
    inner = b.get(t) or {}
    return (inner.get("file") or {}).get("url") or (inner.get("external") or {}).get("url")


class FlashSync:
    def __init__(self, app, col):
        self.app = app
        self.col = col
        self.busy = False
        self.last_error = ""

    @property
    def settings(self):
        return self.app.settings

    def _page(self, client):
        """The shared flashcards page (made under your account's shared home page the first time)."""
        known = self.col.get_meta("sync_page")
        if known:
            return known
        import devices
        home = devices.Registry(self.settings, self.app.db).home() or self.settings.get("root_page")
        if not home:
            raise RuntimeError("connect Notion first")
        pages = [b for b in client._children(home)
                 if b.get("type") == "child_page" and b["child_page"].get("title") == PAGE_TITLE]
        if pages:
            pid = sorted(pages, key=lambda b: b.get("created_time", ""))[0]["id"]
        else:
            pid = client.create_page(home, PAGE_TITLE)
        self.col.set_meta("sync_page", pid)
        return pid

    def status(self):
        at = float(self.col.get_meta("sync_at", 0) or 0)
        if self.busy:
            return "Syncing with your other devices…"
        if self.last_error:
            return "Sync: " + self.last_error
        if not at:
            return "Not synced yet."
        mins = int((time.time() - at) // 60)
        return "Synced with your other devices " + ("just now" if mins < 1 else f"{mins} min ago" if mins < 60 else
                                                     time.strftime("at %H:%M, %d %b", time.localtime(at)))

    def needed(self, max_age=600):
        """Sync if something changed here, or it's been a while (to get other devices' changes)."""
        at = float(self.col.get_meta("sync_at", 0) or 0)
        return self.col.signature() != self.col.get_meta("sync_sig") or time.time() - at > max_age

    def run(self):
        """One full sync. Runs in a background thread. Returns a summary or raises."""
        if self.busy:
            return "already syncing"
        token = self.settings.get("token")
        if not token:
            raise RuntimeError("connect Notion first")
        self.busy = True
        try:
            client = NotionClient(token)
            try:
                page = self._page(client)
                blocks = list(client._children(page))
            except NotionError as e:
                if e.status in (404, 400):          # the page was deleted: make it again
                    self.col.set_meta("sync_page", "")
                    page = self._page(client)
                    blocks = list(client._children(page))
                else:
                    raise
            copies, media = [], {}
            for b in blocks:
                cap = _caption(b)
                if cap.startswith(SYNC_PREFIX):
                    try:
                        t = float(cap.rsplit("|", 1)[1])
                    except (ValueError, IndexError):
                        t = 0
                    copies.append((t, b))
                elif cap.startswith(MEDIA_PREFIX):
                    media[cap[len(MEDIA_PREFIX):]] = b
            copies.sort(key=lambda x: x[0])
            summary = "nothing new"
            newest_t = 0
            if copies:
                newest_t, newest = copies[-1]
                if str(newest_t) != self.col.get_meta("sync_seen"):
                    url = _file_url(client.block(newest["id"]))     # fresh link (they expire)
                    r = requests.get(url, timeout=(20, 120))
                    r.raise_for_status()
                    summary = self.col.merge_sync(json.loads(r.content.decode("utf-8")))
            # pictures: get the ones missing here, send the ones missing there
            import flashcards as F
            mine = self.col.export_sync(device=self._device())
            for name in mine["media"]:
                p = F.Collection.media_path(name)
                if name in media and not os.path.isfile(p):
                    url = _file_url(client.block(media[name]["id"]))
                    if url:
                        r = requests.get(url, timeout=(20, 120))
                        if r.ok:
                            F.Collection._store_media(name, r.content)
            mine = self.col.export_sync(device=self._device())        # after merging the downloaded copy
            for name in mine["media"]:
                p = F.Collection.media_path(name)
                if name not in media and os.path.isfile(p):
                    import core
                    up = client.upload_file(p, core.upload_name(name), core.mime(name), os.path.getsize(p))
                    client.attach(page, up, "file", MEDIA_PREFIX + name)
            # send the merged copy, then tidy older ones
            t = round(time.time(), 3)
            fd, tmp = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(mine, f, ensure_ascii=False, separators=(",", ":"))
            try:
                up = client.upload_file(tmp, "flashcards-sync.txt", "text/plain", os.path.getsize(tmp))
                client.attach(page, up, "file", f"{SYNC_PREFIX}{self._device()}|{t}")
            finally:
                os.remove(tmp)
            for ct, b in copies:
                if ct <= newest_t:
                    try:
                        client.delete_block(b["id"])
                    except NotionError:
                        pass
            self.col.set_meta("sync_seen", str(t))
            self.col.set_meta("sync_sig", self.col.signature())
            self.col.set_meta("sync_at", time.time())
            self.last_error = ""
            return summary
        except Exception as e:
            self.last_error = str(e)[:160]
            raise
        finally:
            self.busy = False

    def _device(self):
        try:
            import devices
            return devices.my_name(self.settings).replace("|", "/")
        except Exception:
            return "Laptop"
