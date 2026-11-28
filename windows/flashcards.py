"""Flashcards: decks, cards and spaced repetition (FSRS, the modern scheduler Anki also uses).

Everything is kept in flashcards.db in the app's data folder. Anki decks (.apkg / .colpkg) and
text files (.txt / .csv / .tsv) can be imported, and decks exported back to .apkg (opens in Anki,
AnkiDroid and this app on the phone) or to text.

Cards are plain text. Pictures from imported Anki decks are kept as [img:file.jpg] markers, with the
picture files in the flashcards_media folder.
"""
import csv
import hashlib
import html
import io
import json
import math
import os
import random
import re
import sqlite3
import string
import tempfile
import threading
import time
import zipfile

try:
    import core
    DATA_DIR = core.DATA_DIR
except Exception:                       # used on its own (tests)
    DATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "NotionLaptopSync")
    os.makedirs(DATA_DIR, exist_ok=True)

MEDIA_DIR = os.path.join(DATA_DIR, "flashcards_media")

NEW, LEARNING, REVIEW, RELEARNING = 0, 1, 2, 3
AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4
DAY_STARTS_HOUR = 4                     # like Anki: a new study day starts at 4 am
LEARN_AHEAD = 20 * 60                   # learning cards due within 20 minutes are shown now

# ---- FSRS-4.5 (default parameters, trained on millions of Anki reviews) ----------------------------

W = [0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031, 1.6474, 0.1367, 1.0461,
     2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755]
DECAY = -0.5
FACTOR = 19 / 81
MAX_IVL = 36500


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def retrievability(elapsed_days, s):
    if s <= 0:
        return 0.0
    return (1 + FACTOR * max(0.0, elapsed_days) / s) ** DECAY


def interval_days(s, retention):
    ivl = s / FACTOR * (retention ** (1 / DECAY) - 1)
    return int(_clamp(round(ivl), 1, MAX_IVL))


def init_stability(g):
    return max(0.1, W[g - 1])


def init_difficulty(g):
    return _clamp(W[4] - (g - 3) * W[5], 1, 10)


def next_difficulty(d, g):
    nd = d - W[6] * (g - 3)
    nd = W[7] * init_difficulty(GOOD) + (1 - W[7]) * nd       # drifts back towards the middle
    return _clamp(nd, 1, 10)


def recall_stability(d, s, r, g):
    hard = W[15] if g == HARD else 1.0
    easy = W[16] if g == EASY else 1.0
    return s * (1 + math.exp(W[8]) * (11 - d) * s ** -W[9] * (math.exp(W[10] * (1 - r)) - 1) * hard * easy)


def forget_stability(d, s, r):
    return max(0.1, min(s, W[11] * d ** -W[12] * ((s + 1) ** W[13] - 1) * math.exp(W[14] * (1 - r))))


# Learning steps (minutes) before a new card moves to days, and after a lapse.
LEARN_STEPS = [1, 10]
RELEARN_STEPS = [10]


def schedule(card, g, now=None, retention=0.9, fuzz=True):
    """What happens to a card if answered with grade g (1 Again … 4 Easy).
    card: dict with state, step, due, stability, difficulty, last_review, reps, lapses.
    Returns a new dict (the card isn't changed)."""
    now = now or time.time()
    c = dict(card)
    state = c.get("state") or NEW
    s, d = c.get("stability") or 0.0, c.get("difficulty") or 0.0
    last = c.get("last_review") or 0
    elapsed = max(0.0, (now - last) / 86400) if last else 0.0
    c["reps"] = (c.get("reps") or 0) + 1
    c["last_review"] = now

    def to_review(stab):
        ivl = interval_days(stab, retention)
        if fuzz and ivl > 2:
            spread = max(1, round(ivl * 0.05))
            ivl = int(_clamp(ivl + random.randint(-spread, spread), 2, MAX_IVL))
        c["state"], c["step"] = REVIEW, 0
        c["due"] = day_start(now) + ivl * 86400
        c["ivl"] = ivl

    def learn_step(steps, i):
        c["step"] = i
        c["due"] = now + steps[i] * 60
        c["ivl"] = 0

    if state == NEW:
        c["stability"], c["difficulty"] = init_stability(g), init_difficulty(g)
        if g == EASY:
            to_review(c["stability"])
        else:
            c["state"] = LEARNING
            if g == AGAIN:
                learn_step(LEARN_STEPS, 0)
            elif g == HARD:
                c["step"] = 0
                c["due"] = now + (LEARN_STEPS[0] + LEARN_STEPS[1 if len(LEARN_STEPS) > 1 else 0]) / 2 * 60
                c["ivl"] = 0
            else:
                if len(LEARN_STEPS) > 1:
                    learn_step(LEARN_STEPS, 1)
                else:
                    to_review(c["stability"])
        return c

    if state in (LEARNING, RELEARNING):
        steps = LEARN_STEPS if state == LEARNING else RELEARN_STEPS
        step = c.get("step") or 0
        # same-day reviews nudge stability a little
        c["difficulty"] = next_difficulty(d or init_difficulty(GOOD), g)
        if g == AGAIN:
            c["stability"] = max(0.1, (s or init_stability(AGAIN)) * 0.8)
            learn_step(steps, 0)
        elif g == HARD:
            c["step"] = step
            c["due"] = now + steps[min(step, len(steps) - 1)] * 60 * (1.5 if step == 0 else 1)
            c["ivl"] = 0
        elif g == GOOD:
            if step + 1 < len(steps):
                learn_step(steps, step + 1)
            else:
                c["stability"] = max(s or init_stability(GOOD), init_stability(GOOD) if state == LEARNING else s)
                to_review(c["stability"])
        else:
            c["stability"] = max((s or init_stability(EASY)) * 1.5, init_stability(EASY) if state == LEARNING else s)
            to_review(c["stability"])
        return c

    # REVIEW
    r = retrievability(elapsed, s)
    c["difficulty"] = next_difficulty(d, g)
    if g == AGAIN:
        c["stability"] = forget_stability(d, s, r)
        c["lapses"] = (c.get("lapses") or 0) + 1
        c["state"] = RELEARNING
        learn_step(RELEARN_STEPS, 0)
    else:
        ns = recall_stability(d, s, r, g)
        c["stability"] = ns
        to_review(ns)
    return c


def day_start(t=None):
    """Start (epoch seconds) of the study day containing t (days start at 4 am local time)."""
    t = t or time.time()
    lt = time.localtime(t - DAY_STARTS_HOUR * 3600)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return midnight + DAY_STARTS_HOUR * 3600


def human_interval(seconds):
    if seconds < 60:
        return "<1m"
    m = seconds / 60
    if m < 60:
        return f"{round(m)}m"
    h = m / 60
    if h < 24:
        return f"{round(h)}h"
    d = h / 24
    if d < 30:
        return f"{round(d)}d"
    if d < 365:
        return f"{d / 30:.1f}mo".replace(".0mo", "mo")
    return f"{d / 365:.1f}y".replace(".0y", "y")


# ---- text helpers ---------------------------------------------------------------------------

IMG_RE = re.compile(r"\[img:([^\]]+)\]")


def html_to_text(s):
    """Anki field HTML -> our plain text (keeps line breaks and pictures as [img:name])."""
    if not s:
        return ""
    s = re.sub(r"<img[^>]*?src\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))[^>]*>",
               lambda m: f"[img:{html.unescape(m.group(1) or m.group(2) or m.group(3) or '')}]", s, flags=re.I)
    s = re.sub(r"\[sound:[^\]]*\]", "", s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</?(div|p|tr|h\d)\b[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"</li>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"<style.*?</style>|<script.*?</script>", "", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return tidy(s).strip()


def tidy(s):
    """Cleans imported text for showing: a bullet on its own line joins the line after it."""
    if not s:
        return s or ""
    s = re.sub(r"(^|\n)[ \t]*([•·▪◦\-\*])[ \t]*\n+[ \t]*", r"\1\2 ", s)
    return re.sub(r"\n{3,}", "\n\n", s)


def text_to_html(s):
    """Our plain text -> Anki field HTML."""
    parts = []
    pos = 0
    for m in IMG_RE.finditer(s or ""):
        parts.append(html.escape(s[pos:m.start()]).replace("\n", "<br>"))
        parts.append(f'<img src="{html.escape(m.group(1), quote=True)}">')
        pos = m.end()
    parts.append(html.escape((s or "")[pos:]).replace("\n", "<br>"))
    return "".join(parts)


def images_in(s):
    return IMG_RE.findall(s or "")


CLOZE_RE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)


def cloze_cards(text):
    """Cloze text -> [(front, back)] one per cloze number."""
    nums = sorted({int(m.group(1)) for m in CLOZE_RE.finditer(text)})
    out = []
    for n in nums:
        def front(m):
            if int(m.group(1)) == n:
                return f"[{m.group(3)}]" if m.group(3) else "[…]"
            return m.group(2)

        def back(m):
            return f"【{m.group(2)}】" if int(m.group(1)) == n else m.group(2)
        out.append((CLOZE_RE.sub(front, text), CLOZE_RE.sub(back, text)))
    return out


def _guid():
    chars = string.ascii_letters + string.digits + "!#$%&()*+,-./:;<=>?@[]^_`{|}~"
    return "".join(random.choice(chars) for _ in range(10))


def _now_ms():
    return int(time.time() * 1000)


# ---- the collection -------------------------------------------------------------------------

class Collection:
    def __init__(self, path=None):
        self.path = path or os.path.join(DATA_DIR, "flashcards.db")
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            for p in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA foreign_keys=ON"):
                try:
                    self.conn.execute(p)
                except sqlite3.Error:
                    pass
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS decks(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                new_per_day INTEGER DEFAULT 20, reviews_per_day INTEGER DEFAULT 200,
                retention REAL DEFAULT 0.9, created REAL, mod REAL);
            CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, guid TEXT UNIQUE, deck_id INTEGER NOT NULL
                REFERENCES decks(id) ON DELETE CASCADE, front TEXT NOT NULL, back TEXT NOT NULL,
                tags TEXT DEFAULT '', reverse INTEGER DEFAULT 0, created REAL, mod REAL);
            CREATE TABLE IF NOT EXISTS cards(id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL
                REFERENCES notes(id) ON DELETE CASCADE, ord INTEGER DEFAULT 0, deck_id INTEGER NOT NULL,
                state INTEGER DEFAULT 0, step INTEGER DEFAULT 0, due REAL DEFAULT 0, ivl INTEGER DEFAULT 0,
                stability REAL DEFAULT 0, difficulty REAL DEFAULT 0, reps INTEGER DEFAULT 0,
                lapses INTEGER DEFAULT 0, last_review REAL DEFAULT 0, suspended INTEGER DEFAULT 0,
                position INTEGER DEFAULT 0, mod REAL);
            CREATE INDEX IF NOT EXISTS cards_deck_due ON cards(deck_id, state, due);
            CREATE INDEX IF NOT EXISTS cards_note ON cards(note_id);
            CREATE TABLE IF NOT EXISTS revlog(id INTEGER PRIMARY KEY, card_id INTEGER, grade INTEGER,
                state INTEGER, ivl INTEGER, took REAL, time REAL);
            CREATE INDEX IF NOT EXISTS revlog_time ON revlog(time);
            CREATE TABLE IF NOT EXISTS graves(kind TEXT, guid TEXT, time REAL, PRIMARY KEY(kind, guid));
            CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            """)
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(decks)")]
            if "guid" not in cols:
                self.conn.execute("ALTER TABLE decks ADD COLUMN guid TEXT")
            for (did,) in self.conn.execute("SELECT id FROM decks WHERE guid IS NULL OR guid=''").fetchall():
                self.conn.execute("UPDATE decks SET guid=? WHERE id=?", (_guid() + _guid(), did))
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # decks

    def decks(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM decks ORDER BY name COLLATE NOCASE")]

    def deck(self, deck_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM decks WHERE id=?", (deck_id,)).fetchone()
        return dict(r) if r else None

    def deck_by_name(self, name, create=True):
        name = (name or "Default").strip() or "Default"
        with self.lock:
            r = self.conn.execute("SELECT id FROM decks WHERE name=? COLLATE NOCASE", (name,)).fetchone()
            if r:
                return r[0]
            if not create:
                return None
            cur = self.conn.execute("INSERT INTO decks(name, created, mod, guid) VALUES(?,?,?,?)",
                                    (name, time.time(), time.time(), _guid() + _guid()))
            self.conn.commit()
            return cur.lastrowid

    def rename_deck(self, deck_id, name):
        with self.lock:
            self.conn.execute("UPDATE decks SET name=?, mod=? WHERE id=?", (name.strip(), time.time(), deck_id))
            self.conn.commit()

    def set_deck_options(self, deck_id, new_per_day, reviews_per_day, retention):
        with self.lock:
            self.conn.execute("UPDATE decks SET new_per_day=?, reviews_per_day=?, retention=?, mod=? WHERE id=?",
                              (int(new_per_day), int(reviews_per_day), float(_clamp(retention, 0.7, 0.99)),
                               time.time(), deck_id))
            self.conn.commit()

    def _grave(self, kind, guid):
        if guid:
            self.conn.execute("INSERT OR REPLACE INTO graves VALUES(?,?,?)", (kind, guid, time.time()))

    def delete_deck(self, deck_id):
        with self.lock:
            g = self.conn.execute("SELECT guid FROM decks WHERE id=?", (deck_id,)).fetchone()
            if g:
                self._grave("deck", g[0])
            for (ng,) in self.conn.execute("SELECT guid FROM notes WHERE deck_id=?", (deck_id,)).fetchall():
                self._grave("note", ng)
            self.conn.execute("DELETE FROM cards WHERE deck_id=?", (deck_id,))
            self.conn.execute("DELETE FROM notes WHERE deck_id=?", (deck_id,))
            self.conn.execute("DELETE FROM decks WHERE id=?", (deck_id,))
            self.conn.commit()

    # notes & cards

    def add_note(self, deck_id, front, back, tags="", reverse=False, guid=None, commit=True):
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO notes(guid, deck_id, front, back, tags, reverse, created, mod) VALUES(?,?,?,?,?,?,?,?)",
                (guid or _guid(), deck_id, front, back, tags.strip(), 1 if reverse else 0, now, now))
            nid = cur.lastrowid
            pos = (self.conn.execute("SELECT COALESCE(MAX(position),0) FROM cards").fetchone()[0] or 0) + 1
            for ord_ in ((0, 1) if reverse else (0,)):
                self.conn.execute("INSERT INTO cards(note_id, ord, deck_id, position, mod) VALUES(?,?,?,?,?)",
                                  (nid, ord_, deck_id, pos + ord_, now))
            if commit:
                self.conn.commit()
        return nid

    def update_note(self, note_id, deck_id, front, back, tags, reverse):
        now = time.time()
        with self.lock:
            self.conn.execute("UPDATE notes SET deck_id=?, front=?, back=?, tags=?, reverse=?, mod=? WHERE id=?",
                              (deck_id, front, back, tags.strip(), 1 if reverse else 0, now, note_id))
            # (the cards' study progress isn't changed, so their sync time stays as it is)
            self.conn.execute("UPDATE cards SET deck_id=? WHERE note_id=?", (deck_id, note_id))
            has_rev = self.conn.execute("SELECT 1 FROM cards WHERE note_id=? AND ord=1", (note_id,)).fetchone()
            if reverse and not has_rev:
                self.conn.execute("INSERT INTO cards(note_id, ord, deck_id, position, mod) VALUES(?,?,?,?,?)",
                                  (note_id, 1, deck_id, int(now), now))
            elif not reverse and has_rev:
                self.conn.execute("DELETE FROM cards WHERE note_id=? AND ord=1", (note_id,))
            self.conn.commit()

    def note(self, note_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        return dict(r) if r else None

    def delete_note(self, note_id):
        with self.lock:
            g = self.conn.execute("SELECT guid FROM notes WHERE id=?", (note_id,)).fetchone()
            if g:
                self._grave("note", g[0])
            self.conn.execute("DELETE FROM cards WHERE note_id=?", (note_id,))
            self.conn.execute("DELETE FROM notes WHERE id=?", (note_id,))
            self.conn.commit()

    def move_notes(self, note_ids, deck_id):
        with self.lock:
            for nid in note_ids:
                self.conn.execute("UPDATE notes SET deck_id=?, mod=? WHERE id=?", (deck_id, time.time(), nid))
                self.conn.execute("UPDATE cards SET deck_id=? WHERE note_id=?", (deck_id, nid))
            self.conn.commit()

    def set_suspended(self, card_id, on):
        with self.lock:
            self.conn.execute("UPDATE cards SET suspended=?, mod=? WHERE id=?", (1 if on else 0, time.time(), card_id))
            self.conn.commit()

    def reset_cards(self, note_ids):
        with self.lock:
            for nid in note_ids:
                self.conn.execute("UPDATE cards SET state=0, step=0, due=0, ivl=0, stability=0, difficulty=0, "
                                  "reps=0, lapses=0, last_review=0, mod=? WHERE note_id=?", (time.time(), nid))
            self.conn.commit()

    def card(self, card_id):
        with self.lock:
            r = self.conn.execute(
                "SELECT c.*, n.front, n.back, n.tags, n.reverse FROM cards c JOIN notes n ON n.id=c.note_id "
                "WHERE c.id=?", (card_id,)).fetchone()
        return dict(r) if r else None

    def search(self, query="", deck_id=None, limit=2000):
        """Notes matching the words (in front, back or tags), newest first."""
        sql = ("SELECT n.id, n.front, n.back, n.tags, n.deck_id, d.name AS deck, MIN(c.due) AS due, "
               "MAX(c.ivl) AS ivl, MIN(c.state) AS state, MAX(c.suspended) AS suspended, SUM(c.lapses) AS lapses "
               "FROM notes n JOIN decks d ON d.id=n.deck_id LEFT JOIN cards c ON c.note_id=n.id WHERE 1=1")
        args = []
        if deck_id:
            sql += " AND n.deck_id=?"
            args.append(deck_id)
        for w in (query or "").split():
            if w.lower().startswith("tag:"):
                sql += " AND (' ' || n.tags || ' ') LIKE ?"
                args.append(f"% {w[4:]} %")
            else:
                sql += " AND (n.front LIKE ? OR n.back LIKE ? OR n.tags LIKE ?)"
                args += [f"%{w}%"] * 3
        sql += f" GROUP BY n.id ORDER BY n.id DESC LIMIT {int(limit)}"
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args)]

    # studying

    def _studied_today(self, deck_id, start):
        with self.lock:
            new = self.conn.execute(
                "SELECT COUNT(DISTINCT r.card_id) FROM revlog r JOIN cards c ON c.id=r.card_id "
                "WHERE c.deck_id=? AND r.time>=? AND r.state=0", (deck_id, start)).fetchone()[0]
            rev = self.conn.execute(
                "SELECT COUNT(*) FROM revlog r JOIN cards c ON c.id=r.card_id "
                "WHERE c.deck_id=? AND r.time>=? AND r.state=2", (deck_id, start)).fetchone()[0]
        return new, rev

    def counts(self, deck_id, now=None):
        """(new, learning, review) cards to study now, after the daily limits."""
        now = now or time.time()
        start = day_start(now)
        d = self.deck(deck_id) or {}
        new_done, rev_done = self._studied_today(deck_id, start)
        with self.lock:
            new = self.conn.execute("SELECT COUNT(*) FROM cards WHERE deck_id=? AND state=0 AND suspended=0",
                                    (deck_id,)).fetchone()[0]
            lrn = self.conn.execute("SELECT COUNT(*) FROM cards WHERE deck_id=? AND state IN (1,3) AND suspended=0 "
                                    "AND due<=?", (deck_id, now + LEARN_AHEAD)).fetchone()[0]
            rev = self.conn.execute("SELECT COUNT(*) FROM cards WHERE deck_id=? AND state=2 AND suspended=0 "
                                    "AND due<?", (deck_id, start + 86400)).fetchone()[0]
        new = max(0, min(new, (d.get("new_per_day") or 0) - new_done))
        rev = max(0, min(rev, (d.get("reviews_per_day") or 0) - rev_done))
        return new, lrn, rev

    def total(self, deck_id):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM cards WHERE deck_id=?", (deck_id,)).fetchone()[0]

    def next_card(self, deck_id, now=None, exclude=None):
        """The next card to study (learning first, then reviews and new cards mixed), or None."""
        now = now or time.time()
        new_left, _, rev_left = self.counts(deck_id, now)
        start = day_start(now)
        ex = exclude or -1
        with self.lock:
            lrn = self.conn.execute(
                "SELECT id FROM cards WHERE deck_id=? AND state IN (1,3) AND suspended=0 AND due<=? AND id!=? "
                "ORDER BY due LIMIT 1", (deck_id, now, ex)).fetchone()
            if lrn:
                return lrn[0]
            rev = self.conn.execute(
                "SELECT id FROM cards WHERE deck_id=? AND state=2 AND suspended=0 AND due<? AND id!=? "
                "ORDER BY due LIMIT 1", (deck_id, start + 86400, ex)).fetchone() if rev_left > 0 else None
            new = self.conn.execute(
                "SELECT id FROM cards WHERE deck_id=? AND state=0 AND suspended=0 AND id!=? "
                "ORDER BY position, id LIMIT 1", (deck_id, ex)).fetchone() if new_left > 0 else None
            if rev and new:
                # mix: roughly one new card for every few reviews
                return new[0] if random.random() < new_left / max(1, new_left + rev_left) else rev[0]
            if rev or new:
                return (rev or new)[0]
            ahead = self.conn.execute(
                "SELECT id FROM cards WHERE deck_id=? AND state IN (1,3) AND suspended=0 AND due<=? "
                "ORDER BY due LIMIT 1", (deck_id, now + LEARN_AHEAD)).fetchone()
            return ahead[0] if ahead else None

    def preview(self, card_id, now=None):
        """{grade: seconds until the card comes back} for the four buttons."""
        now = now or time.time()
        c = self.card(card_id)
        ret = (self.deck(c["deck_id"]) or {}).get("retention") or 0.9
        return {g: max(0, schedule(c, g, now, ret, fuzz=False)["due"] - now) for g in (AGAIN, HARD, GOOD, EASY)}

    def answer(self, card_id, g, took=0.0, now=None):
        """Records an answer. Returns the card as it was before (for Undo)."""
        now = now or time.time()
        c = self.card(card_id)
        ret = (self.deck(c["deck_id"]) or {}).get("retention") or 0.9
        n = schedule(c, g, now, ret)
        with self.lock:
            self.conn.execute(
                "UPDATE cards SET state=?, step=?, due=?, ivl=?, stability=?, difficulty=?, reps=?, lapses=?, "
                "last_review=?, mod=? WHERE id=?",
                (n["state"], n.get("step") or 0, n["due"], n.get("ivl") or 0, n["stability"], n["difficulty"],
                 n["reps"], n.get("lapses") or 0, n["last_review"], now, card_id))
            cur = self.conn.execute("INSERT INTO revlog(card_id, grade, state, ivl, took, time) VALUES(?,?,?,?,?,?)",
                                    (card_id, g, c["state"], n.get("ivl") or 0, took, now))
            self.conn.commit()
        c["_revlog"] = cur.lastrowid
        return c

    def undo(self, before):
        with self.lock:
            self.conn.execute(
                "UPDATE cards SET state=?, step=?, due=?, ivl=?, stability=?, difficulty=?, reps=?, lapses=?, "
                "last_review=?, mod=? WHERE id=?",
                (before["state"], before["step"], before["due"], before["ivl"], before["stability"],
                 before["difficulty"], before["reps"], before["lapses"], before["last_review"], time.time(),
                 before["id"]))
            if before.get("_revlog"):
                self.conn.execute("DELETE FROM revlog WHERE id=?", (before["_revlog"],))
            self.conn.commit()

    # statistics

    def today(self):
        start = day_start()
        with self.lock:
            n, t = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(took),0) FROM revlog WHERE time>=?",
                                     (start,)).fetchone()
            ok = self.conn.execute("SELECT COUNT(*) FROM revlog WHERE time>=? AND grade>1 AND state=2",
                                   (start,)).fetchone()[0]
            revs = self.conn.execute("SELECT COUNT(*) FROM revlog WHERE time>=? AND state=2", (start,)).fetchone()[0]
        return {"reviews": n, "seconds": t, "correct_pct": (ok / revs * 100) if revs else None}

    def per_day(self, days=112):
        """{day index (0 = today): number of answers} for the last `days` days."""
        start = day_start()
        since = start - (days - 1) * 86400
        with self.lock:
            rows = self.conn.execute("SELECT time FROM revlog WHERE time>=?", (since,)).fetchall()
        out = {}
        for (t,) in rows:
            i = int((start - day_start(t)) // 86400 + 0.5)
            out[i] = out.get(i, 0) + 1
        return out

    def streak(self):
        per = self.per_day(400)
        n = 0 if per.get(0) else 0
        i = 0 if per.get(0) else 1
        while per.get(i):
            n += 1
            i += 1
        return n

    def forecast(self, days=7):
        start = day_start()
        with self.lock:
            rows = self.conn.execute("SELECT due FROM cards WHERE state=2 AND suspended=0 AND due<?",
                                     (start + days * 86400,)).fetchall()
        out = [0] * days
        for (due,) in rows:
            out[max(0, min(days - 1, int((due - start) // 86400)))] += 1
        return out

    # ---- media -------------------------------------------------------------------------------

    @staticmethod
    def media_path(name):
        return os.path.join(MEDIA_DIR, os.path.basename(name))

    @staticmethod
    def _store_media(name, data):
        """Saves a picture; if a different file already has that name, keeps both. Returns the name used."""
        os.makedirs(MEDIA_DIR, exist_ok=True)
        name = os.path.basename(name) or "file"
        p = os.path.join(MEDIA_DIR, name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                if f.read() == data:
                    return name
            stem, ext = os.path.splitext(name)
            name = f"{stem}-{hashlib.sha1(data).hexdigest()[:8]}{ext}"
            p = os.path.join(MEDIA_DIR, name)
        with open(p, "wb") as f:
            f.write(data)
        return name

    # ---- import ------------------------------------------------------------------------------

    def import_file(self, path, deck_name=None, keep_progress=True, progress=lambda m: None):
        ext = os.path.splitext(path)[1].lower()
        if ext in (".apkg", ".colpkg", ".zip"):
            return self.import_apkg(path, keep_progress, progress)
        return self.import_text(path, deck_name or os.path.splitext(os.path.basename(path))[0])

    def import_text(self, path, deck_name):
        """Text like Anki's: one card per line, front<TAB>back[<TAB>tags]. Also ; or , separated (CSV).
        Lines starting with # are settings (#separator:tab, #deck:Name, #tags column:3, #html:true)."""
        with open(path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8-sig", "replace")
        sep, deck, tag_col, is_html, deck_col = None, deck_name, None, None, None
        body = []
        for line in text.splitlines():
            if line.startswith("#") and ":" in line:
                k, _, v = line[1:].partition(":")
                k, v = k.strip().lower(), v.strip()
                if k == "separator":
                    sep = {"tab": "\t", "comma": ",", "semicolon": ";", "space": " ", "pipe": "|",
                           "colon": ":"}.get(v.lower(), v[:1] or None)
                elif k == "deck":
                    deck = v
                elif k == "tags column":
                    tag_col = int(v) - 1 if v.isdigit() else None
                elif k == "deck column":
                    deck_col = int(v) - 1 if v.isdigit() else None
                elif k == "html":
                    is_html = v.lower() == "true"
                continue
            if line.strip():
                body.append(line)
        if not body:
            return 0, deck
        if sep is None:
            sample = "\n".join(body[:20])
            sep = "\t" if "\t" in sample else (";" if sample.count(";") > sample.count(",") else ",")
        rows = list(csv.reader(io.StringIO("\n".join(body)), delimiter=sep))
        if is_html is None:
            is_html = any(re.search(r"<(br|div|b|i|img|span)\b", c or "", re.I) for r in rows[:50] for c in r)
        n = 0
        with self.lock:
            decks = {}
            for r in rows:
                if not r or not any(c.strip() for c in r):
                    continue
                fields = [c for i, c in enumerate(r) if i not in (tag_col, deck_col)]
                front = fields[0] if fields else ""
                back = fields[1] if len(fields) > 1 else ""
                tags = r[tag_col] if tag_col is not None and tag_col < len(r) else (
                    fields[2] if len(fields) > 2 and tag_col is None else "")
                dname = r[deck_col] if deck_col is not None and deck_col < len(r) else deck
                if is_html:
                    front, back = html_to_text(front), html_to_text(back)
                if "{{c" in front and CLOZE_RE.search(front):
                    pairs = cloze_cards(front)
                    if back:
                        pairs = [(f, b + "\n\n" + back) for f, b in pairs]
                else:
                    pairs = [(front, back)]
                did = decks.get(dname) or decks.setdefault(dname, self.deck_by_name(dname))
                for f, b in pairs:
                    self.add_note(did, f.strip(), b.strip(), tags, commit=False)
                    n += 1
            self.conn.commit()
        return n, deck

    def import_apkg(self, path, keep_progress=True, progress=lambda m: None):
        """Imports an Anki package (.apkg or .colpkg). Returns (cards added, [deck names])."""
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            col_name = None
            zstd = False
            if "collection.anki21b" in names:
                col_name, zstd = "collection.anki21b", True
            elif "collection.anki21" in names:
                col_name = "collection.anki21"
            elif "collection.anki2" in names:
                col_name = "collection.anki2"
            if not col_name:
                raise ValueError("This isn't an Anki deck file (no collection inside).")
            data = z.read(col_name)
            if zstd:
                data = _zstd_decompress(data)
            media_map = {}
            if "media" in names:
                mraw = z.read("media")
                try:
                    media_map = {k: v for k, v in json.loads(mraw.decode("utf-8")).items()}
                except Exception:
                    try:
                        media_map = {str(i): n for i, n in enumerate(_media_entries(_zstd_decompress(mraw)))}
                        zstd = True
                    except Exception:
                        media_map = {}
            fd, tmp = tempfile.mkstemp(suffix=".anki2")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            try:
                renamed = {}
                for i, (k, fname) in enumerate(media_map.items()):
                    if k not in names or not fname:
                        continue
                    blob = z.read(k)
                    if zstd:
                        try:
                            blob = _zstd_decompress(blob)
                        except Exception:
                            pass
                    used = self._store_media(fname, blob)
                    if used != fname:
                        renamed[fname] = used
                    if i % 50 == 0:
                        progress(f"Copying pictures… {i}/{len(media_map)}")
                return self._import_collection(tmp, keep_progress, renamed, progress)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _import_collection(self, path, keep_progress, renamed, progress):
        src = sqlite3.connect(path)
        src.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # deck names and note types: old packages keep them as JSON in col, new ones in tables
            decks, models = {}, {}
            if "decks" in tables and src.execute("SELECT COUNT(*) FROM decks").fetchone()[0]:
                for r in src.execute("SELECT id, name FROM decks"):
                    decks[r["id"]] = r["name"].replace("\x1f", "::")
            else:
                row = src.execute("SELECT decks FROM col").fetchone()
                for k, v in json.loads(row[0] or "{}").items():
                    decks[int(k)] = v.get("name", "Default")
            if "notetypes" in tables and src.execute("SELECT COUNT(*) FROM notetypes").fetchone()[0]:
                for r in src.execute("SELECT id, name, config FROM notetypes"):
                    kind = 1 if _proto_varint_field(r["config"], 1) == 1 else 0
                    flds = [f["name"] for f in src.execute("SELECT name FROM fields WHERE ntid=? ORDER BY ord",
                                                           (r["id"],))]
                    tmpls = [t["name"] for t in src.execute("SELECT name FROM templates WHERE ntid=? ORDER BY ord",
                                                            (r["id"],))]
                    models[r["id"]] = {"type": kind, "flds": flds, "tmpls": tmpls, "name": r["name"]}
            else:
                row = src.execute("SELECT models FROM col").fetchone()
                for k, v in json.loads(row[0] or "{}").items():
                    models[int(k)] = {"type": v.get("type", 0), "flds": [f["name"] for f in v.get("flds", [])],
                                      "tmpls": [t.get("name", "") for t in v.get("tmpls", [])],
                                      "tmpl_q": [t.get("qfmt", "") for t in v.get("tmpls", [])],
                                      "name": v.get("name", "")}
            crt = src.execute("SELECT crt FROM col").fetchone()[0] or 0
            if crt > 10 ** 11:
                crt //= 1000
            notes = {r["id"]: r for r in src.execute("SELECT id, guid, mid, flds, tags FROM notes")}
            cards_by_note = {}
            for r in src.execute("SELECT id, nid, did, ord, type, queue, due, ivl, factor, reps, lapses FROM cards"):
                cards_by_note.setdefault(r["nid"], []).append(r)
            with self.lock:
                existing = {r[0] for r in self.conn.execute("SELECT guid FROM notes")}
            added, deck_names = 0, set()
            now = time.time()
            today = day_start(now)
            deck_ids = {}
            total = len(notes)
            for i, (nid, n) in enumerate(notes.items()):
                if i % 200 == 0:
                    progress(f"Importing cards… {i}/{total}")
                fields = (n["flds"] or "").split("\x1f")
                if renamed:
                    fields = [_rename_media(f, renamed) for f in fields]
                m = models.get(n["mid"], {"type": 0, "flds": [], "tmpls": []})
                cards = sorted(cards_by_note.get(nid, []), key=lambda c: c["ord"])
                did = cards[0]["did"] if cards else 1
                dname = decks.get(did) or "Imported"
                if dname == "Default" and len(decks) > 1:
                    dname = "Default"
                deck_names.add(dname)
                if dname not in deck_ids:
                    deck_ids[dname] = self.deck_by_name(dname)
                deck_id = deck_ids[dname]
                tags = (n["tags"] or "").strip()
                if m["type"] == 1:                     # cloze
                    text = html_to_text(fields[0])
                    extra = html_to_text(fields[1]) if len(fields) > 1 else ""
                    pairs = cloze_cards(text)
                    for k, (f, b) in enumerate(pairs):
                        guid = f"{n['guid']}#c{k + 1}"
                        if guid in existing:
                            continue
                        self.add_note(deck_id, f, b + ("\n\n" + extra if extra else ""), tags, guid=guid, commit=False)
                        added += 1
                    continue
                if n["guid"] in existing:
                    continue
                front = html_to_text(fields[0]) if fields else ""
                back = html_to_text(fields[1]) if len(fields) > 1 else ""
                # extra fields go on the back (e.g. examples, notes)
                for j, extra in enumerate(fields[2:], start=2):
                    t = html_to_text(extra)
                    if t:
                        label = m["flds"][j] if j < len(m["flds"]) else ""
                        back += ("\n\n" + (f"{label}: " if label else "") + t)
                reverse = len(cards) > 1 or len(m.get("tmpls") or []) > 1
                nid_new = self.add_note(deck_id, front, back.strip(), tags, reverse=reverse and len(cards) > 1,
                                        guid=n["guid"], commit=False)
                added += 1
                if keep_progress:
                    with self.lock:
                        mine = self.conn.execute("SELECT id, ord FROM cards WHERE note_id=?", (nid_new,)).fetchall()
                    for c in cards:
                        target = next((x[0] for x in mine if x[1] == min(c["ord"], 1)), None)
                        if target is None or c["type"] == 0 or c["queue"] < 0 and c["type"] == 0:
                            continue
                        ivl = max(1, c["ivl"] or 1) if c["type"] == 2 else 0
                        factor = c["factor"] or 2500
                        if c["type"] == 2:
                            due = crt + c["due"] * 86400 if c["due"] < 10 ** 8 else c["due"]
                            due = day_start(due) if due > 0 else today
                            state = REVIEW
                        else:
                            due = c["due"] if c["due"] > 10 ** 8 else now
                            state = RELEARNING if c["type"] == 3 else LEARNING
                        diff = _clamp(11 - factor / 1000 * 2.4, 1, 10)
                        with self.lock:
                            self.conn.execute(
                                "UPDATE cards SET state=?, due=?, ivl=?, stability=?, difficulty=?, reps=?, lapses=?, "
                                "last_review=?, suspended=? WHERE id=?",
                                (state, due, ivl, float(max(ivl, 0.5)), diff, c["reps"] or 0, c["lapses"] or 0,
                                 (due - ivl * 86400) if state == REVIEW else now, 1 if c["queue"] == -1 else 0,
                                 target))
            with self.lock:
                self.conn.commit()
            return added, sorted(deck_names)
        finally:
            src.close()

    # ---- export ------------------------------------------------------------------------------

    def export_text(self, path, deck_id=None):
        rows = self.search("", deck_id, limit=10 ** 9)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("#separator:tab\n#html:true\n#tags column:3\n")
            w = csv.writer(f, delimiter="\t", lineterminator="\n")
            for r in reversed(rows):
                w.writerow([text_to_html(r["front"]), text_to_html(r["back"]), r["tags"] or ""])
        return len(rows)

    def export_apkg(self, path, deck_id=None, with_progress=True):
        """Writes an Anki package (opens in Anki 2.1+, AnkiDroid, and this app)."""
        with self.lock:
            decks = [dict(r) for r in self.conn.execute(
                "SELECT * FROM decks" + (" WHERE id=?" if deck_id else ""), ((deck_id,) if deck_id else ()))]
            ids = [d["id"] for d in decks]
            if not ids:
                raise ValueError("No deck to export.")
            marks = ",".join("?" * len(ids))
            notes = [dict(r) for r in self.conn.execute(f"SELECT * FROM notes WHERE deck_id IN ({marks})", ids)]
            cards = [dict(r) for r in self.conn.execute(f"SELECT * FROM cards WHERE deck_id IN ({marks})", ids)]
        now = int(time.time())
        crt = int(day_start())
        base_id = _now_ms()
        model_basic, model_rev = base_id, base_id + 1
        deck_anki = {d["id"]: base_id + 10 + i for i, d in enumerate(decks)}

        def model(mid, name, reverse):
            tmpls = [{"name": "Card 1", "ord": 0, "qfmt": "{{Front}}",
                      "afmt": "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}", "did": None, "bqfmt": "", "bafmt": ""}]
            if reverse:
                tmpls.append({"name": "Card 2", "ord": 1, "qfmt": "{{Back}}",
                              "afmt": "{{FrontSide}}\n\n<hr id=answer>\n\n{{Front}}", "did": None, "bqfmt": "",
                              "bafmt": ""})
            return {"id": mid, "name": name, "type": 0, "mod": now, "usn": -1, "sortf": 0,
                    "did": deck_anki[decks[0]["id"]], "tmpls": tmpls,
                    "flds": [{"name": n, "ord": i, "sticky": False, "rtl": False, "font": "Arial", "size": 20,
                              "media": []} for i, n in enumerate(("Front", "Back"))],
                    "css": ".card {\n font-family: arial;\n font-size: 20px;\n text-align: center;\n color: black;\n"
                           " background-color: white;\n}\n",
                    "latexPre": "\\documentclass[12pt]{article}\n\\special{papersize=3in,5in}\n\\usepackage[utf8]{inputenc}\n"
                                "\\usepackage{amssymb,amsmath}\n\\pagestyle{empty}\n\\setlength{\\parindent}{0in}\n"
                                "\\begin{document}\n",
                    "latexPost": "\\end{document}", "latexsvg": False,
                    "req": [[0, "any", [0]]] + ([[1, "any", [1]]] if reverse else []),
                    "tags": [], "vers": []}
        models = {str(model_basic): model(model_basic, "Basic (Notion Sync)", False),
                  str(model_rev): model(model_rev, "Basic and reversed (Notion Sync)", True)}
        anki_decks = {"1": _anki_deck(1, "Default", now)}
        for d in decks:
            anki_decks[str(deck_anki[d["id"]])] = _anki_deck(deck_anki[d["id"]], d["name"], now)
        dconf = {"1": _anki_dconf(now)}
        conf = {"nextPos": 1, "estTimes": True, "activeDecks": [1], "sortType": "noteFld", "timeLim": 0,
                "sortBackwards": False, "addToCur": True, "curDeck": 1, "newBury": True, "newSpread": 0,
                "dueCounts": True, "curModel": str(model_basic), "collapseTime": 1200}

        fd, tmp = tempfile.mkstemp(suffix=".anki2")
        os.close(fd)
        os.remove(tmp)
        db = sqlite3.connect(tmp)
        try:
            db.executescript(_ANKI_SCHEMA)
            db.execute("INSERT INTO col VALUES(1,?,?,?,11,0,0,0,?,?,?,?,?)",
                       (crt, now * 1000, now * 1000, json.dumps(conf), json.dumps(models), json.dumps(anki_decks),
                        json.dumps(dconf), "{}"))
            media_names = set()
            note_anki = {}
            for i, n in enumerate(notes):
                nid = base_id + 100 + i
                note_anki[n["id"]] = nid
                front, back = text_to_html(n["front"]), text_to_html(n["back"])
                media_names.update(images_in(n["front"]) + images_in(n["back"]))
                sfld = html_to_text(front)
                csum = int(hashlib.sha1(sfld.encode("utf-8")).hexdigest()[:8], 16)
                tags = " ".join(t for t in (n["tags"] or "").split())
                db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (nid, n["guid"] or _guid(), model_rev if n["reverse"] else model_basic,
                            int(n["mod"] or now), -1, f" {tags} " if tags else "", front + "\x1f" + back,
                            sfld, csum, 0, ""))
            for i, c in enumerate(cards):
                cid = base_id + 100 + len(notes) + i
                did = deck_anki[c["deck_id"]]
                typ = queue = 0
                due, ivl, factor = c["position"] or i + 1, 0, 2500
                if with_progress and c["state"] != NEW:
                    if c["state"] == REVIEW:
                        typ = queue = 2
                        due = int((day_start(c["due"]) - crt) // 86400)
                        ivl = max(1, int(c["ivl"] or round(c["stability"] or 1)))
                    else:
                        typ = 1 if c["state"] == LEARNING else 3
                        queue = 1
                        due = int(c["due"])
                    factor = int(_clamp(1300 + (10 - (c["difficulty"] or 5)) * 300, 1300, 4000))
                if c["suspended"]:
                    queue = -1
                db.execute("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (cid, note_anki[c["note_id"]], did, c["ord"], int(c["mod"] or now), -1, typ, queue, due,
                            ivl, factor, c["reps"] if with_progress else 0, c["lapses"] if with_progress else 0,
                            0, 0, 0, 0, ""))
            db.commit()
        finally:
            db.close()
        try:
            media_index = {}
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(tmp, "collection.anki2")
                k = 0
                for name in sorted(media_names):
                    p = self.media_path(name)
                    if os.path.isfile(p):
                        z.write(p, str(k))
                        media_index[str(k)] = name
                        k += 1
                z.writestr("media", json.dumps(media_index))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return len(notes)


def _rename_media(field, renamed):
    for old, new in renamed.items():
        field = field.replace(old, new)
    return field


def _anki_deck(did, name, now):
    return {"id": did, "name": name, "mod": now, "usn": -1, "lrnToday": [0, 0], "revToday": [0, 0],
            "newToday": [0, 0], "timeToday": [0, 0], "collapsed": False, "browserCollapsed": False, "desc": "",
            "dyn": 0, "conf": 1, "extendNew": 0, "extendRev": 0}


def _anki_dconf(now):
    return {"id": 1, "name": "Default", "mod": now, "usn": -1, "maxTaken": 60, "autoplay": True, "timer": 0,
            "replayq": True, "dyn": False,
            "new": {"bury": False, "delays": [1.0, 10.0], "initialFactor": 2500, "ints": [1, 4, 0], "order": 1,
                    "perDay": 20},
            "lapse": {"delays": [10.0], "leechAction": 1, "leechFails": 8, "minInt": 1, "mult": 0.0},
            "rev": {"bury": False, "ease4": 1.3, "ivlFct": 1.0, "maxIvl": 36500, "perDay": 200, "hardFactor": 1.2}}


_ANKI_SCHEMA = """
CREATE TABLE col (id integer primary key, crt integer not null, mod integer not null, scm integer not null,
  ver integer not null, dty integer not null, usn integer not null, ls integer not null, conf text not null,
  models text not null, decks text not null, dconf text not null, tags text not null);
CREATE TABLE notes (id integer primary key, guid text not null, mid integer not null, mod integer not null,
  usn integer not null, tags text not null, flds text not null, sfld integer not null, csum integer not null,
  flags integer not null, data text not null);
CREATE TABLE cards (id integer primary key, nid integer not null, did integer not null, ord integer not null,
  mod integer not null, usn integer not null, type integer not null, queue integer not null, due integer not null,
  ivl integer not null, factor integer not null, reps integer not null, lapses integer not null,
  left integer not null, odue integer not null, odid integer not null, flags integer not null, data text not null);
CREATE TABLE revlog (id integer primary key, cid integer not null, usn integer not null, ease integer not null,
  ivl integer not null, lastIvl integer not null, factor integer not null, time integer not null,
  type integer not null);
CREATE TABLE graves (usn integer not null, oid integer not null, type integer not null);
CREATE INDEX ix_notes_usn on notes (usn);
CREATE INDEX ix_cards_usn on cards (usn);
CREATE INDEX ix_revlog_usn on revlog (usn);
CREATE INDEX ix_cards_nid on cards (nid);
CREATE INDEX ix_cards_sched on cards (did, queue, due);
CREATE INDEX ix_revlog_cid on revlog (cid);
CREATE INDEX ix_notes_csum on notes (csum);
"""


# ---- newer Anki packages (zstd compressed) --------------------------------------------------

def _zstd_decompress(data):
    try:
        import zstandard
    except ImportError:
        raise ValueError("This deck was saved by a newer Anki in a compressed format. In Anki, export it again "
                         "and tick “Support older Anki versions”, then import that file.")
    return zstandard.ZstdDecompressor().decompressobj().decompress(data)


def _varint(b, i):
    shift = result = 0
    while True:
        x = b[i]
        i += 1
        result |= (x & 0x7F) << shift
        if not x & 0x80:
            return result, i
        shift += 7


def _proto_fields(b):
    """Yields (field number, wire type, value) from protobuf bytes."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif wt == 5:
            v = b[i:i + 4]
            i += 4
        elif wt == 1:
            v = b[i:i + 8]
            i += 8
        else:
            break
        yield fn, wt, v


def _proto_varint_field(b, field):
    try:
        for fn, wt, v in _proto_fields(bytes(b or b"")):
            if fn == field and wt == 0:
                return v
    except Exception:
        pass
    return 0


def _media_entries(b):
    """Media list of a new-style package: MediaEntries { repeated MediaEntry entries = 1 { string name = 1 } }."""
    out = []
    for fn, wt, v in _proto_fields(b):
        if fn == 1 and wt == 2:
            name = ""
            for f2, w2, v2 in _proto_fields(v):
                if f2 == 1 and w2 == 2:
                    name = v2.decode("utf-8", "replace")
            out.append(name)
    return out


# ---- sync between devices (the same format on the phone) ------------------------------------------

SYNC_VERSION = 1


def _sync_methods():
    def signature(self):
        """Changes whenever anything that syncs changes."""
        with self.lock:
            parts = [self.conn.execute(q).fetchone() for q in (
                "SELECT COUNT(*), COALESCE(MAX(mod),0) FROM decks", "SELECT COUNT(*), COALESCE(MAX(mod),0) FROM notes",
                "SELECT COUNT(*), COALESCE(MAX(mod),0) FROM cards", "SELECT COUNT(*), COALESCE(MAX(time),0) FROM revlog",
                "SELECT COUNT(*), COALESCE(MAX(time),0) FROM graves")]
        return "|".join(f"{a}:{round(b or 0, 3)}" for a, b in parts)

    def get_meta(self, k, default=None):
        with self.lock:
            r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
            self.conn.commit()

    def export_sync(self, device=""):
        """Everything, in the shared sync format."""
        with self.lock:
            q = lambda sql: [dict(r) for r in self.conn.execute(sql)]
            decks = q("SELECT guid, name, new_per_day, reviews_per_day, retention, mod FROM decks")
            notes = q("SELECT n.guid, d.guid AS deck, n.front, n.back, n.tags, n.reverse, n.created, n.mod "
                      "FROM notes n JOIN decks d ON d.id=n.deck_id")
            cards = q("SELECT n.guid AS note, c.ord, c.state, c.step, c.due, c.ivl, c.stability, c.difficulty, c.reps, "
                      "c.lapses, c.last_review, c.suspended, c.position, c.mod FROM cards c JOIN notes n ON n.id=c.note_id")
            revlog = q("SELECT n.guid AS note, c.ord, r.grade, r.state, r.ivl, r.took, r.time FROM revlog r "
                       "JOIN cards c ON c.id=r.card_id JOIN notes n ON n.id=c.note_id")
            graves = q("SELECT kind, guid, time FROM graves")
        media = sorted({m for n in notes for m in images_in(n["front"]) + images_in(n["back"])})
        return {"v": SYNC_VERSION, "device": device, "time": time.time(), "decks": decks, "notes": notes,
                "cards": cards, "revlog": revlog, "graves": graves, "media": media}

    def merge_sync(self, data):
        """Merges another device's collection into this one (newest change wins; nothing is lost twice).
        Returns a short summary."""
        if not data or data.get("v", 0) > SYNC_VERSION:
            return "skipped (made by a newer app version — update this app)"
        added = updated = removed = 0
        with self.lock:
            c = self.conn
            # 1. deletions
            for g in data.get("graves", []):
                old = c.execute("SELECT time FROM graves WHERE kind=? AND guid=?", (g["kind"], g["guid"])).fetchone()
                if not old or old[0] < g["time"]:
                    c.execute("INSERT OR REPLACE INTO graves VALUES(?,?,?)", (g["kind"], g["guid"], g["time"]))
                if g["kind"] == "note":
                    r = c.execute("SELECT id, mod FROM notes WHERE guid=?", (g["guid"],)).fetchone()
                    if r and (r[1] or 0) <= g["time"]:
                        c.execute("DELETE FROM cards WHERE note_id=?", (r[0],))
                        c.execute("DELETE FROM notes WHERE id=?", (r[0],))
                        removed += 1
                elif g["kind"] == "deck":
                    r = c.execute("SELECT id, mod FROM decks WHERE guid=?", (g["guid"],)).fetchone()
                    if r and (r[1] or 0) <= g["time"] and not c.execute(
                            "SELECT 1 FROM notes WHERE deck_id=? AND mod>?", (r[0], g["time"])).fetchone():
                        c.execute("DELETE FROM cards WHERE deck_id=?", (r[0],))
                        c.execute("DELETE FROM notes WHERE deck_id=?", (r[0],))
                        c.execute("DELETE FROM decks WHERE id=?", (r[0],))
            graves = {(k, g) for k, g in c.execute("SELECT kind, guid FROM graves")}
            # 2. decks (matched by id, or by name for decks made separately on each device)
            deck_id = {}
            for d in data.get("decks", []):
                if ("deck", d["guid"]) in graves and not any(n.get("deck") == d["guid"] for n in data.get("notes", [])):
                    continue
                r = c.execute("SELECT id, guid, mod, name FROM decks WHERE guid=?", (d["guid"],)).fetchone() or \
                    c.execute("SELECT id, guid, mod, name FROM decks WHERE name=? COLLATE NOCASE", (d["name"],)).fetchone()
                if r:
                    did = r[0]
                    if r[1] != d["guid"] and d["guid"] < (r[1] or "~"):
                        c.execute("UPDATE decks SET guid=? WHERE id=?", (d["guid"], did))    # both settle on one id
                    if (d.get("mod") or 0) > (r[2] or 0):
                        clash = c.execute("SELECT id FROM decks WHERE name=? COLLATE NOCASE AND id!=?",
                                          (d["name"], did)).fetchone()
                        c.execute("UPDATE decks SET name=?, new_per_day=?, reviews_per_day=?, retention=?, mod=? WHERE id=?",
                                  (r[3] if clash else d["name"], d["new_per_day"], d["reviews_per_day"], d["retention"],
                                   d["mod"], did))
                else:
                    did = c.execute("INSERT INTO decks(guid, name, new_per_day, reviews_per_day, retention, created, mod) "
                                    "VALUES(?,?,?,?,?,?,?)", (d["guid"], d["name"], d["new_per_day"], d["reviews_per_day"],
                                                              d["retention"], time.time(), d.get("mod") or 0)).lastrowid
                deck_id[d["guid"]] = did
            # 3. notes
            note_id = {}
            for n in data.get("notes", []):
                if ("note", n["guid"]) in graves:
                    continue
                did = deck_id.get(n["deck"])
                if did is None:
                    continue
                r = c.execute("SELECT id, mod FROM notes WHERE guid=?", (n["guid"],)).fetchone()
                if not r:
                    nid = c.execute("INSERT INTO notes(guid, deck_id, front, back, tags, reverse, created, mod) "
                                    "VALUES(?,?,?,?,?,?,?,?)", (n["guid"], did, n["front"], n["back"], n["tags"] or "",
                                                                n["reverse"], n.get("created") or 0, n.get("mod") or 0)).lastrowid
                    added += 1
                else:
                    nid = r[0]
                    if (n.get("mod") or 0) > (r[1] or 0):
                        c.execute("UPDATE notes SET deck_id=?, front=?, back=?, tags=?, reverse=?, mod=? WHERE id=?",
                                  (did, n["front"], n["back"], n["tags"] or "", n["reverse"], n["mod"], nid))
                        c.execute("UPDATE cards SET deck_id=? WHERE note_id=?", (did, nid))
                        if not n["reverse"]:
                            c.execute("DELETE FROM cards WHERE note_id=? AND ord=1", (nid,))
                        updated += 1
                note_id[n["guid"]] = nid
            # 4. cards (scheduling: the most recent change wins)
            for k in data.get("cards", []):
                nid = note_id.get(k["note"])
                if nid is None:
                    continue
                r = c.execute("SELECT id, mod FROM cards WHERE note_id=? AND ord=?", (nid, k["ord"])).fetchone()
                vals = (k["state"], k["step"], k["due"], k["ivl"], k["stability"], k["difficulty"], k["reps"], k["lapses"],
                        k["last_review"], k["suspended"], k.get("mod") or 0)
                deck = c.execute("SELECT deck_id FROM notes WHERE id=?", (nid,)).fetchone()[0]
                if not r:
                    c.execute("INSERT INTO cards(note_id, ord, deck_id, state, step, due, ivl, stability, difficulty, reps, "
                              "lapses, last_review, suspended, mod, position) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (nid, k["ord"], deck) + vals + (k.get("position") or 0,))
                elif (k.get("mod") or 0) > (r[1] or 0):
                    c.execute("UPDATE cards SET state=?, step=?, due=?, ivl=?, stability=?, difficulty=?, reps=?, lapses=?, "
                              "last_review=?, suspended=?, mod=? WHERE id=?", vals + (r[0],))
            # 5. review history (every answer on every device counts)
            have = {(a, b, round(t, 3)) for a, b, t in c.execute(
                "SELECT n.guid, c.ord, r.time FROM revlog r JOIN cards c ON c.id=r.card_id JOIN notes n ON n.id=c.note_id")}
            for rv in data.get("revlog", []):
                key = (rv["note"], rv["ord"], round(rv["time"], 3))
                if key in have or rv["note"] not in note_id:
                    continue
                cid = c.execute("SELECT id FROM cards WHERE note_id=? AND ord=?", (note_id[rv["note"]], rv["ord"])).fetchone()
                if cid:
                    c.execute("INSERT INTO revlog(card_id, grade, state, ivl, took, time) VALUES(?,?,?,?,?,?)",
                              (cid[0], rv["grade"], rv["state"], rv["ivl"], rv["took"], rv["time"]))
                    have.add(key)
            c.commit()
        return f"{added} new, {updated} changed, {removed} deleted"

    for f in (signature, get_meta, set_meta, export_sync, merge_sync):
        setattr(Collection, f.__name__, f)


_sync_methods()
