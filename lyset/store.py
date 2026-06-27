"""
SQLite-backed history store for poll snapshots.

Saves every poll to lyset_history.db at the project root.
Data is kept indefinitely until manually deleted.
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
        _con.execute(
            # ts_ms is UTC milliseconds (JS-compatible); resolution is "15m" or "1h"
            'CREATE TABLE IF NOT EXISTS prices '
            '(ts_ms INTEGER PRIMARY KEY, import_dkk REAL, export_dkk REAL, '
            ' spot_est REAL, resolution TEXT, forecast INTEGER)'
        )
        _con.execute(
            # pv_w/p10_w/p90_w are Watts (converted from Solcast kW); ts_ms = period_end UTC ms
            'CREATE TABLE IF NOT EXISTS solar_forecast '
            '(ts_ms INTEGER PRIMARY KEY, pv_w REAL, p10_w REAL, p90_w REAL)'
        )
        _con.execute(
            # w = predicted grid import in Watts; ts_ms = UTC ms of 15-min slot start
            'CREATE TABLE IF NOT EXISTS consumption_forecast '
            '(ts_ms INTEGER PRIMARY KEY, w REAL)'
        )
        _con.execute(
            # Power simulation output per 30-min slot.
            # Past slots use INSERT OR IGNORE (keep first/earliest prediction for comparison).
            # Future slots use INSERT OR REPLACE (keep latest prediction as it refines).
            'CREATE TABLE IF NOT EXISTS power_forecast '
            '(ts_ms INTEGER PRIMARY KEY, soc REAL, batt_w REAL, grid_w REAL)'
        )
        _con.commit()
    return _con


def init():
    with _lock:
        _get_con()


def save(ts: float, data: dict):
    with _lock:
        con = _get_con()
        con.execute(
            'INSERT OR REPLACE INTO polls VALUES (?, ?)',
            (ts, json.dumps(data, default=str)),
        )
        con.commit()


def save_prices(records: list[dict]):
    """Upsert a batch of price records from PriceWorker."""
    with _lock:
        con = _get_con()
        for r in records:
            con.execute(
                'INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?, ?)',
                (r['ts'], r['import'], r['export'],
                 r.get('spot_est', 0.0), r.get('resolution', '1h'),
                 1 if r.get('forecast') else 0),
            )
        con.commit()


def load_prices(from_ms: int, to_ms: int) -> list[dict]:
    """Return stored price records in [from_ms, to_ms] (UTC milliseconds)."""
    with _lock:
        rows = _get_con().execute(
            'SELECT ts_ms, import_dkk, export_dkk, spot_est, resolution, forecast '
            'FROM prices WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms',
            (from_ms, to_ms),
        ).fetchall()
    return [
        {
            'ts': ts, 'import': imp, 'export': exp,
            'spot_est': spe, 'resolution': res, 'forecast': bool(fc),
        }
        for ts, imp, exp, spe, res, fc in rows
    ]


def save_solar_forecast(records: list[dict]):
    """
    Persist solar forecast records.
    Past slots use INSERT OR IGNORE — keeps the first (earliest) prediction so
    historical forecasts remain visible on charts even after later fetches.
    Future slots use INSERT OR REPLACE — always reflects the latest Solcast data.
    """
    now_ms = int(time.time() * 1000)
    with _lock:
        con = _get_con()
        for r in records:
            if r['ts_ms'] <= now_ms:
                con.execute(
                    'INSERT OR IGNORE INTO solar_forecast VALUES (?, ?, ?, ?)',
                    (r['ts_ms'], r['pv_w'], r.get('p10_w'), r.get('p90_w')),
                )
            else:
                con.execute(
                    'INSERT OR REPLACE INTO solar_forecast VALUES (?, ?, ?, ?)',
                    (r['ts_ms'], r['pv_w'], r.get('p10_w'), r.get('p90_w')),
                )
        con.commit()


def load_solar_forecast(from_ms: int, to_ms: int) -> list[dict]:
    """Return stored solar forecast records in [from_ms, to_ms] (UTC milliseconds)."""
    with _lock:
        rows = _get_con().execute(
            'SELECT ts_ms, pv_w, p10_w, p90_w FROM solar_forecast '
            'WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms',
            (from_ms, to_ms),
        ).fetchall()
    return [
        {'ts_ms': ts, 'pv_w': pv, 'p10_w': p10, 'p90_w': p90}
        for ts, pv, p10, p90 in rows
    ]


def save_consumption_forecast(records: list[dict]):
    """Insert consumption forecast records, keeping the first prediction per slot.
    INSERT OR IGNORE preserves the original ~24h-ahead prediction so it can be
    compared against actual measurements once the slot has passed.
    """
    with _lock:
        con = _get_con()
        for r in records:
            if r.get('w') is not None:
                con.execute(
                    'INSERT OR IGNORE INTO consumption_forecast VALUES (?, ?)',
                    (r['ts_ms'], r['w']),
                )
        con.commit()


def load_consumption_forecast(from_ms: int, to_ms: int) -> list[dict]:
    """Return stored consumption forecast records in [from_ms, to_ms]."""
    with _lock:
        rows = _get_con().execute(
            'SELECT ts_ms, w FROM consumption_forecast '
            'WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms',
            (from_ms, to_ms),
        ).fetchall()
    return [{'ts_ms': ts, 'w': w} for ts, w in rows]


def save_power_forecast(records: list[dict], now_ms: int):
    """
    Persist power simulation predictions.
    Past slots (ts_ms <= now_ms): INSERT OR IGNORE — keeps earliest prediction for later comparison.
    Future slots (ts_ms > now_ms): INSERT OR REPLACE — keeps latest prediction as it refines.
    """
    with _lock:
        con = _get_con()
        for r in records:
            soc  = r.get('soc')
            bw   = r.get('batt_w')
            gw   = r.get('grid_w')
            if soc is None:
                continue
            if r['ts_ms'] <= now_ms:
                con.execute(
                    'INSERT OR IGNORE INTO power_forecast VALUES (?, ?, ?, ?)',
                    (r['ts_ms'], soc, bw, gw),
                )
            else:
                con.execute(
                    'INSERT OR REPLACE INTO power_forecast VALUES (?, ?, ?, ?)',
                    (r['ts_ms'], soc, bw, gw),
                )
        con.commit()


def load_power_forecast(from_ms: int, to_ms: int) -> list[dict]:
    with _lock:
        rows = _get_con().execute(
            'SELECT ts_ms, soc, batt_w, grid_w FROM power_forecast '
            'WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms',
            (from_ms, to_ms),
        ).fetchall()
    return [{'ts_ms': ts, 'soc': soc, 'batt_w': bw, 'grid_w': gw}
            for ts, soc, bw, gw in rows]


def clear_history():
    """Delete all inverter poll records."""
    with _lock:
        con = _get_con()
        con.execute('DELETE FROM polls')
        con.commit()


def load_last_24h() -> list[tuple[float, dict]]:
    cutoff = time.time() - 86400
    with _lock:
        rows = _get_con().execute(
            'SELECT ts, data FROM polls WHERE ts > ? ORDER BY ts', (cutoff,)
        ).fetchall()
    return [(ts, json.loads(d)) for ts, d in rows]
