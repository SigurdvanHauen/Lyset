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
   Soak the PV surplus into the battery (via _absorb_surplus) with the export cap
   (mode 5) curtailing anything beyond the battery's rate, so nothing is sold at a
   negative price. Below 90% SoC this is native self-consumption (47086=2; instant,
   full-rate); near full it's the forced charge (47086=1, 47247, 47100=1), which
   can't discharge to grid. Battery full → force-idle.

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
   Discharge to grid (47086=1 forced mode, 47100=2 force discharge, 47249=2500 W
   re-asserted every tick): sell stored energy high, rebuy cheaper later. Grid
   charge blocked by 47087=0 so the battery can only discharge.

5. DEFAULT — self-consumption
   SURPLUS (PV > load): absorb the surplus (via _absorb_surplus) — native
   self-consumption (47086=2) below 90% SoC (the hardware charges from 100% of the
   surplus with no controller lag), falling back to a forced charge only near full
   (belt-and-braces; see NATIVE_SC_SOC_MAX); excess PV exports (cap lifted).
   DEFICIT (PV ≤ load): max self-consumption with zero export — mode 2
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
      47247 forcible CHARGE power (setpoint for 47100=1 charge),
      47249 forcible DISCHARGE power (setpoint for 47100=2 discharge). 47247 and
      47249 are DISTINCT — writing 47247 does NOT set the discharge rate; the
      discharge setpoint 47249 must be written or the inverter defaults to ~1634 W.
  • 47086 (working mode, U16) uses StorageWorkingModesC (verified against
      wlcrs/huawei-solar-lib): 0=adaptive, 1=fixed/forced charge-discharge,
      2=MAXIMISE SELF-CONSUMPTION, 3=TOU(LG), 4=FULLY FED TO GRID, 5=TOU(LUNA2000).
      NEVER write 4 here: this code originally did (using the enum of the read-only
      status register 37006, where 4 *is* self-consumption) and the inverter —
      correctly, per "fully fed to grid" — dumped the battery to grid at 2500 W at
      any SoC (observed at 9%, 30%, 98%) whenever the export cap was lifted.
  • Other forced control is U16: 47087 grid-charge enable,
      47100 forced command (0=stop,1=charge,2=discharge).

On disable → restore: 47100=0, 47087=0, 47086=2 (self-consumption).
"""
import asyncio
import logging
import os
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


def arbitrage_enabled() -> bool:
    """Whether the planner may use export/discharge arbitrage (sell stored energy
    high, rebuy cheaper). Toggled from Settings → Auto controller; default ON.
    Read live from the env each call so the Settings save (which reloads ``.env``
    into the process env) takes effect without a restart. Every other battery
    strategy — grid charge, hold, self-consumption — is unaffected."""
    return os.getenv('ARBITRAGE_ENABLED', '1').strip().lower() not in ('0', 'false', 'no', 'off', '')


def arbitrage_min_gain() -> float:
    """Minimum predicted round-trip gain (as a fraction — 0.05 = 5 %) required
    before the planner will discharge the battery to the grid for arbitrage.

    The gain is measured against the OPPORTUNITY COST of the stored energy: the
    most expensive upcoming import that same energy could otherwise offset via
    self-consumption. Requiring export_now ≥ opportunity_cost × (1 + gain) means
    the trade can never lose money to self-consumption — because import always
    exceeds export for a given hour, dumping the pack into an evening peak and
    re-importing the house load at a higher price is exactly what this blocks.
    Settings → Auto controller; read live so a save applies without a restart."""
    try:
        pct = float(os.getenv('ARBITRAGE_MIN_GAIN_PCT', '5'))
    except (TypeError, ValueError):
        pct = 5.0
    return max(pct, 0.0) / 100.0

# ── Thresholds (also imported by server.py for the SoC simulation) ────────────
NEGATIVE_EXPORT_DKK   = 0.0     # export below 0 → curtail PV feed-in and force-charge
                                # (any negative export means we'd PAY to export — never
                                # do it; the −0.007 DKK case slipped a −0.01 dead-band)
EXPORT_RESUME_DKK     = 0.03    # ASYMMETRIC hysteresis (not a symmetric dead-band): we
                                # still curtail the instant export < 0 (never sell at a
                                # loss), but once curtailing we stay curtailed until the
                                # export price is clearly positive (> this). Stops the
                                # curtail decision — and the physical APC export-limit
                                # write — from chattering every slot when the export price
                                # hovers around 0 (the picket-fence export plan).
MIN_PV_W              = 300     # below this, don't bother writing a PV limit
CHEAP_IMPORT_DKK      = 0.50    # legacy constant — kept for _simulate_soc in server.py
GRID_CHARGE_SOC_START = 75.0    # only begin grid charging below this (hysteresis low)
GRID_CHARGE_SOC_MAX   = 80.0    # stop grid charging above this (hysteresis high)
GRID_CHARGE_W         = 2500    # W — grid charge rate
FORCE_CHARGE_SOC_MAX  = 95.0    # above this, force-idle instead of force-charge
NATIVE_SC_SOC_MAX     = 90.0    # below this SoC, charge a PV surplus with Huawei's native
                                # Maximum Self-Consumption (47086=2) — a sub-second hardware
                                # loop that soaks 100% of the surplus up to the battery's
                                # physical rate, with none of the 15 s force-charge lag or
                                # 100 W quantisation. The near-full "dump to grid" that this
                                # gate originally worked around turned out to be the 47086=4
                                # enum bug (4 = fully fed to grid, not self-consumption), so
                                # true mode 2 is probably safe at any SoC — but its near-full
                                # behaviour is unverified on this firmware, so the gate stays
                                # as belt-and-braces: above it, the forced-charge fallback.
MAX_FORCE_CHARGE_W    = 5000    # W — legacy; inverter clamps to rated max
BATT_MAX_CHARGE_W     = 2500    # W — LUNA2000-5kWh physical max charge rate (C/2)
BATT_MAX_DISCHARGE_W  = 2500    # W — full discharge rate; asserted on 47077 so native
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
ARBIT_MIN_EXCESS_KWH = 0.30  # only discharge to grid when at least this much stored
                             # energy is surplus to upcoming self-consumption needs
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
_SC_DEBOUNCE_TICKS = 3   # ticks (45 s) the surplus/deficit sign must persist before
                         # the default self-consumption branch switches sides

# Private aliases for internal use
_NEGATIVE_EXPORT_DKK  = NEGATIVE_EXPORT_DKK
_EXPORT_RESUME_DKK    = EXPORT_RESUME_DKK
_MIN_PV_W             = MIN_PV_W
_GRID_CHARGE_SOC_START = GRID_CHARGE_SOC_START
_GRID_CHARGE_SOC_MAX  = GRID_CHARGE_SOC_MAX
_GRID_CHARGE_W        = GRID_CHARGE_W
_FORCE_CHARGE_SOC_MAX = FORCE_CHARGE_SOC_MAX
_NATIVE_SC_SOC_MAX    = NATIVE_SC_SOC_MAX
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
_ARBIT_MIN_EXCESS_KWH = ARBIT_MIN_EXCESS_KWH
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
        self._export_curtailed: bool          = False  # zero-export hysteresis state
        self._last_soc:       Optional[float] = None   # last real SoC read (37760
                                                       # drops out on ~10% of polls)
        self._sc_surplus:     bool            = False  # debounced branch-5 state
        self._sc_streak:      int             = 0      # ticks disagreeing with it
        self._applied:        dict[int, int]  = {}     # addr → last value written
        self._tick:           int             = 0
        self._solar_fc:       list[dict]      = []     # latest Solcast forecast
        self._load_fc:        list[dict]      = []     # latest consumption forecast

    def set_command_callback(self, cb: Callable[[str, str], None]):
        self._on_command = cb

    def set_solar_forecast(self, fc: list[dict]):
        """Latest Solcast forecast ([{ts_ms, pv_w}, ...], 30-min period_end UTC).
        Used by the hold branch to tell whether the sun will refill the battery
        before an upcoming price peak."""
        self._solar_fc = fc or []

    def set_consumption_forecast(self, fc: list[dict]):
        """Latest house-consumption forecast ([{ts_ms, w}, ...], 15-min slots).
        Used by the arbitrage branch to size how much stored energy is reserved
        for upcoming self-consumption vs. free to export now."""
        self._load_fc = fc or []

    @staticmethod
    def _price_at(prices: list, ts_ms: int) -> Optional[float]:
        """Import price (DKK/kWh) of the slot covering ts_ms — the latest price
        record at or before it. Prices aren't assumed sorted."""
        best = None
        for p in prices:
            t = p.get('ts')
            if t is not None and t <= ts_ms and (best is None or t > best['ts']):
                best = p
        return best.get('import') if best else None

    def _solar_at(self, ts_ms: int) -> float:
        """Forecast PV (W) nearest ts_ms (nearest-slot, robust to start/end
        stamping). 0 when no forecast."""
        best, best_d = 0.0, None
        for r in self._solar_fc:
            t = r.get('ts_ms')
            if t is None:
                continue
            d = abs(t - ts_ms)
            if best_d is None or d < best_d:
                best_d, best = d, (r.get('pv_w') or 0.0)
        return best

    def _reserve_energy_kwh(self, from_ms: int, to_ms: int, export_now: float,
                            prices: list) -> float:
        """
        Energy (kWh) the battery should keep for upcoming self-consumption that is
        worth MORE than exporting now — the opportunity cost of a discharge-to-grid.

        For each future consumption slot in (from_ms, to_ms] whose IMPORT price
        exceeds export_now, the battery's best use is to cover that slot's net
        deficit (load − forecast solar) rather than export now and rebuy then. Sum
        those deficits. Slots cheaper than export_now are NOT reserved: their load
        is better covered by rebuying cheap (or exporting now and letting solar
        refill the pack), which is exactly the arbitrage we want to allow.

        Conservative simplifications: per-slot deficits are summed without modelling
        inter-slot solar recharge (over-reserves slightly, never under-reserves),
        and the window is the arbitrage horizon, so a price peak further out than
        ARBIT_HORIZON_H is left to solar to cover (true in summer; in winter the
        rebuy-floor check still guards the trade).
        """
        if not self._load_fc:
            return 0.0
        reserve = 0.0
        for r in self._load_fc:
            ts = r.get('ts_ms')
            w  = r.get('w')
            if ts is None or w is None or ts <= from_ms or ts > to_ms:
                continue
            imp = self._price_at(prices, ts)
            if imp is None or imp <= export_now:
                continue
            deficit_w = max(0.0, w - self._solar_at(ts))
            reserve += deficit_w / 1000.0 * 0.25   # 15-min slot → kWh
        return reserve

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
        # Record the commanded state so the negative-export decision can apply
        # hysteresis (every branch calls this exactly once per tick, so it always
        # reflects reality).
        self._export_curtailed = zero_export

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

    def _charge_from_surplus(self, worker, surplus_w: float, batt_soc: float,
                             grid_w=None, batt_w=None) -> str:
        """
        Force-charge the battery from the PV surplus (never from grid), or idle it
        when full / when there is no surplus. Returns a human-readable detail string.

        This is the near-full replacement for native self-consumption during a
        SURPLUS. In forced-charge mode (47086=1, 47100=1) the battery physically
        cannot discharge to grid. (The "mode 4 dumps a near-full battery to grid"
        this originally guarded against — observed SoC 97 %, batt −2500 W to grid,
        at a negative price — was really the 47086=4 fully-fed-to-grid enum bug;
        kept because mode 2's near-full behaviour is unverified.) Forcing a charge
        preserves the stored energy for
        the evening import peak / a high export price, while still letting the battery
        soak up surplus it has room for. Excess PV beyond the battery's max charge
        rate exports normally, or is curtailed by the export-limit overlay when the
        export price is negative.

        Charge power is sized from METER FEEDBACK, not the computed pv−load surplus:
        the surplus actually reaching the grid tie is (current battery charge) −
        (current net import) = ``batt_w − grid_w`` (grid_w > 0 = importing). The raw
        PV/load reads glitch badly on this SDongle — adjacent polls swung PV
        1.6↔6.2 kW — and an open-loop pv−load surplus let a single glitched spike
        command a 2500 W force-charge that then stuck, pulling ~1957 W from the grid
        at 1.21 DKK/kWh while the real surplus was ~540 W. The battery + meter
        readings are physical and can't be inflated by a bad PV read, so sizing off
        them can never command more charge than the real surplus. It's also a
        deadbeat loop — grid settles to ≈0 in one tick regardless of the current
        setpoint. Falls back to the computed surplus only when a meter read is
        missing. Quantised to 100 W to damp jitter.

        47247 (forcible charge power) does NOT read back, so a dropped write can't be
        detected; the state-gated cache would then mark a stale HIGH setpoint as
        applied and leave the battery stuck importing (the failure above). So the
        setpoint is re-asserted EVERY tick, bypassing the cache — the same
        self-healing pattern used for 47249 in the arbitrage branch — rather than
        gated through _apply.
        """
        if grid_w is not None and batt_w is not None:
            real_surplus = batt_w - grid_w   # batt_w > 0 charging, grid_w > 0 importing
        else:
            real_surplus = surplus_w
        if batt_soc < _FORCE_CHARGE_SOC_MAX and real_surplus > 100:
            charge_w = min(int(round(real_surplus / 100.0)) * 100, _BATT_MAX_CHARGE_W)
            charge_w = max(charge_w, 0)
            self._apply(worker, [
                (16, 47087, 0, 'AutoCtrl: grid charge OFF'),
                (16, 47086, 1, 'AutoCtrl: mode=forced'),
            ])
            worker.write_u32(47247, charge_w, f'AutoCtrl: forced charge power {charge_w} W')
            self._applied[47247] = charge_w
            self._apply(worker, [
                (16, 47100, 1, 'AutoCtrl: force CHARGE'),
            ])
            return f'charging {charge_w} W from surplus'
        self._apply(worker, [
            (16, 47087, 0, 'AutoCtrl: grid charge OFF'),
            (16, 47086, 1, 'AutoCtrl: mode=forced'),
            (16, 47100, 0, 'AutoCtrl: force IDLE'),
        ])
        reason = 'full' if batt_soc >= _FORCE_CHARGE_SOC_MAX else 'no surplus'
        return f'idle ({reason})'

    def _absorb_surplus(self, worker, surplus_w: float, batt_soc: float, data: dict,
                        zero_export: bool, grid_w=None, batt_w=None) -> str:
        """
        Store a PV surplus in the battery, choosing HOW based on SoC.

        Below _NATIVE_SC_SOC_MAX: hand off to Huawei's native Maximum
        Self-Consumption (47086=2). It's a sub-second hardware control loop, so it
        charges from 100% of the instantaneous surplus up to the battery's physical
        rate with none of the forced-charge path's 15 s deadbeat lag or 100 W
        quantisation — this is the "free-running on the surplus" behaviour. The
        forced command is cleared, so the battery physically can't be commanded to
        discharge here either.

        At/above _NATIVE_SC_SOC_MAX: fall back to the meter-fed forced charge, which
        cannot discharge to grid — belt-and-braces while mode 2's near-full
        behaviour is unverified (see NATIVE_SC_SOC_MAX).

        The export-limit overlay is the caller's policy: zero_export=True curtails
        excess PV (negative price), False lets it export (good price).
        """
        self._set_export_limit(worker, zero_export, data)
        if batt_soc < _NATIVE_SC_SOC_MAX:
            # Native self-consumption — identical register writes to the deficit
            # path; the hardware decides charge vs. discharge from the live
            # PV/load balance.
            self._set_self_consumption(worker, data)
            return (f'self-consumption (mode 2, charges from surplus up to '
                    f'{_BATT_MAX_CHARGE_W} W)')
        return self._charge_from_surplus(worker, surplus_w, batt_soc, grid_w, batt_w)

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
        let self-consumption keep grid-charging at +2200 W into a 3.6 DKK peak (the
        grid-charge-disable write had dropped). Comparing against the polled value we
        re-issue only what's wrong, every tick, until the inverter confirms — at most
        a few writes, self-healing, no flooding.

        47087=0 grid charge OFF (no import to charge), 47100=0 clear any forced
        command, 47086=2 max self-consumption (StorageWorkingModesC — NOT 4, which
        is "fully fed to grid" and dumps the battery to grid at any SoC when the
        export cap is lifted; see the module docstring's register notes).
        """
        # Keep the optimistic _apply cache (used by the forced branches, which share
        # these registers) coherent with the intended state, so a later forced branch
        # never skips a write believing the register is still forced.
        # 47247 (forcible CHARGE power) and 47249 (forcible DISCHARGE power) are
        # cleared from cache so that re-entering a forced charge/discharge always
        # sends a fresh write — the inverter resets both to internal defaults
        # (~1634 W discharge observed) when leaving forced mode, and without this the
        # cache would suppress the 2500 W re-write, capping the rate.
        self._applied[47087] = 0
        self._applied[47100] = 0
        self._applied[47086] = 2
        self._applied.pop(47247, None)
        self._applied.pop(47249, None)

        gce = data.get('grid_charge_enable')
        if gce is not None and int(round(gce)) != 0:
            worker.write_u16(47087, 0, 'AutoCtrl: grid charge OFF')
        fm = data.get('batt_forced_mode')
        if fm is not None and int(round(fm)) != 0:
            worker.write_u16(47100, 0, 'AutoCtrl: clear forced command')
        wm = data.get('batt_working_mode')
        if wm is not None and int(round(wm)) != 2:
            worker.write_u16(47086, 2, 'AutoCtrl: mode=max self-consumption')

    def _ensure_power_limits(self, worker, data: dict):
        """
        Keep the battery's max charge / max discharge power (47075 / 47077) at the
        full rate. These are GLOBAL limits that cap the battery in every mode — a low
        max-discharge-power pins native self-consumption to a trickle (observed:
        −400 W discharge while the deficit was 600 W, ~215 W imported). The forced
        branches used to set them only when forcing; asserting them here makes them
        right in self-consumption too.

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
        self._sc_surplus = False
        self._sc_streak = 0
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
        worker.write_u16(47086, 2, 'AutoCtrl OFF: mode=max self-consumption')
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
        # house_load is validated and hold-filled at the read layer (sanitize.py):
        # negative skew samples are dropped and a fresh last-good value is
        # substituted. If it is STILL absent (no good reading for 90 s), skip the
        # tick — the old `or 0` fallback turned "unknown" into "load 0 W", which
        # manufactured a fake PV surplus and flipped the self-consumption branch.
        house_load = data.get('house_load')
        if house_load is None:
            log.info('AutoCtrl: no house_load reading — skipping tick')
            return None
        house_load = max(0.0, house_load)
        # SoC register 37760 sits in its own read batch and drops out on ~10% of
        # polls. A literal default here (the old `or 50.0`) fed fake data into real
        # decisions — arbitrage discharge fired at an actual SoC of 5% and grid
        # charge at an actual 100% (both refused by the BMS, but wrong branches).
        # Carry the last real reading instead; before the first one, skip the tick.
        batt_soc = data.get('batt_soc')
        if batt_soc is None:
            if self._last_soc is None:
                log.info('AutoCtrl: no SoC reading yet — skipping tick')
                return None
            batt_soc = self._last_soc
        else:
            self._last_soc = batt_soc
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
        # Max discharge limit read-back — if this is low (e.g. 400 W) it caps native
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
        # (a low max-discharge-power pins native self-consumption to a trickle).
        self._ensure_power_limits(worker, data)

        # Grid export limitation (47415) is decided PER BRANCH below, each calling
        # _set_export_limit exactly once before returning — so at most one export
        # write per tick (no flooding) and each regime controls its own policy:
        # cap feed-in to 0 W when we must not export (negative price, or a
        # self-consumption deficit where nothing should be sold), lift it when we
        # want to export (arbitrage, surplus PV at a good price).

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
        # Asymmetric hysteresis: curtail the instant export < 0, but once curtailing
        # keep curtailing until the price is clearly positive (> RESUME) so a price
        # hovering around 0 can't flip the export limit every slot.
        neg_thresh = _EXPORT_RESUME_DKK if self._export_curtailed else _NEGATIVE_EXPORT_DKK
        if export_dkk < neg_thresh:
            self._grid_charging = False
            # zero_export=True: self-consumption charges from the surplus, the export cap
            # curtails anything beyond the battery's rate so nothing is sold at a negative price.
            batt_detail = self._absorb_surplus(worker, surplus_w, batt_soc, data, True,
                                               grid_w, batt_w)
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
        # during a surplus the battery should keep charging via self-consumption instead.
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

        # ── 4. Export arbitrage: sell stored energy high — the BUSINESS CASE ──
        # A discharge to grid only makes sense if it beats the best alternative use
        # of that same stored kWh: self-consuming it at the most expensive upcoming
        # slot (which would otherwise be imported). So the bar is the OPPORTUNITY
        # COST — the max import price over the look-ahead window — not the cheapest
        # future import. Because import > export for a given hour, comparing against
        # the cheapest rebuy (the old gate) let the pack dump into an evening peak
        # and then re-import the house load at an even higher price — a guaranteed
        # loss (observed 2026-07-01: sold ~3 kWh at ~1.6 while the 21–22h load
        # imported at ~2.65). Requiring export_now ≥ opportunity_cost × (1 + gain)
        # makes the round trip provably non-losing; the gain % is user-set.
        #
        # The reserve/excess check is kept as a secondary SoC guard. With the new
        # bar there is by construction no upcoming slot pricier than export_now, so
        # the reserve is ~0 and this just prevents deep-cycling for a marginal gain.
        arbit_window = _window(_ARBIT_HORIZON_H)
        if arbitrage_enabled() and arbit_window and batt_soc > _MIN_SOC_ARBIT:
            opportunity_cost   = max(p['import'] for p in arbit_window)  # best self-consumption value
            min_gain           = arbitrage_min_gain()
            threshold          = opportunity_cost * (1.0 + min_gain)
            gain_pct           = (export_dkk / opportunity_cost - 1.0) * 100.0 if opportunity_cost > 0 else -100.0
            profitable         = export_dkk >= threshold
            cap_kwh       = data.get('batt_rated_capacity') or 5.0
            available_kwh = max(0.0, (batt_soc - _MIN_SOC_ARBIT) / 100.0 * cap_kwh)
            reserve_kwh   = (self._reserve_energy_kwh(
                now_ms, now_ms + _ARBIT_HORIZON_H * 3_600_000, export_dkk, prices)
                if self._load_fc else 0.0)
            excess_kwh    = available_kwh - reserve_kwh
            do_export     = profitable and excess_kwh > _ARBIT_MIN_EXCESS_KWH
            gate_lbl      = (f'gain {gain_pct:+.0f}% vs need {min_gain * 100:.0f}%, '
                             f'excess {excess_kwh:.1f} kWh')
            if do_export:
                self._grid_charging = False
                self._set_export_limit(worker, False, data)   # MUST export to grid
                # Write 47249 (forcible DISCHARGE power) unconditionally every tick,
                # bypassing the _apply cache. THIS is the discharge setpoint. The
                # earlier code wrote 47247 — the forcible CHARGE power — so the
                # discharge rate was never set and the inverter fell back to its
                # internal default (~1634 W), which is exactly the cap we kept hitting.
                # 47247 and 47249 are distinct registers (verified against
                # wlcrs/huawei-solar-lib: STORAGE_FORCIBLE_CHARGE_POWER=47247,
                # STORAGE_FORCIBLE_DISCHARGE_POWER=47249). The inverter resets 47249 to
                # its ~1634 W default when leaving forced mode, so re-assert every tick.
                worker.write_u32(47249, _BATT_MAX_DISCHARGE_W,
                                 f'AutoCtrl: forcible discharge power {_BATT_MAX_DISCHARGE_W} W')
                self._applied[47249] = _BATT_MAX_DISCHARGE_W
                self._apply(worker, [
                    (16, 47087, 0, 'AutoCtrl: grid charge OFF'),
                    (16, 47086, 1, 'AutoCtrl: mode=forced'),
                    (16, 47100, 2, 'AutoCtrl: force DISCHARGE'),
                ])
                detail = (f'Arb. discharge: export {export_dkk:.3f} DKK ≥ '
                          f'opportunity cost {opportunity_cost:.3f} DKK ×{1 + min_gain:.2f}, '
                          f'{gate_lbl} (SoC {batt_soc:.0f}%)')
                return _Cmd(mode='arbit_discharge', detail=detail)

        # ── 5. Default: self-consumption ──────────────────────────────────────
        # SURPLUS (PV meaningfully exceeds load): absorb the surplus into the
        # battery (native self-consumption below 90% SoC, meter-fed forced charge
        # near full); excess PV exports at the (non-negative) price, so the export
        # cap is lifted. Gated on real PV (> MIN_PV_W) so a glitched house_load
        # can't fake a surplus and trigger a grid charge.
        #
        # DEFICIT (PV ≤ load): max self-consumption with zero export — mode 2
        # covers the load natively (no fixed setpoint), plus Active Power Control
        # mode 5 ("zero export limitation", set via _set_export_limit) so the
        # inverter drives the battery to cover the load and never exports. The
        # raised discharge ceiling (47077, via _ensure_power_limits) lets it cover
        # the whole load, not a trickle.
        self._grid_charging = False
        # Debounced surplus/deficit selection: house_load glitches (0 W and ~5 kW in
        # adjacent polls) flapped this branch every few ticks, rewriting 47086/47100/
        # 47415 each time — and with the old 47086=4 (fully-fed-to-grid) enum bug a
        # single glitched flip to the surplus side lifted the export cap and let the
        # inverter dump the battery to grid (observed −2500 W batt, −4080 W grid at
        # 12:37 on 2026-07-01, and again at 28–30% SoC on 2026-07-03 07:10). Only
        # switch sides after the new side has won _SC_DEBOUNCE_TICKS consecutive
        # ticks; a held surplus branch is safe during a real deficit because the
        # meter-fed charge sizing goes negative and idles the battery.
        want_surplus = surplus_w > 100 and pv_w > _MIN_PV_W
        if want_surplus != self._sc_surplus:
            self._sc_streak += 1
            if self._sc_streak >= _SC_DEBOUNCE_TICKS:
                self._sc_surplus = want_surplus
                self._sc_streak = 0
        else:
            self._sc_streak = 0
        if self._sc_surplus:
            # zero_export=False: excess PV beyond the charge rate exports at the good price.
            batt_detail = self._absorb_surplus(worker, surplus_w, batt_soc, data, False,
                                               grid_w, batt_w)
            detail = (f'Self-consumption: battery {batt_detail} '
                      f'(PV {pv_w:.0f} W, load {house_load:.0f} W, '
                      f'surplus {surplus_w:+.0f} W, SoC {batt_soc:.1f}%)')
            return _Cmd(mode='export_unlimited', detail=detail)
        self._set_export_limit(worker, True, data)        # mode 5: zero export limitation
        self._set_self_consumption(worker, data)
        detail = (f'Self-consumption: zero-export, battery covers load (mode 2 + APC 5) '
                  f'(PV {pv_w:.0f} W, load {house_load:.0f} W, '
                  f'deficit {surplus_w:+.0f} W, SoC {batt_soc:.1f}%)')
        return _Cmd(mode='sc_discharge', detail=detail)
