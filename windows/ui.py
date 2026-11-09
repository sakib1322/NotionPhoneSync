"""The look of the app: a Windows 11 style theme, coloured file icons, and the Drive-style sidebar."""
import os
import tkinter as tk
from tkinter import ttk

BG = "#f8fafd"          # page background (Google Drive-like)
SIDEBAR = "#f8fafd"
CARD = "#ffffff"
SELECTED = "#c2e7ff"    # selected tile / nav item
HOVER = "#e9eef6"
TEXT = "#1f1f1f"
MUTED = "#5f6368"
ACCENT = "#0b57d0"
FONT = "Segoe UI Variable Text" if os.name == "nt" else "Segoe UI"


# Windows' own icon font (Segoe Fluent Icons on Windows 11, Segoe MDL2 Assets on Windows 10).
GLYPHS = {
    "drive": ("\uE753", "☁"), "recent": ("\uE823", "◷"), "laptop": ("\uE7F8", "▭"), "phone": ("\uE8EA", "▯"),
    "backup": ("\uE895", "⟳"), "folders": ("\uE8B7", "▣"), "storage": ("\uEDA2", "▤"), "devices": ("\uE772", "⇆"),
    "voice": ("\uE720", "♪"), "account": ("\uE77B", "◉"), "activity": ("\uE81C", "≡"), "settings": ("\uE713", "⚙"),
    "search": ("\uE721", "⌕"), "add": ("\uE710", "+"), "list": ("\uE8FD", "☰"), "grid": ("\uF0E2", "▦"),
    "info": ("\uE946", "i"), "refresh": ("\uE72C", "⟳"), "delete": ("\uE74D", "✕"), "download": ("\uE896", "↓"),
    "upload": ("\uE898", "↑"), "rename": ("\uE8AC", "✎"), "move": ("\uE8DE", "→"), "open": ("\uE8E5", "↗"),
    "close": ("\uE711", "✕"), "down": ("\uE70D", "▾"), "newfolder": ("\uE8F4", "+"),
}
ICON_FONT = None


def glyph(name):
    g, fallback = GLYPHS.get(name, ("", "•"))
    return g if ICON_FONT else fallback


def gfont(size=12):
    return (ICON_FONT, size) if ICON_FONT else (FONT, size)


def apply_theme(root):
    """Sun Valley (Windows 11) theme, light, with Drive-like colours and fonts."""
    try:
        import sv_ttk
        sv_ttk.set_theme("light")
    except Exception:
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
    root.configure(bg=BG)
    setup_scaling(root)
    global ICON_FONT
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
        ICON_FONT = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fams), None)
    except Exception:
        ICON_FONT = None
    st = ttk.Style()
    for f in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            root.nametowidget(".")  # noqa
            import tkinter.font as tkfont
            tkfont.nametofont(f).configure(family=FONT, size=10)
        except Exception:
            pass
    st.layout("Hidden.TNotebook.Tab", [])
    st.configure("Hidden.TNotebook", borderwidth=0, tabmargins=0, padding=0)
    st.configure("Treeview", rowheight=px(30))
    st.configure("Title.TLabel", font=(FONT, 18))
    st.configure("Crumb.TLabel", font=(FONT, 15))
    st.configure("CrumbLink.TLabel", font=(FONT, 15), foreground=MUTED)
    st.configure("Muted.TLabel", foreground=MUTED)
    st.configure("Icon.TButton", font=gfont(12), padding=(8, 4))
    # The theme recolours every plain (non-ttk) widget whenever a style changes, which would wipe
    # our own colours. Let it run once now, then switch that off.
    try:
        root.tk.call("bind", root.winfo_class(), "<<ThemeChanged>>", "")
        root.tk.call("configure_colors")
    except tk.TclError:
        pass
    # File list without the expand arrows (they're for trees, not a flat list).
    try:
        def strip(layout):
            out = []
            for name, opts in layout:
                if "indicator" in name:
                    continue
                opts = dict(opts)
                if "children" in opts:
                    opts["children"] = strip(opts["children"])
                out.append((name, opts))
            return out
        st.layout("Files.Treeview.Item", strip(st.layout("Treeview.Item")))
        st.configure("Files.Treeview", rowheight=px(36), indent=0)
    except tk.TclError:
        pass


# ---- file icons ------------------------------------------------------------------------------

TYPES = {
    # label, colour
    "folder": ("", "#5f6368"),
    "photo": ("IMG", "#1a73e8"),
    "video": ("VID", "#d93025"),
    "audio": ("AUD", "#9334e6"),
    "pdf": ("PDF", "#ea4335"),
    "doc": ("DOC", "#4285f4"),
    "sheet": ("XLS", "#188038"),
    "slide": ("PPT", "#f29900"),
    "zip": ("ZIP", "#795548"),
    "code": ("</>", "#455a64"),
    "text": ("TXT", "#5f6368"),
    "app": ("APP", "#00897b"),
    "file": ("FILE", "#80868b"),
}
EXT = {
    "doc": "doc", "docx": "doc", "odt": "doc", "rtf": "doc", "pages": "doc",
    "xls": "sheet", "xlsx": "sheet", "csv": "sheet", "ods": "sheet", "numbers": "sheet",
    "ppt": "slide", "pptx": "slide", "odp": "slide", "key": "slide",
    "zip": "zip", "rar": "zip", "7z": "zip", "tar": "zip", "gz": "zip",
    "py": "code", "js": "code", "html": "code", "css": "code", "json": "code", "kt": "code", "java": "code", "c": "code",
    "txt": "text", "md": "text", "log": "text",
    "apk": "app", "exe": "app", "msi": "app",
    "pdf": "pdf",
}
KIND = {"Folder": "folder", "Photo": "photo", "Video": "video", "Audio": "audio", "PDF": "pdf"}


def type_of(name, kind):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if kind in KIND and kind != "PDF":
        return KIND[kind]
    return EXT.get(ext, KIND.get(kind, "file"))


_cache = {}
S = 1.0     # screen scale (1.0 at 100%, 1.5 at 150% …), set by setup_scaling()


def px(n):
    """A size in pixels at 100% scale → the size on this screen."""
    return int(round(n * S))


def make_dpi_aware():
    """Ask Windows for real pixels instead of a blurry stretched window. Call before tk.Tk()."""
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # sharp on the main screen
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def setup_scaling(root):
    global S
    try:
        S = max(1.0, min(3.0, root.winfo_fpixels("1i") / 96.0))
    except Exception:
        S = 1.0


def icon(t, size):
    """A crisp coloured file icon (PhotoImage), Drive style, drawn once per size and cached."""
    size = px(size)
    key = (t, size)
    if key in _cache:
        return _cache[key]
    from PIL import Image, ImageDraw, ImageTk
    k = 8                       # draw 8x bigger, then shrink: smooth edges at any size
    s = size * k
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    colour = TYPES.get(t, TYPES["file"])[1]
    W = (255, 255, 255, 255)
    if t == "folder":
        d.rounded_rectangle([s * .06, s * .16, s * .46, s * .34], radius=s * .06, fill="#5f6368")
        d.rounded_rectangle([s * .06, s * .24, s * .94, s * .84], radius=s * .08, fill="#5f6368")
        d.rounded_rectangle([s * .06, s * .32, s * .94, s * .84], radius=s * .08, fill="#757a80")
    else:
        # A rounded square in the type's colour with a simple white symbol.
        m = s * .1
        d.rounded_rectangle([m, m, s - m, s - m], radius=s * .14, fill=colour)
        c = s / 2
        u = s * .8 / 10          # one unit of the inner grid
        lw = max(k, int(u * .9))  # line width
        if t == "photo":
            d.ellipse([c + u * .8, c - u * 2.6, c + u * 2.4, c - u * 1.0], fill=W)
            d.polygon([(c - u * 3, c + u * 2.4), (c - u * .8, c - u * .8), (c + u * .9, c + u * 1.2),
                       (c + u * 1.8, c + u * .2), (c + u * 3, c + u * 2.4)], fill=W)
        elif t == "video":
            d.polygon([(c - u * 1.5, c - u * 2.4), (c + u * 2.4, c), (c - u * 1.5, c + u * 2.4)], fill=W)
        elif t == "audio":
            d.ellipse([c - u * 2.6, c + u * .6, c - u * .2, c + u * 2.8], fill=W)
            d.rectangle([c - u * 1.2, c - u * 2.8, c - u * .2, c + u * 1.7], fill=W)
            d.polygon([(c - u * .2, c - u * 2.8), (c + u * 2.4, c - u * 1.8), (c + u * 2.4, c - u * .7), (c - u * .2, c - u * 1.7)], fill=W)
        elif t == "sheet":
            d.rectangle([c - u * 2.6, c - u * 2.6, c + u * 2.6, c + u * 2.6], outline=W, width=lw)
            d.line([c - u * 2.6, c, c + u * 2.6, c], fill=W, width=lw)
            d.line([c - u * .6, c - u * 2.6, c - u * .6, c + u * 2.6], fill=W, width=lw)
        elif t == "slide":
            d.rounded_rectangle([c - u * 2.8, c - u * 2.0, c + u * 2.8, c + u * 2.0], radius=u * .4, outline=W, width=lw)
            d.rectangle([c - u * 1.4, c - u * .7, c + u * 1.4, c + u * .7], fill=W)
        elif t == "zip":
            for i in range(-3, 3):
                y = c + i * u * .9
                x = c - u * .9 if i % 2 else c
                d.rectangle([x, y, x + u * .9, y + u * .7], fill=W)
            d.rounded_rectangle([c - u * .9, c + u * 2.2, c + u * .9, c + u * 3.2], radius=u * .2, fill=W)
        elif t == "code":
            d.line([(c - u * 1, c - u * 2), (c - u * 2.8, c), (c - u * 1, c + u * 2)], fill=W, width=lw, joint="curve")
            d.line([(c + u * 1, c - u * 2), (c + u * 2.8, c), (c + u * 1, c + u * 2)], fill=W, width=lw, joint="curve")
        elif t == "app":
            d.rounded_rectangle([c - u * 2.4, c - u * 2.4, c - u * .4, c - u * .4], radius=u * .3, fill=W)
            d.rounded_rectangle([c + u * .4, c - u * 2.4, c + u * 2.4, c - u * .4], radius=u * .3, fill=W)
            d.rounded_rectangle([c - u * 2.4, c + u * .4, c - u * .4, c + u * 2.4], radius=u * .3, fill=W)
            d.rounded_rectangle([c + u * .4, c + u * .4, c + u * 2.4, c + u * 2.4], radius=u * .3, fill=W)
        elif t == "pdf" and size >= 36:
            from PIL import ImageFont
            fnt = None
            for f in ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
                try:
                    fnt = ImageFont.truetype(f, int(u * 2.6))
                    break
                except Exception:
                    pass
            fnt = fnt or ImageFont.load_default()
            w = d.textlength("PDF", font=fnt)
            d.text((c - w / 2, c - u * 1.6), "PDF", fill=W, font=fnt)
        else:   # documents, text, pdf (small), anything else: lines of text
            for i, wd in enumerate((5.2, 5.2, 5.2, 3.2)):
                y = c - u * 2.1 + i * u * 1.4
                d.rounded_rectangle([c - u * 2.6, y, c - u * 2.6 + u * wd, y + u * .75], radius=u * .3, fill=W)
    im = im.resize((size, size), Image.LANCZOS)
    ph = ImageTk.PhotoImage(im)
    _cache[key] = ph
    return ph


def thumb(path, size):
    """A square photo thumbnail (PhotoImage) from an image file, or None."""
    key = ("thumb", path, size)
    if key in _cache:
        return _cache[key]
    try:
        from PIL import Image, ImageOps, ImageTk
        im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        im = ImageOps.fit(im, (size, size), Image.LANCZOS)
        ph = ImageTk.PhotoImage(im)
    except Exception:
        return None
    if len(_cache) > 600:
        for k in [k for k in _cache if k[0] == "thumb"][:200]:
            _cache.pop(k, None)
    _cache[key] = ph
    return ph


# ---- sidebar ----------------------------------------------------------------------------------

class Sidebar:
    """Drive-style navigation down the left side."""

    def __init__(self, app, parent):
        self.app = app
        self.frame = tk.Frame(parent, bg=SIDEBAR, width=px(256))
        self.frame.pack(side="left", fill="y")
        self.frame.pack_propagate(False)
        self.items = {}        # key -> widget
        self.current = None

        head = tk.Frame(self.frame, bg=SIDEBAR)
        head.pack(fill="x", padx=18, pady=(16, 14))
        try:
            from PIL import Image, ImageTk
            import winutil
            self._logo = ImageTk.PhotoImage(Image.open(winutil.resource("icon.ico")).convert("RGBA").resize((px(32), px(32)), Image.LANCZOS))
            tk.Label(head, image=self._logo, bg=SIDEBAR).pack(side="left")
        except Exception:
            tk.Label(head, text=glyph("drive"), font=gfont(20), fg=ACCENT, bg=SIDEBAR).pack(side="left")
        tk.Label(head, text="Notion Sync", font=(FONT, 15), fg=TEXT, bg=SIDEBAR).pack(side="left", padx=8)

        new = tk.Frame(self.frame, bg=CARD, cursor="hand2", highlightthickness=1, highlightbackground="#dadce0")
        new.pack(anchor="w", padx=14, pady=(0, 14))
        a1 = tk.Label(new, text=glyph("add"), font=gfont(14), bg=CARD, fg=TEXT, padx=0)
        a1.pack(side="left", padx=(16, 10), pady=12)
        a2 = tk.Label(new, text="New", font=(FONT, 11), bg=CARD, fg=TEXT)
        a2.pack(side="left", padx=(0, 22))
        for w in (new, a1, a2):
            w.bind("<Button-1>", lambda e: self.app.files.new_menu(new))
            w.bind("<Enter>", lambda e: [x.configure(bg="#edf2fa") for x in (new, a1, a2)])
            w.bind("<Leave>", lambda e: [x.configure(bg=CARD) for x in (new, a1, a2)])

        self.nav = tk.Frame(self.frame, bg=SIDEBAR)
        self.nav.pack(fill="x")
        self.devices_box = None
        self.foot = tk.Label(self.frame, text="", font=(FONT, 9), fg=MUTED, bg=SIDEBAR, justify="left",
                             wraplength=200, anchor="w")
        self.foot.pack(side="bottom", fill="x", padx=18, pady=12)
        self.rebuild()

    def _item(self, parent, key, icon_name, text, cmd):
        row = tk.Frame(parent, bg=SIDEBAR, cursor="hand2")
        row.pack(fill="x", padx=(8, 12), pady=1)
        ic = tk.Label(row, text=glyph(icon_name), font=gfont(12), fg=TEXT, bg=SIDEBAR, width=2)
        ic.pack(side="left", padx=(16, 8), pady=6)
        lbl = tk.Label(row, text=text, font=(FONT, 10), fg=TEXT, bg=SIDEBAR, anchor="w")
        lbl.pack(side="left", fill="x", expand=True)
        row._parts = (row, ic, lbl)
        row._text = lbl

        def paint(bg):
            for w in row._parts:
                w.configure(bg=bg)
        for w in row._parts:
            w.bind("<Button-1>", lambda e: (self.select(key), cmd()))
            w.bind("<Enter>", lambda e: paint(HOVER) if self.current != key else None)
            w.bind("<Leave>", lambda e: paint(SELECTED if self.current == key else SIDEBAR))
        row._paint = paint
        self.items[key] = row
        return row

    def _section(self, text):
        tk.Label(self.nav, text=text, font=(FONT, 9), fg=MUTED, bg=SIDEBAR, anchor="w").pack(
            fill="x", padx=26, pady=(12, 2))

    def rebuild(self):
        for w in self.nav.winfo_children():
            w.destroy()
        self.items.clear()
        a = self.app
        self._item(self.nav, "drive", "drive", "My Drive", lambda: a.open_files(a.settings.get("root_page")))
        self._item(self.nav, "recent", "recent", "Recent", lambda: a.open_files(None, recent=True))
        others = []
        try:
            import devices
            kinds = {d.page: (d.type or "") for d in devices.cached(a.settings)}
            for sid, title in a.files.cloud().sources():
                if sid != a.settings.get("root_page"):
                    others.append((sid, title, kinds.get(sid, "")))
        except Exception:
            pass
        if others:
            self._section("Your other devices")
            for sid, title, typ in others:
                phone = "phone" in typ.lower() or ("phone" in title.lower() and "laptop" not in typ.lower())
                self._item(self.nav, "src:" + sid, "phone" if phone else "laptop", title,
                           lambda s=sid: a.open_files(s))
        self._section("This laptop")
        self._item(self.nav, "backup", "backup", "Backup", lambda: a.nb.select(0))
        self._item(self.nav, "folders", "folders", "Folders to back up", lambda: a.nb.select(3))
        self._item(self.nav, "space", "storage", "Storage on this laptop", lambda: a.nb.select(2))
        self._item(self.nav, "voice", "voice", "Voice changer", lambda: a.nb.select(8))
        self._section("Account")
        self._item(self.nav, "devices", "devices", "Devices", lambda: a.nb.select(7))
        self._item(self.nav, "account", "account", "Account & clipboard", lambda: a.nb.select(4))
        self._item(self.nav, "activity", "activity", "Activity", lambda: a.nb.select(5))
        self._item(self.nav, "settings", "settings", "Settings & updates", lambda: a.nb.select(6))
        if self.current in self.items:
            cur, self.current = self.current, None
            self.select(cur)

    def select(self, key):
        old = self.items.get(self.current)
        if old:
            old._paint(SIDEBAR)
            old._text.configure(font=(FONT, 10))
        self.current = key
        new = self.items.get(key)
        if new:
            new._paint(SELECTED)
            new._text.configure(font=(FONT, 10, "bold"))

    def set_footer(self, text):
        if self.foot.cget("text") != text:
            self.foot.configure(text=text)
