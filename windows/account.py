"""Sign in with Google (your setup is kept in a hidden app folder in your own Google
Drive, so a new laptop picks up where you left off) and moving the setup with a code."""
import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser

import requests

import core

try:
    import gconfig  # written at build time from the GitHub secrets
    CLIENT_ID, CLIENT_SECRET = gconfig.CLIENT_ID, gconfig.CLIENT_SECRET
except ImportError:
    CLIENT_ID, CLIENT_SECRET = "", ""

SCOPES = "openid email https://www.googleapis.com/auth/drive.appdata"
FILE = "notion-sync-setup.json"
PREFIX = "NSYNC1:"


class NoDriveAccess(RuntimeError):
    def __init__(self):
        super().__init__(
            "Google didn't give the app access to its settings file in your Drive.\n\n"
            "Please sign in again, and on Google's page tick the box\n"
            "\u201cSee, create, and delete its own configuration data in your Google Drive\u201d\n"
            "(or press \u201cSelect all\u201d). The app can only see its own small hidden file, "
            "never your Drive files.")


def _check(r):
    """Turns Google Drive errors into plain messages."""
    if r.ok:
        return
    reason = ""
    try:
        err = r.json().get("error") or {}
        reason = ((err.get("errors") or [{}])[0].get("reason") or err.get("status") or "")
        msg = err.get("message") or ""
    except Exception:
        msg = r.text[:200]
    if r.status_code == 403 and reason in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used in project" in msg:
        raise RuntimeError("The Google Drive API is turned off in the \u201cNotion Sync\u201d Google Cloud project.\n"
                           "Open console.cloud.google.com \u2192 APIs & Services \u2192 Library \u2192 Google Drive API \u2192 Enable, "
                           "wait a minute, then sign in again.")
    if r.status_code in (401, 403):
        raise NoDriveAccess()
    raise RuntimeError(f"Google Drive error {r.status_code}: {msg or reason}")


def available():
    return bool(CLIENT_ID and CLIENT_SECRET)


# ---- what is saved ------------------------------------------------------------------

LAPTOP_KEYS = ["root_page", "root_title", "folders", "labels", "excluded", "skip_hidden", "skip_cloud_only",
               "plugged_only", "auto_sync", "interval", "cache_limit_mb"]


def _merge_sources(a, b):
    seen = {}
    for arr in (a or [], b or []):
        for o in arr:
            if o.get("id"):
                seen.setdefault(o["id"], o.get("title", ""))
    return [{"id": k, "title": v} for k, v in seen.items()]


def payload(settings, base=None):
    base = dict(base or {})
    laptop = {k: settings.get(k) for k in LAPTOP_KEYS}
    srcs = []
    if settings.get("root_page"):
        srcs.append({"id": settings.get("root_page"), "title": settings.get("root_title") or "Laptop Backup"})
    for line in settings.get("extra_sources") or []:
        sid, _, title = line.partition("|")
        srcs.append({"id": sid, "title": title})
    base.update({"version": 1, "notion_token": settings.get("token"), "laptop": laptop,
                 "sources": _merge_sources(base.get("sources"), srcs), "updated": int(time.time() * 1000)})
    return base


def sources_in(js):
    out = []
    for part, default in (("phone", "Phone Backup"), ("laptop", "Laptop Backup")):
        p = js.get(part) or {}
        if p.get("root_page"):
            out.append((p["root_page"], p.get("root_title") or default))
    for o in js.get("sources") or []:
        out.append((o.get("id"), o.get("title")))
    seen, res = set(), []
    for sid, t in out:
        if sid and sid not in seen:
            seen.add(sid)
            res.append((sid, t or "Backup"))
    return res


def apply(settings, js):
    """Uses a saved setup on this laptop. Returns False if it has no laptop backup page."""
    if js.get("notion_token"):
        settings.set("token", js["notion_token"])
    extra = list(settings.get("extra_sources") or [])
    for sid, t in sources_in(js):
        if not any(e.startswith(sid + "|") for e in extra):
            extra.append(f"{sid}|{t}")
    settings.set("extra_sources", extra)
    lap = js.get("laptop") or {}
    if not lap.get("root_page"):
        return bool(settings.get("root_page"))
    for k in LAPTOP_KEYS:
        if k in lap and lap[k] is not None and k not in ("folders", "labels"):
            settings.set(k, lap[k])
    # Folders: keep the ones that exist on this laptop; otherwise use this laptop's standard folders.
    folders = [f for f in (lap.get("folders") or []) if os.path.isdir(f)]
    labels = dict(lap.get("labels") or {})
    if not folders:
        import winutil
        folders = winutil.default_folders()
        # Same names as before (Documents, Pictures…) so files land in the same cloud folders.
        by_name = {v.lower(): v for v in labels.values()}
        for f in folders:
            n = os.path.basename(f.rstrip("\\/"))
            labels.setdefault(core.norm(f), by_name.get(n.lower(), n))
    settings.set("folders", folders)
    settings.set("labels", labels)
    return True


def transfer_code(settings):
    raw = json.dumps(payload(settings)).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def read_code(text):
    t = (text or "").strip()
    if not t.startswith(PREFIX):
        return None
    b = t[len(PREFIX):]
    try:
        return json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
    except Exception:
        return None


# ---- Google -------------------------------------------------------------------------

class _Catcher(http.server.BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.result.update({k: v[0] for k, v in q.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in q
        self.wfile.write(("<html><body style='font-family:Segoe UI;padding:40px'><h2>" +
                          ("Signed in. You can close this tab and return to Notion Laptop Sync."
                           if ok else "Sign-in was cancelled.") + "</h2></body></html>").encode())

    def log_message(self, *a):
        pass


def sign_in(settings, timeout=300):
    """Opens Google's sign-in page in the browser; stores the account on success. Returns the email."""
    if not available():
        raise RuntimeError("Google sign-in isn't set up in this build yet")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    _Catcher.result = {}
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}"
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline",
        "prompt": "consent", "state": state})
    webbrowser.open(url)
    srv.timeout = 2
    end = time.time() + timeout
    while time.time() < end and not _Catcher.result:
        srv.handle_request()
    srv.server_close()
    res = _Catcher.result
    if res.get("state") != state or "code" not in res:
        raise RuntimeError("Sign-in was cancelled or timed out")
    tok = requests.post("https://oauth2.googleapis.com/token", data={
        "code": res["code"], "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"}, timeout=30).json()
    if "refresh_token" not in tok:
        raise RuntimeError(tok.get("error_description") or tok.get("error") or "Google didn't sign you in")
    if "drive.appdata" not in (tok.get("scope") or ""):
        raise NoDriveAccess()
    email = ""
    try:
        part = tok["id_token"].split(".")[1]
        email = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email", "")
    except Exception:
        pass
    settings.set("google_refresh", tok["refresh_token"])
    settings.set("google_email", email)
    return email


def sign_out(settings):
    settings.set("google_refresh", "")
    settings.set("google_email", "")
    settings.set("google_saved_at", 0)


def _access_token(settings):
    rt = settings.get("google_refresh")
    if not rt:
        raise RuntimeError("Not signed in")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "refresh_token": rt, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token"}, timeout=30).json()
    if "access_token" not in r:
        raise RuntimeError(r.get("error_description") or r.get("error") or "Google sign-in expired; sign in again")
    return r["access_token"]


def _find(tok):
    r = requests.get("https://www.googleapis.com/drive/v3/files", params={
        "spaces": "appDataFolder", "fields": "files(id)", "q": f"name='{FILE}'"},
        headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    _check(r)
    files = r.json().get("files") or []
    return files[0]["id"] if files else None


def download(settings):
    tok = _access_token(settings)
    fid = _find(tok)
    if not fid:
        return None
    r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}", params={"alt": "media"},
                     headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    _check(r)
    return r.json()


def upload(settings):
    """Saves this laptop's setup to your Google account (keeping the phone's part)."""
    tok = _access_token(settings)
    fid = _find(tok)
    base = None
    if fid:
        r = requests.get(f"https://www.googleapis.com/drive/v3/files/{fid}", params={"alt": "media"},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        if r.ok:
            base = r.json()
    body = json.dumps(payload(settings, base))
    h = {"Authorization": f"Bearer {tok}"}
    if fid:
        r = requests.patch(f"https://www.googleapis.com/upload/drive/v3/files/{fid}", params={"uploadType": "media"},
                           data=body, headers={**h, "Content-Type": "application/json"}, timeout=30)
    else:
        boundary = secrets.token_hex(8)
        meta = json.dumps({"name": FILE, "parents": ["appDataFolder"]})
        data = (f"--{boundary}\r\nContent-Type: application/json\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/json\r\n\r\n{body}\r\n--{boundary}--").encode()
        r = requests.post("https://www.googleapis.com/upload/drive/v3/files", params={"uploadType": "multipart"},
                          data=data, headers={**h, "Content-Type": f"multipart/related; boundary={boundary}"}, timeout=30)
    _check(r)
    settings.set("google_saved_at", time.time())


def save_if_changed(settings):
    if not settings.get("google_refresh") or not settings.get("token"):
        return False
    p = payload(settings)
    p.pop("updated", None)
    h = hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()
    if settings.get("google_saved_hash") == h:
        return False
    upload(settings)
    settings.set("google_saved_hash", h)
    return True


def run_async(fn, done):
    """Runs fn() in the background, then done(result, error)."""
    def work():
        try:
            done(fn(), None)
        except Exception as e:
            done(None, e)
    threading.Thread(target=work, daemon=True).start()
