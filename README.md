# Lyset

**Lyset** (Danish: *The Light*) is a local solar energy monitoring and optimisation platform for a Huawei SUN2000-6KTL-M1 inverter paired with a LUNA2000 battery and an EV charger.

The long-term goal is to replicate — and improve on — third-party optimisation services like intelligcharge.ai by running the logic locally, with full control over the data and decision making.

---

## Current state — Phase 1: GUI monitor

A standalone Python desktop application that connects to the inverter over **Modbus TCP**, reads all available registers in real time, and displays them in a live dashboard with scrolling plots.

![Dashboard screenshot](docs/dashboard.png)

### Features

| Feature | Detail |
|---|---|
| Live dashboard | Colour-coded value cards for power flow, PV strings, grid, battery, energy totals |
| Real-time plots | Scrolling strip charts (5 min → today) for power flow, PV strings, battery SOC, grid voltage |
| Verified register map | All addresses confirmed against live hardware (SUN2000-6KTL-M1 + LUNA2000-5-H1) |
| Auto-reconnect | Detects lost connection and retries with backoff |
| CSV export | One-click export of every recorded snapshot |
| Log console | Built-in log tab showing Modbus traffic and errors in real time |
| Dark UI | Clean dark theme throughout, including pyqtgraph plots |

### Tech stack

| Layer | Library |
|---|---|
| GUI | PySide6 (Qt 6) |
| Plots | pyqtgraph |
| Modbus TCP | pymodbus 3.13 |
| Data | numpy, pandas |

---

## Hardware

| Device | Model |
|---|---|
| Inverter | Huawei SUN2000-6KTL-M1 |
| Battery | Huawei LUNA2000-5-H1 (5 kWh) |
| Solar array | 6.9 kWp |
| Dongle | Huawei SDongle (exposes Modbus TCP on port 502) |

---

## Getting started

### Prerequisites

- Python 3.11+
- The inverter reachable on your local network

### Install

```bash
git clone https://github.com/SigurdvanHauen/Lyset.git
cd Lyset
pip install -r requirements.txt
```

### Run

```bash
python main.py
```

Enter the inverter IP, port (`502`), and slave ID (`1`) in the connection bar, then click **Connect**.

> **Note — single connection limit:** The SUN2000 SDongle only allows one Modbus TCP client at a time. If Home Assistant (or any other Modbus client) is connected, this app will fail to connect. Temporarily disable the competing integration while using this app, or see the roadmap below.

---

## Project structure

```
Lyset/
├── main.py                   # Entry point
├── requirements.txt
└── lyset/
    ├── register_map.py       # Full SUN2000 + LUNA2000 Modbus register table
    ├── modbus_client.py      # Background QThread — batched FC3 reads, auto-reconnect
    └── gui/
        ├── dashboard.py      # Live value cards
        ├── plots.py          # Real-time pyqtgraph strip charts
        └── main_window.py    # Main window, connection bar, log console, CSV export
```

---

## Modbus register map highlights

All addresses verified by live scan against the physical hardware.

| Group | Key registers |
|---|---|
| PV strings | 32016–32019 — string voltage & current (gain /10, /100) |
| Inverter output | 32064 — active power (I32, W) |
| Grid | 32069–32078 — phase voltages, frequency |
| Battery SOC | **37760** — U16, gain /10, % |
| Battery power | **37001** — I32, W (positive = charging) |
| Battery temperature | **37022** — I16, gain /10, °C |
| Battery SOH | **37004** — U16, gain /10, % |
| Grid meter power | 37113 — I32, W (positive = export) |
| Grid meter energy | 37119 / 37121 — exported / imported kWh |

---

## Roadmap

### Phase 2 — Optimisation engine

- Danish spot price feed (Energi Data Service API)
- Tariff model (grid tariffs, DSO fees, VAT)
- Solar production forecast (Open-Meteo)
- Consumption prediction (time-of-day model)
- Battery charge/discharge scheduler: maximise self-consumption, avoid negative export prices
- Production curtailment when export price is negative

### Phase 3 — Deployment

- Proxmox LXC service (headless, runs 24/7)
- Home Assistant add-on that owns the Modbus connection and publishes all sensor data via MQTT
- HA integration reads from MQTT — eliminates the single-connection conflict

---

## Licence

MIT
