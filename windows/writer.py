"""AI writing helper (like SwiftSlate): type a sentence anywhere, finish it with a command such as /im,
and the text is rewritten in place by the AI you chose (Gemini, Claude, ChatGPT, DeepSeek, Qwen …).

Laptop side: a keyboard watcher notices the command as you type it, then selects your text, copies it,
asks the AI, and pastes the result over it. Your clipboard is put back afterwards.
"""
import os
import threading
import time

import requests

# ---- AI providers ----------------------------------------------------------------------------------------
# kind "openai" = the common OpenAI-style API most providers offer; "anthropic" = Claude's own API.

PROVIDERS = {
    "gemini": ("Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "auto",
               "aistudio.google.com/apikey"),
    "claude": ("Claude (Anthropic)", "anthropic", "https://api.anthropic.com/v1", "claude-haiku-5-5",
               "console.anthropic.com"),
    "openai": ("ChatGPT (OpenAI)", "openai", "https://api.openai.com/v1", "gpt-4.1-mini", "platform.openai.com/api-keys"),
    "deepseek": ("DeepSeek", "openai", "https://api.deepseek.com/v1", "deepseek-chat", "platform.deepseek.com"),
    "qwen": ("Qwen (Alibaba)", "openai", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen-plus",
             "bailian.console.alibabacloud.com"),
    "grok": ("Grok (xAI)", "openai", "https://api.x.ai/v1", "grok-3-mini", "console.x.ai"),
    "groq": ("Groq (fast, free tier)", "openai", "https://api.groq.com/openai/v1", "auto",
             "console.groq.com/keys"),
    "mistral": ("Mistral", "openai", "https://api.mistral.ai/v1", "mistral-small-latest", "console.mistral.ai"),
    "openrouter": ("OpenRouter (hundreds of models)", "openai", "https://openrouter.ai/api/v1", "auto",
                   "openrouter.ai/keys"),
    "custom": ("Other (OpenAI-compatible)", "openai", "", "", ""),
}

# ---- commands ----------------------------------------------------------------------------------------------

COMMANDS = {
    "/im": "Fix the grammar, spelling and punctuation and improve the wording so it reads naturally and clearly. "
           "Keep the same meaning, tone and language.",
    "/fix": "Fix only grammar, spelling and punctuation. Change nothing else.",
    "/formal": "Rewrite it to sound formal and professional.",
    "/friendly": "Rewrite it to sound warm, friendly and casual.",
    "/short": "Make it shorter and more concise, keeping the key points.",
    "/long": "Expand it with a little more detail, keeping the same tone.",
    "/en": "Translate it into natural English.",
    "/bn": "Translate it into natural Bangla (Bangladeshi style; English terms may be written in Bangla script "
           "where that's how people normally say them).",
    "/reply": "Write a good reply to this message (the text is a message someone sent me).",
    "/emoji": "Add a few fitting emojis, keep the text otherwise the same.",
}

SYSTEM = ("You are a writing assistant inside the user's keyboard. You receive text the user typed and an "
          "instruction. Return ONLY the resulting text — no quotes, no explanations, no preface, no markdown. "
          "Keep line breaks where they make sense. Unless the instruction is to translate, answer in the same "
          "language as the text.")


def commands(settings):
    """All commands: the built-in ones plus your own (Settings: writer_custom = {"/x": "instruction"})."""
    out = dict(COMMANDS)
    out.update({k: v for k, v in (settings.get("writer_custom") or {}).items() if k.startswith("/") and v})
    return out


def triggers(settings):
    """[(what you type, the command it means)], e.g. ("/im", "/im") or (".im", "/im"), longest first.
    You choose the starting character in the app: / (slash), . (full stop) or both."""
    pre = settings.get("writer_prefix") or "both"
    starts = ["/", "."] if pre == "both" else [pre]
    out = []
    for cmd in commands(settings):
        for st in starts:
            out.append((st + cmd[1:], cmd))
    return sorted(out, key=lambda t: len(t[0]), reverse=True)


def match_trigger(settings, text):
    """(typed command, command) if the text ends with one, else None. A slash command needs a space (or
    nothing) before it, so links like a/b aren't touched; a full-stop command can follow a word directly
    ("I has went.im")."""
    for typed, cmd in triggers(settings):
        if text.endswith(typed):
            before = text[: -len(typed)]
            if typed.startswith("/") and before and not before[-1].isspace():
                continue
            if typed.startswith(".") and before.endswith("."):
                continue                                    # "..." isn't a command
            return typed, cmd
    return None


def probe(settings, log=None):
    """Checks, from the most powerful model down, which ones answer right now. Models that work again are
    put back in line at once (so Auto goes back up); it stops at the first one that works.
    Returns [(model, 'ok' / 'resting until …' / reason)]."""
    import time as _t
    prov, kind, base, model, key = config(settings)
    if model != AUTO or not key:
        return []
    names = auto_models(settings, prov, key, base)
    allcool = dict(settings.get("writer_cool") or {})
    cool = dict(allcool.get(prov) or {})
    out = []
    for m in names:
        now = _t.time()
        try:
            _rewrite(settings, "Reply with just: OK", "ping", timeout=25, model=m)
            cool.pop(m, None)
            out.append((m, "ok"))
            break
        except QuotaHit as e:
            cool[m] = e.until
            out.append((m, "resting until " + _t.strftime("%I:%M %p", _t.localtime(e.until)).lstrip("0")))
        except ModelGone:
            cool[m] = now + 7 * 86400
            out.append((m, "not available with this key"))
        except SlowModel:
            cool[m] = now + 15 * 60
            out.append((m, "too slow right now"))
        except RuntimeError as e:
            if "API key was refused" in str(e):
                raise
            cool[m] = now + 10 * 60
            out.append((m, str(e)[:60]))
    allcool[prov] = cool
    settings.set("writer_cool", allcool)
    settings.set("writer_probe_at", _t.time())
    if out and out[-1][1] == "ok":
        settings.set("writer_last_model", out[-1][0])
        if log:
            log(f"Writing helper: best model available now is {out[-1][0]}.")
    return out


def config(settings):
    prov = settings.get("writer_provider") or "gemini"
    label, kind, base, model, _ = PROVIDERS.get(prov, PROVIDERS["custom"])
    keys = settings.get("writer_keys") or {}
    models = settings.get("writer_models") or {}
    if prov == "custom":
        base = (settings.get("writer_base") or "").rstrip("/")
    return prov, kind, base, (models.get(prov) or model), keys.get(prov) or ""


class ModelGone(RuntimeError):
    """The chosen model doesn't exist (any more) for this key."""
    def __init__(self, msg, suggestion=None):
        super().__init__(msg)
        self.suggestion = suggestion


FAMILY = {"gemini": "flash", "claude": "haiku", "openai": "mini", "deepseek": "chat", "qwen": "plus",
          "grok": "mini", "groq": "llama", "mistral": "small"}
BAD = ("embed", "tts", "image", "audio", "vision", "live", "transcribe", "realtime", "search", "moderation",
       "guard", "whisper", "dall", "veo", "imagen", "lite", "nano", "thinking", "learnlm", "gemma")


def pick_model(prov, names):
    """The best current model for quick rewriting from what the provider offers (newest of the usual family)."""
    import re
    fam = FAMILY.get(prov, "")
    good = [n for n in names if not any(b in n.lower() for b in BAD)]
    pool = [n for n in good if fam and fam in n.lower()] or good or list(names)
    stable = [n for n in pool if "preview" not in n.lower() and "exp" not in n.lower()] or pool
    ver = lambda n: tuple(int(x) for x in re.findall(r"\d+", n)[:3])
    return sorted(stable, key=lambda n: (ver(n), -len(n)), reverse=True)[0] if stable else None


AUTO = "auto"
AUTO_LABEL = "Auto — best free model (moves down when its free limit runs out)"


class QuotaHit(RuntimeError):
    """This model's free limit is used up for now; `until` = when it's expected back (epoch seconds)."""
    def __init__(self, msg, until):
        super().__init__(msg)
        self.until = until


class SlowModel(RuntimeError):
    pass


def _pacific_midnight():
    """Next midnight in California, when Google resets daily free limits (approximate summer time)."""
    import datetime as dt
    now = dt.datetime.utcnow()
    off = 7 if 3 < now.month < 11 else 8
    local = now - dt.timedelta(hours=off)
    nxt = (local + dt.timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return (nxt + dt.timedelta(hours=off) - dt.datetime(1970, 1, 1)).total_seconds()


TIERS = (("lite", 1), ("nano", 0.5), ("8b", 0.5), ("opus", 5), ("ultra", 5), ("pro", 4), ("large", 4), ("max", 4), ("sonnet", 3.5), ("plus", 3),
         ("medium", 3), ("70b", 3), ("flash", 2), ("haiku", 2), ("chat", 2), ("mini", 1.5), ("small", 1.5),
         )
AUTO_BAD = tuple(b for b in BAD if b not in ("lite", "nano", "thinking")) + ("latest", "tuning", "001", "aqa")


def rank_models(prov, names):
    """Models from most to least powerful (newest generation first, then the bigger tier)."""
    import re
    names = [n for n in names if not any(b in n.lower() for b in AUTO_BAD)]
    if prov == "openrouter":
        names = [n for n in names if n.endswith(":free")]        # only the free ones

    def score(n):
        low = n.lower()
        ver = tuple(int(x) for x in re.findall(r"\d+", re.sub(r"\d+b\b", "", low))[:2]) or (0,)
        tier = next((w for k, w in TIERS if k in low), 2)
        size = max([int(x) for x in re.findall(r"(\d+)b\b", low)] or [0])
        stable = 0 if ("preview" in low or "exp" in low) else 1
        return (ver, tier, size, stable)
    return sorted(set(names), key=score, reverse=True)


def auto_models(settings, prov, key, base):
    """The ranked list, refreshed twice a day."""
    import time as _t
    cache = dict(settings.get("writer_auto_list") or {})
    c = cache.get(prov)
    if c and _t.time() - c.get("at", 0) < 12 * 3600 and c.get("names"):
        return c["names"]
    names = rank_models(prov, list_models(settings, prov, key, base if prov == "custom" else None))
    cache[prov] = {"at": _t.time(), "names": names}
    settings.set("writer_auto_list", cache)
    return names


def auto_status(settings):
    """[(model, None or 'free limit used up until 1:05 PM')] for showing in the app."""
    import time as _t
    prov = config(settings)[0]
    cool = (settings.get("writer_cool") or {}).get(prov, {})
    out = []
    for m in ((settings.get("writer_auto_list") or {}).get(prov) or {}).get("names", []):
        u = cool.get(m, 0)
        out.append((m, None if u <= _t.time() else "resting until " + _t.strftime("%d %b %I:%M %p", _t.localtime(u))))
    return out


def rewrite(settings, instruction, text, timeout=60, log=None, deadline=None):
    """Rewrites the text. With the model set to Auto, it uses the most powerful model that still has free
    use left, steps down when a limit runs out, and goes back up once the limit has reset."""
    import time as _t
    prov, kind, base, model, key = config(settings)
    if model != AUTO:
        try:
            return _rewrite(settings, instruction, text, timeout)
        except ModelGone as e:
            # The model was retired (AI companies do this often): switch to a current one by itself and retry.
            new = e.suggestion
            if not new:
                try:
                    new = pick_model(prov, list_models(settings, prov, key, base if prov == "custom" else None))
                except Exception:
                    new = None
            if not new or new == model:
                raise RuntimeError(f"The model “{model}” isn't available. Press “Load the list of models” and pick one.")
            ms = dict(settings.get("writer_models") or {})
            ms[prov] = new
            settings.set("writer_models", ms)
            return _rewrite(settings, instruction, text, timeout)
    if not key:
        raise RuntimeError("No API key yet: add one in the app (Writing helper).")
    names = auto_models(settings, prov, key, base)
    if not names:
        raise RuntimeError("This AI didn't list any models to choose from. Pick a model yourself.")
    allcool = dict(settings.get("writer_cool") or {})
    cool = dict(allcool.get(prov) or {})
    now = _t.time()
    soonest = None
    last_err = None
    for m in names:
        if cool.get(m, 0) > now:
            soonest = min(soonest or cool[m], cool[m])
            continue
        left = (deadline - _t.time()) if deadline else 60
        if left < 4:
            break
        try:
            # Auto: give each model a short time, so a slow one is skipped quickly.
            out = _rewrite(settings, instruction, text, timeout=max(4, min(14, left - 1)), model=m, tries=1)
            settings.set("writer_last_model", m)
            return out
        except QuotaHit as e:
            cool[m] = e.until
            why = f"free limit used up until {_t.strftime('%I:%M %p', _t.localtime(e.until)).lstrip('0')}"
        except ModelGone as e:
            cool[m] = now + 7 * 86400
            why = "not available with this key"
        except SlowModel as e:
            cool[m] = now + 15 * 60
            why = "too slow right now"
        except RuntimeError as e:
            last_err = e
            if "API key was refused" in str(e):
                raise
            cool[m] = now + 10 * 60
            why = str(e)[:80]
        allcool[prov] = cool
        settings.set("writer_cool", allcool)
        if log:
            log(f"Writing helper: {m} — {why}; trying the next one.")
    when = f" The first one is back at {_t.strftime('%I:%M %p', _t.localtime(soonest)).lstrip('0')}." if soonest else ""
    raise RuntimeError(f"All free models of this AI are used up or failing right now.{when}"
                       + (f" Last error: {last_err}" if last_err and not soonest else ""))


def _rewrite(settings, instruction, text, timeout=60, model=None, tries=2):
    prov, kind, base, model0, key = config(settings)
    model = model or model0
    if not key:
        raise RuntimeError("No API key yet: add one in the app (Writing helper).")
    if not base:
        raise RuntimeError("No address set for this AI.")
    user = f"Instruction: {instruction}\n\nText:\n{text}"
    if kind == "anthropic":
        r = requests.post(f"{base}/messages", timeout=timeout, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 2048, "system": SYSTEM,
                  "messages": [{"role": "user", "content": user}]})
        _check(r)
        out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    else:
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if prov == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/sakib1322/NotionPhoneSync", "X-Title": "Notion Sync"})
        body = {"model": model, "temperature": 0.3,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
        if prov == "gemini":
            # Newer Gemini models "think" for a long time first; a rewrite doesn't need that.
            body["reasoning_effort"] = "low"
        r = None
        for attempt in range(tries + 1):
            try:
                r = requests.post(f"{base}/chat/completions", timeout=(min(15, timeout), timeout), headers=headers, json=body)
            except requests.Timeout:
                if attempt >= tries - 1:
                    raise SlowModel("The AI took too long to answer. Try again, or pick a faster model "
                                       "(names with “flash”, “mini” or “haiku”).")
                continue
            if r.status_code == 400 and "reasoning" in r.text.lower() and "reasoning_effort" in body:
                body.pop("reasoning_effort")          # this model doesn't take that setting
                continue
            break
        _check(r)
        out = r.json()["choices"][0]["message"]["content"] or ""
    out = out.strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'“”":
        out = out[1:-1].strip()
    if not out:
        raise RuntimeError("The AI sent back nothing.")
    return out


def _check(r):
    if r.ok:
        return
    try:
        j = r.json()
        if isinstance(j, list) and j:
            j = j[0]
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error") or j.get("message")
    except Exception:
        msg = r.text[:200]
    msg = str(msg or "")
    low = msg.lower()
    if r.status_code == 404 or ("model" in low and ("not found" in low or "no longer available" in low
                                                    or "does not exist" in low or "not supported" in low)):
        import re
        m = re.search(r"use (?:models/)?([\w.\-]+)", msg)
        raise ModelGone(msg, m.group(1) if m else None)
    if r.status_code in (401, 403):
        raise RuntimeError(f"The API key was refused ({msg or r.status_code}).")
    if r.status_code == 429:
        import re
        import time as _t
        raw = r.text
        if re.search(r"limit:\s*0\b", raw):
            raise QuotaHit("This model has no free use with this key.", _t.time() + 7 * 86400)
        if re.search(r"(?i)per ?day|perday|daily", raw):
            raise QuotaHit("Today's free limit is used up.", _pacific_midnight())
        m = re.search(r'retryDelay"?\s*:\s*"?(\d+(?:\.\d+)?)s', raw) or re.search(r"(?i)retry in (\d+(?:\.\d+)?)\s*s", raw)
        ra = r.headers.get("retry-after")
        secs = float(m.group(1)) if m else (float(ra) if ra and ra.replace(".", "").isdigit() else 60)
        raise QuotaHit("Too many requests right now.", _t.time() + max(20, secs))
    raise RuntimeError(f"The AI said: {msg or r.status_code}")


def list_models(settings, prov, key, base=None):
    """Model names the provider offers (for the dropdown)."""
    label, kind, dbase, _, _ = PROVIDERS.get(prov, PROVIDERS["custom"])
    base = (base or dbase).rstrip("/")
    if kind == "anthropic":
        r = requests.get(f"{base}/models", timeout=20, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        r = requests.get(f"{base}/models", timeout=20, headers={"Authorization": f"Bearer {key}"})
    _check(r)
    data = r.json().get("data") or r.json().get("models") or []
    names = []
    for m in data:
        n = m.get("id") or m.get("name") or ""
        if n.startswith("models/"):
            n = n[7:]
        if n:
            names.append(n)
    return sorted(set(names))


# ---- the keyboard watcher (Windows) ---------------------------------------------------------------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32")      # its own copy: other parts of the app set different call types
    kernel32 = ctypes.windll.kernel32
    ULONG_PTR = ctypes.c_size_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
    user32.SetWindowsHookExW.restype = wt.HHOOK
    user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.ToUnicodeEx.argtypes = [wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_ubyte), wt.LPWSTR, ctypes.c_int,
                                   wt.UINT, wt.HKL]
    user32.GetKeyboardLayout.restype = wt.HKL
    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

MAGIC = 0x4E53574B   # marks the keys we press ourselves, so the watcher ignores them
VK_BACK, VK_RETURN, VK_TAB, VK_ESCAPE, VK_SHIFT, VK_CONTROL, VK_MENU = 0x08, 0x0D, 0x09, 0x1B, 0x10, 0x11, 0x12
VK_LEFT, VK_RIGHT, VK_UP, VK_HOME, VK_END = 0x25, 0x27, 0x26, 0x24, 0x23
RESET_KEYS = {VK_RETURN, VK_TAB, VK_ESCAPE, 0x21, 0x22, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, 0x28, 0x2E}

# Apps where "select all" would grab a whole document: rewrite only the current paragraph there.
DOC_APPS = {"winword.exe", "wps.exe", "soffice.bin", "onenote.exe", "notepad.exe", "notepad++.exe", "code.exe",
            "wordpad.exe", "obsidian.exe", "notion.exe", "acrobat.exe"}
DOC_TITLES = ("Google Docs", "Word", "Notion", "Overleaf", "Docs")


def _key(vk, up=False):
    i = INPUT()
    i.type = 1
    i.u.ki = KEYBDINPUT(vk, 0, (0x2 if up else 0) | (0x1 if vk in (VK_LEFT, VK_RIGHT, VK_UP, VK_HOME, VK_END) else 0),
                        0, MAGIC)
    return i


def press(*combo):
    """Presses a key combination, e.g. press(VK_CONTROL, ord('C'))."""
    seq = [_key(k) for k in combo] + [_key(k, True) for k in reversed(combo)]
    arr = (INPUT * len(seq))(*seq)
    user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))


def _foreground():
    """(exe name, window title) of the app you're typing in."""
    h = user32.GetForegroundWindow()
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(h, title, 256)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    exe = ""
    try:
        import psutil
        exe = psutil.Process(pid.value).name().lower()
    except Exception:
        pass
    return exe, title.value


class Writer:
    def __init__(self, app):
        self.app = app
        self.s = app.settings
        self.buf = ""
        self.busy = False
        self.last_key = 0
        self.waiting = False
        self.cancel = threading.Event()
        self.hook = None
        self._proc = None
        self.thread = None

    def enabled(self):
        return bool(self.s.get("writer_on")) and os.name == "nt"

    def start(self):
        if os.name != "nt" or self.thread:
            return
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # the keyboard watcher thread

    def _install(self):
        if self.hook:
            try:
                user32.UnhookWindowsHookEx(self.hook)
            except Exception:
                pass
        self.hook = user32.SetWindowsHookExW(13, self._proc, kernel32.GetModuleHandleW(None), 0)   # WH_KEYBOARD_LL
        return bool(self.hook)

    def _loop(self):
        self._proc = HOOKPROC(self._hookproc)
        user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
        user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
        if not self._install():
            self._log(f"Writing helper couldn't watch the keyboard (error {kernel32.GetLastError()}).")
            return
        # Windows quietly drops a keyboard watcher that's ever slow to answer (e.g. while a big backup keeps
        # the app busy). So put it back every minute; it costs nothing.
        user32.SetTimer(None, 0, 60000, None)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0113 and not msg.hWnd:      # WM_TIMER for this thread
                if not self.busy:
                    self._install()
                continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _hookproc(self, code, wp, lp):
        try:
            if code == 0 and wp in (0x100, 0x104) and self.enabled():   # key down
                k = ctypes.cast(lp, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if k.dwExtraInfo != MAGIC:          # not a key we pressed ourselves
                    self.last_key = time.time()
                    if self.busy:
                        # You pressed Esc, or started editing, while the AI was working: cancel, and
                        # leave your text exactly as it is.
                        if self.waiting and k.vkCode not in (VK_SHIFT, VK_CONTROL, VK_MENU, 0xA0, 0xA1, 0xA2, 0xA3):
                            self.cancel.set()
                    else:
                        self._typed(k.vkCode, k.scanCode)
        except Exception:
            pass
        return user32.CallNextHookEx(None, code, wp, lp)

    def _typed(self, vk, scan):
        if vk in (VK_SHIFT, VK_CONTROL, VK_MENU, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x14, 0x5B, 0x5C):
            return
        if vk == VK_BACK:
            self.buf = self.buf[:-1]
            return
        down = lambda v: bool(user32.GetAsyncKeyState(v) & 0x8000)
        if vk in RESET_KEYS or down(VK_CONTROL) or down(VK_MENU) or down(0x5B) or down(0x5C):
            self.buf = ""
            return
        state = (ctypes.c_ubyte * 256)()
        if down(VK_SHIFT):                               # shift and caps lock matter for the letter
            state[VK_SHIFT] = 0x80
        state[0x14] = user32.GetKeyState(0x14) & 0x01
        out = ctypes.create_unicode_buffer(8)
        hkl = user32.GetKeyboardLayout(user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None))
        n = user32.ToUnicodeEx(vk, scan, state, out, 8, 0x4, hkl)     # 0x4: don't disturb dead keys
        if n <= 0:
            return
        self.buf = (self.buf + out.value[:n])[-40:]
        hit = match_trigger(self.s, self.buf)
        if hit:
            self._log(f"Writing helper: noticed {hit[0]} — rewriting…")
            self.buf = ""
            self.busy = True
            threading.Thread(target=self._run, args=hit, daemon=True).start()

    # doing the rewrite

    def _run(self, trig, cmd):
        import clip_sync
        saved = None
        if not config(self.s)[4]:
            self._notice("Add an AI key first: Notion Laptop Sync → Writing helper.", error=True)
            self.busy = False
            return
        try:
            time.sleep(0.08)
            for _ in trig:                                   # remove the command you typed
                press(VK_BACK)
            time.sleep(0.05)
            exe, title = _foreground()
            mode = self.s.get("writer_select") or "smart"
            paragraph = mode == "paragraph" or (mode == "smart" and (exe in DOC_APPS or any(t in title for t in DOC_TITLES)))
            try:
                saved = clip_sync.read()
            except Exception:
                saved = None
            self._pause_clip(10)
            before = clip_sync.sequence()
            if paragraph:
                press(VK_CONTROL, VK_SHIFT, VK_UP)            # select back to the start of the paragraph
            else:
                press(VK_CONTROL, ord("A"))                   # select everything in the box
            time.sleep(0.08)
            press(VK_CONTROL, ord("C"))
            text = None
            for _ in range(30):
                time.sleep(0.04)
                if clip_sync.sequence() != before:
                    got = clip_sync.read()
                    text = got[1] if got and got[0] == "text" else None
                    break
            if not text or not text.strip():
                press(VK_RIGHT)
                self._notice("Nothing to rewrite here.", error=True)
                return
            # Put the cursor back at the end (nothing stays selected while the AI works, so a stray key
            # can't wipe your text).
            press(VK_RIGHT)
            cmds = commands(self.s)
            self._notice("✨ Writing…  (press Esc to cancel)")
            box = {}

            def ask():
                try:
                    box["out"] = rewrite(self.s, cmds[cmd], text.rstrip(), log=self._log, deadline=time.time() + 35)
                except Exception as e:
                    box["err"] = e
            worker = threading.Thread(target=ask, daemon=True)
            self.cancel.clear()
            self.waiting = True
            worker.start()
            t0 = time.time()
            while worker.is_alive() and not self.cancel.is_set() and time.time() - t0 < 40:
                time.sleep(0.05)
            self.waiting = False
            if self.cancel.is_set():
                self._notice("Cancelled — your text is unchanged.", error=True)
                return
            if worker.is_alive():
                self._notice("The AI took too long. Your text is unchanged — try again.", error=True)
                return
            if "err" in box:
                self._notice(str(box["err"]), error=True)
                self._log(f"Writing helper: {box['err']}")
                return
            result = box["out"]
            if text.endswith((" ", "\n")) and not result.endswith((" ", "\n")):
                result += text[len(text.rstrip()):]
            # Select the same text again and paste the new version over it.
            if paragraph:
                press(VK_CONTROL, VK_SHIFT, VK_UP)
            else:
                press(VK_CONTROL, ord("A"))
            time.sleep(0.06)
            self._pause_clip(10)
            clip_sync.write_text(result)
            time.sleep(0.05)
            press(VK_CONTROL, ord("V"))
            time.sleep(0.4)
            self._notice(None)
        except Exception as e:
            self._log(f"Writing helper: {e}")
            self._notice(None)
        finally:
            if saved:
                time.sleep(0.3)
                try:
                    self._pause_clip(3)
                    kind, payload = saved
                    if kind == "text":
                        clip_sync.write_text(payload)
                    elif kind == "files":
                        clip_sync.write_files(payload)
                    elif kind == "image":
                        import tempfile
                        p = os.path.join(tempfile.gettempdir(), "nls-clip-restore.png")
                        payload.save(p, "PNG")
                        clip_sync.write_image(p)
                except Exception:
                    pass
            self._pause_clip(1.5)
            self.busy = False

    def _pause_clip(self, secs):
        """Keeps the shared clipboard from sending these copies/pastes to your other devices."""
        clip = getattr(self.app, "clip", None)
        if clip is not None:
            clip.pause_until = max(getattr(clip, "pause_until", 0), time.time() + secs)

    def _log(self, msg):
        self.app.events.put(lambda: self.app.log(msg))

    def _notice(self, text, error=False):
        self.app.events.put(lambda: self.app.writer_notice(text, error))


# ---- sharing AI keys between your devices ---------------------------------------------------------------
# Each device lists which AI keys it has (shown masked, like AIza…x9Q) in the devices list your devices share
# in Notion. The keys themselves are stored there encrypted with a key made from your Notion sign-in, so
# only your own signed-in devices can read them.

def _cipher(token):
    import hashlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(hashlib.sha256(("notion-sync-ai-v1|" + (token or "")).encode()).digest())


def mask(key):
    key = key or ""
    return (key[:4] + "…" + key[-3:]) if len(key) > 10 else "…"


def share_payload(settings):
    """What this device tells your other devices about its AI keys (or None if it doesn't share)."""
    import base64
    import json
    if settings.get("writer_share") is False:
        return None
    keys = {k: v for k, v in (settings.get("writer_keys") or {}).items() if v}
    if not keys or not settings.get("token"):
        return None
    models = settings.get("writer_models") or {}
    ats = settings.get("writer_keys_at") or {}
    secret = {p: {"key": k, "model": models.get(p) or "", "base": settings.get("writer_base") if p == "custom" else ""}
              for p, k in keys.items()}
    nonce = os.urandom(12)
    enc = base64.b64encode(nonce + _cipher(settings.get("token")).encrypt(nonce, json.dumps(secret).encode(), None)).decode()
    return {"providers": {p: {"mask": mask(k), "model": models.get(p) or "", "at": int(ats.get(p) or 0)}
                          for p, k in keys.items()}, "enc": enc}


def read_shared(settings, ai):
    """The keys another device shared: {provider: {"key", "model", "base"}} (empty if they can't be read)."""
    import base64
    import json
    try:
        raw = base64.b64decode(ai["enc"])
        return json.loads(_cipher(settings.get("token")).decrypt(raw[:12], raw[12:], None))
    except Exception:
        return {}


def others_keys(settings):
    """[(device, {provider: {"mask", "model", "at"}}, the device's ai block)] for your other devices that share keys."""
    import devices as _dev
    me = _dev.my_id(settings)
    out = []
    for d in _dev.cached(settings):
        if d.id == me or d.id.startswith("owner:") or d.type not in ("laptop", "phone"):
            continue
        ai = (d.info() or {}).get("ai") or {}
        if ai.get("providers"):
            out.append((d, ai["providers"], ai))
    return out


def take_key(settings, ai, prov):
    """Copies one AI key (and its model) from another device's shared block. Returns True if done."""
    import time as _t
    got = read_shared(settings, ai).get(prov)
    if not got or not got.get("key"):
        return False
    ks = dict(settings.get("writer_keys") or {})
    ks[prov] = got["key"]
    settings.set("writer_keys", ks)
    if got.get("model"):
        ms = dict(settings.get("writer_models") or {})
        ms[prov] = got["model"]
        settings.set("writer_models", ms)
    if prov == "custom" and got.get("base"):
        settings.set("writer_base", got["base"])
    ats = dict(settings.get("writer_keys_at") or {})
    ats[prov] = int(_t.time() * 1000)
    settings.set("writer_keys_at", ats)
    return True


def auto_import(settings):
    """Adds keys from your other devices for AIs this device has no key for yet (if that's switched on).
    Returns the list of (provider, device name) added."""
    if settings.get("writer_autoimport") is False:
        return []
    have = {p for p, k in (settings.get("writer_keys") or {}).items() if k}
    added = []
    for d, provs, ai in sorted(others_keys(settings), key=lambda t: -max((v.get("at") or 0) for v in t[1].values())):
        for p in provs:
            if p not in have and take_key(settings, ai, p):
                have.add(p)
                added.append((p, d.name))
    return added
