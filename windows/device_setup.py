"""The sign-in screen the laptop app shows until it's signed in, and the step after it:
continue as one of your existing laptops, or set this one up as a new device with its own
name and backup page."""
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import account
import devices
from notion import NotionClient, NotionError, parse_page_id

TITLE = "Notion Laptop Sync"


class SignInScreen:
    def __init__(self, app, on_done):
        self.app = app
        self.settings = app.settings
        self.on_done = on_done
        self.js = None          # the saved setup (from Google or a code)
        self.method = ""
        self.frame = ttk.Frame(app.root, padding=28)
        self.frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.frame.lift()
        self._step_sign_in()

    # ---- helpers -------------------------------------------------------------------

    def _clear(self):
        for w in self.frame.winfo_children():
            w.destroy()

    def _title(self, text, sub=None):
        ttk.Label(self.frame, text=text, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        if sub:
            ttk.Label(self.frame, text=sub, wraplength=640, justify="left", foreground="#555").pack(anchor="w", pady=(6, 14))

    def _busy(self, text):
        self._clear()
        self._title(text)
        bar = ttk.Progressbar(self.frame, mode="indeterminate", length=320)
        bar.pack(anchor="w", pady=12)
        bar.start(12)

    def _bg(self, work, done):
        def run():
            try:
                r, e = work(), None
            except Exception as ex:
                r, e = None, ex
            self.app.events.put(lambda: done(r, e))
        threading.Thread(target=run, daemon=True).start()

    def close(self):
        self.frame.destroy()

    # ---- step 1: sign in -------------------------------------------------------------

    def _step_sign_in(self, note=None):
        self._clear()
        self._title("Sign in to Notion Sync",
                    "Sign in to use the app. Your phones and laptops signed in to the same account share one "
                    "cloud, and each one keeps its own name and backup page.")
        if note:
            ttk.Label(self.frame, text=note, wraplength=640, foreground="#b00020").pack(anchor="w", pady=(0, 10))
        b = ttk.Button(self.frame, text="Sign in with Google", command=self._google)
        b.pack(anchor="w", ipadx=12, ipady=4)
        if not account.available():
            b.state(["disabled"])
        ttk.Button(self.frame, text="Use a setup code from another device…", command=self._code).pack(anchor="w", pady=8, ipadx=12, ipady=4)
        ttk.Label(self.frame, wraplength=640, foreground="#666", justify="left",
                  text="A setup code: on a phone that's signed in, Settings → Move to a new device; "
                       "on a laptop, Notion tab → Move to a new device.").pack(anchor="w", pady=(4, 0))

    def _google(self):
        self._busy("Finish signing in in your browser…")

        def work():
            account.sign_in(self.settings)
            return account.download(self.settings)

        def done(js, err):
            if err:
                account.sign_out(self.settings)
                msg = str(err)
                if isinstance(err, account.NoDriveAccess):
                    msg = "Almost there. " + msg
                self._step_sign_in(f"Google sign-in didn't work: {msg}")
                return
            self.js, self.method = js or {}, "google"
            self._after_sign_in()
        self._bg(work, done)

    def _code(self):
        code = simpledialog.askstring(TITLE, "Paste the setup code (starts with NSYNC1:):", parent=self.app.root)
        if not code:
            return
        js = account.read_code(code)
        if not js or not js.get("notion_token"):
            messagebox.showerror(TITLE, "That isn't a Notion Sync setup code.", parent=self.app.root)
            return
        self.js, self.method = js, "code"
        self._after_sign_in()

    # ---- step 2: which device is this? ------------------------------------------------

    def _after_sign_in(self):
        token = (self.js or {}).get("notion_token") or self.settings.get("token")
        if not token:
            self._step_connect()
            return
        # This laptop was already backing up: it stays the same device.
        if self.settings.get("token") and self.settings.get("root_page") and \
                (not self.js.get("notion_token") or self.js.get("notion_token") == self.settings.get("token")):
            self._step_name_existing()
            return
        self.settings.set("token", token)
        self._busy("Looking up your devices…")

        def work():
            reg = devices.Registry(self.settings, self.app.db)
            return reg.all(), reg.candidate_parents()
        self._bg(work, lambda r, e: self._step_choose(*(r or ([], []))) if not e else
                 self._step_sign_in(f"Couldn't reach Notion: {e}"))

    def _step_name_existing(self):
        self._clear()
        self._title("Name this laptop",
                    f"This laptop keeps backing up to “{self.settings.get('root_title') or 'Laptop Backup'}”. "
                    "Give it a name so you can tell your devices apart.")
        name = tk.StringVar(value=devices.my_name(self.settings))
        ttk.Entry(self.frame, textvariable=name, width=40, font=("Segoe UI", 11)).pack(anchor="w", pady=6)
        ttk.Button(self.frame, text="Continue", command=lambda: self._finish_existing_install(name.get())).pack(anchor="w", pady=10, ipadx=12)

    def _finish_existing_install(self, name):
        name = name.strip() or devices.default_name()
        self.settings.set("device_name", name[:60])
        self.settings.set("token", (self.js or {}).get("notion_token") or self.settings.get("token"))
        self._finish()

    def _step_choose(self, devs, parents):
        self._clear()
        laptops = [d for d in devs if d.type == "laptop" and d.page]
        self._title("Set up this laptop",
                    "Continue as one of your laptops (e.g. you replaced it), or add this one as a new device "
                    "with its own backup page.")
        choice = tk.StringVar(value="new")
        for d in laptops:
            ttk.Radiobutton(self.frame, variable=choice, value=d.id,
                            text=f"Continue as “{d.name}” — backs up to “{d.page_title or 'its page'}” "
                                 f"(last seen {d.seen_ago()})").pack(anchor="w", pady=2)
        ttk.Radiobutton(self.frame, variable=choice, value="new", text="Set up as a new laptop").pack(anchor="w", pady=(8, 2))

        box = ttk.Frame(self.frame, padding=(24, 4, 0, 0))
        box.pack(anchor="w", fill="x")
        ttk.Label(box, text="Name").grid(row=0, column=0, sticky="w")
        name = tk.StringVar(value=devices.default_name())
        ttk.Entry(box, textvariable=name, width=40).grid(row=0, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(box, text="Put its backup page inside").grid(row=1, column=0, sticky="w")
        labels = [t for _, t in parents]
        where = tk.StringVar(value=labels[0] if labels else "")
        combo = ttk.Combobox(box, textvariable=where, values=labels, width=38, state="readonly" if labels else "disabled")
        combo.grid(row=1, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(box, text="…or paste a page link").grid(row=2, column=0, sticky="w")
        link = tk.StringVar()
        ttk.Entry(box, textvariable=link, width=40).grid(row=2, column=1, sticky="w", padx=8, pady=3)
        ttk.Label(box, wraplength=560, foreground="#666", justify="left",
                  text="For a page link: in Notion, make a page (e.g. “Backups”), then ••• → Connections → add your "
                       "integration, and Share → Copy link.").grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        def go():
            if choice.get() != "new":
                dev = next(d for d in laptops if d.id == choice.get())
                if not messagebox.askyesno(TITLE, f"Continue as “{dev.name}” on this computer?\n\n"
                                                  "Only do this if that laptop isn't in use any more — two laptops "
                                                  "acting as one would mix their backups.", parent=self.app.root):
                    return
                account.apply_device(self.settings, self.js, dev)
                self._finish()
                return
            nm = name.get().strip() or devices.default_name()
            pid = parse_page_id(link.get()) if link.get().strip() else None
            if not pid:
                sel = combo.current() if labels else -1
                if sel < 0:
                    messagebox.showinfo(TITLE, "Choose where to put this laptop's backup page, or paste a page link.",
                                        parent=self.app.root)
                    return
                pid = parents[sel][0]
            self._create_new(nm, pid)
        ttk.Button(self.frame, text="Continue", command=go).pack(anchor="w", pady=14, ipadx=12)

    def _create_new(self, name, parent):
        self._busy(f"Making “{name} Backup” in Notion…")

        def work():
            reg = devices.Registry(self.settings, self.app.db)
            title = f"{name} Backup"
            pid = reg.create_page(parent, title)
            return pid, title

        def done(r, e):
            if e:
                msg = str(e)
                if isinstance(e, NotionError) and e.status in (400, 403, 404):
                    msg = "Notion wouldn't let the app make a page there. Open that page in Notion → ••• → " \
                          "Connections → add your integration, then try again."
                messagebox.showerror(TITLE, msg, parent=self.app.root)
                self._after_sign_in()
                return
            account.new_device(self.settings, self.js, name, r[0], r[1])
            self._finish()
        self._bg(work, done)

    # ---- first device ever: connect Notion -----------------------------------------------

    def _step_connect(self):
        self._clear()
        self._title("Connect Notion",
                    "This is your first device. Paste your Notion integration secret and the link to the page "
                    "this laptop should back up to.")
        ttk.Label(self.frame, text="Integration secret (notion.so/profile/integrations)").pack(anchor="w")
        tok = tk.StringVar()
        ttk.Entry(self.frame, textvariable=tok, show="•", width=60).pack(anchor="w", pady=4)
        ttk.Label(self.frame, text="Backup page link (open it → ••• → Connections → add your integration → Share → Copy link)").pack(anchor="w", pady=(8, 0))
        link = tk.StringVar()
        ttk.Entry(self.frame, textvariable=link, width=60).pack(anchor="w", pady=4)
        ttk.Label(self.frame, text="Name this laptop").pack(anchor="w", pady=(8, 0))
        name = tk.StringVar(value=devices.default_name())
        ttk.Entry(self.frame, textvariable=name, width=40).pack(anchor="w", pady=4)
        msg = ttk.Label(self.frame, text="", foreground="#b00020")
        msg.pack(anchor="w")

        def go():
            t, pid = tok.get().strip(), parse_page_id(link.get())
            if not t or not pid:
                msg.config(text="Paste both the secret and the page link.")
                return
            msg.config(text="Checking…")

            def work():
                return NotionClient(t).page_title(pid)

            def done(title, e):
                if e:
                    msg.config(text=f"Couldn't open that page: {e}")
                    return
                self.settings.set("token", t)
                self.js = dict(self.js or {}, notion_token=t)
                account.new_device(self.settings, self.js, name.get().strip() or devices.default_name(), pid, title)
                self._finish()
            self._bg(work, done)
        ttk.Button(self.frame, text="Continue", command=go).pack(anchor="w", pady=10, ipadx=12)

    # ---- done ---------------------------------------------------------------------------

    def _finish(self):
        self.settings.set("signed_in", self.method or "code")
        self._busy("Finishing…")

        def work():
            reg = devices.Registry(self.settings, self.app.db)
            reg.register_me()
            reg.all()
            if self.settings.get("google_refresh"):
                try:
                    account.upload(self.settings)
                except Exception:
                    pass
        self._bg(work, lambda r, e: self.on_done())
