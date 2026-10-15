"""
Daily consumption profile model for predicting house load.

Maintains a 96-slot (15 min each) time-of-day profile of house load (W) plus a
short per-slot history for confidence bands.

Time-of-day, not time-of-week: once EV charging is separated out this house shows
no weekday pattern (2026-08/10: 16.5–17.5 kWh every weekday), and a weekly profile
gets only one observation per slot per week — so it lagged and was noisier.

THE FORECAST IS A ROBUST SHAPE SCALED TO THE TRUE DAILY ENERGY. A kettle or oven
switched on at a random time is real energy but not a repeatable pattern: a plain
mean bakes each such event into its quarter-hour, so the forecast came out spiky
and the spikes rarely recurred. So:
  • shape   — per-slot Huber EWM: each day's deviation from the shape is clipped to
              ±1·(typical deviation), so a one-off spike barely moves it, while a
              change that repeats day after day still pulls it over;
  • profile — plain per-slot EWM mean, used for its daily ENERGY total only;
  • forecast = smoothed shape × (Σ profile / Σ shape): the random-appliance energy
              is spread over the day instead of dropped, so battery planning still
              sees the right kWh.
Walk-forward over 2026-08-29..10-14 vs the previous plain-mean forecast: curve
spikiness (mean |Δ| between adjacent slots) 33 → 16 W, 15-min MAE 261 → 258 W,
6-h energy MAE 1.015 → 0.987 kWh, daily energy bias unchanged (+0.28 kWh).

Bands are the 5th–95th percentile of the slot's last 14 days, smoothed in time.
Day-to-day drift means that range holds the actual load ~82 % of the time — i.e.
it is an honest ~80 % band (the old ±1.28σ band held only ~70 %).

Learning is a replay of the same update() over history (seed_from_polls), then
continues online, so seeded and live state are always computed identically.

Slot index: hour * 4 + minute // 15  (local Europe/Copenhagen time)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

log = logging.getLogger(__name__)

_TZ            = ZoneInfo('Europe/Copenhagen')
_SLOTS_PER_DAY = 96
_SLOTS_TOTAL   = _SLOTS_PER_DAY

_DEFAULT_ALPHA = 0.15   # EWM learning rate per daily observation (~1-week memory)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# The house always draws at least this (standby/always-on loads); predictions are
# floored to it so a sparse slot can't forecast an unphysical ~0 W.
_MIN_STANDBY_W = _env_float('CONSUMPTION_MIN_STANDBY_W', 300.0)

# Slot averages above this are Modbus glitches (house_load carries unsanitised
# batt/meter noise), not load — they never enter the model.
_MAX_SLOT_W = _env_float('CONSUMPTION_MAX_SLOT_W', 15000.0)

# Huber shape: a day's deviation is clipped to ±_ROBUST_C × the slot's typical
# absolute deviation (EWM, floored so quiet night slots aren't frozen).
_ROBUST_C         = 1.0
_SPREAD_INIT_W    = 300.0
_SPREAD_FLOOR_W   = 150.0

# Time smoothing of the forecast: σ = 3 slots (45 min). Household load is smooth in
# expectation; 3 halved the curve's jitter vs 1.2 at identical accuracy.
_SMOOTH_SIGMA_SLOTS = _env_float('CONSUMPTION_SMOOTH_SIGMA_SLOTS', 3.0)

_BAND_DAYS     = 14
_BAND_LO_PCT   = 5.0
_BAND_HI_PCT   = 95.0
_BAND_MIN_OBS  = 5

_SLOT_MIN_SAMPLES = 10   # fewer polls in a 15-min slot = partial slot (downtime/restart);
                         # ~19 expected at a 30 s interval + ~16 s read


def _local_idx(dt_local: datetime) -> int:
    return dt_local.hour * 4 + dt_local.minute // 15


def slot_average(samples: list[float]) -> Optional[float]:
    """Average of one 15-min slot's house_load polls, or None for a partial slot.

    10 %-trimmed mean: single-poll house_load glitches (0 W and ~5 kW in adjacent
    polls — non-simultaneous batched reads) fall in the trimmed tails, while
    sustained real load (oven, charger) spans enough samples to survive. Shared by
    the online path (server) and seed_from_polls so both learn from the same values.
    """
    if len(samples) < _SLOT_MIN_SAMPLES:
        return None
    srt  = sorted(samples)
    trim = len(srt) // 10
    kept = srt[trim:len(srt) - trim] if trim else srt
    return sum(kept) / len(kept)


def _slot_records(records: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Collapse per-poll (ts, W) records into per-15-min-slot (slot_start_ts, avg W)."""
    out: list[tuple[float, float]] = []
    key, buf = None, []
    for ts, w in records:
        if w is None:
            continue
        k = int(ts // 900)
        if k != key:
            if key is not None:
                avg = slot_average(buf)
                if avg is not None:
                    out.append((key * 900.0, avg))
            key, buf = k, []
        buf.append(max(0.0, float(w)))
    if key is not None:
        avg = slot_average(buf)
        if avg is not None:
            out.append((key * 900.0, avg))
    return out


def _smooth_profile_circular(profile: np.ndarray, counts: np.ndarray,
                             sigma: float) -> np.ndarray:
    """Gaussian-smooth a daily 96-slot array along the time-of-day axis.

    Circular (23:45 wraps to 00:00), so there is no seam. Slots never observed are
    excluded via a normalised mask, so they neither contribute to nor are dragged
    toward zero by their neighbours. Returns a smoothed copy.
    """
    if sigma <= 0:
        return profile
    r = max(1, int(round(3 * sigma)))
    kernel = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    kernel /= kernel.sum()

    def _circ_conv(a: np.ndarray) -> np.ndarray:
        padded = np.concatenate([a[-r:], a, a[:r]])
        return np.convolve(padded, kernel, mode='valid')

    mask = (counts > 0).astype(float)
    num  = _circ_conv(np.where(mask > 0, profile, 0.0))
    den  = _circ_conv(mask)
    return np.where(den > 0, num / den, profile)


class ConsumptionModel:
    def __init__(self, alpha: float = _DEFAULT_ALPHA):
        self.alpha   = alpha
        self.profile = np.zeros(_SLOTS_TOTAL)                 # W, plain EWM mean (energy level)
        self.shape   = np.zeros(_SLOTS_TOTAL)                 # W, Huber EWM (robust daily shape)
        self.spread  = np.full(_SLOTS_TOTAL, _SPREAD_INIT_W)  # W, EWM |actual − shape|
        self.counts  = np.zeros(_SLOTS_TOTAL)                 # observations per slot
        self.recent: list[list[float]] = [[] for _ in range(_SLOTS_TOTAL)]  # last 14 days
        # Per-slot EWM of the EV-charging contribution: max(0, actual − profile) on a
        # slot above the EV threshold, 0 on every other slot. Updated every slot, so it
        # converges to the PROBABILITY-WEIGHTED expected extra load ("charges about
        # half the days at this hour" → about half the session's excess). Kept apart
        # from the baseline so battery planning can drop it under the EV lock.
        self.ev_addon  = np.zeros(_SLOTS_TOTAL)
        self.ev_counts = np.zeros(_SLOTS_TOTAL)

    # ── learning ──────────────────────────────────────────────────────────────

    def seed_from_polls(self, records: list[tuple[float, float]],
                        ev_threshold_w: Optional[float] = None) -> int:
        """
        Rebuild the model from house_load poll history [(ts_utc, W), ...] by
        replaying update()/update_ev_addon() over its 15-min slot averages in time
        order — exactly what the online path would have learned. Slots above
        ev_threshold_w are EV charging: they feed only the EV addon, never the
        baseline. Returns the number of slots replayed into the baseline.
        """
        slots = _slot_records(records)
        if not slots:
            return 0
        self.__init__(self.alpha)
        n = 0
        for ts, w in slots:
            if w > _MAX_SLOT_W:
                continue
            if ev_threshold_w is None or w <= ev_threshold_w:
                self.update(ts, w)
                n += 1
            if ev_threshold_w is not None:
                self.update_ev_addon(ts, w, ev_threshold_w)
        return n

    def seed_from_excel(self, excel_path: str) -> int:
        """
        Seed from a MeterData.xlsx file (D06 column = net grid import), replayed
        through update() in time order. NOTE: D06 is net import, not house load —
        seed_from_polls() replaces it as soon as poll history exists.
        """
        import pandas as pd

        log.info('ConsumptionModel: loading Excel %s', excel_path)
        df  = pd.read_excel(excel_path)
        d06 = df[df['Metering_point_type_Code'] == 'D06'][['From_date', 'Volume']].copy()
        if d06.empty:
            log.warning('ConsumptionModel: no D06 rows found in Excel')
            return 0
        d06['From_date'] = pd.to_datetime(d06['From_date'])
        try:
            d06['local'] = d06['From_date'].dt.tz_localize(
                _TZ, ambiguous='infer', nonexistent='shift_forward')
        except Exception:
            d06['local'] = d06['From_date'].dt.tz_localize('UTC')
        d06 = d06.sort_values('local')
        for ts, kwh in zip(d06['local'], d06['Volume']):
            self.update(ts.timestamp(), float(kwh) * 4000.0)   # kWh/15min → W
        log.info('ConsumptionModel: seeded %d D06 rows', len(d06))
        return len(d06)

    def update(self, ts_utc: float, watts: float):
        """Learn one completed 15-min slot average (call once per slot)."""
        idx = _local_idx(datetime.fromtimestamp(ts_utc, tz=_TZ))
        x   = min(max(0.0, watts), _MAX_SLOT_W)
        if self.counts[idx] < 1:
            self.profile[idx] = x
            self.shape[idx]   = x
        else:
            a = self.alpha
            self.profile[idx] = (1 - a) * self.profile[idx] + a * x
            lim = _ROBUST_C * self.spread[idx]
            self.shape[idx]  += a * min(max(x - self.shape[idx], -lim), lim)
            self.spread[idx]  = max(_SPREAD_FLOOR_W,
                                    (1 - a) * self.spread[idx] + a * abs(x - self.shape[idx]))
        hist = self.recent[idx]
        hist.append(x)
        del hist[:-_BAND_DAYS]
        self.counts[idx] += 1

    def update_ev_addon(self, ts_utc: float, watts: float, threshold_w: float):
        """
        Update the per-slot EV addon with one completed slot average. Call for EVERY
        slot, alongside update(): a slot above threshold_w contributes its excess over
        the baseline, every other slot contributes 0, so the EWM is the expected extra.
        """
        idx = _local_idx(datetime.fromtimestamp(ts_utc, tz=_TZ))
        x = min(max(0.0, watts), _MAX_SLOT_W)
        baseline = float(self.profile[idx]) if self.counts[idx] >= 1 else _MIN_STANDBY_W
        contrib = max(0.0, x - baseline) if x > threshold_w else 0.0
        if self.ev_counts[idx] < 1:
            self.ev_addon[idx] = contrib
        else:
            self.ev_addon[idx] = (1 - self.alpha) * self.ev_addon[idx] + self.alpha * contrib
        self.ev_counts[idx] += 1

    # ── prediction ───────────────────────────────────────────────────────────

    def _baseline(self) -> np.ndarray:
        """Smoothed robust shape scaled to the plain mean's daily energy."""
        seen = self.counts > 0
        shape = _smooth_profile_circular(self.shape, self.counts, _SMOOTH_SIGMA_SLOTS)
        if seen.any() and self.shape[seen].sum() > 0:
            shape = shape * (self.profile[seen].sum() / self.shape[seen].sum())
        return np.maximum(shape, _MIN_STANDBY_W)

    def _bands(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Smoothed 5th/95th percentiles of each slot's last 14 days + validity mask."""
        lo = np.full(_SLOTS_TOTAL, np.nan)
        hi = np.full(_SLOTS_TOTAL, np.nan)
        n  = np.array([len(h) for h in self.recent], dtype=float)
        for i, h in enumerate(self.recent):
            if len(h) >= _BAND_MIN_OBS:
                lo[i] = np.percentile(h, _BAND_LO_PCT)
                hi[i] = np.percentile(h, _BAND_HI_PCT)
        ok = n >= _BAND_MIN_OBS
        lo = _smooth_profile_circular(np.nan_to_num(lo), ok.astype(float), _SMOOTH_SIGMA_SLOTS)
        hi = _smooth_profile_circular(np.nan_to_num(hi), ok.astype(float), _SMOOTH_SIGMA_SLOTS)
        return lo, hi, ok

    def predict(self, from_ts_utc: float, n_slots: int = 96) -> list[dict]:
        """
        n_slots × 15-min predictions from from_ts_utc:
        [{'ts_ms', 'w', 'ev_w', 'p10_w', 'p90_w'}]. 'w' = baseline + expected EV
        load; 'ev_w' is that EV part (battery planning drops it under the EV lock).
        'p10_w'/'p90_w' bound a calibrated ~80 % range (see module docstring); None
        while a slot has fewer than 5 days of history.
        """
        base = self._baseline()
        ev   = _smooth_profile_circular(self.ev_addon, self.ev_counts, _SMOOTH_SIGMA_SLOTS)
        lo, hi, ok = self._bands()
        t = int(from_ts_utc // 900) * 900
        out = []
        for i in range(n_slots):
            ts  = t + i * 900
            idx = _local_idx(datetime.fromtimestamp(ts, tz=_TZ))
            ev_w = float(ev[idx]) if self.ev_counts[idx] > 0 else 0.0
            b    = float(base[idx])
            w    = b + ev_w
            if ok[idx]:
                p10 = round(max(_MIN_STANDBY_W, min(float(lo[idx]), b)), 1)
                p90 = round(max(w, float(hi[idx]) + ev_w), 1)
            else:
                p10 = p90 = None
            out.append({'ts_ms': ts * 1000, 'w': round(w, 1), 'ev_w': round(ev_w, 1),
                        'p10_w': p10, 'p90_w': p90})
        return out

    @property
    def coverage(self) -> int:
        """Number of slots with at least one observation."""
        return int(np.sum(self.counts > 0))

    # ── persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path):
        path.write_text(json.dumps({
            'alpha':     self.alpha,
            'profile':   self.profile.tolist(),
            'shape':     self.shape.tolist(),
            'spread':    self.spread.tolist(),
            'counts':    self.counts.tolist(),
            'recent':    self.recent,
            'ev_addon':  self.ev_addon.tolist(),
            'ev_counts': self.ev_counts.tolist(),
        }))

    @classmethod
    def load(cls, path: Path) -> ConsumptionModel:
        d = json.loads(path.read_text())
        if 'shape' not in d or len(d['profile']) != _SLOTS_TOTAL:
            # Older format — the caller starts fresh and re-seeds from poll history.
            raise ValueError('model file predates the robust-shape format')
        m = cls(alpha=d.get('alpha', _DEFAULT_ALPHA))
        m.profile   = np.array(d['profile'],   dtype=float)
        m.shape     = np.array(d['shape'],     dtype=float)
        m.spread    = np.array(d['spread'],    dtype=float)
        m.counts    = np.array(d['counts'],    dtype=float)
        m.recent    = [list(map(float, h)) for h in d['recent']]
        m.ev_addon  = np.array(d['ev_addon'],  dtype=float)
        m.ev_counts = np.array(d['ev_counts'], dtype=float)
        return m
