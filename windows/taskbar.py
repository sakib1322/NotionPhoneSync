"""A small read-out on the Windows taskbar: your internet speed (↑ upload, ↓ download) and the
name of the virtual desktop you're on (Win+Tab desktops like "BSIL Common", "Projects")."""
import ctypes
import os
import time
import tkinter as tk
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

class TaskbarMeter:
    def __init__(self, app):
        self.app = app
        self.s = app.settings
        self.net = NetMeter()
        self.win = None
        self.hwnd = None
        self.last_desktop = None
        self.light = None
        self.drag = None
        self.tick = 0
        self.last_text = None
        if os.name == "nt":
            app.root.after(1500, self._loop)

    def wanted(self):
        return bool(self.s.get("taskbar_speed") or self.s.get("taskbar_desktop"))

    # building

    def _build(self):
        self.light = light_taskbar()
        bg = "#f3f3f3" if self.light else "#1c1c1c"    # close to the taskbar's own colour
        fg = "#1a1a1a" if self.light else "#ffffff"
        self.key = bg
        w = tk.Toplevel(self.app.root)
        w.overrideredirect(True)
        w.configure(bg=bg)
        try:
            w.attributes("-topmost", True)
            w.attributes("-transparentcolor", bg)      # see-through: only the text shows
        except tk.TclError:
            pass
        self.desk = tk.Label(w, text="", bg=bg, fg=fg, font=(ui.FONT, 10, "bold"), padx=ui.px(6))
        box = tk.Frame(w, bg=bg)
        self.up = tk.Label(box, text="", bg=bg, fg=fg, font=(ui.FONT, 9), anchor="w", width=13)
        self.down = tk.Label(box, text="", bg=bg, fg=fg, font=(ui.FONT, 9), anchor="w", width=13)
        self.up.pack(anchor="w")
        self.down.pack(anchor="w")
        self.box = box
        for x in (w, self.desk, box, self.up, self.down):
            x.bind("<ButtonPress-1>", self._press)
            x.bind("<B1-Motion>", self._motion)
            x.bind("<ButtonRelease-1>", self._release)
            x.bind("<Double-Button-1>", lambda e: self.app.show_window())
            x.bind("<Button-3>", self._menu)
        self.win = w
        self._layout()
        w.update_idletasks()
        try:
            self.hwnd = ctypes.windll.user32.GetParent(w.winfo_id()) or w.winfo_id()
            # A tool window that never takes focus: stays on every virtual desktop, not in Alt+Tab.
            GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_APPWINDOW = -20, 0x80, 0x08000000, 0x40000
            st = ctypes.windll.user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE,
                                                (st | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW)
        except Exception:
            pass

    def _layout(self):
        for x in (self.desk, self.box):
            x.pack_forget()
        if self.s.get("taskbar_desktop"):
            self.desk.pack(side="left", fill="y")
        if self.s.get("taskbar_speed"):
            self.box.pack(side="left", padx=(ui.px(4), ui.px(6)))

    def destroy(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
        self.win = None
        self.hwnd = None

    def refresh_settings(self):
        if not self.wanted():
            self.destroy()
            return
        if self.win is None:
            self._build()
        self._layout()
        self.last_text = None
        self._place()

    # every second

    def _loop(self):
        try:
            self._update()
        except Exception as e:
            self.tick += 1
            if self.tick < 3:
                try:
                    self.app.log(f"Taskbar read-out: {e}")
                except Exception:
                    pass
        self.app.root.after(1000, self._loop)

    def _update(self):
        if getattr(self.app, "quitting", False):
            self.destroy()
            return
        if not self.wanted():
            self.destroy()
            return
        if self.win is None or light_taskbar() != self.light:
            self.destroy()
            self._build()
        up, down = self.net.read()
        if self.s.get("taskbar_speed"):
            self.up.configure(text="↑: " + speed_text(up))
            self.down.configure(text="↓: " + speed_text(down))
        if self.s.get("taskbar_desktop"):
            name = desktop_name()
            if name != self.last_desktop:
                self.last_desktop = name
                self.desk.configure(text=name)
        self._place()

    def _place(self):
        if self.win is None or self.drag:
            return
        bar, note = taskbar_rects()
        if not bar or fullscreen_app():
            self.win.withdraw()
            return
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        if bar[1] >= sh - 2 or bar[3] - bar[1] < 20 or bar[3] - bar[1] > 200:   # hidden, or a side taskbar
            self.win.withdraw()
            return
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = bar[3] - bar[1]
        right = note[0] if note else bar[2] - ui.px(330)
        off = self.s.get("taskbar_offset")
        x = (bar[2] - int(off) - w) if off is not None else (right - w - ui.px(6))
        x = max(bar[0], min(x, bar[2] - w))
        geo = f"{w}x{h}+{x}+{bar[1]}"
        if self.win.state() == "withdrawn":
            self.win.deiconify()
        if self.win.geometry() != geo:
            self.win.geometry(geo)
        # The taskbar is "always on top" too; stay above it without taking focus.
        if self.hwnd:
            SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x2, 0x1, 0x10, 0x40
            ctypes.windll.user32.SetWindowPos(self.hwnd, -1, 0, 0, 0, 0,
                                              SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    # moving it along the taskbar

    def _press(self, e):
        self.drag = (e.x_root, self.win.winfo_x())

    def _motion(self, e):
        if not self.drag:
            return
        x0, wx = self.drag
        self.win.geometry(f"+{wx + e.x_root - x0}+{self.win.winfo_y()}")

    def _release(self, e):
        if not self.drag:
            return
        moved = abs(e.x_root - self.drag[0]) > 3
        self.drag = None
        if moved:
            bar, _ = taskbar_rects()
            if bar:
                self.s.set("taskbar_offset", bar[2] - (self.win.winfo_x() + self.win.winfo_width()))

    def _menu(self, e):
        m = tk.Menu(self.win, tearoff=0)
        m.add_command(label="Open Notion Laptop Sync", command=self.app.show_window)
        m.add_separator()
        m.add_command(label=("Hide" if self.s.get("taskbar_speed") else "Show") + " internet speed",
                      command=lambda: self._toggle("taskbar_speed"))
        m.add_command(label=("Hide" if self.s.get("taskbar_desktop") else "Show") + " desktop name",
                      command=lambda: self._toggle("taskbar_desktop"))
        m.add_command(label="Put it back next to the clock", command=self._reset)
        m.tk_popup(e.x_root, e.y_root)

    def _toggle(self, key):
        self.s.set(key, not self.s.get(key))
        try:
            self.app.taskbar_vars[key].set(self.s.get(key))
        except Exception:
            pass
        self.refresh_settings()

    def _reset(self):
        self.s.set("taskbar_offset", None)
        self._place()
