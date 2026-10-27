"""Shared clipboard on the laptop: whatever you copy (text, images, files) goes to the
phone, and whatever the phone sends lands on this laptop's clipboard, ready for Ctrl+V.
Passwords copied from password managers are never sent."""
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

import core
from clip_relay import MAX_FILE, Relay, fingerprint

try:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32")
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    WINDOWS = True
except (ImportError, AttributeError, OSError):
    WINDOWS = False

CF_UNICODETEXT, CF_DIB, CF_HDROP = 13, 8, 15
GMEM_MOVEABLE = 0x0002


# ---- the Windows clipboard ------------------------------------------------------------

class _Open:
    def __enter__(self):
        for _ in range(20):
            if user32.OpenClipboard(None):
                return self
            time.sleep(0.05)
        raise OSError("The clipboard is busy")

    def __exit__(self, *a):
        user32.CloseClipboard()


def sequence():
    return user32.GetClipboardSequenceNumber() if WINDOWS else 0


def _format(name):
    return user32.RegisterClipboardFormatW(name)


def is_sensitive():
    """True when the copying app marked the content private (e.g. a password manager)."""
    if user32.IsClipboardFormatAvailable(_format("ExcludeClipboardContentFromMonitorProcessing")):
        return True
    for name in ("CanUploadToCloudClipboard", "CanIncludeInClipboardHistory"):
        fmt = _format(name)
        if user32.IsClipboardFormatAvailable(fmt):
            with _Open():
                h = user32.GetClipboardData(fmt)
                if h:
                    p = kernel32.GlobalLock(h)
                    try:
                        if p and ctypes.c_uint32.from_address(p).value == 0:
                            return True
                    finally:
                        kernel32.GlobalUnlock(h)
    return False


def read():
    """What's on the clipboard: ('files', [paths]) / ('text', str) / ('image', PIL image) / None."""
    if is_sensitive():
        return None
    with _Open():
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            h = user32.GetClipboardData(CF_HDROP)
            n = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
            paths = []
            for i in range(n):
                ln = shell32.DragQueryFileW(h, i, None, 0)
                buf = ctypes.create_unicode_buffer(ln + 1)
                shell32.DragQueryFileW(h, i, buf, ln + 1)
                paths.append(buf.value)
            return ("files", paths) if paths else None
        if user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            h = user32.GetClipboardData(CF_UNICODETEXT)
            p = kernel32.GlobalLock(h)
            try:
                text = ctypes.wstring_at(p) if p else ""
            finally:
                kernel32.GlobalUnlock(h)
            if text.strip():
                return ("text", text)
        has_image = user32.IsClipboardFormatAvailable(CF_DIB)
    if has_image:
        from PIL import ImageGrab
        img = ImageGrab.grabclipboard()
        if img is not None and not isinstance(img, list):
            return ("image", img)
    return None


def _put(fmt, data):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    if not user32.SetClipboardData(fmt, h):
        raise OSError("Couldn't set the clipboard")


def write_text(text):
    with _Open():
        user32.EmptyClipboard()
        _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))


def write_image(path):
    import io
    from PIL import Image
    im = Image.open(path).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "BMP")
    with _Open():
        user32.EmptyClipboard()
        _put(CF_DIB, buf.getvalue()[14:])  # a BMP file minus its 14-byte file header is a DIB


def write_files(paths):
    """Puts files on the clipboard the way File Explorer does, so Ctrl+V pastes them."""
    import struct
    names = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # DROPFILES: offset, point, fNC, fWide
    with _Open():
        user32.EmptyClipboard()
        _put(CF_HDROP, header + names)
        _put(_format("Preferred DropEffect"), struct.pack("<I", 1))  # copy, not move


# ---- syncing ------------------------------------------------------------------------------

def received_dir():
    d = os.path.join(os.path.expanduser("~"), "Downloads", "From phone")
    os.makedirs(d, exist_ok=True)
    return d


class ClipSync:
    def __init__(self, app):
        self.app = app
        self.settings = app.settings
        self.db = app.db
        self.log = app.log
        self.ignore_seq = -1
        self.last_fp = None
        self.busy = False
        self.stop = threading.Event()
        self.last_error = ""
        self.last_sent = 0
        self.last_received = 0
        self.boxes = 0
        self.checked = 0

    def status(self):
        """One line for the window."""
        if not self.settings.get("clipboard_sync"):
            return "Off."
        if not (self.settings.get("token") and self.settings.get("root_page")):
            return "Connect Notion first."
        if self.last_error:
            return f"⚠ Problem: {self.last_error[:150]}"
        if not self.checked:
            return "Starting…"
        parts = ["✓ Working"]
        if self.last_sent:
            parts.append("last sent " + time.strftime("%I:%M %p", time.localtime(self.last_sent)))
        if self.last_received:
            parts.append("last received " + time.strftime("%I:%M %p", time.localtime(self.last_received)))
        return " · ".join(parts)

    def enabled(self):
        return bool(self.settings.get("clipboard_sync") and self.settings.get("token") and self.settings.get("root_page"))

    def start(self):
        if not WINDOWS:
            return
        threading.Thread(target=self._watch, daemon=True).start()
        threading.Thread(target=self._receive, daemon=True).start()

    # Copied on the laptop -> phone
    def _watch(self):
        last = sequence()
        while not self.stop.is_set():
            time.sleep(0.8)
            seq = sequence()
            if seq == last:
                continue
            last = seq
            if seq == self.ignore_seq or not self.enabled():
                continue
            try:
                item = read()
            except Exception:
                continue
            if item:
                threading.Thread(target=self._send, args=(item,), daemon=True).start()

    def _send(self, item):
        kind, payload = item
        try:
            if kind == "image":
                path = os.path.join(tempfile.gettempdir(), f"Copied image {time.strftime('%Y-%m-%d %H.%M.%S')}.png")
                payload.save(path, "PNG")
                payload = [path]
            if kind == "files":
                payload = [p for p in payload if os.path.isfile(p)]
                big = [p for p in payload if os.path.getsize(p) > MAX_FILE]
                if big:
                    self.log(f"Shared clipboard: skipped {len(big)} file(s) over 100 MB.")
                payload = [p for p in payload if p not in big]
                if not payload:
                    return
            fp = fingerprint("text" if kind == "text" else "files", payload)
            if fp == self.last_fp:
                return  # just received this from the phone
            self.last_fp = fp
            Relay(self.settings, self.db, "Laptop").send(kind, payload)
            self.last_sent = time.time()
            self.last_error = ""
            what = "text" if kind == "text" else ", ".join(os.path.basename(p) for p in payload)
            self.log(f"Shared clipboard: sent {what[:80]} to the phone.")
        except Exception as e:
            self.last_error = str(e)
            self.log(f"Shared clipboard: couldn't send ({e}).")

    # Sent from the phone -> laptop clipboard
    def _receive(self):
        since = datetime.now(timezone.utc)
        seen = set()
        last_cleanup = 0
        while not self.stop.is_set():
            time.sleep(1.5)
            if not self.enabled():
                since = datetime.now(timezone.utc)
                continue
            try:
                relay = Relay(self.settings, self.db, "Laptop")
                for item in relay.poll(since):
                    if item["id"] in seen:
                        continue
                    seen.add(item["id"])
                    self._apply(relay, item)
                    self.last_received = time.time()
                if time.time() - last_cleanup > 3600:
                    last_cleanup = time.time()
                    relay.cleanup()
                self.checked = time.time()
                self.last_error = ""
            except Exception as e:
                if str(e) != self.last_error:
                    self.log(f"Shared clipboard: can't reach the mailbox in Notion ({e}). Will keep trying.")
                self.last_error = str(e)
                time.sleep(10)

    def _apply(self, relay, item):
        import requests
        if item["kind"] == "text":
            self.last_fp = fingerprint("text", item["text"])
            write_text(item["text"])
            self.ignore_seq = sequence()
            self.log("Shared clipboard: text from the phone is on the clipboard.")
            return
        # Pick names first, then download all files side by side.
        jobs, taken = [], set()
        for name, url in relay.files_of(item):
            safe = "".join("_" if c in '\\/:*?"<>|' else c for c in name)
            dest = os.path.join(received_dir(), safe)
            base, ext = os.path.splitext(dest)
            n = 2
            while os.path.exists(dest) or dest in taken:
                dest = f"{base} ({n}){ext}"
                n += 1
            taken.add(dest)
            jobs.append((url, dest))

        def get(job):
            url, dest = job
            with requests.get(url, stream=True, timeout=(20, 120)) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1024 * 1024):
                        f.write(chunk)
            return dest
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(jobs)))) as ex:
            paths = list(ex.map(get, jobs))
        if not paths:
            return
        self.last_fp = fingerprint("files", paths)
        if item["kind"] == "image" and len(paths) == 1:
            try:
                write_image(paths[0])
            except Exception:
                write_files(paths)
        else:
            write_files(paths)
        self.ignore_seq = sequence()
        self.log(f"Shared clipboard: {len(paths)} file(s) from the phone are on the clipboard (also in Downloads\\From phone).")
