"""The Space tab: how much the app stores, and its downloaded copies of cloud files."""
import os
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import core
import space

LIMITS = [("250 MB", 250), ("500 MB", 500), ("1 GB", 1024), ("2 GB", 2048), ("5 GB", 5120), ("10 GB", 10240)]


class SpaceTab:
    def __init__(self, app, f):
        self.app = app
        self.settings = app.settings
        self.db = app.db
        self._build(f)

    def _h(self, f, text):
        ttk.Label(f, text=text, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(10, 4))

    def _build(self, f):
        self._h(f, "This app uses")
        self.usage_lbl = ttk.Label(f, text="…", font=("Segoe UI", 18, "bold"))
        self.usage_lbl.pack(anchor="w")
        self.usage_detail = ttk.Label(f, foreground="#555")
        self.usage_detail.pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=6)
        ttk.Label(row, text="Keep files opened from the cloud up to:").pack(side="left")
        self.limit_var = tk.StringVar(value=next((l for l, mb in LIMITS if mb == self.settings.get("cache_limit_mb")), "1 GB"))
        cb = ttk.Combobox(row, textvariable=self.limit_var, values=[l for l, _ in LIMITS], state="readonly", width=8)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_limit())
        ttk.Button(row, text="See downloads", command=self.see_downloads).pack(side="left", padx=(12, 4))
        ttk.Button(row, text="Clear downloads", command=self.clear_downloads).pack(side="left")
        ttk.Label(f, text="Files you open from the cloud are kept here so they open quickly next time; "
                          "the oldest are removed automatically when the limit is reached.",
                  foreground="#666", wraplength=720).pack(anchor="w")

        ttk.Label(f, text="Only copies downloaded from the cloud are counted and cleared here. "
                          "Your own files on this laptop are never removed.", foreground="#666", wraplength=720).pack(anchor="w", pady=(8, 0))

    def _ui(self, fn):
        self.app.events.put(fn)

    def on_shown(self):
        self.refresh()

    def refresh(self):
        def work():
            dl, th, data = space.usage()

            def show():
                self.usage_lbl.config(text=core.human_size(dl + th + data))
                self.usage_detail.config(text=f"Downloaded files {core.human_size(dl)} (limit {self.limit_var.get()}) · "
                                              f"previews {core.human_size(th)} · app data {core.human_size(data)}")
            self._ui(show)
        threading.Thread(target=work, daemon=True).start()

    def _set_limit(self):
        self.settings.set("cache_limit_mb", dict(LIMITS)[self.limit_var.get()])
        threading.Thread(target=lambda: (space.trim_all(self.settings), self._ui(self.refresh)), daemon=True).start()

    def see_downloads(self):
        items = space.downloads()
        win = tk.Toplevel(self.usage_lbl)
        win.title("Downloaded on this laptop")
        win.geometry("640x420")
        box = scrolledtext.ScrolledText(win, font=("Consolas", 9))
        box.pack(fill="both", expand=True)
        total = sum(s for s, _ in items)
        box.insert("end", f"{len(items):,} files · {core.human_size(total)}\n\n" if items else "Nothing downloaded right now.")
        for s, p in items[:300]:
            box.insert("end", f"{core.human_size(s):>10}   {os.path.basename(p)}\n")
        box.configure(state="disabled")

    def clear_downloads(self):
        if messagebox.askyesno("Clear downloads", "Remove the copies of cloud files you opened on this laptop, and the previews?\n\n"
                                                  "Nothing is removed from the cloud."):
            space.clear_downloads()
            self.refresh()
