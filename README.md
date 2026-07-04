# Lyset

**Lyset** (Danish: *the light*) is a self-hosted dashboard and **battery optimiser** for Huawei **SUN2000** solar inverters with a **LUNA2000** battery. It talks to the inverter directly over Modbus TCP, shows a live web dashboard, pulls electricity prices and a solar forecast, learns your household's consumption, and can **actively control the battery** — charging when power is cheap, discharging when it's expensive, and never exporting at negative prices.

It runs entirely on your own hardware with no cloud account required, as an open alternative to paid optimisation services.

---

## What it does

- **Live dashboard** — PV, grid, battery and house load, streamed over a WebSocket and updated every few seconds. Click any chart to expand it.
- **Electricity prices** — fetches all-in import and export prices (Denmark, via [Strømligning](https://stromligning.dk)).
- **Solar forecast** — pulls a PV forecast from [Solcast](https://solcast.com) and self-calibrates it against your roof's measured output over time.
- **Consumption learning** — builds a weekly household-load profile from your own meter data and keeps adapting it.
- **Plan & savings** — overlays past actuals with a forward plan (solar, load, battery, grid, price) and tracks the money saved versus buying every kWh from the grid.
- **Battery auto-controller** — each cycle it decides whether to grid-charge, hold, arbitrage-discharge, soak up surplus, or just self-consume, and writes the battery registers to make it happen.
- **Home Assistant feed** — one flat-JSON endpoint exposes everything for REST sensors.

---

## Compatibility

Lyset speaks standard **Huawei SUN2000 Modbus TCP** through an **SDongle** (port 502). It was built and verified against one SUN2000 + LUNA2000 setup, but should work with similar systems.

Register availability and battery-control behaviour **vary by firmware**, so expect to verify a few register addresses against your own inverter (see [`register_map.py`](lyset/register_map.py)) and to watch the logs the first time you let it control the battery.

> ⚠️ The SDongle allows **only one Modbus client at a time.** If Home Assistant's `huawei_solar` integration (or anything else) is already connected to the inverter, Lyset can't connect — and vice-versa. Let Lyset own the connection and have Home Assistant read from Lyset instead (see below).

---

## How it works

A background thread polls the inverter over Modbus every few seconds and hands each reading to a FastAPI server. The server stores history in SQLite, runs the price / solar / consumption workers, feeds the battery auto-controller, and pushes live updates to the browser and to Home Assistant.

```
  Inverter + SDongle  ──Modbus TCP──▶  Lyset (FastAPI + SQLite)  ──▶  Browser dashboard
  Prices / Solar / your meter  ──────▶       │  auto-controller     ──▶  Home Assistant
                                             └──writes back to the inverter
```

The Modbus worker opens a fresh connection for each poll and closes it immediately — the SDongle drops idle connections, so this is intentional.

---

## Install

Requires **Python 3.11+** and the inverter reachable on your LAN.

```bash
git clone https://github.com/SigurdvanHauen/Lyset.git
cd Lyset
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Set up

You can configure Lyset **entirely from the browser** — no file editing needed for most things.

1. **Start it:**

   ```bash
   python main.py
   ```

   The server runs on **`0.0.0.0:8000`**. Open **http://localhost:8000** (or `http://<server-ip>:8000` from another device).

2. **Configure it:** click the **gear icon** (top-right) to open **Settings**. Set your inverter's IP address and, optionally, your API keys and tunables. Changes are saved to a local `.env` file and take effect immediately.

Prefer files? Create a `.env` in the project root before first start:

```ini
INVERTER_HOST=192.168.x.x           # your inverter's LAN address

# Optional — unlock prices & forecast. Leave out to run as a pure monitor.
STROMLIGNING_API_KEY=your-key
STROMLIGNING_POSTAL_CODE=your-danish-postal-code   # picks your tariff zone
SOLCAST_API_KEY=your-key
```

Find the inverter's IP in your router's DHCP list or the FusionSolar app.

**API keys are optional.** Without them Lyset still runs as a live monitor — the price, forecast and optimisation features simply stay idle until you add the keys. A free [Solcast](https://solcast.com) hobbyist key and a [Strømligning](https://stromligning.dk) key unlock the rest. (Prices are Denmark-specific; the monitoring and solar features work anywhere.)

---

## The battery auto-controller

When enabled, the controller checks prices, PV, load and state-of-charge each cycle and picks one behaviour:

| Regime | What it does |
|---|---|
| **Force-charge on negative export** | Caps feed-in to 0 W and soaks surplus PV into the battery so nothing is sold at a loss. |
| **Grid charge** | Banks cheap grid energy when a pricier period is coming. |
| **Hold** | Lets cheap grid cover the load now and saves the battery for a coming peak. |
| **Arbitrage discharge** | Sells stored energy to the grid — but only when it's guaranteed to beat re-buying that energy later. |
| **Self-consumption** (default) | Normal behaviour: use solar first, battery second, grid last. |

It's **off-safe**: disable it (from the dashboard, or `AUTO_CONTROLLER_AUTOSTART=0`) and Lyset stops writing to the inverter entirely and becomes a pure monitor. Thresholds live at the top of [`auto_controller.py`](lyset/auto_controller.py).

> Battery control was tuned for one specific firmware. **Watch the logs and confirm the battery behaves before trusting it unattended.**

---

## Home Assistant

Because only one Modbus client can talk to the inverter, point Home Assistant at Lyset instead of the inverter:

```
GET http://<lyset-host>:8000/api/ha/snapshot
```

It returns one flat JSON object (PV, grid, battery, SoC, house load, prices, next-slot forecasts) — ideal for a REST sensor with `json_attributes`. Sign conventions match the inverter: `grid_w > 0` = importing, `batt_w > 0` = charging.

---

## Running 24/7

Lyset is happy running headless on any small always-on machine. Example `systemd` unit:

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

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now lyset
journalctl -u lyset -f        # follow logs
```

History (`lyset_history.db`) and the learned model (`lyset_model.json`) live in the working directory and persist across restarts — back them up if you care about the data. You can also download a copy any time from the Log tab or `GET /api/download/db`.

---

## Under the hood

- **No build step** — the dashboard is a single self-contained `static/index.html` using Chart.js from a CDN.
- **REST + WebSocket API** — everything the dashboard uses is available programmatically; browse the routes in [`server.py`](lyset/server.py).
- **Data** — one SQLite file plus one JSON model file, both in the working directory.
- There's also a legacy PySide6 desktop GUI (`lyset/gui/`) from the project's first phase; the web server is now the primary interface.

---

## Licence

MIT
