"""The Flashcards page: decks, studying, adding/editing, browsing, import and export."""
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import flashcards as F
import ui

BLUE, RED, GREEN = "#1a73e8", "#d93025", "#188038"
SOFT = "#f0f4f9"
GRADES = [(F.AGAIN, "Again", "#d93025", "#fce8e6"), (F.HARD, "Hard", "#b06000", "#fef7e0"),
          (F.GOOD, "Good", "#188038", "#e6f4ea"), (F.EASY, "Easy", "#1a73e8", "#e8f0fe")]


class FlashTab:
    def __init__(self, app, frame):
        self.app = app
        self.frame = frame
        self.col = app.flash_col
        self.mode = "home"
        self.deck_id = None
        self.card_id = None
        self.revealed = False
        self.shown_at = 0
        self.undo_stack = []
        self._imgs = []
        self._wait_job = None
        frame.configure(padding=(24, 16, 24, 12))
        self.home = ttk.Frame(frame)
        self.study = ttk.Frame(frame)
        self._build_home(self.home)
        self._build_study(self.study)
        self.home.pack(fill="both", expand=True)
        app.root.bind("<KeyPress>", self._key, add="+")

    # ---- home ---------------------------------------------------------------------------------

    def _build_home(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Flashcards", style="Title.TLabel").pack(side="left")
        ttk.Button(top, text="Export…", command=self.export_dialog).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Import…", command=self.import_file).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="New deck", command=self.new_deck).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="Browse", command=lambda: self.browse()).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="+  Add cards", style="Accent.TButton", command=lambda: self.edit_dialog()).pack(
            side="right", padx=(6, 0))

        srow = ttk.Frame(f)
        srow.pack(fill="x", pady=(8, 0))
        self.sync_lbl = ttk.Label(srow, text="", foreground=ui.MUTED)
        self.sync_lbl.pack(side="left")
        ttk.Button(srow, text="Sync now", command=self.sync_now).pack(side="left", padx=10)
        stats = ttk.Frame(f)
        stats.pack(fill="x", pady=(10, 6))
        self.stat = {}
        for key, title in (("today", "Studied today"), ("streak", "Streak"), ("correct", "Remembered"),
                           ("week", "Due in the next 7 days")):
            c = tk.Frame(stats, bg=SOFT, padx=ui.px(14), pady=ui.px(10))
            c.pack(side="left", padx=(0, 10), fill="y")
            tk.Label(c, text=title, bg=SOFT, fg=ui.MUTED, font=(ui.FONT, 9)).pack(anchor="w")
            big = tk.Label(c, text="–", bg=SOFT, fg=ui.TEXT, font=(ui.FONT, 16, "bold"))
            big.pack(anchor="w")
            small = tk.Label(c, text="", bg=SOFT, fg=ui.MUTED, font=(ui.FONT, 9))
            small.pack(anchor="w")
            self.stat[key] = (big, small)

        self.heat = tk.Canvas(f, height=ui.px(7 * 13 + 4), bg=ui.CARD, highlightthickness=0)
        self.heat.pack(fill="x", pady=(6, 4))

        self.note = ttk.Label(f, text="Tip: while studying press Space to show the answer, then 1 Again · 2 Hard · "
                                      "3 Good · 4 Easy. Ctrl+Z undoes, E edits the card.", foreground=ui.MUTED)
        self.note.pack(side="bottom", anchor="w", pady=(8, 0))
        btns = ttk.Frame(f)
        btns.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(btns, text="Study", style="Accent.TButton", command=self.start_study).pack(side="left")
        for text, cmd in (("Add cards", lambda: self.edit_dialog(deck_id=self._sel())),
                          ("Browse", lambda: self.browse(self._sel())), ("Options", self.options_dialog),
                          ("Rename", self.rename_deck), ("Delete", self.delete_deck)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=(6, 0))
        self.empty = ttk.Label(f, text="No decks yet. Press “+ Add cards” to make your first card, or Import… an "
                                       "Anki deck (.apkg) or a text file.", foreground=ui.MUTED)
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True, pady=(8, 4))
        self.deck_box = box
        cols = ("new", "learn", "due", "total")
        t = ttk.Treeview(box, columns=cols, show="tree headings", height=4, selectmode="browse")
        t.heading("#0", text="Deck")
        t.column("#0", width=ui.px(340), stretch=True)
        for c, txt, w in (("new", "New", 80), ("learn", "Learning", 90), ("due", "Due", 80), ("total", "Cards", 80)):
            t.heading(c, text=txt)
            t.column(c, width=ui.px(w), anchor="e", stretch=False)
        t.tag_configure("ready", font=(ui.FONT, 10, "bold"))
        sc = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sc.set)
        t.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        t.bind("<Double-1>", lambda e: self.start_study())
        t.bind("<Return>", lambda e: self.start_study())
        t.bind("<Button-3>", self._deck_menu)
        self.tree = t

    def _sel(self, need=False):
        s = self.tree.selection()
        if s:
            return int(s[0])
        if need:
            if self.col.decks():
                messagebox.showinfo("Flashcards", "Click a deck in the list first.")
            else:
                messagebox.showinfo("Flashcards", "You don't have any decks yet.\n\nPress “+ Add cards” to make your "
                                                  "first card (a deck is made for it), or Import… an Anki deck.")
        return None

    def _deck_menu(self, e):
        row = self.tree.identify_row(e.y)
        if not row:
            return
        self.tree.selection_set(row)
        m = tk.Menu(self.app.root, tearoff=0)
        m.add_command(label="Study", command=self.start_study)
        m.add_command(label="Add cards", command=lambda: self.edit_dialog(deck_id=int(row)))
        m.add_command(label="Browse", command=lambda: self.browse(int(row)))
        m.add_command(label="Options", command=self.options_dialog)
        m.add_separator()
        m.add_command(label="Rename", command=self.rename_deck)
        m.add_command(label="Export…", command=lambda: self.export_dialog(int(row)))
        m.add_command(label="Delete", command=self.delete_deck)
        m.tk_popup(e.x_root, e.y_root)

    def refresh(self):
        sel = self._sel()
        self.tree.delete(*self.tree.get_children())
        decks = self.col.decks()
        for d in decks:
            n, l, r = self.col.counts(d["id"])
            name = d["name"].replace("::", "  ›  ")
            self.tree.insert("", "end", iid=str(d["id"]), text="  " + name,
                             values=(n or "–", l or "–", r or "–", self.col.total(d["id"])),
                             tags=("ready",) if (n or l or r) else ())
        if decks:
            self.empty.pack_forget()
            pick = str(sel) if sel and self.tree.exists(str(sel)) else str(decks[0]["id"])
            self.tree.selection_set(pick)
            self.tree.focus(pick)
        elif not self.empty.winfo_ismapped():
            self.empty.pack(anchor="w", pady=6, before=self.deck_box)
        t = self.col.today()
        b, s = self.stat["today"]
        b.config(text=f"{t['reviews']}")
        s.config(text=f"{int(t['seconds'] // 60)} min" if t["reviews"] else "cards")
        b, s = self.stat["streak"]
        st = self.col.streak()
        b.config(text=f"{st} day" + ("" if st == 1 else "s"))
        s.config(text="keep it going!" if st else "study today to start")
        b, s = self.stat["correct"]
        b.config(text=f"{t['correct_pct']:.0f}%" if t["correct_pct"] is not None else "–")
        s.config(text="of reviews today")
        b, s = self.stat["week"]
        fc = self.col.forecast(7)
        b.config(text=f"{sum(fc)}")
        s.config(text="tomorrow: " + str(fc[1] if len(fc) > 1 else 0))
        self._draw_heat()
        self._sync_label()

    def _draw_heat(self):
        c = self.heat
        c.delete("all")
        weeks = 26
        per = self.col.per_day(weeks * 7)
        sz, gap = ui.px(11), ui.px(2)
        top = max(per.values()) if per else 0
        today_wd = (time.localtime(F.day_start()).tm_wday + 1) % 7      # Sunday = 0
        x0 = ui.px(2)
        for i in range(weeks * 7):
            col_i = weeks - 1 - (i + (6 - today_wd)) // 7
            row = (today_wd - i) % 7
            if col_i < 0:
                continue
            n = per.get(i, 0)
            if not n:
                fill = "#ebedf0"
            else:
                k = n / top if top else 0
                fill = "#9be9a8" if k < 0.25 else "#40c463" if k < 0.5 else "#30a14e" if k < 0.75 else "#216e39"
            x, y = x0 + col_i * (sz + gap), ui.px(2) + row * (sz + gap)
            c.create_rectangle(x, y, x + sz, y + sz, fill=fill, outline="")
        lx = x0 + weeks * (sz + gap) + ui.px(12)
        c.create_text(lx, ui.px(4), anchor="nw", fill=ui.MUTED, font=(ui.FONT, 9),
                      text=f"Your last {weeks} weeks\nDarker = more cards studied that day")

    def on_shown(self):
        if self.mode == "home":
            self.refresh()
        self._sync_label()
        if self.app.flash_sync.needed(120) and self.app.settings.get("token"):
            self.sync_now()

    def sync_now(self):
        if not self.app.settings.get("token"):
            messagebox.showinfo("Flashcards", "Connect Notion first (Account & clipboard) to sync with your phone.")
            return
        self.app.flash_sync_now()
        self._sync_label()

    def _sync_label(self):
        try:
            self.sync_lbl.config(text=self.app.flash_sync.status())
        except Exception:
            pass

    def after_sync(self):
        self._sync_label()
        if self.mode == "home":
            self.refresh()
        elif self.mode == "study" and not self.revealed:
            self._counts()

    # ---- decks --------------------------------------------------------------------------------

    def new_deck(self):
        name = simpledialog.askstring("New deck", "Name of the new deck (use :: for a sub-deck, e.g. Law::IP):",
                                      parent=self.app.root)
        if name and name.strip():
            self.col.deck_by_name(name.strip())
            self.refresh()

    def rename_deck(self):
        did = self._sel(need=True)
        if not did:
            return
        d = self.col.deck(did)
        name = simpledialog.askstring("Rename deck", "New name:", initialvalue=d["name"], parent=self.app.root)
        if name and name.strip() and name.strip() != d["name"]:
            if self.col.deck_by_name(name.strip(), create=False):
                messagebox.showerror("Rename deck", "There's already a deck with that name.")
                return
            self.col.rename_deck(did, name)
            self.refresh()

    def delete_deck(self):
        did = self._sel(need=True)
        if not did:
            return
        d = self.col.deck(did)
        n = self.col.total(did)
        if messagebox.askyesno("Delete deck", f"Delete “{d['name']}” and its {n} card(s)? This can't be undone.\n\n"
                                              "(Tip: Export… it first if you might want it back.)", icon="warning"):
            self.col.delete_deck(did)
            self.refresh()

    def options_dialog(self):
        did = self._sel(need=True)
        if not did:
            return
        d = self.col.deck(did)
        w = self._dialog(f"Options — {d['name']}")
        body = ttk.Frame(w, padding=20)
        body.pack(fill="both", expand=True)
        newv = tk.IntVar(value=d["new_per_day"])
        revv = tk.IntVar(value=d["reviews_per_day"])
        retv = tk.DoubleVar(value=round(d["retention"] * 100))
        ttk.Label(body, text="New cards per day").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Spinbox(body, from_=0, to=9999, textvariable=newv, width=8).grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(body, text="Maximum reviews per day").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Spinbox(body, from_=0, to=99999, textvariable=revv, width=8).grid(row=1, column=1, sticky="w", padx=10)
        ttk.Label(body, text="How much to remember").grid(row=2, column=0, sticky="w", pady=6)
        lbl = ttk.Label(body, text="", width=6)
        ttk.Scale(body, from_=80, to=97, variable=retv, length=ui.px(200),
                  command=lambda v: lbl.config(text=f"{float(v):.0f}%")).grid(row=2, column=1, sticky="w", padx=10)
        lbl.grid(row=2, column=2, sticky="w")
        lbl.config(text=f"{retv.get():.0f}%")
        ttk.Label(body, text="90% is a good balance. Higher means you'll remember more, but see cards more often.",
                  foreground=ui.MUTED, wraplength=ui.px(380), justify="left").grid(row=3, column=0, columnspan=3,
                                                                                    sticky="w", pady=(4, 12))

        def save():
            try:
                self.col.set_deck_options(did, newv.get(), revv.get(), retv.get() / 100)
            except (tk.TclError, ValueError):
                messagebox.showerror("Options", "Please type whole numbers.", parent=w)
                return
            w.destroy()
            self.refresh()
        row = ttk.Frame(body)
        row.grid(row=4, column=0, columnspan=3, sticky="e")
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Save", style="Accent.TButton", command=save).pack(side="right")
        return w

    # ---- studying -------------------------------------------------------------------------------

    def _build_study(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Button(top, text="←  Decks", command=self.show_home).pack(side="left")
        self.deck_lbl = ttk.Label(top, text="", font=(ui.FONT, 14))
        self.deck_lbl.pack(side="left", padx=14)
        cnt = ttk.Frame(top)
        cnt.pack(side="right")
        self.c_new = tk.Label(cnt, text="0", fg=BLUE, bg=ui.CARD, font=(ui.FONT, 12, "bold"))
        self.c_lrn = tk.Label(cnt, text="0", fg=RED, bg=ui.CARD, font=(ui.FONT, 12, "bold"))
        self.c_rev = tk.Label(cnt, text="0", fg=GREEN, bg=ui.CARD, font=(ui.FONT, 12, "bold"))
        for lbl, cap in ((self.c_new, "new"), (self.c_lrn, "learning"), (self.c_rev, "due")):
            lbl.pack(side="left")
            tk.Label(cnt, text=" " + cap + "    ", fg=ui.MUTED, bg=ui.CARD, font=(ui.FONT, 9)).pack(side="left")
        more = ttk.Menubutton(top, text="More")
        mm = tk.Menu(more, tearoff=0)
        mm.add_command(label="Edit this card   (E)", command=self.edit_current)
        mm.add_command(label="Undo last answer   (Ctrl+Z)", command=self.undo)
        mm.add_command(label="Suspend this card   (S)", command=self.suspend_current)
        mm.add_command(label="Delete this card", command=self.delete_current)
        more["menu"] = mm
        more.pack(side="right", padx=10)

        self.bar = ttk.Frame(f)
        self.bar.pack(side="bottom", fill="x")          # first, so a small screen never hides the buttons
        card = tk.Frame(f, bg=ui.CARD, highlightthickness=1, highlightbackground="#dadce0")
        card.pack(fill="both", expand=True, pady=(14, 10))
        self.txt = tk.Text(card, wrap="word", bd=0, highlightthickness=0, bg=ui.CARD, fg=ui.TEXT, height=4,
                           font=(ui.FONT, 20), padx=ui.px(30), pady=ui.px(30), cursor="arrow", spacing3=4)
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_configure("c", justify="center")
        self.txt.tag_configure("back", justify="center", foreground="#202124")
        self.txt.tag_configure("muted", justify="center", foreground=ui.MUTED, font=(ui.FONT, 12))
        self.txt.tag_configure("rule", justify="center", foreground="#dadce0", font=(ui.FONT, 10))
        self.txt.configure(state="disabled")

        self.show_btn = tk.Button(self.bar, text="Show answer      (Space)", font=(ui.FONT, 12), bd=0, relief="flat",
                                  bg=ui.ACCENT, fg="white", activebackground="#0842a0", activeforeground="white",
                                  cursor="hand2", padx=20, pady=12, command=self.reveal)
        self.grade_row = ttk.Frame(self.bar)
        self.grade_btns = {}
        for i, (g, name, fg, bg) in enumerate(GRADES):
            b = tk.Button(self.grade_row, text=name, font=(ui.FONT, 11, "bold"), bd=0, relief="flat", bg=bg, fg=fg,
                          activebackground=bg, activeforeground=fg, cursor="hand2", padx=10, pady=10,
                          command=lambda g=g: self.grade(g))
            b.grid(row=0, column=i, sticky="ew", padx=4)
            self.grade_row.columnconfigure(i, weight=1, uniform="g")
            self.grade_btns[g] = b
        self.done_row = ttk.Frame(self.bar)
        ttk.Button(self.done_row, text="Back to decks", style="Accent.TButton", command=self.show_home).pack()

    def start_study(self, deck_id=None):
        did = deck_id or self._sel(need=True)
        if not did:
            return
        self.deck_id = did
        self.mode = "study"
        self.undo_stack.clear()
        self.home.pack_forget()
        self.study.pack(fill="both", expand=True)
        self.deck_lbl.config(text=self.col.deck(did)["name"].replace("::", " › "))
        self.next_card()

    def show_home(self):
        self._cancel_wait()
        self.mode = "home"
        self.study.pack_forget()
        self.home.pack(fill="both", expand=True)
        self.refresh()

    def _cancel_wait(self):
        if self._wait_job:
            try:
                self.app.root.after_cancel(self._wait_job)
            except Exception:
                pass
            self._wait_job = None

    def _counts(self):
        n, l, r = self.col.counts(self.deck_id)
        self.c_new.config(text=str(n))
        self.c_lrn.config(text=str(l))
        self.c_rev.config(text=str(r))

    def _bar(self, which):
        for w in (self.show_btn, self.grade_row, self.done_row):
            w.pack_forget()
        if which == "show":
            self.show_btn.pack(fill="x")
        elif which == "grade":
            self.grade_row.pack(fill="x")
        elif which == "done":
            self.done_row.pack()

    def next_card(self):
        self._cancel_wait()
        self._counts()
        cid = self.col.next_card(self.deck_id)
        self.card_id = cid
        self.revealed = False
        if not cid:
            self._finished()
            return
        c = self.col.card(cid)
        self._write(self._side(c, front=True), None)
        self.shown_at = time.time()
        self._bar("show")

    def _side(self, c, front):
        q, a = (c["front"], c["back"]) if c["ord"] == 0 else (c["back"], c["front"])
        return q if front else a

    def _finished(self):
        self._write("", None)
        t = self.txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        # anything still learning later today?
        with self.col.lock:
            nxt = self.col.conn.execute("SELECT MIN(due) FROM cards WHERE deck_id=? AND state IN (1,3) "
                                        "AND suspended=0", (self.deck_id,)).fetchone()[0]
        t.insert("end", "\n🎉\n", "c")
        t.insert("end", "You're done with this deck for now.\n", "c")
        if nxt and nxt - time.time() < 6 * 3600:
            wait = max(0, nxt - time.time())
            t.insert("end", f"\nA card you're learning comes back in {F.human_interval(wait)}.\n"
                            "Stay here and it will show up by itself.", "muted")
            self._wait_job = self.app.root.after(int(min(wait, 3600) * 1000) + 500, self.next_card)
        else:
            fc = self.col.forecast(2)
            t.insert("end", f"\nTomorrow: {fc[1] if len(fc) > 1 else 0} card(s) to review.", "muted")
        t.configure(state="disabled")
        self._bar("done")

    def _write(self, question, answer):
        t = self.txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        self._imgs = []
        t.insert("end", "\n")
        self._insert_rich(question, "c")
        if answer is not None:
            t.insert("end", "\n\n" + "─" * 30 + "\n\n", "rule")
            self._insert_rich(answer, "back")
        t.configure(state="disabled")
        t.yview_moveto(0)

    def _insert_rich(self, text, tag):
        t = self.txt
        pos = 0
        for m in F.IMG_RE.finditer(text or ""):
            t.insert("end", text[pos:m.start()], tag)
            ph = self._photo(m.group(1))
            if ph:
                t.insert("end", "\n", tag)
                t.image_create("end", image=ph, padx=4, pady=6)
                t.tag_add(tag, "end-2c", "end-1c")
                t.insert("end", "\n", tag)
            else:
                t.insert("end", f"[picture: {m.group(1)}]", "muted")
            pos = m.end()
        t.insert("end", (text or "")[pos:], tag)

    def _photo(self, name):
        p = F.Collection.media_path(name)
        if not os.path.isfile(p):
            return None
        try:
            from PIL import Image, ImageTk
            im = Image.open(p)
            im.thumbnail((ui.px(560), ui.px(320)))
            ph = ImageTk.PhotoImage(im)
            self._imgs.append(ph)
            return ph
        except Exception:
            return None

    def reveal(self):
        if self.mode != "study" or not self.card_id or self.revealed:
            return
        c = self.col.card(self.card_id)
        if not c:
            return self.next_card()
        self.revealed = True
        self._write(self._side(c, True), self._side(c, False))
        pv = self.col.preview(self.card_id)
        for g, name, _, _ in GRADES:
            self.grade_btns[g].config(text=f"{name}\n{F.human_interval(pv[g])}")
        self._bar("grade")

    def grade(self, g):
        if self.mode != "study" or not self.card_id or not self.revealed:
            return
        took = min(60.0, time.time() - self.shown_at)
        before = self.col.answer(self.card_id, g, took)
        self.undo_stack.append(before)
        del self.undo_stack[:-20]
        self.next_card()

    def undo(self):
        if not self.undo_stack:
            return
        before = self.undo_stack.pop()
        self.col.undo(before)
        self.card_id = before["id"]
        self._cancel_wait()
        self._counts()
        c = self.col.card(self.card_id)
        self.revealed = False
        self._write(self._side(c, True), None)
        self.shown_at = time.time()
        self._bar("show")

    def edit_current(self):
        if self.card_id:
            c = self.col.card(self.card_id)
            if c:
                self.edit_dialog(note_id=c["note_id"], after=self._redraw_current)

    def _redraw_current(self):
        if self.mode == "study" and self.card_id:
            c = self.col.card(self.card_id)
            if not c:
                return self.next_card()
            self._write(self._side(c, True), self._side(c, False) if self.revealed else None)

    def suspend_current(self):
        if self.card_id:
            self.col.set_suspended(self.card_id, True)
            self.next_card()

    def delete_current(self):
        if not self.card_id:
            return
        c = self.col.card(self.card_id)
        if c and messagebox.askyesno("Delete card", "Delete this card (and its reverse card, if any)?"):
            self.col.delete_note(c["note_id"])
            self.undo_stack = [u for u in self.undo_stack if u.get("note_id") != c["note_id"]]
            self.next_card()

    def _key(self, e):
        try:
            if self.mode != "study" or self.app.nb.index("current") != self.app.TAB_FLASH:
                return
            w = self.app.root.focus_get()
            if w is None or w.winfo_toplevel() is not self.app.root:
                return
            if w.winfo_class() in ("Entry", "TEntry", "Text", "TCombobox", "TSpinbox", "Spinbox") and w is not self.txt:
                return
        except (KeyError, tk.TclError):
            return
        k = e.keysym.lower()
        ctrl = bool(e.state & 0x4)
        if ctrl and k == "z":
            self.undo()
        elif ctrl:
            return
        elif k in ("space", "return"):
            if not self.revealed:
                self.reveal()
            elif k == "space":
                self.grade(F.GOOD)
        elif k in ("1", "2", "3", "4", "kp_1", "kp_2", "kp_3", "kp_4") and self.revealed:
            self.grade(int(k[-1]))
        elif k == "e":
            self.edit_current()
        elif k == "s":
            self.suspend_current()
        elif k == "escape":
            self.show_home()
        else:
            return
        return "break"

    # ---- add / edit ----------------------------------------------------------------------------

    def _dialog(self, title):
        w = tk.Toplevel(self.app.root)
        w.title(title)
        w.transient(self.app.root)
        w.configure(bg=ui.BG)
        try:
            import winutil
            w.iconbitmap(winutil.resource("icon.ico"))
        except Exception:
            pass
        w.bind("<Escape>", lambda e: w.destroy())
        return w

    def edit_dialog(self, note_id=None, deck_id=None, after=None):
        note = self.col.note(note_id) if note_id else None
        w = self._dialog("Edit card" if note else "Add cards")
        w.geometry(f"{ui.px(620)}x{ui.px(560)}")
        body = ttk.Frame(w, padding=18)
        body.pack(fill="both", expand=True)
        decks = [d["name"] for d in self.col.decks()]
        cur = None
        if note:
            cur = (self.col.deck(note["deck_id"]) or {}).get("name")
        elif deck_id:
            cur = (self.col.deck(deck_id) or {}).get("name")
        if not cur and self._sel():
            cur = (self.col.deck(self._sel()) or {}).get("name")
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Label(row, text="Deck").pack(side="left")
        deck_var = tk.StringVar(value=cur or (decks[0] if decks else "My first deck"))
        ttk.Combobox(row, textvariable=deck_var, values=decks, width=40).pack(side="left", padx=10)
        ttk.Label(body, text="Front (the question)").pack(anchor="w", pady=(12, 2))
        front = tk.Text(body, height=5, wrap="word", font=(ui.FONT, 12), undo=True, bd=1, relief="solid",
                        highlightthickness=0, padx=8, pady=6)
        front.pack(fill="both", expand=True)
        ttk.Label(body, text="Back (the answer)").pack(anchor="w", pady=(10, 2))
        back = tk.Text(body, height=5, wrap="word", font=(ui.FONT, 12), undo=True, bd=1, relief="solid",
                       highlightthickness=0, padx=8, pady=6)
        back.pack(fill="both", expand=True)
        last_text = [front]
        front.bind("<FocusIn>", lambda e: last_text.__setitem__(0, front))
        back.bind("<FocusIn>", lambda e: last_text.__setitem__(0, back))
        row2 = ttk.Frame(body)
        row2.pack(fill="x", pady=(10, 0))
        ttk.Label(row2, text="Tags").pack(side="left")
        tags = tk.StringVar(value=note["tags"] if note else "")
        ttk.Entry(row2, textvariable=tags, width=30).pack(side="left", padx=10)

        def add_picture():
            p = filedialog.askopenfilename(parent=w, title="Choose a picture",
                                           filetypes=[("Pictures", "*.png *.jpg *.jpeg *.gif *.webp *.bmp")])
            if not p:
                return
            with open(p, "rb") as fh:
                name = F.Collection._store_media(os.path.basename(p), fh.read())
            last_text[0].insert("insert", f"[img:{name}]")
        ttk.Button(row2, text="Add picture…", command=add_picture).pack(side="right")
        rev = tk.BooleanVar(value=bool(note["reverse"]) if note else False)
        ttk.Checkbutton(body, text="Also make a reverse card (shows the back, asks for the front)",
                        variable=rev).pack(anchor="w", pady=(8, 0))
        status = ttk.Label(body, text="", foreground=GREEN)
        status.pack(anchor="w", pady=(6, 0))
        if note:
            front.insert("1.0", note["front"])
            back.insert("1.0", note["back"])

        def save(_=None):
            f_, b_ = front.get("1.0", "end-1c").strip(), back.get("1.0", "end-1c").strip()
            if not f_:
                status.config(text="Type something on the front first.", foreground=RED)
                front.focus_set()
                return "break"
            did = self.col.deck_by_name(deck_var.get())
            if note:
                self.col.update_note(note["id"], did, f_, b_, tags.get(), rev.get())
                w.destroy()
                if after:
                    after()
            else:
                self.col.add_note(did, f_, b_, tags.get(), rev.get())
                front.delete("1.0", "end")
                back.delete("1.0", "end")
                front.focus_set()
                status.config(text=f"✓ Added to {deck_var.get()} ({self.col.total(did)} cards). Add another, or close.",
                              foreground=GREEN)
                if deck_var.get() not in decks:
                    decks.append(deck_var.get())
            if self.mode == "home":
                self.refresh()
            return "break"
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Close", command=w.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Save  (Ctrl+Enter)" if note else "Add  (Ctrl+Enter)", style="Accent.TButton",
                   command=save).pack(side="right")
        for wd in (w, front, back):
            wd.bind("<Control-Return>", save)
        front.bind("<Tab>", lambda e: (back.focus_set(), "break")[1])
        back.bind("<Shift-Tab>", lambda e: (front.focus_set(), "break")[1])
        front.focus_set()
        return w

    # ---- browse --------------------------------------------------------------------------------

    def browse(self, deck_id=None):
        w = self._dialog("Browse cards")
        w.geometry(f"{ui.px(980)}x{ui.px(600)}")
        body = ttk.Frame(w, padding=14)
        body.pack(fill="both", expand=True)
        top = ttk.Frame(body)
        top.pack(fill="x")
        q = tk.StringVar()
        ent = ttk.Entry(top, textvariable=q, width=40)
        ent.pack(side="left")
        decks = self.col.decks()
        names = ["All decks"] + [d["name"] for d in decks]
        dv = tk.StringVar(value=(self.col.deck(deck_id) or {}).get("name", "All decks") if deck_id else "All decks")
        ttk.Combobox(top, textvariable=dv, values=names, state="readonly", width=30).pack(side="left", padx=8)
        count = ttk.Label(top, text="", foreground=ui.MUTED)
        count.pack(side="right")
        ttk.Label(body, text="Search words in the cards, or tag:name for a tag.", foreground=ui.MUTED).pack(
            anchor="w", pady=(4, 6))
        box = ttk.Frame(body)
        box.pack(fill="both", expand=True)
        cols = ("back", "deck", "due", "ivl", "tags")
        t = ttk.Treeview(box, columns=cols, show="tree headings")
        t.heading("#0", text="Front")
        t.column("#0", width=ui.px(280))
        for c, txt, wd in (("back", "Back", 260), ("deck", "Deck", 140), ("due", "Next", 90), ("ivl", "Interval", 70),
                           ("tags", "Tags", 110)):
            t.heading(c, text=txt)
            t.column(c, width=ui.px(wd), stretch=c in ("back",))
        t.tag_configure("susp", foreground="#999")
        sc = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sc.set)
        t.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")
        job = [None]

        def one_line(s, n=80):
            s = F.IMG_RE.sub("🖼", (s or "").replace("\n", " "))
            return s if len(s) <= n else s[:n - 1] + "…"

        def fill():
            job[0] = None
            did = next((d["id"] for d in decks if d["name"] == dv.get()), None)
            rows = self.col.search(q.get(), did)
            t.delete(*t.get_children())
            now = time.time()
            for r in rows:
                if r["state"] in (None, F.NEW):
                    nxt = "new"
                else:
                    nxt = "now" if (r["due"] or 0) <= now else time.strftime("%d %b", time.localtime(r["due"]))
                t.insert("", "end", iid=str(r["id"]), text=one_line(r["front"]),
                         values=(one_line(r["back"]), r["deck"], nxt, f"{r['ivl']}d" if r["ivl"] else "",
                                 r["tags"] or ""), tags=("susp",) if r["suspended"] else ())
            count.config(text=f"{len(rows)} card(s)" + (" (first 2000)" if len(rows) >= 2000 else ""))

        def later(*_):
            if job[0]:
                w.after_cancel(job[0])
            job[0] = w.after(250, fill)
        q.trace_add("write", later)
        dv.trace_add("write", later)

        def chosen():
            return [int(i) for i in t.selection()]

        def edit(_=None):
            s = chosen()
            if s:
                self.edit_dialog(note_id=s[0], after=fill)

        def delete():
            s = chosen()
            if s and messagebox.askyesno("Delete", f"Delete {len(s)} card(s)? This can't be undone.", parent=w):
                for nid in s:
                    self.col.delete_note(nid)
                fill()

        def move():
            s = chosen()
            if not s:
                return
            name = simpledialog.askstring("Move to deck", "Deck name (new or existing):", parent=w)
            if name and name.strip():
                self.col.move_notes(s, self.col.deck_by_name(name.strip()))
                fill()

        def reset():
            s = chosen()
            if s and messagebox.askyesno("Forget", f"Make {len(s)} card(s) new again (forget their progress)?",
                                         parent=w):
                self.col.reset_cards(s)
                fill()

        def suspend():
            s = chosen()
            for nid in s:
                with self.col.lock:
                    cards = self.col.conn.execute("SELECT id, suspended FROM cards WHERE note_id=?", (nid,)).fetchall()
                for cid, sus in cards:
                    self.col.set_suspended(cid, not sus)
            fill()
        t.bind("<Double-1>", edit)
        t.bind("<Delete>", lambda e: delete())
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(8, 0))
        for text, cmd in (("Edit", edit), ("Move to deck…", move), ("Suspend / unsuspend", suspend),
                          ("Forget progress", reset), ("Delete", delete)):
            ttk.Button(btns, text=text, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Close", command=w.destroy).pack(side="right")
        w.bind("<Destroy>", lambda e: self.refresh() if e.widget is w and self.mode == "home" else None)
        fill()
        ent.focus_set()
        return w

    # ---- import / export ------------------------------------------------------------------------

    def import_file(self, path=None):
        path = path or filedialog.askopenfilename(
            parent=self.app.root, title="Import flashcards",
            filetypes=[("Anki decks and text", "*.apkg *.colpkg *.txt *.csv *.tsv"), ("Anki deck", "*.apkg *.colpkg"),
                       ("Text", "*.txt *.csv *.tsv"), ("All files", "*.*")])
        if not path:
            return
        prog = self._dialog("Importing…")
        lbl = ttk.Label(prog, text="Reading the file…", padding=24)
        lbl.pack()
        bar = ttk.Progressbar(prog, mode="indeterminate", length=ui.px(280))
        bar.pack(padx=24, pady=(0, 20))
        bar.start(12)

        def say(m):
            self.app.events.put(lambda: lbl.winfo_exists() and lbl.config(text=m))

        def work():
            try:
                n, decks = self.col.import_file(path, progress=say)
                err = None
            except Exception as e:
                n, decks, err = 0, [], e

            def done():
                if prog.winfo_exists():
                    prog.destroy()
                if err:
                    messagebox.showerror("Import", f"Couldn't import this file:\n{err}")
                else:
                    if isinstance(decks, str):
                        decks_txt = decks
                    else:
                        decks_txt = ", ".join(decks[:6]) + (" …" if len(decks) > 6 else "")
                    messagebox.showinfo("Import", f"Added {n} card(s)" + (f" to: {decks_txt}" if decks_txt else "") +
                                        ("." if n else ".\n(Cards you already have are skipped.)"))
                    self.app.log(f"Flashcards: imported {n} card(s) from {os.path.basename(path)}.")
                self.refresh()
            self.app.events.put(done)
        threading.Thread(target=work, daemon=True).start()

    def export_dialog(self, deck_id=None):
        decks = self.col.decks()
        if not decks:
            messagebox.showinfo("Export", "There are no decks to export yet.")
            return
        w = self._dialog("Export flashcards")
        body = ttk.Frame(w, padding=20)
        body.pack(fill="both", expand=True)
        names = ["All decks"] + [d["name"] for d in decks]
        dv = tk.StringVar(value=(self.col.deck(deck_id) or {}).get("name", "All decks") if deck_id else
                          ((self.col.deck(self._sel()) or {}).get("name") or "All decks"))
        ttk.Label(body, text="Deck").grid(row=0, column=0, sticky="w")
        ttk.Combobox(body, textvariable=dv, values=names, state="readonly", width=34).grid(row=0, column=1, sticky="w",
                                                                                          padx=10)
        fmt = tk.StringVar(value="apkg")
        ttk.Radiobutton(body, text="Anki deck (.apkg) — opens in Anki, AnkiDroid and this app on your phone",
                        value="apkg", variable=fmt).grid(row=1, column=0, columnspan=2, sticky="w", pady=(14, 2))
        ttk.Radiobutton(body, text="Text (.txt) — front, back and tags separated by tabs (Excel, Quizlet…)",
                        value="txt", variable=fmt).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        prog = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="Include study progress (Anki deck only)", variable=prog).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 14))

        def go():
            did = next((d["id"] for d in decks if d["name"] == dv.get()), None)
            base = (dv.get() if did else "Flashcards").replace("::", " - ")
            base = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in base)
            ext = fmt.get()
            path = filedialog.asksaveasfilename(parent=w, title="Save as", initialfile=f"{base}.{ext}",
                                                defaultextension="." + ext,
                                                filetypes=[("Anki deck", "*.apkg")] if ext == "apkg" else
                                                [("Text", "*.txt")])
            if not path:
                return
            try:
                if ext == "apkg":
                    n = self.col.export_apkg(path, did, with_progress=prog.get())
                else:
                    n = self.col.export_text(path, did)
            except Exception as e:
                messagebox.showerror("Export", f"Couldn't export:\n{e}", parent=w)
                return
            w.destroy()
            self.app.log(f"Flashcards: exported {n} card(s) to {path}.")
            messagebox.showinfo("Export", f"Saved {n} card(s) to:\n{path}")
        row = ttk.Frame(body)
        row.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(row, text="Cancel", command=w.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(row, text="Export…", style="Accent.TButton", command=go).pack(side="right")
        return w
