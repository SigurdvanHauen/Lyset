"""
AutoController — periodic optimizer for solar/battery control.

Decision loop (every 60 s while enabled), in priority order:

1. NEGATIVE EXPORT PRICE (export_dkk < −0.01 DKK/kWh)
   a) Limit PV inverter output to household consumption (40525=2, 40527=house_load W)
      — only written when solar is actually producing (pv_w > 300 W)
   b) Battery — prevent discharge to grid:
      SoC < 95 %: force-charge at max rate (47086=1, 47098=5000 W, 47100=1)
      SoC ≥ 95 %: force-idle, neither charge nor discharge (47086=1, 47100=0)

2. GRID CHARGE (import_dkk < future_max_import − CHARGE_MARGIN AND SoC < threshold)
   Remove PV limit (40525=0), grid-charge battery (47087=1, 47079=2000 W, 47086=4, 47100=0).
   Dynamic threshold: justifies charging whenever today/tomorrow has a significantly
   more expensive slot within CHARGE_HORIZON_H hours.

3. HOLD BATTERY FOR PEAK (upcoming import > import_now + HOLD_DELTA AND import_now < MAX_HOLD_IMPORT)
   Force-idle battery (47086=1, 47100=0) so the current (cheap) load is covered
   by grid import rather than draining battery needed for the upcoming expensive period.

4. EXPORT ARBITRAGE (export_dkk > cheapest future import + ARBIT_MARGIN AND SoC > MIN_SOC_ARBIT
                     AND import_dkk < MAX_HOLD_IMPORT)
   Force-discharge battery to grid (47086=1, 47100=2); battery energy is sold now
   and replaced later when import is cheapest within ARBIT_HORIZON_H hours.
   Guard: only fires when import is cheap — during expensive periods self-consumption
   saves more per kWh than the export round-trip gain.

5. DEFAULT — max self-consumption
   Remove PV limit (40525=0), mode=4 (47086=4), clear forced commands (47100=0).

On disable → restore: 40525=0, 47100=0, 47087=0, 47086=4.
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

_EVAL_INTERVAL_S = 60

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

    def __init__(self):
        self.enabled:         bool            = False
        self.last_action:     str             = '—'
        self.last_action_ts:  Optional[float] = None
        self._on_command:     Optional[Callable[[str, str], None]] = None
        self._grid_charging:  bool            = False  # hysteresis state

    def set_command_callback(self, cb: Callable[[str, str], None]):
        self._on_command = cb

    # ── Public control ────────────────────────────────────────────────────────

    def enable(self, worker, prices: list, last_data: dict):
        self.enabled = True
        log.info('AutoCtrl: enabled')
        if worker and worker.is_alive() and prices and last_data:
            cmd = self._decide(worker, prices, last_data)
            if cmd:
                self._record(cmd)

    def disable(self, worker):
        self.enabled = False
        self._grid_charging = False
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
        worker.write_u16(40525, 0, 'AutoCtrl OFF: remove PV limit')
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

        # ── 1. Negative export price ──────────────────────────────────────────
        if export_dkk < _NEGATIVE_EXPORT_DKK:
            self._grid_charging = False
            if pv_w > _MIN_PV_W:
                limit_w = max(int(house_load), 100)
                worker.write_u16(40525, 2, 'AutoCtrl: W-limit mode')
                worker.write_i32(40527, limit_w, f'AutoCtrl: PV limit {limit_w} W')
                pv_detail = f'PV limited to {limit_w} W'
            else:
                worker.write_u16(40525, 0, 'AutoCtrl: no limit (low solar)')
                pv_detail = 'Low solar'

            worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
            if batt_soc < _FORCE_CHARGE_SOC_MAX:
                worker.write_u16(47086, 1, 'AutoCtrl: mode=forced')
                worker.write_u32(47098, _MAX_FORCE_CHARGE_W, f'AutoCtrl: charge {_MAX_FORCE_CHARGE_W} W')
                worker.write_u16(47100, 1, 'AutoCtrl: force CHARGE')
                batt_detail = f', battery force-charging (SoC {batt_soc:.0f}%)'
            else:
                worker.write_u16(47086, 1, 'AutoCtrl: mode=forced (idle)')
                worker.write_u16(47100, 0, 'AutoCtrl: force IDLE')
                batt_detail = f', battery idle — full (SoC {batt_soc:.0f}%)'

            detail = f'{pv_detail} (export {export_dkk:.3f} DKK){batt_detail}'
            return _Cmd(mode='export_limited', detail=detail)

        # ── 2. Grid charge: cheap now vs expensive later ──────────────────────
        # Uses forced-charge mode (47086=1, 47100=1) rather than mode-4 + grid-charge-enable
        # (47087=1) because mode 4 still runs self-consumption logic that can discharge the
        # battery for house loads, counteracting the grid charge at night.
        charge_window = _window(_CHARGE_HORIZON_H)
        if charge_window and batt_soc < gc_threshold:
            future_max_import = max(p['import'] for p in charge_window)
            if import_dkk < future_max_import - _CHARGE_MARGIN_DKK:
                self._grid_charging = True
                worker.write_u16(40525, 0, 'AutoCtrl: no PV limit')
                worker.write_u16(47087, 0, 'AutoCtrl: grid charge feature OFF')
                worker.write_u16(47086, 1, 'AutoCtrl: mode=forced')
                worker.write_u32(47098, _GRID_CHARGE_W, f'AutoCtrl: charge {_GRID_CHARGE_W} W')
                worker.write_u16(47100, 1, 'AutoCtrl: force CHARGE')
                detail = (f'Grid charging {_GRID_CHARGE_W} W '
                          f'(import {import_dkk:.3f} DKK, future max {future_max_import:.3f} DKK, '
                          f'SoC {batt_soc:.0f}%)')
                return _Cmd(mode='grid_charge', detail=detail)

        # ── 3. Hold battery for upcoming price peak ───────────────────────────
        # Force-idle to preserve SoC; load is covered by (currently cheaper) grid.
        hold_window = _window(_HOLD_HORIZON_H)
        if (hold_window
                and import_dkk < _MAX_HOLD_IMPORT_DKK
                and batt_soc > _MIN_SOC_HOLD):
            max_upcoming = max(p['import'] for p in hold_window)
            if max_upcoming > import_dkk + _HOLD_DELTA_DKK:
                self._grid_charging = False
                worker.write_u16(40525, 0, 'AutoCtrl: no PV limit')
                worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
                worker.write_u16(47086, 1, 'AutoCtrl: mode=forced (idle)')
                worker.write_u16(47100, 0, 'AutoCtrl: force IDLE')
                detail = (f'Holding battery: peak {max_upcoming:.3f} DKK in ≤{_HOLD_HORIZON_H}h '
                          f'(now {import_dkk:.3f} DKK, SoC {batt_soc:.0f}%)')
                return _Cmd(mode='hold_battery', detail=detail)

        # ── 4. Export arbitrage ───────────────────────────────────────────────
        # Only when import is cheap — during expensive periods self-consumption
        # saves more than the export round-trip gain, so arbitrage is not worthwhile.
        arbit_window = _window(_ARBIT_HORIZON_H)
        if (arbit_window
                and batt_soc > _MIN_SOC_ARBIT
                and import_dkk < _MAX_HOLD_IMPORT_DKK):
            future_min_import = min(p['import'] for p in arbit_window)
            if export_dkk > future_min_import + _ARBIT_MARGIN_DKK:
                self._grid_charging = False
                worker.write_u16(40525, 0, 'AutoCtrl: no PV limit')
                worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
                worker.write_u16(47086, 1, 'AutoCtrl: mode=forced')
                worker.write_u16(47100, 2, 'AutoCtrl: force DISCHARGE')
                detail = (f'Arb. discharge: export {export_dkk:.3f} DKK > '
                          f'future min import {future_min_import:.3f} DKK (SoC {batt_soc:.0f}%)')
                return _Cmd(mode='arbit_discharge', detail=detail)

        # ── 5. Default: max self-consumption ──────────────────────────────────
        self._grid_charging = False
        worker.write_u16(40525, 0, 'AutoCtrl: no PV limit')
        worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
        worker.write_u16(47086, 4, 'AutoCtrl: mode=max self-consumption')
        worker.write_u16(47100, 0, 'AutoCtrl: stop forced mode')
        detail = (f'Max self-consumption '
                  f'(export {export_dkk:.3f} DKK, import {import_dkk:.3f} DKK)')
        return _Cmd(mode='export_unlimited', detail=detail)
