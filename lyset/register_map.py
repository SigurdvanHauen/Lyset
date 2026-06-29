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
    # 47075: global max charge power cap (W) — overrides all other charge limits
    # 47076: global max discharge power cap (W)
    # 47079: grid-to-battery charge power limit (W) — only applies when 47087=1
    # 47086: 1=forced charge/discharge, 4=max self-consumption
    # 47087: 0=grid charge disabled, 1=grid charge enabled
    # 47100: 0=none, 1=force-charge, 2=force-discharge
    Register(47075,  1, 'U16',  1,    'W',   'Max charge power',         'Control', 'batt_max_charge_w'),
    Register(47076,  1, 'U16',  1,    'W',   'Max discharge power',      'Control', 'batt_max_discharge_w'),
    Register(47079,  1, 'U16',  1,    'W',   'Grid charge power',        'Control', 'batt_grid_charge_w'),
    Register(47086,  1, 'U16',  1,    '',    'Battery working mode',     'Control', 'batt_working_mode'),
    Register(47087,  1, 'U16',  1,    '',    'Grid charge enable',       'Control', 'grid_charge_enable'),
    Register(47100,  1, 'U16',  1,    '',    'Forced charge/discharge',  'Control', 'batt_forced_mode'),
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

BATTERY_WORKING_MODES: dict[int, str] = {
    0: 'None',
    1: 'Forced charge/discharge',
    2: 'Time-of-use (LG)',
    3: 'Fixed charge/discharge',
    4: 'Maximise self-consumption',
    5: 'Fully fed to grid',
    6: 'Time-of-use (LG, Pro)',
}

BATT_FORCED_MODES: dict[int, str] = {
    0: 'None',
    1: 'Charging',
    2: 'Discharging',
}
