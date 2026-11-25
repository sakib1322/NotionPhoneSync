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
    "gemini": ("Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash",
               "aistudio.google.com/apikey"),
    "claude": ("Claude (Anthropic)", "anthropic", "https://api.anthropic.com/v1", "claude-haiku-5-5",
               "console.anthropic.com"),
    "openai": ("ChatGPT (OpenAI)", "openai", "https://api.openai.com/v1", "gpt-4.1-mini", "platform.openai.com/api-keys"),
    "deepseek": ("DeepSeek", "openai", "https://api.deepseek.com/v1", "deepseek-chat", "platform.deepseek.com"),
    "qwen": ("Qwen (Alibaba)", "openai", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen-plus",
             "bailian.console.alibabacloud.com"),
    "grok": ("Grok (xAI)", "openai", "https://api.x.ai/v1", "grok-3-mini", "console.x.ai"),
    "groq": ("Groq (fast, free tier)", "openai", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile",
             "console.groq.com/keys"),
    "mistral": ("Mistral", "openai", "https://api.mistral.ai/v1", "mistral-small-latest", "console.mistral.ai"),
    "openrouter": ("OpenRouter (hundreds of models)", "openai", "https://openrouter.ai/api/v1", "openrouter/auto",
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


def config(settings):
    prov = settings.get("writer_provider") or "gemini"
    label, kind, base, model, _ = PROVIDERS.get(prov, PROVIDERS["custom"])
    keys = settings.get("writer_keys") or {}
    models = settings.get("writer_models") or {}
    if prov == "custom":
        base = (settings.get("writer_base") or "").rstrip("/")
    return prov, kind, base, (models.get(prov) or model), keys.get(prov) or ""


def rewrite(settings, instruction, text, timeout=45):
    prov, kind, base, model, key = config(settings)
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
        r = requests.post(f"{base}/chat/completions", timeout=timeout, headers=headers, json={
            "model": model, "temperature": 0.3,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]})
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
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error") or j.get("message")
    except Exception:
        msg = r.text[:200]
    if r.status_code in (401, 403):
        raise RuntimeError(f"The API key was refused ({msg or r.status_code}).")
    if r.status_code == 429:
        raise RuntimeError("Too many requests or out of credit for this AI right now.")
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

    user32 = ctypes.windll.user32
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

    def _loop(self):
        self._proc = HOOKPROC(self._hookproc)
        self.hook = user32.SetWindowsHookExW(13, self._proc, kernel32.GetModuleHandleW(None), 0)   # WH_KEYBOARD_LL
        if not self.hook:
            self._log(f"Writing helper couldn't watch the keyboard (error {kernel32.GetLastError()}).")
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _hookproc(self, code, wp, lp):
        try:
            if code == 0 and wp in (0x100, 0x104) and self.enabled() and not self.busy:   # key down
                k = ctypes.cast(lp, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if k.dwExtraInfo != MAGIC and not (k.flags & 0x10):      # not a key we (or another app) faked
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
        for trig in sorted(commands(self.s), key=len, reverse=True):
            if self.buf.endswith(trig):
                before = self.buf[: -len(trig)]
                if before and not before[-1].isspace():
                    continue                                # "a/im" inside a word or link: ignore
                self.buf = ""
                self.busy = True
                threading.Thread(target=self._run, args=(trig,), daemon=True).start()
                return

    # doing the rewrite

    def _run(self, trig):
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
            cmds = commands(self.s)
            self._notice("✨ Writing…")
            try:
                result = rewrite(self.s, cmds[trig], text.rstrip())
            except Exception as e:
                press(VK_RIGHT)
                self._notice(str(e), error=True)
                self._log(f"Writing helper: {e}")
                return
            if text.endswith((" ", "\n")) and not result.endswith((" ", "\n")):
                result += text[len(text.rstrip()):]
            self._pause_clip(10)
            clip_sync.write_text(result)
            time.sleep(0.05)
            press(VK_CONTROL, ord("V"))
            time.sleep(0.5)
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
