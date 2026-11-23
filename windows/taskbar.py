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


def fullscreen_app():
    """Is a full-screen app (video, game, slideshow) in front? Then the read-out hides."""
    u = ctypes.windll.user32
    fg = u.GetForegroundWindow()
    if not fg:
        return False
    cls = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(fg, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return False
    r = _rect(fg)
    if not r:
        return False
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    return r[0] <= 0 and r[1] <= 0 and r[2] >= sw and r[3] >= sh


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


def _font(size, bold=False):
    from PIL import ImageFont
    for f in (("segoeuib.ttf", "seguisb.ttf") if bold else ("segoeui.ttf",)) + ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except Exception:
            pass
    return ImageFont.load_default()


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
        while not getattr(self.app, "quitting", False):
            if self.s.get("taskbar_bt"):
                try:
                    self.bt = battery.bt_levels()
                except Exception:
                    self.bt = []
            for _ in range(60):
                if getattr(self.app, "quitting", False):
                    return
                time.sleep(1)

    # the window's own thread

    def _run(self):
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
            self.hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                                               "NotionSyncTaskbarMeter", "Notion Sync taskbar", WS_POPUP,
                                               0, 0, 10, 10, None, None, hinst, None)
            if not self.hwnd:
                self._log(f"couldn't create the window (error {kernel32.GetLastError()})")
                return
            user32.SetTimer(self.hwnd, 1, 1000, None)     # speed etc.: every second
            user32.SetTimer(self.hwnd, 2, 400, None)      # stay above the taskbar
            self._tick()
            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self._log(str(e))

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TIMER:
                if getattr(self.app, "quitting", False):
                    user32.DestroyWindow(hwnd)
                elif wp == 1:
                    self._tick()
                elif wp == 2 and self.visible and not self.drag:
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
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
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, wx + pt.x - x0, r[1] if r else 0, 0, 0,
                                        SWP_NOSIZE | SWP_NOACTIVATE)
                return 0
            if msg == WM_LBUTTONUP and self.drag:
                moved = self.drag[2]
                self.drag = None
                user32.ReleaseCapture()
                if moved:
                    bar, _ = taskbar_rects()
                    r = _rect(hwnd)
                    if bar and r:
                        off = bar[2] - r[2]
                        self.s.set("taskbar_offset", off)
                        self._offset = off
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

    def _menu(self, hwnd):
        m = user32.CreatePopupMenu()
        items = [(1, "Open Notion Laptop Sync"), (0, None),
                 (2, ("Hide" if self.s.get("taskbar_speed") else "Show") + " internet speed"),
                 (3, ("Hide" if self.s.get("taskbar_desktop") else "Show") + " desktop name"),
                 (4, ("Hide" if self.s.get("taskbar_bt") else "Show") + " Bluetooth battery"),
                 (0, None), (5, "Put it back next to the clock")]
        for cid, text in items:
            user32.AppendMenuW(m, 0x800 if cid == 0 else 0, cid, text)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)        # so the menu closes when you click elsewhere
        cmd = user32.TrackPopupMenu(m, 0x0100 | 0x0002 | 0x0020, pt.x, pt.y, 0, hwnd, None)  # RETURNCMD|RIGHTBUTTON|BOTTOMALIGN
        user32.PostMessageW(hwnd, 0, 0, 0)
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
                self.desk_text = desktop_name()
            except Exception:
                self.desk_text = ""
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
        if not bar or fullscreen_app() or bar[1] >= sh - 2 or not 20 <= bar[3] - bar[1] <= 200:
            return self._hide()     # no taskbar, it's hidden or on the side, or a full-screen app is showing
        h = bar[3] - bar[1]
        img = self.render(h, light_taskbar())
        w = img.width
        right = note[0] if note else bar[2] - ui.px(330)
        off = getattr(self, "_offset", "unset")
        if off == "unset":
            off = self._offset = self.s.get("taskbar_offset")
        x = (bar[2] - int(off) - w) if off is not None else (right - w - ui.px(4))
        x = max(bar[0], min(x, bar[2] - w))
        self._blit(img, x, bar[1])
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self.visible = True

    def render(self, h, light):
        """Draws the read-out (a see-through picture) for a taskbar h pixels tall."""
        from PIL import Image, ImageDraw
        fg = (26, 26, 26, 255) if light else (255, 255, 255, 255)
        small, big = _font(max(10, round(12 * ui.S))), _font(max(11, round(14 * ui.S)), bold=True)
        parts = []      # (lines, font, align)
        if self.s.get("taskbar_desktop") and self.desk_text:
            parts.append(([self.desk_text], big, "l"))
        if self.s.get("taskbar_bt") and self.bt:
            parts.append(([f"{(n if len(n) <= 16 else n[:15] + '…')}  {b}%" for n, b in self.bt[:2]], small, "r"))
        if self.s.get("taskbar_speed"):
            parts.append(([self.up_text, self.down_text], small, "l"))
        tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        gap, pad = ui.px(14), ui.px(6)
        widths = []
        for lines, font, _ in parts:
            wmax = max((tmp.textlength(t, font=font) for t in lines), default=0)
            if lines and lines[0].startswith("↑"):
                wmax = max(wmax, tmp.textlength("↓: 888.88 KB/s", font=font))   # steady width: no jiggling
            widths.append(int(wmax) + 1)
        w = max(1, sum(widths) + gap * max(0, len(parts) - 1) + pad * 2)
        im = Image.new("RGBA", (w, h), (0, 0, 0, 1))     # almost invisible, but you can still grab it
        d = ImageDraw.Draw(im)
        x = pad
        for (lines, font, align), pw in zip(parts, widths):
            asc, desc = font.getmetrics()
            lh = asc + desc
            total = lh * len(lines) - (ui.px(2) if len(lines) > 1 else 0)
            y = (h - total) // 2
            for t in lines:
                tw = d.textlength(t, font=font)
                d.text((x + (pw - tw if align == "r" else 0), y), t, font=font, fill=fg)
                y += lh - (ui.px(2) if len(lines) > 1 else 0)
            x += pw + gap
        return im

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
            user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(x, y)), ctypes.byref(wt.SIZE(w, h)),
                                       mem, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), 2)
            gdi32.SelectObject(mem, old)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)
        self.size = (w, h)
