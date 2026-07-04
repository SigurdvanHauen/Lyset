"""
FastAPI web server — replaces the PySide6 desktop GUI.

Endpoints:
  GET  /            → index.html (SPA)
  WS   /ws          → real-time push (data, connection, write_result, log)
  POST /api/connect → start Modbus worker
  POST /api/disconnect
  POST /api/write   → enqueue a register write
  GET  /api/read    → synchronous single-register read (for control panel prefill)
  GET  /api/state   → current connection state + last snapshot + log tail
  GET  /api/ha/snapshot → flat JSON snapshot for Home Assistant REST sensor
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import math
import os
import statistics
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field

from pymodbus.client import ModbusTcpClient

from . import config
from .modbus_client import ModbusWorker
from .prices import PriceWorker, worker_from_env as prices_from_env
from .solcast import SolcastWorker, worker_from_env as solcast_from_env
from .consumption_model import ConsumptionModel
from .solar_calibration import SolarCalibration
from .ev_charger import EVChargerWorker, worker_from_env as ev_charger_from_env
from .auto_controller import (
    AutoController, arbitrage_enabled, arbitrage_min_gain,
    NEGATIVE_EXPORT_DKK, EXPORT_RESUME_DKK, CHEAP_IMPORT_DKK,
    GRID_CHARGE_SOC_START, GRID_CHARGE_SOC_MAX, GRID_CHARGE_W,
    FORCE_CHARGE_SOC_MAX, MAX_FORCE_CHARGE_W,
    CHARGE_MARGIN_DKK, CHARGE_HORIZON_H,
    HOLD_DELTA_DKK, HOLD_HORIZON_H, MAX_HOLD_IMPORT_DKK, MIN_SOC_HOLD,
    HOLD_SOLAR_REFILL_FACTOR,
    ARBIT_MARGIN_DKK, MIN_SOC_ARBIT, ARBIT_HORIZON_H, ARBIT_MIN_EXCESS_KWH,
)
from . import store

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / 'static'

# ── Shared state (module-level, single-process) ───────────────────────────────
_worker: Optional[ModbusWorker] = None
_connected: bool = False
_connection_msg: str = 'Not connected'
_last_data: dict = {}
_log_buffer: list[dict] = []
_ws_clients: set[WebSocket] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None
_msg_queue: Optional[asyncio.Queue] = None

_price_worker: Optional[PriceWorker] = None
_last_prices: list[dict] = []
_price_status: str = 'Not configured'
_price_status_ok: bool = False

_solcast_worker: Optional[SolcastWorker] = None
_last_solar_forecast: list[dict] = []
_solcast_status: str = 'Not configured'
_solcast_status_ok: bool = False

_consumption_model: Optional[ConsumptionModel] = None
_last_consumption_forecast: list[dict] = []
_last_power_forecast: list[dict] = []
_last_sim_soc: float | None = None  # SoC anchor used for the last simulation run
_auto_controller: AutoController = AutoController()
_MODEL_PATH = Path(__file__).parent.parent / 'lyset_model.json'
_solar_cal: SolarCalibration = SolarCalibration()
_SOLAR_CAL_PATH = Path(__file__).parent.parent / 'lyset_solar_cal.json'


_ev_charger_worker: Optional[EVChargerWorker] = None
_last_ev_charger_data: dict = {}
_ev_charger_status: str = 'Not configured'
_ev_charger_status_ok: bool = False
_TZ_LOCAL = ZoneInfo('Europe/Copenhagen')

# Accumulator: collect 10-s Modbus samples within the current 15-min slot
_slot_samples: list[float] = []   # watts values
_slot_key: int = -1               # int(ts_utc / 900)
# One-shot battery-sign diagnostic (see _on_data): counts captured samples.
_disch_diag: dict = {'n': 0}
_backfill_done: bool = False      # run historical prediction backfill once per process
_last_batt_soc: float | None = None  # used to detect single-poll SoC outliers
_last_batt_soc_ts: float = 0.0      # unix timestamp of last accepted SoC reading
_pv_yield_logged: float | None = None  # last daily_yield value written to daily_solar table


# ── WebSocket broadcast ───────────────────────────────────────────────────────

async def _broadcaster():
    """Asyncio task: drain _msg_queue and fan out to all WebSocket clients."""
    while True:
        try:
            text: str = await _msg_queue.get()
            dead: set[WebSocket] = set()
            for ws in list(_ws_clients):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.add(ws)
            for ws in dead:
                try:
                    await ws.close()
                except Exception:
                    pass
            _ws_clients.difference_update(dead)
        except Exception as exc:
            log.error('Broadcaster error: %s', exc)


def _push(msg: dict):
    """Thread-safe push: serialise msg and schedule delivery on the asyncio loop."""
    if _loop and _msg_queue is not None and not _loop.is_closed():
        text = json.dumps(msg, default=str)
        _loop.call_soon_threadsafe(_msg_queue.put_nowait, text)


# ── Log handler that forwards Python log records to the browser ───────────────

class _TZFormatter(logging.Formatter):
    """logging.Formatter that renders asctime in Danish local time, not the
    server's timezone (the Proxmox LXC runs in UTC)."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, _TZ_LOCAL)
        return dt.strftime(datefmt or '%H:%M:%S')


class _WebLogHandler(logging.Handler):
    _LEVELS = {
        logging.DEBUG:    'debug',
        logging.INFO:     'info',
        logging.WARNING:  'warn',
        logging.ERROR:    'error',
        logging.CRITICAL: 'error',
    }

    def __init__(self):
        super().__init__()
        self.setFormatter(_TZFormatter(
            '%(asctime)s  %(levelname)-8s  %(name)s — %(message)s',
            datefmt='%H:%M:%S',
        ))

    def emit(self, record: logging.LogRecord):
        try:
            entry = {
                't': datetime.now(_TZ_LOCAL).strftime('%H:%M:%S'),
                'level': self._LEVELS.get(record.levelno, 'info'),
                'msg': self.format(record),
            }
            _log_buffer.append(entry)
            if len(_log_buffer) > 500:
                _log_buffer.pop(0)
            _push({'type': 'log', 'entry': entry})
        except Exception:
            pass


# ── SoC forecast simulation ───────────────────────────────────────────────────

def _price_at(prices_sorted: list[dict], ts_ms: int) -> float | None:
    """Import price of the slot covering ts_ms — the latest record at or before it.
    Assumes prices_sorted is ascending by 'ts'."""
    best = None
    for p in prices_sorted:
        if p['ts'] <= ts_ms:
            best = p
        else:
            break
    return best.get('import') if best else None


def _simulate_soc(
    start_soc: float,
    capacity_kwh: float,
    solar_fc: list[dict],
    load_fc: list[dict],
    prices: list[dict] | None = None,
    gc_state: bool = False,
    neg_state: bool = False,
    min_soc: float = 10.0,
    charge_eff: float = 0.97,
    discharge_eff: float = 0.97,
    start_ms: int = None,
) -> list[dict]:
    """
    Simulate battery SoC, battery power, and grid power forward from start_ms.

    start_ms:  anchor timestamp (default: now). Pass a past timestamp to backfill.
    solar_fc:  [{ts_ms, pv_w}, ...]  — 30-min period_end UTC ms
    load_fc:   [{ts_ms, w}, ...]     — 15-min slot_start UTC ms
    prices:    [{ts, import, export}, ...] — when provided, mirrors auto-controller
               decision logic (force-charge on negative export, grid-charge on
               cheap import) per slot.  None → pure self-consumption model.
    gc_state:  current grid-charge hysteresis state (True = currently charging)

    Returns:   [{ts_ms, soc, batt_w, grid_w}, ...]
      batt_w: positive = charging, negative = discharging  (matches batt_power key)
      grid_w: positive = importing, negative = exporting   (matches meter_active_power key)

    Iterates over a 15-min time grid (not solar_fc records) so that night periods
    — which Solcast omits entirely because PV=0 — are still stepped through and the
    battery correctly discharges overnight. The grid matches the consumption
    forecast's 15-min slots, and each record is labelled at its slot-START ts_ms
    (the same convention the house-needs line uses), so the plotted battery-power
    line coincides with the house-load line instead of sitting a coarse 30-min
    backward average above it. PV, still on Solcast's 30-min resolution, is read
    from the 30-min period enclosing each 15-min step.
    """
    if not solar_fc or capacity_kwh <= 0:
        return []

    SLOT_MS  = 15 * 60 * 1000  # 15-min steps matching the consumption forecast
    SLOT_H   = SLOT_MS / 3_600_000  # hours per step (0.25) for energy integration
    SOLAR_MS = 30 * 60 * 1000  # Solcast resolution — each 15-min step reads its
                               # enclosing 30-min PV period
    cutoff_ms = start_ms if start_ms is not None else int(time.time() * 1000)

    # Index solar by period-end ts; night periods (omitted by Solcast) default to 0 W
    solar_by_ts  = {r['ts_ms']: r.get('pv_w') or 0.0 for r in solar_fc}
    load_by_ts   = {r['ts_ms']: r['w'] for r in load_fc if r.get('w') is not None}
    max_power_kw = capacity_kwh * 0.5  # C/2 — LUNA2000 rated charge rate heuristic

    # Price lookup: sorted ascending so we can walk a pointer forward
    prices_sorted = sorted(prices, key=lambda p: p['ts']) if prices else []
    price_ptr     = 0
    arb_enabled   = arbitrage_enabled()  # Settings toggle; mirrors _decide's gate
    arb_min_gain  = arbitrage_min_gain()  # required % gain vs opportunity cost

    end_ms        = max(r['ts_ms'] for r in solar_fc)
    first_slot_ms = int((cutoff_ms // SLOT_MS + 1) * SLOT_MS)

    soc        = start_soc
    gc_active  = gc_state   # grid-charge hysteresis state across steps
    neg_active = neg_state  # zero-export hysteresis state across steps
    result    = [{'ts_ms': cutoff_ms, 'soc': round(soc, 1), 'batt_w': None, 'grid_w': None}]

    for ts_ms in range(first_slot_ms, int(end_ms) + 1, SLOT_MS):
        # ts_ms is a 15-min slot-START, matching the consumption forecast's own
        # ts_ms convention, so the plotted discharge point lands on the same x as
        # the house-needs line for that slot. PV is a power that's flat across its
        # 30-min Solcast period, so read the period enclosing this slot-start.
        pv_w   = solar_by_ts.get((ts_ms // SOLAR_MS + 1) * SOLAR_MS, 0.0)
        load_w = load_by_ts.get(ts_ms)
        if load_w is None:
            continue  # beyond load forecast window — stop stepping

        net_kw = (pv_w - load_w) / 1000.0

        # Advance price pointer to the most recent price at or before this slot
        while (price_ptr + 1 < len(prices_sorted)
               and prices_sorted[price_ptr + 1]['ts'] <= ts_ms):
            price_ptr += 1
        cur_price = (prices_sorted[price_ptr]
                     if price_ptr < len(prices_sorted)
                     and prices_sorted[price_ptr]['ts'] <= ts_ms
                     else None)

        if cur_price and prices_sorted:
            # Mirror AutoController._decide priority order:
            #   1 negative export → self-consumption
            #   2 grid charge   3 hold (deficit only)   4 arbitrage   5 self-consumption
            export_dkk = cur_price.get('export') or 0.0
            import_dkk = cur_price.get('import') or 0.5

            future_from_here = prices_sorted[price_ptr + 1:]
            gc_soc_limit = GRID_CHARGE_SOC_MAX if gc_active else GRID_CHARGE_SOC_START

            # Asymmetric hysteresis mirrors AutoController: curtail the instant export
            # < 0, but once curtailing hold until the price is clearly positive so a
            # near-zero export price can't flip the decision (and the plotted export
            # line) every slot.
            neg_thresh = EXPORT_RESUME_DKK if neg_active else NEGATIVE_EXPORT_DKK
            do_neg = export_dkk < neg_thresh
            neg_active = do_neg
            do_gc = do_hold = do_arbit = False
            if not do_neg:
                # Grid charge: at/near the cheapest upcoming window AND SoC below threshold?
                charge_end = ts_ms + CHARGE_HORIZON_H * 3_600_000
                c_win = [p for p in future_from_here if p['ts'] <= charge_end]
                if c_win and soc < gc_soc_limit:
                    c_min = min(p['import'] for p in c_win)
                    c_max = max(p['import'] for p in c_win)
                    do_gc = (import_dkk <= c_min + CHARGE_MARGIN_DKK
                             and c_max > import_dkk + CHARGE_MARGIN_DKK)

                # Hold: deficit now AND a significantly more expensive slot is coming soon
                if not do_gc and net_kw < 0:
                    hold_end = ts_ms + HOLD_HORIZON_H * 3_600_000
                    h_win = [p for p in future_from_here if p['ts'] <= hold_end]
                    if (h_win
                            and import_dkk < MAX_HOLD_IMPORT_DKK
                            and soc > MIN_SOC_HOLD):
                        peak_p = max(h_win, key=lambda p: p['import'])
                        # Solar-aware: don't hoard for a peak the sun will refill the
                        # pack for. Sum forecast PV between now and the peak; suppress
                        # the hold when it covers the battery (mirrors AutoController).
                        solar_kwh = sum(
                            solar_by_ts.get((t // SOLAR_MS + 1) * SOLAR_MS, 0.0)
                            / 1000.0 * SLOT_H
                            for t in range(ts_ms + SLOT_MS, peak_p['ts'] + 1, SLOT_MS)
                        )
                        solar_will_refill = solar_kwh >= capacity_kwh * HOLD_SOLAR_REFILL_FACTOR
                        do_hold = (
                            peak_p['import'] > import_dkk + HOLD_DELTA_DKK
                            and not solar_will_refill
                        )

                # Arbitrage business case (mirrors AutoController._decide branch 4):
                # export only makes sense if it beats the OPPORTUNITY COST of the
                # stored energy — self-consuming it at the most expensive upcoming
                # import. Require export_now ≥ max_upcoming_import × (1 + min_gain);
                # since import > export for a given hour, this can never lose money by
                # dumping the pack into a peak and re-importing the house load higher.
                if arb_enabled and not do_gc and not do_hold and soc > MIN_SOC_ARBIT:
                    arbit_end = ts_ms + ARBIT_HORIZON_H * 3_600_000
                    a_win = [p for p in future_from_here if p['ts'] <= arbit_end]
                    if a_win and export_dkk >= max(p['import'] for p in a_win) * (1.0 + arb_min_gain):
                        # Secondary SoC guard: keep any energy still needed for future
                        # slots pricier than today's export (≈0 under the gate above).
                        available_kwh = max(0.0, (soc - MIN_SOC_ARBIT) / 100.0 * capacity_kwh)
                        reserve_kwh   = 0.0
                        for lt, lw in load_by_ts.items():
                            if lt <= ts_ms or lt > arbit_end:
                                continue
                            imp = _price_at(prices_sorted, lt)
                            if imp is None or imp <= export_dkk:
                                continue
                            # 15-min load slot lt sits in the 30-min solar period
                            # ending at the next boundary (same mapping as above).
                            period_end = (lt // SOLAR_MS + 1) * SOLAR_MS
                            deficit_w  = max(0.0, lw - solar_by_ts.get(period_end, 0.0))
                            reserve_kwh += deficit_w / 1000.0 * 0.25
                        do_arbit = (available_kwh - reserve_kwh) > ARBIT_MIN_EXCESS_KWH

            if do_neg:
                # Negative export → charge from surplus only (never import, never
                # discharge). The export-limit overlay (47415=zero export) curtails
                # any surplus beyond the battery's max rate, so net feed-in is 0:
                # grid_kw is clamped to ≥0 (no export) for the forecast.
                gc_active  = False
                if soc >= FORCE_CHARGE_SOC_MAX or net_kw <= 0:
                    batt_kw = 0.0
                else:
                    batt_kw = min(net_kw, max_power_kw)
                grid_kw    = max(0.0, batt_kw - net_kw)  # surplus remainder curtailed, not exported
                energy_kwh = batt_kw * SLOT_H * charge_eff

            elif do_gc:
                gc_active  = True
                batt_kw    = min(GRID_CHARGE_W / 1000.0, max_power_kw)
                grid_kw    = batt_kw - net_kw
                energy_kwh = batt_kw * SLOT_H * charge_eff

            elif do_hold:
                gc_active  = False
                batt_kw    = 0.0
                grid_kw    = -net_kw
                energy_kwh = 0.0

            elif do_arbit:
                gc_active  = False
                batt_kw    = 0.0 if soc <= MIN_SOC_ARBIT else -min(GRID_CHARGE_W / 1000.0, max_power_kw)
                grid_kw    = batt_kw - net_kw
                energy_kwh = batt_kw * SLOT_H / discharge_eff  # batt_kw ≤ 0 → SoC falls

            else:
                # Self-consumption (covers negative export and the default case)
                gc_active = False
                if net_kw >= 0:
                    batt_kw = 0.0 if soc >= 100.0 else min(net_kw, max_power_kw)
                else:
                    batt_kw = 0.0 if soc <= min_soc else max(net_kw, -max_power_kw)
                grid_kw    = batt_kw - net_kw
                energy_kwh = batt_kw * SLOT_H * (charge_eff if batt_kw >= 0 else 1.0 / discharge_eff)

        else:
            # No price data — pure self-consumption
            if net_kw >= 0:
                batt_kw = 0.0 if soc >= 100.0 else min(net_kw, max_power_kw)
            else:
                batt_kw = 0.0 if soc <= min_soc else max(net_kw, -max_power_kw)
            grid_kw    = batt_kw - net_kw
            energy_kwh = batt_kw * SLOT_H * (charge_eff if batt_kw >= 0 else 1.0 / discharge_eff)

        soc = max(min_soc, min(100.0, soc + energy_kwh / capacity_kwh * 100.0))
        result.append({
            'ts_ms': ts_ms,
            'soc':    round(soc, 1),
            'batt_w': round(batt_kw * 1000),
            'grid_w': round(grid_kw * 1000),
        })

    # Smooth the SoC column with a 3-point median to remove load-forecast noise
    # that Chart.js bezier interpolation would otherwise amplify into visible spikes.
    socs = [r['soc'] for r in result]
    n = len(socs)
    if n >= 3:
        smoothed = [
            round(statistics.median(socs[max(0, i-1):min(n, i+2)]), 1)
            for i in range(n)
        ]
        for r, s in zip(result, smoothed):
            r['soc'] = s

    return result


# ── Power forecast helpers ────────────────────────────────────────────────────

def _backfill_power_forecast(cap_val: float):
    """
    Seed the power_forecast table with historical predictions so the chart
    shows a continuous line from the start of today, not just from 'now'.

    Finds the earliest actual SoC reading from today in the polls table,
    runs the simulation forward from that anchor, and stores results with
    INSERT OR IGNORE (never overwrites genuine predictions already stored).
    """
    global _backfill_done
    _backfill_done = True

    if not _last_solar_forecast or not _last_consumption_forecast:
        return

    history = store.load_last_24h()
    if not history:
        return

    # Find the earliest SoC reading from the start of today (local time)
    today_start_ms = int(
        datetime.now(_TZ_LOCAL).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    anchor_soc: Optional[float] = None
    anchor_ts_ms: int = 0
    for ts, data in history:
        ts_ms = int(ts * 1000)
        soc = data.get('batt_soc')
        if soc is not None and (anchor_soc is None or ts_ms >= today_start_ms):
            anchor_soc = soc
            anchor_ts_ms = ts_ms
            if ts_ms >= today_start_ms:
                break  # found today's earliest — stop

    if anchor_soc is None:
        return

    now_ms = int(time.time() * 1000)
    fc = _simulate_soc(
        anchor_soc, cap_val, _last_solar_forecast, _last_consumption_forecast,
        start_ms=anchor_ts_ms,
    )
    if fc:
        store.save_power_forecast(fc, now_ms)
        past_n = sum(1 for r in fc if r['ts_ms'] <= now_ms)
        log.info(
            'PowerForecast: backfilled %d past slots from SoC %.1f%% at %s',
            past_n, anchor_soc,
            datetime.fromtimestamp(anchor_ts_ms / 1000, tz=_TZ_LOCAL).strftime('%H:%M'),
        )


def _smooth_forecast_soc(records: list[dict]) -> list[dict]:
    """Apply 5-point sliding median to the soc field of a forecast list."""
    HALF = 2
    socs = [r['soc'] for r in records if r.get('soc') is not None]
    if len(socs) < 3:
        return records
    n = len(socs)
    smoothed = [round(statistics.median(socs[max(0, i-HALF):min(n, i+HALF+1)]), 1) for i in range(n)]
    j = 0
    result = []
    for r in records:
        if r.get('soc') is not None:
            result.append({**r, 'soc': smoothed[j]})
            j += 1
        else:
            result.append(r)
    return result


def _push_power_forecast(fc: list[dict]):
    """Save simulation output to DB and push the full stored window to browsers."""
    global _last_power_forecast
    now_ms = int(time.time() * 1000)
    store.save_power_forecast(fc, now_ms)
    combined = store.load_power_forecast(now_ms - 86_400_000, now_ms + 48 * 3_600_000)
    _last_power_forecast = _smooth_forecast_soc(combined or fc)
    _push({'type': 'power_forecast', 'payload': _last_power_forecast})


def _ev_slot_threshold_w() -> float:
    """House-load slot average above which a 15-min slot is assumed to contain
    EV charging and is excluded from the consumption model. Read live from the
    env so a Settings save applies without restart."""
    try:
        return float(os.getenv('CONSUMPTION_EV_SLOT_W', '6000'))
    except (TypeError, ValueError):
        return 6000.0


# ── Worker callbacks (called from the Modbus daemon thread) ───────────────────

def _on_data(data: dict):
    global _last_data, _slot_samples, _slot_key, _last_batt_soc, _last_batt_soc_ts, _pv_yield_logged, _last_sim_soc
    ts = data.get('_timestamp', time.time())
    # Drop SoC glitches using the same rate-based filter as _clean_history_soc.
    soc = data.get('batt_soc')
    if soc is not None:
        if _last_batt_soc is not None:
            elapsed_s = max(ts - _last_batt_soc_ts, 5.0)
            if abs(soc - _last_batt_soc) > _soc_max_change(elapsed_s):
                log.warning('batt_soc outlier dropped: %.1f%% → %.1f%%', _last_batt_soc, soc)
                data = {**data, 'batt_soc': _last_batt_soc}
            else:
                _last_batt_soc = soc
                _last_batt_soc_ts = ts
        else:
            _last_batt_soc = soc
            _last_batt_soc_ts = ts
    # Strip private keys and non-JSON-serialisable values
    clean = {k: v for k, v in data.items()
             if not k.startswith('_') and isinstance(v, (int, float, str, bool, type(None)))}
    _last_data = clean
    _push({'type': 'data', 'payload': clean})
    store.save(ts, clean)

    # Log daily solar yield whenever the inverter counter changes value
    yield_val = clean.get('daily_yield')
    if yield_val is not None and yield_val != _pv_yield_logged:
        _pv_yield_logged = yield_val
        today_str = datetime.fromtimestamp(ts, tz=_TZ_LOCAL).strftime('%Y-%m-%d')
        store.upsert_daily_solar(today_str, yield_kwh=yield_val)

    # Accumulate house load for the current 15-min slot
    meter_w = clean.get('house_load')
    if meter_w is not None and _consumption_model is not None:
        grid_import_w = max(0.0, meter_w)
        key = int(ts / 900)
        if key != _slot_key:
            # ≥ 15 of the ~90 expected samples (10-s polls / 15 min), else the
            # slot is a partial from downtime/restart and would train on noise.
            if len(_slot_samples) >= 15 and _slot_key >= 0:
                # 10 %-trimmed mean: single-poll house_load glitches (0 W and
                # ~5 kW in adjacent polls — non-simultaneous batched reads) fall
                # in the trimmed tails, while sustained real load (oven, charger)
                # spans enough samples to survive. A median would also suppress
                # real short spikes and under-count energy; the trim doesn't.
                srt  = sorted(_slot_samples)
                trim = len(srt) // 10
                kept = srt[trim:len(srt) - trim] if trim else srt
                avg_w = sum(kept) / len(kept)
                slot_ts = _slot_key * 900.0
                # EV-charging slots are excluded from the HOUSE profile: the
                # SCharger (22 kW) meters as house load, and one session would
                # blow the slot's EWM (and every forecast/reserve computed from
                # it) for weeks. Until the charger is metered separately, any
                # slot above the threshold is treated as EV + house and skipped.
                if avg_w > _ev_slot_threshold_w():
                    log.info('ConsumptionModel: slot %.0f W > %.0f W — assumed EV '
                             'charging, not learned', avg_w, _ev_slot_threshold_w())
                else:
                    _consumption_model.update(slot_ts, avg_w)
                    _save_model_and_regen()
            _slot_samples = [grid_import_w]
            _slot_key = key
        else:
            _slot_samples.append(grid_import_w)

    # ── One-shot battery-sign diagnostic ─────────────────────────────────────
    # Settles whether house_load's `− batt_power` term correctly counts an
    # overnight battery-supplied standby as load. When PV≈0 and the battery is
    # clearly moving power, log the raw terms a handful of times then go quiet.
    # If batt_power is NEGATIVE while discharging, the formula is right and the
    # overnight zeros are just stale seed (will heal). If POSITIVE, the sign is
    # flipped and house_load is wrongly clamped to 0. (Formula: modbus_client.py)
    if _disch_diag['n'] < 8:
        pv_w = clean.get('active_power')
        bp   = clean.get('batt_power')
        if pv_w is not None and bp is not None and pv_w < 50 and abs(bp) > 100:
            _disch_diag['n'] += 1
            log.info(
                'DISCHARGE-DIAG #%d: active_power=%s meter_active_power=%s '
                'batt_power=%s house_load=%s  [batt sign: %s]',
                _disch_diag['n'], pv_w, clean.get('meter_active_power'),
                bp, clean.get('house_load'),
                'NEGATIVE = discharge (formula correct)' if bp < 0
                else 'POSITIVE = discharge (sign flipped!)',
            )

    # Save and push power forecast whenever we have all prerequisites.
    # Re-simulate only when the anchor SoC shifts by ≥ 1 % to avoid pushing
    # a jittery prediction line on every 10-s poll.
    soc_val = clean.get('batt_soc')
    cap_val = clean.get('batt_rated_capacity')
    if (soc_val is not None and cap_val and cap_val > 0
            and _last_solar_forecast and _last_consumption_forecast):
        if not _backfill_done:
            _backfill_power_forecast(cap_val)
        if _last_sim_soc is None or abs(soc_val - _last_sim_soc) >= 1.0:
            _last_sim_soc = soc_val
            # Drive the simulation from the SAME consumption forecast shown as the
            # house-needs line (and consumed by the auto-controller and the backfill
            # path above) rather than a fresh predict(). A fresh predict() carries the
            # model's live global-bias term, which drifts above the persisted series
            # (measured ~+420 W) and inflated the plan's overnight discharge to ~2x the
            # real ~350 W load. In self-consumption at night grid_w=0, so batt discharge
            # equals the load fed in — using the served forecast makes predicted
            # discharge track predicted house needs. Overlay it onto a 48h predict() so
            # the tail beyond the persisted ~24h horizon still fills the plan window.
            load_48h = _consumption_model.predict(time.time(), n_slots=192)
            served = {r['ts_ms']: r['w'] for r in _last_consumption_forecast
                      if r.get('w') is not None}
            if served:
                load_48h = [{**r, 'w': served.get(r['ts_ms'], r['w'])} for r in load_48h]
            fc = _simulate_soc(
                soc_val, cap_val, _last_solar_forecast, load_48h,
                prices=_last_prices if _auto_controller.enabled else None,
                gc_state=_auto_controller._grid_charging,
                neg_state=_auto_controller._export_curtailed,
            )
            if fc:
                _push_power_forecast(fc)


try:
    _CONS_SMOOTH_SIGMA = float(os.getenv('CONSUMPTION_SMOOTH_SIGMA_SLOTS', '1.2'))
except (TypeError, ValueError):
    _CONS_SMOOTH_SIGMA = 1.2


def _smooth_w_timeseries(records: list[dict], sigma: float) -> list[dict]:
    """Gaussian-smooth the 'w' of a consumption series along time (15-min slots).

    The consumption model's per-slot profile smoothing only reaches freshly predicted
    slots; rows already frozen in consumption_forecast (INSERT OR IGNORE) keep their
    original per-slot sampling noise — and the plan sim overlays exactly those frozen
    rows as its load input. Smoothing the SERVED series here de-noises the house-needs
    line, the SoC/grid/export plan, and the auto-controller's load input in one place,
    regardless of what the frozen rows hold. Linear (no week wrap); rows missing 'w'
    pass through and locally shorten the kernel.
    """
    if sigma <= 0 or len(records) < 3:
        return records
    recs   = sorted(records, key=lambda r: r['ts_ms'])
    ws     = [r.get('w') for r in recs]
    n      = len(ws)
    rad    = max(1, int(round(3 * sigma)))
    kernel = [math.exp(-0.5 * (k / sigma) ** 2) for k in range(-rad, rad + 1)]
    out    = []
    for i in range(n):
        if ws[i] is None:
            out.append(recs[i])
            continue
        num = den = 0.0
        for k in range(-rad, rad + 1):
            j = i + k
            if 0 <= j < n and ws[j] is not None:
                wk   = kernel[k + rad]
                num += wk * ws[j]
                den += wk
        out.append({**recs[i], 'w': round(num / den, 1) if den else ws[i]})
    return out


def _merge_cons_bands(records: list[dict], future: list[dict]) -> list[dict]:
    """Attach a p10/p90 confidence band to each stored consumption record.

    The DB keeps only the mean 'w' (frozen at first write via INSERT OR IGNORE); the
    σ-based band comes from the fresh `future` predict(). Because the fresh predict
    carries the model's *current* global bias while 'w' was frozen under an earlier
    bias, transplanting the raw p10/p90 leaves the band off-centre from the displayed
    line. Transplant only the HALF-WIDTHS (p90−mean, mean−p10 — σ-based and
    bias-independent) onto the displayed 'w' so the band brackets the dashed mean line.

    The 'w' is first smoothed along time so the served line (and everything that
    consumes it) is de-noised even where the frozen rows still hold raw per-slot noise.
    """
    records    = _smooth_w_timeseries(records, _CONS_SMOOTH_SIGMA)
    pred_by_ts = {r['ts_ms']: r for r in future}
    out = []
    for r in records:
        pr = pred_by_ts.get(r['ts_ms'])
        if (pr and r.get('w') is not None and pr.get('w') is not None
                and pr.get('p10_w') is not None and pr.get('p90_w') is not None):
            w = r['w']
            out.append({**r,
                        'p10_w': round(max(0.0, w - (pr['w'] - pr['p10_w'])), 1),
                        'p90_w': round(w + (pr['p90_w'] - pr['w']), 1)})
        else:
            out.append({**r, 'p10_w': None, 'p90_w': None})
    return out


def _save_model_and_regen():
    global _last_consumption_forecast
    try:
        _consumption_model.save(_MODEL_PATH)
    except Exception as exc:
        log.warning('ConsumptionModel: save failed — %s', exc)
    # Save next-24h predictions (INSERT OR IGNORE keeps the first prediction per slot,
    # so past predictions are preserved for comparison against actual measurements)
    future = _consumption_model.predict(time.time())
    store.save_consumption_forecast(future)
    # Push the combined window: past 24h of predictions + next 24h. Bands are
    # re-centred on the displayed mean by _merge_cons_bands (see its docstring).
    now_ms = int(time.time() * 1000)
    combined = store.load_consumption_forecast(now_ms - 86_400_000, now_ms + 86_400_000)
    _last_consumption_forecast = _merge_cons_bands(combined or future, future)
    _auto_controller.set_consumption_forecast(_last_consumption_forecast)
    _push({'type': 'consumption_forecast', 'payload': _last_consumption_forecast})


def _on_connection(ok: bool, msg: str):
    global _connected, _connection_msg
    _connected = ok
    _connection_msg = msg
    _push({'type': 'connection', 'ok': ok, 'msg': msg})


def _on_write_result(ok: bool, msg: str):
    _push({'type': 'write_result', 'ok': ok, 'msg': msg})


def _on_error(msg: str):
    _push({'type': 'error', 'msg': msg})


def _on_prices(prices: list[dict]):
    global _last_prices
    _last_prices = prices
    store.save_prices(prices)
    _push({'type': 'prices', 'payload': prices})


def _on_price_status(msg: str, ok: bool):
    global _price_status, _price_status_ok
    _price_status = msg
    _price_status_ok = ok


def _curtailed_slot_ts(forecast_rows: list[dict], from_ms: int, to_ms: int) -> set[int]:
    """period_end_ms of 30-min forecast slots whose export price was negative.

    The auto-controller runs zero-export (mode 5) whenever export <
    NEGATIVE_EXPORT_DKK, clipping PV to house load + battery charge. The measured
    PV in those slots understates the sun, so they must not train the calibration.
    A slot's price is the hourly record covering its midpoint (period_end − 15 min);
    bisect makes it robust to any price resolution. Reach one hour before from_ms
    so the first slot still finds its covering price."""
    prices = store.load_prices(from_ms - 3_600_000, to_ms)
    if not prices:
        return set()
    prices.sort(key=lambda p: p['ts'])
    ptimes = [p['ts'] for p in prices]
    skip: set[int] = set()
    for r in forecast_rows:
        ts = r['ts_ms']
        i = bisect.bisect_right(ptimes, ts - 900_000) - 1   # price covering the slot midpoint
        if i >= 0 and prices[i]['export'] < NEGATIVE_EXPORT_DKK:
            skip.add(ts)
    return skip


def _learn_solar_calibration():
    """Update the per-hour Solcast correction factors from every 30-min slot that
    has passed since the last learning pass: stored forecast (solar_forecast
    table, already calibrated at fetch time) vs actual PV averaged from polls.
    Slots that fell in a negative-export (curtailment) window are skipped — the
    inverter clipped PV there, so their low 'actual' is not a forecast miss.
    Runs at startup and before each Solcast fetch is applied (3×/day)."""
    try:
        now_ms   = int(time.time() * 1000)
        # Never reach back before the cursor; on first run, learn from the last 7 days.
        from_ms  = max(_solar_cal.learned_until_ms, now_ms - 7 * 86_400_000)
        past_fc  = store.load_solar_forecast(from_ms, now_ms)
        if not past_fc:
            return
        actuals  = store.load_pv_avg_by_period_end(from_ms / 1000, now_ms / 1000)
        curtailed = _curtailed_slot_ts(past_fc, from_ms, now_ms)
        used     = _solar_cal.learn(past_fc, actuals, skip_ts=curtailed)
        if used:
            _solar_cal.save(_SOLAR_CAL_PATH)
            log.info('SolarCal: learned from %d slot(s), skipped %d curtailed — %s',
                     used, len(curtailed), _solar_cal.summary())
    except Exception as exc:
        log.warning('SolarCal: learning pass failed — %s', exc)


def _on_solar_forecast(records: list[dict]):
    global _last_solar_forecast
    # Learn from slots that have completed since the last fetch, THEN calibrate
    # the incoming raw Solcast records with the updated per-hour factors before
    # anything downstream (DB, controller, simulation, charts) sees them.
    _learn_solar_calibration()
    records = _solar_cal.apply(records)
    store.save_solar_forecast(records)
    # Push the full stored window (past 24 h preserved + new future data) so the
    # chart shows historical forecasts alongside actuals, not just the latest fetch.
    now_ms = int(time.time() * 1000)
    full = store.load_solar_forecast(now_ms - 86_400_000, now_ms + 2 * 86_400_000)
    _last_solar_forecast = full or records
    _auto_controller.set_solar_forecast(_last_solar_forecast)
    _push({'type': 'solar_forecast', 'payload': _last_solar_forecast})

    # Log today's full-day Solcast total to the daily_solar table
    now_local = datetime.now(_TZ_LOCAL)
    today_str = now_local.strftime('%Y-%m-%d')
    today_start_ms = int(now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    today_end_ms   = today_start_ms + 86_400_000
    fc_total = sum(
        s['pv_w'] * 0.5 / 1000
        for s in _last_solar_forecast
        if today_start_ms < s['ts_ms'] <= today_end_ms
        if s.get('pv_w') is not None
    )
    if fc_total > 0:
        store.upsert_daily_solar(today_str, forecast_kwh=round(fc_total, 2))


def _on_solcast_status(msg: str, ok: bool):
    global _solcast_status, _solcast_status_ok
    _solcast_status = msg
    _solcast_status_ok = ok


def _on_ev_charger_data(data: dict):
    global _last_ev_charger_data
    ts = data.get('_timestamp', time.time())
    _last_ev_charger_data = data
    store.save_ev_charger_poll(ts, data)
    _push({'type': 'ev_charger_data', 'payload': data})


def _on_ev_charger_status(msg: str, ok: bool):
    global _ev_charger_status, _ev_charger_status_ok
    _ev_charger_status = msg
    _ev_charger_status_ok = ok
    _push({'type': 'ev_charger_status', 'msg': msg, 'ok': ok})


def _on_auto_command(mode: str, detail: str):
    ts = time.time()
    store.save_auto_command(ts, mode, detail)
    _push({
        'type': 'auto_mode',
        'enabled': _auto_controller.enabled,
        'last_action': detail,
        'last_action_ts': ts,
        'cmd': {'ts': ts, 'mode': mode, 'detail': detail},
    })


# ── App lifecycle ─────────────────────────────────────────────────────────────

def _start_worker(host: str, port: int, slave_id: int, poll_interval: float):
    global _worker
    if _worker and _worker.is_alive():
        _worker.stop()
        _worker.join(timeout=3)
    _worker = ModbusWorker(
        host=host,
        port=port,
        slave_id=slave_id,
        poll_interval=poll_interval,
        on_data=_on_data,
        on_connection=_on_connection,
        on_write_result=_on_write_result,
        on_error=_on_error,
    )
    _worker.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop, _msg_queue
    _loop = asyncio.get_running_loop()
    _msg_queue = asyncio.Queue()
    asyncio.create_task(_broadcaster())

    handler = _WebLogHandler()
    handler.setLevel(logging.DEBUG)
    for name in ('lyset',):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    # pymodbus is noisy; attach the handler but default to WARNING
    _pm_logger = logging.getLogger('pymodbus')
    _pm_logger.setLevel(logging.WARNING)
    _pm_logger.addHandler(handler)

    store.init()
    purged = store.purge_power_outliers()
    if purged:
        log.warning('Startup: purged %d poll row(s) with outlier power values', purged)
    cleaned = store.clean_soc_history()
    if cleaned:
        log.warning('Startup: cleaned %d poll row(s) with SoC outlier values', cleaned)
    cleaned_fc = store.clean_forecast_soc()
    if cleaned_fc:
        log.warning('Startup: cleaned %d forecast row(s) with SoC outlier values', cleaned_fc)

    # Auto-connect inverter worker
    defaults = ConnectRequest()
    _start_worker(defaults.host, defaults.port, defaults.slave_id, defaults.poll_interval)

    # Start electricity price worker if API key is configured
    global _price_worker
    _price_worker = prices_from_env(on_prices=_on_prices, on_status=_on_price_status)
    if _price_worker:
        _price_worker.start()

    # Load solar-forecast calibration and catch up on slots that completed while
    # the server was down — must precede the Solcast worker, whose first fetch
    # applies these factors.
    global _solar_cal
    if _SOLAR_CAL_PATH.exists():
        try:
            _solar_cal = SolarCalibration.load(_SOLAR_CAL_PATH)
            log.info('SolarCal: loaded — %s', _solar_cal.summary())
        except Exception as exc:
            log.warning('SolarCal: load failed (%s) — starting uncalibrated', exc)
    _learn_solar_calibration()

    # Start Solcast solar forecast worker if API key is configured
    global _solcast_worker, _last_solar_forecast
    _solcast_worker = solcast_from_env(on_forecast=_on_solar_forecast, on_status=_on_solcast_status)
    if _solcast_worker:
        _solcast_worker.start()

    # Start the EV charger cloud poller if FusionSolar credentials are configured
    global _ev_charger_worker
    _ev_charger_worker = ev_charger_from_env(on_data=_on_ev_charger_data, on_status=_on_ev_charger_status)
    if _ev_charger_worker:
        _ev_charger_worker.start()

    # Restore the last EV charger poll from DB so the tab has something immediately
    global _last_ev_charger_data
    ev_recent = store.load_ev_charger_history(time.time() - 86_400, time.time())
    if ev_recent:
        r = ev_recent[-1]
        _last_ev_charger_data = {
            'status': r['status'], 'total_energy_charged': r['total_energy_kwh'],
            'model': r['model'], 'rated_power': r['rated_power_kw'],
            'software_version': r['sw_version'], '_timestamp': r['ts'],
        }

    # Restore last solar forecast from DB so it's available immediately after restart
    now_ms = int(time.time() * 1000)
    cached_fc = store.load_solar_forecast(now_ms - 86_400_000, now_ms + 2 * 86_400_000)
    if cached_fc:
        _last_solar_forecast = cached_fc
        _auto_controller.set_solar_forecast(_last_solar_forecast)
        log.info('Solcast: restored %d cached forecast periods from DB', len(cached_fc))
        # Persist today's Solcast total so /api/daily-solar has it before the next fetch
        now_local   = datetime.now(_TZ_LOCAL)
        day_start   = int(now_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        day_end     = day_start + 86_400_000
        fc_total    = sum(
            s['pv_w'] * 0.5 / 1000
            for s in cached_fc
            if day_start < s['ts_ms'] <= day_end and s.get('pv_w') is not None
        )
        if fc_total > 0:
            store.upsert_daily_solar(now_local.strftime('%Y-%m-%d'), forecast_kwh=round(fc_total, 2))

    # Load or create consumption model
    global _consumption_model, _last_consumption_forecast
    if _MODEL_PATH.exists():
        try:
            _consumption_model = ConsumptionModel.load(_MODEL_PATH)
            log.info('ConsumptionModel: loaded from %s (%d/%d slots filled)',
                     _MODEL_PATH.name, _consumption_model.coverage, 672)
        except Exception as exc:
            log.warning('ConsumptionModel: load failed (%s) — starting fresh', exc)
            _consumption_model = ConsumptionModel()
    else:
        _consumption_model = ConsumptionModel()
        excel_path = os.getenv('CONSUMPTION_HISTORY_PATH', '').strip()
        if excel_path:
            try:
                n = _consumption_model.seed_from_excel(excel_path)
                if n:
                    _consumption_model.save(_MODEL_PATH)
                    log.info('ConsumptionModel: seeded from Excel, saved to %s', _MODEL_PATH.name)
            except Exception as exc:
                log.error('ConsumptionModel: Excel seed failed — %s', exc)
        else:
            log.info('ConsumptionModel: no model file and no CONSUMPTION_HISTORY_PATH set — '
                     'learning from scratch (set CONSUMPTION_HISTORY_PATH to seed from Excel)')

    # Re-seed model from actual house_load history in DB.
    # This corrects any D06 (net grid import) Excel seed — house_load is gross
    # consumption and differs from D06 whenever solar is producing.
    poll_records = store.load_house_load_history()
    if poll_records:
        n_updated = _consumption_model.seed_from_polls(poll_records)
        try:
            _consumption_model.save(_MODEL_PATH)
        except Exception as exc:
            log.warning('ConsumptionModel: save after DB seed failed — %s', exc)
        log.info('ConsumptionModel: re-seeded from %d poll records, %d slots updated, coverage %d/672',
                 len(poll_records), n_updated, _consumption_model.coverage)

    if _consumption_model and _consumption_model.coverage > 0:
        future = _consumption_model.predict(time.time())
        store.save_consumption_forecast(future)
        now_ms = int(time.time() * 1000)
        stored = store.load_consumption_forecast(now_ms - 86_400_000, now_ms + 86_400_000)
        # Bands re-centred on the displayed mean by _merge_cons_bands (DB only keeps 'w')
        _last_consumption_forecast = _merge_cons_bands(stored or future, future)
        _auto_controller.set_consumption_forecast(_last_consumption_forecast)

    # Restore stored power forecast from DB
    global _last_power_forecast
    now_ms = int(time.time() * 1000)
    _last_power_forecast = _smooth_forecast_soc(
        store.load_power_forecast(now_ms - 86_400_000, now_ms + 48 * 3_600_000)
    )
    if _last_power_forecast:
        log.info('PowerForecast: restored %d stored periods from DB', len(_last_power_forecast))

    # Start auto controller — ON by default on (re)start. Override with env
    # AUTO_CONTROLLER_AUTOSTART=0. The run loop only acts once the worker is
    # connected and prices/data are available, and _applied is empty so the
    # first decision re-asserts every register.
    _auto_controller.set_command_callback(_on_auto_command)
    autostart = os.getenv('AUTO_CONTROLLER_AUTOSTART', '1').strip().lower() \
        not in ('0', 'false', 'no', 'off')
    _auto_controller.enabled = autostart
    asyncio.create_task(_auto_controller.run(
        lambda: _worker, lambda: _last_prices, lambda: _last_data,
    ))
    log.info('AutoCtrl: task started (autostart=%s)', autostart)

    yield

    if _worker and _worker.is_alive():
        _worker.stop()
        _worker.join(timeout=3)
    if _price_worker and _price_worker.is_alive():
        _price_worker.stop()
        _price_worker.join(timeout=5)
    if _solcast_worker and _solcast_worker.is_alive():
        _solcast_worker.stop()
        _solcast_worker.join(timeout=5)


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, '').strip()))
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, '').strip())
    except (ValueError, TypeError):
        return default


class ConnectRequest(BaseModel):
    # Defaults are read from the environment (populated from .env) at request
    # time, so the Settings dialog's saved values become the connection defaults.
    host: str = Field(default_factory=lambda: os.getenv('INVERTER_HOST', '192.168.1.185').strip()
                      or '192.168.1.185')
    port: int = Field(default_factory=lambda: _env_int('INVERTER_PORT', 502))
    slave_id: int = Field(default_factory=lambda: _env_int('INVERTER_SLAVE_ID', 1))
    poll_interval: float = Field(default_factory=lambda: _env_float('INVERTER_POLL_INTERVAL', 10.0))


app = FastAPI(title='Lyset — SUN2000 Monitor', lifespan=lifespan)


# ── Static page ───────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / 'index.html').read_text(encoding='utf-8')


# ── REST: connection ──────────────────────────────────────────────────────────


@app.post('/api/connect')
async def api_connect(body: ConnectRequest):
    _start_worker(body.host, body.port, body.slave_id, body.poll_interval)
    return {'ok': True}


@app.post('/api/disconnect')
async def api_disconnect():
    global _worker
    if _worker and _worker.is_alive():
        _worker.stop()
        _worker.join(timeout=3)
        _worker = None
    _on_connection(False, 'Disconnected')
    return {'ok': True}


# ── REST: settings ────────────────────────────────────────────────────────────

def _restart_price_worker():
    global _price_worker
    if _price_worker and _price_worker.is_alive():
        _price_worker.stop()
        _price_worker.join(timeout=5)
    _price_worker = prices_from_env(on_prices=_on_prices, on_status=_on_price_status)
    if _price_worker:
        _price_worker.start()
    return _price_worker is not None


def _restart_ev_charger_worker():
    global _ev_charger_worker
    if _ev_charger_worker and _ev_charger_worker.is_alive():
        _ev_charger_worker.stop()
        _ev_charger_worker.join(timeout=5)
    _ev_charger_worker = ev_charger_from_env(on_data=_on_ev_charger_data, on_status=_on_ev_charger_status)
    if _ev_charger_worker:
        _ev_charger_worker.start()
    return _ev_charger_worker is not None


def _restart_solcast_worker():
    global _solcast_worker
    if _solcast_worker and _solcast_worker.is_alive():
        _solcast_worker.stop()
        _solcast_worker.join(timeout=5)
    _solcast_worker = solcast_from_env(on_forecast=_on_solar_forecast, on_status=_on_solcast_status)
    if _solcast_worker:
        _solcast_worker.start()
    return _solcast_worker is not None


class SettingsRequest(BaseModel):
    values: dict[str, str]


@app.get('/api/settings')
async def api_settings_get():
    return {'groups': config.schema_with_values()}


@app.post('/api/settings')
async def api_settings_post(body: SettingsRequest):
    written = await asyncio.get_running_loop().run_in_executor(
        None, config.write_settings, body.values)
    # Refresh process env from the freshly-written .env so the workers pick up
    # the new values when we rebuild them.
    load_dotenv(dotenv_path=config.ENV_PATH, override=True)

    written_set = set(written)
    restarted: list[str] = []

    if written_set & {'INVERTER_HOST', 'INVERTER_PORT', 'INVERTER_SLAVE_ID', 'INVERTER_POLL_INTERVAL'}:
        defaults = ConnectRequest()
        _start_worker(defaults.host, defaults.port, defaults.slave_id, defaults.poll_interval)
        restarted.append('inverter')

    if written_set & {'STROMLIGNING_API_KEY', 'STROMLIGNING_POSTAL_CODE',
                      'STROMLIGNING_SUPPLIER_ID', 'PRICE_EXPORT_FEE', 'PRICE_POLL_INTERVAL',
                      'PRICE_TILLAEG_ORE', 'PRICE_TRANSPORT_ORE', 'PRICE_ELAFGIFT_ORE',
                      'PRICE_VAT_PCT'}:
        _restart_price_worker()
        restarted.append('prices')

    if written_set & {'SOLCAST_API_KEY', 'SOLCAST_RESOURCE_ID',
                      'SOLCAST_FETCH_HOURS', 'SOLCAST_POLL_INTERVAL'}:
        _restart_solcast_worker()
        restarted.append('solar')

    if written_set & {'FUSIONSOLAR_USERNAME', 'FUSIONSOLAR_PASSWORD',
                      'FUSIONSOLAR_SUBDOMAIN', 'FUSIONSOLAR_EV_POLL_INTERVAL'}:
        _restart_ev_charger_worker()
        restarted.append('ev_charger')

    # CONSUMPTION_HISTORY_PATH / AUTO_CONTROLLER_AUTOSTART only take effect at
    # startup; flag them so the UI can tell the user a restart is needed.
    restart_pending = bool(written_set & {'CONSUMPTION_HISTORY_PATH', 'AUTO_CONTROLLER_AUTOSTART'})

    log.info('Settings saved: %d key(s); restarted %s%s',
             len(written), restarted or 'none',
             ' (restart pending for startup-only keys)' if restart_pending else '')
    return {'ok': True, 'written': written, 'restarted': restarted,
            'restart_pending': restart_pending}


# ── REST: register read/write ─────────────────────────────────────────────────

class WriteRequest(BaseModel):
    type: str    # 'u16' | 'u32' | 'i32'
    address: int
    value: int
    description: str = ''


@app.post('/api/write')
async def api_write(body: WriteRequest):
    if not _worker or not _worker.is_alive():
        raise HTTPException(status_code=400, detail='Not connected')
    if body.type == 'u16':
        _worker.write_u16(body.address, body.value, body.description)
    elif body.type == 'u32':
        _worker.write_u32(body.address, body.value, body.description)
    elif body.type == 'i32':
        _worker.write_i32(body.address, body.value, body.description)
    else:
        raise HTTPException(status_code=400, detail=f'Unknown register type: {body.type}')
    return {'ok': True, 'queued': True}


@app.get('/api/read')
async def api_read(address: int, type: str = 'u16'):
    if not _worker or not _worker.is_alive():
        raise HTTPException(status_code=400, detail='Not connected')
    loop = asyncio.get_running_loop()
    if type == 'u16':
        val = await loop.run_in_executor(None, _worker.read_u16_now, address)
    elif type == 'u32':
        val = await loop.run_in_executor(None, _worker.read_u32_now, address)
    else:
        raise HTTPException(status_code=400, detail=f'Unknown type: {type}')
    if val is None:
        raise HTTPException(status_code=500, detail='Read failed or register not supported')
    return {'value': val}




@app.get('/api/consumption-forecast')
async def api_consumption_forecast(full: int = 0):
    now_ms = int(time.time() * 1000)
    from_ms = 0 if full else now_ms - 86_400_000
    to_ms   = now_ms + 7 * 86_400_000 if full else now_ms + 86_400_000
    loop   = asyncio.get_running_loop()
    data   = await loop.run_in_executor(None, store.load_consumption_forecast, from_ms, to_ms)
    # DB rows carry only the mean 'w'; attach the (re-centred) p10/p90 band from a
    # fresh predict so the expanded modal chart shows confidence bands too.
    if data and _consumption_model:
        future = _consumption_model.predict(time.time(), n_slots=192)
        data = _merge_cons_bands(data, future)
    return {
        'forecast': data if data else _last_consumption_forecast,
        'coverage': _consumption_model.coverage if _consumption_model else 0,
    }


class ExcelImportRequest(BaseModel):
    path: str


@app.post('/api/consumption/import-excel')
async def api_consumption_import_excel(body: ExcelImportRequest):
    if not _consumption_model:
        raise HTTPException(status_code=503, detail='Consumption model not initialised')
    loop = asyncio.get_running_loop()
    def _do_import():
        n = _consumption_model.seed_from_excel(body.path)
        if n:
            _consumption_model.save(_MODEL_PATH)
            _save_model_and_regen()
        return n
    n = await loop.run_in_executor(None, _do_import)
    return {'ok': True, 'rows_imported': n, 'coverage': _consumption_model.coverage}


@app.get('/api/solar-forecast')
async def api_solar_forecast(full: int = 0):
    now_ms = int(time.time() * 1000)
    from_ms = 0 if full else now_ms - 86_400_000
    to_ms   = now_ms + 7 * 86_400_000 if full else now_ms + 2 * 86_400_000
    loop   = asyncio.get_running_loop()
    data   = await loop.run_in_executor(None, store.load_solar_forecast, from_ms, to_ms)
    return {'forecast': data if data else _last_solar_forecast, 'status': _solcast_status}


@app.post('/api/solar-forecast/refresh')
async def api_solar_forecast_refresh():
    if not _solcast_worker or not _solcast_worker.is_alive():
        raise HTTPException(status_code=503, detail='Solcast worker not running')
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _solcast_worker._fetch_once)
    return {'ok': True}


@app.get('/api/daily-solar')
async def api_daily_solar():
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, store.load_daily_solar, 30)

    # If today's DB row is missing a forecast (e.g. server restarted before next Solcast
    # fetch), compute it on-the-fly from the in-memory forecast cache.
    if _last_solar_forecast:
        today_local = datetime.now(_TZ_LOCAL)
        today_str   = today_local.strftime('%Y-%m-%d')
        day_start   = int(today_local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        day_end     = day_start + 86_400_000
        fc_live = round(sum(
            s['pv_w'] * 0.5 / 1000
            for s in _last_solar_forecast
            if day_start < s['ts_ms'] <= day_end and s.get('pv_w') is not None
        ), 2) or None
        if fc_live is not None:
            for d in data:
                if d['date'] == today_str:
                    if d['forecast_kwh'] is None:
                        d['forecast_kwh'] = fc_live
                    break

    return {'days': data}


@app.get('/api/power-forecast')
async def api_power_forecast(full: int = 0):
    """Return stored power forecast: past 24 h (predictions vs actuals) + next 48 h.

    full=1 returns the entire retained history so the chart modal can show the
    prediction line over the whole scrolled-back range, not just ±48 h.
    """
    now_ms = int(time.time() * 1000)
    from_ms = 0 if full else now_ms - 86_400_000
    to_ms   = now_ms + 7 * 86_400_000 if full else now_ms + 48 * 3_600_000
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, store.load_power_forecast, from_ms, to_ms)
    return {'forecast': data or _last_power_forecast}


def _forecast_accuracy(days: int) -> dict:
    """MAE/bias of the solar and consumption forecasts vs measured history over
    the last `days` days, plus the current adaptive-correction state. Bias is
    forecast − actual (positive = over-forecast). Blocking; run in executor."""
    now_ms  = int(time.time() * 1000)
    from_ms = now_ms - days * 86_400_000

    # Solar: stored 30-min forecast vs actual PV (only slots where either side
    # is meaningfully non-zero, so the night doesn't dilute the daytime error).
    fc_rows = store.load_solar_forecast(from_ms, now_ms)
    actuals = store.load_pv_avg_by_period_end(from_ms / 1000, now_ms / 1000)
    solar_pairs = [
        (r['pv_w'], actuals[r['ts_ms']])
        for r in fc_rows
        if r.get('pv_w') is not None and r['ts_ms'] in actuals
        and (r['pv_w'] >= 50 or actuals[r['ts_ms']] >= 50)
    ]
    solar = None
    if solar_pairs:
        fc_sum  = sum(f for f, a in solar_pairs)
        act_sum = sum(a for f, a in solar_pairs)
        solar = {
            'slots':      len(solar_pairs),
            'mae_w':      round(sum(abs(f - a) for f, a in solar_pairs) / len(solar_pairs)),
            'bias_w':     round((fc_sum - act_sum) / len(solar_pairs)),
            'actual_vs_forecast': round(act_sum / fc_sum, 3) if fc_sum > 0 else None,
        }

    # Consumption: stored 15-min forecast (first prediction per slot) vs actual
    # house_load slot averages.
    cons_fc  = store.load_consumption_forecast(from_ms, now_ms)
    load_act = store.load_power_avg_buckets(from_ms / 1000, now_ms / 1000)
    cons_pairs = [
        (r['w'], load_act[r['ts_ms']][0])
        for r in cons_fc
        if r.get('w') is not None and r['ts_ms'] in load_act
        and load_act[r['ts_ms']][0] is not None
    ]
    consumption = None
    if cons_pairs:
        consumption = {
            'slots':        len(cons_pairs),
            'mae_w':        round(sum(abs(f - a) for f, a in cons_pairs) / len(cons_pairs)),
            'bias_w':       round(sum(f - a for f, a in cons_pairs) / len(cons_pairs)),
            'actual_mean_w': round(sum(a for _, a in cons_pairs) / len(cons_pairs)),
        }

    return {
        'days':        days,
        'solar':       solar,
        'consumption': consumption,
        'calibration': {
            'solar':            _solar_cal.state(),
            'consumption_bias_w': round(_consumption_model.bias, 1) if _consumption_model else 0.0,
        },
    }


@app.get('/api/forecast/accuracy')
async def api_forecast_accuracy(days: int = 7):
    """How well the solar and consumption forecasts have matched reality lately,
    and what the adaptive corrections currently are. days is clamped to 1–60."""
    days = max(1, min(60, days))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _forecast_accuracy, days)


class AutoModeRequest(BaseModel):
    enabled: bool


@app.get('/api/automode')
async def api_automode_get():
    loop = asyncio.get_running_loop()
    cmds = await loop.run_in_executor(None, store.load_auto_commands, time.time() - 86_400)
    return {
        'enabled':        _auto_controller.enabled,
        'last_action':    _auto_controller.last_action,
        'last_action_ts': _auto_controller.last_action_ts,
        'commands':       cmds,
    }


@app.post('/api/automode')
async def api_automode_set(body: AutoModeRequest):
    if body.enabled:
        _auto_controller.enable(_worker, _last_prices, _last_data)
    else:
        _auto_controller.disable(_worker)
    _push({
        'type':           'auto_mode',
        'enabled':        _auto_controller.enabled,
        'last_action':    _auto_controller.last_action,
        'last_action_ts': _auto_controller.last_action_ts,
    })
    return {'ok': True, 'enabled': _auto_controller.enabled}


@app.get('/api/history/series')
async def api_history_series(key: str, from_ms: int | None = None):
    """Full retained history for one numeric metric, downsampled for the browser.

    Powers the 'scroll all history' view in the per-metric chart modal — the
    WebSocket only pushes the last 24 h, so this on-demand call backfills the rest.
    """
    from_ts = (from_ms / 1000) if from_ms is not None else 0.0
    loop = asyncio.get_running_loop()
    points = await loop.run_in_executor(None, store.load_series, key, from_ts)
    return {'key': key, 'points': points}


@app.get('/api/download/db')
async def api_download_db():
    """Download a consistent snapshot of the full history DB (polls, prices,
    solar/consumption/power forecasts, daily_solar, auto_commands). The snapshot
    is taken via SQLite's online backup so it's safe while polling continues.
    Open it with any SQLite tool — handy for offline sanity checks."""
    ts  = datetime.now(_TZ_LOCAL).strftime('%Y%m%d-%H%M%S')
    tmp = Path(tempfile.gettempdir()) / f'lyset_history_{ts}.db'
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, store.backup_to, str(tmp))
    return FileResponse(
        str(tmp), media_type='application/octet-stream',
        filename=f'lyset_history_{ts}.db',
    )


# ── EV charger cloud polling (EVChargerWorker) ────────────────────────────────

@app.get('/api/ev/charger/state')
async def api_ev_charger_state():
    """Latest EV charger snapshot plus today's charged energy (derived from
    the day's min/max of the lifetime energy counter — the API doesn't serve
    a daily figure directly). None until the worker's first successful poll."""
    loop = asyncio.get_running_loop()
    now = time.time()
    day_start = datetime.now(_TZ_LOCAL).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = await loop.run_in_executor(None, store.load_ev_charger_history, day_start, now)
    kwh_vals = [r['total_energy_kwh'] for r in today if r['total_energy_kwh'] is not None]
    energy_today_kwh = round(max(kwh_vals) - min(kwh_vals), 3) if len(kwh_vals) >= 2 else None
    return {
        'configured': _ev_charger_worker is not None,
        'status_msg': _ev_charger_status,
        'status_ok':  _ev_charger_status_ok,
        'current':    _last_ev_charger_data or None,
        'energy_today_kwh': energy_today_kwh,
    }


@app.get('/api/ev/charger/history')
async def api_ev_charger_history(hours: int = 48):
    """Raw poll history (lifetime-counter samples). hours is clamped to 1-720 (30 d)."""
    hours = max(1, min(720, hours))
    now = time.time()
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, store.load_ev_charger_history, now - hours * 3600, now)
    return {'history': data}


@app.get('/api/ev/charger/daily')
async def api_ev_charger_daily(days: int = 30):
    """kWh charged per local day (delta of the lifetime counter's last reading
    per day) for the EV Charger tab's bar chart. days clamped to 1-365."""
    days = max(1, min(365, days))
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, store.load_ev_daily_energy, days)
    return {'daily': data}


@app.get('/api/ev/charger/summary')
async def api_ev_charger_summary():
    """kWh charged today / this week / this month / this year + the lifetime
    counter, for the Dashboard's EV cards. Calendar periods, local time."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, store.load_ev_energy_summary)


@app.get('/api/prices')
async def api_prices():
    now_ms = int(time.time() * 1000)
    loop   = asyncio.get_running_loop()
    data   = await loop.run_in_executor(
        None, store.load_prices, now_ms - 86_400_000, now_ms + 7 * 86_400_000
    )
    return {'prices': data if data else _last_prices, 'status': _price_status}


# ── Savings (money saved vs. buying all household power from the grid) ─────────
# "Saved" over an interval = what buying ALL house load from the grid at the
# import price would cost, minus what we actually paid (import cost net of export
# earnings). This folds solar self-consumption, battery time-shift and arbitrage
# export into one DKK figure. Actual uses measured house_load + grid power; the
# prediction uses the persisted consumption model + power-forecast grid power
# (past forecast slots keep their earliest prediction, so past days compare).

def _step_lookup(pairs: list[tuple[int, float]], max_hold_ms: int):
    """Build a hold/step lookup over (ts_ms, value) pairs (must be sorted).

    Returns f(t_ms) = the value of the latest record at or before t, or None when
    t precedes all records or sits more than max_hold_ms past the last one (so a
    forecast that has run out isn't extrapolated indefinitely)."""
    ts   = [p[0] for p in pairs]
    vals = [p[1] for p in pairs]

    def at(t_ms: int):
        i = bisect.bisect_right(ts, t_ms) - 1
        if i < 0 or (t_ms - ts[i]) > max_hold_ms:
            return None
        return vals[i]
    return at


def _saving_increment(load_w, grid_w, imp, exp, hours) -> float:
    """DKK saved over `hours` given load & grid power (W; grid > 0 = import)."""
    load_kw = load_w / 1000.0
    grid_kw = grid_w / 1000.0
    imp_kw  = grid_kw if grid_kw > 0 else 0.0
    exp_kw  = -grid_kw if grid_kw < 0 else 0.0
    baseline = load_kw * imp * hours
    actual   = (imp_kw * imp - exp_kw * (exp or 0.0)) * hours
    return baseline - actual


def _savings_steps(start_s: int, end_s: int, step_s: int):
    """Yield (t_s, actual_dkk, pred_dkk) for each step in [start_s, end_s).

    actual_dkk/pred_dkk are the DKK saved during that step, or None when the
    respective source has no data there. Runs entirely off the history DB so it
    covers any past day, not just the browser's live 24 h window."""
    hours    = step_s / 3600.0
    start_ms = start_s * 1000
    end_ms   = end_s * 1000

    actual_map = store.load_power_avg_buckets(start_s, end_s, step_s)
    prices     = store.load_prices(start_ms - 3_600_000, end_ms + 3_600_000)
    imp_at = _step_lookup([(p['ts'], p['import']) for p in prices], 2 * 3_600_000)
    exp_at = _step_lookup([(p['ts'], p['export']) for p in prices], 2 * 3_600_000)

    cons = store.load_consumption_forecast(start_ms, end_ms)
    powf = store.load_power_forecast(start_ms, end_ms)
    load_at = _step_lookup([(r['ts_ms'], r['w'])      for r in cons if r['w']      is not None], 30 * 60_000)
    grid_at = _step_lookup([(r['ts_ms'], r['grid_w']) for r in powf if r['grid_w'] is not None], 45 * 60_000)

    t = start_s
    while t < end_s:
        t_ms = t * 1000
        imp  = imp_at(t_ms)
        if imp is None:
            yield t, None, None
            t += step_s
            continue
        exp = exp_at(t_ms)
        a   = actual_map.get(t_ms)
        act = (_saving_increment(a[0], a[1], imp, exp, hours)
               if a and a[0] is not None and a[1] is not None else None)
        pl, pg = load_at(t_ms), grid_at(t_ms)
        prd = (_saving_increment(pl, pg, imp, exp, hours)
               if pl is not None and pg is not None else None)
        yield t, act, prd
        t += step_s


@app.get('/api/savings/daily')
async def api_savings_daily(date: str | None = None):
    """Cumulative savings (DKK) across one local calendar day.

    `date` = YYYY-MM-DD (local); defaults to today. Returns the measured cumulative
    curve up to now (or the whole day, if past) and the predicted cumulative curve
    for the full day, at 5-min resolution."""
    now_local = datetime.now(_TZ_LOCAL)
    if date:
        try:
            day = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=_TZ_LOCAL)
        except ValueError:
            raise HTTPException(status_code=400, detail='date must be YYYY-MM-DD')
    else:
        day = now_local
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    start_s, end_s = int(start.timestamp()), int(end.timestamp())
    now_s = time.time()

    def _build():
        actual, pred = [], []
        ca = cp = 0.0
        have_actual = False
        for t, act, prd in _savings_steps(start_s, end_s, 300):
            t_ms = t * 1000
            if prd is not None:
                cp += prd
            pred.append({'ts_ms': t_ms, 'y': round(cp, 4)})
            if t <= now_s:
                if act is not None:
                    ca += act
                    have_actual = True
                actual.append({'ts_ms': t_ms, 'y': round(ca, 4)})
        return actual, pred, ca, cp, have_actual

    loop = asyncio.get_running_loop()
    actual, pred, ca, cp, have_actual = await loop.run_in_executor(None, _build)
    return {
        'date': start.strftime('%Y-%m-%d'),
        'is_today': start.date() == now_local.date(),
        'actual': actual, 'predicted': pred,
        'actual_total': round(ca, 4) if have_actual else None,
        'predicted_total': round(cp, 4),
    }


def _bucket_plan(period: str, now_local: datetime):
    """Return (period_start, period_end, key_of, ordered_buckets) for a period.

    ordered_buckets is a list of {'key', 'label'} defining the bars in order;
    key_of(local_dt) maps a timestamp to one of those keys (or a key absent from
    the set, which the caller skips)."""
    _DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    _MONS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
             'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    if period == 'week':
        start = midnight - timedelta(days=midnight.weekday())
        end   = start + timedelta(days=7)
        buckets = [{'key': k, 'label': _DAYS[k]} for k in range(7)]
        return start, end, (lambda d: d.weekday()), buckets

    if period == 'month':
        start = midnight.replace(day=1)
        end   = (start.replace(year=start.year + 1, month=1) if start.month == 12
                 else start.replace(month=start.month + 1))
        buckets, cur = [], start - timedelta(days=start.weekday())  # Monday of week 1
        while cur < end:
            lab = cur if cur >= start else start          # first in-month day of the week
            buckets.append({'key': cur.date().isoformat(), 'label': f'{lab.day}/{lab.month}'})
            cur += timedelta(days=7)
        return start, end, (lambda d: (d - timedelta(days=d.weekday())).date().isoformat()), buckets

    # year
    start = midnight.replace(month=1, day=1)
    end   = start.replace(year=start.year + 1)
    buckets = [{'key': m, 'label': _MONS[m]} for m in range(now_local.month)]  # Jan..current
    return start, end, (lambda d: d.month - 1), buckets


@app.get('/api/savings/buckets')
async def api_savings_buckets(period: str = 'week'):
    """Per-bucket actual & predicted savings for the current calendar period.

    period=week → one bar per day (Mon–Sun); month → one bar per calendar week;
    year → one bar per month (Jan–current)."""
    if period not in ('week', 'month', 'year'):
        raise HTTPException(status_code=400, detail='period must be week|month|year')
    now_local = datetime.now(_TZ_LOCAL)
    start, end, key_of, buckets = _bucket_plan(period, now_local)
    # Nothing is measured or forecast beyond ~2 days out — cap the walk there.
    walk_end = min(end, now_local + timedelta(days=2))
    start_s, walk_end_s = int(start.timestamp()), int(walk_end.timestamp())

    def _build():
        acc = {b['key']: [0.0, 0.0, False] for b in buckets}   # [actual, pred, had_actual]
        for t, act, prd in _savings_steps(start_s, walk_end_s, 900):
            k = key_of(datetime.fromtimestamp(t, _TZ_LOCAL))
            slot = acc.get(k)
            if slot is None:
                continue
            if act is not None:
                slot[0] += act
                slot[2] = True
            if prd is not None:
                slot[1] += prd
        return acc

    loop = asyncio.get_running_loop()
    acc = await loop.run_in_executor(None, _build)
    out = [{'label': b['label'],
            'actual': round(acc[b['key']][0], 4) if acc[b['key']][2] else None,
            'predicted': round(acc[b['key']][1], 4)} for b in buckets]
    actual_total = round(sum(acc[b['key']][0] for b in buckets
                             if acc[b['key']][2]), 4)
    pred_total   = round(sum(acc[b['key']][1] for b in buckets), 4)
    return {'period': period, 'buckets': out,
            'actual_total': actual_total, 'predicted_total': pred_total}


def _total_actual_savings(from_s: int, to_s: int, step_s: int = 900) -> float:
    """Total measured DKK saved over [from_s, to_s), summed from the 15-min
    SQL-averaged polls priced against the stored import/export rates. Lighter than
    _savings_steps — no forecast lookups, and it visits only the buckets that exist
    (so downtime gaps contribute nothing)."""
    actual_map = store.load_power_avg_buckets(from_s, to_s, step_s)
    if not actual_map:
        return 0.0
    prices = store.load_prices(from_s * 1000 - 3_600_000, to_s * 1000 + 3_600_000)
    imp_at = _step_lookup([(p['ts'], p['import']) for p in prices], 2 * 3_600_000)
    exp_at = _step_lookup([(p['ts'], p['export']) for p in prices], 2 * 3_600_000)
    hours  = step_s / 3600.0
    total  = 0.0
    for ms, (load, grid) in actual_map.items():
        imp = imp_at(ms)
        if imp is None:
            continue
        total += _saving_increment(load, grid, imp, exp_at(ms), hours)
    return total


def _ymd_between(d0, d1):
    """Calendar (years, months, days) from date d0 to date d1.

    Borrowing uses d0's own month length, which keeps every component
    non-negative (d0.day ≤ that length) and reads as the intuitive
    "N months M days" — e.g. 31 Jan → 1 Mar = (0, 1, 1)."""
    import calendar
    if d1 <= d0:
        return 0, 0, 0
    years  = d1.year - d0.year
    months = d1.month - d0.month
    days   = d1.day - d0.day
    if days < 0:
        months -= 1
        days += calendar.monthrange(d0.year, d0.month)[1]
    if months < 0:
        years -= 1
        months += 12
    return years, months, days


@app.get('/api/roi')
async def api_roi():
    """Payback / ROI estimate for the PV system.

    Uses the total measured savings so far to derive an average DKK/day rate, then
    projects when cumulative savings recoup the configured system cost, counting
    from the installation date. Everything is an estimate — the rate is whatever
    the (short) measured history has averaged, extrapolated forward."""
    settings = config.read_settings()
    cost_raw    = (settings.get('PV_SYSTEM_COST') or '').strip()
    install_raw = (settings.get('PV_INSTALL_DATE') or '').strip()
    try:
        cost = float(cost_raw)
    except ValueError:
        cost = None
    install_date = None
    if install_raw:
        try:
            install_date = datetime.strptime(install_raw, '%Y-%m-%d').date()
        except ValueError:
            install_date = None

    if not cost or cost <= 0 or install_date is None:
        return {'configured': False}

    now_local = datetime.now(_TZ_LOCAL)
    today = now_local.date()
    days_since_install = max((today - install_date).days, 0)

    rng = await asyncio.get_running_loop().run_in_executor(None, store.poll_ts_range)
    now_s = time.time()

    def _rate():
        if not rng:
            return 0.0, 0.0
        earliest_s = rng[0]
        total = _total_actual_savings(int(earliest_s), int(now_s))
        span_days = max((now_s - earliest_s) / 86400.0, 0.5)
        return total, span_days

    total_saved, span_days = await asyncio.get_running_loop().run_in_executor(None, _rate)
    avg_daily = (total_saved / span_days) if span_days else 0.0

    est_saved = avg_daily * days_since_install
    recouped_pct = (est_saved / cost * 100.0) if cost else 0.0

    result = {
        'configured': True,
        'cost': round(cost, 2),
        'install_date': install_date.isoformat(),
        'days_since_install': days_since_install,
        'avg_daily': round(avg_daily, 4),
        'est_saved': round(est_saved, 2),
        'recouped_pct': round(recouped_pct, 1),
        'measured_days': round(span_days, 1),
    }

    if avg_daily <= 0:
        result.update(status='never', break_even_date=None, ymd=None)
        return result

    total_days = cost / avg_daily
    break_even_date = install_date + timedelta(days=round(total_days))
    result['break_even_date'] = break_even_date.isoformat()
    if break_even_date <= today:
        result['status'] = 'reached'
        result['ymd'] = dict(zip(('years', 'months', 'days'),
                                 _ymd_between(break_even_date, today)))
    else:
        result['status'] = 'projected'
        result['ymd'] = dict(zip(('years', 'months', 'days'),
                                 _ymd_between(today, break_even_date)))
    return result


@app.delete('/api/history')
async def api_clear_history():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, store.clear_history)
    global _last_data
    _last_data = {}
    _push({'type': 'history', 'points': []})
    log.info('History cleared by user request')
    return {'ok': True}


@app.get('/api/ha/snapshot')
async def api_ha_snapshot():
    """
    Flat JSON snapshot for Home Assistant REST sensor integration.
    Returns current inverter measurements + active prices + next-slot forecasts
    in a single call, suitable for HA json_attributes parsing.
    Poll every 30 s — Modbus data refreshes every 10 s, prices every 30 min.
    """
    now_ms = int(time.time() * 1000)

    # Active price slot: last record whose ts <= now_ms (list is sorted ascending)
    cur_price = None
    for p in reversed(_last_prices):
        if p['ts'] <= now_ms:
            cur_price = p
            break

    # Next Solcast period (period_end timestamps, so the first ts_ms strictly > now)
    next_solar = next((s for s in _last_solar_forecast if s['ts_ms'] > now_ms), None)

    # Next power forecast slot
    next_power = next((pf for pf in _last_power_forecast if pf['ts_ms'] > now_ms), None)

    # Active 15-min consumption forecast slot (slot_start ts_ms; last one <= now_ms)
    cur_consumption = None
    for cf in _last_consumption_forecast:
        if cf['ts_ms'] <= now_ms:
            cur_consumption = cf
        else:
            break

    pv_w = None
    if _last_data:
        pv_w = (_last_data.get('pv1_power') or 0) + (_last_data.get('pv2_power') or 0)

    # Full-day arrays for ApexCharts data_generator (local calendar day)
    today_start = datetime.now(_TZ_LOCAL).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ms = int(today_start.timestamp() * 1000)
    today_end_ms   = today_start_ms + 86_400_000

    solar_forecast_today = [
        {'ts_ms': s['ts_ms'], 'pv_w': s.get('pv_w'), 'p10_w': s.get('p10_w'), 'p90_w': s.get('p90_w')}
        for s in _last_solar_forecast
        if today_start_ms < s['ts_ms'] <= today_end_ms
    ]
    prices_today = [
        {'ts': p['ts'], 'import': p['import'], 'export': p['export'], 'resolution': p.get('resolution', '1h')}
        for p in _last_prices
        if today_start_ms <= p['ts'] < today_end_ms
    ]

    # kWh produced today — from inverter's own daily counter (resets at midnight)
    daily_yield_kwh = _last_data.get('daily_yield') if _last_data else None

    # Solcast estimate of total solar production for the full calendar day (sum of 30-min periods)
    solar_fc_kwh_today = None
    if _last_solar_forecast:
        total = sum(
            s['pv_w'] * 0.5 / 1000
            for s in _last_solar_forecast
            if today_start_ms < s['ts_ms'] <= today_end_ms
            if s.get('pv_w') is not None
        )
        solar_fc_kwh_today = round(total, 2)

    return {
        'ts_ms':     now_ms,
        'connected': _connected,

        # Real-time inverter measurements
        'pv_w':           pv_w,
        'grid_w':          _last_data.get('meter_active_power'),  # + import, − export
        'batt_w':          _last_data.get('batt_power'),           # + charging, − discharging
        'batt_soc':        _last_data.get('batt_soc'),
        'house_load_w':    _last_data.get('house_load'),
        'inverter_state':  _last_data.get('inverter_state'),

        # Daily energy totals
        'daily_yield_kwh':    daily_yield_kwh,    # actual kWh produced today (inverter counter)
        'solar_fc_kwh_today': solar_fc_kwh_today, # Solcast estimate of full-day yield

        # Active electricity prices (DKK/kWh)
        'import_price_dkk': cur_price['import'] if cur_price else None,
        'export_price_dkk': cur_price['export'] if cur_price else None,

        # Next Solcast solar period (mean + P10/P90 confidence)
        'solar_fc_w':     next_solar['pv_w']   if next_solar else None,
        'solar_fc_p10_w': next_solar['p10_w']  if next_solar else None,
        'solar_fc_p90_w': next_solar['p90_w']  if next_solar else None,
        'solar_fc_ts_ms': next_solar['ts_ms']  if next_solar else None,

        # Next power simulation slot
        'forecast_soc':    next_power['soc']    if next_power else None,
        'forecast_batt_w': next_power['batt_w'] if next_power else None,
        'forecast_grid_w': next_power['grid_w'] if next_power else None,
        'forecast_ts_ms':  next_power['ts_ms']  if next_power else None,

        # Active consumption forecast slot
        'consumption_fc_w': cur_consumption['w'] if cur_consumption else None,

        # Full-day arrays for ApexCharts cards (ts_ms = period_end for solar, slot_start for prices)
        'solar_forecast_today': solar_forecast_today,
        'prices_today':         prices_today,
    }


@app.post('/api/pymodbus-logs')
async def api_pymodbus_logs(body: dict):
    enabled = bool(body.get('enabled', False))
    level = logging.DEBUG if enabled else logging.WARNING
    logging.getLogger('pymodbus').setLevel(level)
    return {'enabled': enabled}


@app.get('/api/state')
async def api_state():
    return {
        'connected': _connected,
        'msg': _connection_msg,
        'data': _last_data,
        'log': _log_buffer[-100:],
        'price_status': _price_status,
        'price_status_ok': _price_status_ok,
    }


# ── WebSocket ─────────────────────────────────────────────────────────────────

def _soc_max_change(elapsed_s: float) -> float:
    return max(2.0, elapsed_s / 40.0)


def _clean_history_soc(history: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """5-point sliding median on batt_soc — removes spikes up to 2 consecutive polls."""
    HALF = 2
    # Collect (list-index, soc) for rows that have batt_soc
    indexed = [(i, d['batt_soc']) for i, (_, d) in enumerate(history)
               if d.get('batt_soc') is not None]
    if len(indexed) < 3:
        return history
    socs = [s for _, s in indexed]
    n    = len(socs)
    replacements: dict[int, float] = {}
    for j, (orig_i, original) in enumerate(indexed):
        lo  = max(0, j - HALF)
        hi  = min(n, j + HALF + 1)
        med = round(statistics.median(socs[lo:hi]), 1)
        if abs(med - original) >= 0.5:
            replacements[orig_i] = med
    if not replacements:
        return history
    return [
        (ts, {**d, 'batt_soc': replacements[i]} if i in replacements else d)
        for i, (ts, d) in enumerate(history)
    ]


@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        # Send history before registering — large payload, keep it off the broadcaster path.
        loop = asyncio.get_running_loop()
        history = await loop.run_in_executor(None, store.load_last_24h)
        if history:
            history = _clean_history_soc(history)
            pts = [{'ts': int(ts * 1000), 'data': d} for ts, d in history]
            await ws.send_text(json.dumps({'type': 'history', 'points': pts}))

        # Send current state before registering — avoids a concurrent-send race
        # with _broadcaster which could silently prune this ws from _ws_clients.
        await ws.send_text(json.dumps({
            'type': 'connection', 'ok': _connected, 'msg': _connection_msg,
        }))
        if _last_data:
            await ws.send_text(json.dumps({'type': 'data', 'payload': _last_data}, default=str))
        if _last_prices:
            await ws.send_text(json.dumps({'type': 'prices', 'payload': _last_prices}))
        if _last_solar_forecast:
            await ws.send_text(json.dumps({'type': 'solar_forecast', 'payload': _last_solar_forecast}))
        if _last_consumption_forecast:
            await ws.send_text(json.dumps({'type': 'consumption_forecast', 'payload': _last_consumption_forecast}))
        if _last_power_forecast:
            await ws.send_text(json.dumps({'type': 'power_forecast', 'payload': _last_power_forecast}))
        # Send auto-mode state and command history
        auto_cmds = await loop.run_in_executor(None, store.load_auto_commands, time.time() - 86_400)
        if auto_cmds:
            await ws.send_text(json.dumps({'type': 'auto_history', 'commands': auto_cmds}))
        await ws.send_text(json.dumps({
            'type':           'auto_mode',
            'enabled':        _auto_controller.enabled,
            'last_action':    _auto_controller.last_action,
            'last_action_ts': _auto_controller.last_action_ts,
        }))
        _ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        finally:
            _ws_clients.discard(ws)
    except WebSocketDisconnect:
        pass
