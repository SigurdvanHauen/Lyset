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
from datetime import date, timedelta
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
            # pv_w/p10_w/p90_w are Watts (converted from Solcast kW); ts_ms = period_end UTC ms.
            # raw_w is the UNCALIBRATED Solcast value; pv_w has the per-hour site
            # calibration applied. The calibration learner needs raw_w so each
            # observation is independent of the factor active when it was stored.
            'CREATE TABLE IF NOT EXISTS solar_forecast '
            '(ts_ms INTEGER PRIMARY KEY, pv_w REAL, p10_w REAL, p90_w REAL, raw_w REAL)'
        )
        try:  # migrate pre-calibration DBs (their pv_w was stored uncalibrated)
            _con.execute('ALTER TABLE solar_forecast ADD COLUMN raw_w REAL')
        except sqlite3.OperationalError:
            pass  # column already exists
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
        for _col in ('soc_p10', 'soc_p90', 'batt_w_p10', 'batt_w_p90',
                     'grid_w_p10', 'grid_w_p90'):
            try:  # migrate pre-band DBs — each confidence bound is a nullable column
                _con.execute(f'ALTER TABLE power_forecast ADD COLUMN {_col} REAL')
            except sqlite3.OperationalError:
                pass  # column already exists
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
        _con.execute(
            # EV charger cloud-poll history (EVChargerWorker). status is the raw
            # top-level FusionSolar device status code (meaning unconfirmed for
            # this device type). total_energy_kwh is a LIFETIME cumulative
            # counter, same semantics as the inverter's total_yield — "today's
            # energy" is derived by the app from this table's daily min/max, not
            # served directly. raw holds the full parsed signal dict as JSON so
            # a field this app doesn't recognise yet is still visible.
            'CREATE TABLE IF NOT EXISTS ev_charger_polls '
            '(ts REAL PRIMARY KEY, status INTEGER, total_energy_kwh REAL, '
            ' model TEXT, rated_power_kw REAL, sw_version TEXT, raw TEXT)'
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
            row = (r['ts_ms'], r['pv_w'], r.get('p10_w'), r.get('p90_w'),
                   r.get('raw_w', r['pv_w']))
            if r['ts_ms'] <= now_ms:
                con.execute(
                    'INSERT OR IGNORE INTO solar_forecast VALUES (?, ?, ?, ?, ?)', row,
                )
            else:
                con.execute(
                    'INSERT OR REPLACE INTO solar_forecast VALUES (?, ?, ?, ?, ?)', row,
                )
        con.commit()


def load_solar_forecast(from_ms: int, to_ms: int) -> list[dict]:
    """Return stored solar forecast records in [from_ms, to_ms] (UTC milliseconds)."""
    with _lock:
        rows = _get_con().execute(
            'SELECT ts_ms, pv_w, p10_w, p90_w, raw_w FROM solar_forecast '
            'WHERE ts_ms >= ? AND ts_ms <= ? ORDER BY ts_ms',
            (from_ms, to_ms),
        ).fetchall()
    return [
        {'ts_ms': ts, 'pv_w': pv, 'p10_w': p10, 'p90_w': p90, 'raw_w': raw}
        for ts, pv, p10, p90, raw in rows
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


def save_power_forecast(records: list[dict], now_ms: int, overwrite_past: bool = False):
    """
    Persist power simulation predictions.
    Past slots (ts_ms <= now_ms): INSERT OR IGNORE — keeps earliest prediction for later comparison.
    Future slots (ts_ms > now_ms): INSERT OR REPLACE — keeps latest prediction as it refines.

    overwrite_past=True forces INSERT OR REPLACE on past slots too, so a corrected
    re-simulation can flush stale/buggy frozen predictions (the past line is only ever
    a plan overlay, not a scoring record).

    The simulation's leading anchor row (batt_w None — the current-SoC start point) is
    NOT persisted: each regeneration would drop one at a fresh odd timestamp carrying the
    REAL SoC, and those, interleaved with the frozen predicted 15-min slots, made the
    plotted Predicted-SoC line saw-tooth against the smooth actual line.
    """
    with _lock:
        con = _get_con()
        for r in records:
            soc  = r.get('soc')
            if soc is None or r.get('batt_w') is None:
                continue
            vals = (
                r['ts_ms'], soc, r.get('batt_w'), r.get('grid_w'),
                r.get('soc_p10'), r.get('soc_p90'),
                r.get('batt_w_p10'), r.get('batt_w_p90'),
                r.get('grid_w_p10'), r.get('grid_w_p90'),
            )
            verb = ('INSERT OR IGNORE' if r['ts_ms'] <= now_ms and not overwrite_past
                    else 'INSERT OR REPLACE')
            con.execute(
                f'{verb} INTO power_forecast VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                vals,
            )
        con.commit()


def delete_power_forecast_anchors() -> int:
    """Remove legacy simulation anchor rows (batt_w IS NULL) — the real-SoC start
    points earlier builds persisted, which saw-toothed the plotted Predicted-SoC line.
    Called once on startup; new saves no longer create them. Returns rows deleted."""
    with _lock:
        con = _get_con()
        n = con.execute('DELETE FROM power_forecast WHERE batt_w IS NULL').rowcount
        con.commit()
    return n


def load_power_forecast(from_ms: int, to_ms: int) -> list[dict]:
    with _lock:
        rows = _get_con().execute(
            'SELECT ts_ms, soc, batt_w, grid_w, soc_p10, soc_p90, '
            'batt_w_p10, batt_w_p90, grid_w_p10, grid_w_p90 FROM power_forecast '
            'WHERE ts_ms >= ? AND ts_ms <= ? AND batt_w IS NOT NULL ORDER BY ts_ms',
            (from_ms, to_ms),
        ).fetchall()
    return [{'ts_ms': ts, 'soc': soc, 'batt_w': bw, 'grid_w': gw,
             'soc_p10': s10, 'soc_p90': s90,
             'batt_w_p10': b10, 'batt_w_p90': b90,
             'grid_w_p10': g10, 'grid_w_p90': g90}
            for ts, soc, bw, gw, s10, s90, b10, b90, g10, g90 in rows]


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


def save_ev_charger_poll(ts: float, data: dict):
    """Persist one EVChargerWorker poll (parse_realtime() output)."""
    with _lock:
        con = _get_con()
        con.execute(
            'INSERT OR REPLACE INTO ev_charger_polls VALUES (?, ?, ?, ?, ?, ?, ?)',
            (ts, data.get('status'), data.get('total_energy_charged'),
             data.get('model'), data.get('rated_power'), data.get('software_version'),
             json.dumps(data.get('raw'), default=str)),
        )
        con.commit()


def load_ev_charger_history(from_ts: float, to_ts: float) -> list[dict]:
    """Return EV charger poll history in [from_ts, to_ts] (Unix seconds)."""
    with _lock:
        rows = _get_con().execute(
            'SELECT ts, status, total_energy_kwh, model, rated_power_kw, sw_version '
            'FROM ev_charger_polls WHERE ts >= ? AND ts <= ? ORDER BY ts',
            (from_ts, to_ts),
        ).fetchall()
    return [
        {'ts': ts, 'status': status, 'total_energy_kwh': kwh,
         'model': model, 'rated_power_kw': rated_kw, 'sw_version': sw}
        for ts, status, kwh, model, rated_kw, sw in rows
    ]


def _ev_day_deltas() -> tuple[dict[str, float], float | None]:
    """Per-local-day EV-charged kWh plus the latest lifetime-counter reading.

    Each day's figure is the delta between that day's LAST reading and the
    previous day-with-data's last reading, so the series sums exactly to the
    counter's growth — energy charged across midnight or during a cloud-poll
    outage lands on the first day after the gap instead of being lost (the
    day-min/max approach would drop it). Days without any poll are absent
    from the dict (unknown), never a fake 0. The first day ever recorded
    uses its own first poll as the opening baseline so day-one charging
    still shows.
    """
    with _lock:
        con = _get_con()
        # SQLite guarantees the bare column comes from the max(ts) row.
        rows = con.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS d, total_energy_kwh, max(ts) "
            'FROM ev_charger_polls WHERE total_energy_kwh IS NOT NULL '
            'GROUP BY d ORDER BY d',
        ).fetchall()
        first = con.execute(
            'SELECT total_energy_kwh FROM ev_charger_polls '
            'WHERE total_energy_kwh IS NOT NULL ORDER BY ts LIMIT 1',
        ).fetchone()
    deltas: dict[str, float] = {}
    prev = first[0] if first else None
    for d, kwh, _ in rows:
        if prev is not None:
            # Clamp tiny negatives (counter jitter); a real counter reset
            # (device swap/factory reset) also lands on 0 rather than a
            # huge negative bar.
            deltas[d] = max(0.0, round(kwh - prev, 3))
        prev = kwh
    return deltas, prev  # prev has ended up as the newest counter reading


def load_ev_daily_energy(days: int = 30) -> list[dict]:
    """Daily EV-charged energy for the last `days` local dates, oldest first.

    Derived from the lifetime counter via _ev_day_deltas(); days without any
    poll yield None (unknown), never a fake 0.
    """
    deltas, _ = _ev_day_deltas()
    start = date.today() - timedelta(days=days - 1)
    return [
        {'date': (start + timedelta(days=i)).isoformat(),
         'kwh': deltas.get((start + timedelta(days=i)).isoformat())}
        for i in range(days)
    ]


def load_ev_energy_summary() -> dict:
    """EV-charged kWh for calendar periods (local time, week starts Monday):
    today, this week, this month, this year, plus the lifetime counter itself.

    Period sums add up the known day deltas inside the period; 'day' is None
    when today has no poll yet (unknown, not 0). 'lifetime' is None until the
    first successful poll ever.
    """
    deltas, lifetime = _ev_day_deltas()
    today = date.today()

    def _since(start: date) -> float:
        s = start.isoformat()
        return round(sum(v for d, v in deltas.items() if d >= s), 3)

    return {
        'day':      deltas.get(today.isoformat()),
        'week':     _since(today - timedelta(days=today.weekday())),
        'month':    _since(today.replace(day=1)),
        'year':     _since(today.replace(month=1, day=1)),
        'lifetime': lifetime,
    }


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


def purge_power_outliers(threshold_w: float = 35_000) -> int:
    """Delete poll rows where any power field exceeds threshold_w.

    I32 Modbus reads occasionally return garbage values in the millions of watts
    (register word-order corruption). The threshold must clear the SCharger-22KT
    EV charger (22 kW three-phase on the meter) plus house load — at 10 kW this
    purge would erase every poll of a charging session — while still cleanly
    rejecting the million-watt glitches.  Returns the number of rows deleted.
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


def load_pv_avg_by_period_end(from_ts: float, to_ts: float,
                              min_samples: int = 30) -> dict[int, float]:
    """Average actual PV power (pv1_power + pv2_power, W) per 30-min period,
    keyed by period_end UTC ms to align with Solcast forecast records.

    Used by the solar-forecast calibration. Buckets with fewer than min_samples
    polls (downtime, restarts) are dropped — a half-empty bucket biases the
    actual/forecast ratio.
    """
    with _lock:
        rows = _get_con().execute(
            'SELECT CAST(ts / 1800 AS INTEGER) AS b, '
            '       AVG(COALESCE(json_extract(data, "$.pv1_power"), 0) '
            '         + COALESCE(json_extract(data, "$.pv2_power"), 0)), '
            '       COUNT(*) '
            'FROM polls WHERE ts >= ? AND ts < ? '
            '  AND (json_extract(data, "$.pv1_power") IS NOT NULL '
            '    OR json_extract(data, "$.pv2_power") IS NOT NULL) '
            'GROUP BY b',
            (from_ts, to_ts),
        ).fetchall()
    return {(int(b) + 1) * 1800 * 1000: pv for b, pv, n in rows if n >= min_samples}


def poll_ts_range() -> tuple[float, float] | None:
    """Return (earliest_ts, latest_ts) Unix seconds of stored polls, or None."""
    with _lock:
        row = _get_con().execute('SELECT MIN(ts), MAX(ts) FROM polls').fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0]), float(row[1])


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
