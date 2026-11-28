"""A small read-out on the Windows taskbar: your internet speed (↑ upload, ↓ download) and the
name of the virtual desktop you're on (Win+Tab desktops like "BSIL Common", "Projects")."""
import ctypes
import os
import time
import uuid

import ui

VD_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops"


# ---- what to show ------------------------------------------------------------------------------

def speed_text(bps):
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.2f} KB/s"
    if bps < 1024 ** 3:
        return f"{bps / 1024 / 1024:.2f} MB/s"
    return f"{bps / 1024 ** 3:.2f} GB/s"


class NetMeter:
    """Bytes per second sent and received, over all real network adapters."""

    def __init__(self):
        self.last = None

    def _totals(self):
        import psutil
        sent = recv = 0
        for name, c in psutil.net_io_counters(pernic=True).items():
            if "loopback" in name.lower():
                continue
            sent += c.bytes_sent
            recv += c.bytes_recv
        return sent, recv

    def read(self):
        """(upload B/s, download B/s) since the last call."""
        now = time.monotonic()
        try:
            sent, recv = self._totals()
        except Exception:
            return 0.0, 0.0
        if self.last is None:
            self.last = (now, sent, recv)
            return 0.0, 0.0
        t0, s0, r0 = self.last
        self.last = (now, sent, recv)
        dt = max(0.2, now - t0)
        return max(0, sent - s0) / dt, max(0, recv - r0) / dt


def _guid(b):
    return "{" + str(uuid.UUID(bytes_le=bytes(b))).upper() + "}"


def desktop_name():
    """The name of the virtual desktop you're on, as Windows shows it in Task View."""
    if os.name != "nt":
        return "Desktop 1"
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, VD_KEY)
    except OSError:
        return "Desktop 1"
    with k:
        try:
            raw = winreg.QueryValueEx(k, "VirtualDesktopIDs")[0] or b""
        except OSError:
            raw = b""
        ids = [_guid(raw[i:i + 16]) for i in range(0, len(raw) - 15, 16)]
        cur = None
        try:   # Windows 11
            cur = _guid(winreg.QueryValueEx(k, "CurrentVirtualDesktop")[0])
        except OSError:
            pass
    if cur is None:   # Windows 10 keeps it per sign-in session
        try:
            sid = ctypes.c_ulong()
            ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                rf"Software\Microsoft\Windows\CurrentVersion\Explorer\SessionInfo\{sid.value}\VirtualDesktops") as k2:
                cur = _guid(winreg.QueryValueEx(k2, "CurrentVirtualDesktop")[0])
        except OSError:
            pass
    if not cur:
        return "Desktop 1"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, VD_KEY + "\\Desktops\\" + cur) as k3:
            name = winreg.QueryValueEx(k3, "Name")[0]
            if name:
                return str(name)
    except OSError:
        pass
    return f"Desktop {ids.index(cur) + 1}" if cur in ids else "Desktop 1"


def desktop_state():
    """([names of all your virtual desktops in order], index of the one you're on)."""
    if os.name != "nt":
        return ["Desktop 1"], 0
    import winreg
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, VD_KEY)
    except OSError:
        return [desktop_name()], 0
    with k:
        try:
            raw = winreg.QueryValueEx(k, "VirtualDesktopIDs")[0] or b""
        except OSError:
            raw = b""
    ids = [_guid(raw[i:i + 16]) for i in range(0, len(raw) - 15, 16)]
    cur_name = desktop_name()
    if not ids:
        return [cur_name], 0
    names = []
    for n, g in enumerate(ids):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, VD_KEY + "\\Desktops\\" + g) as k3:
                nm = winreg.QueryValueEx(k3, "Name")[0]
        except OSError:
            nm = ""
        names.append(str(nm) if nm else f"Desktop {n + 1}")
    idx = names.index(cur_name) if cur_name in names else 0
    return names, idx


def hard_sleep(hibernate=False, app=None):
    """Puts the laptop to sleep (or hibernates it) right now, even when apps, browsers or USB
    dongles are keeping it awake."""
    time.sleep(0.6)                      # let the menu close first
    k32 = ctypes.windll.kernel32
    pp = ctypes.windll.powrprof
    pp.SetSuspendState.argtypes = [ctypes.c_bool, ctypes.c_bool, ctypes.c_bool]

    def tell(title, msg):
        if app is not None:
            app.events.put(lambda: (app.log(msg), app.notify(title, msg)))

    if not hibernate:
        if pp.SetSuspendState(False, True, True):      # (hibernate, force, ignore wake-up timers)
            return
        # Newer laptops ("Modern Standby", like yours) don't take that order (error 50). On those,
        # turning the screen off the way the power button does is what puts them into standby
        # (Microsoft's own suggestion).
        u = ctypes.windll.user32
        u.SendMessageTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                                          ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        WM_SYSCOMMAND, SC_MONITORPOWER, HWND_BROADCAST = 0x0112, 0xF170, 0xFFFF
        u.SendMessageTimeoutW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2, 0x0002, 2000, None)  # SMTO_ABORTIFHUNG
        return
    import battery
    err = battery.hibernate_now(app.settings if app is not None else None)
    if err:
        tell("Couldn't hibernate", err)


def sys_build():
    try:
        import sys
        return sys.getwindowsversion().build
    except Exception:
        return 0


def switch_desktop(step, count=1):
    """Goes to the next (step=1) or previous (step=-1) virtual desktop, like Ctrl+Win+→ / ←.
    count > 1 taps the arrow several times while Ctrl+Win stay held (used to wrap round from the
    last desktop to the first, or the first to the last)."""
    if os.name != "nt":
        return
    u = ctypes.windll.user32
    VK_CONTROL, VK_LWIN, KEYUP, EXT = 0x11, 0x5B, 0x2, 0x1
    arrow = 0x27 if step > 0 else 0x25
    u.keybd_event(VK_CONTROL, 0, 0, 0)
    u.keybd_event(VK_LWIN, 0, EXT, 0)
    for i in range(max(1, count)):
        u.keybd_event(arrow, 0, EXT, 0)
        u.keybd_event(arrow, 0, EXT | KEYUP, 0)
        if i + 1 < count:
            time.sleep(0.03)
    u.keybd_event(VK_LWIN, 0, EXT | KEYUP, 0)
    u.keybd_event(VK_CONTROL, 0, KEYUP, 0)


def accent_colour():
    """Windows' accent colour (the one you picked in Settings → Personalisation → Colours)."""
    if os.name != "nt":
        return (0, 103, 192)
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as k:
            v = int(winreg.QueryValueEx(k, "AccentColor")[0]) & 0xFFFFFFFF
            return (v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF)     # stored as ABGR
    except OSError:
        return (0, 103, 192)


def light_taskbar():
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return bool(winreg.QueryValueEx(k, "SystemUsesLightTheme")[0])
    except OSError:
        return False


# ---- where the taskbar is ---------------------------------------------------------------------------

class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _rect(hwnd):
    r = RECT()
    if hwnd and ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return r.left, r.top, r.right, r.bottom
    return None


def taskbar_rects():
    """(taskbar rect, notification-area rect or None), in screen pixels."""
    u = ctypes.windll.user32
    tray = u.FindWindowW("Shell_TrayWnd", None)
    bar = _rect(tray)
    note = _rect(u.FindWindowExW(tray, 0, "TrayNotifyWnd", None)) if tray else None
    if note and (note[2] - note[0] < 20 or note[3] - note[1] < 10):
        note = None
    return bar, note


SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
                 "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow", "Windows.UI.Core.CoreWindow",
                 "Shell_InputSwitchTopLevelWindow", "TaskListThumbnailWnd", "#32768", "ControlCenterWindow"}


def fullscreen_app():
    """Is a real full-screen app (video, game, slideshow) in front? Then the read-out hides.
    Windows' own pop-ups (the tray's ^ menu, Start, Quick Settings…) don't count."""
    try:
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        if state.value not in (2, 3, 4):     # busy full-screen app, full-screen game, presentation mode
            return False
        u = ctypes.windll.user32
        fg = u.GetForegroundWindow()
        if not fg:
            return False
        cls = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(fg, cls, 128)
        if cls.value in SHELL_CLASSES:
            return False
        r = _rect(fg)
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        return bool(r) and r[0] <= 0 and r[1] <= 0 and r[2] >= sw and r[3] >= sh
    except Exception:
        return False


# ---- the read-out window -----------------------------------------------------------------------------

# ---- the read-out: a native, see-through Windows window ------------------------------------------------
# (Drawn with Windows' own layered-window API, like TrafficMonitor does: it always sits above the
#  taskbar, never takes focus, shows on every virtual desktop and has smooth, sharp text.)

if os.name == "nt":
    from ctypes import wintypes as wt
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                    ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
                    ("time", wt.DWORD), ("pt", wt.POINT)]

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    kernel32 = ctypes.windll.kernel32
    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
    user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                           ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
    user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID]
    user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
    user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    user32.GetParent.argtypes = [wt.HWND]
    user32.GetParent.restype = wt.HWND
    user32.IsWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.GetWindow.restype = wt.HWND
    user32.FindWindowW.restype = wt.HWND
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetDC.argtypes = [wt.HWND]
    user32.GetDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    gdi32.CreateCompatibleDC.restype = wt.HDC
    gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
    gdi32.CreateDIBSection.restype = wt.HBITMAP
    gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
    gdi32.SelectObject.restype = wt.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wt.HDC]
    kernel32.GetModuleHandleW.restype = wt.HMODULE

WM_DESTROY, WM_TIMER, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEMOVE = 0x0002, 0x0113, 0x0201, 0x0202, 0x0200
WM_LBUTTONDBLCLK, WM_RBUTTONUP, WM_CLOSE, WM_APP = 0x0203, 0x0205, 0x0010, 0x8000
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_NOACTIVATE = 0x80000, 0x80, 0x8, 0x08000000


_fonts = {}


def _font(size, bold=False):
    """Windows 11's own font (Segoe UI Variable), semibold for the desktop name; older fonts as a fallback."""
    key = (size, bold)
    if key in _fonts:
        return _fonts[key]
    from PIL import ImageFont
    f = None
    try:
        f = ImageFont.truetype("SegUIVar.ttf", size)
        axes = {a.get("name", b"").decode() if isinstance(a.get("name"), bytes) else str(a.get("name")): a
                for a in f.get_variation_axes()}
        vals = []
        for a in f.get_variation_axes():
            n = a.get("name")
            n = n.decode() if isinstance(n, bytes) else str(n)
            if n.lower().startswith("weight") or n.lower() == "wght":
                vals.append(600 if bold else 400)
            elif n.lower().startswith("optical") or n.lower() == "opsz":
                vals.append(max(a["minimum"], min(a["maximum"], size * 0.75)))
            else:
                vals.append(a.get("default", a["minimum"]))
        f.set_variation_by_axes(vals)
    except Exception:
        f = None
    if f is None:
        for name in (("seguisb.ttf", "segoeuib.ttf") if bold else ("segoeui.ttf",)) + ("arial.ttf", "DejaVuSans.ttf"):
            try:
                f = ImageFont.truetype(name, size)
                break
            except Exception:
                pass
    f = f or ImageFont.load_default()
    _fonts[key] = f
    return f


# ---- Bluetooth device icons --------------------------------------------------------------------------

KINDS = [
    ("headphones", ("bud", "headphone", "headset", "airpod", "earbud", "ear ", "wh-", "wf-", "jbl", "bose", "beats",
                    "soundcore", "sony", "galaxy buds", "freebuds", "pods", "audio", "hands-free", "speaker", "boom")),
    ("mouse", ("mouse", "mx master", "mx anywhere", "mx ergo", "trackball", "pebble")),
    ("keyboard", ("keyboard", "keys", "mx keys", "k380", "k780")),
    ("gamepad", ("controller", "gamepad", "xbox", "dualsense", "dualshock", "joy-con", "8bitdo", "wireless controller")),
    ("pen", ("pen", "stylus")),
    ("phone", ("phone", "galaxy s", "iphone", "pixel", "redmi", "oneplus")),
    ("watch", ("watch", "band", "fit")),
]


def device_kind(name):
    n = name.lower()
    for kind, words in KINDS:
        if any(w in n for w in words):
            return kind
    return "bluetooth"


def device_icon(kind, size, colour):
    """A small outline icon (headphones, mouse, keyboard, controller…) in the given colour."""
    from PIL import Image, ImageDraw
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    lw = max(k, round(S * .085))
    c = colour

    def rr(box, r, fill=None):
        d.rounded_rectangle([S * box[0], S * box[1], S * box[2], S * box[3]], radius=S * r,
                            outline=None if fill else c, width=lw, fill=c if fill else None)
    if kind == "headphones":
        d.arc([S * .12, S * .08, S * .88, S * .84], 180, 360, fill=c, width=lw)
        d.line([S * .12 + lw / 2, S * .46, S * .12 + lw / 2, S * .62], fill=c, width=lw)
        d.line([S * .88 - lw / 2, S * .46, S * .88 - lw / 2, S * .62], fill=c, width=lw)
        rr((.10, .52, .32, .94), .07, fill=True)
        rr((.68, .52, .90, .94), .07, fill=True)
    elif kind == "mouse":
        rr((.25, .06, .75, .94), .25)
        d.line([S * .5, S * .14, S * .5, S * .36], fill=c, width=lw)
    elif kind == "keyboard":
        rr((.04, .22, .96, .78), .10)
        for row, n in ((.38, 5), (.52, 5)):
            for i in range(n):
                x = S * (.18 + i * .16)
                d.rectangle([x - lw * .6, S * row - lw * .6, x + lw * .6, S * row + lw * .6], fill=c)
        d.line([S * .30, S * .65, S * .70, S * .65], fill=c, width=lw)
    elif kind == "gamepad":
        rr((.04, .24, .96, .78), .26)
        d.line([S * .18, S * .51, S * .40, S * .51], fill=c, width=lw)
        d.line([S * .29, S * .40, S * .29, S * .62], fill=c, width=lw)
        for (px, py) in ((.66, .44), (.78, .58)):
            d.ellipse([S * px - lw, S * py - lw, S * px + lw, S * py + lw], fill=c)
    elif kind == "pen":
        d.line([S * .22, S * .78, S * .72, S * .28], fill=c, width=lw * 2)
        d.polygon([(S * .12, S * .88), (S * .18, S * .70), (S * .30, S * .82)], fill=c)
    elif kind == "phone":
        rr((.28, .06, .72, .94), .10)
        d.line([S * .44, S * .84, S * .56, S * .84], fill=c, width=lw)
    elif kind == "watch":
        rr((.26, .24, .74, .76), .14)
        rr((.36, .04, .64, .24), .03)
        rr((.36, .76, .64, .96), .03)
    else:   # the Bluetooth rune
        pts = [(S * .28, S * .30), (S * .70, S * .66), (S * .50, S * .86), (S * .50, S * .14), (S * .70, S * .34),
               (S * .28, S * .70)]
        d.line(pts, fill=c, width=lw, joint="curve")
    return im.resize((size, size), Image.LANCZOS)


class TaskbarMeter:
    """Internet speed, desktop name and Bluetooth batteries, drawn on the taskbar near the clock."""

    def __init__(self, app):
        self.app = app
        self.s = app.settings
        self.net = NetMeter()
        self.hwnd = None
        self.bt = []
        self.up_text = self.down_text = ""
        self.desk_text = ""
        self.visible = False
        self.menu_open = False
        self.embedded = False
        self._bar = None
        self.drag = None
        self.size = (0, 0)
        self.errors = 0
        self.wake = None
        if os.name == "nt":
            import threading
            threading.Thread(target=self._bt_loop, daemon=True).start()
            threading.Thread(target=self._run, daemon=True).start()

    # compatibility with the settings page / self-test
    @property
    def win(self):
        return self.hwnd

    def wanted(self):
        return bool(self.s.get("taskbar_speed") or self.s.get("taskbar_desktop") or self.s.get("taskbar_bt"))

    def refresh_settings(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP, 0, 0)    # redraw now

    def _ui(self, fn):
        self.app.events.put(fn)

    def _log(self, msg):
        self.errors += 1
        if self.errors <= 3:
            self._ui(lambda: self.app.log("Taskbar read-out: " + msg))

    # background: Bluetooth batteries once a minute

    def _bt_loop(self):
        import battery
        last = None
        while not getattr(self.app, "quitting", False):
            if self.s.get("taskbar_bt"):
                try:
                    self.bt = battery.bt_levels()
                    note = (", ".join(f"{n} {b}%" for n, b in self.bt) if self.bt else
                            "no connected device is reporting its battery to Windows")
                except Exception as e:
                    self.bt = []
                    note = f"couldn't check ({e})"
                names = tuple(n for n, _ in self.bt) if self.bt else note
                if names != last:          # say what's found when it changes (not every minute)
                    last = names
                    self._ui(lambda n=note: self.app.log("Bluetooth battery: " + n))
                if self.hwnd:
                    user32.PostMessageW(self.hwnd, WM_APP, 0, 0)
            for _ in range(60):
                if getattr(self.app, "quitting", False):
                    return
                time.sleep(1)

    # the window's own thread

    def _run(self):
        """Creates the read-out as part of the taskbar itself (like TrafficMonitor), so it can never be
        covered by it — no blinking when you hover app icons or open the ^ pop-up. If Explorer restarts,
        it's created again."""
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._proc = WNDPROC(self._wndproc)       # keep a reference, or Python frees it
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.style = 0x8                            # double-clicks
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
            wc.lpszClassName = "NotionSyncTaskbarMeter"
            user32.RegisterClassExW(ctypes.byref(wc))
        except Exception as e:
            self._log(str(e))
            return
        while not getattr(self.app, "quitting", False):
            try:
                self._run_once(hinst)
            except Exception as e:
                self._log(str(e))
            self.hwnd = None
            self.visible = False
            for _ in range(30):                       # Explorer restarting: try again shortly
                if getattr(self.app, "quitting", False):
                    return
                time.sleep(0.1)

    def _run_once(self, hinst):
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        self.embedded = bool(tray) and not self.s.get("taskbar_float") and not getattr(self, "_embed_failed", False)
        if self.embedded:
            self.hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                                               "NotionSyncTaskbarMeter", "Notion Sync taskbar",
                                               0x40000000 | 0x04000000,          # WS_CHILD | WS_CLIPSIBLINGS
                                               0, 0, 10, 10, tray, None, hinst, None)
            if not self.hwnd:
                self._embed_failed = True
                self.embedded = False
        if not self.embedded:
            self.hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                                               "NotionSyncTaskbarMeter", "Notion Sync taskbar", WS_POPUP,
                                               0, 0, 10, 10, None, None, hinst, None)
        if not self.hwnd:
            self._log(f"couldn't create the window (error {kernel32.GetLastError()})")
            time.sleep(5)
            return
        self.visible = False
        user32.SetTimer(self.hwnd, 1, 1000, None)        # speed etc.: every second
        user32.SetTimer(self.hwnd, 2, 250 if self.embedded else 100, None)   # stay on top
        if not self.embedded:
            # React the instant something else comes to the front (e.g. the tray's ^ pop-up).
            WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
            self._hookproc = WINEVENTPROC(lambda *a: self._on_front_change())
            user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
            user32.SetWinEventHook.restype = wt.HANDLE
            self._hooks = [user32.SetWinEventHook(ev, ev, None, self._hookproc, 0, 0, 0)
                           for ev in (0x0003, 0x0006, 0x0007)]
        self._tick()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for hk in getattr(self, "_hooks", []) or []:
            try:
                user32.UnhookWinEvent(hk)
            except Exception:
                pass
        self._hooks = []

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TIMER:
                if getattr(self.app, "quitting", False):
                    user32.DestroyWindow(hwnd)
                elif wp == 1:
                    self._tick()
                elif wp == 3:
                    user32.KillTimer(hwnd, 3)
                    self._tick(read_net=False)
                elif wp == 4:
                    self._anim_frame()
                elif wp == 2 and self.visible and not self.drag and not self.menu_open:
                    if self.embedded:
                        if not user32.IsWindow(user32.GetParent(hwnd)):
                            user32.DestroyWindow(hwnd)       # Explorer restarted: start again
                        elif user32.GetWindow(user32.GetParent(hwnd), 5) != hwnd:     # GW_CHILD = top child
                            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                    else:
                        self._raise_if_covered()
                return 0
            if msg == WM_APP:
                self._tick(read_net=False)
                return 0
            if msg == WM_LBUTTONDOWN:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                r = _rect(hwnd)
                self.drag = (pt.x, r[0] if r else 0, False)
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_MOUSEMOVE and self.drag:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                x0, wx, _ = self.drag
                if abs(pt.x - x0) > 3:
                    self.drag = (x0, wx, True)
                    r = _rect(hwnd)
                    bar, note = taskbar_rects()
                    nx = wx + pt.x - x0
                    if bar and r:
                        lim = (note[0] - ui.px(4)) if note else bar[2]
                        nx = max(bar[0], min(nx, lim - (r[2] - r[0])))
                    if self.embedded and bar:
                        user32.SetWindowPos(hwnd, 0, nx - bar[0], 0, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
                    else:
                        user32.SetWindowPos(hwnd, HWND_TOPMOST, nx, r[1] if r else 0, 0, 0,
                                            SWP_NOSIZE | SWP_NOACTIVATE)
                return 0
            if msg == WM_LBUTTONUP and self.drag:
                moved = self.drag[2]
                self.drag = None
                user32.ReleaseCapture()
                span = getattr(self, "_name_span", None)
                if not moved and span:
                    cx, cy = ctypes.c_short(lp & 0xFFFF).value, ctypes.c_short((lp >> 16) & 0xFFFF).value
                    if span[0] <= cx <= span[1]:
                        h = self.size[1] or 48
                        if cy < h * 0.32:
                            self._go(-1)        # clicked the name above: previous desktop
                        elif cy > h * 0.68:
                            self._go(1)         # the name below: next desktop
                    return 0
                if moved:
                    bar, _ = taskbar_rects()
                    r = _rect(hwnd)
                    if bar and r:
                        off = bar[2] - r[2]
                        self.s.set("taskbar_offset", off)
                        self._offset = off
                return 0
            if msg == 0x020A:     # mouse wheel: over the desktop name, switch desktops
                span = getattr(self, "_name_span", None)
                r = _rect(hwnd)
                sx = ctypes.c_short(lp & 0xFFFF).value
                if span and r and r[0] + span[0] <= sx <= r[0] + span[1]:
                    delta = ctypes.c_short((wp >> 16) & 0xFFFF).value
                    now = time.monotonic()
                    if now - getattr(self, "_last_switch", 0) > 0.3:
                        self._last_switch = now
                        self._go(-1 if delta > 0 else 1)
                return 0
            if msg == WM_LBUTTONDBLCLK:
                self._ui(self.app.show_window)
                return 0
            if msg == WM_RBUTTONUP:
                self._menu(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            self._log(str(e))
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _menu_owner(self):
        """An invisible window of our own to own the right-click menu (the read-out itself lives
        inside the taskbar, which belongs to Windows)."""
        o = getattr(self, "_owner", None)
        if o and user32.IsWindow(o):
            return o
        self._owner = user32.CreateWindowExW(WS_EX_TOOLWINDOW, "NotionSyncTaskbarMeter", "", WS_POPUP,
                                             -100, -100, 1, 1, None, None, kernel32.GetModuleHandleW(None), None)
        return self._owner

    def _menu(self, hwnd):
        m = user32.CreatePopupMenu()
        items = [(1, "Open Notion Laptop Sync"), (0, None),
                 (2, ("Hide" if self.s.get("taskbar_speed") else "Show") + " internet speed"),
                 (3, ("Hide" if self.s.get("taskbar_desktop") else "Show") + " desktop name"),
                 (4, ("Hide" if self.s.get("taskbar_bt") else "Show") + " Bluetooth battery"),
                 (0, None), (5, "Put it back next to the clock"),
                 (0, None), (6, "Sleep now (force)"), (7, "Hibernate now (deepest, saves the most battery)")]
        for cid, text in items:
            user32.AppendMenuW(m, 0x800 if cid == 0 else 0, cid, text)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        owner = self._menu_owner()
        user32.SetForegroundWindow(owner)       # so the menu closes when you click elsewhere
        self.menu_open = True
        try:
            cmd = user32.TrackPopupMenu(m, 0x0100 | 0x0002 | 0x0020, pt.x, pt.y, 0, owner, None)  # RETURNCMD|RIGHTBUTTON|BOTTOMALIGN
        finally:
            self.menu_open = False
        user32.PostMessageW(owner, 0, 0, 0)
        user32.DestroyMenu(m)
        keys = {2: "taskbar_speed", 3: "taskbar_desktop", 4: "taskbar_bt"}
        if cmd == 1:
            self._ui(self.app.show_window)
        elif cmd in keys:
            k = keys[cmd]
            new = not self.s.get(k)
            self.s.set(k, new)          # (settings are safe to change from this thread)

            def tick_box(k=k, new=new):
                try:
                    self.app.taskbar_vars[k].set(new)
                except Exception:
                    pass
            self._ui(tick_box)
            self._tick(read_net=False)
        elif cmd in (6, 7):
            import threading
            threading.Thread(target=hard_sleep, args=(cmd == 7, self.app), daemon=True).start()
        elif cmd == 5:
            self._offset = None
            self.s.set("taskbar_offset", None)
            self._tick(read_net=False)

    # what's drawn

    def _tick(self, read_net=True):
        if read_net:
            up, down = self.net.read()
            self.up_text, self.down_text = "↑: " + speed_text(up), "↓: " + speed_text(down)
        if self.s.get("taskbar_desktop"):
            try:
                names, idx = desktop_state()
            except Exception:
                names, idx = [""], 0
            old = getattr(self, "desk_idx", None)
            tgt = getattr(self, "_target", None)
            if tgt is not None and idx != tgt and time.monotonic() - getattr(self, "_anim_t0", 0) < getattr(self, "_hold", 0.8):
                idx = tgt                       # Windows hasn't caught up with the switch yet
            self.desk_names = names
            if old is not None and idx != old and not self._animating_to(idx):
                d, n = old - idx, len(names)
                if n > 2 and abs(d) > n / 2:    # the short way round the circle
                    d -= n if d > 0 else -n
                self._slide(d)                  # moved by keyboard / Task View: slide too
            self.desk_idx = idx
            self.desk_text = names[idx] if names else ""
        self._place()

    # the sliding desktop names

    def _go(self, step):
        names = getattr(self, "desk_names", None) or [self.desk_text]
        idx = getattr(self, "desk_idx", 0)
        n = len(names)
        if n < 2:
            return
        new = (idx + step) % n                   # a full circle: after the last comes the first
        if new == idx + step:
            switch_desktop(step)
            self._hold = 0.8
        else:
            # Windows can't wrap round by itself: go the other way, all the way.
            switch_desktop(-step, count=n - 1)
            self._hold = 0.8 + 0.25 * (n - 1)
        self.desk_idx = new
        self.desk_text = names[new]
        self._target = new
        self._slide(-step)
        user32.SetTimer(self.hwnd, 3, 400, None)   # then check what Windows says

    def _animating_to(self, idx):
        return getattr(self, "_target", None) == idx and \
            time.monotonic() - getattr(self, "_anim_t0", 0) < max(1.0, getattr(self, "_hold", 0.8) + 0.2)

    def _slide(self, start):
        """Starts a smooth slide: the names start `start` rows away and glide into place."""
        self._anim_from = getattr(self, "anim_off", 0.0) + start
        self.anim_off = self._anim_from
        self._anim_t0 = time.monotonic()
        user32.SetTimer(self.hwnd, 4, 15, None)    # ~60 frames a second

    def _anim_frame(self):
        t = (time.monotonic() - self._anim_t0) / 0.22
        if t >= 1:
            self.anim_off = 0.0
            user32.KillTimer(self.hwnd, 4)
        else:
            self.anim_off = self._anim_from * (1 - t) ** 3      # ease-out
        self._place()

    def _hide(self):
        if self.visible:
            user32.ShowWindow(self.hwnd, 0)
            self.visible = False

    def _place(self):
        if not self.hwnd or self.drag:
            return
        if not self.wanted() or getattr(self.app, "quitting", False):
            return self._hide()
        bar, note = taskbar_rects()
        sh = user32.GetSystemMetrics(1)
        away = (not bar or bar[1] >= sh - 2 or not 20 <= bar[3] - bar[1] <= 200 or
                (not self.embedded and fullscreen_app()))     # (as part of the taskbar, it hides with it)
        now = time.monotonic()
        if away:
            # Only hide if it stays that way for a moment: tray pop-ups and menus opening/closing
            # briefly look like this, and hiding for them made it flicker.
            if not getattr(self, "_away_since", None):
                self._away_since = now
            if now - self._away_since > 1.5:
                return self._hide()
            return
        self._away_since = None
        # Keep a steady position: the tray area briefly changes size while its ^ pop-up opens/closes.
        # Windows 11 sometimes leaves the ^ arrow out of the tray area it reports, so remember the
        # furthest-left edge seen in the last few minutes and stay left of that.
        if note:
            hist = getattr(self, "_note_hist", [])
            hist = [(t, l) for t, l in hist if now - t < 300] + [(now, note[0])]
            self._note_hist = hist
            low = min(l for _, l in hist)
            if getattr(self, "_note_low", None) is None or low < self._note_low:
                self._note_low = low
            first = getattr(self, "_note_first", None)
            if first is None:
                first = self._note_first = note[0]
            # Until the ^ arrow has shown up in the reported area once, leave room for it.
            extra = ui.px(40) if (self._note_low >= first and sys_build() >= 22000) else 0
            note = (min(low, self._note_low) - extra,) + tuple(note[1:])
        h = bar[3] - bar[1]
        img = self.render(h, light_taskbar())
        w = img.width
        right = note[0] if note else bar[2] - ui.px(330)
        off = getattr(self, "_offset", "unset")
        if off == "unset":
            off = self._offset = self.s.get("taskbar_offset")
        x = (bar[2] - int(off) - w) if off is not None else (right - w - ui.px(4))
        limit = (note[0] - ui.px(4)) if note else bar[2]      # never on top of the tray icons
        x = max(bar[0], min(x, limit - w))
        self._bar = bar
        self._blit(img, x, bar[1])
        if not self.visible:
            if self.embedded:
                user32.ShowWindow(self.hwnd, 8)       # SW_SHOWNA
            else:
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.visible = True
        elif not self.menu_open and not self.embedded:
            self._raise_if_covered()

    def _on_front_change(self):
        try:
            if self.visible and not self.drag and not self.menu_open and not fullscreen_app():
                self._raise_if_covered()
        except Exception:
            pass

    def _raise_if_covered(self):
        """Comes back above the taskbar when the taskbar has covered it — but stays below menus and
        pop-ups (its own right-click menu, the tray's ^ menu, Start, notifications…)."""
        GW_HWNDPREV = 3
        tray = user32.FindWindowW("Shell_TrayWnd", None)
        h = self.hwnd
        for _ in range(500):
            h = user32.GetWindow(h, GW_HWNDPREV)
            if not h:
                return
            if h == tray:
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                return

    def render(self, h, light):
        """Draws the read-out (a see-through picture) for a taskbar h pixels tall."""
        from PIL import Image, ImageDraw
        fg = (26, 26, 26, 255) if light else (255, 255, 255, 255)
        red = (215, 21, 38, 255) if light else (255, 92, 92, 255)
        small, big = _font(max(10, round(12 * ui.S))), _font(max(11, round(14 * ui.S)), bold=True)
        pct = _font(max(12, round(15 * ui.S)))
        tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        gap, pad = ui.px(16), ui.px(6)
        isz = max(14, round(20 * ui.S))          # device icon size
        parts = []      # (kind, data, width)
        self._name_span = None
        if self.s.get("taskbar_desktop") and self.desk_text:
            names = getattr(self, "desk_names", None) or [self.desk_text]
            idx = min(getattr(self, "desk_idx", 0), len(names) - 1)
            near = [names[i] for i in range(max(0, idx - 1), min(len(names), idx + 2))]
            name_font = _font(max(11, round(13 * ui.S)), bold=True)
            dw = (ui.px(4) + ui.px(8) + ui.px(10) * 2 +
                  max(int(tmp.textlength(n, font=name_font)) + 1 for n in near))
            parts.append(("desk", (names, idx), dw))
        if self.s.get("taskbar_bt") and self.bt:
            items = []
            for name, b in self.bt[:5]:
                t = f"{b}%"
                items.append((device_kind(name), t, b, int(tmp.textlength(t, font=pct)) + 1))
            w = sum(isz + ui.px(5) + tw for _, _, _, tw in items) + ui.px(12) * (len(items) - 1)
            parts.append(("bt", items, w))
        if self.s.get("taskbar_speed"):
            w = int(max(tmp.textlength(t, font=small) for t in (self.up_text, self.down_text, "↓: 888.88 KB/s"))) + 1
            parts.append(("speed", (self.up_text, self.down_text), w))
        w = max(1, sum(p[2] for p in parts) + gap * max(0, len(parts) - 1) + pad * 2)
        im = Image.new("RGBA", (w, h), (0, 0, 0, 1))     # almost invisible, but you can still grab it
        d = ImageDraw.Draw(im)
        x = pad
        for kind, data, pw in parts:
            if kind == "desk":
                self._name_span = (x - pad, x + pw + gap // 2)
                self._draw_wheel(im, data, x, pw, h, fg)
            elif kind == "speed":
                asc, desc = small.getmetrics()
                lh = asc + desc - ui.px(2)
                y = (h - lh * 2) // 2
                for t in data:
                    d.text((x, y), t, font=small, fill=fg)
                    y += lh
            else:
                cx = x
                asc, desc = pct.getmetrics()
                for dk, t, b, tw in data:
                    col = red if b <= 20 else fg        # red when it's time to charge
                    im.alpha_composite(device_icon(dk, isz, col), (cx, (h - isz) // 2))
                    cx += isz + ui.px(5)
                    d.text((cx, (h - asc - desc) // 2), t, font=pct, fill=col)
                    cx += tw + ui.px(12)
            x += pw + gap
        return im

    def _draw_wheel(self, im, data, x, pw, h, fg):
        """Windows 11-style desktop switcher: a soft accent pill holds the desktop you're on, the
        previous/next names fade in above and below, and a dot column shows where you are.
        Everything glides when you switch."""
        from PIL import Image, ImageDraw
        names, idx = data
        off = getattr(self, "anim_off", 0.0)
        light = fg[0] < 128
        acc = getattr(self, "_accent", None)
        if acc is None or time.monotonic() - getattr(self, "_accent_t", 0) > 30:
            acc = self._accent = accent_colour()
            self._accent_t = time.monotonic()
        k = 4                                     # draw shapes 4x bigger, then shrink: smooth edges
        dot_w, gap, padx = ui.px(4), ui.px(8), ui.px(10)
        shapes = Image.new("RGBA", (pw * k, h * k), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shapes)
        # the pill
        ph = max(ui.px(22), int(h * 0.46))
        px0 = dot_w + gap
        pill = (px0 * k, (h - ph) // 2 * k, (pw - 1) * k, ((h - ph) // 2 + ph) * k)
        sd.rounded_rectangle(pill, radius=ph * k // 2,
                             fill=acc + ((46,) if light else (70,)))
        # the dots (one per desktop, the current one is a longer accent bar that slides)
        n = len(names)
        if n > 1:
            sp = min(ui.px(7), (h - ui.px(10)) / max(1, n - 1 + 2))
            top = h / 2 - sp * (n - 1) / 2 - sp / 2
            cur = idx - off
            for i in range(n):
                cy = top + sp / 2 + i * sp
                if abs(i - cur) < 0.5:
                    continue
                r = dot_w / 2 * 0.75
                sd.ellipse([(dot_w / 2 - r) * k, (cy - r) * k, (dot_w / 2 + r) * k, (cy + r) * k],
                           fill=fg[:3] + (90,))
            cy = top + sp / 2 + cur * sp
            sd.rounded_rectangle([0, (cy - sp * 0.9) * k, dot_w * k, (cy + sp * 0.9) * k], radius=dot_w * k // 2,
                                 fill=acc + (255,))
        im.alpha_composite(shapes.resize((pw, h), Image.LANCZOS), (x, 0))
        # the names
        step = h * 0.36
        big_px, small_px = max(11, round(13 * ui.S)), max(8, round(9.5 * ui.S))
        tx = x + px0 + padx
        for i, name in enumerate(names):
            d = i - idx
            if n > 2:                            # a circle: the last desktop sits above the first
                d = (d + n // 2) % n - n // 2
            dpos = d + off
            a = abs(dpos)
            if a > 1.4:
                continue
            kk = min(1.0, a)
            size = round(big_px + (small_px - big_px) * kk)
            font = _font(size, bold=a < 0.5)
            alpha = (1 - 0.62 * kk) if a <= 1 else 0.38 * max(0.0, (1.4 - a) / 0.4)
            asc, desc = font.getmetrics()
            cy = h / 2 + dpos * step
            layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).text((tx, cy - (asc + desc) / 2 + ui.px(1)), name, font=font,
                                       fill=fg[:3] + (int(255 * alpha),))
            im.alpha_composite(layer)

    def _blit(self, im, x, y):
        import numpy as np
        w, h = im.size
        a = np.asarray(im, dtype=np.uint16)
        alpha = a[..., 3:4]
        rgb = (a[..., :3] * alpha // 255).astype(np.uint8)
        bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], a[..., 3].astype(np.uint8)])
        data = np.ascontiguousarray(bgra).tobytes()
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        bi = BITMAPINFOHEADER()
        bi.biSize, bi.biWidth, bi.biHeight = ctypes.sizeof(BITMAPINFOHEADER), w, -h
        bi.biPlanes, bi.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(mem, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, data, len(data))
            old = gdi32.SelectObject(mem, bmp)
            blend = BLENDFUNCTION(0, 0, 255, 1)
            if self.embedded:
                bar = self._bar
                user32.SetWindowPos(self.hwnd, 0, x - bar[0], y - bar[1], w, h, SWP_NOACTIVATE)
                ok = user32.UpdateLayeredWindow(self.hwnd, screen, None, ctypes.byref(wt.SIZE(w, h)),
                                                mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
                if not ok:
                    # This Windows doesn't allow it inside the taskbar: float above it instead.
                    self._embed_failed = True
                    self._log(f"couldn't draw inside the taskbar (error {kernel32.GetLastError()}); floating above it")
                    user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            else:
                user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                                           mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
            gdi32.SelectObject(mem, old)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
        self.size = (w, h)
