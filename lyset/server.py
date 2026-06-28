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
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from pymodbus.client import ModbusTcpClient

from .modbus_client import ModbusWorker
from .prices import PriceWorker, worker_from_env as prices_from_env
from .solcast import SolcastWorker, worker_from_env as solcast_from_env
from .consumption_model import ConsumptionModel
from .auto_controller import AutoController
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
_auto_controller: AutoController = AutoController()
_MODEL_PATH = Path(__file__).parent.parent / 'lyset_model.json'
_TZ_LOCAL = ZoneInfo('Europe/Copenhagen')

# Accumulator: collect 10-s Modbus samples within the current 15-min slot
_slot_samples: list[float] = []   # watts values
_slot_key: int = -1               # int(ts_utc / 900)
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
        self.setFormatter(logging.Formatter(
            '%(asctime)s  %(levelname)-8s  %(name)s — %(message)s',
            datefmt='%H:%M:%S',
        ))

    def emit(self, record: logging.LogRecord):
        try:
            entry = {
                't': time.strftime('%H:%M:%S'),
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

def _simulate_soc(
    start_soc: float,
    capacity_kwh: float,
    solar_fc: list[dict],
    load_fc: list[dict],
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
    Returns:   [{ts_ms, soc, batt_w, grid_w}, ...]
      batt_w: positive = charging, negative = discharging  (matches batt_power key)
      grid_w: positive = importing, negative = exporting   (matches meter_active_power key)
    """
    if not solar_fc or capacity_kwh <= 0:
        return []

    cutoff_ms = start_ms if start_ms is not None else int(time.time() * 1000)
    load_by_ts = {r['ts_ms']: r['w'] for r in load_fc if r.get('w') is not None}
    max_power_kw = capacity_kwh * 0.5  # C/2 — LUNA2000 rated charge rate heuristic

    soc = start_soc
    result = [{'ts_ms': cutoff_ms, 'soc': round(soc, 1), 'batt_w': None, 'grid_w': None}]

    for rec in sorted(solar_fc, key=lambda r: r['ts_ms']):
        ts_ms = rec['ts_ms']
        if ts_ms <= cutoff_ms:
            continue

        pv_w = rec.get('pv_w') or 0.0

        # Map Solcast period [ts_ms-30min, ts_ms] to two 15-min consumption slots
        slot1_ms = ts_ms - 30 * 60 * 1000
        slot2_ms = ts_ms - 15 * 60 * 1000
        load1 = load_by_ts.get(slot1_ms)
        load2 = load_by_ts.get(slot2_ms)

        if load1 is not None and load2 is not None:
            load_w = (load1 + load2) / 2.0
        elif load1 is not None:
            load_w = load1
        elif load2 is not None:
            load_w = load2
        else:
            continue  # no consumption data for this period

        # net_kw > 0: surplus (solar > load); < 0: deficit (load > solar)
        net_kw = (pv_w - load_w) / 1000.0

        # Battery takes surplus / covers deficit, clamped by max rate and SoC limits
        if net_kw >= 0:
            batt_kw = 0.0 if soc >= 100.0 else min(net_kw, max_power_kw)
        else:
            batt_kw = 0.0 if soc <= min_soc else max(net_kw, -max_power_kw)

        # Grid makes up the remainder (energy balance: solar + grid = load + batt_charge)
        grid_kw = batt_kw - net_kw  # positive = import, negative = export

        if batt_kw >= 0:
            energy_kwh = batt_kw * 0.5 * charge_eff
        else:
            energy_kwh = batt_kw * 0.5 / discharge_eff

        soc = max(min_soc, min(100.0, soc + energy_kwh / capacity_kwh * 100.0))
        result.append({
            'ts_ms': ts_ms,
            'soc':    round(soc, 1),
            'batt_w': round(batt_kw * 1000),
            'grid_w': round(grid_kw * 1000),
        })

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


def _push_power_forecast(fc: list[dict]):
    """Save simulation output to DB and push the full stored window to browsers."""
    global _last_power_forecast
    now_ms = int(time.time() * 1000)
    store.save_power_forecast(fc, now_ms)
    combined = store.load_power_forecast(now_ms - 86_400_000, now_ms + 48 * 3_600_000)
    _last_power_forecast = combined or fc
    _push({'type': 'power_forecast', 'payload': _last_power_forecast})


# ── Worker callbacks (called from the Modbus daemon thread) ───────────────────

def _on_data(data: dict):
    global _last_data, _slot_samples, _slot_key, _last_batt_soc, _last_batt_soc_ts, _pv_yield_logged
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
            if _slot_samples and _slot_key >= 0:
                avg_w = sum(_slot_samples) / len(_slot_samples)
                slot_ts = _slot_key * 900.0
                _consumption_model.update(slot_ts, avg_w)
                _save_model_and_regen()
            _slot_samples = [grid_import_w]
            _slot_key = key
        else:
            _slot_samples.append(grid_import_w)

    # Save and push power forecast whenever we have all prerequisites
    soc_val = clean.get('batt_soc')
    cap_val = clean.get('batt_rated_capacity')
    if (soc_val is not None and cap_val and cap_val > 0
            and _last_solar_forecast and _last_consumption_forecast):
        if not _backfill_done:
            _backfill_power_forecast(cap_val)
        fc = _simulate_soc(soc_val, cap_val, _last_solar_forecast, _last_consumption_forecast)
        if fc:
            _push_power_forecast(fc)


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
    # Push the combined window: past 24h of predictions + next 24h
    now_ms = int(time.time() * 1000)
    combined = store.load_consumption_forecast(now_ms - 86_400_000, now_ms + 86_400_000)
    _last_consumption_forecast = combined or future
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


def _on_solar_forecast(records: list[dict]):
    global _last_solar_forecast
    store.save_solar_forecast(records)
    # Push the full stored window (past 24 h preserved + new future data) so the
    # chart shows historical forecasts alongside actuals, not just the latest fetch.
    now_ms = int(time.time() * 1000)
    full = store.load_solar_forecast(now_ms - 86_400_000, now_ms + 2 * 86_400_000)
    _last_solar_forecast = full or records
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
    for name in ('lyset', 'pymodbus'):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)

    store.init()
    purged = store.purge_power_outliers()
    if purged:
        log.warning('Startup: purged %d poll row(s) with outlier power values', purged)
    cleaned = store.clean_soc_history()
    if cleaned:
        log.warning('Startup: cleaned %d poll row(s) with SoC outlier values', cleaned)

    # Auto-connect inverter worker
    defaults = ConnectRequest()
    _start_worker(defaults.host, defaults.port, defaults.slave_id, defaults.poll_interval)

    # Start electricity price worker if API key is configured
    global _price_worker
    _price_worker = prices_from_env(on_prices=_on_prices, on_status=_on_price_status)
    if _price_worker:
        _price_worker.start()

    # Start Solcast solar forecast worker if API key is configured
    global _solcast_worker, _last_solar_forecast
    _solcast_worker = solcast_from_env(on_forecast=_on_solar_forecast, on_status=_on_solcast_status)
    if _solcast_worker:
        _solcast_worker.start()

    # Restore last solar forecast from DB so it's available immediately after restart
    now_ms = int(time.time() * 1000)
    cached_fc = store.load_solar_forecast(now_ms - 86_400_000, now_ms + 2 * 86_400_000)
    if cached_fc:
        _last_solar_forecast = cached_fc
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

    if _consumption_model and _consumption_model.coverage > 0:
        future = _consumption_model.predict(time.time())
        store.save_consumption_forecast(future)
        now_ms = int(time.time() * 1000)
        stored = store.load_consumption_forecast(now_ms - 86_400_000, now_ms + 86_400_000)
        _last_consumption_forecast = stored or future

    # Restore stored power forecast from DB
    global _last_power_forecast
    now_ms = int(time.time() * 1000)
    _last_power_forecast = store.load_power_forecast(now_ms - 86_400_000, now_ms + 48 * 3_600_000)
    if _last_power_forecast:
        log.info('PowerForecast: restored %d stored periods from DB', len(_last_power_forecast))

    # Start auto controller — disabled by default, user enables via UI
    _auto_controller.set_command_callback(_on_auto_command)
    asyncio.create_task(_auto_controller.run(
        lambda: _worker, lambda: _last_prices, lambda: _last_data,
    ))
    log.info('AutoCtrl: task started (disabled by default — enable via UI)')

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


class ConnectRequest(BaseModel):
    host: str = '192.168.1.185'
    port: int = 502
    slave_id: int = 1
    poll_interval: float = 10.0


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
async def api_consumption_forecast():
    now_ms = int(time.time() * 1000)
    loop   = asyncio.get_running_loop()
    data   = await loop.run_in_executor(
        None, store.load_consumption_forecast, now_ms - 86_400_000, now_ms + 86_400_000
    )
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
async def api_solar_forecast():
    now_ms = int(time.time() * 1000)
    loop   = asyncio.get_running_loop()
    data   = await loop.run_in_executor(
        None, store.load_solar_forecast, now_ms - 86_400_000, now_ms + 2 * 86_400_000
    )
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
async def api_power_forecast():
    """Return stored power forecast: past 24 h (predictions vs actuals) + next 48 h."""
    now_ms = int(time.time() * 1000)
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        None, store.load_power_forecast, now_ms - 86_400_000, now_ms + 48 * 3_600_000
    )
    return {'forecast': data or _last_power_forecast}


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


@app.get('/api/prices')
async def api_prices():
    now_ms = int(time.time() * 1000)
    loop   = asyncio.get_running_loop()
    data   = await loop.run_in_executor(
        None, store.load_prices, now_ms - 86_400_000, now_ms + 7 * 86_400_000
    )
    return {'prices': data if data else _last_prices, 'status': _price_status}


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
    """Apply the same time-weighted SoC outlier filter to historical DB rows."""
    last_soc: float | None = None
    last_ts: float = 0.0
    result = []
    for ts, d in history:
        soc = d.get('batt_soc')
        if soc is not None:
            if last_soc is not None:
                elapsed_s = max(ts - last_ts, 5.0)
                if abs(soc - last_soc) > _soc_max_change(elapsed_s):
                    d = {**d, 'batt_soc': last_soc}
                    soc = last_soc
            last_soc = soc
            last_ts = ts
        result.append((ts, d))
    return result


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
