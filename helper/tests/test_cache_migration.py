"""A cache file made by v0.4 must still open after the v0.5 tables are added, and keep its rows."""
import sqlite3

from balloonlens.cache import Cache


def test_v04_database_opens_keeps_its_rows_and_gains_the_new_tables(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE tr (key TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL, "
                     "used REAL NOT NULL); INSERT INTO tr VALUES ('k', '번역', 1, 1);")
    db.commit()
    db.close()

    cache = Cache(path)
    assert cache.get_tr("k") == "번역"
    assert cache.get_caps("claude") is None
    assert cache.get_pause("claude") is None
    assert cache.usage_summary("claude", since_7d=0, since_5h=0, since_today=0)["calls_7d"] == 0
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 2


def test_usage_ledger_keeps_counts_and_tokens_only(tmp_path):
    cache = Cache(tmp_path / "c.db")
    cache.record_usage(1000.0, "claude", "claude-haiku-5-5", 3, {"input": 5, "output": 2, "cached": 1}, 0.4, "ok")
    columns = [row[1] for row in cache._db.execute("PRAGMA table_info(usage_events)")]
    assert "text" not in columns and "src" not in columns and "dialogue" not in columns
    assert cache.total_usage("claude") == 1
