"""Talks to the Notion API: pages, file uploads and blocks."""
import re
import threading
import time
import uuid

import requests

BASE = "https://api.notion.com/v1"
VERSION = "2022-06-28"
SINGLE_PART_MAX = 20 * 1024 * 1024
PART_SIZE = 10 * 1024 * 1024


class NotionError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


class Stopped(Exception):
    """The sync was stopped by the user (or the app is closing)."""


class Traffic:
    """Counts this app's own internet traffic, for the speed meter."""

    def __init__(self):
        self.up = 0
        self.down = 0
        self.lock = threading.Lock()

    def add_up(self, n):
        with self.lock:
            self.up += n

    def add_down(self, n):
        with self.lock:
            self.down += n


TRAFFIC = Traffic()

# Notion allows about 3 requests per second.
_gate = threading.Lock()
_last = [0.0]


def _throttle():
    with _gate:
        wait = _last[0] + 0.35 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()


def parse_page_id(text):
    """Pulls the page ID out of a Notion link (or accepts a bare ID)."""
    clean = text.strip().split("?")[0].split("#")[0]
    found = re.findall(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", clean)
    if not found:
        return None
    h = found[-1].replace("-", "").lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class _Body:
    """A multipart/form-data body streamed from one slice of a file.

    Reading it in small pieces lets an upload stop within a moment of
    pressing Stop, and lets the speed meter count bytes as they go out.
    """

    def __init__(self, path, offset, length, filename, content_type, part_number, stop):
        boundary = uuid.uuid4().hex
        self.content_type = f"multipart/form-data; boundary={boundary}"
        safe = filename.replace('"', "'").replace("\r", " ").replace("\n", " ")
        pre = b""
        if part_number is not None:
            pre += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"part_number\"\r\n\r\n"
                    f"{part_number}\r\n").encode()
        pre += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe}\"\r\n"
                f"Content-Type: {content_type}\r\n\r\n").encode("utf-8")
        self.pre = pre
        self.post = f"\r\n--{boundary}--\r\n".encode()
        self.path, self.offset, self.length = path, offset, length
        self.total = len(self.pre) + length + len(self.post)
        self.stop = stop
        self.pos = 0
        self.f = None

    def __len__(self):
        return self.total

    def __iter__(self):
        while True:
            chunk = self.read(64 * 1024)
            if not chunk:
                return
            yield chunk

    def read(self, n=-1):
        if self.stop.is_set():
            self.close()
            raise Stopped()
        if n is None or n < 0:
            n = self.total
        out = bytearray()
        pre_end = len(self.pre)
        file_end = pre_end + self.length
        while len(out) < n and self.pos < self.total:
            want = n - len(out)
            if self.pos < pre_end:
                chunk = self.pre[self.pos:self.pos + want]
            elif self.pos < file_end:
                if self.f is None:
                    self.f = open(self.path, "rb")
                    self.f.seek(self.offset)
                chunk = self.f.read(min(want, file_end - self.pos))
                if not chunk:
                    self.close()
                    raise IOError("File changed while uploading")
            else:
                q = self.pos - file_end
                chunk = self.post[q:q + want]
            out += chunk
            self.pos += len(chunk)
        if self.pos >= self.total:
            self.close()
        TRAFFIC.add_up(len(out))
        return bytes(out)

    def close(self):
        if self.f is not None:
            self.f.close()
            self.f = None


class NotionClient:
    def __init__(self, token, stop=None):
        self.token = token
        self.stop = stop or threading.Event()
        self.s = requests.Session()

    def _wait(self, secs):
        if self.stop.wait(secs):
            raise Stopped()

    def _call(self, method, path, json_body=None, body_factory=None):
        attempt = 0
        while True:
            attempt += 1
            if self.stop.is_set():
                raise Stopped()
            _throttle()
            headers = {"Authorization": f"Bearer {self.token}", "Notion-Version": VERSION}
            body = None
            try:
                if body_factory is not None:
                    body = body_factory()
                    headers["Content-Type"] = body.content_type
                    r = self.s.request(method, BASE + path, data=body, headers=headers, timeout=(30, 300))
                else:
                    r = self.s.request(method, BASE + path, json=json_body, headers=headers, timeout=(30, 180))
            except Stopped:
                raise
            except (requests.RequestException, OSError):
                if self.stop.is_set():
                    raise Stopped()
                if attempt >= 4:
                    raise
                self._wait(min(2 ** attempt * 2, 30))
                continue
            except Exception:
                if self.stop.is_set():
                    raise Stopped()
                raise
            finally:
                if body is not None:
                    body.close()
            TRAFFIC.add_down(len(r.content))
            if 200 <= r.status_code < 300:
                return r.json() if r.content else {}
            try:
                j = r.json()
            except ValueError:
                j = {}
            code = j.get("code", "")
            msg = j.get("message") or r.text[:300]
            if (r.status_code in (409, 429) or r.status_code >= 500) and attempt < 6:
                ra = r.headers.get("Retry-After", "")
                self._wait(min(int(ra) if ra.isdigit() else 2 ** attempt, 60))
                continue
            raise NotionError(r.status_code, code, msg)

    def _children(self, page_id):
        cursor = None
        while True:
            q = f"/blocks/{page_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            res = self._call("GET", q)
            for b in res.get("results", []):
                yield b
            if not res.get("has_more"):
                return
            cursor = res.get("next_cursor")
            if not cursor:
                return

    def page_title(self, page_id):
        props = self._call("GET", f"/pages/{page_id}").get("properties", {})
        for p in props.values():
            if p.get("type") == "title":
                return "".join(t.get("plain_text", "") for t in p.get("title", [])) or "Untitled"
        return "Untitled"

    def child_pages(self, page_id):
        out = {}
        for b in self._children(page_id):
            if b.get("type") == "child_page":
                out.setdefault(b["child_page"].get("title", ""), b["id"])
        return out

    def media_captions(self, page_id):
        """Captions of the files already on a page, as caption -> block ID."""
        out = {}
        for b in self._children(page_id):
            t = b.get("type")
            if t not in ("image", "video", "audio", "pdf", "file"):
                continue
            text = "".join(c.get("plain_text", "") for c in b.get(t, {}).get("caption", []))
            if text:
                out.setdefault(text, b["id"])
        return out

    def create_page(self, parent_id, title):
        body = {
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": [{"text": {"content": title[:1900]}}]}},
        }
        return self._call("POST", "/pages", json_body=body)["id"]

    def upload_file(self, path, name, content_type, size):
        """Uploads a file (in 10 MB parts when it is over 20 MB) and returns the upload ID."""
        if size <= SINGLE_PART_MAX:
            fid = self._call("POST", "/file_uploads", json_body={
                "mode": "single_part", "filename": name, "content_type": content_type})["id"]
            self._call("POST", f"/file_uploads/{fid}/send",
                       body_factory=lambda: _Body(path, 0, size, name, content_type, None, self.stop))
            return fid
        parts = (size + PART_SIZE - 1) // PART_SIZE
        fid = self._call("POST", "/file_uploads", json_body={
            "mode": "multi_part", "number_of_parts": parts, "filename": name, "content_type": content_type})["id"]
        for i in range(parts):
            off = i * PART_SIZE
            ln = min(PART_SIZE, size - off)
            self._call("POST", f"/file_uploads/{fid}/send",
                       body_factory=lambda off=off, ln=ln, i=i: _Body(path, off, ln, name, content_type, i + 1, self.stop))
        self._call("POST", f"/file_uploads/{fid}/complete", json_body={})
        return fid

    def attach(self, page_id, upload_id, btype, caption):
        """Adds the uploaded file to the end of a page and returns the new block's ID."""
        block = {
            "object": "block",
            "type": btype,
            btype: {
                "type": "file_upload",
                "file_upload": {"id": upload_id},
                "caption": [{"type": "text", "text": {"content": caption[:1900]}}],
            },
        }
        res = self._call("PATCH", f"/blocks/{page_id}/children", json_body={"children": [block]})
        return res["results"][-1]["id"]

    def delete_block(self, block_id):
        self._call("DELETE", f"/blocks/{block_id}")

    # ---- index database ------------------------------------------------------

    def find_child_database(self, page_id, title):
        for b in self._children(page_id):
            if b.get("type") == "child_database" and b.get("child_database", {}).get("title") == title:
                return b["id"]
        return None

    def create_index_database(self, parent_id, title):
        props = {
            "Name": {"title": {}}, "Path": {"rich_text": {}}, "Size": {"number": {"format": "number"}},
            "Modified": {"date": {}}, "Kind": {"select": {}}, "Block": {"rich_text": {}},
            "Page": {"rich_text": {}}, "Device": {"select": {}}, "Source": {"rich_text": {}},
        }
        body = {"parent": {"type": "page_id", "page_id": parent_id},
                "title": [{"type": "text", "text": {"content": title}}], "properties": props}
        return self._call("POST", "/databases", json_body=body)["id"]

    def create_row(self, db_id, properties):
        return self._call("POST", "/pages", json_body={"parent": {"database_id": db_id}, "properties": properties})["id"]

    def update_row(self, row_id, properties):
        self._call("PATCH", f"/pages/{row_id}", json_body={"properties": properties})

    def archive_page(self, page_id):
        self._call("PATCH", f"/pages/{page_id}", json_body={"archived": True})

    def query_database(self, db_id, body):
        return self._call("POST", f"/databases/{db_id}/query", json_body=body)

    def block(self, block_id):
        return self._call("GET", f"/blocks/{block_id}")
