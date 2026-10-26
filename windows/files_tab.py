"""The Files tab: your cloud drive on the laptop."""
import os
import shutil
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import core
from cloud import CACHE_DIR, Cloud, Row

ICONS = {"Folder": "📁", "Photo": "🖼", "Video": "🎬", "Audio": "🎵", "PDF": "📕", "Document": "📄", "Other": "📄"}
SORTS = [("Newest first", "mtime DESC"), ("Oldest first", "mtime ASC"),
         ("Name (A–Z)", "name COLLATE NOCASE ASC"), ("Largest first", "size DESC")]


class FilesTab:
    def __init__(self, app, frame):
        self.app = app
        self.settings = app.settings
        self.db = app.db
        self.root_id = ""
        self.path = ""
        self.query = ""
        self.items = {}          # tree item id -> ("folder", key) or ("file", Row)
        self.refreshing = False
        self.last_refresh = 0
        self.preview_img = None
        self.preview_for = None
        self._build(frame)

    # ---- layout ---------------------------------------------------------------------

    def _build(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Show:").pack(side="left")
        self.source_var = tk.StringVar()
        self.source_box = ttk.Combobox(top, textvariable=self.source_var, state="readonly", width=28)
        self.source_box.pack(side="left", padx=(4, 12))
        self.source_box.bind("<<ComboboxSelected>>", lambda e: self._source_chosen())
        self.search_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.search_var, width=30)
        e.pack(side="left")
        e.bind("<Return>", lambda ev: self.search())
        ttk.Button(top, text="🔍 Search all", command=self.search).pack(side="left", padx=4)
        ttk.Button(top, text="Clear", command=self.clear_search).pack(side="left")
        ttk.Button(top, text="⟳ Refresh", command=lambda: self.refresh(full=True)).pack(side="right")
        ttk.Button(top, text="Storage", command=self.show_storage).pack(side="right", padx=4)

        nav = ttk.Frame(f)
        nav.pack(fill="x", pady=(8, 4))
        ttk.Button(nav, text="⬆ Up", width=6, command=self.go_up).pack(side="left")
        self.crumbs = ttk.Frame(nav)
        self.crumbs.pack(side="left", padx=8)
        ttk.Label(nav, text="Sort:").pack(side="right")
        self.sort_var = tk.StringVar(value=SORTS[0][0])
        sb = ttk.Combobox(nav, textvariable=self.sort_var, state="readonly", width=14, values=[s[0] for s in SORTS])
        sb.pack(side="right", padx=4)
        sb.bind("<<ComboboxSelected>>", lambda e: self.show())

        acts = ttk.Frame(f)
        acts.pack(fill="x", pady=(0, 6))
        for text, cmd in [("⬆ Upload files…", self.upload), ("＋ New folder", self.new_folder),
                          ("Open", self.open_selected), ("Save as…", self.save_as),
                          ("Rename", self.rename), ("Move…", self.move), ("Delete", self.delete)]:
            ttk.Button(acts, text=text, command=cmd).pack(side="left", padx=(0, 4))

        pane = ttk.PanedWindow(f, orient="horizontal")
        pane.pack(fill="both", expand=True)
        left = ttk.Frame(pane)
        cols = ("size", "date", "kind")
        self.tree = ttk.Treeview(left, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="Name")
        self.tree.heading("size", text="Size")
        self.tree.heading("date", text="Modified")
        self.tree.heading("kind", text="Type / location")
        self.tree.column("#0", width=330)
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("date", width=130)
        self.tree.column("kind", width=170)
        sc = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sc.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<BackSpace>", lambda e: self.go_up())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.preview())
        self.tree.bind("<Button-3>", self.context_menu)
        pane.add(left, weight=3)

        right = ttk.Frame(pane, padding=8)
        self.preview_label = ttk.Label(right, anchor="center")
        self.preview_label.pack(fill="x")
        self.preview_text = ttk.Label(right, wraplength=260, justify="left")
        self.preview_text.pack(fill="x", pady=8)
        pane.add(right, weight=1)

        self.status = ttk.Label(f, foreground="#555")
        self.status.pack(fill="x", pady=(6, 0))

    # ---- sources & refresh ---------------------------------------------------------------

    def cloud(self, stop=None):
        return Cloud(self.settings, self.db, stop)

    def on_shown(self):
        if not self.settings.get("token") or not self.settings.get("root_page"):
            self._set_status("Connect Notion in the Notion tab first.")
            return
        self._fill_sources()
        if not self.root_id:
            self.root_id = self.settings.get("root_page")
            self._fill_sources()
        self.show()
        if time.time() - self.last_refresh > 120:
            self.refresh(full=False)

    def _fill_sources(self):
        srcs = self.cloud().sources()
        self.source_box["values"] = [t for _, t in srcs]
        for sid, t in srcs:
            if sid == self.root_id:
                self.source_var.set(t)

    def _source_chosen(self):
        title = self.source_var.get()
        for sid, t in self.cloud().sources():
            if t == title:
                self.root_id, self.path = sid, ""
                self.clear_search(show=False)
                self.show()
                return

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
                    self._fill_sources()
                    self.show()
                    if err:
                        self._set_status(f"Couldn't refresh everything: {err}")
                self._ui(done)

        threading.Thread(target=work, daemon=True).start()

    def _ui(self, fn):
        self.app.events.put(fn)

    def _set_status(self, text):
        self.status.config(text=text)

    # ---- showing ---------------------------------------------------------------------------

    def _order(self):
        return dict(SORTS).get(self.sort_var.get(), "mtime DESC")

    def _where(self, r):
        title = dict(self.cloud().sources()).get(r.root, "Backup")
        return title + (" › " + r.path.replace("/", " › ") if r.path else "")

    def show(self):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        if not self.root_id:
            return
        if self.query:
            roots = [sid for sid, _ in self.cloud().sources()]
            rows = [Row(t) for t in self.db.search_idx(roots, self.query)]
            for r in rows:
                self._add_file(r, where=True)
            self._render_crumbs()
            self._set_status(f"{len(rows):,} result(s) for “{self.query}”" + (" (showing the newest 500)" if len(rows) >= 500 else ""))
            return
        folders = self.db.sub_folders(self.root_id, self.path)
        if self._order().startswith("size"):
            folders.sort(key=lambda t: -t[2])
        for name, n, b in folders:
            key = f"{self.path}/{name}" if self.path else name
            iid = self.tree.insert("", "end", text=f"{ICONS['Folder']}  {name}",
                                   values=(core.human_size(b) if b else "", "", f"{n:,} files"))
            self.items[iid] = ("folder", key)
        files = [Row(t) for t in self.db.files_in(self.root_id, self.path, self._order())]
        for r in files:
            self._add_file(r, where=False)
        self._render_crumbs()
        n, b = self.db.storage(self.root_id)[0]
        if n == 0 and not self.db.get_meta("index_db:" + self.root_id):
            self._set_status("This backup has no file index yet. It's created by the next sync of the app that uploads to it.")
        else:
            pending = self.db.count_pending_index() if self.root_id == self.settings.get("root_page") else 0
            extra = f"  ·  Adding {pending:,} earlier uploads to the index…" if pending else ""
            self._set_status(f"{n:,} files · {core.human_size(b)} in this backup{extra}")

    def _add_file(self, r, where):
        date = time.strftime("%d %b %Y %H:%M", time.localtime(r.mtime / 1000)) if r.mtime else ""
        iid = self.tree.insert("", "end", text=f"{ICONS.get(r.kind, '📄')}  {r.name}",
                               values=(core.human_size(r.size) if r.size else "", date,
                                       self._where(r) if where else r.kind))
        self.items[iid] = ("file", r)

    def _render_crumbs(self):
        for w in self.crumbs.winfo_children():
            w.destroy()
        if self.query:
            ttk.Label(self.crumbs, text="Search results", font=("Segoe UI", 10, "bold")).pack(side="left")
            return
        title = dict(self.cloud().sources()).get(self.root_id, "Backup")
        parts = [(title, "")]
        segs = self.path.split("/") if self.path else []
        for i, s in enumerate(segs):
            parts.append((s, "/".join(segs[:i + 1])))
        for i, (t, p) in enumerate(parts):
            if i:
                ttk.Label(self.crumbs, text="›").pack(side="left", padx=2)
            if i == len(parts) - 1:
                ttk.Label(self.crumbs, text=t, font=("Segoe UI", 10, "bold")).pack(side="left")
            else:
                lbl = ttk.Label(self.crumbs, text=t, foreground="#2563EB", cursor="hand2")
                lbl.pack(side="left")
                lbl.bind("<Button-1>", lambda e, p=p: self.go_to(p))

    def go_to(self, path):
        self.path = path
        self.show()

    def go_up(self):
        if self.query:
            self.clear_search()
        elif self.path:
            self.go_to(self.path.rpartition("/")[0])

    def search(self):
        self.query = self.search_var.get().strip()
        self.show()

    def clear_search(self, show=True):
        self.search_var.set("")
        self.query = ""
        if show:
            self.show()

    def selected(self):
        return [self.items[i] for i in self.tree.selection() if i in self.items]

    def selected_files(self):
        return [r for kind, r in self.selected() if kind == "file"]

    # ---- preview ----------------------------------------------------------------------------

    def preview(self):
        sel = self.selected()
        if len(sel) != 1:
            self.preview_label.config(image="", text="")
            self.preview_text.config(text=f"{len(sel)} selected" if sel else "")
            return
        kind, obj = sel[0]
        if kind == "folder":
            self.preview_label.config(image="", text="📁", font=("Segoe UI Emoji", 48))
            self.preview_text.config(text=obj.split("/")[-1])
            return
        r = obj
        lines = [r.name, self._where(r)]
        if r.size:
            lines.append(core.human_size(r.size))
        if r.mtime:
            lines.append(time.strftime("%d %b %Y, %H:%M", time.localtime(r.mtime / 1000)))
        if r.device:
            lines.append(f"From: {r.device}")
        self.preview_text.config(text="\n".join(lines))
        self.preview_label.config(image="", text=ICONS.get(r.kind, "📄"), font=("Segoe UI Emoji", 48))
        if r.kind != "Photo":
            return
        self.preview_for = r.block

        def work():
            try:
                from PIL import Image, ImageOps
                thumb = os.path.join(CACHE_DIR, "thumbs", r.block + ".png")
                if not os.path.isfile(thumb):
                    src = self.cloud().download(r)
                    im = ImageOps.exif_transpose(Image.open(src))
                    im.thumbnail((280, 280))
                    os.makedirs(os.path.dirname(thumb), exist_ok=True)
                    im.save(thumb)
            except Exception:
                return

            def show():
                if self.preview_for != r.block:
                    return
                try:
                    from PIL import Image, ImageTk
                    self.preview_img = ImageTk.PhotoImage(Image.open(thumb))
                    self.preview_label.config(image=self.preview_img, text="")
                except Exception:
                    pass
            self._ui(show)

        threading.Thread(target=work, daemon=True).start()

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
                    self.show()
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
            dest = filedialog.asksaveasfilename(initialfile=r.name, title="Save a copy as")
            if not dest:
                return
            targets = [(r, dest)]
        else:
            folder = filedialog.askdirectory(title=f"Save {len(files)} files to")
            if not folder:
                return
            targets = [(r, os.path.join(folder, r.name)) for r in files]

        def fn(c, p):
            for i, (r, dest) in enumerate(targets):
                p(i, len(targets))
                shutil.copy2(c.download(r), dest)
                self._remember_saved(dest, r)
        self._run("Saving", fn, after=lambda _: self._set_status(f"Saved {len(targets)} file(s)."))

    def _remember_saved(self, dest, r):
        """If saved into a backed-up folder, record it as already in the cloud (so it isn't uploaded again)."""
        n = core.norm(dest)
        for root in self.settings.get("folders") or []:
            if n.startswith(core.norm(root) + "\\") or n.startswith(core.norm(root) + "/"):
                st = os.stat(dest)
                self.db.save(dest, st.st_size, int(st.st_mtime * 1000), "done", r.block, None, 0)
                self.db.set_index_id(dest, r.row_id)
                return

    def upload(self):
        if not self.root_id:
            return
        if self.query:
            messagebox.showinfo("Notion Laptop Sync", "Open a folder first (clear the search).")
            return
        paths = filedialog.askopenfilenames(title="Upload files to this cloud folder")
        if not paths:
            return
        root, dest = self.root_id, self.path

        def fn(c, p):
            for i, path in enumerate(paths):
                self._ui(lambda i=i, path=path: self._set_status(f"Uploading {i + 1} of {len(paths)}: {os.path.basename(path)}"))
                c.upload(path, root, dest)
        self._run("Uploading", fn, after=lambda _: self._set_status(f"Uploaded {len(paths)} file(s)."))

    def new_folder(self):
        if self.query or not self.root_id:
            return
        name = simpledialog.askstring("New folder", "Folder name:", parent=self.tree)
        if not name or not name.strip():
            return
        name = name.strip().replace("/", "-")
        root, parent = self.root_id, self.path
        self._run("Creating folder", lambda c, p: c.new_folder(root, parent, name))

    def rename(self):
        sel = self.selected()
        if len(sel) != 1:
            return
        kind, obj = sel[0]
        old = obj.split("/")[-1] if kind == "folder" else obj.name
        name = simpledialog.askstring("Rename", "New name:", initialvalue=old, parent=self.tree)
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
            messagebox.showinfo("Notion Laptop Sync", "Files can only move within the same backup.")
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
        """A small window listing every cloud folder in a backup; returns the chosen path or None."""
        win = tk.Toplevel(self.tree)
        win.title(title)
        win.geometry("420x480")
        win.transient(self.tree.winfo_toplevel())
        tree = ttk.Treeview(win, show="tree")
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        top = tree.insert("", "end", text=dict(self.cloud().sources()).get(root, "Backup"), open=True, values=("",))
        paths = {top: ""}

        def add(parent_iid, path):
            for name, _, _ in self.db.sub_folders(root, path):
                p = f"{path}/{name}" if path else name
                iid = tree.insert(parent_iid, "end", text=f"📁 {name}")
                paths[iid] = p
                add(iid, p)
        add(top, "")
        result = {"path": None}

        def ok():
            sel = tree.selection()
            if sel:
                result["path"] = paths[sel[0]]
            win.destroy()
        ttk.Button(win, text="Move here", command=ok).pack(pady=(0, 8))
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
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        m = tk.Menu(self.tree, tearoff=0)
        m.add_command(label="Open", command=self.open_selected)
        m.add_command(label="Save as…", command=self.save_as)
        m.add_separator()
        m.add_command(label="Rename", command=self.rename)
        m.add_command(label="Move…", command=self.move)
        m.add_command(label="Delete", command=self.delete)
        files = self.selected_files()
        import space
        if len(files) == 1 and space.is_downloaded(files[0].block):
            m.add_command(label="Remove downloaded copy", command=lambda r=files[0]: (
                space.remove_download(r.block), self._set_status(f"Removed the downloaded copy of {r.name}. Still in the cloud.")))
        if self.query and len(files) == 1:
            r = files[0]
            m.add_separator()
            m.add_command(label="Show in folder", command=lambda: self._show_in_folder(r))
        m.tk_popup(event.x_root, event.y_root)

    def _show_in_folder(self, r):
        self.clear_search(show=False)
        self.root_id, self.path = r.root, r.path
        self._fill_sources()
        self.show()
