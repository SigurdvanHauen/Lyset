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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from pymodbus.client import ModbusTcpClient

from .modbus_client import ModbusWorker
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
    global _last_data
    # Strip private keys and non-JSON-serialisable values
    clean = {k: v for k, v in data.items()
             if not k.startswith('_') and isinstance(v, (int, float, str, bool, type(None)))}
    _last_data = clean
    _push({'type': 'data', 'payload': clean})
    store.save(data.get('_timestamp', time.time()), clean)


def _on_connection(ok: bool, msg: str):
    global _connected, _connection_msg
    _connected = ok
    _connection_msg = msg
    _push({'type': 'connection', 'ok': ok, 'msg': msg})


def _on_write_result(ok: bool, msg: str):
    _push({'type': 'write_result', 'ok': ok, 'msg': msg})


def _on_error(msg: str):
    _push({'type': 'error', 'msg': msg})


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

    # Auto-connect on startup using the default settings
    defaults = ConnectRequest()
    _start_worker(defaults.host, defaults.port, defaults.slave_id, defaults.poll_interval)

    yield

    if _worker and _worker.is_alive():
        _worker.stop()
        _worker.join(timeout=3)


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


import socket as _socket


def _probe_ports(host: str, ports: list[int], timeout: float = 2.0) -> list[int]:
    """Return which TCP ports on host accept connections."""
    open_ports = []
    for p in ports:
        try:
            s = _socket.create_connection((host, p), timeout=timeout)
            s.close()
            open_ports.append(p)
        except Exception:
            pass
    return open_ports


def _modbus_scan_once(host: str, port: int, slave_id: int,
                       fc: int, start: int, count: int, timeout: float = 3.0
                       ) -> tuple[dict[int, int], str | None]:
    """Open a fresh connection, read one batch, close. Returns (raw_regs, error_str)."""
    client = ModbusTcpClient(host=host, port=port, timeout=timeout, retries=0)
    if not client.connect():
        return {}, f'TCP connect failed'
    try:
        if fc == 3:
            r = client.read_holding_registers(address=start, count=count, device_id=slave_id)
        else:
            r = client.read_input_registers(address=start, count=count, device_id=slave_id)
        if r.isError():
            return {}, str(r)
        return {start + i: v for i, v in enumerate(r.registers)}, None
    except Exception as exc:
        return {}, str(exc)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _charger_scan_sync(host: str, port: int, slave_id: int, fc: int) -> dict:
    """Full register scan — call via run_in_executor."""
    RANGES = [
        (0, 25), (25, 25), (50, 25), (75, 25),
        (100, 25), (125, 25),
        (1000, 25), (1025, 25), (1050, 25), (1075, 25),
        (1100, 25), (1125, 25), (1150, 25), (1175, 25),
        (1200, 25),
        (2000, 25), (2025, 25),
        (3000, 25),
        (40000, 25),
        (47000, 25),
    ]

    # Quick port probe so we know what's actually listening
    probe_ports = [502, 6607, 8080, 8899, 8888, 9999, 10000]
    open_ports = _probe_ports(host, probe_ports)

    registers: dict[int, int] = {}
    scan_errors: dict[str, str] = {}

    # Verify the target port is open first
    if port not in open_ports and not any(True for p in open_ports if p == port):
        # Still try even if probe missed it, but flag it
        pass

    for start, count in RANGES:
        raw, err = _modbus_scan_once(host, port, slave_id, fc, start, count)
        if err:
            scan_errors[str(start)] = err
        else:
            for addr, val in raw.items():
                if val != 0:
                    registers[addr] = val

    return {
        'ok': True, 'host': host, 'port': port, 'slave_id': slave_id, 'fc': fc,
        'open_ports': open_ports,
        'registers': registers,
        'scan_errors': scan_errors,
    }


@app.get('/api/charger/scan')
async def charger_scan(
    host: str = '192.168.1.97',
    port: int = 502,
    slave_id: int = 1,
    fc: int = 3,
):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _charger_scan_sync, host, port, slave_id, fc)


@app.get('/api/state')
async def api_state():
    return {
        'connected': _connected,
        'msg': _connection_msg,
        'data': _last_data,
        'log': _log_buffer[-100:],
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
        _ws_clients.add(ws)
        try:
            while True:
                await ws.receive_text()
        finally:
            _ws_clients.discard(ws)
    except WebSocketDisconnect:
        pass
