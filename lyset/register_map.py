"""
Huawei SUN2000-6KTL-M1 + LUNA2000 battery Modbus register map.

All addresses are Modbus holding-register (FC3) addresses as published in the
Huawei SUN2000 Modbus Interface Definitions document (SDT-MAN-002).

Register tuple format:
    (address, count, data_type, gain, unit, description)

data_type: 'U16', 'I16', 'U32', 'I32', 'STR'
gain:      divide raw value by this to get engineering value (1 = no scaling)
"""

from dataclasses import dataclass
from typing import Optional

# ── data types ───────────────────────────────────────────────────────────────

@dataclass
class Register:
    address: int
    count: int          # number of 16-bit registers to read
    data_type: str      # U16 | I16 | U32 | I32 | STR
    gain: float         # divide raw by gain → engineering value
    unit: str
    description: str
    group: str          # display group
    key: str            # unique snake_case key used in the data dict


# ── register definitions ─────────────────────────────────────────────────────

REGISTERS: list[Register] = [
    # ── Device info ──────────────────────────────────────────────────────────
    Register(30000, 15, 'STR',  1,    '',    'Model',                    'Device',  'model'),
    Register(30015, 10, 'STR',  1,    '',    'Serial number',            'Device',  'serial_number'),
    Register(30025,  1, 'U16',  1,    '',    'Product model',            'Device',  'product_model'),
    Register(30070,  1, 'U16',  1,    '',    'Number of PV strings',     'Device',  'pv_string_count'),
    Register(30071,  1, 'U16',  1,    '',    'Number of MPP trackers',   'Device',  'mppt_count'),
    Register(30072,  1, 'U16',  1,    'kW',  'Rated power',              'Device',  'rated_power'),

    # ── PV strings (up to 8 strings; SUN2000-6KTL-M1 typically has 2) ───────
    Register(32016,  1, 'U16',  10,   'V',   'PV1 voltage',              'PV',      'pv1_voltage'),
    Register(32017,  1, 'I16',  100,  'A',   'PV1 current',              'PV',      'pv1_current'),
    Register(32018,  1, 'U16',  10,   'V',   'PV2 voltage',              'PV',      'pv2_voltage'),
    Register(32019,  1, 'I16',  100,  'A',   'PV2 current',              'PV',      'pv2_current'),

    # ── Grid / output ────────────────────────────────────────────────────────
    Register(32064,  2, 'I32',  1,    'W',   'Active power',             'Output',  'active_power'),
    Register(32066,  2, 'I32',  1,    'var', 'Reactive power',           'Output',  'reactive_power'),
    Register(32068,  1, 'I16',  1000, 'PF',  'Power factor',             'Output',  'power_factor'),
    Register(32069,  1, 'U16',  10,   'V',   'Grid A-phase voltage',     'Grid',    'grid_voltage_a'),
    Register(32070,  1, 'U16',  10,   'V',   'Grid B-phase voltage',     'Grid',    'grid_voltage_b'),
    Register(32071,  1, 'U16',  10,   'V',   'Grid C-phase voltage',     'Grid',    'grid_voltage_c'),
    Register(32072,  2, 'I32',  1000, 'A',   'Grid A-phase current',     'Grid',    'grid_current_a'),
    Register(32074,  2, 'I32',  1000, 'A',   'Grid B-phase current',     'Grid',    'grid_current_b'),
    Register(32076,  2, 'I32',  1000, 'A',   'Grid C-phase current',     'Grid',    'grid_current_c'),
    Register(32078,  1, 'U16',  100,  'Hz',  'Grid frequency',           'Grid',    'grid_frequency'),
    Register(32080,  1, 'U16',  1,    '',    'Inverter state',           'Output',  'inverter_state'),
    Register(32087,  1, 'I16',  10,   '°C',  'Internal temperature',     'Output',  'internal_temp'),

    # ── Daily / total energy ─────────────────────────────────────────────────
    # 32114: daily yield (SUN2000-M1 firmware); 32106 mirrors a cumulative counter, not today
    # 32108: accumulated lifetime yield; gain=100 (not 1000) confirmed by live scan
    Register(32114,  2, 'U32',  100,  'kWh', 'Daily energy yield',       'Energy',  'daily_yield'),
    Register(32108,  2, 'U32',  100,  'kWh', 'Total energy yield',       'Energy',  'total_yield'),

    # ── Battery (LUNA2000) — addresses verified by live register scan ────────────
    Register(37000,  1, 'U16',  1,    '',    'Battery running status',    'Battery', 'batt_status'),
    Register(37001,  2, 'I32',  1,    'W',   'Battery charge/discharge',  'Battery', 'batt_power'),
    Register(37003,  1, 'U16',  10,   'V',   'Battery bus voltage',       'Battery', 'batt_bus_voltage'),
    Register(37004,  1, 'U16',  10,   '%',   'Battery SOH',               'Battery', 'batt_soh'),
    Register(37022,  1, 'I16',  10,   '°C',  'Battery temperature',       'Battery', 'batt_temperature'),
    Register(37067,  1, 'U16',  100,  'kWh', 'Total charge energy',       'Battery', 'batt_charge_total'),
    Register(37069,  1, 'U16',  100,  'kWh', 'Total discharge energy',    'Battery', 'batt_discharge_total'),
    Register(37071,  1, 'U16',  1000, 'kWh', 'Rated capacity',            'Battery', 'batt_rated_capacity'),
    Register(37760,  1, 'U16',  10,   '%',   'Battery SOC',               'Battery', 'batt_soc'),

    # ── Power meter (DTSU666 / SDongle) ──────────────────────────────────────
    Register(37113,  2, 'I32',  1,    'W',   'Grid power (+ import)',    'Meter',   'meter_active_power'),
    Register(37115,  2, 'I32',  1,    'var', 'Grid reactive power',      'Meter',   'meter_reactive_power'),
    Register(37119,  2, 'U32',  100,  'kWh', 'Grid exported energy',     'Meter',   'meter_export_energy'),
    Register(37121,  2, 'U32',  100,  'kWh', 'Grid imported energy',     'Meter',   'meter_import_energy'),
    Register(37118,  1, 'U16',  100,  'Hz',  'Meter frequency',          'Meter',   'meter_frequency'),

    # ── Battery control state (written by auto-controller; polled to reflect live state) ──
    # 47086: StorageWorkingModesC — 1=fixed/forced charge-discharge, 2=max
    # self-consumption, 4=FULLY FED TO GRID (never write 4; see BATTERY_WORKING_MODES)
    # 47087: 0=grid charge disabled, 1=grid charge enabled
    # 47100: 0=none, 1=force-charge, 2=force-discharge
    # 47075/47077 (U32, W): max charge / max discharge power. These are GLOBAL limits
    # that cap the battery in EVERY mode incl. self-consumption — a low max-discharge-power pins
    # self-consumption discharge (e.g. -400 W while the deficit is 600 W). Polled so the
    # controller can read them back and re-assert the full rate. (47098 = forced power is
    # NOT accessible — Illegal Data Address.) Forcible setpoints are split by direction:
    # 47247 = forcible CHARGE power, 47249 = forcible DISCHARGE power (U32 each).
    Register(47086,  1, 'U16',  1,    '',    'Battery working mode',     'Control', 'batt_working_mode'),
    Register(47087,  1, 'U16',  1,    '',    'Grid charge enable',       'Control', 'grid_charge_enable'),
    Register(47100,  1, 'U16',  1,    '',    'Forced charge/discharge',  'Control', 'batt_forced_mode'),
    Register(47075,  2, 'U32',  1,    'W',   'Max charge power',         'Control', 'max_charge_power'),
    Register(47077,  2, 'U32',  1,    'W',   'Max discharge power',      'Control', 'max_discharge_power'),

    # ── Active Power Control / export limitation (zero-export to grid) ────────
    # 47415: active power control mode — 0=unlimited, 1=DI scheduling,
    #        5=zero-power grid connection (no export), 6=power-limited (kW via
    #        47416), 7=power-limited (% via 47418). Mode 5 is how FusionSolar /
    #        IntelliCharge implement "do not export": the inverter curtails PV
    #        beyond what battery+load absorb and never feeds battery to grid.
    # 47416: max feed-in to grid in W (I32), used when mode=6.
    # 47418: max feed-in to grid in % of rated (I16, gain 10), used when mode=7.
    # Polled read-only here so the dashboard/log can confirm the write took effect
    # on this SDongle firmware (read-back verification).
    Register(47415,  1, 'U16',  1,    '',    'Active power ctrl mode',   'Control', 'active_power_mode'),
    Register(47416,  2, 'I32',  1,    'W',   'Max feed-in to grid',      'Control', 'max_feed_grid_w'),
    Register(47418,  1, 'I16',  10,   '%',   'Max feed-in to grid %',    'Control', 'max_feed_grid_pct'),
]

# Quick lookup by key
REGISTER_BY_KEY: dict[str, Register] = {r.key: r for r in REGISTERS}

# Groups used for organizing the UI
GROUPS: list[str] = ['PV', 'Output', 'Grid', 'Battery', 'Meter', 'Energy', 'Control', 'Device']


# ── Inverter state codes ──────────────────────────────────────────────────────

INVERTER_STATES: dict[int, str] = {
    0x0000: 'Standby: initialising',
    0x0001: 'Standby: detecting insulation',
    0x0002: 'Standby: detecting irradiation',
    0x0003: 'Standby: grid detecting',
    0x0100: 'Starting',
    0x0200: 'On-grid (generating)',
    0x0201: 'On-grid: power-limited',
    0x0202: 'On-grid: self-derating',
    0x0300: 'Shutdown: fault',
    0x0301: 'Shutdown: command',
    0x0302: 'Shutdown: OVGR',
    0x0303: 'Shutdown: communication disconnected',
    0x0304: 'Shutdown: power limited',
    0x0305: 'Shutdown: manual start required',
    0x0306: 'Shutdown: DC switch off',
    0x0401: 'Grid scheduling: cos φ-P curve',
    0x0402: 'Grid scheduling: Q-U curve',
    0x0403: 'Grid scheduling: PF-U curve',
    0x0404: 'Grid scheduling: dry-contact',
    0x0405: 'Grid scheduling: Q-P curve',
    0x0500: 'Spot-check ready',
    0x0501: 'Spot-checking',
    0x0600: 'Inspecting',
    0x0700: 'AFCI self check',
    0x0800: 'I-V scanning',
    0x0900: 'DC input detection',
    0x0A00: 'Running: off-grid charging',
    0x1000: 'Standby: no irradiation',
}

BATTERY_STATUSES: dict[int, str] = {
    0: 'Offline',
    1: 'Standby',
    2: 'Running',
    3: 'Fault',
    4: 'Sleep mode',
}

# Values of register 47086 (storage working mode SETTINGS) — StorageWorkingModesC
# in wlcrs/huawei-solar-lib. NOT the enum of the read-only status register 37006
# (StorageWorkingModesB), where 4 = self-consumption. Using the B labels here hid
# a serious bug: writing 4 to 47086 put the inverter in FULLY FED TO GRID, which
# dumps the battery to grid at max rate (any SoC) whenever the export cap is lifted.
BATTERY_WORKING_MODES: dict[int, str] = {
    0: 'Adaptive',
    1: 'Fixed charge/discharge',
    2: 'Maximise self-consumption',
    3: 'Time-of-use (LG)',
    4: 'Fully fed to grid',
    5: 'Time-of-use (LUNA2000)',
}

BATT_FORCED_MODES: dict[int, str] = {
    0: 'None',
    1: 'Charging',
    2: 'Discharging',
}
