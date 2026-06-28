"""
AutoController — periodic optimizer for solar/battery control.

Decision loop (every 60 s while enabled), in priority order:

1. NEGATIVE EXPORT PRICE (export_dkk < −0.01 DKK/kWh)
   a) Limit PV inverter output to household consumption (40525=2, 40527=house_load W)
      — only written when solar is actually producing (pv_w > 300 W)
   b) Battery — prevent discharge to grid:
      SoC < 95 %: force-charge at max rate (47086=1, 47098=5000 W, 47100=1)
      SoC ≥ 95 %: force-idle, neither charge nor discharge (47086=1, 47100=0)

2. CHEAP IMPORT (import_dkk < 0.50 DKK/kWh AND batt_soc < 80 %)
   Remove PV limit (40525=0), grid-charge battery (47087=1, 47079=2000 W, 47086=4, 47100=0)

3. DEFAULT — max self-consumption
   Remove PV limit (40525=0), mode=4 (47086=4), clear forced commands (47100=0)

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
CHEAP_IMPORT_DKK      = 0.50    # import below → allow grid charging
GRID_CHARGE_SOC_START = 75.0    # only begin grid charging below this (hysteresis low)
GRID_CHARGE_SOC_MAX   = 80.0    # stop grid charging above this (hysteresis high)
GRID_CHARGE_W         = 2000    # W — grid charge rate
FORCE_CHARGE_SOC_MAX  = 95.0    # above this, force-idle instead of force-charge
MAX_FORCE_CHARGE_W    = 5000    # W — battery charge rate (inverter clamps to rated max)
_EVAL_INTERVAL_S      = 60      # how often the loop re-evaluates

# Private aliases kept for internal use within this module
_NEGATIVE_EXPORT_DKK  = NEGATIVE_EXPORT_DKK
_MIN_PV_W             = MIN_PV_W
_CHEAP_IMPORT_DKK     = CHEAP_IMPORT_DKK
_GRID_CHARGE_SOC_START = GRID_CHARGE_SOC_START
_GRID_CHARGE_SOC_MAX  = GRID_CHARGE_SOC_MAX
_GRID_CHARGE_W        = GRID_CHARGE_W
_FORCE_CHARGE_SOC_MAX = FORCE_CHARGE_SOC_MAX
_MAX_FORCE_CHARGE_W   = MAX_FORCE_CHARGE_W


@dataclass
class _Cmd:
    mode:   str   # 'export_unlimited' | 'export_limited' | 'grid_charge'
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

        # Hysteresis: continue grid charging up to SOC_MAX, only start below SOC_START
        gc_threshold = _GRID_CHARGE_SOC_MAX if self._grid_charging else _GRID_CHARGE_SOC_START

        # ── 1. Negative export price ──────────────────────────────────────────
        if export_dkk < _NEGATIVE_EXPORT_DKK:
            self._grid_charging = False
            # Limit inverter output to household consumption
            if pv_w > _MIN_PV_W:
                limit_w = max(int(house_load), 100)
                worker.write_u16(40525, 2, 'AutoCtrl: W-limit mode')
                worker.write_i32(40527, limit_w, f'AutoCtrl: PV limit {limit_w} W')
                pv_detail = f'PV limited to {limit_w} W'
            else:
                worker.write_u16(40525, 0, 'AutoCtrl: no limit (low solar)')
                pv_detail = 'Low solar'

            # Stop battery from discharging to grid
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

        # ── 2. Cheap import: charge battery from grid ─────────────────────────
        if import_dkk < _CHEAP_IMPORT_DKK and batt_soc < gc_threshold:
            self._grid_charging = True
            worker.write_u16(40525, 0, 'AutoCtrl: no PV limit')
            worker.write_u16(47087, 1, 'AutoCtrl: grid charge ON')
            worker.write_u32(47079, _GRID_CHARGE_W, f'AutoCtrl: grid {_GRID_CHARGE_W} W')
            worker.write_u16(47086, 4, 'AutoCtrl: mode=max self-consumption')
            worker.write_u16(47100, 0, 'AutoCtrl: clear forced mode')
            detail = (f'Grid charging {_GRID_CHARGE_W} W '
                      f'(import {import_dkk:.3f} DKK, SoC {batt_soc:.0f}%)')
            return _Cmd(mode='grid_charge', detail=detail)

        # ── 3. Default: max self-consumption ──────────────────────────────────
        self._grid_charging = False
        worker.write_u16(40525, 0, 'AutoCtrl: no PV limit')
        worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
        worker.write_u16(47086, 4, 'AutoCtrl: mode=max self-consumption')
        worker.write_u16(47100, 0, 'AutoCtrl: stop forced mode')
        detail = (f'Max self-consumption '
                  f'(export {export_dkk:.3f} DKK, import {import_dkk:.3f} DKK)')
        return _Cmd(mode='export_unlimited', detail=detail)
