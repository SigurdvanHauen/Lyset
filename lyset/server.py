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
_MODEL_PATH = Path(__file__).parent.parent / 'lyset_model.json'
_TZ_LOCAL = ZoneInfo('Europe/Copenhagen')

# Accumulator: collect 10-s Modbus samples within the current 15-min slot
_slot_samples: list[float] = []   # watts values
_slot_key: int = -1               # int(ts_utc / 900)


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


# ── Worker callbacks (called from the Modbus daemon thread) ───────────────────

def _on_data(data: dict):
    global _last_data, _slot_samples, _slot_key
    # Strip private keys and non-JSON-serialisable values
    clean = {k: v for k, v in data.items()
             if not k.startswith('_') and isinstance(v, (int, float, str, bool, type(None)))}
    _last_data = clean
    _push({'type': 'data', 'payload': clean})
    ts = data.get('_timestamp', time.time())
    store.save(ts, clean)

    # Accumulate grid import for the current 15-min slot
    meter_w = clean.get('meter_active_power')
    if meter_w is not None and _consumption_model is not None:
        grid_import_w = max(0.0, -meter_w)   # negative meter = importing from grid
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


def _save_model_and_regen():
    global _last_consumption_forecast
    try:
        _consumption_model.save(_MODEL_PATH)
    except Exception as exc:
        log.warning('ConsumptionModel: save failed — %s', exc)
    forecast = _consumption_model.predict(time.time())
    _last_consumption_forecast = forecast
    store.save_consumption_forecast(forecast)
    _push({'type': 'consumption_forecast', 'payload': forecast})


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
    _last_solar_forecast = records
    store.save_solar_forecast(records)
    _push({'type': 'solar_forecast', 'payload': records})


def _on_solcast_status(msg: str, ok: bool):
    global _solcast_status, _solcast_status_ok
    _solcast_status = msg
    _solcast_status_ok = ok


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

    # Auto-connect inverter worker
    defaults = ConnectRequest()
    _start_worker(defaults.host, defaults.port, defaults.slave_id, defaults.poll_interval)

    # Start electricity price worker if API key is configured
    global _price_worker
    _price_worker = prices_from_env(on_prices=_on_prices, on_status=_on_price_status)
    if _price_worker:
        _price_worker.start()

    # Start Solcast solar forecast worker if API key is configured
    global _solcast_worker
    _solcast_worker = solcast_from_env(on_forecast=_on_solar_forecast, on_status=_on_solcast_status)
    if _solcast_worker:
        _solcast_worker.start()

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
        _last_consumption_forecast = _consumption_model.predict(time.time())

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
        None, store.load_consumption_forecast, now_ms, now_ms + 86_400_000
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


@app.get('/api/prices')
async def api_prices():
    now_ms = int(time.time() * 1000)
    loop   = asyncio.get_running_loop()
    data   = await loop.run_in_executor(
        None, store.load_prices, now_ms - 86_400_000, now_ms + 7 * 86_400_000
    )
    return {'prices': data if data else _last_prices, 'status': _price_status}


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

@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        # Send history before registering — large payload, keep it off the broadcaster path.
        loop = asyncio.get_running_loop()
        history = await loop.run_in_executor(None, store.load_last_24h)
        if history:
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
        _ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        finally:
            _ws_clients.discard(ws)
    except WebSocketDisconnect:
        pass
