# Lyset

**Lyset** (Danish: *the light*) is a self-hosted monitoring and **battery-optimisation** server for a Huawei SUN2000 solar inverter with a LUNA2000 battery. It polls the inverter over Modbus TCP, serves a live web dashboard, pulls Danish electricity prices and a solar forecast, learns your household consumption, and can **actively control the battery** to charge cheap / discharge expensive / avoid exporting at negative prices — running entirely on your own hardware, with no cloud dependency.

It is an open replacement for paid optimisation services (e.g. IntelliCharge), with full control over the data and the decision logic.

---

## Contents

- [What it does](#what-it-does)
- [Hardware](#hardware)
- [How it works](#how-it-works)
- [Setup](#setup)
- [Configuration (`.env`)](#configuration-env)
- [Connecting to your inverter](#connecting-to-your-inverter)
- [The battery auto-controller](#the-battery-auto-controller)
- [HTTP API](#http-api)
- [WebSocket API](#websocket-api)
- [Home Assistant integration](#home-assistant-integration)
- [Running 24/7 (Proxmox LXC / systemd)](#running-247-proxmox-lxc--systemd)
- [Data storage](#data-storage)
- [Hardware constraints & gotchas](#hardware-constraints--gotchas)
- [Project layout](#project-layout)
- [Licence](#licence)

---

## What it does

- **Live dashboard** — power flow, PV strings, grid, battery SoC, energy totals, updated every 10 s over a WebSocket. No page reloads.
- **History & charts** — every metric is stored in SQLite and plotted; click any card to open a scrollable chart of its full history.
- **Electricity prices** — fetches all-in import and export (sell-back) prices for your DSO zone from [Strømligning](https://stromligning.dk).
- **Solar forecast** — pulls a PV production forecast (mean + P10/P90 confidence band) from [Solcast](https://solcast.com).
- **Consumption forecast** — learns a weekly household-load profile online from your own meter data.
- **Plan view** — overlays past actuals with a forward plan (solar, load, battery charge/discharge, grid, SoC) plus the price curve, IntelliCharge-style.
- **Auto-controller** — decides each cycle whether to grid-charge, hold, arbitrage-discharge, force-charge from surplus, or run plain self-consumption, and writes the battery-control registers to make it happen.
- **Home Assistant feed** — a single flat-JSON endpoint exposes everything for HA REST sensors (see below).

> There is also a legacy **PySide6 desktop GUI** (`lyset/gui/`) from the project's first phase. It is no longer the primary interface; the web server is. The two share the same `register_map.py` and `modbus_client.py`.

---

## Hardware

Built and verified against:

| Device | Model |
|---|---|
| Inverter | Huawei SUN2000-6KTL-M1 |
| Battery | Huawei LUNA2000-5-H1 (5 kWh) |
| Solar array | ~6.9 kWp |
| Dongle | Huawei SDongle-A05 (exposes Modbus TCP on port **502**) |

It should work with other SUN2000 + LUNA2000 setups that speak Modbus TCP through an SDongle, but register availability and battery-control behaviour vary by firmware — expect to verify a few addresses against your own unit (see [`register_map.py`](lyset/register_map.py)).

---

## How it works

```
┌─────────────┐   Modbus TCP    ┌───────────────────────────────────────────┐
│  SUN2000    │◀───port 502────▶│  ModbusWorker (background thread)          │
│  + SDongle  │  connect-per-poll│  reads all registers every 10 s           │
└─────────────┘                 └──────────────┬────────────────────────────┘
                                               │ callback
   Strømligning ──prices──┐                    ▼
   Solcast ──solar fc──┐  │      ┌───────────────────────────────────────────┐
   (your meter) ─load──┘  └─────▶│  FastAPI server (lyset/server.py)         │
                                 │  • SQLite history store                   │
                                 │  • AutoController (battery decisions)     │
                                 │  • REST + WebSocket + HA snapshot         │
                                 └──────────────┬────────────────────────────┘
                                                │
                         ┌──────────────────────┼─────────────────────┐
                         ▼                      ▼                     ▼
                    Browser SPA           Home Assistant         (writes back to
                  (live dashboard)        (REST sensors)          the inverter)
```

The Modbus worker **opens a fresh TCP connection at the start of every poll and closes it immediately** — the SDongle drops idle connections after ~30 s and a half-open socket hangs silently. This is deliberate; do not switch it to a persistent connection.

---

## Setup

### Prerequisites

- **Python 3.11+**
- The inverter reachable on your LAN (you'll need its IP)
- Optional but recommended: a [Strømligning API key](https://stromligning.dk) (prices) and a [Solcast hobbyist API key](https://solcast.com) (solar forecast). Without them the server still runs as a monitor; the optimisation features just stay idle.

### Install

```bash
git clone https://github.com/SigurdvanHauen/Lyset.git
cd Lyset
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Configure

Create a `.env` file in the project root (see [Configuration](#configuration-env) for all keys):

```ini
# Minimal — prices + solar forecast. Leave out to run as a pure monitor.
STROMLIGNING_API_KEY=your-key-here
STROMLIGNING_POSTAL_CODE=5500
SOLCAST_API_KEY=your-key-here
```

### Run

```bash
python main.py
```

This starts the FastAPI server (via uvicorn) on **`0.0.0.0:8000`**. Open **http://localhost:8000** (or `http://<server-ip>:8000` from another device) for the dashboard.

On startup the server auto-connects to the inverter, starts the price/solar/consumption workers (if configured), and **enables the auto-controller** (unless you disable it — see below).

---

## Configuration (`.env`)

All configuration is via environment variables, loaded from `.env` at startup (`python-dotenv`). Everything has a default; only the API keys are really required to unlock the paid features.

### Electricity prices (Strømligning)

| Variable | Default | Meaning |
|---|---|---|
| `STROMLIGNING_API_KEY` | — | API key (Bearer). **Unset → price polling disabled.** |
| `STROMLIGNING_POSTAL_CODE` | `5500` | Danish postal code, used to auto-pick your DSO tariff zone. |
| `STROMLIGNING_SUPPLIER_ID` | auto | Override the DSO lookup with a known supplier ID. |
| `PRICE_EXPORT_FEE` | `0.033375` | DKK/kWh of feed-in/balance tariffs subtracted from the spot price to get the export (sell-back) payout. |
| `PRICE_POLL_INTERVAL` | `1800` | Seconds between price refreshes (30 min). |

> **Export price model:** the export payout is the bare Nord Pool spot (`details.electricity.value` from the API — already excl. VAT/elafgift) minus `PRICE_EXPORT_FEE`. Elafgift and VAT do **not** apply to surplus sales, so export goes negative whenever the spot itself is negative.

### Solar forecast (Solcast)

| Variable | Default | Meaning |
|---|---|---|
| `SOLCAST_API_KEY` | — | API key. **Unset → solar forecast disabled.** |
| `SOLCAST_RESOURCE_ID` | auto-discover | Rooftop site UUID(s), comma-separated. If unset it's discovered once via `/rooftop_sites` (costs 1 API call). Note the UUID from the logs and set it to avoid the extra call. |
| `SOLCAST_FETCH_HOURS` | `6,12,18` | Local hours at which to fetch (the free tier is rate-limited — see below). |
| `SOLCAST_POLL_INTERVAL` | `21600` | Seconds between refreshes (6 h). |

> Solcast hobbyist accounts allow **10 calls/day per site**. With 1 site keep `SOLCAST_POLL_INTERVAL ≥ 8640`; with 2 sites `≥ 17280`. Fetching at fixed hours (default) is the simplest way to stay under quota.

### Consumption model

| Variable | Default | Meaning |
|---|---|---|
| `CONSUMPTION_HISTORY_PATH` | — | Path to a meter-data Excel file (Eloverblik `MeterData.xlsx`) used to seed the weekly load profile on first run. Without it the model learns from scratch from live polls. |
| `CONSUMPTION_MIN_STANDBY_W` | `300` | Floor (W) applied to load predictions so empty/overnight slots never read as 0 W. |

### Auto-controller

| Variable | Default | Meaning |
|---|---|---|
| `AUTO_CONTROLLER_AUTOSTART` | `1` | `0`/`false`/`no`/`off` to start with the controller **disabled** (monitor only — never writes to the battery). You can also toggle it live in the Control tab. |

> **Inverter connection** is not an env var. The startup default is `192.168.1.185:502`, slave `1`, 10 s poll, defined in `ConnectRequest` ([server.py](lyset/server.py)). Either edit that default for your inverter or set it at runtime from the Control tab (see next section).

---

## Connecting to your inverter

The server auto-connects on startup using the `ConnectRequest` defaults. If your inverter lives at a different IP:

- **At runtime:** open the **Control** tab in the web UI and enter your inverter's host / port (`502`) / slave ID (`1`), then connect. This calls `POST /api/connect`.
- **Persistently:** edit the defaults in `ConnectRequest` in [`lyset/server.py`](lyset/server.py) so they survive restarts.

Find the inverter's IP from your router's DHCP table, or in the FusionSolar app under the SDongle's network settings.

> ⚠️ **The SDongle allows only one Modbus TCP client at a time.** If Home Assistant's `huawei_solar` integration (or any other Modbus client) is already connected, Lyset can't connect, and vice-versa. **Do not point both directly at the inverter.** The intended pattern is: **Lyset owns the single Modbus connection, and Home Assistant reads from Lyset's HA snapshot endpoint** (below) — no conflict.

---

## The battery auto-controller

Every 15 s the controller looks at the current/next price slots, PV, load, and SoC, and picks one regime:

| Regime | When | Action |
|---|---|---|
| **Negative-export force-charge** | export price < threshold | Cap feed-in to 0 W (APC zero-export) and soak surplus PV into the battery so nothing is dumped to grid at a loss. |
| **Grid charge** | now is near the cheapest of the next *N* h **and** a materially pricier slot is coming **and** there's room | Force-charge from grid to bank cheap energy. |
| **Hold** | drawing from battery, with a much pricier slot ahead and no solar to refill it | Force-idle; let cheap grid cover the load now and save the battery for the peak. |
| **Arbitrage discharge** | export price beats the cheapest upcoming import by a margin and SoC is high | Force-discharge to grid. |
| **Self-consumption** (default) | none of the above | Surplus → charge from surplus; deficit → max self-consumption with zero export. |

Design notes:
- Writes are **read-back gated** where the register can be polled (re-issued only until the inverter confirms, then stopped) and **deduped** otherwise — so the single-client SDongle is never flooded with redundant writes.
- Disabling the controller (Control tab, or `AUTO_CONTROLLER_AUTOSTART=0`) restores safe defaults and **stops all writes** — Lyset becomes a pure monitor.
- The thresholds and horizons live as constants at the top of [`lyset/auto_controller.py`](lyset/auto_controller.py).

> The control logic was tuned for one specific SUN2000/LUNA2000 firmware. **If you reuse it, watch the logs and verify the battery behaves before trusting it unattended** — some registers don't read back on every firmware, and battery-mode semantics differ.

---

## HTTP API

Base URL `http://<server>:8000`. JSON in/out.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The single-page web dashboard. |
| `POST` | `/api/connect` | Start the Modbus worker. Body: `{host, port, slave_id, poll_interval}`. |
| `POST` | `/api/disconnect` | Stop the worker. |
| `GET` | `/api/state` | Connection state + last snapshot + recent log lines. |
| `POST` | `/api/write` | Queue a register write. Body: `{type: "u16"\|"u32"\|"i32", address, value, description}`. |
| `GET` | `/api/read?address=&type=` | Synchronous single-register read (Control-tab prefill). |
| `GET` | `/api/prices` | Stored price series (past 24 h → +7 days). |
| `GET` | `/api/solar-forecast?full=` | Solcast forecast series. |
| `POST` | `/api/solar-forecast/refresh` | Force an immediate Solcast fetch. |
| `GET` | `/api/consumption-forecast?full=` | Consumption forecast + model coverage. |
| `POST` | `/api/consumption/import-excel` | Seed the consumption model from an Excel file. Body: `{path}`. |
| `GET` | `/api/power-forecast?full=` | Simulated forward SoC / battery / grid plan. |
| `GET` | `/api/daily-solar` | Per-day produced vs. forecast kWh (last 30 days). |
| `GET` | `/api/history/series?key=&from_ms=` | Full downsampled history for one metric. |
| `GET` | `/api/automode` | Auto-controller state + recent command log. |
| `POST` | `/api/automode` | Enable/disable the controller. Body: `{enabled}`. |
| `GET` | `/api/ha/snapshot` | **Flat snapshot for Home Assistant** (see below). |
| `GET` | `/api/download/db` | Download a consistent SQLite backup of the whole history DB. |
| `DELETE` | `/api/history` | Clear stored history. |
| `POST` | `/api/pymodbus-logs` | Toggle verbose pymodbus logging. Body: `{enabled}`. |
| `WS` | `/ws` | Real-time push (below). |

---

## WebSocket API

Connect to `ws://<server>:8000/ws`. On connect the server sends an initial burst (history, current connection state, last data, prices, forecasts, auto-command log), then streams updates. Each message is JSON with a `type`:

| `type` | Payload | When |
|---|---|---|
| `data` | live inverter snapshot (`payload`) | every poll (~10 s) |
| `history` | array of past points | once on connect / after clear |
| `connection` | `{ok, msg}` | connect/disconnect events |
| `prices` | price series (`payload`) | on price refresh |
| `solar_forecast` | Solcast series (`payload`) | on solar refresh |
| `consumption_forecast` | load forecast (`payload`) | on model update |
| `power_forecast` | forward SoC/battery/grid plan (`payload`) | on re-simulation |
| `auto_mode` | `{enabled, last_action, last_action_ts}` | controller toggled |
| `auto_history` | recent command log | once on connect |
| `write_result` | `{ok, msg}` | after a register write |
| `error` | `{msg}` | Modbus/worker errors |
| `log` | `{entry}` | every server log line (powers the Log tab) |

---

## Home Assistant integration

The single-Modbus-client limit means HA should **not** talk to the inverter directly while Lyset is running. Instead, point HA at Lyset's snapshot endpoint:

```
GET http://<lyset-host>:8000/api/ha/snapshot
```

It returns one flat JSON object with the current state plus next-slot forecasts — designed for the HA REST sensor with `json_attributes`.

### What's exposed

| Field | Unit | Meaning |
|---|---|---|
| `ts_ms`, `connected` | — | snapshot time; inverter link state |
| `pv_w` | W | total PV power (string 1 + 2) |
| `grid_w` | W | grid meter power (**+ import, − export**) |
| `batt_w` | W | battery power (**+ charging, − discharging**) |
| `batt_soc` | % | battery state of charge |
| `house_load_w` | W | computed household consumption |
| `inverter_state` | code | inverter state register |
| `daily_yield_kwh` | kWh | produced today (inverter's own counter) |
| `solar_fc_kwh_today` | kWh | Solcast estimate of full-day yield |
| `import_price_dkk` / `export_price_dkk` | DKK/kWh | active price slot |
| `solar_fc_w`, `solar_fc_p10_w`, `solar_fc_p90_w`, `solar_fc_ts_ms` | W / ms | next Solcast period (mean + P10/P90 band) |
| `forecast_soc`, `forecast_batt_w`, `forecast_grid_w`, `forecast_ts_ms` | % / W / ms | next simulated plan slot |
| `consumption_fc_w` | W | active consumption-forecast slot |
| `solar_forecast_today` | array | today's 30-min forecast periods (for charts) |
| `prices_today` | array | today's price slots (for charts) |

### Example HA configuration

```yaml
# configuration.yaml
rest:
  - resource: http://192.168.1.50:8000/api/ha/snapshot
    scan_interval: 30          # Modbus refreshes every 10 s, prices every 30 min
    sensor:
      - name: "Solar Power"
        value_template: "{{ value_json.pv_w }}"
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement

      - name: "Battery SoC"
        value_template: "{{ value_json.batt_soc }}"
        unit_of_measurement: "%"
        device_class: battery
        state_class: measurement

      - name: "Grid Power"
        value_template: "{{ value_json.grid_w }}"
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement

      - name: "Import Price"
        value_template: "{{ value_json.import_price_dkk }}"
        unit_of_measurement: "DKK/kWh"
        state_class: measurement

      - name: "Export Price"
        value_template: "{{ value_json.export_price_dkk }}"
        unit_of_measurement: "DKK/kWh"
        state_class: measurement

      - name: "Solar Produced Today"
        value_template: "{{ value_json.daily_yield_kwh }}"
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
        # full forecast arrays are available as attributes:
        json_attributes:
          - solar_forecast_today
          - prices_today
          - solar_fc_kwh_today
```

Sign conventions match the inverter: **`grid_w` > 0 = importing, `batt_w` > 0 = charging.** Flip the sign in a template if you want "export"/"discharge" as positive.

> If you'd rather HA own the Modbus link, you can — but then **stop Lyset's worker first** (`POST /api/disconnect`, or run with the controller off), because both cannot hold the single SDongle socket at once.

---

## Running 24/7 (Proxmox LXC / systemd)

Lyset is designed to run headless on a small always-on host (the reference deployment is a Proxmox LXC). A minimal systemd unit:

```ini
# /etc/systemd/system/lyset.service
[Unit]
Description=Lyset solar/battery server
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/Lyset
ExecStart=/opt/Lyset/.venv/bin/python main.py
EnvironmentFile=/opt/Lyset/.env
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now lyset
journalctl -u lyset -f        # follow logs
```

Update by pulling the repo and restarting the service. The SQLite history DB (`lyset_history.db`) and learned model (`lyset_model.json`) live in the working directory and persist across restarts — back them up if you care about the history.

---

## Data storage

Everything is a single SQLite file, **`lyset_history.db`**, created on first run in the working directory ([`lyset/store.py`](lyset/store.py)). Tables: inverter polls, prices, solar/consumption/power forecasts, daily solar totals, and the auto-controller command log. The learned consumption profile is a separate JSON file, **`lyset_model.json`**.

Grab a consistent copy any time (safe while polling) from `GET /api/download/db` or the Log tab's download button — it's a normal SQLite database you can open with any tool.

---

## Hardware constraints & gotchas

- **Single Modbus client** — the SDongle allows exactly one TCP client. Don't run HA's `huawei_solar` against the inverter at the same time as Lyset.
- **Connect-per-poll** — a fresh connection is opened and closed each cycle on purpose (the SDongle drops idle sockets after ~30 s). Don't "optimise" it into a persistent connection.
- **Device-info registers (30000+)** don't respond through the SDongle proxy on this firmware and are excluded from polling.
- **Battery SoC lives at register 37760**, far from the other battery registers (37001–37022); the batch reader handles the gap.
- **pymodbus 3.x** — use the `device_id=` keyword (not `slave=`).
- **`run_coroutine_threadsafe` is avoided** for the worker→browser push path (it silently drops futures on Windows); the code uses `call_soon_threadsafe` + an `asyncio.Queue` instead.

---

## Project layout

```
Lyset/
├── main.py                     # entry point — starts uvicorn
├── requirements.txt
├── .env                        # your config (not committed)
└── lyset/
    ├── server.py               # FastAPI app: REST, WebSocket, worker lifecycle, HA snapshot
    ├── register_map.py         # single source of truth for all Modbus registers
    ├── modbus_client.py        # ModbusWorker thread — batched reads, connect-per-poll
    ├── auto_controller.py      # battery decision/control logic
    ├── prices.py               # Strømligning price worker
    ├── solcast.py              # Solcast solar-forecast worker
    ├── consumption_model.py    # weekly load profile (seed + online EMA learning)
    ├── store.py                # SQLite history store
    ├── static/index.html       # single-file web dashboard (Chart.js, no build step)
    └── gui/                    # legacy PySide6 desktop app (phase 1)
```

No build step for the frontend — `static/index.html` is a single self-contained SPA using Chart.js from a CDN. No test suite or linter is configured; `pip install -r requirements.txt` is the only dependency check.

---

## Licence

MIT
