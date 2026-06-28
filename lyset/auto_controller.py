"""
AutoController — periodic optimizer for solar/battery control.

Decision loop (every 60 s while enabled):

1. ACTIVE POWER LIMIT (registers 40525 / 40527)
   • export_price < −0.01 DKK/kWh AND pv_power > 300 W
     → limit PV output to house_load + 300 W headroom  (40525=2, 40527=limit)
   • otherwise → no limit  (40525=0)

2. BATTERY / GRID CHARGING (registers 47086, 47087, 47079)
   • import_price < 0.50 DKK/kWh AND batt_soc < 80 %
     → grid charge at 2000 W  (47087=1, 47079=2000, 47086=4)
   • otherwise → disable grid charge  (47087=0, 47086=4)

On disable → restore defaults: 40525=0, 47087=0, 47086=4.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── Thresholds ─────────────────────────────────────────────────────────────────
_NEGATIVE_EXPORT_DKK = -0.01   # export price below → limit PV
_MIN_PV_W            = 300     # don't bother limiting when solar is negligible
_CHEAP_IMPORT_DKK    = 0.50    # import price below → allow grid charging
_GRID_CHARGE_SOC_MAX = 80.0    # don't grid-charge above this SoC
_GRID_CHARGE_W       = 2000    # W — grid charge rate
_LOAD_HEADROOM_W     = 300     # W — safety margin above house_load when capping PV
_EVAL_INTERVAL_S     = 60      # how often the loop re-evaluates


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
        self.enabled:         bool           = False
        self.last_action:     str            = '—'
        self.last_action_ts:  Optional[float] = None
        self._on_command:     Optional[Callable[[str, str], None]] = None

    def set_command_callback(self, cb: Callable[[str, str], None]):
        """Register a callback invoked with (mode, detail) after each decision."""
        self._on_command = cb

    # ── Public control ────────────────────────────────────────────────────────

    def enable(self, worker, prices: list, last_data: dict):
        """Enable auto mode and immediately apply the first decision."""
        self.enabled = True
        log.info('AutoCtrl: enabled')
        if worker and worker.is_alive() and prices and last_data:
            cmd = self._decide(worker, prices, last_data)
            if cmd:
                self._record(cmd)

    def disable(self, worker):
        """Disable auto mode and restore sane inverter defaults."""
        self.enabled = False
        log.info('AutoCtrl: disabled — restoring defaults')
        if worker and worker.is_alive():
            self._restore_defaults(worker)

    # ── Async evaluation loop ─────────────────────────────────────────────────

    async def run(self, get_worker, get_prices, get_last_data):
        """Asyncio task: re-evaluate every _EVAL_INTERVAL_S seconds."""
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

        # Find the most recent price slot that has started
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

        # ── 1. Active power limit ─────────────────────────────────────────────
        if export_dkk < _NEGATIVE_EXPORT_DKK and pv_w > _MIN_PV_W:
            limit_w = max(int(house_load) + _LOAD_HEADROOM_W, 500)
            worker.write_u16(40525, 2, 'AutoCtrl: W-limit mode')
            worker.write_i32(40527, limit_w, f'AutoCtrl: limit {limit_w} W')
            pv_part = f'PV limited to {limit_w} W'
            mode    = 'export_limited'
        else:
            worker.write_u16(40525, 0, 'AutoCtrl: no PV limit')
            pv_part = 'PV unrestricted'
            mode    = 'export_unlimited'

        # ── 2. Grid charging ──────────────────────────────────────────────────
        if import_dkk < _CHEAP_IMPORT_DKK and batt_soc < _GRID_CHARGE_SOC_MAX:
            worker.write_u16(47087, 1, 'AutoCtrl: grid charge ON')
            worker.write_u32(47079, _GRID_CHARGE_W, f'AutoCtrl: grid {_GRID_CHARGE_W} W')
            worker.write_u16(47086, 4, 'AutoCtrl: mode=max self-consumption')
            batt_part = f', grid charging {_GRID_CHARGE_W} W (import {import_dkk:.3f} DKK, SoC {batt_soc:.0f}%)'
            mode = 'grid_charge'
        else:
            worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
            worker.write_u16(47086, 4, 'AutoCtrl: mode=max self-consumption')
            batt_part = f' (export {export_dkk:.3f} DKK, import {import_dkk:.3f} DKK)'

        return _Cmd(mode=mode, detail=pv_part + batt_part)
