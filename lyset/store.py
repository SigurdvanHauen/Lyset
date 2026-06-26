"""
SQLite-backed history store for poll snapshots.

Saves every poll to lyset_history.db at the project root.
Rows older than 24 h are pruned on each write.
Thread-safe: a single shared connection protected by a Lock.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / 'lyset_history.db'
_lock = threading.Lock()
_con: sqlite3.Connection | None = None


def _get_con() -> sqlite3.Connection:
    global _con
    if _con is None:
        _con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _con.execute('PRAGMA journal_mode=WAL')
        _con.execute(
            'CREATE TABLE IF NOT EXISTS polls '
            '(ts REAL PRIMARY KEY, data TEXT NOT NULL)'
        )
        _con.commit()
    return _con


def init():
    with _lock:
        _get_con()


def save(ts: float, data: dict):
    cutoff = time.time() - 86400
    with _lock:
        con = _get_con()
        con.execute(
            'INSERT OR REPLACE INTO polls VALUES (?, ?)',
            (ts, json.dumps(data, default=str)),
        )
        con.execute('DELETE FROM polls WHERE ts < ?', (cutoff,))
        con.commit()


def load_last_24h() -> list[tuple[float, dict]]:
    cutoff = time.time() - 86400
    with _lock:
        rows = _get_con().execute(
            'SELECT ts, data FROM polls WHERE ts > ? ORDER BY ts', (cutoff,)
        ).fetchall()
    return [(ts, json.loads(d)) for ts, d in rows]
