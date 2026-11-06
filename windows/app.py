"""Notion Laptop Sync: backs up folders on this laptop to a Notion page."""
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import webbrowser
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import requests

import account
import clip_sync
import device_setup
import pagefix
import voice
import devices
import core
import winutil
from files_tab import FilesTab
from space_tab import SpaceTab
from notion import TRAFFIC, NotionClient, NotionError, parse_page_id
from sync import Syncer
from version import BUILD

REPO = "sakib1322/NotionPhoneSync"
INSTANCE_PORT = 47391
TITLE = "Notion Laptop Sync"
TAB_FILES, TAB_SPACE, TAB_NOTION, TAB_ACTIVITY, TAB_DEVICES = 1, 2, 4, 5, 7


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
        self.clip = clip_sync.ClipSync(self)
        self.clip.start()
        self.signin = None
        if not self.settings.get("signed_in"):
            self.signin = device_setup.SignInScreen(self, self._signed_in)
            minimized = False
        threading.Thread(target=self._devices_loop, daemon=True).start()
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
        self.space = SpaceTab(self, self._tab(nb, "Space"))
        self._build_folders(self._tab(nb, "Folders"))
        self._build_notion(self._tab(nb, "Notion"))
        self._build_activity(self._tab(nb, "Activity"))
        self._build_about(self._tab(nb, "Updates & settings"))
        self._build_devices(self._tab(nb, "Devices"))
        self._build_voice(self._tab(nb, "Voice"))
        nb.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        if not self.settings.get("token"):
            nb.select(TAB_NOTION)

    def _tab_changed(self):
        try:
            if self.nb.index("current") == TAB_DEVICES:
                self._fill_devices()
            if self.nb.index("current") == TAB_FILES:
                self.files.on_shown()
            elif self.nb.index("current") == TAB_SPACE:
                self.space.on_shown()
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
        self._h(f, "Account")
        self.acct_lbl = self._p(f, "", foreground="#555")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=4)
        self.google_btn = ttk.Button(row, text="Sign out", command=self.sign_out_click)
        self.google_btn.pack(side="left", padx=(0, 6))
        ttk.Button(row, text="Move to a new device…", command=self.show_transfer).pack(side="left", padx=6)

        self._h(f, "This laptop and your devices")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=2)
        self.dev_lbl = ttk.Label(row, text="", font=("Segoe UI", 10, "bold"))
        self.dev_lbl.pack(side="left")
        ttk.Button(row, text="Rename…", command=self.rename_device).pack(side="left", padx=10)
        self.devs_lbl = self._p(f, "", foreground="#555")
        self._account_ui()

        self._h(f, "Shared clipboard")
        self.clip_var = tk.BooleanVar(value=self.settings.get("clipboard_sync"))
        ttk.Checkbutton(f, text="Share my clipboard with my other devices",
                        variable=self.clip_var,
                        command=lambda: self.settings.set("clipboard_sync", self.clip_var.get())).pack(anchor="w")
        self._p(f, "Text you copy here is on your other devices' clipboards a few seconds later. Files and pictures "
                   "aren't sent automatically (to save space): the other devices show what you copied and download it "
                   "only when you tap Get there. Things sent from a phone land here, ready for Ctrl+V (files are also "
                   "saved in Downloads\\From phone). Passwords copied from password managers are never sent.", foreground="#666")
        self.clip_status = self._p(f, "Starting…")
        orow = ttk.Frame(f)
        orow.pack(anchor="w", pady=2)
        self.offer_lbl = ttk.Label(orow, text="", wraplength=600)
        self.offer_lbl.pack(side="left")
        self.offer_btn = ttk.Button(orow, text="Get", command=self._get_offer)
        self.offer_shown = None

        self._h(f, "1. Integration secret")
        self._p(f, "From notion.so/profile/integrations (Developer tools → Connections). "
                   "Use the same one as the phone app (the shared clipboard needs it).")
        self.token_var = tk.StringVar(value=self.settings.get("token"))
        ttk.Entry(f, textvariable=self.token_var, show="•", width=80).pack(anchor="w", pady=4)

        self._h(f, "2. Link to your Laptop Backup page")
        self._p(f, "In Notion, open the page → ••• → Connections → add your integration, then Share → Copy link.")
        self.link_var = tk.StringVar(value=self.settings.get("root_page"))
        ttk.Entry(f, textvariable=self.link_var, width=80).pack(anchor="w", pady=4)

        ttk.Button(f, text="Save & test connection", command=self.save_notion).pack(anchor="w", pady=8)
        title = self.settings.get("root_title")
        self.notion_lbl = self._p(f, f"✓ Connected to “{title}”" if title else "Not connected yet.")

    # ---- voice changer --------------------------------------------------------------

    def _build_voice(self, f):
        self.voice = voice.VoiceChanger(self.settings, self.log)
        self._h(f, "Voice changer for calls")
        self._p(f, "Change your voice live in WhatsApp Desktop, Google Meet, Zoom, Teams or any call app on this "
                   "laptop. Your microphone goes through the effect into a virtual microphone; in the call app you "
                   "choose that as your microphone.", foreground="#555")

        self.v_cable = ttk.Label(f, text="", wraplength=700, justify="left")
        self.v_cable.pack(anchor="w", pady=(6, 2))
        self.v_cable_btn = ttk.Button(f, text="Get VB-CABLE (free) — opens vb-audio.com",
                                      command=lambda: webbrowser.open("https://vb-audio.com/Cable/"))
        self.v_cable_btn.pack(anchor="w")

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(12, 4))
        ttk.Label(row, text="Voice").pack(side="left")
        self.v_preset = ttk.Combobox(row, state="readonly", width=34, values=list(voice.PRESETS))
        cur = voice.OLD_NAMES.get(self.settings.get("voice_preset"), self.settings.get("voice_preset"))
        self.v_preset.set(cur if cur in voice.PRESETS else "Deeper man")
        self.v_preset.pack(side="left", padx=8)
        self.v_preset.bind("<<ComboboxSelected>>", lambda e: self.settings.set("voice_preset", self.v_preset.get()))

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=4)
        ttk.Label(row, text="Fine-tune pitch").pack(side="left")
        self.v_pitch_lbl = ttk.Label(row, text="", width=14)
        pv = tk.DoubleVar(value=float(self.settings.get("voice_pitch") or 0))

        def on_pitch(_=None):
            v = round(pv.get() * 2) / 2
            self.settings.set("voice_pitch", v)
            self.v_pitch_lbl.config(text=f"{v:+.1f} semitones" if v else "no extra")
        ttk.Scale(row, from_=-12, to=12, variable=pv, length=260, command=on_pitch).pack(side="left", padx=8)
        self.v_pitch_lbl.pack(side="left")
        on_pitch()

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=4)
        ttk.Label(row, text="Voice body").pack(side="left")
        self.v_body_lbl = ttk.Label(row, text="", width=30)
        bv = tk.DoubleVar(value=float(self.settings.get("voice_body") or 0))

        def on_body(_=None):
            v = round(bv.get())
            self.settings.set("voice_body", float(v))
            self.v_body_lbl.config(text="as the voice sets it" if not v else
                                   (f"{v:+d}% smaller (lighter, younger)" if v > 0 else f"{v:+d}% bigger (fuller, older)"))
        ttk.Scale(row, from_=-20, to=20, variable=bv, length=260, command=on_body).pack(side="left", padx=8)
        self.v_body_lbl.pack(side="left")
        on_body()
        self._p(f, "Pitch is how high or low your voice is; voice body is how big the person sounds. "
                   "For a natural result, move them a little, together.", foreground="#777")

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=4)
        ttk.Label(row, text="Microphone").pack(side="left")
        self.v_mic = ttk.Combobox(row, state="readonly", width=46)
        self.v_mic.pack(side="left", padx=8)
        self.v_mic.bind("<<ComboboxSelected>>", lambda e: self.settings.set(
            "voice_mic", "" if self.v_mic.current() == 0 else self.v_mic.get()))
        ttk.Button(row, text="Refresh", command=lambda: (self.voice.rescan(), self._voice_devices())).pack(side="left")

        mon = tk.BooleanVar(value=bool(self.settings.get("voice_monitor")))
        ttk.Checkbutton(f, text="Hear myself (use headphones, or you'll get feedback)", variable=mon,
                        command=lambda: self.settings.set("voice_monitor", mon.get())).pack(anchor="w", pady=4)

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(10, 4))
        self.v_btn = ttk.Button(row, text="Start voice changer", command=self.voice_toggle)
        self.v_btn.pack(side="left")
        ttk.Label(row, text="   Mic level").pack(side="left")
        self.v_meter = ttk.Progressbar(row, length=200, maximum=1.0)
        self.v_meter.pack(side="left", padx=6)
        self.v_status = self._p(f, "")
        self._p(f, "In your call app: Settings → Audio → Microphone → “CABLE Output (VB-Audio Virtual Cable)”. "
                   "Keep your speaker as normal. To talk normally again, choose “Normal (no change)” or switch the "
                   "call app back to your real microphone.", foreground="#555")
        self._voice_devices()

    def _voice_devices(self):
        try:
            mics = [n for _, n in self.voice.mics()]
        except Exception:
            mics = []
        self.v_mic.config(values=["Windows default microphone"] + mics)
        cur = self.settings.get("voice_mic")
        self.v_mic.set(cur if cur in mics else "Windows default microphone")
        has = self.voice.cable() is not None
        if not has:
            self.voice.rescan()
            has = self.voice.cable() is not None
        self.v_cable.config(text="✓ Virtual microphone (VB-CABLE) found." if has else
                            "Step 1 (once): install VB-CABLE, the free virtual microphone. Download it, unzip, right-click "
                            "VBCABLE_Setup_x64.exe → Run as administrator → Install, then restart the laptop.")
        if has:
            self.v_cable_btn.pack_forget()
        elif not self.v_cable_btn.winfo_ismapped():
            self.v_cable_btn.pack(anchor="w", after=self.v_cable)

    def voice_toggle(self):
        if self.voice.running:
            self.voice.stop()
        else:
            try:
                self.voice.start()
            except Exception as e:
                messagebox.showerror(TITLE, f"Couldn't start the voice changer:\n{e}")
                self._voice_devices()
        self._voice_ui()

    def _voice_ui(self):
        try:
            on = self.voice.running
            self.v_btn.config(text="Stop voice changer" if on else "Start voice changer")
            self.v_meter["value"] = min(1.0, self.voice.level) if on else 0
            self.v_status.config(text=(f"● On — mic: {self.voice.used_mic} — sounding: {self.settings.get('voice_preset')}"
                                       + (f" {float(self.settings.get('voice_pitch') or 0):+.1f}" if self.settings.get('voice_pitch') else ""))
                                 if on else "Off.")
        except Exception:
            pass

    def _build_devices(self, f):
        self._h(f, "Your devices")
        self._p(f, "Every phone and laptop signed in to your account, with what each one last reported. "
                   "A device counts as online if it checked in within the last few minutes.", foreground="#555")
        cols = ("status", "type", "backup", "cloud", "last", "clip", "version")
        tree = ttk.Treeview(f, columns=cols, show="tree headings", height=8)
        tree.heading("#0", text="Device")
        tree.column("#0", width=170)
        for c, t, w in (("status", "Status", 150), ("type", "Type", 60), ("backup", "Backup", 170),
                        ("cloud", "In the cloud", 130), ("last", "Last backup", 120), ("clip", "Clipboard", 120),
                        ("version", "Version", 60)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="w")
        tree.pack(fill="both", expand=True, pady=6)
        tree.bind("<<TreeviewSelect>>", lambda e: self._device_detail())
        self.dev_tree = tree
        row = ttk.Frame(f)
        row.pack(anchor="w")
        ttk.Button(row, text="Refresh", command=self.refresh_devices).pack(side="left")
        self.dev_note = ttk.Label(row, text="", foreground="#555")
        self.dev_note.pack(side="left", padx=10)
        self.dev_detail = self._p(f, "")
        self._fill_devices()

    @staticmethod
    def _describe_device(d, me):
        info = d.info()
        online = d.online()
        if info.get("syncing"):
            state = "● Online · backing up"
        elif online:
            state = "● Online"
        else:
            state = f"○ Offline · seen {d.seen_ago()}"
        pend = info.get("pending") or 0
        failed = info.get("failed") or 0
        if info.get("syncing"):
            backup = f"{pend:,} left" if pend else (info.get("now") or "working…")[:40]
        elif "pending" in info:
            backup = "Up to date" if not pend else f"{pend:,} waiting"
            if failed:
                backup += f", {failed:,} problems"
        else:
            backup = "—"
        n, b = info.get("cloud_files"), info.get("cloud_bytes")
        cloud = f"{n:,} files · {b / 1073741824:.1f} GB" if isinstance(n, int) and isinstance(b, (int, float)) else "—"
        last = info.get("last_sync")
        last_txt = time.strftime("%d %b, %I:%M %p", time.localtime(last / 1000 if last > 1e11 else last)) if last else "—"
        wifi = info.get("wifi") or []
        clip = "Off" if info.get("clipboard") is False else (f"Wi-Fi: {', '.join(wifi)}" if wifi else ("On" if info else "—"))
        name = d.name + ("  (this laptop)" if d.id == me else "")
        return name, (state, d.type, backup, cloud, last_txt, clip, str(info.get("version") or ""))

    def _fill_devices(self):
        try:
            me = devices.my_id(self.settings)
            devs = devices.cached(self.settings)
            # This laptop: show live numbers rather than the last report.
            devs = [d for d in devs if d.id != me]
            mine = devices.Device("", me, devices.my_name(self.settings), "laptop", self.settings.get("root_page"),
                                  self.settings.get("root_title"), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            import json as _json
            mine.status = _json.dumps(self.device_status())
            devs = [mine] + sorted(devs, key=lambda d: (not d.online(), (d.name or "").lower()))
            self.dev_rows = {}
            self.dev_tree.delete(*self.dev_tree.get_children())
            for d in devs:
                name, vals = self._describe_device(d, me)
                iid = self.dev_tree.insert("", "end", text=name, values=vals)
                self.dev_rows[iid] = d
            seen = self.settings.get("devices_seen_at")
            self.dev_note.config(text=time.strftime("List updated %I:%M %p", time.localtime(seen)) if seen else "")
        except Exception as e:
            self.log(f"Devices tab error: {e}")

    def _device_detail(self):
        sel = self.dev_tree.selection()
        d = self.dev_rows.get(sel[0]) if sel else None
        if not d:
            return
        info = d.info()
        lines = [f"{d.name} — {d.type}, backs up to “{d.page_title or 'its page'}”",
                 f"Last seen: {d.seen_ago()}" + (f" · version {info['version']}" if info.get("version") else "")]
        if info.get("now"):
            lines.append(f"Doing: {info['now']}")
        if "pending" in info:
            lines.append(f"Uploaded: {info.get('uploaded', 0):,} · waiting: {info.get('pending', 0):,} · problems: {info.get('failed', 0):,}"
                         f" · auto-sync: {'on' if info.get('auto') else 'off'}")
        self.dev_detail.config(text="\n".join(lines))

    def device_status(self):
        """What this laptop reports to your other devices."""
        c = self.db.counts()
        try:
            with self.db.lock:
                n, b = self.db.conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE status='done'").fetchone()
        except Exception:
            n, b = None, None
        return {"platform": "windows", "version": BUILD, "syncing": self.running(),
                "now": self.status.get("text", "") if self.running() else "",
                "pending": getattr(self.syncer, "pending", 0) if self.running() else 0,
                "uploaded": c.get("done", 0), "failed": c.get("failed", 0) + c.get("retry", 0),
                "cloud_files": n if isinstance(n, int) else None, "cloud_bytes": b if isinstance(b, (int, float)) else None,
                "last_sync": int((self.settings.get("last_run") or 0) * 1000), "auto": bool(self.settings.get("auto_sync")),
                "clipboard": bool(self.settings.get("clipboard_sync")), "wifi": self.clip.lan.connected_names()}

    def refresh_devices(self):
        self.dev_note.config(text="Refreshing…")

        def work():
            reg = devices.Registry(self.settings, self.db)
            reg.register_me(self.device_status())
            reg.all()
        account.run_async(work, lambda r, e: self.events.put(lambda: (
            self._fill_devices(), self._account_ui(),
            e and self.dev_note.config(text=f"Couldn't refresh: {e}"))))

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
                if self.settings.get("google_refresh"):
                    try:
                        account.upload(self.settings)
                    except Exception:
                        pass
                msg = f"✓ Connected to “{title}”"
            except NotionError as e:
                msg = {401: "Notion didn't accept that secret. Copy it again."}.get(
                    e.status, "Can't open that page. In Notion, open it → ••• → Connections → add your integration."
                    if e.status in (400, 404) else f"Notion error: {e}")
            except Exception as e:
                msg = f"Couldn't reach Notion: {e}"
            self.events.put(lambda: self.notion_lbl.config(text=msg))

        threading.Thread(target=work, daemon=True).start()

    # ---- account -----------------------------------------------------------------

    def _account_ui(self):
        email = self.settings.get("google_email")
        how = self.settings.get("signed_in")
        if email:
            saved = self.settings.get("google_saved_at")
            text = f"Signed in with Google as {email}" + (time.strftime(" · setup saved %d %b, %I:%M %p", time.localtime(saved)) if saved else "")
        elif how:
            text = "Signed in with a setup code."
        else:
            text = "Not signed in."
        self.acct_lbl.config(text=text)
        try:
            self.dev_lbl.config(text=f"This laptop: {devices.my_name(self.settings)}"
                                     f"  ·  backs up to “{self.settings.get('root_title') or 'its page'}”")
            me = devices.my_id(self.settings)
            others = [d for d in devices.cached(self.settings) if d.id != me]
            self.devs_lbl.config(text=("Your other devices: " + ", ".join(
                f"{d.name} ({d.type}, seen {d.seen_ago()})" for d in others)) if others else
                "Your other devices will show up here once they're signed in.")
        except Exception:
            pass

    def sign_out_click(self):
        if not messagebox.askyesno(TITLE, "Sign out? The app stops syncing until you sign in again. "
                                          "Your files on this laptop and in the cloud are unchanged."):
            return
        account.sign_out(self.settings)
        self.settings.set("signed_in", "")
        self.stop_sync()
        self.signin = device_setup.SignInScreen(self, self._signed_in)

    def _signed_in(self):
        if self.signin:
            self.signin.close()
            self.signin = None
        self._reload_settings_ui()
        try:
            winutil.set_startup(self.settings.get("startup"))
        except OSError:
            pass
        self.log(f"Signed in. This laptop is “{devices.my_name(self.settings)}”, backing up to "
                 f"“{self.settings.get('root_title')}”.")

    def rename_device(self):
        new = simpledialog.askstring(TITLE, "Name this laptop:", initialvalue=devices.my_name(self.settings), parent=self.root)
        if not new or not new.strip():
            return

        def work():
            devices.Registry(self.settings, self.db).rename_me(new, self.device_status())
        account.run_async(work, lambda r, e: self.events.put(lambda: (
            self._account_ui(), e and messagebox.showerror(TITLE, f"Renamed here, but couldn't update Notion: {e}"))))
        self.settings.set("device_name", new.strip()[:60])
        self._account_ui()

    def _devices_loop(self):
        """Every few minutes: tells your other devices this laptop is on and what it's doing,
        and reads theirs (for the Devices tab)."""
        last = 0
        while not self.stop_event.is_set():
            if self.settings.get("signed_in") and self.settings.get("token") and time.time() - last > 300:
                try:
                    reg = devices.Registry(self.settings, self.db)
                    reg.register_me(self.device_status())
                    reg.all()
                    last = time.time()
                    self.events.put(self._account_ui)
                    self.events.put(self._fill_devices)
                    owner = pagefix.conflict(self.settings)
                    if owner and not getattr(self, "conflict_open", False):
                        self.conflict_open = True
                        self.events.put(lambda o=owner: self._show_conflict(o))
                except Exception as e:
                    self.log(f"Couldn't update your devices list ({e}).")
                    last = time.time() - 240
            self.stop_event.wait(20)

    def _show_conflict(self, owner):
        """This laptop has been backing up into another device's page: give it its own."""
        owner = pagefix.conflict(self.settings)
        if not owner:
            self.conflict_open = False
            return
        self.stop_sync()
        self.show_window()
        win = tk.Toplevel(self.root)
        win.title("This laptop needs its own backup page")
        win.transient(self.root)
        win.protocol("WM_DELETE_WINDOW", lambda: None)
        fr = ttk.Frame(win, padding=18)
        fr.pack(fill="both", expand=True)
        name = devices.my_name(self.settings)
        ttk.Label(fr, text="This laptop needs its own backup page", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(fr, wraplength=520, justify="left", text=(
            f"“{self.settings.get('root_title')}” belongs to {owner.name}, but this laptop ({name}) was backing up "
            "into it too. Backup is paused here so the two don't mix.\n\n"
            f"The app will make “{name} Backup” for this laptop, back everything up there, and then remove the "
            f"copies this laptop put in {owner.name}'s page (only its own uploads, and only once they're safely "
            "in the new page).")).pack(anchor="w", pady=(6, 10))
        ttk.Label(fr, text="Put the new page inside").pack(anchor="w")
        where = ttk.Combobox(fr, state="readonly", width=48, values=["Loading…"])
        where.current(0)
        where.pack(anchor="w", pady=3)
        ttk.Label(fr, text="…or paste a page link").pack(anchor="w", pady=(6, 0))
        link = tk.StringVar()
        ttk.Entry(fr, textvariable=link, width=50).pack(anchor="w", pady=3)
        msg = ttk.Label(fr, text="", foreground="#b00020", wraplength=520)
        msg.pack(anchor="w")
        parents = []

        def loaded(r, e):
            def ui():
                if e:
                    where.config(values=["(couldn't load — paste a page link)"])
                    where.current(0)
                    return
                parents[:] = r or []
                where.config(values=[t for _, t in parents] or ["(none — paste a page link)"])
                where.current(0)
            self.events.put(ui)
        account.run_async(lambda: devices.Registry(self.settings, self.db).candidate_parents(), loaded)

        def go():
            pid = parse_page_id(link.get()) if link.get().strip() else (parents[where.current()][0] if parents else None)
            if not pid:
                msg.config(text="Choose where to put the new page, or paste a page link.")
                return
            msg.config(text="Making the page…", foreground="#555")
            title = f"{name} Backup"

            def work():
                reg = devices.Registry(self.settings, self.db)
                new = reg.create_page(pid, title)
                n = pagefix.move_to(self.settings, self.db, new, title, self.log)
                reg.register_me(self.device_status())
                reg.all()
                return n

            def done(n, e):
                def ui():
                    if e:
                        msg.config(text=f"Couldn't make the page: {e}. Make sure that page is connected to your "
                                        "integration in Notion (••• → Connections).", foreground="#b00020")
                        return
                    self.conflict_open = False
                    win.destroy()
                    self._reload_settings_ui()
                    self._fill_devices()
                    messagebox.showinfo(TITLE, f"This laptop now backs up to “{title}”. The backup starts now.")
                    self.sync_now()
                self.events.put(ui)
            account.run_async(work, done)
        ttk.Button(fr, text=f"Make “{name} Backup” and move there", command=go).pack(anchor="w", pady=10)

        def keep():
            if not messagebox.askyesno(TITLE, f"“{self.settings.get('root_title')}” belongs to this laptop ({name})?\n\n"
                                              f"Then {owner.name} will be asked to move to its own page instead.", parent=win):
                return

            def work():
                reg = devices.Registry(self.settings, self.db)
                reg.claim_page()
                reg.register_me(self.device_status())
                reg.all()

            def done(r, e):
                def ui():
                    if e:
                        msg.config(text=f"Couldn't save that: {e}", foreground="#b00020")
                        return
                    self.conflict_open = False
                    win.destroy()
                    self._fill_devices()
                    self.log(f"You said “{self.settings.get('root_title')}” belongs to this laptop; "
                             f"{owner.name} will move to its own page.")
                    self.sync_now()
                self.events.put(ui)
            account.run_async(work, done)
        ttk.Separator(fr).pack(fill="x", pady=6)
        ttk.Label(fr, text="Is that wrong?", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Button(fr, text=f"This page belongs to this laptop ({name}) — keep it", command=keep).pack(anchor="w", pady=4)

    def notify(self, title, msg):
        if self.tray is not None:
            try:
                self.tray.notify(msg, title)
            except Exception:
                pass

    def _get_offer(self):
        if self.offer_shown:
            self.clip.get_offer(self.offer_shown)

    def _reload_settings_ui(self):
        self.token_var.set(self.settings.get("token"))
        self.link_var.set(self.settings.get("root_page"))
        title = self.settings.get("root_title")
        self.notion_lbl.config(text=f"✓ Connected to “{title}”" if title else "Not connected yet.")
        self.auto_var.set(self.settings.get("auto_sync"))
        self.minutes_var.set(str(self.settings.get("interval")))
        self._update_auto_label()
        self.hidden_var.set(self.settings.get("skip_hidden"))
        self.cloud_var.set(self.settings.get("skip_cloud_only"))
        self._refresh_folder_lists()
        self._account_ui()

    def google_click(self):
        if self.settings.get("google_email"):
            if messagebox.askyesno(TITLE, "Sign out of Google? Your files and setup on this laptop are unchanged."):
                account.sign_out(self.settings)
                self._account_ui()
            return
        self.acct_lbl.config(text="Finish signing in in your browser…")

        def work():
            account.sign_in(self.settings)
            return account.download(self.settings)

        def done(cloud, err):
            def ui():
                if err:
                    # Don't stay half signed in.
                    account.sign_out(self.settings)
                    self._account_ui()
                    if isinstance(err, account.NoDriveAccess):
                        if messagebox.askretrycancel(TITLE, f"Almost there.\n\n{err}"):
                            self.google_click()
                    else:
                        messagebox.showerror(TITLE, f"Google sign-in didn't work:\n{err}")
                    return
                lap = (cloud or {}).get("laptop") or {}
                if lap.get("root_page") and (not self.settings.get("token") or lap["root_page"] != self.settings.get("root_page")):
                    if messagebox.askyesno(TITLE, f"Your Google account has a saved setup for “{lap.get('root_title') or 'Laptop Backup'}”.\n\n"
                                                  "Use it on this laptop? Files already in the cloud won't be uploaded again."):
                        self._apply_setup(cloud)
                        return
                if self.settings.get("token"):
                    account.run_async(lambda: account.upload(self.settings), lambda r, e: self.events.put(self._account_ui))
                self._account_ui()
            self.events.put(ui)
        account.run_async(work, done)

    def _apply_setup(self, js):
        if not account.apply(self.settings, js):
            pages = account.sources_in(js)
            win = tk.Toplevel(self.root)
            win.title("Which page is this laptop's backup?")
            ttk.Label(win, text="Which Notion page is this laptop's backup?", padding=10).pack()
            for pid, t in pages:
                ttk.Button(win, text=t, command=lambda pid=pid, t=t: (
                    self.settings.set("root_page", pid), self.settings.set("root_title", t), win.destroy(),
                    self._after_apply())).pack(fill="x", padx=10, pady=2)
            ttk.Button(win, text="Another page (paste its link in the Notion tab)", command=win.destroy).pack(fill="x", padx=10, pady=8)
            self._reload_settings_ui()
            return
        self._after_apply()

    def _after_apply(self):
        self._reload_settings_ui()
        try:
            winutil.set_startup(self.settings.get("startup"))
        except OSError:
            pass
        messagebox.showinfo(TITLE, "All set. Your backup continues; files already in the cloud won't be uploaded again.")
        if self.settings.get("google_refresh"):
            account.run_async(lambda: account.upload(self.settings), lambda r, e: self.events.put(self._account_ui))

    def show_transfer(self):
        if not self.settings.get("token"):
            messagebox.showinfo(TITLE, "Set up Notion first.")
            return
        code = account.transfer_code(self.settings)
        win = tk.Toplevel(self.root)
        win.title("Move to a new device")
        try:
            import qrcode
            from PIL import ImageTk
            img = qrcode.make(code).resize((300, 300))
            win._qr = ImageTk.PhotoImage(img)
            ttk.Label(win, image=win._qr).pack(padx=16, pady=10)
        except Exception:
            pass
        ttk.Label(win, wraplength=420, padding=(16, 0),
                  text="Phone: Settings → Account → Set up from code → Scan this QR.\n"
                       "Another laptop: Notion tab → Set up from code → paste the code.\n\n"
                       "This code contains your Notion secret: use it only on your own devices.").pack()
        e = ttk.Entry(win, width=50)
        e.insert(0, code)
        e.pack(padx=16, pady=8)

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(code)
            messagebox.showinfo(TITLE, "Code copied. Paste it on the other device, then copy something else to clear it.", parent=win)
        ttk.Button(win, text="Copy code", command=copy).pack(pady=(0, 12))

    def receive_transfer(self):
        code = simpledialog.askstring(TITLE, "Paste the setup code (starts with NSYNC1:):", parent=self.root)
        if not code:
            return
        js = account.read_code(code)
        if not js:
            messagebox.showerror(TITLE, "That isn't a Notion Sync setup code.")
            return
        if messagebox.askyesno(TITLE, "Use this setup on this laptop? Files already in the cloud won't be uploaded again."):
            self._apply_setup(js)

    def ready(self):
        return bool(self.settings.get("signed_in") and self.settings.get("token") and self.settings.get("root_page")
                    and self.settings.get("folders") and not pagefix.conflict(self.settings))

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
        # Moved to its own page: once everything is backed up there, remove its old copies.
        if pagefix.pending_cleanup() and not self.stop_event.is_set() and getattr(self.syncer, "pending", 0) <= 1:
            try:
                pagefix.cleanup(self.settings, self.db, self.log, self.stop_event)
            except Exception as e:
                self.log(f"Couldn't finish removing the old copies yet ({e}); will try after the next backup.")

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
        ticks = 0
        while not self.quitting:
            time.sleep(20)
            ticks += 1
            if ticks % 15 == 0:
                # Keep the setup saved in your Google account up to date (only when something changed).
                try:
                    if account.save_if_changed(self.settings):
                        self.events.put(self._account_ui)
                except Exception:
                    pass
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
        if getattr(self, "voice", None) is not None:
            self._voice_ui()
        try:
            st = "Shared clipboard: " + self.clip.status()
            if self.clip_status.cget("text") != st:
                self.clip_status.config(text=st)
            off = self.clip.latest_offer()
            if self.clip.get_note:
                t, show = self.clip.get_note, False
            elif off:
                t, show = f"Copied on {off.get('devName') or 'another device'}: {self.clip._describe(off)}", True
            else:
                t, show = "", False
            if self.offer_lbl.cget("text") != t:
                self.offer_lbl.config(text=t)
            self.offer_shown = off.get("id") if (off and show) else None
            if show and not self.offer_btn.winfo_ismapped():
                self.offer_btn.pack(side="left", padx=10)
            elif not show and self.offer_btn.winfo_ismapped():
                self.offer_btn.pack_forget()
        except Exception:
            pass
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
                pystray.MenuItem("Voice changer on/off", lambda: self.events.put(self.voice_toggle)),
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
        try:
            self.voice.stop()
        except Exception:
            pass
        self._quit_rest()

    def _quit_rest(self):
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
    # The packaged (windowed) app has no console to print to.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    errors = []
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    app = App(False, listener)
    app.root.report_callback_exception = lambda *a: errors.append(a)
    steps = [
        lambda: [app.nb.select(i) for i in range(9)],
        lambda: print(voice.selftest()),
        lambda: print("AUDIO LIB OK", voice.VoiceChanger.sd().__version__),
        app._voice_devices,
        app._voice_ui,
        app._fill_devices,
        lambda: app.nb.select(TAB_SPACE),
        app.space.refresh,
        lambda: app.nb.select(TAB_FILES),
        app.files.show,
        lambda: app.nb.select(TAB_ACTIVITY),
        app._refresh_status,
        lambda: app.set_interval(30),
        lambda: app.minutes_var.set("45"),
        app._set_custom_interval,
        app._refresh_folder_lists,
        lambda: app.nb.select(0),
        app._account_ui,
        # The sign-in screen and its steps draw without errors.
        lambda: app.signin and app.signin._step_connect(),
        lambda: app.signin and app.signin._step_choose([], [("p1", "Backups")]),
        lambda: app.signin and app.signin._step_choose(
            [devices.Device("r", "d1", "Office Dell", "laptop", "pg", "Office Dell Backup", "2026-11-01T00:00:00Z")],
            [("p1", "Backups")]),
        lambda: app.signin and app.signin._step_name_existing(),
        lambda: app.signin and app.signin._step_sign_in("test note"),
    ]
    for i, step in enumerate(steps):
        app.root.after(1500 + i * 300, step)
    app.root.after(9000, app.quit)
    app.run()
    if errors:
        import traceback
        for e in errors:
            traceback.print_exception(*e)
        sys.exit(1)
    assert app.settings.get("interval") == 45, app.settings.get("interval")
    if clip_sync.WINDOWS:
        # The shared clipboard really can read and write the Windows clipboard.
        clip_sync.write_text("Notion Sync clipboard test")
        assert clip_sync.read() == ("text", "Notion Sync clipboard test"), clip_sync.read()
        tmp = os.path.join(tempfile.gettempdir(), "clip test.txt")
        with open(tmp, "w") as fh:
            fh.write("x")
        clip_sync.write_files([tmp])
        got = clip_sync.read()
        assert got and got[0] == "files" and os.path.normcase(got[1][0]) == os.path.normcase(tmp), got
        from PIL import Image
        png = os.path.join(tempfile.gettempdir(), "clip test.png")
        Image.new("RGB", (40, 30), (200, 30, 30)).save(png)
        clip_sync.write_image(png)
        got = clip_sync.read()
        assert got and got[0] == "image" and got[1].size == (40, 30), got
        print("CLIPBOARD OK")
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
