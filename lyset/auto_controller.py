"""
AutoController — periodic optimizer for solar/battery control.

Runs every 15 s while enabled and picks ONE action per tick, in priority order:

0. EXPORT LIMITATION (per-branch) — register 47415.
   Each branch sets Active Power Control once before returning. Set to mode 5
   ("zero-power grid connection" = FusionSolar's "Zero Export Limitation") when we
   must not export — a negative price (curtail PV surplus) or self-consumption (so
   the inverter covers the load from the battery and never exports). Lifted
   (mode 0) when we want the grid: arbitrage discharge and surplus PV export at a
   good price. Mode 5 is what the user verified by hand in FusionSolar.

1. NEGATIVE EXPORT (export < −0.01 DKK/kWh)
   Force-charge the battery (47086=1, 47247, 47100=1) to soak up the PV surplus.
   Combined with the export-limit overlay above this guarantees zero export: the
   battery absorbs up to its max rate and mode 5 curtails the rest. The forced
   charge is belt-and-suspenders — even if this firmware ignored 47415 the
   battery still soaks surplus up to its max rate. Battery full → force-idle.

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

5. DEFAULT — self-consumption
   SURPLUS (PV > load): force-charge from the surplus only — mode 4 dumps a
   near-full battery to grid during a surplus on this firmware, so a forced charge
   is used instead; excess PV exports (cap lifted) or is curtailed at a negative
   price. DEFICIT (PV ≤ load): max self-consumption with zero export — mode 4
   covers the load natively (no fixed setpoint) AND Active Power Control mode 5
   ("zero export limitation") so the inverter drives the battery to cover the load
   and never exports. Grid charge held OFF (47087=0) and any forced command cleared
   (47100=0), read-back driven. The discharge ceiling (47077) is raised so it can
   cover the whole load, not a trickle. This is the combination the user confirmed
   by hand in FusionSolar.

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
from datetime import datetime
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo
    _TZ_LOCAL = ZoneInfo('Europe/Copenhagen')
except Exception:  # pragma: no cover — fallback if tz database is unavailable
    _TZ_LOCAL = None

log = logging.getLogger(__name__)

# ── Thresholds (also imported by server.py for the SoC simulation) ────────────
NEGATIVE_EXPORT_DKK   = 0.0     # export below 0 → curtail PV feed-in and force-charge
                                # (any negative export means we'd PAY to export — never
                                # do it; the −0.007 DKK case slipped a −0.01 dead-band)
MIN_PV_W              = 300     # below this, don't bother writing a PV limit
CHEAP_IMPORT_DKK      = 0.50    # legacy constant — kept for _simulate_soc in server.py
GRID_CHARGE_SOC_START = 75.0    # only begin grid charging below this (hysteresis low)
GRID_CHARGE_SOC_MAX   = 80.0    # stop grid charging above this (hysteresis high)
GRID_CHARGE_W         = 2500    # W — grid charge rate
FORCE_CHARGE_SOC_MAX  = 95.0    # above this, force-idle instead of force-charge
MAX_FORCE_CHARGE_W    = 5000    # W — legacy; inverter clamps to rated max
BATT_MAX_CHARGE_W     = 2500    # W — LUNA2000-5kWh physical max charge rate (C/2)
BATT_MAX_DISCHARGE_W  = 2500    # W — full discharge rate; asserted on 47077 so mode 4
                                # self-consumption isn't pinned to a low max-discharge limit

# Active Power Control (register 47415) — grid export limitation.
# Verified against wlcrs/huawei-solar-lib: ActivePowerControlMode enum.
EXPORT_LIMIT_MODE_UNLIMITED = 0   # normal: grid used freely (charge/arbitrage OK)
EXPORT_LIMIT_MODE_ZERO      = 5   # "zero-power grid connection" (no 47416 needed)
EXPORT_LIMIT_MODE_WATT      = 6   # "power-limited grid connection (W)": cap net
                                  # feed-in to the watts in 47416. With 47416=0 this
                                  # is FusionSolar's "Grid connection with limited
                                  # power" = 0 kW, the dynamic-curtailment path
                                  # IntelliCharge uses. Inverter curtails surplus PV
                                  # beyond the battery's charge rate and never exports.
EXPORT_LIMIT_FEED_W         = 0   # W — feed-in cap applied while limiting (0 = none)

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
# Solar-aware hold suppression: don't hoard charge for a peak that the sun will
# refill the battery for anyway. If forecast PV energy before the peak is at least
# this multiple of the battery's capacity, skip the hold and discharge now —
# solar will top the pack back up before the expensive slot arrives.
HOLD_SOLAR_REFILL_FACTOR = 1.0

_EVAL_INTERVAL_S = 15

# Private aliases for internal use
_NEGATIVE_EXPORT_DKK  = NEGATIVE_EXPORT_DKK
_MIN_PV_W             = MIN_PV_W
_GRID_CHARGE_SOC_START = GRID_CHARGE_SOC_START
_GRID_CHARGE_SOC_MAX  = GRID_CHARGE_SOC_MAX
_GRID_CHARGE_W        = GRID_CHARGE_W
_FORCE_CHARGE_SOC_MAX = FORCE_CHARGE_SOC_MAX
_MAX_FORCE_CHARGE_W   = MAX_FORCE_CHARGE_W
_BATT_MAX_CHARGE_W    = BATT_MAX_CHARGE_W
_BATT_MAX_DISCHARGE_W = BATT_MAX_DISCHARGE_W
_EXPORT_LIMIT_MODE_UNLIMITED = EXPORT_LIMIT_MODE_UNLIMITED
_EXPORT_LIMIT_MODE_ZERO      = EXPORT_LIMIT_MODE_ZERO
_EXPORT_LIMIT_MODE_WATT      = EXPORT_LIMIT_MODE_WATT
_EXPORT_LIMIT_FEED_W         = EXPORT_LIMIT_FEED_W
_ARBIT_MARGIN_DKK     = ARBIT_MARGIN_DKK
_MIN_SOC_ARBIT        = MIN_SOC_ARBIT
_ARBIT_HORIZON_H      = ARBIT_HORIZON_H
_CHARGE_MARGIN_DKK    = CHARGE_MARGIN_DKK
_CHARGE_HORIZON_H     = CHARGE_HORIZON_H
_HOLD_DELTA_DKK       = HOLD_DELTA_DKK
_HOLD_HORIZON_H       = HOLD_HORIZON_H
_MAX_HOLD_IMPORT_DKK  = MAX_HOLD_IMPORT_DKK
_MIN_SOC_HOLD         = MIN_SOC_HOLD
_HOLD_SOLAR_REFILL_FACTOR = HOLD_SOLAR_REFILL_FACTOR


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
    # recovers if the inverter state drifts (e.g. manual change, reconnect). The
    # read-back-gated writes self-heal every tick on their own; this blind
    # re-assert only matters for registers that don't read back (e.g. 47247 forced
    # charge power), so it can be infrequent — keeping it low just floods the
    # single-client SDongle with redundant writes.
    _RESYNC_EVERY = 60  # ticks (60 × 15 s = 15 min)

    def __init__(self):
        self.enabled:         bool            = False
        self.last_action:     str             = '—'
        self.last_action_ts:  Optional[float] = None
        self._on_command:     Optional[Callable[[str, str], None]] = None
        self._grid_charging:  bool            = False  # hysteresis state
        self._applied:        dict[int, int]  = {}     # addr → last value written
        self._tick:           int             = 0
        self._solar_fc:       list[dict]      = []     # latest Solcast forecast

    def set_command_callback(self, cb: Callable[[str, str], None]):
        self._on_command = cb

    def set_solar_forecast(self, fc: list[dict]):
        """Latest Solcast forecast ([{ts_ms, pv_w}, ...], 30-min period_end UTC).
        Used by the hold branch to tell whether the sun will refill the battery
        before an upcoming price peak."""
        self._solar_fc = fc or []

    def _expected_solar_kwh(self, from_ms: int, to_ms: int) -> float:
        """Forecast PV energy (kWh) over (from_ms, to_ms] from the latest solar
        forecast (30-min period_end records)."""
        if to_ms <= from_ms:
            return 0.0
        total = 0.0
        for r in self._solar_fc:
            ts = r.get('ts_ms')
            if ts is None or ts <= from_ms or ts > to_ms:
                continue
            total += (r.get('pv_w') or 0.0) / 1000.0 * 0.5  # 30-min slot → kWh
        return total

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

    def _set_export_limit(self, worker, zero_export: bool, data: dict):
        """
        Grid export limitation via Active Power Control (register 47415).

        zero_export=True  → mode 5 ("zero-power grid connection" = FusionSolar's
          "Zero Export Limitation"). The inverter covers the load from PV + battery
          and never exports: it refuses to discharge the battery to grid and curtails
          any PV surplus beyond what the battery + load absorb. The user verified this
          mode does exactly the right thing on this firmware by setting it manually in
          FusionSolar — so we set the same register here. (Earlier we used mode 6 +
          47416=0 on the assumption mode 5 might not be honoured; it is, and mode 5 is
          simpler — no feed-in cap register needed.)
        zero_export=False → mode 0 ("unlimited"): normal operation. Required so
          grid charge and arbitrage discharge can use the grid, and so surplus PV
          can export at a good price.

        DRIVEN OFF THE LIVE READ-BACK (47415 is polled) WHEN IT READS BACK, else
        the optimistic _applied cache. When the inverter reports its mode we compare
        and re-issue the write every tick until it confirms, then stop — self-healing
        without flooding. But on firmware where 47415 does NOT read back
        (active_power_mode polls as None, logged 'apc=?'), `None != target` is always
        true, so the old code re-wrote it EVERY tick forever — flooding the
        single-client SDongle. In that case fall back to the optimistic _apply cache:
        write once, then re-assert only on the periodic resync.
        """
        target = _EXPORT_LIMIT_MODE_ZERO if zero_export else _EXPORT_LIMIT_MODE_UNLIMITED
        label  = 'zero export limitation' if zero_export else 'unlimited'
        desc   = f'AutoCtrl: active power ctrl mode={target} ({label})'

        cur_mode = data.get('active_power_mode')
        cur_mode = int(round(cur_mode)) if cur_mode is not None else None

        if cur_mode is None:
            # 47415 doesn't read back — write once and let the resync re-assert,
            # never blindly every tick.
            self._apply(worker, [(16, 47415, target, desc)])
        else:
            if cur_mode != target:
                worker.write_u16(47415, target, desc)
            # Keep the cache coherent so a resync clear won't needlessly re-write a
            # register the inverter already confirms is correct.
            self._applied[47415] = target

    def _charge_from_surplus(self, worker, surplus_w: float, batt_soc: float) -> str:
        """
        Force-charge the battery from the PV surplus (never from grid), or idle it
        when full / when there is no surplus. Returns a human-readable detail string.

        This is the safe replacement for mode 4 during a SURPLUS. In forced-charge
        mode (47086=1, 47100=1) the battery physically cannot discharge to grid —
        which is the whole point: this firmware's mode 4 dumps a near-full battery
        straight to grid during a solar surplus (observed SoC 97 %, batt −2500 W to
        grid, at a negative price). Forcing a charge preserves the stored energy for
        the evening import peak / a high export price, while still letting the battery
        soak up surplus it has room for. Excess PV beyond the battery's max charge
        rate exports normally, or is curtailed by the export-limit overlay when the
        export price is negative.

        Charge power tracks the surplus (capped to the battery's max rate) so we
        never pull from the grid, quantised to 100 W so small PV jitter doesn't
        rewrite 47247 every tick.
        """
        if batt_soc < _FORCE_CHARGE_SOC_MAX and surplus_w > 100:
            charge_w = min(int(round(surplus_w / 100.0)) * 100, _BATT_MAX_CHARGE_W)
            self._apply(worker, [
                (16, 47087, 0,        'AutoCtrl: grid charge OFF'),
                (16, 47086, 1,        'AutoCtrl: mode=forced'),
                (32, 47247, charge_w, f'AutoCtrl: forced charge power {charge_w} W'),
                (16, 47100, 1,        'AutoCtrl: force CHARGE'),
            ])
            return f'charging {charge_w} W from surplus'
        self._apply(worker, [
            (16, 47087, 0, 'AutoCtrl: grid charge OFF'),
            (16, 47086, 1, 'AutoCtrl: mode=forced'),
            (16, 47100, 0, 'AutoCtrl: force IDLE'),
        ])
        reason = 'full' if batt_soc >= _FORCE_CHARGE_SOC_MAX else 'no surplus'
        return f'idle ({reason})'

    def _set_self_consumption(self, worker, data: dict):
        """
        Put the battery in true max self-consumption: cover the house load from the
        battery natively (no fixed power setpoint), targeting grid ≈ 0. Grid charge
        is held OFF (no import to charge) and any forced command is cleared (so a
        stuck force-discharge can't dump the battery to grid — the real cause of the
        earlier −2500 W "dump").

        DRIVEN OFF THE LIVE READ-BACK (47086/47087/47100 are polled), NOT the
        optimistic _applied cache. The SDongle drops writes intermittently; the cache
        would mark a failed write done and leave a stale state active — exactly what
        let mode 4 keep grid-charging at +2200 W into a 3.6 DKK peak (the
        grid-charge-disable write had dropped). Comparing against the polled value we
        re-issue only what's wrong, every tick, until the inverter confirms — at most
        a few writes, self-healing, no flooding.

        47087=0 grid charge OFF (no import to charge), 47100=0 clear any forced
        command, 47086=4 max self-consumption.
        """
        # Keep the optimistic _apply cache (used by the forced branches, which share
        # these registers) coherent with the intended state, so a later forced branch
        # never skips a write believing the register is still forced.
        self._applied[47087] = 0
        self._applied[47100] = 0
        self._applied[47086] = 4

        gce = data.get('grid_charge_enable')
        if gce is not None and int(round(gce)) != 0:
            worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
        fm = data.get('batt_forced_mode')
        if fm is not None and int(round(fm)) != 0:
            worker.write_u16(47100, 0, 'AutoCtrl: clear forced command')
        wm = data.get('batt_working_mode')
        if wm is not None and int(round(wm)) != 4:
            worker.write_u16(47086, 4, 'AutoCtrl: mode=max self-consumption')

    def _ensure_power_limits(self, worker, data: dict):
        """
        Keep the battery's max charge / max discharge power (47075 / 47077) at the
        full rate. These are GLOBAL limits that cap the battery in every mode — a low
        max-discharge-power pins mode 4 self-consumption to a trickle (observed:
        −400 W discharge while the deficit was 600 W, ~215 W imported). The forced
        branches used to set them only when forcing; asserting them here makes them
        right in mode 4 too.

        Read-back driven where the SDongle exposes the registers (re-issue only when
        the polled value is wrong); optimistic fallback (state-gated _apply) when they
        don't read back, so they're still set at least once per resync.
        """
        for addr, key, target in (
            (47075, 'max_charge_power',    _BATT_MAX_CHARGE_W),
            (47077, 'max_discharge_power', _BATT_MAX_DISCHARGE_W),
        ):
            cur = data.get(key)
            if cur is None:
                self._apply(worker, [(32, addr, target, f'AutoCtrl: {key}={target} W')])
            elif int(round(cur)) != target:
                worker.write_u32(addr, target, f'AutoCtrl: {key}={target} W')
                self._applied[addr] = target

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
        worker.write_u16(47415, _EXPORT_LIMIT_MODE_UNLIMITED,
                         'AutoCtrl OFF: export limit unlimited')

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
        # house_load = active_power + meter − batt_power: during a fast battery
        # charge↔discharge transition the batched reads are captured a few ms apart
        # and this briefly computes a physically impossible value (seen at −4084 W).
        # Clamp to ≥0 so a glitch can never masquerade as a chargeable PV surplus.
        house_load = max(0.0, data.get('house_load') or 0)
        batt_soc   = data.get('batt_soc')   or 50.0
        grid_w     = data.get('meter_active_power') or 0.0  # +import / -export
        batt_w     = data.get('batt_power') or 0.0           # +charge / -discharge

        # Active price slot in Danish local time — lets us verify the controller is
        # matching the correct hour (now_ms and price ts are both UTC epoch ms).
        try:
            slot_dt = datetime.fromtimestamp(cur_price['ts'] / 1000, _TZ_LOCAL)
            slot_lbl = slot_dt.strftime('%a %H:%M')
        except Exception:
            slot_lbl = '?'

        # Live Active Power Control read-back (47415 mode + 47416 feed cap) — confirms
        # whether the inverter actually accepted the export-limit write (mode 6,
        # feed 0 W = limiting). apc=6/feed=0 while still exporting → firmware ignores
        # it; apc unchanged → write rejected.
        apc_mode = data.get('active_power_mode')
        apc_lbl  = f'{int(apc_mode)}' if apc_mode is not None else '?'
        feed_w   = data.get('max_feed_grid_w')
        feed_lbl = f'{feed_w:.0f}' if feed_w is not None else '?'
        # Max discharge limit read-back — if this is low (e.g. 400 W) it caps mode 4
        # self-consumption no matter what; '?' means the SDongle won't read it back.
        dis_lim  = data.get('max_discharge_power')
        dis_lbl  = f'{dis_lim:.0f}' if dis_lim is not None else '?'

        log.info(
            'AutoCtrl: slot=%s  import=%.3f  export=%.3f  PV=%.0fW  grid=%+.0fW  load=%.0fW  SoC=%.1f%%  batt=%+.0fW  apc=%s  feed=%sW  disLim=%sW',
            slot_lbl, import_dkk, export_dkk, pv_w, grid_w, house_load, batt_soc, batt_w, apc_lbl, feed_lbl, dis_lbl,
        )

        # Periodically clear the applied-state cache so the next _apply() re-asserts
        # every register, recovering from any external drift in the inverter state.
        self._tick += 1
        if self._tick % self._RESYNC_EVERY == 0:
            self._applied.clear()

        # Keep the battery's max charge/discharge power at the full rate in every mode
        # (a low max-discharge-power pins mode 4 self-consumption to a trickle).
        self._ensure_power_limits(worker, data)

        # Grid export limitation (47415) is decided PER BRANCH below, each calling
        # _set_export_limit exactly once before returning — so at most one export
        # write per tick (no flooding) and each regime controls its own policy:
        # cap feed-in to 0 W when we must not export (negative price, or
        # self-consumption where mode 4 would otherwise dump the battery to grid),
        # lift it when we want to export (arbitrage, surplus PV at a good price).

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

        # ── 1. Negative export → force-charge from the SURPLUS only ───────────
        # At a negative export price we must not feed the grid. The export-limit
        # overlay above curtails surplus PV beyond the battery, and here we force a
        # charge so the battery soaks up what it can and can never discharge to grid.
        # Charge power tracks the surplus (never imports); full / no surplus → idle.
        if export_dkk < _NEGATIVE_EXPORT_DKK:
            self._grid_charging = False
            self._set_export_limit(worker, True, data)   # cap feed-in to 0 W
            batt_detail = self._charge_from_surplus(worker, surplus_w, batt_soc)
            detail = (f'Negative export {export_dkk:.3f} DKK — battery {batt_detail} '
                      f'(SoC {batt_soc:.0f}%, PV {pv_w:.0f} W, load {house_load:.0f} W)')
            return _Cmd(mode='export_limited', detail=detail)

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
                self._set_export_limit(worker, False, data)   # grid used freely
                self._apply(worker, [
                    (16, 47087, 0,              'AutoCtrl: grid charge feature OFF'),
                    (16, 47086, 1,              'AutoCtrl: mode=forced'),
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
            peak_p       = max(hold_window, key=lambda p: p['import'])
            max_upcoming = peak_p['import']
            # Solar-aware suppression: if enough PV is forecast before the peak to
            # recharge the pack, don't hoard — discharge now and let the sun refill
            # the battery before the expensive slot. (Refilled charge is free; the
            # cheap import we'd otherwise pay overnight is pure loss.)
            cap_kwh   = data.get('batt_rated_capacity') or 5.0
            solar_kwh = self._expected_solar_kwh(now_ms, peak_p['ts'])
            solar_will_refill = solar_kwh >= cap_kwh * _HOLD_SOLAR_REFILL_FACTOR
            if max_upcoming > import_dkk + _HOLD_DELTA_DKK and not solar_will_refill:
                self._grid_charging = False
                self._set_export_limit(worker, False, data)   # battery idle, no export
                self._apply(worker, [
                    (16, 47087, 0, 'AutoCtrl: grid charge OFF'),
                    (16, 47086, 1, 'AutoCtrl: mode=forced'),
                    (16, 47100, 0, 'AutoCtrl: force IDLE'),
                ])
                detail = (f'Holding battery: peak {max_upcoming:.3f} DKK in ≤{_HOLD_HORIZON_H}h '
                          f'(now {import_dkk:.3f} DKK, SoC {batt_soc:.0f}%, '
                          f'solar before peak {solar_kwh:.1f}/{cap_kwh:.1f} kWh)')
                return _Cmd(mode='hold_battery', detail=detail)
            if max_upcoming > import_dkk + _HOLD_DELTA_DKK and solar_will_refill:
                log.info(
                    'AutoCtrl: hold suppressed — %.1f kWh PV forecast before peak '
                    '%.3f DKK refills the %.1f kWh pack; discharging instead',
                    solar_kwh, max_upcoming, cap_kwh,
                )

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
                self._set_export_limit(worker, False, data)   # MUST export to grid
                self._apply(worker, [
                    (16, 47087, 0,              'AutoCtrl: grid charge OFF'),
                    (16, 47086, 1,              'AutoCtrl: mode=forced'),
                    (32, 47247, _GRID_CHARGE_W, f'AutoCtrl: forced discharge power {_GRID_CHARGE_W} W'),
                    (16, 47100, 2,              'AutoCtrl: force DISCHARGE'),
                ])
                detail = (f'Arb. discharge: export {export_dkk:.3f} DKK > '
                          f'future min import {future_min_import:.3f} DKK (SoC {batt_soc:.0f}%)')
                return _Cmd(mode='arbit_discharge', detail=detail)

        # ── 5. Default: self-consumption ──────────────────────────────────────
        # SURPLUS (PV meaningfully exceeds load): force-charge from the surplus only
        # — the battery stores what it can and cannot be dumped to grid; excess PV
        # exports at the (non-negative) price, so the export cap is lifted. Gated on
        # real PV (> MIN_PV_W) so a glitched house_load can't fake a surplus and
        # trigger a grid charge. Mode 4 isn't used here because it dumps a near-full
        # battery to grid during a surplus on this firmware.
        #
        # DEFICIT (PV ≤ load): max self-consumption with zero export — mode 4 covers
        # the load natively (no fixed setpoint), plus Active Power Control mode 5
        # ("zero export limitation", set via _set_export_limit) so the inverter drives
        # the battery to cover the load and never exports. This is the exact mode the
        # user confirmed works by hand in FusionSolar. The raised discharge ceiling
        # (47077, via _ensure_power_limits) lets it cover the whole load, not a trickle.
        self._grid_charging = False
        if surplus_w > 100 and pv_w > _MIN_PV_W:
            self._set_export_limit(worker, False, data)   # let excess PV export
            batt_detail = self._charge_from_surplus(worker, surplus_w, batt_soc)
            detail = (f'Self-consumption: battery {batt_detail} '
                      f'(PV {pv_w:.0f} W, load {house_load:.0f} W, '
                      f'surplus {surplus_w:+.0f} W, SoC {batt_soc:.1f}%)')
            return _Cmd(mode='export_unlimited', detail=detail)
        self._set_export_limit(worker, True, data)        # mode 5: zero export limitation
        self._set_self_consumption(worker, data)
        detail = (f'Self-consumption: zero-export, battery covers load (mode 4 + APC 5) '
                  f'(PV {pv_w:.0f} W, load {house_load:.0f} W, '
                  f'deficit {surplus_w:+.0f} W, SoC {batt_soc:.1f}%)')
        return _Cmd(mode='sc_discharge', detail=detail)
