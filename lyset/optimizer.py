"""
Battery schedule optimiser — picks the cheapest sequence of controller modes over
the forecast horizon by dynamic programming.

Modes are exactly what the controller can command: SC (native max
self-consumption), HOLD (forced idle) and CHARGE (forced charge at full rate, from
PV surplus plus grid). Each slot's cost is the grid import × import price minus
export × export price under that mode's battery physics; a fixed penalty per mode
change trades a little money for fewer commands to the fragile SDongle. Stored
energy left at the horizon is valued at the horizon's mean import price.

Replayed over 2026-08-30..10-13 with realistic inputs (load from the walk-forward
consumption model, stored Solcast forecasts, actual prices; decisions executed
against ACTUAL load/PV): 552 DKK at penalty 0.5 vs 582 DKK for the rule-based
controller with grid charging off, at 1.7 vs 0.7 mode changes per day
(penalty 0.2: 540 DKK, 3.1/day; 1.0: 560 DKK, 0.8/day).
"""
from __future__ import annotations

import numpy as np

SC, HOLD, CHARGE = 0, 1, 2
_N_MODES = 3
_SOC_STEPS = 181          # 0.5 %-point grid between min_soc and 100 %
_SLOT_H = 0.25


def _step(soc: np.ndarray, mode: int, pv_kw: float, load_kw: float, ev_kw: float,
          imp: float, exp: float, cap: float, min_soc: float, pmax: float,
          chg_eff: float, dis_eff: float) -> tuple[np.ndarray, np.ndarray]:
    """One 15-min slot for an array of starting SoCs → (next SoC, cost DKK).
    The battery serves the house load only (the EV lock leaves EV charging to the
    grid); at a negative export price surplus is curtailed, never sold."""
    head  = np.maximum((100.0 - soc) / 100.0 * cap / chg_eff / _SLOT_H, 0.0)
    avail = np.maximum((soc - min_soc) / 100.0 * cap * dis_eff / _SLOT_H, 0.0)
    net = pv_kw - load_kw
    if mode == SC:
        b = np.minimum(min(net, pmax), head) if net >= 0 else np.maximum(max(net, -pmax), -avail)
    elif mode == HOLD:
        b = np.zeros_like(soc)
    else:
        b = np.minimum(pmax, head)
    grid = load_kw + ev_kw - pv_kw + b
    if exp < 0:
        grid = np.maximum(grid, 0.0)
    stored = b * _SLOT_H * np.where(b > 0, chg_eff, 1.0 / dis_eff)
    soc2 = np.clip(soc + stored / cap * 100.0, min_soc, 100.0)
    cost = np.where(grid > 0, grid * imp, grid * exp) * _SLOT_H
    return soc2, cost


def plan(soc: float, prev_mode: int, slots: list[dict], cap_kwh: float,
         switch_penalty: float, min_soc: float, pmax_kw: float,
         chg_eff: float, dis_eff: float) -> list[int]:
    """
    Optimal mode for every slot in `slots` (15-min, ascending; keys pv_w, load_w,
    ev_w, import, export), starting from `soc` with the battery currently in
    `prev_mode`. Returns [] when there is nothing to plan.
    """
    K = len(slots)
    if K == 0 or cap_kwh <= 0:
        return []
    grid_s = np.linspace(min_soc, 100.0, _SOC_STEPS)
    term = float(np.mean([s['import'] for s in slots])) * dis_eff
    V = np.tile(-(grid_s - min_soc) / 100.0 * cap_kwh * dis_eff * term, (_N_MODES, 1))
    policy = np.empty((K, _N_MODES, _SOC_STEPS), dtype=np.int8)
    switch = switch_penalty * (np.arange(_N_MODES)[:, None] != np.arange(_N_MODES)[None, :])
    trans = []
    for k in range(K - 1, -1, -1):
        s = slots[k]
        Q = np.empty((_N_MODES, _SOC_STEPS))
        step_k = []
        for m in range(_N_MODES):
            s2, cost = _step(grid_s, m, s['pv_w'] / 1000.0, s['load_w'] / 1000.0,
                             s['ev_w'] / 1000.0, s['import'], s['export'],
                             cap_kwh, min_soc, pmax_kw, chg_eff, dis_eff)
            Q[m] = cost + np.interp(s2, grid_s, V[m])
            step_k.append(s2)
        trans.append(step_k)
        tot = Q[None, :, :] + switch[:, :, None]          # [prev_mode, mode, soc]
        policy[k] = tot.argmin(axis=1)
        V = tot.min(axis=1)
    trans.reverse()

    modes, cur, pm = [], float(np.clip(soc, min_soc, 100.0)), prev_mode
    for k in range(K):
        j = int(np.argmin(np.abs(grid_s - cur)))
        m = int(policy[k, pm, j])
        modes.append(m)
        cur = float(np.interp(cur, grid_s, trans[k][m]))
        pm = m
    return modes
