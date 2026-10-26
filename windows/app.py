"""Notion Laptop Sync: backs up folders on this laptop to a Notion page."""
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import requests

import core
import winutil
from files_tab import FilesTab
from notion import TRAFFIC, NotionClient, NotionError, parse_page_id
from sync import Syncer
from version import BUILD

REPO = "sakib1322/NotionPhoneSync"
INSTANCE_PORT = 47391
TITLE = "Notion Laptop Sync"
TAB_FILES, TAB_NOTION, TAB_ACTIVITY = 1, 3, 4


class App:
    def __init__(self, minimized, listener):
        self.settings = core.Settings()
        self.db = core.Db()
        self.log = core.Log()
        self.listener = listener
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.sync_thread = None
        self.syncer = None
        self.status = {"text": "", "done": 0, "total": 0}
        self.started = time.time()
        self.updating = False
        self.quitting = False
        self.tray = None
        self.last_traffic = (TRAFFIC.up, TRAFFIC.down, time.monotonic())
        self.speed = (0.0, 0.0)
        self.session_start = (TRAFFIC.up, TRAFFIC.down)

        if self.settings.get("folders") is None:
            folders = winutil.default_folders()
            self.settings.set("folders", folders)
            self.settings.set("labels", self._labels_for(folders, {}))
        if self.settings.get("startup"):
            try:
                winutil.set_startup(True)
            except OSError:
                pass

        self.root = tk.Tk()
        self.root.title(TITLE)
        self.root.geometry("780x660")
        self.root.minsize(640, 520)
        try:
            self.root.iconbitmap(winutil.resource("icon.ico"))
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._build()
        self._start_tray()
        threading.Thread(target=self._listen_for_second_launch, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()
        if minimized:
            self.root.withdraw()
        self.root.after(300, self._tick)
        self.root.after(1500, lambda: self.check_update(install=False))

    # ---- layout --------------------------------------------------------------

    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.nb = nb
        self._build_backup(self._tab(nb, "Backup"))
        self.files = FilesTab(self, self._tab(nb, "Files"))
        self._build_folders(self._tab(nb, "Folders"))
        self._build_notion(self._tab(nb, "Notion"))
        self._build_activity(self._tab(nb, "Activity"))
        self._build_about(self._tab(nb, "Updates & settings"))
        nb.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        if not self.settings.get("token"):
            nb.select(TAB_NOTION)

    def _tab_changed(self):
        try:
            if self.nb.index("current") == TAB_FILES:
                self.files.on_shown()
        except Exception as e:
            self.log(f"Files tab error: {e}")

    def _tab(self, nb, name):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text=name)
        return f

    def _h(self, parent, text):
        ttk.Label(parent, text=text, font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(10, 4))

    def _p(self, parent, text, **kw):
        lbl = ttk.Label(parent, text=text, wraplength=720, justify="left", **kw)
        lbl.pack(anchor="w", pady=1)
        return lbl

    def _build_backup(self, f):
        self._h(f, "Speed")
        self.speed_lbl = ttk.Label(f, text="↑ –     ↓ –", font=("Consolas", 20, "bold"))
        self.speed_lbl.pack(anchor="w")
        self.speed_note = self._p(f, "", foreground="#666")

        self._h(f, "Status")
        self.state_lbl = self._p(f, "")
        self.current_lbl = self._p(f, "", foreground="#444")
        self.bar = ttk.Progressbar(f, mode="determinate", length=720)
        self.bar.pack(anchor="w", fill="x", pady=4)
        self.counts_lbl = self._p(f, "")

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="Sync now", command=self.sync_now).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Stop syncing", command=self.stop_sync).pack(side="left", padx=6)
        ttk.Button(row, text="Show skipped & failed", command=self.show_problems).pack(side="left", padx=6)
        ttk.Button(row, text="Try them again", command=self.retry_problems).pack(side="left", padx=6)

        self._h(f, "Auto-sync")
        self.auto_var = tk.BooleanVar(value=self.settings.get("auto_sync"))
        self.auto_cb = ttk.Checkbutton(f, variable=self.auto_var, command=self.toggle_auto)
        self.auto_cb.pack(anchor="w")
        self._update_auto_label()

        presets = ttk.Frame(f)
        presets.pack(anchor="w", pady=4)
        for label, minutes in [("5m", 5), ("15m", 15), ("30m", 30), ("1h", 60), ("2h", 120),
                               ("6h", 360), ("12h", 720), ("1d", 1440)]:
            ttk.Button(presets, text=label, width=5,
                       command=lambda m=minutes: self.set_interval(m)).pack(side="left", padx=2)
        custom = ttk.Frame(f)
        custom.pack(anchor="w", pady=2)
        ttk.Label(custom, text="Custom:").pack(side="left")
        self.minutes_var = tk.StringVar(value=str(self.settings.get("interval")))
        ttk.Entry(custom, textvariable=self.minutes_var, width=8).pack(side="left", padx=4)
        ttk.Label(custom, text="minutes").pack(side="left")
        ttk.Button(custom, text="Set", command=self._set_custom_interval).pack(side="left", padx=6)

        self.plugged_var = tk.BooleanVar(value=self.settings.get("plugged_only"))
        ttk.Checkbutton(f, text="Auto-sync only while the laptop is plugged in", variable=self.plugged_var,
                        command=lambda: self.settings.set("plugged_only", self.plugged_var.get())).pack(anchor="w", pady=(6, 0))
        self.next_lbl = self._p(f, "", foreground="#666")

    def _build_folders(self, f):
        self._h(f, "Folders to back up")
        self._p(f, "Each folder becomes a page inside your Notion backup page, with sub-pages that match its sub-folders.")
        self.folders_list = tk.Listbox(f, height=8, activestyle="none")
        self.folders_list.pack(fill="x", pady=4)
        row = ttk.Frame(f)
        row.pack(anchor="w")
        ttk.Button(row, text="Add folder or drive…", command=self.add_folder).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Remove selected", command=self.remove_folder).pack(side="left", padx=6)
        ttk.Button(row, text="Reset to my standard folders", command=self.reset_folders).pack(side="left", padx=6)

        self._h(f, "Folders to skip")
        self.excl_list = tk.Listbox(f, height=4, activestyle="none")
        self.excl_list.pack(fill="x", pady=4)
        row2 = ttk.Frame(f)
        row2.pack(anchor="w")
        ttk.Button(row2, text="Add folder to skip…", command=self.add_excluded).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Remove selected", command=self.remove_excluded).pack(side="left", padx=6)

        self._h(f, "Options")
        self.hidden_var = tk.BooleanVar(value=self.settings.get("skip_hidden"))
        ttk.Checkbutton(f, text="Skip hidden and system files", variable=self.hidden_var,
                        command=lambda: self.settings.set("skip_hidden", self.hidden_var.get())).pack(anchor="w")
        self.cloud_var = tk.BooleanVar(value=self.settings.get("skip_cloud_only"))
        ttk.Checkbutton(f, text="Skip OneDrive files that are online-only (backing them up would download them all)",
                        variable=self.cloud_var,
                        command=lambda: self.settings.set("skip_cloud_only", self.cloud_var.get())).pack(anchor="w")
        self._refresh_folder_lists()

    def _build_notion(self, f):
        self._h(f, "1. Integration secret")
        self._p(f, "From notion.so/profile/integrations (Developer tools → Connections). "
                   "You can use the same one as the phone app.")
        self.token_var = tk.StringVar(value=self.settings.get("token"))
        ttk.Entry(f, textvariable=self.token_var, show="•", width=80).pack(anchor="w", pady=4)

        self._h(f, "2. Link to your Laptop Backup page")
        self._p(f, "In Notion, open the page → ••• → Connections → add your integration, then Share → Copy link.")
        self.link_var = tk.StringVar(value=self.settings.get("root_page"))
        ttk.Entry(f, textvariable=self.link_var, width=80).pack(anchor="w", pady=4)

        ttk.Button(f, text="Save & test connection", command=self.save_notion).pack(anchor="w", pady=8)
        title = self.settings.get("root_title")
        self.notion_lbl = self._p(f, f"✓ Connected to “{title}”" if title else "Not connected yet.")

    def _build_activity(self, f):
        self.log_box = scrolledtext.ScrolledText(f, font=("Consolas", 9), wrap="none", height=30)
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

    def _build_about(self, f):
        self._h(f, "App updates")
        self.update_lbl = self._p(f, f"Version: build {BUILD}")
        ttk.Button(f, text="Check for update & install", command=lambda: self.check_update(install=True)).pack(anchor="w", pady=4)
        self._p(f, "Updating keeps your backup progress. The app restarts and picks up where it left off.",
                foreground="#666")

        self._h(f, "Startup")
        self.startup_var = tk.BooleanVar(value=self.settings.get("startup"))
        ttk.Checkbutton(f, text="Start with Windows (runs quietly in the system tray)", variable=self.startup_var,
                        command=self.toggle_startup).pack(anchor="w")
        self._p(f, "Closing the window keeps the app running in the tray (near the clock). "
                   "To exit fully, right-click the tray icon → Quit.", foreground="#666")

        self._h(f, "Your data")
        self._p(f, f"Settings, progress and the activity log are kept in {core.DATA_DIR}. "
                   "They survive updates and reinstalls.")
        ttk.Button(f, text="Open that folder", command=lambda: os.startfile(core.DATA_DIR)).pack(anchor="w", pady=4)

    # ---- folder lists ----------------------------------------------------------

    @staticmethod
    def _labels_for(folders, existing):
        """Gives each folder a page title, e.g. 'Documents' or 'D drive'; duplicates get (2), (3)…"""
        labels = dict(existing)
        used = set(labels.values())
        for p in folders:
            k = core.norm(p)
            if k in labels:
                continue
            base = os.path.basename(p.rstrip("\\/"))
            if not base:
                base = p.rstrip("\\/:") + " drive"
            name, n = base, 2
            while name in used:
                name = f"{base} ({n})"
                n += 1
            labels[k] = name
            used.add(name)
        return labels

    def _refresh_folder_lists(self):
        labels = self.settings.get("labels")
        self.folders_list.delete(0, "end")
        for p in self.settings.get("folders") or []:
            self.folders_list.insert("end", f"{p}    →  Notion page “{labels.get(core.norm(p), '')}”")
        self.excl_list.delete(0, "end")
        for p in self.settings.get("excluded"):
            self.excl_list.insert("end", p)

    def add_folder(self):
        path = filedialog.askdirectory(title="Choose a folder or drive to back up")
        if not path:
            return
        path = os.path.normpath(path)
        folders = self.settings.get("folders") or []
        n = core.norm(path)
        for p in folders:
            q = core.norm(p)
            if n == q or n.startswith(q + "\\") or q.startswith(n + "\\"):
                messagebox.showinfo(TITLE, f"That overlaps with a folder you already back up:\n{p}\n\n"
                                           "Remove that one first, so files aren't uploaded twice.")
                return
        folders.append(path)
        self.settings.set("folders", folders)
        self.settings.set("labels", self._labels_for(folders, self.settings.get("labels")))
        self._refresh_folder_lists()

    def remove_folder(self):
        sel = self.folders_list.curselection()
        if not sel:
            return
        folders = self.settings.get("folders") or []
        del folders[sel[0]]
        self.settings.set("folders", folders)
        self._refresh_folder_lists()

    def reset_folders(self):
        folders = winutil.default_folders()
        self.settings.set("folders", folders)
        self.settings.set("labels", self._labels_for(folders, self.settings.get("labels")))
        self._refresh_folder_lists()

    def add_excluded(self):
        path = filedialog.askdirectory(title="Choose a folder to skip")
        if not path:
            return
        ex = self.settings.get("excluded")
        ex.append(os.path.normpath(path))
        self.settings.set("excluded", ex)
        self._refresh_folder_lists()

    def remove_excluded(self):
        sel = self.excl_list.curselection()
        if not sel:
            return
        ex = self.settings.get("excluded")
        del ex[sel[0]]
        self.settings.set("excluded", ex)
        self._refresh_folder_lists()

    # ---- notion ----------------------------------------------------------------

    def save_notion(self):
        token = self.token_var.get().strip()
        pid = parse_page_id(self.link_var.get())
        if not token:
            self.notion_lbl.config(text="Paste the integration secret first.")
            return
        if not pid:
            self.notion_lbl.config(text="That doesn't look like a Notion page link.")
            return
        self.notion_lbl.config(text="Checking…")

        def work():
            try:
                title = NotionClient(token).page_title(pid)
                if self.settings.get("root_page") and self.settings.get("root_page") != pid:
                    self.db.reset_all()
                self.settings.set("token", token)
                self.settings.set("root_page", pid)
                self.settings.set("root_title", title)
                msg = f"✓ Connected to “{title}”"
            except NotionError as e:
                msg = {401: "Notion didn't accept that secret. Copy it again."}.get(
                    e.status, "Can't open that page. In Notion, open it → ••• → Connections → add your integration."
                    if e.status in (400, 404) else f"Notion error: {e}")
            except Exception as e:
                msg = f"Couldn't reach Notion: {e}"
            self.events.put(lambda: self.notion_lbl.config(text=msg))

        threading.Thread(target=work, daemon=True).start()

    def ready(self):
        return bool(self.settings.get("token") and self.settings.get("root_page") and self.settings.get("folders"))

    # ---- syncing -----------------------------------------------------------------

    def running(self):
        return self.sync_thread is not None and self.sync_thread.is_alive()

    def sync_now(self):
        if not self.ready():
            messagebox.showinfo(TITLE, "Connect Notion (Notion tab) and choose folders (Folders tab) first.")
            return
        self.start_sync()

    def start_sync(self):
        if self.running():
            return
        self.stop_event = threading.Event()
        self.syncer = Syncer(self.settings, self.db, self.log, self._set_status)
        self.sync_thread = threading.Thread(target=self._run_sync, daemon=True)
        self.sync_thread.start()

    def _run_sync(self):
        try:
            self.syncer.run(self.stop_event)
        except Exception as e:
            self.log(f"Sync stopped: {e.__class__.__name__}: {e}")
        finally:
            self.settings.set("last_run", time.time())
            self._set_status("", 0, 0)

    def _set_status(self, text, done, total):
        self.status = {"text": text, "done": done, "total": total}

    def stop_sync(self):
        if self.running():
            self.stop_event.set()
            self.log("Stopping…")

    def toggle_auto(self):
        on = self.auto_var.get()
        if on and not self.ready():
            messagebox.showinfo(TITLE, "Connect Notion (Notion tab) and choose folders (Folders tab) first.")
            self.auto_var.set(False)
            return
        self.settings.set("auto_sync", on)
        if on:
            self.start_sync()
        else:
            self.stop_sync()

    def set_interval(self, minutes):
        if minutes < 5:
            messagebox.showinfo(TITLE, "The shortest interval is 5 minutes.")
            minutes = 5
        minutes = min(minutes, 10080)
        self.settings.set("interval", minutes)
        self.minutes_var.set(str(minutes))
        self._update_auto_label()

    def _set_custom_interval(self):
        try:
            self.set_interval(int(self.minutes_var.get().strip()))
        except ValueError:
            messagebox.showinfo(TITLE, "Type a number of minutes, e.g. 45.")

    def _update_auto_label(self):
        self.auto_cb.config(text=f"Auto-sync every {core.interval_label(self.settings.get('interval'))}")

    def _scheduler(self):
        while not self.quitting:
            time.sleep(20)
            try:
                s = self.settings
                if not s.get("auto_sync") or self.running() or not self.ready():
                    continue
                due = max(self.started + 60, s.get("last_run") + s.get("interval") * 60)
                if time.time() < due:
                    continue
                if s.get("plugged_only") and not winutil.on_mains_power():
                    continue
                self.events.put(self.start_sync)
            except Exception:
                pass

    def show_problems(self):
        rows = self.db.problems()
        win = tk.Toplevel(self.root)
        win.title("Skipped & failed files")
        win.geometry("720x420")
        box = scrolledtext.ScrolledText(win, font=("Consolas", 9), wrap="word")
        box.pack(fill="both", expand=True)
        box.insert("end", "\n\n".join(f"{p}\n→ {st}: {err or ''}" for p, st, err in rows)
                   or "Nothing skipped or failed.")
        box.configure(state="disabled")

    def retry_problems(self):
        self.db.reset_problems()
        messagebox.showinfo(TITLE, "They'll be tried again on the next sync.")

    # ---- the once-a-second refresh ---------------------------------------------------

    def _tick(self):
        while True:
            try:
                self.events.get_nowait()()
            except queue.Empty:
                break
            except Exception:
                pass
        up, down, t0 = self.last_traffic
        now = time.monotonic()
        cu, cd = TRAFFIC.up, TRAFFIC.down
        dt = max(now - t0, 0.001)
        su, sd = self.speed
        su = su * 0.4 + (cu - up) / dt * 0.6
        sd = sd * 0.4 + (cd - down) / dt * 0.6
        self.speed = (su, sd)
        self.last_traffic = (cu, cd, now)
        self.speed_lbl.config(text=f"↑ {core.human_rate(su)}     ↓ {core.human_rate(sd)}")
        self.speed_note.config(text=f"Since the app started: sent {core.human_size(cu - self.session_start[0])}, "
                                    f"received {core.human_size(cd - self.session_start[1])}. This app's traffic only.")

        if int(now) % 2 == 0:
            self._refresh_status()
        if self.tray is not None:
            state = "Syncing" if self.running() else "Idle"
            try:
                self.tray.title = f"{TITLE}: {state}  ↑ {core.human_rate(su)}"[:127]
            except Exception:
                pass
        self.root.after(1000, self._tick)

    def _refresh_status(self):
        st = self.status
        if self.running():
            self.state_lbl.config(text="● Syncing now" + (" (stopping…)" if self.stop_event.is_set() else ""))
            self.current_lbl.config(text=st["text"])
            if st["total"]:
                self.bar.config(mode="determinate", maximum=st["total"], value=st["done"])
            else:
                self.bar.config(mode="determinate", maximum=1, value=0)
        else:
            self.state_lbl.config(text="○ Idle")
            self.current_lbl.config(text="")
            self.bar.config(value=0)
        c = self.db.counts()
        waiting = getattr(self.syncer, "pending", 0) if self.running() else 0
        last = self.settings.get("last_run")
        last_txt = time.strftime("%d %b %Y, %H:%M", time.localtime(last)) if last else "never"
        self.counts_lbl.config(
            text=f"Uploaded: {c.get('done', 0):,}    Waiting: {waiting:,}    Skipped: {c.get('skipped', 0):,}    "
                 f"Failed: {c.get('failed', 0):,}    Will retry: {c.get('retry', 0):,}\nLast run: {last_txt}")
        if self.settings.get("auto_sync"):
            due = max(self.started + 60, last + self.settings.get("interval") * 60)
            when = "soon" if due <= time.time() else time.strftime("%H:%M", time.localtime(due))
            extra = " (waiting for the charger)" if self.settings.get("plugged_only") and not winutil.on_mains_power() else ""
            self.next_lbl.config(text="" if self.running() else f"Next auto-sync: {when}{extra}")
        else:
            self.next_lbl.config(text="Auto-sync is off.")
        if self.nb.index("current") == TAB_ACTIVITY:
            text = self.log.tail(300)
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.insert("end", text)
            self.log_box.configure(state="disabled")

    # ---- tray, window, single instance --------------------------------------------------

    def _start_tray(self):
        try:
            import pystray
            from PIL import Image
            image = Image.open(winutil.resource("icon.ico"))
            menu = pystray.Menu(
                pystray.MenuItem("Open", lambda: self.events.put(self.show_window), default=True),
                pystray.MenuItem("Sync now", lambda: self.events.put(self.sync_now)),
                pystray.MenuItem("Stop syncing", lambda: self.events.put(self.stop_sync)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda: self.events.put(self.quit)),
            )
            self.tray = pystray.Icon("NotionLaptopSync", image, TITLE, menu)
            self.tray.run_detached()
        except Exception as e:
            self.tray = None
            self.log(f"Tray icon unavailable ({e}). Closing the window will exit the app.")

    def show_window(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def hide_to_tray(self):
        if self.tray is None:
            self.quit()
            return
        if not self.settings.get("closed_to_tray_hint"):
            self.settings.set("closed_to_tray_hint", True)
            messagebox.showinfo(TITLE, "Notion Laptop Sync keeps running in the system tray (near the clock) "
                                       "so auto-sync keeps working.\n\nTo exit fully, right-click the tray icon → Quit.")
        self.root.withdraw()

    def _listen_for_second_launch(self):
        while not self.quitting:
            try:
                conn, _ = self.listener.accept()
                with conn:
                    if conn.recv(16) == b"show":
                        self.events.put(self.show_window)
            except OSError:
                return

    def quit(self):
        self.quitting = True
        self.stop_event.set()
        if self.sync_thread is not None:
            self.sync_thread.join(timeout=10)
        try:
            self.listener.close()
        except OSError:
            pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def toggle_startup(self):
        on = self.startup_var.get()
        self.settings.set("startup", on)
        try:
            winutil.set_startup(on)
        except OSError as e:
            messagebox.showerror(TITLE, f"Couldn't change the startup setting: {e}")

    # ---- updates -------------------------------------------------------------------------

    def check_update(self, install):
        if self.updating:
            return
        self.updating = True
        self.update_lbl.config(text="Checking for updates…")

        def show(t):
            self.events.put(lambda: self.update_lbl.config(text=t))

        def work():
            try:
                r = requests.get(f"https://api.github.com/repos/{REPO}/releases?per_page=30",
                                 headers={"Accept": "application/vnd.github+json"}, timeout=20)
                r.raise_for_status()
                latest = None
                for rel in r.json():
                    tag = rel.get("tag_name", "")
                    if tag.startswith("win-build-") and tag[10:].isdigit():
                        asset = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")), None)
                        if asset and (latest is None or int(tag[10:]) > latest[0]):
                            latest = (int(tag[10:]), asset["browser_download_url"], asset.get("size", 0))
                if latest is None or latest[0] <= BUILD:
                    show(f"Version: build {BUILD}. You have the latest version.")
                    return
                if not install:
                    show(f"Version: build {BUILD}. Update available: build {latest[0]}. "
                         "Click the button below to install it.")
                    return
                if not winutil.frozen():
                    show("Updates only work in the installed app.")
                    return
                path = os.path.join(tempfile.gettempdir(), "NotionLaptopSync-Setup.exe")
                with requests.get(latest[1], stream=True, timeout=(20, 120)) as d:
                    d.raise_for_status()
                    total = int(d.headers.get("Content-Length") or latest[2] or 0)
                    done = 0
                    with open(path, "wb") as out:
                        for chunk in d.iter_content(256 * 1024):
                            out.write(chunk)
                            done += len(chunk)
                            TRAFFIC.add_down(len(chunk))
                            if total:
                                show(f"Downloading build {latest[0]}… {done * 100 // total}%")
                show(f"Installing build {latest[0]}… The app will reopen by itself.")
                self.log(f"Updating to build {latest[0]}. Backup progress is kept.")
                subprocess.Popen([path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                                 creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP
                self.events.put(self.quit)
            except Exception as e:
                show(f"Couldn't check for updates: {e}")
            finally:
                self.updating = False

        threading.Thread(target=work, daemon=True).start()

    def run(self):
        self.root.mainloop()


def selftest():
    """Used by the build: opens the window, clicks around, and fails on any error."""
    errors = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    app = App(False, listener)
    app.root.report_callback_exception = lambda *a: errors.append(a)
    steps = [
        lambda: [app.nb.select(i) for i in range(6)],
        lambda: app.nb.select(TAB_FILES),
        app.files.show,
        lambda: app.nb.select(TAB_ACTIVITY),
        app._refresh_status,
        lambda: app.set_interval(30),
        lambda: app.minutes_var.set("45"),
        app._set_custom_interval,
        app._refresh_folder_lists,
        lambda: app.nb.select(0),
    ]
    for i, step in enumerate(steps):
        app.root.after(1500 + i * 300, step)
    app.root.after(6000, app.quit)
    app.run()
    if errors:
        import traceback
        for e in errors:
            traceback.print_exception(*e)
        sys.exit(1)
    assert app.settings.get("interval") == 45, app.settings.get("interval")
    print("SELFTEST OK")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    minimized = "--minimized" in sys.argv
    # Right after an update the old copy may still be closing: wait for it.
    tries = 30 if "--after-update" in sys.argv else 1
    listener = None
    for i in range(tries):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", INSTANCE_PORT))
            s.listen(2)
            listener = s
            break
        except OSError:
            s.close()
            if i + 1 < tries:
                time.sleep(1)
    if listener is None:
        # Already running: bring that window to the front instead of opening a second copy.
        if not minimized:
            try:
                with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=3) as c:
                    c.sendall(b"show")
            except OSError:
                pass
        return
    App(minimized, listener).run()


if __name__ == "__main__":
    main()
