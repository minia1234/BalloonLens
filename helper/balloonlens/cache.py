"""SQLite cache for OCR and translation results (section 13)."""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr (
  key TEXT PRIMARY KEY,
  result TEXT NOT NULL,
  created REAL NOT NULL,
  used REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tr (
  key TEXT PRIMARY KEY,
  text TEXT NOT NULL,
  created REAL NOT NULL,
  used REAL NOT NULL
);
-- User fixes (FR-08/09). tgt_lang '' = corrected OCR source text; otherwise a hand-edited translation.
CREATE TABLE IF NOT EXISTS edits (
  image_hash TEXT NOT NULL,
  src_lang TEXT NOT NULL,
  tgt_lang TEXT NOT NULL,
  bbox TEXT NOT NULL,
  text TEXT NOT NULL,
  updated REAL NOT NULL,
  PRIMARY KEY (image_hash, src_lang, tgt_lang, bbox)
);
-- Reader corrections to detected regions for one image (see layout.py).
CREATE TABLE IF NOT EXISTS layouts (
  image_hash TEXT NOT NULL,
  src_lang TEXT NOT NULL,
  layout TEXT NOT NULL,
  updated REAL NOT NULL,
  PRIMARY KEY (image_hash, src_lang)
);
-- Reader-confirmed notes per work (scope = registered work URL).
CREATE TABLE IF NOT EXISTS works (
  scope TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  updated REAL NOT NULL
);
-- Translated pages of a work, used as context for later pages (see memory.py).
CREATE TABLE IF NOT EXISTS pages (
  scope TEXT NOT NULL,
  image_hash TEXT NOT NULL,
  src_lang TEXT NOT NULL,
  tgt_lang TEXT NOT NULL,
  page_url TEXT NOT NULL,
  position INTEGER NOT NULL,
  ord TEXT NOT NULL,
  rules TEXT NOT NULL,
  lines TEXT NOT NULL,
  seen REAL NOT NULL,
  PRIMARY KEY (scope, image_hash, src_lang, tgt_lang)
);
-- v0.5.0: subscription backends. The usage ledger keeps counts and tokens only, never dialogue text.
CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  backend TEXT NOT NULL,
  model TEXT NOT NULL,
  lines INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cached_tokens INTEGER NOT NULL,
  seconds REAL NOT NULL,
  outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_by_backend_ts ON usage_events (backend, ts);
CREATE TABLE IF NOT EXISTS backend_state (
  backend TEXT PRIMARY KEY,
  paused_until REAL NOT NULL,
  reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backend_caps (
  backend TEXT PRIMARY KEY,
  per_5h INTEGER NOT NULL,
  per_7d INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS calibration (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backend TEXT NOT NULL,
  ts REAL NOT NULL,
  percent REAL NOT NULL,
  calls_total INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS backend_flags (
  backend TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
"""
PAGE_COLUMNS =("scope", "image_hash", "src_lang", "tgt_lang", "page_url", "position", "ord", "rules", "lines", "seen")


def bbox_key(bbox) -> str:
    return ",".join(str(int(v)) for v in bbox)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ocr_key(image_hash: str, src_lang: str, ocr_version: str) -> str:
    return sha256(json.dumps([image_hash, src_lang, ocr_version]).encode())


def tr_key(text: str, src: str, tgt: str, model: str, prompt_version: str, scope: str) -> str:
    # scope (e.g. site/work id) keeps identical sentences from being force-shared across works.
    return sha256(json.dumps([text, src, tgt, model, prompt_version, scope], ensure_ascii=False).encode())


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.execute("PRAGMA user_version = 2")  # 1 = v0.4 tables, 2 = v0.5 subscription tables
        self._lock = threading.Lock()

    def _get(self, table: str, col: str, key: str):
        with self._lock:
            row = self._db.execute(f"SELECT {col} FROM {table} WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            self._db.execute(f"UPDATE {table} SET used=? WHERE key=?", (time.time(), key))
            self._db.commit()
            return row[0]

    def _put(self, table: str, col: str, key: str, value: str):
        now = time.time()
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO {table} (key, {col}, created, used) VALUES (?, ?, ?, ?)",
                (key, value, now, now),
            )
            self._db.commit()

    def get_ocr(self, key: str):
        raw = self._get("ocr", "result", key)
        return json.loads(raw) if raw is not None else None

    def put_ocr(self, key: str, segments: list):
        self._put("ocr", "result", key, json.dumps(segments, ensure_ascii=False))

    def get_tr(self, key: str):
        return self._get("tr", "text", key)

    def put_tr(self, key: str, text: str):
        self._put("tr", "text", key, text)

    def put_edit(self, image_hash: str, src: str, tgt: str, bbox, text: str | None):
        """text=None removes the edit. tgt='' stores a source (OCR) correction."""
        args = (image_hash, src, tgt, bbox_key(bbox))
        with self._lock:
            if text is None:
                self._db.execute("DELETE FROM edits WHERE image_hash=? AND src_lang=? AND tgt_lang=? AND bbox=?", args)
            else:
                self._db.execute("INSERT OR REPLACE INTO edits VALUES (?, ?, ?, ?, ?, ?)", (*args, text, time.time()))
            self._db.commit()

    def get_edits(self, image_hash: str, src: str, tgt: str) -> dict:
        with self._lock:
            rows = self._db.execute(
                "SELECT tgt_lang, bbox, text FROM edits WHERE image_hash=? AND src_lang=? AND tgt_lang IN ('', ?)",
                (image_hash, src, tgt),
            ).fetchall()
        out = {"src": {}, "tgt": {}}
        for lang, box, text in rows:
            out["tgt" if lang else "src"][box] = text
        return out

    def move_edits(self, image_hash: str, src: str, old_key: str, new_bbox) -> int:
        """A reader moved or resized a region: its source and translation fixes follow it."""
        new_key = bbox_key(new_bbox)
        if old_key == new_key:
            return 0
        with self._lock:
            rows = self._db.execute("SELECT tgt_lang, text FROM edits WHERE image_hash=? AND src_lang=? AND bbox=?",
                                    (image_hash, src, old_key)).fetchall()
            for lang, text in rows:
                self._db.execute("INSERT OR REPLACE INTO edits VALUES (?, ?, ?, ?, ?, ?)",
                                 (image_hash, src, lang, new_key, text, time.time()))
            self._db.execute("DELETE FROM edits WHERE image_hash=? AND src_lang=? AND bbox=?", (image_hash, src, old_key))
            self._db.commit()
        return len(rows)

    # ----- region layouts -----
    def get_layout(self, image_hash: str, src: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT layout FROM layouts WHERE image_hash=? AND src_lang=?",
                                   (image_hash, src)).fetchone()
        return json.loads(row[0]) if row else None

    def put_layout(self, image_hash: str, src: str, layout: dict | None):
        with self._lock:
            if layout is None:
                self._db.execute("DELETE FROM layouts WHERE image_hash=? AND src_lang=?", (image_hash, src))
            else:
                self._db.execute("INSERT OR REPLACE INTO layouts VALUES (?, ?, ?, ?)",
                                 (image_hash, src, json.dumps(layout), time.time()))
            self._db.commit()

    # ----- work memory -----
    def get_work(self, scope: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM works WHERE scope=?", (scope,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_work(self, scope: str, data: dict):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO works VALUES (?, ?, ?)",
                             (scope, json.dumps(data, ensure_ascii=False), time.time()))
            self._db.commit()

    def delete_work(self, scope: str):
        with self._lock:
            self._db.execute("DELETE FROM works WHERE scope=?", (scope,))
            self._db.execute("DELETE FROM pages WHERE scope=?", (scope,))
            self._db.commit()

    def put_page(self, scope, image_hash, src, tgt, page_url, position, order, rules, lines, keep=300):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (scope, image_hash, src, tgt, page_url, position, json.dumps(order), rules,
                              json.dumps(lines, ensure_ascii=False), time.time()))
            # Bounded per work: forget the least recently seen pages.
            self._db.execute("""DELETE FROM pages WHERE scope=? AND image_hash NOT IN (
                                  SELECT image_hash FROM pages WHERE scope=? ORDER BY seen DESC LIMIT ?)""",
                             (scope, scope, keep))
            self._db.commit()

    def _page_rows(self, where: str, args: tuple) -> list[dict]:
        with self._lock:
            rows = self._db.execute(f"SELECT {', '.join(PAGE_COLUMNS)} FROM pages WHERE {where}", args).fetchall()
        pages = []
        for row in rows:
            page = dict(zip(PAGE_COLUMNS, row))
            page["order"] = json.loads(page.pop("ord"))
            page["lines"] = json.loads(page["lines"])
            pages.append(page)
        return pages

    def get_page(self, scope, image_hash, src, tgt) -> dict | None:
        rows = self._page_rows("scope=? AND image_hash=? AND src_lang=? AND tgt_lang=?", (scope, image_hash, src, tgt))
        return rows[0] if rows else None

    def recent_pages(self, scope, src, tgt, limit=60) -> list[dict]:
        return self._page_rows("scope=? AND src_lang=? AND tgt_lang=? ORDER BY seen DESC LIMIT ?",
                               (scope, src, tgt, limit))

    def page_stats(self, scope) -> dict:
        with self._lock:
            count, last = self._db.execute("SELECT COUNT(*), MAX(seen) FROM pages WHERE scope=?", (scope,)).fetchone()
        return {"count": count, "last_seen": last}

    def clear_pages(self, scope):
        with self._lock:
            self._db.execute("DELETE FROM pages WHERE scope=?", (scope,))
            self._db.commit()

    def update_page_line(self, scope, image_hash, src, tgt, key, src_text=None, tgt_text=None, clear=False):
        """Keep the stored page in step with a reader's edit so later pages see the confirmed wording."""
        page = self.get_page(scope, image_hash, src, tgt)
        if not page:
            return
        for line in page["lines"]:
            if line["key"] != key:
                continue
            if clear:
                line["edited"] = False
            else:
                if src_text:
                    line["src"] = src_text
                if tgt_text:
                    line.update(tgt=tgt_text, status="done", edited=True)
        with self._lock:
            self._db.execute("UPDATE pages SET lines=? WHERE scope=? AND image_hash=? AND src_lang=? AND tgt_lang=?",
                             (json.dumps(page["lines"], ensure_ascii=False), scope, image_hash, src, tgt))
            self._db.commit()

    # ----- subscription backends (v0.5.0) -----
    def record_usage(self, ts, backend, model, lines, usage, seconds, outcome):
        usage = usage or {}
        with self._lock:
            self._db.execute(
                "INSERT INTO usage_events (ts, backend, model, lines, input_tokens, output_tokens, cached_tokens, "
                "seconds, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (float(ts), backend, str(model)[:60], int(lines), int(usage.get("input", 0)),
                 int(usage.get("output", 0)), int(usage.get("cached", 0)), float(seconds or 0), str(outcome)[:40]))
            self._db.commit()

    def count_usage(self, backend, since) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM usage_events WHERE backend=? AND ts>=?",
                                    (backend, since)).fetchone()[0]

    def oldest_usage(self, backend, since):
        with self._lock:
            row = self._db.execute("SELECT MIN(ts) FROM usage_events WHERE backend=? AND ts>=?",
                                   (backend, since)).fetchone()
        return row[0] if row and row[0] is not None else None

    def usage_summary(self, backend, *, since_7d, since_5h, since_today) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(CASE WHEN ts>=:h5 THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN ts>=:today THEN 1 ELSE 0 END), 0), "
                "COUNT(*), COALESCE(SUM(lines), 0), COALESCE(SUM(input_tokens), 0), "
                "COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cached_tokens), 0) "
                "FROM usage_events WHERE backend=:b AND ts>=:d7",
                {"h5": since_5h, "today": since_today, "b": backend, "d7": since_7d}).fetchone()
        return {"calls_5h": row[0], "calls_today": row[1], "calls_7d": row[2], "lines_7d": row[3],
                "input_tokens_7d": row[4], "output_tokens_7d": row[5], "cached_tokens_7d": row[6]}

    def recent_outcomes(self, backend, limit) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT outcome FROM usage_events WHERE backend=? ORDER BY id DESC LIMIT ?",
                                    (backend, int(limit))).fetchall()
        return [row[0] for row in rows]

    def total_usage(self, backend) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM usage_events WHERE backend=?", (backend,)).fetchone()[0]

    def get_pause(self, backend):
        with self._lock:
            row = self._db.execute("SELECT paused_until, reason FROM backend_state WHERE backend=?", (backend,)).fetchone()
        return {"until": row[0], "reason": row[1]} if row else None

    def put_pause(self, backend, until, reason):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO backend_state VALUES (?, ?, ?)",
                             (backend, float(until), str(reason)[:200]))
            self._db.commit()

    def clear_pause(self, backend):
        with self._lock:
            self._db.execute("DELETE FROM backend_state WHERE backend=?", (backend,))
            self._db.commit()

    def get_caps(self, backend):
        with self._lock:
            row = self._db.execute("SELECT per_5h, per_7d FROM backend_caps WHERE backend=?", (backend,)).fetchone()
        return {"per_5h": row[0], "per_7d": row[1]} if row else None

    def put_caps(self, backend, per_5h, per_7d):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO backend_caps VALUES (?, ?, ?)", (backend, int(per_5h), int(per_7d)))
            self._db.commit()

    def add_calibration(self, backend, ts, percent, calls_total):
        with self._lock:
            self._db.execute("INSERT INTO calibration (backend, ts, percent, calls_total) VALUES (?, ?, ?, ?)",
                             (backend, float(ts), float(percent), int(calls_total)))
            self._db.commit()

    def list_calibration(self, backend, limit=20) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT ts, percent, calls_total FROM calibration WHERE backend=? "
                                    "ORDER BY id DESC LIMIT ?", (backend, int(limit))).fetchall()
        return [{"ts": r[0], "percent": r[1], "calls_total": r[2]} for r in rows]

    def get_flags(self, backend) -> dict:
        with self._lock:
            row = self._db.execute("SELECT data FROM backend_flags WHERE backend=?", (backend,)).fetchone()
        return json.loads(row[0]) if row else {}

    def put_flags(self, backend, data: dict):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO backend_flags VALUES (?, ?)",
                             (backend, json.dumps(data, ensure_ascii=False)))
            self._db.commit()
