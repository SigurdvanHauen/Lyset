# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

```bash
pip install -r requirements.txt
python main.py          # starts FastAPI on 0.0.0.0:8000
```

There is no test suite and no linter config. The only runtime dependency check is `pip install -r requirements.txt`.

## Architecture

Lyset has two coexisting phases:

- **Phase 1 (legacy):** `lyset/gui/` — PySide6 desktop app. Not the primary interface but kept in the repo. The README still describes this phase.
- **Phase 2 (current):** `lyset/server.py` + `lyset/static/index.html` — FastAPI web server with WebSocket-based live dashboard. `main.py` starts this via uvicorn.

### Core data flow

```
ModbusWorker (daemon thread)
  → _on_data() callback
  → _push() — loop.call_soon_threadsafe(_msg_queue.put_nowait, text)
  → _broadcaster() asyncio task — fans out to _ws_clients
  → browser WebSocket
```

`run_coroutine_threadsafe` must **not** be used for this path — it silently drops futures on Windows. Use `call_soon_threadsafe` + `asyncio.Queue` instead (already in place).

### Key files

| File | Role |
|---|---|
| `lyset/register_map.py` | Single source of truth for all Modbus registers. Every read references `REGISTERS: list[Register]`. |
| `lyset/modbus_client.py` | `ModbusWorker(threading.Thread)` — polls the inverter, fires callbacks. Shared by both the GUI and the web server. |
| `lyset/server.py` | FastAPI app: WebSocket push, REST endpoints, worker lifecycle. |
| `lyset/static/index.html` | Single-file SPA (no build step). Chart.js 4.x from CDN. Four tabs: Dashboard, Charts, Control, Log. |

### ModbusWorker design

**Connect-per-poll:** the worker opens a fresh TCP connection at the start of every cycle, reads all registers in batches, then immediately closes it. This is deliberate — the SUN2000 drops idle connections after ~30 s and a half-open socket causes silent hangs. Do not switch to a persistent connection.

`_temp_read()` handles on-demand reads (Control tab prefill) by opening its own short-lived connection when `_client` is None between polls.

Registers are batched by contiguous address range (MAX_GAP=10, MAX_BATCH=100). Batch failures are logged as warnings and skipped — never re-raised — so one bad register doesn't abort the whole poll.

### Hardware constraints

- **Single connection:** the SDongle only allows one Modbus TCP client at a time. Home Assistant's `huawei_solar` integration will block this app.
- **Device-info registers (30000+)** do not respond via the SDongle proxy on this firmware — they are excluded from `_poll()` by the `group != 'Device'` filter.
- **pymodbus 3.x API:** use `device_id=` keyword, not `slave=`.
- **Battery SOC** is at register 37760, far from the other battery registers (37001–37022). The batching logic handles this gap correctly.

### `register_map.py` conventions

`Register` fields: `address`, `count` (number of 16-bit words), `data_type` (`U16`/`I16`/`U32`/`I32`/`STR`), `gain` (divide raw by gain for engineering value), `unit`, `description`, `group`, `key` (snake_case, used as the dict key in data payloads).

Computed keys added by `_poll()` (not in the register map): `pv1_power`, `pv2_power`, `house_load`, `inverter_state_label`, `batt_status_label`.
