"""The Files screen: your cloud drive on the laptop, laid out like Google Drive."""
import os
import queue
import shutil
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import core
import ui
from cloud import CACHE_DIR, Cloud, Row

SORTS = [("Last modified", "mtime DESC"), ("Oldest first", "mtime ASC"),
         ("Name", "name COLLATE NOCASE ASC"), ("Size", "size DESC")]
CARD_W, CARD_GAP = 196, 14
THUMB_W, THUMB_H = 172, 118
PAGE = 300                  # tiles drawn at a time (more with "Show more")
THUMB_MAX_BYTES = 30 * 1024 * 1024
TILE_BG = "#f0f4f9"


def _when(ms):
    if not ms:
        return ""
    t = time.localtime(ms / 1000)
    now = time.localtime()
    if t.tm_year == now.tm_year and t.tm_yday == now.tm_yday:
        return time.strftime("%I:%M %p", t).lstrip("0")
    if t.tm_year == now.tm_year:
        return time.strftime("%d %b", t).lstrip("0")
    return time.strftime("%d %b %Y", t).lstrip("0")


def _short(name, n=24):
    return name if len(name) <= n else name[:n - 1] + "…"


class FilesTab:
    def __init__(self, app, frame):
        global CARD_W, CARD_GAP, THUMB_W, THUMB_H
        CARD_W, CARD_GAP = ui.px(212), ui.px(16)
        THUMB_W, THUMB_H = ui.px(188), ui.px(132)
        self.app = app
        self.settings = app.settings
        self.db = app.db
        self.root_id = ""
        self.path = ""
        self.query = ""
        self.recent = False
        self.view = self.settings.get("files_view") or "grid"
        self.entries = []        # what's on screen: ("folder", path) or ("file", Row)
        self.sel = set()         # indexes into entries
        self.anchor = None
        self.limit = PAGE
        self.tiles = {}          # entry index -> tile frame (grid view)
        self.refreshing = False
        self.last_refresh = 0
        self.gen = 0             # bumps on every redraw (old thumbnail jobs are dropped)
        self.thumb_q = queue.Queue()
        self.preview_img = None
        self.details_on = bool(self.settings.get("files_details"))
        self._relayout_job = None
        self._cols = 0
        self._search_job = None
        self._folder_info = {}
        self._iids = {}
        self._build(frame)
        threading.Thread(target=self._thumb_worker, daemon=True).start()

    # ---- layout ---------------------------------------------------------------------

    def _build(self, f):
        f.configure(padding=(18, 12, 18, 8))
        top = ttk.Frame(f)
        top.pack(fill="x")
        # Big rounded-looking search box, like Drive's.
        box = tk.Frame(top, bg="#e9eef6", padx=12, pady=8)
        box.pack(side="left", fill="x", expand=True)
        tk.Label(box, text=ui.glyph("search"), bg="#e9eef6", fg=ui.MUTED, font=ui.gfont(12)).pack(side="left")
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(box, textvariable=self.search_var, bd=0, bg="#e9eef6", fg=ui.TEXT,
                                     font=(ui.FONT, 12), insertbackground=ui.TEXT, highlightthickness=0)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.search_entry.bind("<Return>", lambda ev: self.search())
        self.search_entry.bind("<Escape>", lambda ev: self.clear_search())
        self.search_var.trace_add("write", lambda *a: self._search_typed())
        self._hint(self.search_entry, "Search in all your devices")
        x = tk.Label(box, text=ui.glyph("close"), bg="#e9eef6", fg=ui.MUTED, cursor="hand2", font=ui.gfont(10))
        x.pack(side="right")
        x.bind("<Button-1>", lambda e: self.clear_search())

        tools = ttk.Frame(top)
        tools.pack(side="right", padx=(12, 0))
        self.btn_info = ttk.Button(tools, text=ui.glyph("info"), style="Icon.TButton", command=self.toggle_details)
        self.btn_info.pack(side="right", padx=2)
        self.btn_grid = ttk.Button(tools, text=ui.glyph("grid"), style="Icon.TButton", command=lambda: self.set_view("grid"))
        self.btn_grid.pack(side="right", padx=2)
        self.btn_list = ttk.Button(tools, text=ui.glyph("list"), style="Icon.TButton", command=lambda: self.set_view("list"))
        self.btn_list.pack(side="right", padx=2)
        ttk.Button(tools, text=ui.glyph("refresh"), style="Icon.TButton", command=lambda: self.refresh(full=True)).pack(side="right", padx=2)

        nav = ttk.Frame(f)
        nav.pack(fill="x", pady=(14, 6))
        self.crumbs = ttk.Frame(nav)
        self.crumbs.pack(side="left")
        self.sort_var = tk.StringVar(value=self.settings.get("files_sort") or SORTS[0][0])
        sb = ttk.Combobox(nav, textvariable=self.sort_var, state="readonly", width=13, values=[s[0] for s in SORTS])
        sb.pack(side="right")
        sb.bind("<<ComboboxSelected>>", lambda e: (self.settings.set("files_sort", self.sort_var.get()), self.show()))
        ttk.Label(nav, text="Sort by", style="Muted.TLabel").pack(side="right", padx=6)

        # Action bar: appears when something is selected (like Drive's).
        self.actbar = ttk.Frame(f)
        self.act_count = ttk.Label(self.actbar, text="")
        self.act_count.pack(side="left", padx=(2, 12))
        ttk.Button(self.actbar, text=ui.glyph("close"), style="Icon.TButton", command=self.select_none).pack(side="left", padx=(0, 8))
        self.act_count.pack_forget()
        self.act_count.pack(side="left", padx=(0, 16))
        for text, cmd in [("Open", self.open_selected), ("Download", self.save_as), ("Rename", self.rename),
                          ("Move to…", self.move), ("Delete", self.delete)]:
            ttk.Button(self.actbar, text=text, command=cmd).pack(side="left", padx=(0, 6))

        self.body = ttk.Frame(f)
        self.body.pack(fill="both", expand=True)

        # Details pane (right).
        self.details = tk.Frame(self.body, bg=ui.CARD, width=ui.px(300), highlightthickness=1, highlightbackground="#e3e3e3")
        self.details.pack_propagate(False)
        self.det_img = tk.Label(self.details, bg=ui.CARD)
        self.det_img.pack(pady=(16, 8))
        self.det_name = tk.Label(self.details, bg=ui.CARD, fg=ui.TEXT, font=(ui.FONT, 12, "bold"), wraplength=250, justify="left")
        self.det_name.pack(fill="x", padx=16)
        self.det_text = tk.Label(self.details, bg=ui.CARD, fg=ui.MUTED, font=(ui.FONT, 10), wraplength=250, justify="left", anchor="w")
        self.det_text.pack(fill="x", padx=16, pady=10)

        # Grid view: a scrolling canvas of tiles.
        self.grid_box = ttk.Frame(self.body)
        self.canvas = tk.Canvas(self.grid_box, bg=ui.BG, highlightthickness=0, takefocus=1)
        gsc = ttk.Scrollbar(self.grid_box, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=gsc.set)
        gsc.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=ui.BG)
        self.inner_id = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.canvas.bind("<Button-1>", lambda e: (self.canvas.focus_set(), self.select_none()))
        self.canvas.bind("<Button-3>", lambda e: self.background_menu(e))
        self.grid_box.bind("<Enter>", lambda e: self._wheel(True))
        self.grid_box.bind("<Leave>", lambda e: self._wheel(False))

        # List view.
        self.list_box = ttk.Frame(self.body)
        cols = ("owner", "date", "size")
        self.tree = ttk.Treeview(self.list_box, columns=cols, selectmode="extended", style="Files.Treeview")
        self.tree.heading("#0", text="Name")
        self.tree.heading("owner", text="Location")
        self.tree.heading("date", text="Last modified")
        self.tree.heading("size", text="File size")
        self.tree.column("#0", width=ui.px(320), minwidth=ui.px(160), stretch=True)
        self.tree.column("owner", width=ui.px(170), minwidth=ui.px(80), stretch=False)
        self.tree.column("date", width=ui.px(120), minwidth=ui.px(80), stretch=False)
        self.tree.column("size", width=ui.px(90), minwidth=ui.px(60), anchor="e", stretch=False)
        sc = ttk.Scrollbar(self.list_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sc.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._tree_selected())
        self.tree.bind("<Button-3>", self.context_menu)

        for w in (self.canvas, self.tree):
            w.bind("<Return>", lambda e: self.open_selected())
            w.bind("<BackSpace>", lambda e: self.go_up())
            w.bind("<Delete>", lambda e: self.delete())
            w.bind("<F2>", lambda e: self.rename())
            w.bind("<Control-a>", lambda e: (self.select_all(), "break")[1])
        self.app.root.bind("<Control-f>", lambda e: self.search_entry.focus_set())

        self.status = ttk.Label(f, style="Muted.TLabel")
        self.status.pack(fill="x", pady=(6, 0))
        self._place_views()
        self._setup_drop()

    def _hint(self, entry, text):
        """Grey placeholder text in an entry."""
        def show(_=None):
            if not entry.get():
                entry.insert(0, text)
                entry.configure(fg=ui.MUTED)
                entry._hint = True

        def hide(_=None):
            if getattr(entry, "_hint", False):
                entry._hint = False
                entry.delete(0, "end")
                entry.configure(fg=ui.TEXT)
        entry.bind("<FocusIn>", hide, add="+")
        entry.bind("<FocusOut>", show, add="+")
        show()

    def _search_text(self):
        return "" if getattr(self.search_entry, "_hint", False) else self.search_var.get().strip()

    def _place_views(self):
        for w in (self.details, self.grid_box, self.list_box):
            w.pack_forget()
        if self.details_on:
            self.details.pack(side="right", fill="y", padx=(12, 0))
        (self.grid_box if self.view == "grid" else self.list_box).pack(side="left", fill="both", expand=True)

    def _wheel(self, on):
        if on:
            self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        else:
            for s in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.canvas.unbind_all(s)

    def _setup_drop(self):
        """Drag files or folders in from File Explorer to upload them here."""
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            TkinterDnD._require(self.app.root)
            for w in (self.canvas, self.tree, self.inner):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", lambda e: (self.upload_paths(list(self.app.root.tk.splitlist(e.data))), e.action)[1])
            self.can_drop = True
        except Exception:
            self.can_drop = False

    def set_view(self, v):
        self.view = v
        self.settings.set("files_view", v)
        self._place_views()
        self.show(keep_sel=True)

    def toggle_details(self):
        self.details_on = not self.details_on
        self.settings.set("files_details", self.details_on)
        self._place_views()
        self._details()

    # ---- sources & refresh ---------------------------------------------------------------

    def cloud(self, stop=None):
        return Cloud(self.settings, self.db, stop)

    def on_shown(self):
        if not self.settings.get("token") or not self.settings.get("root_page"):
            self._set_status("Connect Notion first (Account & clipboard).")
            return
        if not self.root_id and not self.recent:
            self.root_id = self.settings.get("root_page")
        self.show()
        if time.time() - self.last_refresh > 120:
            self.refresh(full=False)

    def open_source(self, sid, recent=False):
        """Called by the sidebar: My Drive, Recent or a device."""
        self.recent = recent
        if not recent:
            self.root_id = sid or self.settings.get("root_page")
        self.path = ""
        self.clear_search(show=False)
        self.on_shown()

    def refresh(self, full):
        if self.refreshing:
            return
        self.refreshing = True
        self.last_refresh = time.time()
        self._set_status("Updating the file list…")

        def work():
            err = None
            try:
                c = self.cloud()
                try:
                    c.discover()
                except Exception:
                    pass
                for sid, title in c.sources():
                    try:
                        c.pull(sid, full=full, progress=lambda n, t=title: self._ui(
                            lambda: self._set_status(f"Loading the file list for {t}… {n:,} files")))
                    except Exception as e:
                        err = e
            finally:
                def done():
                    self.refreshing = False
                    self.show(keep_sel=True)
                    try:
                        self.app.sidebar.rebuild()
                    except Exception:
                        pass
                    if err:
                        self._set_status(f"Couldn't refresh everything: {err}")
                self._ui(done)

        threading.Thread(target=work, daemon=True).start()

    def _ui(self, fn):
        self.app.events.put(fn)

    def _set_status(self, text):
        self.status.config(text=text)

    # ---- what's on screen ------------------------------------------------------------------

    def _order(self):
        return dict(SORTS).get(self.sort_var.get(), "mtime DESC")

    def _titles(self):
        return dict(self.cloud().sources())

    def _where(self, r, titles=None):
        title = (titles or self._titles()).get(r.root, "Backup")
        return title + (" › " + r.path.replace("/", " › ") if r.path else "")

    def _load(self):
        """Fills self.entries for the current place; returns a status line."""
        self.entries = []
        titles = self._titles()
        if self.query or self.recent:
            roots = list(titles)
            if self.query:
                rows = [Row(t) for t in self.db.search_idx(roots, self.query)]
                note = f"{len(rows):,} result(s) for “{self.query}”" + (" (the newest 500)" if len(rows) >= 500 else "")
            else:
                rows = [Row(t) for t in self.db.recent_idx(roots, 300)]
                note = "Your most recently changed files on all your devices"
            self.entries = [("file", r) for r in rows]
            return note
        if not self.root_id:
            return ""
        folders = self.db.sub_folders(self.root_id, self.path)
        o = self._order()
        if o.startswith("size"):
            folders.sort(key=lambda t: -t[2])
        elif o.startswith("name"):
            folders.sort(key=lambda t: t[0].lower())
        self._folder_info = {}
        for name, n, b in folders:
            key = f"{self.path}/{name}" if self.path else name
            self._folder_info[key] = (n, b)
            self.entries.append(("folder", key))
        self.entries += [("file", Row(t)) for t in self.db.files_in(self.root_id, self.path, o)]
        n, b = self.db.storage(self.root_id)[0]
        if n == 0 and not self.db.get_meta("index_db:" + self.root_id):
            return "This device has no file index yet. It's created by the next sync of the app that uploads to it."
        pending = self.db.count_pending_index() if self.root_id == self.settings.get("root_page") else 0
        extra = f"  ·  Adding {pending:,} earlier uploads to the index…" if pending else ""
        drop = "  ·  Drag files here from File Explorer to upload" if getattr(self, "can_drop", False) else ""
        return f"{n:,} files · {core.human_size(b)} on {titles.get(self.root_id, 'this device')}{extra}{drop}"

    def _key(self, e):
        return e[1] if e[0] == "folder" else "f:" + e[1].block + e[1].row_id

    def show(self, keep_sel=False):
        before = {self._key(self.entries[i]) for i in self.sel if i < len(self.entries)} if keep_sel else set()
        note = self._load()
        self.sel = {i for i, e in enumerate(self.entries) if self._key(e) in before}
        self.limit = PAGE
        self.gen += 1
        self._render_crumbs()
        if self.view == "grid":
            self._draw_grid()
        else:
            self._draw_list()
        self._selection_changed()
        self._set_status(note)

    def _render_crumbs(self):
        for w in self.crumbs.winfo_children():
            w.destroy()
        if self.query:
            ttk.Label(self.crumbs, text="Search results", style="Crumb.TLabel").pack(side="left")
            return
        if self.recent:
            ttk.Label(self.crumbs, text="Recent", style="Crumb.TLabel").pack(side="left")
            return
        title = "My Drive" if self.root_id == self.settings.get("root_page") else self._titles().get(self.root_id, "Backup")
        parts = [(title, "")]
        segs = self.path.split("/") if self.path else []
        for i, s in enumerate(segs):
            parts.append((s, "/".join(segs[:i + 1])))
        if len(parts) > 4:
            parts = parts[:1] + [("…", parts[-4][1])] + parts[-3:]
        for i, (t, p) in enumerate(parts):
            if i:
                ttk.Label(self.crumbs, text="  ›  ", style="CrumbLink.TLabel").pack(side="left")
            last = i == len(parts) - 1
            lbl = ttk.Label(self.crumbs, text=(t + "  " + ui.glyph("down")) if last and not ui.ICON_FONT else t,
                            style="Crumb.TLabel" if last else "CrumbLink.TLabel", cursor="hand2")
            lbl.pack(side="left")
            if last:
                lbl.bind("<Button-1>", lambda e: self.new_menu(lbl))
                if ui.ICON_FONT:
                    arr = ttk.Label(self.crumbs, text=ui.glyph("down"), font=ui.gfont(11), cursor="hand2")
                    arr.pack(side="left", padx=(8, 0), pady=(4, 0))
                    arr.bind("<Button-1>", lambda e: self.new_menu(lbl))
            else:
                lbl.bind("<Button-1>", lambda e, p=p: self.go_to(p))

    # grid view

    def _canvas_resized(self, e):
        self.canvas.itemconfigure(self.inner_id, width=e.width)
        cols = max(1, (e.width - 4) // (CARD_W + CARD_GAP))
        if cols != self._cols and self.view == "grid":
            if self._relayout_job:
                self.canvas.after_cancel(self._relayout_job)
            self._relayout_job = self.canvas.after(120, self._draw_grid)

    def _draw_grid(self):
        self._relayout_job = None
        for w in self.inner.winfo_children():
            w.destroy()
        self.tiles.clear()
        self.canvas.yview_moveto(0)
        width = self.canvas.winfo_width()
        cols = max(1, (width - 4) // (CARD_W + CARD_GAP)) if width > 10 else 4
        self._cols = cols
        folders = [i for i, e in enumerate(self.entries) if e[0] == "folder"]
        files = [i for i, e in enumerate(self.entries) if e[0] == "file"]
        row = 0
        if not self.entries:
            msg = ("No matching files." if self.query else
                   "Nothing here yet.\nDrop files here, or use ＋ New to upload." if not self.recent else "No files yet.")
            tk.Label(self.inner, text=msg, bg=ui.BG, fg=ui.MUTED, font=(ui.FONT, 12), justify="center").grid(
                row=0, column=0, columnspan=cols, pady=80, padx=40)
            return
        shown = 0
        for title, idxs, maker in (("Folders", folders, self._folder_tile), ("Files", files, self._file_tile)):
            if not idxs:
                continue
            tk.Label(self.inner, text=title, bg=ui.BG, fg=ui.TEXT, font=(ui.FONT, 10, "bold"), anchor="w").grid(
                row=row, column=0, columnspan=cols, sticky="w", padx=4, pady=(10, 6))
            row += 1
            drawn = 0
            for k, i in enumerate(idxs):
                if shown >= self.limit:
                    break
                t = maker(i)
                t.grid(row=row + k // cols, column=k % cols, padx=(0, CARD_GAP), pady=(0, CARD_GAP), sticky="nw")
                self.tiles[i] = t
                shown += 1
                drawn += 1
            row += (drawn + cols - 1) // cols
        if len(self.entries) > self.limit:
            b = ttk.Button(self.inner, text=f"Show more ({len(self.entries) - self.limit:,} left)", command=self._more)
            b.grid(row=row + 1, column=0, columnspan=cols, pady=12)
        for i in self.sel:
            self._paint(i)

    def _more(self):
        self.limit += PAGE
        y = self.canvas.yview()[0]
        self._draw_grid()
        self.canvas.update_idletasks()
        self.canvas.yview_moveto(y)

    def _bind_tile(self, widgets, i):
        for w in widgets:
            w.bind("<Button-1>", lambda e, i=i: self._click(i, e))
            w.bind("<Double-1>", lambda e, i=i: self._open_index(i))
            w.bind("<Button-3>", lambda e, i=i: self._tile_menu(i, e))
            w.bind("<Enter>", lambda e, i=i: self._hover(i, True))
            w.bind("<Leave>", lambda e, i=i: self._hover(i, False))
            try:
                if self.can_drop and self.entries[i][0] == "folder":
                    from tkinterdnd2 import DND_FILES
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", lambda e, i=i: (self.upload_paths(
                        list(self.app.root.tk.splitlist(e.data)), into=self.entries[i][1]), e.action)[1])
            except Exception:
                pass

    def _folder_tile(self, i):
        key = self.entries[i][1]
        t = tk.Frame(self.inner, bg=TILE_BG, width=CARD_W, height=ui.px(52), cursor="hand2")
        t.pack_propagate(False)
        ic = tk.Label(t, image=ui.icon("folder", 24), bg=TILE_BG)
        ic.pack(side="left", padx=(14, 10))
        nm = tk.Label(t, text=_short(key.split("/")[-1], 20), bg=TILE_BG, fg=ui.TEXT, font=(ui.FONT, 10, "bold"), anchor="w")
        nm.pack(side="left", fill="x", expand=True)
        t._parts = (t, ic, nm)
        t._bgparts = t._parts
        self._bind_tile(t._parts, i)
        return t

    def _file_tile(self, i):
        r = self.entries[i][1]
        typ = ui.type_of(r.name, r.kind)
        t = tk.Frame(self.inner, bg=TILE_BG, width=CARD_W, height=THUMB_H + ui.px(64), cursor="hand2")
        t.pack_propagate(False)
        head = tk.Frame(t, bg=TILE_BG)
        head.pack(fill="x", padx=10, pady=(9, 7))
        ic = tk.Label(head, image=ui.icon(typ, 20), bg=TILE_BG)
        ic.pack(side="left")
        nm = tk.Label(head, text=_short(r.name, 22), bg=TILE_BG, fg=ui.TEXT, font=(ui.FONT, 10), anchor="w")
        nm.pack(side="left", padx=(8, 0), fill="x", expand=True)
        box = tk.Frame(t, bg=ui.CARD, width=THUMB_W, height=THUMB_H)
        box.pack_propagate(False)
        box.pack(padx=12)
        pic = tk.Label(box, bg=ui.CARD, image=ui.icon(typ, 64))
        pic.pack(fill="both", expand=True)
        sub = tk.Label(t, text=self._sub(r), bg=TILE_BG, fg=ui.MUTED, font=(ui.FONT, 8), anchor="w")
        sub.pack(fill="x", padx=12, pady=(3, 0))
        t._parts = (t, head, ic, nm, box, pic, sub)
        t._bgparts = (t, head, ic, nm, sub)
        self._bind_tile(t._parts, i)
        if typ in ("photo", "pdf"):
            self._want_thumb(i, r, pic)
        return t

    def _sub(self, r):
        bits = [_when(r.mtime), core.human_size(r.size) if r.size else ""]
        if self.query or self.recent:
            bits.append(self._titles().get(r.root, ""))
        return "  ·  ".join(b for b in bits if b)

    def _paint(self, i, hover=False):
        t = self.tiles.get(i)
        if not t:
            return
        bg = ui.SELECTED if i in self.sel else ("#e1e5ea" if hover else TILE_BG)
        for w in t._bgparts:
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass

    def _hover(self, i, on):
        if i not in self.sel:
            self._paint(i, hover=on)

    def _click(self, i, e):
        self.canvas.focus_set()
        ctrl = e.state & 0x4
        shift = e.state & 0x1
        if shift and self.anchor is not None:
            a, b = sorted((self.anchor, i))
            self.sel = set(range(a, b + 1))
        elif ctrl:
            self.sel ^= {i}
            self.anchor = i
        else:
            self.sel = {i}
            self.anchor = i
        for k in self.tiles:
            self._paint(k)
        self._selection_changed()

    def _tile_menu(self, i, e):
        if i not in self.sel:
            self.sel = {i}
            for k in self.tiles:
                self._paint(k)
            self._selection_changed()
        self._menu(e)

    def _open_index(self, i):
        self.sel = {i}
        self.open_selected()

    # list view

    def _draw_list(self):
        self.tree.delete(*self.tree.get_children())
        self._iids = {}
        titles = self._titles()
        for i, e in enumerate(self.entries[:max(self.limit, 3000)]):
            if e[0] == "folder":
                n, b = self._folder_info.get(e[1], (0, 0))
                iid = self.tree.insert("", "end", text="  " + e[1].split("/")[-1], image=ui.icon("folder", 18),
                                       values=(f"{n:,} files", "", core.human_size(b) if b else ""))
            else:
                r = e[1]
                where = self._where(r, titles) if (self.query or self.recent) else (r.device or "")
                iid = self.tree.insert("", "end", text="  " + r.name, image=ui.icon(ui.type_of(r.name, r.kind), 18),
                                       values=(where, _when(r.mtime), core.human_size(r.size) if r.size else ""))
            self._iids[iid] = i
        self._syncing_tree = True
        self.tree.selection_set([iid for iid, i in self._iids.items() if i in self.sel])
        self._syncing_tree = False

    def _tree_selected(self):
        if getattr(self, "_syncing_tree", False):
            return
        self.sel = {self._iids[i] for i in self.tree.selection() if i in self._iids}
        self._selection_changed()

    # selection

    def selected(self):
        return [self.entries[i] for i in sorted(self.sel) if i < len(self.entries)]

    def selected_files(self):
        return [r for kind, r in self.selected() if kind == "file"]

    def select_none(self):
        self.sel = set()
        if self.view == "list":
            self.tree.selection_set([])
        for k in self.tiles:
            self._paint(k)
        self._selection_changed()

    def select_all(self):
        self.sel = set(range(len(self.entries)))
        if self.view == "list":
            self.tree.selection_set(list(self._iids))
        for k in self.tiles:
            self._paint(k)
        self._selection_changed()

    def _selection_changed(self):
        n = len(self.sel)
        if n:
            self.act_count.config(text=f"{n} selected")
            if not self.actbar.winfo_ismapped():
                self.actbar.pack(fill="x", pady=(0, 8), before=self.body)
        elif self.actbar.winfo_ismapped():
            self.actbar.pack_forget()
        self._details()

    # ---- details & thumbnails ----------------------------------------------------------------

    def _details(self):
        if not self.details_on:
            return
        sel = self.selected()
        if len(sel) != 1:
            self.det_img.config(image=ui.icon("folder" if not sel else "file", 96) if sel else "", text="")
            here = ("Search results" if self.query else "Recent" if self.recent else
                    self.path.split("/")[-1] if self.path else
                    "My Drive" if self.root_id == self.settings.get("root_page") else self._titles().get(self.root_id, ""))
            self.det_name.config(text=f"{len(sel)} items selected" if sel else here)
            self.det_text.config(text="" if sel else "Select a file or folder to see its details.")
            return
        kind, obj = sel[0]
        if kind == "folder":
            n, b = getattr(self, "_folder_info", {}).get(obj, (0, 0))
            self.det_img.config(image=ui.icon("folder", 96))
            self.det_name.config(text=obj.split("/")[-1])
            self.det_text.config(text=f"Folder\n\n{n:,} files · {core.human_size(b)}\n\nIn: {self._titles().get(self.root_id, '')}")
            return
        r = obj
        typ = ui.type_of(r.name, r.kind)
        self.det_img.config(image=ui.icon(typ, 96))
        self.det_name.config(text=r.name)
        lines = [f"Type:  {r.kind}", f"Size:  {core.human_size(r.size)}" if r.size else "",
                 f"Location:  {self._where(r)}",
                 "Modified:  " + time.strftime("%d %b %Y, %I:%M %p", time.localtime(r.mtime / 1000)) if r.mtime else "",
                 f"Came from:  {r.device}" if r.device else ""]
        try:
            import space
            if space.is_downloaded(r.block) or self.cloud().local_copy(r):
                lines.append("\n✓ Available on this laptop")
        except Exception:
            pass
        self.det_text.config(text="\n".join(l for l in lines if l))
        if typ in ("photo", "pdf"):
            self._want_thumb(-1, r, self.det_img, big=True)

    def _thumb_path(self, r):
        return os.path.join(CACHE_DIR, "thumbs", r.block + ".v2.png")

    def _want_thumb(self, i, r, label, big=False):
        size = (ui.px(260), ui.px(260)) if big else (THUMB_W, THUMB_H)
        p = self._thumb_path(r)
        if os.path.isfile(p):
            self._set_thumb(label, p, size, r)
        elif r.size and r.size <= THUMB_MAX_BYTES:
            self.thumb_q.put((self.gen, r, label, size))

    def _set_thumb(self, label, p, size, r=None):
        try:
            from PIL import Image, ImageOps, ImageTk
            im = Image.open(p).convert("RGB")
            pdf = r is not None and ui.type_of(r.name, r.kind) == "pdf"
            if size == (ui.px(260), ui.px(260)):
                im = ImageOps.contain(im, size, Image.LANCZOS)
            else:   # fill the card; for documents show the top of the first page
                im = ImageOps.fit(im, size, Image.LANCZOS, centering=(0.5, 0.0) if pdf else (0.5, 0.5))
            ph = ImageTk.PhotoImage(im)
            label.configure(image=ph)
            label._ph = ph
        except Exception:
            pass

    def _thumb_worker(self):
        """Makes photo previews one by one, in the background. Only the small preview is kept."""
        while True:
            gen, r, label, size = self.thumb_q.get()
            if gen != self.gen:
                continue
            p = self._thumb_path(r)
            try:
                if not os.path.isfile(p):
                    self._make_thumb(r, p)
            except Exception as e:
                self._thumb_errors = getattr(self, "_thumb_errors", 0) + 1
                if self._thumb_errors <= 5:
                    self._ui(lambda e=e, n=r.name: self.app.log(f"Couldn't make a preview of {n}: {e}"))
                continue
            if not os.path.isfile(p):
                continue

            def put(label=label, p=p, size=size, gen=gen, r=r):
                if gen == self.gen and label.winfo_exists():
                    self._set_thumb(label, p, size, r)
            self._ui(put)

    def _make_thumb(self, r, dest):
        from PIL import Image, ImageOps
        c = self.cloud()
        src = c.local_copy(r)
        safe = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in r.name)
        cached = os.path.join(CACHE_DIR, r.block, safe)
        tmp = None
        if not src and os.path.isfile(cached):
            src = cached
        if not src:
            import requests
            url = c.fresh_url(r)
            if not url:
                return
            fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(r.name)[1])
            os.close(fd)
            with requests.get(url, stream=True, timeout=(20, 60)) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as out:
                    for chunk in resp.iter_content(256 * 1024):
                        out.write(chunk)
            src = tmp
        try:
            if ui.type_of(r.name, r.kind) == "pdf":
                import pypdfium2 as pdfium
                doc = pdfium.PdfDocument(src)
                try:
                    page = doc[0]
                    im = page.render(scale=560 / max(1, page.get_width())).to_pil().convert("RGB")
                    page.close()
                finally:
                    doc.close()
            else:
                im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
            im.thumbnail((560, 720))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            im.save(dest + ".part.png")
            os.replace(dest + ".part.png", dest)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ---- navigation ---------------------------------------------------------------------------

    def go_to(self, path):
        self.path = path
        self.recent = False
        self.show()

    def go_up(self):
        if self.query:
            self.clear_search()
        elif self.path:
            self.go_to(self.path.rpartition("/")[0])

    def _search_typed(self):
        if getattr(self.search_entry, "_hint", False):
            return
        if self._search_job:
            self.app.root.after_cancel(self._search_job)
        self._search_job = self.app.root.after(350, self.search)

    def search(self):
        self._search_job = None
        q = self._search_text()
        if q == self.query:
            return
        self.query = q
        self.show()

    def clear_search(self, show=True):
        if not getattr(self.search_entry, "_hint", False):
            self.search_var.set("")
        self.query = ""
        if self.app.root.focus_get() is not self.search_entry:
            self.search_entry.event_generate("<FocusOut>")
        if show:
            self.show()

    # ---- actions --------------------------------------------------------------------------------

    def _run(self, label, fn, after=None):
        """Runs fn(cloud, progress) in the background with status updates, then refreshes."""
        self._set_status(label + "…")

        def work():
            err = None
            result = None
            try:
                result = fn(self.cloud(), lambda i, n: self._ui(lambda: self._set_status(f"{label}… {i + 1:,} of {n:,}")))
            except Exception as e:
                err = e

            def done():
                if err:
                    self._set_status(f"{label} failed: {err}")
                    messagebox.showerror("Notion Laptop Sync", f"{label} failed:\n{err}")
                else:
                    self.show(keep_sel=True)
                    if after:
                        after(result)
            self._ui(done)

        threading.Thread(target=work, daemon=True).start()

    def open_selected(self):
        sel = self.selected()
        if not sel:
            return
        kind, obj = sel[0]
        if kind == "folder":
            self.sel = set()
            self.go_to(obj)
            return
        r = obj
        self._run(f"Getting {r.name}", lambda c, p: c.download(r, progress=lambda d, t: self._ui(
            lambda: self._set_status(f"Downloading {r.name}… {d * 100 // t if t else 0}%"))),
            after=lambda path: (os.startfile(path), self._set_status(f"Opened {r.name}")))

    def save_as(self):
        files = self.selected_files()
        if not files:
            return
        if len(files) == 1:
            r = files[0]
            dest = filedialog.asksaveasfilename(initialfile=r.name, title="Download a copy to")
            if not dest:
                return
            targets = [(r, dest)]
        else:
            folder = filedialog.askdirectory(title=f"Download {len(files)} files to")
            if not folder:
                return
            targets = [(r, os.path.join(folder, r.name)) for r in files]

        def fn(c, p):
            for i, (r, dest) in enumerate(targets):
                p(i, len(targets))
                shutil.copy2(c.download(r), dest)
                self._remember_saved(dest, r)
        self._run("Downloading", fn, after=lambda _: self._set_status(f"Downloaded {len(targets)} file(s)."))

    def _remember_saved(self, dest, r):
        """If saved into a backed-up folder, record it as already in the cloud (so it isn't uploaded again)."""
        n = core.norm(dest)
        for root in self.settings.get("folders") or []:
            if n.startswith(core.norm(root) + "\\") or n.startswith(core.norm(root) + "/"):
                st = os.stat(dest)
                self.db.save(dest, st.st_size, int(st.st_mtime * 1000), "done", r.block, None, 0)
                self.db.set_index_id(dest, r.row_id)
                return

    def _target(self):
        """Where new uploads go: the open folder, or My Drive when looking at search results or Recent."""
        if self.query or self.recent or not self.root_id:
            return self.settings.get("root_page"), ""
        return self.root_id, self.path

    def new_menu(self, widget):
        m = tk.Menu(widget, tearoff=0)
        m.add_command(label="New folder", command=self.new_folder)
        m.add_separator()
        m.add_command(label="File upload", command=self.upload)
        m.add_command(label="Folder upload", command=self.upload_folder)
        m.tk_popup(widget.winfo_rootx(), widget.winfo_rooty() + widget.winfo_height())

    def upload(self):
        paths = filedialog.askopenfilenames(title="Upload files")
        if paths:
            self.upload_paths(list(paths))

    def upload_folder(self):
        d = filedialog.askdirectory(title="Upload a folder")
        if d:
            self.upload_paths([d])

    def upload_paths(self, paths, into=None):
        """Uploads files and whole folders (keeping their folder structure)."""
        root, dest = self._target()
        if not root:
            return
        if into is not None:
            dest = into
        jobs = []
        for p in paths:
            p = os.path.normpath(p)
            if os.path.isdir(p):
                base = os.path.basename(p)
                for d, dirs, files in os.walk(p):
                    dirs[:] = [x for x in dirs if not x.startswith(".")]
                    rel = os.path.relpath(d, p).replace("\\", "/")
                    sub = base if rel == "." else f"{base}/{rel}"
                    for f in files:
                        jobs.append((os.path.join(d, f), f"{dest}/{sub}" if dest else sub))
            elif os.path.isfile(p):
                jobs.append((p, dest))
        if not jobs:
            return
        if len(jobs) > 200 and not messagebox.askyesno("Upload", f"Upload {len(jobs):,} files?"):
            return

        def fn(c, prog):
            for i, (path, d) in enumerate(jobs):
                self._ui(lambda i=i, path=path: self._set_status(
                    f"Uploading {i + 1:,} of {len(jobs):,}: {os.path.basename(path)}"))
                c.upload(path, root, d)
        self._run("Uploading", fn, after=lambda _: self._set_status(f"Uploaded {len(jobs):,} file(s)."))

    def new_folder(self):
        root, parent = self._target()
        if not root:
            return
        name = simpledialog.askstring("New folder", "Folder name:", initialvalue="Untitled folder", parent=self.canvas)
        if not name or not name.strip():
            return
        name = name.strip().replace("/", "-")
        if self.query or self.recent:
            self.clear_search(show=False)
            self.recent = False
            self.root_id, self.path = root, ""
        self._run("Creating folder", lambda c, p: c.new_folder(root, parent, name))

    def rename(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        kind, obj = sel[0]
        old = obj.split("/")[-1] if kind == "folder" else obj.name
        name = simpledialog.askstring("Rename", "New name:", initialvalue=old, parent=self.canvas)
        if not name or name.strip() == old:
            return
        name = name.strip().replace("/", "-")
        if kind == "folder":
            root = self.root_id
            self._run("Renaming folder", lambda c, p: c.rename_folder(root, obj, name, p))
        else:
            self._run("Renaming", lambda c, p: c.rename(obj, name))

    def move(self):
        files = self.selected_files()
        if not files:
            messagebox.showinfo("Notion Laptop Sync", "Select one or more files to move.")
            return
        root = files[0].root
        if any(r.root != root for r in files):
            messagebox.showinfo("Notion Laptop Sync", "Files can only move within the same device.")
            return
        dest = self._pick_folder(root, f"Move {len(files)} file(s) to")
        if dest is None:
            return

        def fn(c, p):
            for i, r in enumerate(files):
                p(i, len(files))
                c.move(r, dest)
        self._run("Moving", fn)

    def _pick_folder(self, root, title):
        """A small window listing every cloud folder on a device; returns the chosen path or None."""
        win = tk.Toplevel(self.canvas)
        win.title(title)
        win.geometry("420x480")
        win.transient(self.canvas.winfo_toplevel())
        tree = ttk.Treeview(win, show="tree")
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        top = tree.insert("", "end", text="  " + self._titles().get(root, "Backup"), image=ui.icon("folder", 18), open=True)
        paths = {top: ""}

        def add(parent_iid, path):
            for name, _, _ in self.db.sub_folders(root, path):
                p = f"{path}/{name}" if path else name
                iid = tree.insert(parent_iid, "end", text="  " + name, image=ui.icon("folder", 18))
                paths[iid] = p
                add(iid, p)
        add(top, "")
        result = {"path": None}

        def ok():
            sel = tree.selection()
            if sel:
                result["path"] = paths[sel[0]]
            win.destroy()
        ttk.Button(win, text="Move here", style="Accent.TButton", command=ok).pack(pady=(0, 8))
        tree.selection_set(top)
        win.grab_set()
        win.wait_window()
        return result["path"]

    def delete(self):
        sel = self.selected()
        if not sel:
            return
        folders = [k for kind, k in sel if kind == "folder"]
        files = [r for kind, r in sel if kind == "file"]
        what = ", ".join(filter(None, [f"{len(files)} file(s)" if files else "", f"{len(folders)} folder(s)" if folders else ""]))
        if not messagebox.askyesno("Delete", f"Delete {what} from the cloud?\n\nThey move to Notion's trash "
                                             "(recoverable there for 30 days). Copies on your laptop and phone are not touched."):
            return
        root = self.root_id
        self.sel = set()

        def fn(c, p):
            for i, r in enumerate(files):
                p(i, len(files))
                c.delete(r)
            for f in folders:
                c.delete_folder(root, f, p)
        self._run("Deleting", fn)

    def show_storage(self):
        out = []
        for sid, title in self.cloud().sources():
            (n, b), kinds = self.db.storage(sid)
            if not n:
                continue
            out.append(f"{title}: {n:,} files · {core.human_size(b)}")
            for k, c, s in kinds:
                out.append(f"     {k}: {c:,} · {core.human_size(s or 0)}")
            top = sorted(self.db.sub_folders(sid, ""), key=lambda t: -t[2])[:5]
            if top:
                out.append("     Biggest folders: " + ", ".join(f"{t[0]} ({core.human_size(t[2])})" for t in top))
            out.append("")
        messagebox.showinfo("Storage", "\n".join(out).strip() or "Nothing in the file index yet.")

    def context_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return self.background_menu(event)
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self._menu(event)

    def background_menu(self, event):
        m = tk.Menu(self.canvas, tearoff=0)
        m.add_command(label="New folder", command=self.new_folder)
        m.add_command(label="File upload", command=self.upload)
        m.add_command(label="Folder upload", command=self.upload_folder)
        m.add_separator()
        m.add_command(label="Storage used", command=self.show_storage)
        m.add_command(label="Refresh", command=lambda: self.refresh(full=True))
        m.tk_popup(event.x_root, event.y_root)

    def _menu(self, event):
        m = tk.Menu(self.canvas, tearoff=0)
        m.add_command(label="Open", command=self.open_selected)
        if self.selected_files():
            m.add_command(label="Download", command=self.save_as)
        m.add_separator()
        m.add_command(label="Rename", command=self.rename)
        m.add_command(label="Move to…", command=self.move)
        m.add_command(label="File information", command=lambda: (not self.details_on and self.toggle_details()))
        files = self.selected_files()
        import space
        if len(files) == 1 and space.is_downloaded(files[0].block):
            m.add_command(label="Remove downloaded copy", command=lambda r=files[0]: (
                space.remove_download(r.block), self._set_status(f"Removed the downloaded copy of {r.name}. Still in the cloud.")))
        if (self.query or self.recent) and len(files) == 1:
            r = files[0]
            m.add_command(label="Show file location", command=lambda: self._show_in_folder(r))
        m.add_separator()
        m.add_command(label="Delete", command=self.delete)
        m.tk_popup(event.x_root, event.y_root)

    def _show_in_folder(self, r):
        self.clear_search(show=False)
        self.recent = False
        self.root_id, self.path = r.root, r.path
        self.show()
        for i, e in enumerate(self.entries):
            if e[0] == "file" and e[1].block == r.block:
                self.sel = {i}
                for k in self.tiles:
                    self._paint(k)
                self._selection_changed()
                break
