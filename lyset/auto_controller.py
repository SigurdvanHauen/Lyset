"""
AutoController — periodic optimizer for solar/battery control.

Decision loop (every 60 s while enabled), in priority order:

1. CHEAP IMPORT (registers 47086, 47087, 47079, 47100)
   • import_price < 0.50 DKK/kWh AND batt_soc < 80 %
     → mode=max-self-consumption (47086=4), grid charge ON (47087=1, 47079=2000 W)
     → clear forced mode (47100=0)

2. NEGATIVE EXPORT + BATTERY HAS ROOM (registers 47086, 47098, 47100)
   • export_price < −0.01 DKK/kWh AND pv_power > 300 W AND batt_soc < 95 %
     → force-charge battery at max rate (47086=1, 47098=5000 W, 47100=1)
       to absorb excess solar instead of exporting at negative prices

3. DEFAULT — max self-consumption, clear stale forced commands
   • mode=4 (47086=4), stop forced mode (47100=0), grid charge OFF (47087=0)

Register 40525 (active power curtailment) is written as best-effort in all cases
where export is negative — some firmware versions silently ignore it.

On disable → restore: 40525=0, 47100=0, 47087=0, 47086=4.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
_NEGATIVE_EXPORT_DKK   = -0.01   # export price below → try to limit/absorb
_MIN_PV_W              = 300     # ignore when solar output is negligible
_CHEAP_IMPORT_DKK      = 0.50    # import price below → allow grid charging
_GRID_CHARGE_SOC_MAX   = 80.0    # don't grid-charge above this SoC
_GRID_CHARGE_W         = 2000    # W — grid charge rate
_FORCE_CHARGE_SOC_MAX  = 95.0    # don't force-charge above this SoC
_MAX_FORCE_CHARGE_W    = 5000    # W — max battery charge rate (inverter clamps internally)
_LOAD_HEADROOM_W       = 300     # W — margin above house_load when writing PV curtail limit
_EVAL_INTERVAL_S       = 60      # how often the loop re-evaluates


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

        # Best-effort PV curtailment (some firmware ignores 40525).
        # Battery force-charge (priority 2 below) acts as the real fallback.
        if export_dkk < _NEGATIVE_EXPORT_DKK and pv_w > _MIN_PV_W:
            limit_w = max(int(house_load) + _LOAD_HEADROOM_W, 500)
            worker.write_u16(40525, 2, 'AutoCtrl: W-limit mode')
            worker.write_i32(40527, limit_w, f'AutoCtrl: PV limit {limit_w} W')
        else:
            worker.write_u16(40525, 0, 'AutoCtrl: no PV limit')

        # ── Priority 1: Cheap import → charge from grid ───────────────────────
        if import_dkk < _CHEAP_IMPORT_DKK and batt_soc < _GRID_CHARGE_SOC_MAX:
            worker.write_u16(47087, 1, 'AutoCtrl: grid charge ON')
            worker.write_u32(47079, _GRID_CHARGE_W, f'AutoCtrl: grid {_GRID_CHARGE_W} W')
            worker.write_u16(47086, 4, 'AutoCtrl: mode=max self-consumption')
            worker.write_u16(47100, 0, 'AutoCtrl: clear forced mode')
            detail = (f'Grid charging {_GRID_CHARGE_W} W '
                      f'(import {import_dkk:.3f} DKK, SoC {batt_soc:.0f}%)')
            return _Cmd(mode='grid_charge', detail=detail)

        # ── Priority 2: Negative export + room in battery → force-charge ──────
        # Absorbs excess solar that would otherwise be exported at a negative price.
        # Also stops any stale forced-discharge command from a third-party system.
        if export_dkk < _NEGATIVE_EXPORT_DKK and pv_w > _MIN_PV_W and batt_soc < _FORCE_CHARGE_SOC_MAX:
            worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
            worker.write_u16(47086, 1, 'AutoCtrl: mode=forced charge/discharge')
            worker.write_u32(47098, _MAX_FORCE_CHARGE_W, f'AutoCtrl: force {_MAX_FORCE_CHARGE_W} W')
            worker.write_u16(47100, 1, 'AutoCtrl: force CHARGE')
            detail = (f'Force-charging battery at {_MAX_FORCE_CHARGE_W} W to absorb excess solar '
                      f'(export {export_dkk:.3f} DKK, SoC {batt_soc:.0f}%)')
            return _Cmd(mode='export_limited', detail=detail)

        # ── Default: max self-consumption, clear any stale forced command ──────
        worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
        worker.write_u16(47086, 4, 'AutoCtrl: mode=max self-consumption')
        worker.write_u16(47100, 0, 'AutoCtrl: stop forced mode')
        if export_dkk < _NEGATIVE_EXPORT_DKK:
            detail = (f'Battery full (SoC {batt_soc:.0f}% ≥ {_FORCE_CHARGE_SOC_MAX:.0f}%), '
                      f'cannot absorb more solar (export {export_dkk:.3f} DKK) — exporting at loss')
        else:
            detail = (f'Max self-consumption '
                      f'(export {export_dkk:.3f} DKK, import {import_dkk:.3f} DKK)')
        return _Cmd(mode='export_unlimited', detail=detail)
