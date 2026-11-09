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
    st.configure("Treeview", rowheight=30)
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
        st.configure("Files.Treeview", rowheight=34, indent=0)
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


def icon(t, size):
    """A coloured icon (PhotoImage) for a file type, drawn once and cached."""
    key = (t, size)
    if key in _cache:
        return _cache[key]
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    s = size * 3  # draw big, shrink for smooth edges
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    label, colour = TYPES.get(t, TYPES["file"])
    if t == "folder":
        d.rounded_rectangle([s * .08, s * .22, s * .5, s * .38], radius=s * .05, fill="#c4c7c5")
        d.rounded_rectangle([s * .08, s * .3, s * .92, s * .82], radius=s * .07, fill="#c4c7c5")
        d.rounded_rectangle([s * .08, s * .36, s * .92, s * .82], radius=s * .07, fill="#a8abaf")
    else:
        d.rounded_rectangle([s * .18, s * .08, s * .82, s * .92], radius=s * .08, fill=colour)
        d.polygon([(s * .62, s * .08), (s * .82, s * .28), (s * .62, s * .28)], fill=(255, 255, 255, 90))
        try:
            fnt = ImageFont.truetype("segoeuib.ttf", int(s * .17))
        except Exception:
            try:
                fnt = ImageFont.truetype("DejaVuSans-Bold.ttf", int(s * .17))
            except Exception:
                fnt = ImageFont.load_default()
        w = d.textlength(label, font=fnt)
        d.text(((s - w) / 2, s * .52), label, fill="white", font=fnt)
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
        self.frame = tk.Frame(parent, bg=SIDEBAR, width=236)
        self.frame.pack(side="left", fill="y")
        self.frame.pack_propagate(False)
        self.items = {}        # key -> widget
        self.current = None

        head = tk.Frame(self.frame, bg=SIDEBAR)
        head.pack(fill="x", padx=18, pady=(16, 14))
        try:
            from PIL import Image, ImageTk
            import winutil
            self._logo = ImageTk.PhotoImage(Image.open(winutil.resource("icon.ico")).convert("RGBA").resize((30, 30), Image.LANCZOS))
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
