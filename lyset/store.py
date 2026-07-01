"""
SQLite-backed history store for poll snapshots.

Saves every poll to lyset_history.db at the project root.
Data is kept indefinitely until manually deleted.
Thread-safe: a single shared connection protected by a Lock.
"""

import json
import sqlite3
import statistics
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
        _con.execute(
            # One row per local calendar day. yield_kwh = inverter daily counter (register 32114);
            # forecast_kwh = Solcast full-day sum at the time of last update.
            'CREATE TABLE IF NOT EXISTS daily_solar '
            '(date TEXT PRIMARY KEY, yield_kwh REAL, forecast_kwh REAL)'
        )
        _con.execute(
            # Auto-controller command log. mode is one of:
            # 'export_unlimited' | 'export_limited' | 'grid_charge'
            'CREATE TABLE IF NOT EXISTS auto_commands '
            '(ts REAL, mode TEXT, detail TEXT)'
        )
        _con.commit()
    return _con


def init():
    with _lock:
        _get_con()


def backup_to(dest_path: str):
    """Write a consistent snapshot of the live DB to dest_path using SQLite's
    online backup API — safe to call while polls are still being written (the
    WAL is checkpointed into the copy). Used by the data-download endpoint."""
    with _lock:
        src = _get_con()
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()


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


def upsert_daily_solar(date: str, yield_kwh: float | None = None, forecast_kwh: float | None = None):
    """Insert or update a daily solar record.  Only non-None arguments overwrite existing values."""
    with _lock:
        con = _get_con()
        con.execute(
            'INSERT INTO daily_solar (date, yield_kwh, forecast_kwh) VALUES (?, ?, ?)'
            ' ON CONFLICT(date) DO UPDATE SET'
            '  yield_kwh    = COALESCE(excluded.yield_kwh,    yield_kwh),'
            '  forecast_kwh = COALESCE(excluded.forecast_kwh, forecast_kwh)',
            (date, yield_kwh, forecast_kwh),
        )
        con.commit()


def load_daily_solar(days: int = 30) -> list[dict]:
    """Return the most recent `days` daily solar records, oldest first."""
    with _lock:
        rows = _get_con().execute(
            'SELECT date, yield_kwh, forecast_kwh FROM daily_solar'
            ' ORDER BY date DESC LIMIT ?',
            (days,),
        ).fetchall()
    return [{'date': d, 'yield_kwh': y, 'forecast_kwh': f} for d, y, f in reversed(rows)]


def save_auto_command(ts: float, mode: str, detail: str):
    """Append one auto-controller decision to the command log."""
    with _lock:
        con = _get_con()
        con.execute('INSERT INTO auto_commands VALUES (?, ?, ?)', (ts, mode, detail))
        con.commit()


def load_auto_commands(from_ts: float) -> list[dict]:
    """Return auto-controller commands since from_ts (Unix seconds), oldest first."""
    with _lock:
        rows = _get_con().execute(
            'SELECT ts, mode, detail FROM auto_commands WHERE ts >= ? ORDER BY ts',
            (from_ts,),
        ).fetchall()
    return [{'ts': ts, 'mode': mode, 'detail': detail} for ts, mode, detail in rows]


def purge_power_outliers(threshold_w: float = 10_000) -> int:
    """Delete poll rows where any power field exceeds threshold_w.

    I32 Modbus reads occasionally return garbage values in the millions of watts
    (register word-order corruption).  10 kW is well above the 6.9 kWp system
    peak while cleanly rejecting those glitches.  Returns the number of rows deleted.
    """
    with _lock:
        con = _get_con()
        cur = con.execute(
            'DELETE FROM polls WHERE '
            '  ABS(COALESCE(json_extract(data, "$.active_power"),       0)) > ? OR '
            '  ABS(COALESCE(json_extract(data, "$.meter_active_power"), 0)) > ? OR '
            '  ABS(COALESCE(json_extract(data, "$.batt_power"),         0)) > ?',
            (threshold_w, threshold_w, threshold_w),
        )
        con.commit()
        return cur.rowcount


def clean_soc_history() -> int:
    """Rewrite batt_soc spikes in-place using a 5-point sliding median.

    For each row, if the stored SoC differs from the median of the 5 surrounding
    values by more than 0.5 %, overwrite it with that median.  Unlike the forward
    rate filter, this handles multi-poll drift because each value is judged against
    its neighbours rather than against the last accepted value.
    Returns the number of rows updated.
    """
    HALF = 2  # 5-point window: 2 before + self + 2 after
    with _lock:
        con = _get_con()
        rows = con.execute(
            'SELECT ts, json_extract(data, "$.batt_soc") FROM polls '
            'WHERE json_extract(data, "$.batt_soc") IS NOT NULL '
            'ORDER BY ts'
        ).fetchall()

        if len(rows) < 3:
            return 0

        socs = [r[1] for r in rows]
        n    = len(socs)
        updates: list[tuple[float, float]] = []

        for i, (ts, original) in enumerate(rows):
            lo  = max(0, i - HALF)
            hi  = min(n, i + HALF + 1)
            med = round(statistics.median(socs[lo:hi]), 1)
            if abs(med - original) >= 0.5:
                updates.append((med, ts))

        for corrected, ts in updates:
            con.execute(
                'UPDATE polls SET data = json_set(data, "$.batt_soc", ?) WHERE ts = ?',
                (corrected, ts),
            )
        if updates:
            con.commit()
        return len(updates)


def clean_forecast_soc() -> int:
    """Rewrite power_forecast soc spikes in-place using a 5-point sliding median.

    Returns the number of rows updated.
    """
    HALF = 2
    with _lock:
        con = _get_con()
        rows = con.execute(
            'SELECT ts_ms, soc FROM power_forecast WHERE soc IS NOT NULL ORDER BY ts_ms'
        ).fetchall()

        if len(rows) < 3:
            return 0

        socs = [r[1] for r in rows]
        n    = len(socs)
        updates: list[tuple[float, int]] = []

        for i, (ts_ms, original) in enumerate(rows):
            lo  = max(0, i - HALF)
            hi  = min(n, i + HALF + 1)
            med = round(statistics.median(socs[lo:hi]), 1)
            if abs(med - original) >= 0.5:
                updates.append((med, ts_ms))

        for corrected, ts_ms in updates:
            con.execute('UPDATE power_forecast SET soc = ? WHERE ts_ms = ?', (corrected, ts_ms))
        if updates:
            con.commit()
        return len(updates)


def load_house_load_history(from_ts: float = 0.0) -> list[tuple[float, float]]:
    """Return (ts_utc, house_load_w) tuples for all poll rows that have house_load."""
    with _lock:
        rows = _get_con().execute(
            'SELECT ts, json_extract(data, "$.house_load") FROM polls '
            'WHERE json_extract(data, "$.house_load") IS NOT NULL '
            'AND ts >= ? ORDER BY ts',
            (from_ts,),
        ).fetchall()
    return [(ts, w) for ts, w in rows]


def load_power_avg_buckets(from_ts: float, to_ts: float,
                           step_s: int = 900) -> dict[int, tuple[float, float]]:
    """Average house_load and meter_active_power (W) over fixed step_s windows.

    Returns {bucket_start_ms: (house_load_w, grid_w)} for the savings calculation.
    Grouping in SQLite keeps a year of 10-s polls fast to aggregate. Only buckets
    with both fields present are returned; gaps (downtime) are simply absent.
    """
    with _lock:
        rows = _get_con().execute(
            'SELECT CAST(ts / ? AS INTEGER) AS b, '
            '       AVG(json_extract(data, "$.house_load")), '
            '       AVG(json_extract(data, "$.meter_active_power")) '
            'FROM polls WHERE ts >= ? AND ts < ? '
            '  AND json_extract(data, "$.house_load")       IS NOT NULL '
            '  AND json_extract(data, "$.meter_active_power") IS NOT NULL '
            'GROUP BY b ORDER BY b',
            (step_s, from_ts, to_ts),
        ).fetchall()
    return {int(b) * step_s * 1000: (hl, gp) for b, hl, gp in rows}


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


def load_series(key: str, from_ts: float = 0.0, max_points: int = 8000) -> list[tuple[int, float]]:
    """
    Return the full history of a single numeric poll field as (ts_ms, value) pairs.

    Pulls every poll that has the field set (the stored JSON includes the computed
    keys too, e.g. pv1_power / house_load), so this serves the entire retained
    history rather than just the last 24 h.  When the result exceeds max_points it
    is evenly strided down so the browser stays responsive over weeks of data;
    the most recent point is always kept.
    """
    with _lock:
        rows = _get_con().execute(
            'SELECT ts, json_extract(data, ?) FROM polls '
            'WHERE ts >= ? AND json_extract(data, ?) IS NOT NULL ORDER BY ts',
            (f'$.{key}', from_ts, f'$.{key}'),
        ).fetchall()

    n = len(rows)
    if n > max_points:
        stride = (n + max_points - 1) // max_points
        picked = rows[::stride]
        if picked[-1] is not rows[-1]:
            picked.append(rows[-1])   # always keep the latest sample
        rows = picked
    return [(int(ts * 1000), val) for ts, val in rows]
