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
voice = None          # the voice changer (numpy, audio library) loads when its page is first opened
import devices
import core
import ui
import winutil
from files_tab import FilesTab
from space_tab import SpaceTab
from notion import TRAFFIC, NotionClient, NotionError, parse_page_id
from sync import Syncer
from version import BUILD

REPO = "sakib1322/NotionPhoneSync"
INSTANCE_PORT = 47391
TITLE = "Notion Laptop Sync"
TAB_FILES, TAB_SPACE, TAB_NOTION, TAB_ACTIVITY, TAB_DEVICES, TAB_FLASH = 1, 2, 4, 5, 7, 12


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

        ui.make_dpi_aware()
        self.root = tk.Tk()
        self.root.title(TITLE)
        ui.setup_scaling(self.root)
        w, h = ui.px(1180), ui.px(760)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(w, sw - 40), min(h, sh - 80)
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(ui.px(860), ui.px(560))
        try:
            self.root.iconbitmap(winutil.resource("icon.ico"))
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        # Coming back from minimised: redraw everything at once, before anything else runs.
        self.root.bind("<Map>", lambda e: self.root.update_idletasks() if e.widget is self.root else None, add="+")
        self.hidden = bool(minimized)
        if minimized:
            self.root.withdraw()
        self._build()
        threading.Thread(target=self._listen_for_second_launch, daemon=True).start()
        self.clip = clip_sync.ClipSync(self)
        import writer
        self.writer = writer.Writer(self)
        self.writer.start()                   # the keyboard watcher: cheap, and wanted straight away
        self.signin = None
        if not self.settings.get("signed_in"):
            self.signin = device_setup.SignInScreen(self, self._signed_in)
            minimized = False
            self.hidden = False
            self.root.deiconify()
        self.root.after(300, self._tick)
        self.root.after(50, self._pump)
        # Speed: the window is drawn first; background work starts a moment later, one piece at a
        # time, so opening the app isn't held up (this matters a lot on a slow laptop).
        self.root.after(400, self._start_tray)
        self.root.after(1200, self.clip.start)
        self.root.after(2500, lambda: threading.Thread(target=self._scheduler, daemon=True).start())
        self.root.after(4000, lambda: threading.Thread(target=self._devices_loop, daemon=True).start())
        self.root.after(30000, lambda: self.check_update(install=False))
        self.root.after(8000, self._settle_memory)

    def _settle_memory(self):
        """After opening: tidy up once, then keep Python's garbage collector off the startup objects
        (fewer pauses while you click around)."""
        import gc
        try:
            gc.collect()
            gc.freeze()
        except Exception:
            pass
        # No black boxes when the window comes back after being minimised (see winutil).
        mb = winutil.keep_in_memory()
        if mb:
            self.log(f"Keeping {mb} MB of the app in memory so its window redraws instantly.")

    # ---- layout --------------------------------------------------------------

    def _build(self):
        ui.apply_theme(self.root)
        outer = tk.Frame(self.root, bg=ui.BG)
        outer.pack(fill="both", expand=True)
        side_holder = tk.Frame(outer, bg=ui.SIDEBAR)
        side_holder.pack(side="left", fill="y")
        main = tk.Frame(outer, bg=ui.CARD)
        main.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=(10, 12))
        nb = ttk.Notebook(main, style="Hidden.TNotebook")
        nb.pack(fill="both", expand=True)
        self.nb = nb
        self._build_backup(self._tab(nb, "Backup"))
        self.files = FilesTab(self, self._tab(nb, "Files", title=False))
        self.space = SpaceTab(self, self._tab(nb, "Storage on this laptop"))
        self._build_folders(self._tab(nb, "Folders to back up"))
        self._build_notion(self._tab(nb, "Account & clipboard"))
        self._build_activity(self._tab(nb, "Activity"))
        self._build_about(self._tab(nb, "Settings & updates"))
        self._build_devices(self._tab(nb, "Devices"))
        self.voice = None
        self._voice_frame = self._tab(nb, "Voice changer")   # filled in when first opened (it's slow to load)
        self._build_battery(self._tab(nb, "Battery"))
        self._build_writer(self._tab(nb, "Writing helper (AI)"))
        self._build_battusage(self._tab(nb, "Battery usage & health"))
        self.flash = None
        self._flash_frame = self._tab(nb, "Flashcards", title=False)      # filled in when first opened
        self.sidebar = ui.Sidebar(self, side_holder)
        import taskbar
        self.taskbar = taskbar.TaskbarMeter(self)
        nb.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())
        if not self.settings.get("token"):
            nb.select(TAB_NOTION)
        else:
            nb.select(TAB_FILES)
            self.sidebar.select("drive")

    SIDE_KEYS = {0: "backup", 2: "space", 3: "folders", 4: "account", 5: "activity", 6: "settings", 7: "devices", 8: "voice", 9: "battery", 10: "writer", 11: "battusage", 12: "flash"}
    TAB_FLASH = TAB_FLASH

    def _ensure_flash(self):
        if self.flash is None:
            from flash_tab import FlashTab
            self.flash = FlashTab(self, self._flash_frame)
        return self.flash

    def open_files(self, sid, recent=False):
        """Sidebar: show My Drive, Recent or another device's files."""
        self.nb.select(TAB_FILES)
        self.files.open_source(sid, recent=recent)

    def _tab_changed(self):
        try:
            i = self.nb.index("current")
            key = self.SIDE_KEYS.get(i)
            if key and getattr(self, "sidebar", None) and self.sidebar.current != key:
                self.sidebar.select(key)
            if i == 8:
                self._ensure_voice()
            if i == 9:
                self._battery_shown()
            if i == 11:
                self._ensure_battmon()
            if i == TAB_FLASH:
                self._ensure_flash().on_shown()
            if getattr(self, "battmon", None) is not None:
                self.battmon.set_visible(i == 11)
            if i == TAB_DEVICES:
                self._fill_devices()
            if i == TAB_ACTIVITY:
                self._fill_activity()          # straight away, not on the next tick
            if i == TAB_FILES:
                self.files.on_shown()
            elif i == TAB_SPACE:
                self.space.on_shown()
        except Exception as e:
            self.log(f"Files tab error: {e}")

    def _tab(self, nb, name, title=True):
        f = ttk.Frame(nb, padding=(24, 16, 24, 12))
        nb.add(f, text=name)
        if title:
            ttk.Label(f, text=name, style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        return f

    def _h(self, parent, text):
        ttk.Label(parent, text=text, font=(ui.FONT, 11, "bold")).pack(anchor="w", pady=(12, 4))

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

    def _ensure_voice(self):
        """Loads the voice changer the first time it's needed (its page, or the tray menu)."""
        if self.voice is not None:
            return
        global voice
        if voice is None:
            import voice as _voice
            voice = _voice
        self._build_voice(self._voice_frame)

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
        self.v_mic.config(values=["Windows default microphone"])
        self.v_mic.set(self.settings.get("voice_mic") or "Windows default microphone")
        ttk.Button(row, text="Refresh", command=lambda: self._voice_devices(rescan=True)).pack(side="left")

        mon = tk.BooleanVar(value=bool(self.settings.get("voice_monitor")))
        self.v_mon_var = mon
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
        self.v_warn = ttk.Label(f, text="", foreground="#b45309", wraplength=760, justify="left")
        self.v_warn.pack(anchor="w")
        self._p(f, "In your call app: Settings → Audio → Microphone → “CABLE Output (VB-Audio Virtual Cable)”. "
                   "Keep your speaker as normal. To talk normally again, choose “Normal (no change)” or switch the "
                   "call app back to your real microphone.", foreground="#555")
        self._voice_devices()

    def _voice_devices(self, rescan=False):
        """Reads the microphones in the background: the audio library can take seconds to list them."""
        if self.voice is None or getattr(self, "_vscan", False):
            return
        self._vscan = True
        self.v_cable.config(text="Checking your microphones…")

        def work():
            mics, has = [], False
            try:
                if rescan:
                    self.voice.rescan()
                try:
                    mics = [n for _, n in self.voice.mics()]
                except Exception:
                    mics = []
                has = self.voice.cable() is not None
                if not has and not rescan:
                    self.voice.rescan()
                    has = self.voice.cable() is not None
            except Exception:
                pass
            finally:
                self._vscan = False
            self.events.put(lambda: self._voice_devices_show(mics, has))
        threading.Thread(target=work, daemon=True).start()

    def _voice_devices_show(self, mics, has):
        self.v_mic.config(values=["Windows default microphone"] + mics)
        cur = self.settings.get("voice_mic")
        self.v_mic.set(cur if cur in mics else "Windows default microphone")
        self.v_cable.config(text="✓ Virtual microphone (VB-CABLE) found." if has else
                            "Step 1 (once): install VB-CABLE, the free virtual microphone. Download it, unzip, right-click "
                            "VBCABLE_Setup_x64.exe → Run as administrator → Install, then restart the laptop.")
        if has:
            self.v_cable_btn.pack_forget()
        elif not self.v_cable_btn.winfo_ismapped():
            self.v_cable_btn.pack(anchor="w", after=self.v_cable)

    def voice_toggle(self):
        self._ensure_voice()
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
            w = getattr(self.voice, "warning", "")
            if w and self.v_warn.cget("text") != "⚠ " + w:
                self.v_warn.config(text="⚠ " + w)
                if not self.settings.get("voice_monitor"):
                    self.v_mon_var.set(False)
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
                "clipboard": bool(self.settings.get("clipboard_sync")), "wifi": self.clip.lan.connected_names(),
                "ai": __import__("writer").share_payload(self.settings)}

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

    # ---- battery usage per app & battery health -----------------------------------------------------

    def _ensure_battmon(self):
        """The per-app battery monitor starts a minute after the app opens (or when its page is opened),
        so it doesn't slow down opening the app."""
        if getattr(self, "battmon", None) is None:
            import battmon
            self.battmon = battmon.Monitor()
        try:
            self.battmon.set_visible(self.nb.index("current") == 11)
        except Exception:
            pass

    def _build_battusage(self, f):
        self.battmon = None
        self.root.after(60000, self._ensure_battmon)
        top = ttk.Frame(f)
        top.pack(fill="x", pady=(0, 6))
        self.bu_cards = {}
        for key, title in (("now", "Battery"), ("draw", "Power use now"), ("left", "Time left"), ("health", "Battery health")):
            c = tk.Frame(top, bg="#f0f4f9", padx=ui.px(14), pady=ui.px(10))
            c.pack(side="left", padx=(0, 10), fill="y")
            tk.Label(c, text=title, bg="#f0f4f9", fg=ui.MUTED, font=(ui.FONT, 9)).pack(anchor="w")
            big = tk.Label(c, text="…", bg="#f0f4f9", fg=ui.TEXT, font=(ui.FONT, 16, "bold"))
            big.pack(anchor="w")
            small = tk.Label(c, text="", bg="#f0f4f9", fg=ui.MUTED, font=(ui.FONT, 9), justify="left")
            small.pack(anchor="w")
            self.bu_cards[key] = (big, small)
        cols = ("power", "pmin", "phour", "used", "cpu", "gpu")
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=(6, 4))
        t = ttk.Treeview(box, columns=cols, show="tree headings", height=16)
        t.heading("#0", text="App")
        for c, txt, w in (("power", "Power now", 90), ("pmin", "% battery / min", 110), ("phour", "% / hour", 80),
                          ("used", "Used since start", 120), ("cpu", "CPU", 70), ("gpu", "Graphics", 80)):
            t.heading(c, text=txt)
            t.column(c, width=ui.px(w), anchor="e", stretch=False)
        t.column("#0", width=ui.px(280), stretch=True)
        sc = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sc.set)
        t.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        self.bu_tree = t
        self.bu_note = self._p(f, "", foreground="#666")
        self._p(f, "How it's worked out: Windows reports how fast the battery is draining. That's shared out between "
                   "apps by how much processor and graphics work each does; a part is kept for the screen, Wi-Fi and "
                   "Windows. While plugged in there's no drain to measure, so the numbers are estimates (marked ≈). "
                   "Battery health = how much the battery holds now compared with when it was new.", foreground="#666")
        self.root.after(1500, self._battusage_tick)

    def _battusage_tick(self):
        try:
            if self.nb.index("current") == 11:
                self._battusage_fill()
        except Exception as e:
            self.log(f"Battery usage: {e}")
        self.root.after(3000, self._battusage_tick)

    def _battusage_fill(self):
        if self.battmon is None:
            return
        rows, sm, hl = self.battmon.snapshot()
        est = "" if sm.get("measured") else "≈ "
        b1, s1 = self.bu_cards["now"]
        if sm.get("pct") is not None:
            b1.config(text=f"{sm['pct']:.0f}%")
            if sm.get("plugged"):
                s1.config(text="Plugged in" + (f" · charging {sm['charge_w']:.1f} W" if sm.get("charge_w") else ""))
            else:
                s1.config(text="On battery")
        b2, s2 = self.bu_cards["draw"]
        if sm.get("watts") is not None:
            b2.config(text=f"{est}{sm['watts']:.1f} W")
            s2.config(text=f"{est}{sm['pct_min']:.2f}% a minute · {sm['pct_min'] * 60:.0f}% an hour\n"
                           f"CPU {sm['cpu']:.0f}%  · graphics {sm['gpu']:.0f}%")
        b3, s3 = self.bu_cards["left"]
        sl = sm.get("secsleft")
        if sm.get("plugged"):
            b3.config(text="—")
            s3.config(text="Charging" if sm.get("charge_w") else "Plugged in")
        elif isinstance(sl, (int, float)) and sl > 0 and sl < 10 ** 6:
            b3.config(text=f"{int(sl // 3600)}h {int(sl % 3600 // 60)}m")
            s3.config(text="at the current use")
        elif sm.get("watts") and sm.get("pct") is not None:
            mins = sm["pct"] / max(sm["pct_min"], 0.001)
            b3.config(text=f"≈ {int(mins // 60)}h {int(mins % 60)}m")
            s3.config(text="at the current use")
        b4, s4 = self.bu_cards["health"]
        if hl.get("health_pct"):
            b4.config(text=f"{hl['health_pct']:.0f}%")
            s4.config(text=f"Holds {hl['full_wh']:.1f} Wh of {hl['design_wh']:.1f} Wh when new"
                           + (f"\n{hl['cycles']} charge cycles" if hl.get("cycles") else ""))
        else:
            b4.config(text="…")
            s4.config(text="Reading the battery report…")
        t = self.bu_tree
        sel = t.selection()
        t.delete(*t.get_children())
        for r in rows[:60]:
            if r["w"] < 0.01 and r["used_pct"] < 0.005:
                continue
            t.insert("", "end", iid=r["app"], text="  " + r["app"] + (f"  ({r['procs']} processes)" if r["procs"] > 1 else ""),
                     values=(f"{est}{r['w']:.2f} W", f"{est}{r['pct_min']:.3f}%", f"{est}{r['pct_hour']:.1f}%",
                             f"{r['used_pct']:.2f}%", f"{r['cpu']:.1f}%", f"{r['gpu']:.0f}%" if r["gpu"] else ""))
        for i in sel:
            if t.exists(i):
                t.selection_add(i)
        mins = (time.time() - sm.get("since", time.time())) / 60
        self.bu_note.config(text=f"Since this app started ({mins:.0f} min ago): {sm.get('used_pct', 0):.1f}% of the battery used. "
                                 + ("Measured from the battery." if sm.get("measured") else
                                    "Plugged in: estimated from what the apps are doing."))

    # ---- writing helper (AI) ---------------------------------------------------------------------

    def _build_writer(self, f):
        import writer
        self._p(f, "Type in any app — WhatsApp, Chrome, Word, email — then finish with a command like /im. "
                   "Your text is rewritten in place by the AI you choose. Ctrl+Z puts the original back.")
        self.wr_on = tk.BooleanVar(value=bool(self.settings.get("writer_on")))
        ttk.Checkbutton(f, text="Turn on the writing helper", variable=self.wr_on,
                        command=lambda: self.settings.set("writer_on", self.wr_on.get())).pack(anchor="w", pady=(4, 2))
        self.wr_alive = ttk.Label(f, text="", foreground="#666")
        self.wr_alive.pack(anchor="w", pady=(0, 8))

        def alive():
            try:
                w = getattr(self, "writer", None)
                if not self.settings.get("writer_on"):
                    t = "Off."
                elif not w or not w.hook:
                    t = "⚠ Not watching the keyboard — restart the app (Activity shows why)."
                elif w.last_key and time.time() - w.last_key < 120:
                    t = f"✓ Watching your typing (last key {int(time.time() - w.last_key)} s ago). Type a test anywhere, e.g. “i has went.im”."
                else:
                    t = "✓ Ready. Type something in any app to test it, e.g. “i has went.im”."
                self.wr_alive.config(text=t)
            except Exception:
                pass
            self.root.after(2000, alive)
        alive()

        self._h(f, "Which AI")
        g = ttk.Frame(f)
        g.pack(anchor="w", fill="x")
        labels = [v[0] for v in writer.PROVIDERS.values()]
        keys = list(writer.PROVIDERS)
        prov = self.settings.get("writer_provider") or "gemini"
        self.wr_prov = tk.StringVar(value=writer.PROVIDERS.get(prov, writer.PROVIDERS["gemini"])[0])
        self.wr_key = tk.StringVar()
        self.wr_model = tk.StringVar()
        self.wr_base = tk.StringVar(value=self.settings.get("writer_base") or "")
        ttk.Label(g, text="AI").grid(row=0, column=0, sticky="w", pady=4)
        cb = ttk.Combobox(g, textvariable=self.wr_prov, values=labels, state="readonly", width=34)
        cb.grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(g, text="API key").grid(row=1, column=0, sticky="w", pady=4)
        ke = ttk.Entry(g, textvariable=self.wr_key, show="•", width=48)
        ke.grid(row=1, column=1, sticky="w", padx=8)
        self.wr_keylink = ttk.Label(g, text="", foreground="#0b57d0", cursor="hand2")
        self.wr_keylink.grid(row=1, column=2, sticky="w")
        ttk.Label(g, text="Model").grid(row=2, column=0, sticky="w", pady=4)
        self.wr_model_cb = ttk.Combobox(g, textvariable=self.wr_model, width=58)
        self.wr_model_cb.grid(row=2, column=1, sticky="w", padx=8)
        ttk.Button(g, text="Load the list of models", command=self._writer_models).grid(row=2, column=2, sticky="w")
        self.wr_base_lbl = ttk.Label(g, text="Address")
        self.wr_base_e = ttk.Entry(g, textvariable=self.wr_base, width=48)

        def cur():
            return keys[labels.index(self.wr_prov.get())] if self.wr_prov.get() in labels else "gemini"

        if not self.settings.get("writer_auto_migrated"):
            # Switch to the new Auto mode once (best free model, steps down/up by itself).
            ms = dict(self.settings.get("writer_models") or {})
            for pv in ("gemini", "groq", "openrouter"):
                ms[pv] = writer.AUTO
            self.settings.set("writer_models", ms)
            self.settings.set("writer_auto_migrated", True)

        def load(*_):
            p = cur()
            self.settings.set("writer_provider", p)
            self.wr_key.set((self.settings.get("writer_keys") or {}).get(p, ""))
            m = (self.settings.get("writer_models") or {}).get(p) or writer.PROVIDERS[p][3]
            self.wr_model.set(writer.AUTO_LABEL if m == writer.AUTO else m)
            self.wr_model_cb.config(values=[writer.AUTO_LABEL])
            url = writer.PROVIDERS[p][4]
            self.wr_keylink.config(text=("Get a key ↗" if url else ""))
            self.wr_keylink.bind("<Button-1>", lambda e, u=url: url and __import__("webbrowser").open("https://" + u))
            if p == "custom":
                self.wr_base_lbl.grid(row=3, column=0, sticky="w", pady=4)
                self.wr_base_e.grid(row=3, column=1, sticky="w", padx=8)
            else:
                self.wr_base_lbl.grid_remove()
                self.wr_base_e.grid_remove()

        def save(*_):
            p = cur()
            ks = dict(self.settings.get("writer_keys") or {})
            if ks.get(p, "") != self.wr_key.get().strip():
                ats = dict(self.settings.get("writer_keys_at") or {})
                ats[p] = int(time.time() * 1000)
                self.settings.set("writer_keys_at", ats)
                self._dev_push_now = True          # tell your other devices soon
            ks[p] = self.wr_key.get().strip()
            self.settings.set("writer_keys", ks)
            ms = dict(self.settings.get("writer_models") or {})
            ms[p] = writer.AUTO if self.wr_model.get().strip() in (writer.AUTO_LABEL, writer.AUTO) else self.wr_model.get().strip()
            self.settings.set("writer_models", ms)
            if p == "custom":
                self.settings.set("writer_base", self.wr_base.get().strip())
        cb.bind("<<ComboboxSelected>>", load)
        self._wr_load = load
        for v in (self.wr_key, self.wr_model, self.wr_base):
            v.trace_add("write", save)
        self._wr_cur = cur
        load()

        row = ttk.Frame(f)
        row.pack(anchor="w", pady=(8, 2))
        ttk.Button(row, text="Test it", style="Accent.TButton", command=self._writer_test).pack(side="left")
        self.wr_status = ttk.Label(row, text="", foreground="#666", wraplength=ui.px(560), justify="left")
        self.wr_status.pack(side="left", padx=10)

        self._h(f, "AI keys on your other devices")
        self.wr_share = tk.BooleanVar(value=self.settings.get("writer_share") is not False)
        ttk.Checkbutton(f, text="Share my AI keys with my other devices (stored encrypted; only your signed-in "
                                "devices can read them)", variable=self.wr_share,
                        command=lambda: (self.settings.set("writer_share", self.wr_share.get()),
                                         setattr(self, "_dev_push_now", True))).pack(anchor="w")
        self.wr_auto = tk.BooleanVar(value=self.settings.get("writer_autoimport") is not False)
        ttk.Checkbutton(f, text="Add keys from my other devices by themselves (only for AIs this laptop has no key for)",
                        variable=self.wr_auto,
                        command=lambda: self.settings.set("writer_autoimport", self.wr_auto.get())).pack(anchor="w")
        self.wr_others = ttk.Frame(f)
        self.wr_others.pack(anchor="w", fill="x", pady=(4, 0))
        ttk.Button(f, text="Refresh the list", command=lambda: setattr(self, "_dev_push_now", True) or
                   self.wr_others_note.config(text="Refreshing… (takes a few seconds)")).pack(anchor="w", pady=(4, 0))
        self.wr_others_note = ttk.Label(f, text="", foreground="#666")
        self.wr_others_note.pack(anchor="w")
        self._writer_keys_ui()

        prow = ttk.Frame(f)
        prow.pack(anchor="w", fill="x", pady=(8, 0))
        ttk.Button(prow, text="Check the best model now", command=lambda: self._writer_probe(manual=True)).pack(side="left")
        self.wr_probe = ttk.Label(prow, text="", foreground="#666", wraplength=ui.px(560), justify="left")
        self.wr_probe.pack(side="left", padx=10)
        self._p(f, "In Auto, this checks from the most powerful model down and moves back up to the best one that "
                   "works right now. It also runs by itself every 2 hours.", foreground="#666")
        self.root.after(60000, self._writer_probe_loop)

        self._h(f, "How to start a command")
        self.wr_prefix = tk.StringVar(value=self.settings.get("writer_prefix") or "both")
        for val, text in (("/", "Slash — type a space, then /im   (example:  i has went /im)"),
                          (".", "Full stop — right after your text, .im   (example:  i has went.im)"),
                          ("both", "Either one")):
            ttk.Radiobutton(f, text=text, value=val, variable=self.wr_prefix,
                            command=lambda: self.settings.set("writer_prefix", self.wr_prefix.get())).pack(anchor="w")

        self._h(f, "What gets rewritten")
        self.wr_sel = tk.StringVar(value=self.settings.get("writer_select") or "smart")
        for val, text in (("smart", "Smart: the whole message box in chats and browsers; just the paragraph in Word, "
                                    "Google Docs, Notion and similar"),
                          ("box", "Always everything in the text box"),
                          ("paragraph", "Always just the current paragraph")):
            ttk.Radiobutton(f, text=text, value=val, variable=self.wr_sel,
                            command=lambda: self.settings.set("writer_select", self.wr_sel.get())).pack(anchor="w")

        self._h(f, "Commands")
        cmds = "\n".join(f"{k:<10} {v}" for k, v in writer.COMMANDS.items())
        t = tk.Text(f, height=10, wrap="word", font=(ui.FONT, 9), relief="flat", bg="#f3f3f3")
        t.insert("1.0", cmds)
        t.config(state="disabled")
        t.pack(fill="x", pady=(2, 6))
        self._p(f, "Your own commands (one per line, like:  /lawyer = Rewrite it in a formal legal style):",
                foreground="#666")
        self.wr_custom = tk.Text(f, height=4, wrap="word", font=(ui.FONT, 9))
        self.wr_custom.insert("1.0", "\n".join(f"{k} = {v}" for k, v in (self.settings.get("writer_custom") or {}).items()))
        self.wr_custom.pack(fill="x")

        def save_custom(_=None):
            out = {}
            for line in self.wr_custom.get("1.0", "end").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k.startswith("/") and len(k) > 1 and " " not in k and v.strip():
                        out[k] = v.strip()
            self.settings.set("writer_custom", out)
        self.wr_custom.bind("<FocusOut>", save_custom)
        self.wr_custom.bind("<KeyRelease>", save_custom)

    def _writer_keys_ui(self):
        """Lists which AI keys your other devices have, each with a “Use this key” button."""
        import writer
        box = getattr(self, "wr_others", None)
        if box is None:
            return
        for w in box.winfo_children():
            w.destroy()
        mine = self.settings.get("writer_keys") or {}
        rows = writer.others_keys(self.settings)
        if not rows:
            ttk.Label(box, text="None of your other devices has shared an AI key yet.", foreground="#666").pack(anchor="w")
        for d, provs, ai in rows:
            ttk.Label(box, text=f"{d.name}  ({d.type}{', online' if d.online() else ''})",
                      font=(ui.FONT, 10, "bold")).pack(anchor="w", pady=(6, 0))
            for prov, info in provs.items():
                r = ttk.Frame(box)
                r.pack(anchor="w", fill="x")
                when = time.strftime("%d %b", time.localtime(info.get("at") / 1000)) if info.get("at") else ""
                same = writer.mask(mine.get(prov)) == info.get("mask") if mine.get(prov) else False
                label = writer.PROVIDERS.get(prov, (prov,))[0]
                ttk.Label(r, text=f"   {label}:  {info.get('mask')}" + (f"  · model {info['model']}" if info.get("model") else "")
                          + (f"  · added {when}" if when else "")).pack(side="left")
                if same:
                    ttk.Label(r, text="  ✓ same as this laptop", foreground="#188038").pack(side="left", padx=8)
                else:
                    ttk.Button(r, text="Use this key" if not mine.get(prov) else "Use this key instead",
                               command=lambda ai=ai, prov=prov, name=d.name: self._writer_take(ai, prov, name)).pack(side="left", padx=8)
        if getattr(self, "wr_others_note", None):
            self.wr_others_note.config(text="")

    def _writer_take(self, ai, prov, name):
        import writer
        if writer.take_key(self.settings, ai, prov):
            self.settings.set("writer_provider", prov)
            try:
                labels = [v[0] for v in writer.PROVIDERS.values()]
                self.wr_prov.set(writer.PROVIDERS[prov][0])
                self._wr_load()
            except Exception:
                pass
            self.log(f"Writing helper: now using the {writer.PROVIDERS.get(prov, ('AI',))[0]} key from {name}.")
            self.wr_others_note.config(text=f"Done — using the key from {name}.")
            self._writer_keys_ui()
        else:
            self.wr_others_note.config(text="Couldn't read that key (is that device signed in to the same Notion?).")

    def _writer_probe_loop(self):
        """Every 2 hours: check whether a more powerful model is available again."""
        try:
            import writer
            last = self.settings.get("writer_probe_at") or 0
            if (self.settings.get("writer_on") and writer.config(self.settings)[3] == writer.AUTO
                    and writer.config(self.settings)[4] and time.time() - last > 2 * 3600):
                self._writer_probe()
        except Exception:
            pass
        self.root.after(10 * 60 * 1000, self._writer_probe_loop)

    def _writer_probe(self, manual=False):
        import writer
        if writer.config(self.settings)[3] != writer.AUTO:
            if manual:
                self.wr_probe.config(text="This only applies when the model is set to Auto.")
            return
        if manual:
            self.wr_probe.config(text="Checking from the most powerful model down…")

        def work():
            try:
                res = writer.probe(self.settings, log=lambda m: self.events.put(lambda: self.log(m)))
                if not res:
                    txt = "Add an API key first."
                else:
                    best = res[-1][0] if res[-1][1] == "ok" else None
                    lines = [f"{'✓' if st == 'ok' else '✗'} {m}: {'available — in use now' if st == 'ok' else st}"
                             for m, st in res]
                    txt = ("\n".join(lines) + (f"\nUsing {best}." if best else "\nNone answered right now.") +
                           f"\n(Checked {time.strftime('%I:%M %p').lstrip('0')})")
            except Exception as e:
                txt = f"Couldn't check: {e}"
            self.events.put(lambda: self.wr_probe.config(text=txt))
        threading.Thread(target=work, daemon=True).start()

    def _writer_models(self):
        import writer
        p = self._wr_cur()
        key = self.wr_key.get().strip()
        if not key:
            self.wr_status.config(text="Paste your API key first.")
            return
        self.wr_status.config(text="Loading models…")

        def work():
            try:
                names = writer.list_models(self.settings, p, key, self.wr_base.get().strip() if p == "custom" else None)
                ranked = writer.rank_models(p, names)
                order = " → ".join(ranked[:6]) + (" …" if len(ranked) > 6 else "")
                self.events.put(lambda: (self.wr_model_cb.config(values=[writer.AUTO_LABEL] + names),
                                         self.wr_status.config(text=f"{len(names)} models. Auto tries them in this "
                                                                    f"order: {order}")))
            except Exception as e:
                self.events.put(lambda e=e: self.wr_status.config(text=f"Couldn't load models: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def _writer_test(self):
        import writer
        self.wr_status.config(text="Asking the AI…")
        sample = "i has went to the market yesterday and buyed some vegetable for my family"

        def work():
            try:
                out = writer.rewrite(self.settings, writer.COMMANDS["/im"], sample, log=lambda m: self.events.put(lambda: self.log(m)))
                model = writer.config(self.settings)[3]
                used = self.settings.get("writer_last_model") if model == writer.AUTO else model
                if model != writer.AUTO:
                    self.events.put(lambda: self.wr_model.set(model) if self.wr_model.get() != model else None)
                self.events.put(lambda: self.wr_status.config(text=f"Works ({used})! “{sample}” → “{out}”"))
            except Exception as e:
                self.events.put(lambda e=e: self.wr_status.config(text=f"Not working: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def writer_notice(self, text, error=False):
        """A small pill near where you're typing: “✨ Writing…”, or what went wrong."""
        old = getattr(self, "_wr_pill", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
            self._wr_pill = None
        if not text:
            return
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        bg = "#b3261e" if error else "#1f1f1f"
        w.configure(bg=bg)
        tk.Label(w, text=text, bg=bg, fg="white", font=(ui.FONT, 10), padx=ui.px(12), pady=ui.px(6)).pack()
        x, y = self.root.winfo_pointerx(), self.root.winfo_pointery()
        try:
            import ctypes
            from ctypes import wintypes as wt

            class GUITHREADINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                            ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                            ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]
            gi = GUITHREADINFO()
            gi.cbSize = ctypes.sizeof(GUITHREADINFO)
            if ctypes.windll.user32.GetGUIThreadInfo(0, ctypes.byref(gi)) and gi.hwndCaret:
                pt = wt.POINT(gi.rcCaret.left, gi.rcCaret.bottom)
                ctypes.windll.user32.ClientToScreen(gi.hwndCaret, ctypes.byref(pt))
                x, y = pt.x, pt.y
        except Exception:
            pass
        w.geometry(f"+{x + ui.px(8)}+{y + ui.px(10)}")
        self._wr_pill = w
        w.after(6000 if error else 60000, lambda: w.winfo_exists() and w.destroy())

    # ---- battery ------------------------------------------------------------------------------

    def _build_battery(self, f):
        import battery
        self.batt_now = self._p(f, "")
        self._h(f, "Battery charging (Dell)")
        self._p(f, "The same choices as Dell's battery app. They're saved in the laptop itself, so they keep "
                   "working even when this app isn't running. Windows asks for permission when you apply one.",
                foreground="#666")
        box = ttk.Frame(f)
        box.pack(fill="x", pady=(6, 4))
        left = ttk.Frame(box)
        left.pack(side="left", fill="y", padx=(0, 24))
        ttk.Separator(box, orient="vertical").pack(side="left", fill="y", padx=(0, 24))
        right = ttk.Frame(box)
        right.pack(side="left", fill="both", expand=True)
        self.batt_mode = tk.StringVar(value=self.settings.get("batt_mode") or "Standard")
        for key, title, _ in battery.MODES:
            ttk.Radiobutton(left, text=title, value=key, variable=self.batt_mode,
                            command=self._battery_mode_changed).pack(anchor="w", pady=5)
        self.batt_title = ttk.Label(right, text="", font=(ui.FONT, 14))
        self.batt_title.pack(anchor="w")
        self.batt_desc = ttk.Label(right, text="", wraplength=ui.px(460), justify="left", foreground="#555")
        self.batt_desc.pack(anchor="w", pady=(6, 8))
        self.batt_custom = ttk.Frame(right)
        self.batt_start = tk.IntVar(value=self.settings.get("batt_start") or 50)
        self.batt_stop = tk.IntVar(value=self.settings.get("batt_stop") or 80)
        for text, var, lo, hi in (("Start charging: (at or below)", self.batt_start, 50, 95),
                                  ("Stop charging: (at or above)", self.batt_stop, 55, 100)):
            ttk.Label(self.batt_custom, text=text).pack(anchor="w", pady=(6, 2))
            row = ttk.Frame(self.batt_custom)
            row.pack(fill="x")
            sc = ttk.Scale(row, from_=lo, to=hi, orient="horizontal", length=ui.px(320),
                           command=lambda v, var=var: (var.set(int(float(v))), self._battery_check()))
            sc.set(var.get())
            sc.pack(side="left")
            sp = ttk.Spinbox(row, from_=lo, to=hi, width=5, textvariable=var,
                             command=lambda sc=sc, var=var: (sc.set(var.get()), self._battery_check()))
            sp.pack(side="left", padx=10)
            sp.bind("<KeyRelease>", lambda e, sc=sc, var=var: (sc.set(var.get()) if str(var.get()).isdigit() else None,
                                                               self._battery_check()))
            ttk.Label(row, text="%").pack(side="left")
        self.batt_warn = tk.Label(self.batt_custom, text="", bg="#f2a900", fg="#1a1a1a", font=(ui.FONT, 9, "bold"),
                                  anchor="w", padx=10, pady=6)
        # BIOS setup password (only for laptops that have one).
        self.pw_box = ttk.Frame(f)
        ttk.Label(self.pw_box, text="BIOS setup password:").pack(side="left")
        self.bios_pw = tk.StringVar(value=battery.unprotect(self.settings.get("bios_pwd")) if self.settings.get("bios_pwd") else "")
        self.pw_entry = ttk.Entry(self.pw_box, textvariable=self.bios_pw, show="•", width=22)
        self.pw_entry.pack(side="left", padx=8)
        self.pw_remember = tk.BooleanVar(value=bool(self.settings.get("bios_pwd")))
        ttk.Checkbutton(self.pw_box, text="Remember it on this laptop (encrypted by Windows)",
                        variable=self.pw_remember).pack(side="left", padx=8)
        self.pw_note = ttk.Label(f, text="Your laptop has a BIOS setup password, so Dell asks for it before changing "
                                         "charging. Type it above and press Apply. (It's only sent to Dell's tool on this "
                                         "laptop.)", foreground="#666", wraplength=ui.px(640), justify="left")
        if self.settings.get("bios_pwd") or self.settings.get("bios_pwd_needed"):
            self.pw_box.pack(anchor="w", pady=(10, 0))
        btns = ttk.Frame(f)
        btns.pack(anchor="w", pady=(10, 4))
        self.batt_apply = ttk.Button(btns, text="Apply", style="Accent.TButton", command=self._battery_apply)
        self.batt_apply.pack(side="left")
        ttk.Button(btns, text="Read the current setting", command=lambda: self._battery_read(force=True)).pack(
            side="left", padx=8)

        def open_dell():
            def work():
                ok = battery.open_dell_app()
                self.events.put(lambda: None if ok else self.batt_status.config(
                    text="Dell Power Manager isn't installed on this laptop."))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(btns, text="Open Dell Power Manager", command=open_dell).pack(side="left")
        self.batt_status = self._p(f, "", foreground="#666")
        self._battery_mode_changed()

        self._h(f, "Charge reminders (works on any laptop, no BIOS needed)")
        self._p(f, "The app can't stop the charger itself, but it can tell you when to unplug and when to plug "
                   "back in — keeping the battery between two levels makes it last for years.", foreground="#666")
        self.rem_on = tk.BooleanVar(value=bool(self.settings.get("remind_on")))
        ttk.Checkbutton(f, text="Remind me to unplug and plug in", variable=self.rem_on,
                        command=lambda: self.settings.set("remind_on", self.rem_on.get())).pack(anchor="w", pady=(4, 2))
        rr = ttk.Frame(f)
        rr.pack(anchor="w", pady=2)
        self.rem_stop = tk.IntVar(value=self.settings.get("remind_stop") or 80)
        self.rem_start = tk.IntVar(value=self.settings.get("remind_start") or 40)

        def save_rem(*_):
            try:
                a, b = int(self.rem_start.get()), int(self.rem_stop.get())
            except (tk.TclError, ValueError):
                return
            if 5 <= a < b <= 100:
                self.settings.set("remind_start", a)
                self.settings.set("remind_stop", b)
        ttk.Label(rr, text="Unplug at").pack(side="left")
        ttk.Spinbox(rr, from_=50, to=100, increment=5, width=5, textvariable=self.rem_stop, command=save_rem).pack(side="left", padx=6)
        ttk.Label(rr, text="%      Plug in at").pack(side="left")
        ttk.Spinbox(rr, from_=5, to=95, increment=5, width=5, textvariable=self.rem_start, command=save_rem).pack(side="left", padx=6)
        ttk.Label(rr, text="%").pack(side="left")
        self.rem_stop.trace_add("write", save_rem)
        self.rem_start.trace_add("write", save_rem)
        ttk.Button(f, text="Show a test reminder", command=lambda: self._charge_popup(
            "Battery 80% — unplug the charger", "Example of the reminder you'll see.")).pack(anchor="w", pady=4)
        self.root.after(20000, self._remind_loop)

        self._h(f, "Sleep")
        self._p(f, "If the laptop won't stay asleep (a USB dongle, mouse or network card wakes it), stop those "
                   "devices from waking it. To put it to sleep even when apps keep it awake, right-click the "
                   "taskbar read-out → Sleep now (force), or use the buttons below.", foreground="#666")
        srow = ttk.Frame(f)
        srow.pack(anchor="w", pady=4)

        def stop_wake():
            self.sleep_status.config(text="Working… say Yes to Windows' prompt.")

            def work():
                done, err = battery.stop_wake_devices()
                self.events.put(lambda: self.sleep_status.config(
                    text=("Not changed: " + err) if err else
                    ("Done. These can't wake the laptop any more: " + ", ".join(done) if done else
                     "Done. No device was set to wake the laptop.")))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(srow, text="Stop devices from waking the laptop", command=stop_wake).pack(side="left")

        def stop_blockers():
            self.sleep_status.config(text="Working… say Yes to Windows' prompt.")

            def work():
                done, err = battery.stop_awake_blockers()
                self.events.put(lambda: self.sleep_status.config(
                    text=("Not changed: " + err) if err else
                    ("Done. Windows now ignores these when deciding to sleep: " + ", ".join(done) if done else
                     "Nothing is keeping the laptop awake right now.")))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(srow, text="Stop apps keeping it awake", command=stop_blockers).pack(side="left", padx=(8, 0))

        def go(hib):
            import taskbar
            threading.Thread(target=taskbar.hard_sleep, args=(hib, self), daemon=True).start()
        srow2 = ttk.Frame(f)
        srow2.pack(anchor="w", pady=4)
        ttk.Button(srow2, text="Sleep now (force)", command=lambda: go(False)).pack(side="left")
        ttk.Button(srow2, text="Hibernate now", command=lambda: go(True)).pack(side="left", padx=8)
        self.sleep_status = self._p(f, "", foreground="#666")
        self._p(f, "Hibernate here doesn't keep Windows' big hibernate file (about 40% of your RAM) on drive C: "
                   "it's made just before hibernating and deleted as soon as the laptop wakes. Windows asks "
                   "permission the first time only.", foreground="#666")

        self.hib_keep = tk.BooleanVar(value=bool(self.settings.get("hib_keep_file")))
        ttk.Checkbutton(f, text="Keep the hibernate file after waking (Windows' normal way: uses the space all the "
                                "time, but is the most stable)", variable=self.hib_keep,
                        command=lambda: self.settings.set("hib_keep_file", self.hib_keep.get())).pack(anchor="w", pady=(2, 2))
        self.root.after(45000, self._check_hib_crash)
        if globals().get("_last_crash"):
            txt = globals()["_last_crash"].strip().splitlines()
            self.root.after(3000, lambda: self.log("The app closed unexpectedly last time. Details: "
                                                   + " | ".join(l.strip() for l in txt[:12])))

        def free_hib():
            def work():
                err = battery.hibernate_file_off()
                self.events.put(lambda: self.sleep_status.config(
                    text=("Not changed: " + err) if err else "Done — the hibernate file is deleted and its space is free."))
            threading.Thread(target=work, daemon=True).start()
        ttk.Button(f, text="Delete the hibernate file now (free its space)", command=free_hib).pack(anchor="w", pady=2)

        self._h(f, "Bluetooth devices")
        self.bt_list = self._p(f, "Checking…")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=4)
        ttk.Button(row, text="Check again", command=self._bt_refresh).pack(side="left")
        self._p(f, "Shows earbuds, headphones, mice and keyboards that tell Windows their battery level. "
                   "To show them on the taskbar, turn it on in Settings & updates → Taskbar.", foreground="#666")

    def _remind_loop(self):
        """Every 30 s: is it time to unplug / plug in? Reminds once, then every 10 minutes while it's still so."""
        try:
            if self.settings.get("remind_on"):
                import battery
                b = battery.laptop_battery()
                if b:
                    pct, plugged = b
                    stop, start = int(self.settings.get("remind_stop") or 80), int(self.settings.get("remind_start") or 40)
                    want = ("unplug" if plugged and pct >= stop else "plug" if not plugged and pct <= start else None)
                    now = time.time()
                    st = getattr(self, "_remind_state", (None, 0))
                    if want is None:
                        self._remind_state = (None, 0)
                    elif want != st[0] or now - st[1] > 600:
                        self._remind_state = (want, now)
                        if want == "unplug":
                            self._charge_popup(f"Battery {pct}% — unplug the charger",
                                               f"You asked to stop charging at {stop}%.")
                        else:
                            self._charge_popup(f"Battery {pct}% — plug in the charger",
                                               f"You asked to start charging at {start}%.")
        except Exception as e:
            self.log(f"Charge reminder: {e}")
        self.root.after(30000, self._remind_loop)

    def _charge_popup(self, title, text):
        """A clear pop-up near the clock, with a sound. (Only this one — not a Windows notification too.)"""
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass
        old = getattr(self, "_popup", None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg="#c4c7c5")
        card = tk.Frame(w, bg="white", padx=ui.px(18), pady=ui.px(14))
        card.pack(padx=1, pady=1)
        tk.Label(card, text="🔋  " + title, bg="white", fg="#1f1f1f", font=(ui.FONT, 12, "bold")).pack(anchor="w")
        tk.Label(card, text=text, bg="white", fg="#5f6368", font=(ui.FONT, 10)).pack(anchor="w", pady=(4, 10))
        ttk.Button(card, text="OK", command=w.destroy).pack(anchor="e")
        w.update_idletasks()
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        w.geometry(f"+{sw - w.winfo_reqwidth() - ui.px(16)}+{sh - w.winfo_reqheight() - ui.px(64)}")
        self._popup = w
        w.after(60000, lambda: w.winfo_exists() and w.destroy())

    def _check_hib_crash(self):
        """If Windows blue-screened soon after this app hibernated the laptop, switch to Windows'
        normal way (keep the hibernate file) and say so."""
        last = self.settings.get("last_hibernate") or 0

        def work():
            import battery
            c = battery.last_crash()
            if not c:
                return
            t, code = c
            if time.time() - t < 24 * 3600 and self.settings.get("crash_logged") != t:
                self.events.put(lambda: (self.settings.set("crash_logged", t), self.log(
                    f"Windows had a blue screen at {time.strftime('%d %b %I:%M %p', time.localtime(t))} "
                    f"(stop code {code}).")))
            if not last:
                return
            if t > last and t - last < 6 * 3600 and self.settings.get("hib_crash_seen") != t:
                def done():
                    self.settings.set("hib_crash_seen", t)
                    self.settings.set("hib_keep_file", True)
                    try:
                        self.hib_keep.set(True)
                    except Exception:
                        pass
                    msg = (f"Windows crashed (blue screen, stop code {code}) after hibernating. To be safe, the app now "
                           "keeps the hibernate file like Windows normally does (Battery page → Sleep).")
                    self.log(msg)
                    self.notify("Hibernate switched to the safe way", msg)
                self.events.put(done)
        threading.Thread(target=work, daemon=True).start()

    def _battery_mode_changed(self):
        import battery
        key = self.batt_mode.get()
        for k, title, desc in battery.MODES:
            if k == key:
                self.batt_title.config(text=title)
                self.batt_desc.config(text=desc)
        if key == "Custom":
            self.batt_custom.pack(anchor="w", fill="x")
        else:
            self.batt_custom.pack_forget()
        self._battery_check()

    def _battery_check(self):
        if not hasattr(self, "batt_apply"):     # still building the page
            return True
        try:
            a, b = int(self.batt_start.get()), int(self.batt_stop.get())
        except (tk.TclError, ValueError):
            a, b = 0, 0
        msg = ""
        if self.batt_mode.get() == "Custom":
            if not 50 <= a <= 95:
                msg = "Start charging must be between 50% and 95%"
            elif not 55 <= b <= 100:
                msg = "Stop charging must be between 55% and 100%"
            elif b - a < 5:
                msg = f"Stop charging must be at least 5% above start ({a + 5}% or more)"
        if msg:
            self.batt_warn.config(text="⚠  Warning: " + msg)
            self.batt_warn.pack(anchor="w", fill="x", pady=(10, 0))
        else:
            self.batt_warn.pack_forget()
        self.batt_apply.state(["disabled"] if msg else ["!disabled"])
        return not msg

    def _battery_shown(self):
        import battery
        b = battery.laptop_battery()
        self.batt_now.config(text=(f"This laptop: {b[0]}% · {'plugged in' if b[1] else 'on battery'}" if b else
                                   "This laptop's battery couldn't be read."))
        if not getattr(self, "_batt_read_once", False):
            self._batt_read_once = True
            self._battery_read()
            self._bt_refresh()

    def _battery_read(self, force=False):
        import battery
        self.batt_status.config(text="Reading the laptop's charging setting…")

        def work():
            d = battery.dell_read(elevated=force)
            mk = battery.maker() if d.get("error") else ""

            def done():
                if d.get("mode"):
                    names = {k: t for k, t, _ in battery.MODES}
                    self.batt_mode.set(d["mode"])
                    if d.get("start"):
                        self.batt_start.set(d["start"])
                    if d.get("stop"):
                        self.batt_stop.set(d["stop"])
                    self._battery_mode_changed()
                    extra = f" (start {d['start']}%, stop {d['stop']}%)" if d["mode"] == "Custom" and d.get("stop") else ""
                    self.batt_status.config(text=f"Current setting: {names.get(d['mode'], d['mode'])}{extra}.")
                elif mk and "dell" not in mk.lower():
                    self.batt_status.config(text=f"This is a {mk} laptop. These settings are for Dell laptops.")
                else:
                    self.batt_status.config(text="Couldn't read the current setting here (that's normal without "
                                                 "admin rights). You can still choose one and press Apply.")
            self.events.put(done)
        threading.Thread(target=work, daemon=True).start()

    def _battery_apply(self):
        import battery
        if not self._battery_check():
            return
        mode = self.batt_mode.get()
        a, b = int(self.batt_start.get()), int(self.batt_stop.get())
        self.batt_status.config(text="Applying… say Yes to Windows' prompt. The first time this can take a few "
                                     "minutes while Dell's helper tool is installed.")
        self.batt_apply.state(["disabled"])

        pw = self.bios_pw.get() if self.pw_box.winfo_ismapped() else ""

        def work():
            err = battery.dell_set(mode, a, b, password=pw or None)

            def done():
                self.batt_apply.state(["!disabled"])
                if err == "PASSWORD":
                    self.settings.set("bios_pwd_needed", True)
                    if not self.pw_box.winfo_ismapped():
                        self.pw_box.pack(anchor="w", pady=(10, 0), before=self.batt_apply.master)
                    self.pw_note.pack(anchor="w", pady=(4, 0), before=self.batt_apply.master)
                    self.pw_entry.focus_set()
                    self.batt_status.config(text="Not changed yet: this laptop needs its BIOS setup password (see above).")
                    return
                if err:
                    self.batt_status.config(text="Not changed: " + err)
                    self.log(f"Battery charging not changed: {err} [{getattr(battery.dell_set, 'last', '')}]")
                else:
                    self.pw_note.pack_forget()
                    try:
                        self.settings.set("bios_pwd", battery.protect(pw) if (pw and self.pw_remember.get()) else None)
                    except Exception:
                        pass
                    self.settings.set("batt_mode", mode)
                    self.settings.set("batt_start", a)
                    self.settings.set("batt_stop", b)
                    names = {k: t for k, t, _ in battery.MODES}
                    self.batt_status.config(text=f"Done — charging is now set to {names[mode]}" +
                                                 (f" (start {a}%, stop {b}%)." if mode == "Custom" else "."))
                    self.log(f"Battery charging set to {names[mode]}" + (f" ({a}–{b}%)" if mode == "Custom" else ""))
            self.events.put(done)
        threading.Thread(target=work, daemon=True).start()

    def _bt_refresh(self):
        import battery
        self.bt_list.config(text="Checking…")

        def work():
            try:
                items = battery.bt_levels()
            except Exception:
                items = []
            self.events.put(lambda: self.bt_list.config(
                text="\n".join(f"{n}:  {b}%" for n, b in items) if items else
                "No connected Bluetooth device is reporting its battery right now."))
        threading.Thread(target=work, daemon=True).start()

    def _build_about(self, f):
        self._h(f, "App updates")
        self.update_lbl = self._p(f, f"Version: build {BUILD}")
        ttk.Button(f, text="Check for update & install", command=lambda: self.check_update(install=True)).pack(anchor="w", pady=4)
        self._p(f, "Updating keeps your backup progress. The app restarts and picks up where it left off.",
                foreground="#666")

        self._h(f, "Taskbar")
        self.taskbar_vars = {}
        for key, text in (("taskbar_speed", "Show internet speed on the taskbar (↑ upload, ↓ download)"),
                          ("taskbar_desktop", "Show the name of the desktop you're on (Win+Tab desktops) on the taskbar"),
                          ("taskbar_bt", "Show the battery of connected Bluetooth devices (earbuds, mouse…) on the taskbar")):
            v = tk.BooleanVar(value=bool(self.settings.get(key)))
            self.taskbar_vars[key] = v
            ttk.Checkbutton(f, text=text, variable=v,
                            command=lambda k=key, v=v: (self.settings.set(k, v.get()), self.taskbar.refresh_settings())
                            ).pack(anchor="w")
        self._p(f, "Drag it left or right along the taskbar to move it. Right-click it for options; "
                   "double-click to open this app. It hides while a full-screen video or game is showing.",
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
            if self.settings.get("signed_in") and self.settings.get("token") and (
                    time.time() - last > 300 or getattr(self, "_dev_push_now", False)):
                self._dev_push_now = False
                try:
                    reg = devices.Registry(self.settings, self.db)
                    reg.register_me(self.device_status())
                    reg.all()
                    last = time.time()
                    import writer
                    added = writer.auto_import(self.settings)
                    for prov, dev in added:
                        self.log(f"Writing helper: added the {writer.PROVIDERS.get(prov, ('AI',))[0]} key from {dev}.")
                    self.events.put(self._writer_keys_ui)
                    self.events.put(self._account_ui)
                    self.events.put(self._fill_devices)
                    owner = pagefix.conflict(self.settings)
                    if owner and not getattr(self, "conflict_open", False):
                        self.conflict_open = True
                        self.events.put(lambda o=owner: self._show_conflict(o))
                except Exception as e:
                    self.log(f"Couldn't update your devices list ({e}).")
                    last = time.time() - 240
            self.stop_event.wait(5 if getattr(self, "_dev_push_now", False) else 20)

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

    def _pump(self):
        """Runs results from background work (file lists, previews, progress) right away, not once a second."""
        t0 = time.time()
        busy = False
        while time.time() - t0 < 0.04:
            try:
                self.events.get_nowait()()
                busy = True
            except queue.Empty:
                break
            except Exception:
                busy = True
        # Quick while there's work; when there's none, check less often so the laptop can rest
        # (it used to wake up 33 times a second even while hidden in the tray).
        if busy:
            self._idle_pumps = 0
            delay = 15
        else:
            self._idle_pumps = getattr(self, "_idle_pumps", 0) + 1
            delay = 30 if self._idle_pumps < 20 else (250 if self.hidden else 80)
        self.root.after(delay, self._pump)

    def _tick(self):
        if self.hidden:
            self._tick_hidden()
            return
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

    def _tick_hidden(self):
        """While the window is hidden: no redrawing, just the results of background work and the tray text."""
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
        if self.tray is not None:
            state = "Syncing" if self.running() else "Idle"
            try:
                self.tray.title = f"{TITLE}: {state}  ↑ {core.human_rate(su)}"[:127]
            except Exception:
                pass
        self.root.after(2000, self._tick)

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
        # Counting thousands of files is slow (and waits while a backup writes), so it's done in the
        # background and the last numbers are shown.
        c = getattr(self, "_counts", None) or {}
        if not getattr(self, "_counting", False) and time.time() - getattr(self, "_counts_at", 0) > 5:
            self._counting = True

            def count():
                try:
                    self._counts = self.db.counts()
                    self._counts_at = time.time()
                finally:
                    self._counting = False
            threading.Thread(target=count, daemon=True).start()
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
            self._fill_activity()

    def _fill_activity(self):
        """Shows the activity log — only redrawn when something new was logged, and it stays where you scrolled."""
        text = self.log.tail(300)
        if text == getattr(self, "_activity_text", None):
            return
        self._activity_text = text
        at_end = self.log_box.yview()[1] > 0.98
        y = self.log_box.yview()[0]
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", text)
        self.log_box.configure(state="disabled")
        self.log_box.yview_moveto(1.0 if at_end else y)

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
        """Opens the window fully drawn (no black flash): it's made invisible, drawn, then shown."""
        r = self.root
        was_hidden = self.hidden
        self.hidden = False
        self._idle_pumps = 0
        try:
            r.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        r.deiconify()
        r.state("normal")
        r.update_idletasks()
        r.lift()
        r.focus_force()

        def reveal():
            try:
                r.update_idletasks()
                r.attributes("-alpha", 1.0)
            except tk.TclError:
                pass
        r.after(40, reveal)
        if was_hidden:
            r.after(60, self._catch_up)

    def _catch_up(self):
        """Brings the page up to date straight away after being hidden (it isn't redrawn while hidden)."""
        try:
            self._refresh_status()
            self._tab_changed()
        except Exception:
            pass

    def hide_to_tray(self):
        if self.tray is None:
            self.quit()
            return
        if not self.settings.get("closed_to_tray_hint"):
            self.settings.set("closed_to_tray_hint", True)
            messagebox.showinfo(TITLE, "Notion Laptop Sync keeps running in the system tray (near the clock) "
                                       "so auto-sync keeps working.\n\nTo exit fully, right-click the tray icon → Quit.")
        self.root.withdraw()
        self.hidden = True

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
        try:
            self.battmon.stop()
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
                latest = None
                # 1) The fixed download address (not limited like GitHub's API).
                base = f"https://github.com/{REPO}/releases/download/win-latest/"
                try:
                    v = requests.get(base + "win-version.txt", timeout=20)
                    if v.ok and v.text.strip().isdigit():
                        latest = (int(v.text.strip()), base + "NotionLaptopSync-Setup.exe", 0)
                except requests.RequestException:
                    pass
                rels = []
                if latest is None:
                    # 2) Older way: GitHub's API (limited to 60 checks an hour per internet connection).
                    r = requests.get(f"https://api.github.com/repos/{REPO}/releases?per_page=30",
                                     headers={"Accept": "application/vnd.github+json"}, timeout=20)
                    if r.status_code in (403, 429):
                        raise RuntimeError("GitHub is busy right now (too many update checks from your internet "
                                           "connection). Try again in an hour.")
                    r.raise_for_status()
                    rels = r.json()
                for rel in rels:
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
    shots = os.environ.get("SELFTEST_SHOTS")
    if shots:
        core.Settings().set("signed_in", "selftest")     # screenshots of the pages, not the sign-in screen
    app = App(False, listener)
    app.root.report_callback_exception = lambda *a: errors.append(a)

    def shot(name):
        """Saves a picture of the window (only when the build asks for it, to check the look)."""
        if not shots:
            return
        try:
            from PIL import ImageGrab
            app.root.update_idletasks()
            r = app.root
            x, y, w, h = r.winfo_rootx(), r.winfo_rooty(), r.winfo_width(), r.winfo_height()
            os.makedirs(shots, exist_ok=True)
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(os.path.join(shots, name + ".png"))
        except Exception as e:
            print("shot failed", name, e)
    def shot_win(w, name):
        if not shots:
            return
        try:
            from PIL import ImageGrab
            w.update_idletasks()
            x, y = w.winfo_rootx(), w.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + w.winfo_width(), y + w.winfo_height())).save(os.path.join(shots, name + ".png"))
        except Exception as e:
            print("shot failed", name, e)

    steps = [
        lambda: [app.nb.select(i) for i in range(9)],
        app._ensure_voice,
        app._ensure_battmon,
        lambda: print(voice.selftest()),
        lambda: print("AUDIO LIB OK", voice.VoiceChanger.sd().__version__),
        app._voice_devices,
        app._voice_ui,
        app._fill_devices,
        lambda: app.nb.select(TAB_SPACE),
        app.space.refresh,
        lambda: app.nb.select(TAB_FILES),
        app.files.show,
        # The Drive-style file views draw without errors (with a few made-up files).
        lambda: (app.db.upsert_idx([(f"st{i}", "SELFTEST", n, p, 1000 * i, 1700000000000 + i, core.kind(n), f"stb{i}", "", "Laptop", "")
                                    for i, (n, p) in enumerate([("a.pdf", ""), ("b.jpg", ""), ("c.docx", "Work"), ("d.zip", "Work/Old")])]),
                 setattr(app.files, "root_id", "SELFTEST"), app.files.show()),
        lambda: app.files.set_view("list"),
        app.files.toggle_details,
        lambda: app.files._click(0, type("E", (), {"state": 0})()) if app.files.view == "grid" else app.files.select_all(),
        lambda: app.files.set_view("grid"),
        lambda: app.files._click(1, type("E", (), {"state": 0})()),
        app.files.toggle_details,
        app.files.select_all,
        app.files.select_none,
        lambda: app.files.go_to("Work"),
        lambda: app.open_files(None, recent=True),
        app.sidebar.rebuild,
        lambda: app.sidebar.select("backup"),
        lambda: app.open_files(app.settings.get("root_page")),
        # Battery page: Dell modes UI and the Bluetooth/battery checks run without errors.
        lambda: app.nb.select(9),
        lambda: (app.batt_mode.set("Custom"), app._battery_mode_changed(), app.batt_start.set(80), app.batt_stop.set(82),
                 print("BATTERY warn:", not app._battery_check()), app.batt_stop.set(90), print("BATTERY ok:", app._battery_check())),
        lambda: print("BATTERY read:", ascii(__import__("battery").dell_read()), "bt:", ascii(__import__("battery").bt_levels()),
                      "laptop:", __import__("battery").laptop_battery()) if clip_sync.WINDOWS else None,
        lambda: app._charge_popup("Battery 80% — unplug the charger", "Self-test"),
        lambda: (app._popup.destroy(), print("REMINDER OK")),
        lambda: app.nb.select(10),
        lambda: print("WRITER keyboard watcher:", bool(app.writer.hook), "commands:", len(__import__("writer").commands(app.settings)))
        if clip_sync.WINDOWS else None,
        lambda: app.nb.select(11),
        lambda: print("BATTUSAGE rows:", len(app.battmon.snapshot()[0]), "health:", ascii(app.battmon.snapshot()[2])),
        # Taskbar read-out: desktop name and speed can be read, and the window appears.
        lambda: (print("TASKBAR OK desktop:", __import__("taskbar").desktop_name(),
                       "window:", bool(app.taskbar.hwnd), "in taskbar:", app.taskbar.embedded, "shown:", app.taskbar.visible, "size:", app.taskbar.size,
                       "speed:", ascii(app.taskbar.up_text),
                                              "rects:", __import__("taskbar").taskbar_rects()))
        if clip_sync.WINDOWS else None,
        # The new look and drag-and-drop really loaded (in the built app too).
        lambda: None if not clip_sync.WINDOWS or (app.files.can_drop and ttk.Style().theme_use().startswith("sun-valley")) else
        errors.append((RuntimeError, RuntimeError(f"look not loaded: theme={ttk.Style().theme_use()} drop={app.files.can_drop}"), None)),
        lambda: print("LOOK OK", ttk.Style().theme_use(), "icons:", ui.ICON_FONT, "drop:", app.files.can_drop),
        # Flashcards: make a deck, add cards, study, undo, export and import again, browse.
        lambda: app.nb.select(TAB_FLASH),
        app._ensure_flash,
        lambda: (app.flash.col.add_note(app.flash.col.deck_by_name("Selftest deck"), "Front [img:none.png]", "Back", "t1", True),
                 [app.flash.col.add_note(app.flash.col.deck_by_name("Law::Intellectual Property"), q, a, "ip")
                  for q, a in (("What does copyright protect?", "The expression of ideas, not the ideas themselves."),
                               ("How long does a patent last in Bangladesh?", "20 years from the filing date."),
                               ("TRIPS stands for…", "Trade-Related Aspects of Intellectual Property Rights"))],
                 app.flash.refresh()),
        lambda: shot("flash_home"),
        lambda: app.flash.start_study(app.flash.col.deck_by_name("Law::Intellectual Property")),
        lambda: shot("flash_question"),
        lambda: app.flash.reveal(),
        lambda: shot("flash_answer"),
        lambda: app.flash.show_home(),
        lambda: app.flash.start_study(app.flash.col.deck_by_name("Selftest deck")),
        lambda: app.flash.reveal(),
        lambda: app.flash.grade(3),
        lambda: app.flash.undo(),
        lambda: (app.flash.reveal(), app.flash.grade(4)),
        lambda: app.flash.show_home(),
        lambda: print("FLASH export:", app.flash.col.export_apkg(os.path.join(tempfile.gettempdir(), "selftest.apkg"))),
        lambda: print("FLASH import:", app.flash.col.import_apkg(os.path.join(tempfile.gettempdir(), "selftest.apkg"))),
        lambda: app.flash.tree.selection_set(str(app.flash.col.deck_by_name("Law::Intellectual Property"))),
        lambda: setattr(app, "_opt", app.flash.options_dialog()),
        lambda: print("FLASH options window:", app._opt is not None and app._opt.winfo_exists()),
        lambda: shot_win(app._opt, "flash_options") if app._opt else None,
        lambda: app._opt.destroy() if app._opt else None,
        lambda: setattr(app, "_bw", app.flash.browse()),
        lambda: shot_win(app._bw, "flash_browse"),
        lambda: app._bw.destroy(),
        lambda: setattr(app, "_ed", app.flash.edit_dialog()),
        lambda: shot_win(app._ed, "flash_add"),
        lambda: app._ed.destroy(),
        lambda: app.flash.export_dialog().destroy(),
        lambda: app.flash.col.delete_deck(app.flash.col.deck_by_name("Selftest deck")),
        lambda: app.flash.col.delete_deck(app.flash.col.deck_by_name("Law::Intellectual Property")),
        lambda: app.flash.refresh(),
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
    app.root.after(1500 + len(steps) * 300 + 1500, app.quit)
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


def _crash_log():
    """If the app ever crashes outright, Windows-level details go to crash.log (shown in Activity next time)."""
    try:
        import faulthandler
        path = os.path.join(core.DATA_DIR, "crash.log")
        old = ""
        if os.path.exists(path) and os.path.getsize(path) > 0:
            old = open(path, encoding="utf-8", errors="replace").read()[-3000:]
            os.replace(path, path + ".prev")
        f = open(path, "w", encoding="utf-8")
        faulthandler.enable(f, all_threads=True)
        globals()["_crash_file"] = f
        return old
    except Exception:
        return ""


def main():
    globals()["_last_crash"] = _crash_log()
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
