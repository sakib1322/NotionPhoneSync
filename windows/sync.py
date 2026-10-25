"""One sync run: scan the chosen folders, find files not yet in Notion (or
changed since), and upload them into pages that mirror the folder tree."""
import os
import time

import requests

import core
from notion import NotionClient, NotionError, Stopped

MAX_SIZE = 5 * 1024 ** 3

# Windows file attributes
HIDDEN, SYSTEM = 0x2, 0x4
OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS = 0x1000, 0x40000, 0x400000
CLOUD_ONLY = OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


class Syncer:
    def __init__(self, settings, db, log, status):
        self.settings = settings
        self.db = db
        self.log = log
        self.status = status  # function(text, done, total)
        self.folder_cache = {}
        self.child_cache = {}
        self.caption_cache = {}

    # ---- folders -----------------------------------------------------------

    def _key_for(self, path, root, label):
        rel = os.path.relpath(os.path.dirname(path), root)
        return label if rel in (".", "") else label + "/" + rel.replace("\\", "/")

    def folder_page(self, key, create=True):
        """Finds (or creates) the Notion page for a folder, mirroring the full path."""
        if key == "":
            return self.root_id
        if key in self.folder_cache:
            return self.folder_cache[key]
        pid = self.db.folder_page(key)
        if pid:
            self.folder_cache[key] = pid
            return pid
        parent_key, _, name = key.rpartition("/")
        parent_id = self.folder_page(parent_key, create)
        if parent_id is None:
            return None
        kids = self.child_cache.get(parent_id)
        if kids is None:
            kids = self.client.child_pages(parent_id)
            self.child_cache[parent_id] = kids
        pid = kids.get(name)
        if pid is None:
            if not create:
                return None
            pid = self.client.create_page(parent_id, name)
            kids[name] = pid
        self.db.set_folder_page(key, pid)
        self.folder_cache[key] = pid
        return pid

    # ---- scanning ----------------------------------------------------------

    def scan(self, stop):
        s = self.settings
        skip_hidden = s.get("skip_hidden")
        skip_cloud = s.get("skip_cloud_only")
        excluded = {core.norm(e) for e in s.get("excluded")}
        labels = s.get("labels")
        known = self.db.load_all()
        if not known:
            # Nothing recorded yet (fresh install): check Notion for files that are
            # already there, so nothing is uploaded twice.
            s.set("check_notion", True)
        now_ms = time.time() * 1000
        found = []
        checked = 0
        for root in s.get("folders") or []:
            label = labels.get(core.norm(root)) or os.path.basename(root.rstrip("\\/")) or root
            if not os.path.isdir(root):
                self.log(f"Folder not found, skipped: {root}")
                continue
            stack = [root]
            while stack:
                if stop.is_set():
                    return None, known
                d = stack.pop()
                try:
                    it = os.scandir(d)
                except OSError:
                    continue
                with it:
                    for e in it:
                        try:
                            if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                                continue
                            st = e.stat(follow_symlinks=False)
                            attrs = getattr(st, "st_file_attributes", 0)
                            if skip_hidden and (attrs & (HIDDEN | SYSTEM) or e.name.startswith(".")):
                                continue
                            if e.is_dir(follow_symlinks=False):
                                if core.norm(e.path) not in excluded:
                                    stack.append(e.path)
                                continue
                            if core.is_junk(e.name):
                                continue
                            if skip_cloud and attrs & CLOUD_ONLY:
                                continue
                            checked += 1
                            if checked % 2000 == 0:
                                self.status(f"Scanning… {checked:,} files checked", 0, 0)
                                if checked % 20000 == 0:
                                    self.log(f"Checked {checked:,} files so far…")
                            size = st.st_size
                            mtime = int(st.st_mtime * 1000)
                            # Skip empty files and files still being written.
                            if size == 0 or now_ms - mtime < 60_000:
                                continue
                            r = known.get(core.norm(e.path))
                            if r is None or r.status == "retry" or r.size != size or r.mtime != mtime:
                                found.append((mtime, e.path, root, label))
                        except OSError:
                            continue
        self.log(f"Scan done: checked {checked:,} files.")
        found.sort(reverse=True)  # newest first
        return found, known

    # ---- the run -----------------------------------------------------------

    def run(self, stop):
        s = self.settings
        if not s.get("token") or not s.get("root_page"):
            self.log("Not set up yet: add your Notion secret and page link in the Notion tab.")
            return
        if not s.get("folders"):
            self.log("No folders chosen. Add some in the Folders tab.")
            return
        self.log("Starting sync. Checking the Notion page…")
        self.client = NotionClient(s.get("token"), stop)
        self.root_id = s.get("root_page")
        try:
            self.client.page_title(self.root_id)
        except NotionError as e:
            self.log({401: "Notion rejected the secret. Paste it again in the Notion tab.",
                      404: "Can't open your Notion page. Make sure it's connected to your integration."}
                     .get(e.status, f"Notion error: {e}"))
            return
        except Stopped:
            return
        except (requests.RequestException, OSError) as e:
            self.log(f"Can't reach Notion ({e.__class__.__name__}). Will try again on the next run.")
            return

        self.log("Scanning your folders for files…")
        self.status("Scanning your folders…", 0, 0)
        todo, known = self.scan(stop)
        if todo is None:
            self.log("Sync stopped.")
            return
        self.pending = len(todo)
        if not todo:
            s.set("check_notion", False)
            self.log("Everything is up to date.")
            return
        self.log(f"Found {len(todo):,} new or changed file(s).")

        check_notion = s.get("check_notion")
        network_failures = 0
        for i, (_, path, root, label) in enumerate(todo):
            if stop.is_set():
                self.log("Sync stopped.")
                return
            self.pending = len(todo) - i
            name = os.path.basename(path)
            self.status(f"{i + 1:,} of {len(todo):,}: {name}", i, len(todo))
            try:
                st = os.stat(path)
            except OSError:
                continue
            size, mtime = st.st_size, int(st.st_mtime * 1000)
            old = known.get(core.norm(path))
            key = self._key_for(path, root, label)

            if old is None and check_notion:
                try:
                    pid = self.folder_page(key, create=False)
                    if pid:
                        caps = self.caption_cache.get(pid)
                        if caps is None:
                            caps = self.client.media_captions(pid)
                            self.caption_cache[pid] = caps
                        bid = caps.get(f"{name} · {core.caption_time(mtime)}")
                        if bid:
                            self.db.save(path, size, mtime, "done", bid, None, 0)
                            self.log(f"Already in Notion: {path}")
                            continue
                except Stopped:
                    self.log("Sync stopped.")
                    return
                except Exception:
                    pass

            try:
                self.upload_one(path, name, key, size, mtime, old)
                network_failures = 0
                self.log(f"Uploaded {path}")
            except Stopped:
                self.log("Sync stopped. The file in progress will start again next time.")
                return
            except NotionError as e:
                if e.status in (401, 403):
                    self.log(f"Notion refused access ({e}). Stopping.")
                    return
                if 400 <= e.status < 500:
                    self.db.save(path, size, mtime, "skipped", old.block_id if old else None, str(e), 0)
                    self.log(f"Skipped {path}: {e}")
                else:
                    self.retry_later(path, size, mtime, old, str(e))
            except PermissionError:
                self.retry_later(path, size, mtime, old, "File is open in another program")
            except (requests.RequestException, OSError) as e:
                if stop.is_set():
                    self.log("Sync stopped.")
                    return
                if not os.path.exists(path):
                    continue
                self.retry_later(path, size, mtime, old, e.__class__.__name__)
                network_failures += 1
                if network_failures >= 3:
                    self.log("Connection problem. Will try again on the next run.")
                    return
        self.pending = 0
        s.set("check_notion", False)
        self.log("Sync finished.")

    def retry_later(self, path, size, mtime, old, msg):
        attempts = (old.attempts if old else 0) + 1
        status = "failed" if attempts >= 5 else "retry"
        self.db.save(path, size, mtime, status, old.block_id if old else None, msg, attempts)
        self.log(f"Couldn't upload {path} ({msg}). " + ("Gave up after 5 tries." if status == "failed" else "Will retry."))

    def upload_one(self, path, name, key, size, mtime, old):
        if size > MAX_SIZE:
            raise NotionError(413, "too_large", "Larger than Notion's 5 GB limit")
        up_name = core.upload_name(name)
        content_type = core.mime(up_name)
        btype = core.block_type(up_name)
        upload_id = self.client.upload_file(path, up_name, content_type, size)
        caption = f"{name} · {core.caption_time(mtime)}"
        page_id = self.folder_page(key)
        try:
            block_id = self.client.attach(page_id, upload_id, btype, caption)
        except NotionError as e:
            if e.status == 404:
                # A folder page was deleted in Notion: rebuild the folder map and try again.
                self.db.clear_folders()
                self.folder_cache.clear()
                self.child_cache.clear()
                block_id = self.client.attach(self.folder_page(key), upload_id, btype, caption)
            elif e.status == 400 and btype != "file":
                block_id = self.client.attach(page_id, upload_id, "file", caption)
            else:
                raise
        # If this file was uploaded before and has since changed, remove the old copy.
        if old and old.block_id:
            try:
                self.client.delete_block(old.block_id)
            except Exception:
                pass
        self.db.save(path, size, mtime, "done", block_id, None, 0)
