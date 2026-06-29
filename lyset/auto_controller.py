"""
AutoController — periodic optimizer for solar/battery control.

Runs every 15 s while enabled and picks ONE action per tick, in priority order:

1. NEGATIVE EXPORT (export < −0.01 DKK/kWh)
   Self-consumption (mode 4): route the PV surplus into the battery; never
   discharge to grid.  PV cannot be curtailed via Modbus on this SDongle, so
   surplus beyond the battery's charge rate is still exported at the negative
   price (unavoidable).  We do NOT force-charge from grid here — that would
   import at the positive import price to dodge a small negative export.

2. GRID CHARGE (now at/near the cheapest of the next CHARGE_HORIZON_H h, a
   materially pricier slot ahead, SoC below threshold)
   Force-charge from grid (47086=1, 47075+47247=2500 W, 47100=1) to bank cheap
   energy for the expensive period.

3. HOLD FOR PEAK (deficit now, import < MAX_HOLD, a slot ≥ HOLD_DELTA pricier
   within HOLD_HORIZON_H h, SoC > MIN_SOC_HOLD)
   Force-idle (47086=1, 47100=0): cover the cheap current load from grid and
   save the battery for the peak.  Gated on a deficit — during a surplus the
   battery should keep charging instead of idling.

4. EXPORT ARBITRAGE (export > cheapest future import + ARBIT_MARGIN within
   ARBIT_HORIZON_H h, SoC > MIN_SOC_ARBIT, import < MAX_HOLD)
   Force-discharge to grid (47086=1, 47077+47247=2500 W, 47100=2): sell stored
   energy high, rebuy cheaper later.  Guarded to cheap-import periods — when
   import is expensive, self-consuming the stored energy beats the round-trip.

5. DEFAULT — self-consumption (mode 4)
   Charge from any solar surplus at the full surplus rate, discharge to cover
   load, touch the grid only for the remainder.  No power setpoints to get wrong;
   the inverter balances it natively and never exports battery energy.

WRITES ARE STATE-GATED — _apply() writes a register only when its value differs
from the last applied value, so steady state issues zero Modbus writes per tick.
Rewriting every register every tick floods the single-client SDongle, desyncs the
Modbus transaction IDs, and triggers cascading "device busy" (exception 6) errors
on reads too.  A full re-apply is forced every _RESYNC_EVERY ticks to recover
from external drift.

Register notes for this SDongle firmware:
  • 40525/40527 (PV output limit) — NOT writable (Illegal Data Address).
  • Charge/discharge POWER registers are U32 — write as two words (write_u32),
    never one (write_u16 → Illegal Data Address):
      47075 max charge power, 47077 max discharge power,
      47247 forcible charge/discharge power (the setpoint used while 47100 forces
      a charge/discharge; a low default here pins forced charging to ~200 W).
  • Forced control is U16: 47086 mode (1=forced, 4=self-consumption),
      47087 grid-charge enable, 47100 forced command (0=stop,1=charge,2=discharge).

On disable → restore: 47100=0, 47087=0, 47086=4 (self-consumption).
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── Thresholds (also imported by server.py for the SoC simulation) ────────────
NEGATIVE_EXPORT_DKK   = -0.01   # export below → limit PV and stop battery discharge
MIN_PV_W              = 300     # below this, don't bother writing a PV limit
CHEAP_IMPORT_DKK      = 0.50    # legacy constant — kept for _simulate_soc in server.py
GRID_CHARGE_SOC_START = 75.0    # only begin grid charging below this (hysteresis low)
GRID_CHARGE_SOC_MAX   = 80.0    # stop grid charging above this (hysteresis high)
GRID_CHARGE_W         = 2500    # W — grid charge rate
FORCE_CHARGE_SOC_MAX  = 95.0    # above this, force-idle instead of force-charge
MAX_FORCE_CHARGE_W    = 5000    # W — battery charge rate (inverter clamps to rated max)

# ── Look-ahead optimisation parameters ───────────────────────────────────────
ARBIT_MARGIN_DKK    = 0.10   # export must exceed future cheapest import by at least this
MIN_SOC_ARBIT       = 20.0   # minimum SoC before allowing arbitrage discharge
ARBIT_HORIZON_H     = 12     # hours ahead to find cheapest import for arbitrage
CHARGE_MARGIN_DKK   = 0.15   # import must be at least this cheaper than future max to justify charging
CHARGE_HORIZON_H    = 24     # hours ahead to look for price peaks that justify grid charging
HOLD_DELTA_DKK      = 0.80   # hold battery if a future slot is at least this much more expensive
HOLD_HORIZON_H      = 6      # hours ahead to scan for an expensive upcoming slot
MAX_HOLD_IMPORT_DKK = 1.50   # only hold battery when current import is below this
MIN_SOC_HOLD        = 20.0   # minimum SoC required to bother holding

_EVAL_INTERVAL_S = 15

# Private aliases for internal use
_NEGATIVE_EXPORT_DKK  = NEGATIVE_EXPORT_DKK
_MIN_PV_W             = MIN_PV_W
_GRID_CHARGE_SOC_START = GRID_CHARGE_SOC_START
_GRID_CHARGE_SOC_MAX  = GRID_CHARGE_SOC_MAX
_GRID_CHARGE_W        = GRID_CHARGE_W
_FORCE_CHARGE_SOC_MAX = FORCE_CHARGE_SOC_MAX
_MAX_FORCE_CHARGE_W   = MAX_FORCE_CHARGE_W
_ARBIT_MARGIN_DKK     = ARBIT_MARGIN_DKK
_MIN_SOC_ARBIT        = MIN_SOC_ARBIT
_ARBIT_HORIZON_H      = ARBIT_HORIZON_H
_CHARGE_MARGIN_DKK    = CHARGE_MARGIN_DKK
_CHARGE_HORIZON_H     = CHARGE_HORIZON_H
_HOLD_DELTA_DKK       = HOLD_DELTA_DKK
_HOLD_HORIZON_H       = HOLD_HORIZON_H
_MAX_HOLD_IMPORT_DKK  = MAX_HOLD_IMPORT_DKK
_MIN_SOC_HOLD         = MIN_SOC_HOLD


@dataclass
class _Cmd:
    mode:   str   # 'export_unlimited' | 'export_limited' | 'grid_charge' |
                  # 'arbit_discharge'  | 'hold_battery'
    detail: str   # human-readable summary for the log / UI


class AutoController:
    """
    Manages periodic decisions about inverter output limits and battery charging.
    Disabled by default — call enable() to start controlling the system.
    """

    # Force a full re-apply of all registers every Nth tick, so the controller
    # recovers if the inverter state drifts (e.g. manual change, reconnect).
    _RESYNC_EVERY = 20  # ticks (20 × 15 s = 5 min)

    def __init__(self):
        self.enabled:         bool            = False
        self.last_action:     str             = '—'
        self.last_action_ts:  Optional[float] = None
        self._on_command:     Optional[Callable[[str, str], None]] = None
        self._grid_charging:  bool            = False  # hysteresis state
        self._applied:        dict[int, int]  = {}     # addr → last value written
        self._tick:           int             = 0

    def set_command_callback(self, cb: Callable[[str, str], None]):
        self._on_command = cb

    # ── State-gated register writes ───────────────────────────────────────────

    def _apply(self, worker, regs: list[tuple]):
        """
        Write only the registers whose value changed since the last applied state.

        `regs` is an ordered list of (width, address, value, description) tuples
        (width is 16 or 32).  Order is preserved for changed registers so that
        sequencing constraints (e.g. set forced mode before issuing the forced
        command, clear the command before leaving forced mode) still hold.
        """
        for width, addr, val, desc in regs:
            if self._applied.get(addr) == val:
                continue
            if width == 32:
                worker.write_u32(addr, val, desc)
            else:
                worker.write_u16(addr, val, desc)
            self._applied[addr] = val

    # ── Public control ────────────────────────────────────────────────────────

    def enable(self, worker, prices: list, last_data: dict):
        self.enabled = True
        self._applied.clear()  # force a full re-apply on the first decision
        log.info('AutoCtrl: enabled')
        if worker and worker.is_alive() and prices and last_data:
            cmd = self._decide(worker, prices, last_data)
            if cmd:
                self._record(cmd)

    def disable(self, worker):
        self.enabled = False
        self._grid_charging = False
        self._applied.clear()
        log.info('AutoCtrl: disabled — restoring defaults')
        if worker and worker.is_alive():
            self._restore_defaults(worker)

    # ── Async evaluation loop ─────────────────────────────────────────────────

    async def run(self, get_worker, get_prices, get_last_data):
        await asyncio.sleep(15)  # let startup settle before first tick
        while True:
            await asyncio.sleep(_EVAL_INTERVAL_S)
            if not self.enabled:
                continue
            try:
                worker    = get_worker()
                prices    = get_prices()
                last_data = get_last_data()
                if worker and worker.is_alive() and prices and last_data:
                    cmd = self._decide(worker, prices, last_data)
                    if cmd:
                        self._record(cmd)
            except Exception as exc:
                log.error('AutoController error: %s', exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _restore_defaults(self, worker):
        worker.write_u16(47100, 0, 'AutoCtrl OFF: stop forced mode')
        worker.write_u16(47087, 0, 'AutoCtrl OFF: disable grid charge')
        worker.write_u16(47086, 4, 'AutoCtrl OFF: mode=max self-consumption')

    def _record(self, cmd: _Cmd):
        self.last_action    = cmd.detail
        self.last_action_ts = time.time()
        log.info('AutoCtrl: %s', cmd.detail)
        if self._on_command:
            self._on_command(cmd.mode, cmd.detail)

    def _decide(self, worker, prices: list, data: dict) -> Optional[_Cmd]:
        now_ms = int(time.time() * 1000)

        cur_price = None
        for p in reversed(prices):
            if p['ts'] <= now_ms:
                cur_price = p
                break
        if cur_price is None:
            return None

        export_dkk = cur_price.get('export') or 0.0
        import_dkk = cur_price.get('import') or 0.0
        pv_w       = (data.get('pv1_power') or 0) + (data.get('pv2_power') or 0)
        house_load = data.get('house_load') or 0
        batt_soc   = data.get('batt_soc')   or 50.0
        grid_w     = data.get('meter_active_power') or 0.0  # +import / -export
        batt_w     = data.get('batt_power') or 0.0           # +charge / -discharge

        log.info(
            'AutoCtrl: import=%.3f  export=%.3f  PV=%.0fW  grid=%+.0fW  load=%.0fW  SoC=%.1f%%  batt=%+.0fW',
            import_dkk, export_dkk, pv_w, grid_w, house_load, batt_soc, batt_w,
        )

        # Periodically clear the applied-state cache so the next _apply() re-asserts
        # every register, recovering from any external drift in the inverter state.
        self._tick += 1
        if self._tick % self._RESYNC_EVERY == 0:
            self._applied.clear()

        # Future price slots sorted ascending
        future_prices = sorted(
            (p for p in prices if p['ts'] > now_ms),
            key=lambda p: p['ts'],
        )

        def _window(hours: int) -> list:
            cutoff = now_ms + hours * 3_600_000
            return [p for p in future_prices if p['ts'] <= cutoff]

        # Hysteresis: continue grid charging up to SOC_MAX, only start below SOC_START
        gc_threshold = _GRID_CHARGE_SOC_MAX if self._grid_charging else _GRID_CHARGE_SOC_START

        surplus_w = pv_w - house_load  # + = solar exceeds load

        # Register sequences reused below. Order matters within a sequence: enter
        # forced mode (47086=1) before issuing a forced command (47100), and clear
        # the forced command (47100=0) before leaving forced mode (47086=4). The
        # state-gating in _apply() only writes the registers that actually changed.
        seq_self_consumption = [
            (16, 47087, 0, 'AutoCtrl: grid charge OFF'),
            (16, 47100, 0, 'AutoCtrl: stop forced'),
            (16, 47086, 4, 'AutoCtrl: mode=max self-consumption'),
        ]

        # ── 1. Negative export → self-consumption (mode 4) ────────────────────
        # Negative export implies a PV surplus (export only happens when pv > load).
        # Mode 4 directs that surplus into the battery at its full charge rate and
        # never discharges to grid. We can't curtail PV here, so surplus beyond the
        # battery's charge rate is still exported at the negative price.
        if export_dkk < _NEGATIVE_EXPORT_DKK:
            self._grid_charging = False
            self._apply(worker, seq_self_consumption)
            detail = (f'Negative export {export_dkk:.3f} DKK — self-consumption '
                      f'(PV {pv_w:.0f} W, load {house_load:.0f} W, SoC {batt_soc:.0f}%)')
            return _Cmd(mode='export_unlimited', detail=detail)

        # ── 2. Grid charge: bank cheap energy for a pricier period ────────────
        # Fires when the current slot is at/near the cheapest of the next
        # CHARGE_HORIZON_H h AND a materially more expensive slot lies ahead AND the
        # battery has room. Forced charge so self-consumption can't discharge it
        # back out overnight.
        charge_window = _window(_CHARGE_HORIZON_H)
        if charge_window and batt_soc < gc_threshold:
            future_min_import = min(p['import'] for p in charge_window)
            future_max_import = max(p['import'] for p in charge_window)
            if (import_dkk <= future_min_import + _CHARGE_MARGIN_DKK
                    and future_max_import > import_dkk + _CHARGE_MARGIN_DKK):
                self._grid_charging = True
                self._apply(worker, [
                    (16, 47087, 0,              'AutoCtrl: grid charge feature OFF'),
                    (16, 47086, 1,              'AutoCtrl: mode=forced'),
                    (32, 47075, _GRID_CHARGE_W, f'AutoCtrl: max charge power {_GRID_CHARGE_W} W'),
                    (32, 47247, _GRID_CHARGE_W, f'AutoCtrl: forced charge power {_GRID_CHARGE_W} W'),
                    (16, 47100, 1,              'AutoCtrl: force CHARGE'),
                ])
                detail = (f'Grid charging {_GRID_CHARGE_W} W '
                          f'(import {import_dkk:.3f} DKK, min {future_min_import:.3f}, '
                          f'max {future_max_import:.3f} DKK, SoC {batt_soc:.0f}%)')
                return _Cmd(mode='grid_charge', detail=detail)

        # ── 3. Hold battery for an upcoming peak (deficit only) ───────────────
        # When the house draws from the battery (no surplus) and a much more
        # expensive slot is coming, force-idle so the cheap current load is met by
        # grid import and the battery is saved for the peak. Gated on a deficit —
        # during a surplus the battery should keep charging via mode 4 instead.
        hold_window = _window(_HOLD_HORIZON_H)
        if (surplus_w < 0
                and hold_window
                and import_dkk < _MAX_HOLD_IMPORT_DKK
                and batt_soc > _MIN_SOC_HOLD):
            max_upcoming = max(p['import'] for p in hold_window)
            if max_upcoming > import_dkk + _HOLD_DELTA_DKK:
                self._grid_charging = False
                self._apply(worker, [
                    (16, 47087, 0, 'AutoCtrl: grid charge OFF'),
                    (16, 47086, 1, 'AutoCtrl: mode=forced'),
                    (16, 47100, 0, 'AutoCtrl: force IDLE'),
                ])
                detail = (f'Holding battery: peak {max_upcoming:.3f} DKK in ≤{_HOLD_HORIZON_H}h '
                          f'(now {import_dkk:.3f} DKK, SoC {batt_soc:.0f}%)')
                return _Cmd(mode='hold_battery', detail=detail)

        # ── 4. Export arbitrage: sell stored energy high, rebuy cheaper ───────
        # Force-discharge to grid when the current export price beats the cheapest
        # upcoming import by a margin and the battery has spare charge. Guarded to
        # cheap-import periods — when import is expensive, self-consuming the stored
        # energy saves more than the export round-trip earns.
        arbit_window = _window(_ARBIT_HORIZON_H)
        if (arbit_window
                and batt_soc > _MIN_SOC_ARBIT
                and import_dkk < _MAX_HOLD_IMPORT_DKK):
            future_min_import = min(p['import'] for p in arbit_window)
            if export_dkk > future_min_import + _ARBIT_MARGIN_DKK:
                self._grid_charging = False
                self._apply(worker, [
                    (16, 47087, 0,              'AutoCtrl: grid charge OFF'),
                    (16, 47086, 1,              'AutoCtrl: mode=forced'),
                    (32, 47077, _GRID_CHARGE_W, f'AutoCtrl: max discharge power {_GRID_CHARGE_W} W'),
                    (32, 47247, _GRID_CHARGE_W, f'AutoCtrl: forced discharge power {_GRID_CHARGE_W} W'),
                    (16, 47100, 2,              'AutoCtrl: force DISCHARGE'),
                ])
                detail = (f'Arb. discharge: export {export_dkk:.3f} DKK > '
                          f'future min import {future_min_import:.3f} DKK (SoC {batt_soc:.0f}%)')
                return _Cmd(mode='arbit_discharge', detail=detail)

        # ── 5. Default: maximise self-consumption (mode 4) ────────────────────
        # The inverter's native mode does exactly what we want: charge the battery
        # from any solar surplus (at the full surplus rate), discharge it to cover
        # load, and use the grid only for the remainder — never exporting battery
        # energy nor importing to charge. No power setpoints to get wrong.
        self._grid_charging = False
        self._apply(worker, seq_self_consumption)
        detail = (f'Self-consumption (PV {pv_w:.0f} W, load {house_load:.0f} W, '
                  f'surplus {surplus_w:+.0f} W, SoC {batt_soc:.1f}%)')
        return _Cmd(mode='export_unlimited', detail=detail)
