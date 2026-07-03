"""
Read-side sanitation of polled inverter data.

Every consumer of a poll — the auto-controller's decisions, the WebSocket
dashboard, the history DB, the consumption model — reads the same dict built in
ModbusWorker._poll(). This module cleans that dict ONCE, at the source, so a bad
read can neither steer a control decision nor be presented/stored as truth.

Observed failure modes this guards against (all from the live rig's history DB):

* IMPOSSIBLE VALUES from registers this SDongle/firmware doesn't really support
  or occasionally corrupts: power_factor 4.096 (|PF| ≤ 1), reactive_power
  268,898,326 var, grid_frequency 0.0 Hz while the meter reads 49.97 Hz, and the
  I32 word-order glitches in the millions of watts. → RANGE_LIMITS: a value
  outside its physical range is dropped; better absent than wrong.

* SINGLE-POLL SPIKES in PV/AC power: adjacent polls swung PV 1.6 ↔ 6.2 kW while
  the real output was flat (non-simultaneous batched reads during fast battery
  transitions). One such spike once commanded a 2500 W force-charge that stuck
  (fixed by meter-fed sizing, da16ef2) and glitched load flips flapped the
  self-consumption branch on 2026-07-03 07:10 (which, combined with the 47086
  enum bug, dumped the battery to grid at 0.27 DKK/kWh). → SPIKE_DELTA_W: a jump
  bigger than the key's delta is held back one poll and only accepted when the
  NEXT poll agrees (within the same delta) — a glitch never repeats, a real step
  (oven, cloud edge) is confirmed 1 poll (~10 s) later. batt_power and
  meter_active_power are deliberately NOT spike-filtered: they are the physical
  feedback signals the controller sizes charges from, and holding them stale
  would be worse than a rare glitch they haven't shown.

* DERIVED house_load GOING NEGATIVE (computed active_power + meter − batt from
  reads captured ms apart; −4084 W observed, −321 W on 2026-07-03). A negative
  house load is physically impossible; the old max(0, ·) clamp turned it into a
  fake "load 0 W" that manufactured a PV surplus. → small negatives (≥ −150 W,
  metering noise) clamp to 0; anything lower is a skewed sample and is dropped.

* DROPOUTS: batt_soc (register 37760, own batch) vanishes on ~10% of polls; a
  failed batch drops whole key groups. → HOLD_MAX_S: a missing/rejected key is
  presented as its last accepted value while that is fresh, then goes absent.
  The dashboard already keeps the last shown value for absent keys, and the
  controller skips the tick when critical inputs are absent.

* GARBAGE ENUM CODES in the control read-backs (47086/47087/47100/47415…).
  The controller re-issues writes whenever a read-back disagrees with the
  target, so one corrupt read triggers a needless write — and write flooding
  desyncs the single-client SDongle. → ENUM_VALUES: unknown codes are dropped
  (a None read-back makes the controller fall back to its optimistic cache).

The sanitizer is stateful (last-good values, pending spike candidates) — one
instance lives on the ModbusWorker and apply() runs on its thread only.
"""

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# Physical plausibility per key — (lo, hi), inclusive. Values outside are DROPPED.
# Bounds are generous versions of the hardware limits: SUN2000-6KTL-M1 inverter,
# LUNA2000-5kWh battery (±2500 W), 3×230 V grid. meter_active_power keeps 35 kW
# headroom on purpose: the SCharger-22KT-S0 EV charger (22 kW, three-phase) meters
# as house consumption, and a 10 kW cap would discard every poll of a charging
# session; the glitches this bound exists for are in the millions of watts.
RANGE_LIMITS: dict[str, tuple[float, float]] = {
    'pv1_voltage':          (0.0, 1000.0),
    'pv2_voltage':          (0.0, 1000.0),
    'pv1_current':          (0.0, 30.0),
    'pv2_current':          (0.0, 30.0),
    'pv1_power':            (0.0, 7000.0),
    'pv2_power':            (0.0, 7000.0),
    'active_power':         (-8000.0, 8000.0),
    'reactive_power':       (-8000.0, 8000.0),
    'power_factor':         (-1.0, 1.0),
    'grid_voltage_a':       (0.0, 300.0),
    'grid_voltage_b':       (0.0, 300.0),
    'grid_voltage_c':       (0.0, 300.0),
    'grid_current_a':       (0.0, 60.0),
    'grid_current_b':       (0.0, 60.0),
    'grid_current_c':       (0.0, 60.0),
    'grid_frequency':       (45.0, 55.0),
    'meter_frequency':      (45.0, 55.0),
    'internal_temp':        (-40.0, 120.0),
    'batt_temperature':     (-40.0, 80.0),
    'batt_bus_voltage':     (0.0, 1000.0),
    'batt_soc':             (0.0, 100.0),
    'batt_soh':             (0.0, 100.0),
    'batt_power':           (-3000.0, 3000.0),
    'batt_charge_total':    (0.0, 1e7),
    'batt_discharge_total': (0.0, 1e7),
    'batt_rated_capacity':  (0.5, 100.0),
    'meter_active_power':   (-35000.0, 35000.0),
    'meter_reactive_power': (-35000.0, 35000.0),
    'meter_export_energy':  (0.0, 1e7),
    'meter_import_energy':  (0.0, 1e7),
    'daily_yield':          (0.0, 200.0),
    'total_yield':          (0.0, 1e7),
    'max_charge_power':     (0.0, 5000.0),
    'max_discharge_power':  (0.0, 5000.0),
    'max_feed_grid_w':      (0.0, 100000.0),
    'max_feed_grid_pct':    (0.0, 100.0),
    'house_load':           (0.0, 35000.0),
}

# Valid codes for the control/status read-backs (register 47086 uses
# StorageWorkingModesC — see register_map.BATTERY_WORKING_MODES). inverter_state
# is left unvalidated: its label map renders unknown codes explicitly.
ENUM_VALUES: dict[str, frozenset] = {
    'batt_status':        frozenset(range(0, 5)),
    'batt_working_mode':  frozenset(range(0, 6)),
    'grid_charge_enable': frozenset((0, 1)),
    'batt_forced_mode':   frozenset(range(0, 3)),
    'active_power_mode':  frozenset(range(0, 8)),
}

# Keys whose value must repeat (within the delta) on the next poll before a jump
# bigger than the delta is accepted. Real steps this size exist (oven on, cloud
# edge) — they just arrive one poll late; single-poll glitches never repeat.
SPIKE_DELTA_W: dict[str, float] = {
    'pv1_power':   1500.0,
    'pv2_power':   1500.0,
    'active_power': 2000.0,
    'house_load':  1500.0,
}

# Keys substituted with their last accepted value while it is at most this old
# (seconds) whenever the current poll has no valid reading. Past the age the key
# simply goes absent — consumers treat that as "unknown", never as 0.
HOLD_MAX_S: dict[str, float] = {
    'batt_soc':     600.0,   # drops out on ~10% of polls; moves ≤ ~1%/min anyway
    'house_load':   90.0,
    'pv1_power':    60.0,
    'pv2_power':    60.0,
    'active_power': 60.0,
}

# Small negative house_load is metering/rounding noise around zero; below this
# the sample is skew between the non-simultaneous component reads — drop it.
_HOUSE_LOAD_NOISE_W = -150.0

_LOG_EVERY = 200  # warn on the 1st drop of a key and every Nth after (debug between)


class DataSanitizer:
    """Stateful per-poll cleaner — see the module docstring for the policy."""

    def __init__(self):
        self._last:    dict[str, tuple[float, float]] = {}  # key → (value, ts)
        self._pending: dict[str, float] = {}                # key → unconfirmed jump
        self._drops:   dict[str, int] = {}                  # key → rejects so far

    def _reject(self, key: str, value, reason: str):
        n = self._drops.get(key, 0)
        self._drops[key] = n + 1
        logger = log.warning if n % _LOG_EVERY == 0 else log.debug
        logger('sanitize: dropped %s=%s (%s, reject #%d)', key, value, reason, n + 1)

    def apply(self, data: dict, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        out = dict(data)
        held: set[str] = set()

        # Negative-noise clamp must run before the range check (house_load's
        # valid range starts at 0).
        hl = out.get('house_load')
        if hl is not None and _HOUSE_LOAD_NOISE_W <= hl < 0.0:
            out['house_load'] = 0.0

        # 1. Physical range validation — impossible values are dropped.
        for key, (lo, hi) in RANGE_LIMITS.items():
            v = out.get(key)
            if v is None or not isinstance(v, (int, float)):
                continue
            if not lo <= v <= hi:
                self._reject(key, v, f'outside [{lo:g}, {hi:g}]')
                del out[key]

        # 2. Enum validation — unknown control/status codes are dropped.
        for key, valid in ENUM_VALUES.items():
            v = out.get(key)
            if v is None:
                continue
            if int(round(v)) not in valid:
                self._reject(key, v, 'unknown enum code')
                del out[key]

        # 3. Spike confirmation — a large jump is held back one poll and only
        #    accepted when the next poll repeats it.
        for key, delta in SPIKE_DELTA_W.items():
            v = out.get(key)
            if v is None:
                continue
            last = self._last.get(key)
            if last is None or now - last[1] > HOLD_MAX_S.get(key, 60.0):
                continue  # nothing fresh to compare against — accept
            pending = self._pending.get(key)
            if abs(v - last[0]) <= delta or (pending is not None
                                             and abs(v - pending) <= delta):
                self._pending.pop(key, None)
                continue  # small move, or a confirmed real step — accept
            self._pending[key] = v
            out[key] = last[0]
            held.add(key)
            self._reject(key, v, f'unconfirmed jump from {last[0]:g}')

        # 4. Hold-last-good — substitute a fresh previous value for a key the
        #    current poll couldn't provide (dropped above or batch failure).
        for key, max_age in HOLD_MAX_S.items():
            if key in out:
                continue
            last = self._last.get(key)
            if last is not None and now - last[1] <= max_age:
                out[key] = last[0]
                held.add(key)

        # 5. Remember accepted CURRENT readings only — a held substitution must
        #    not refresh its own timestamp, or stale data would live forever.
        for key in SPIKE_DELTA_W.keys() | HOLD_MAX_S.keys():
            if key in out and key not in held:
                self._last[key] = (out[key], now)

        return out
