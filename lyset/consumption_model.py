"""
Weekly consumption profile model for predicting house load.

Maintains a 7-day × 96-slot (15 min each) profile of average house load (W)
and its variance, enabling p10/p90 confidence bands.

Seeded via seed_from_polls() from actual house_load DB history (exponential
time-decay so recent data weighs more), then updated online via EWM as new
Modbus readings arrive.

Slot index: weekday * 96 + hour * 4 + minute // 15  (local Europe/Copenhagen time)
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

log = logging.getLogger(__name__)

_TZ            = ZoneInfo('Europe/Copenhagen')
_SLOTS_PER_DAY = 96
_SLOTS_TOTAL   = 7 * _SLOTS_PER_DAY   # 672

_DEFAULT_ALPHA = 0.10   # EWM learning rate — faster adaptation than old 0.05
_SEED_CAP      = 30     # max effective count after seeding
_HALF_LIFE_DAYS = 14.0  # recency decay: data 14 days old gets half the weight

# Global bias corrector: EWM of (actual − served forecast) across all slots,
# added onto every prediction. The per-slot profile self-corrects too, but only
# at ~1 observation per slot per week — a persistent whole-model bias (measured
# −175 W on 2026-07-02, mostly stale D06 seed washing out) takes weeks to clear
# slot-by-slot. This corrector clears it in days while the slots converge
# underneath it, then decays to ~0. Clamped so one wild day can't distort it.
_BIAS_ALPHA   = 0.05    # per 15-min slot → ~62% of a step change absorbed per day
_BIAS_CLAMP_W = 500.0

# The house always draws at least this (standby/always-on loads). Predictions are
# floored to it so slots that still hold the stale D06 net-import seed (≈0 W
# overnight, because the battery used to cover the standby) — or slots with no data
# yet — don't forecast an unphysical ~0 W. Non-destructive: the floor is applied to
# predictions only, so the learned profile keeps converging to the true value and
# this just stops the forecast (and the SoC simulation) from dropping below standby.
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

_MIN_STANDBY_W = _env_float('CONSUMPTION_MIN_STANDBY_W', 300.0)

# Robust outlier rejection for learning. house_load = active + grid − batt, and the
# batt and meter channels are intentionally NOT sanitized upstream (see the read-layer
# notes), so a momentary Modbus glitch on either surfaces as a multi-kW spike in one
# 15-min slot. Left unguarded, a plain (weighted) mean bakes that spike into the slot's
# profile — which is what left the daytime forecast jagged and inflated (adjacent slots
# swinging ~1.8 kW) while the true expected curve is smooth. Both the seed and the
# online update therefore winsorize a slot's inputs to centre + K·(robust σ), with an
# absolute physical ceiling as a backstop. This rejects spikes WITHIN a slot; it never
# blends across slots, so real time-of-day structure is preserved.
_MAX_SLOT_W               = _env_float('CONSUMPTION_MAX_SLOT_W', 15000.0)  # W; above = glitch
_OUTLIER_K                = 5.0     # clip values beyond centre + K·spread
_OUTLIER_SPREAD_FLOOR_W   = 400.0   # min spread so genuinely tight slots aren't over-clipped
_UPDATE_OUTLIER_MIN_COUNT = 5       # online clip engages only once a slot has some history

# Temporal-smoothness prior on the profile. Each 15-min-of-week slot's mean is
# estimated from only a few dozen samples of a high-variance quantity (household
# load), so the raw per-slot estimate is noisy: adjacent slots jump by hundreds of W
# purely from which days happened to land in each bin (measured max jump ~3 kW). The
# TRUE expected load is smooth in time, so the forecast borrows strength from
# neighbouring slots — a Gaussian along the time-of-day axis. This regularises the
# estimator itself (so the house-needs line, the SoC plan, the grid/export forecast,
# and the auto-controller all consume a de-noised expected curve); it is NOT a cosmetic
# smoothing of any output line. σ≈1.2 slots (~18 min) removes the quarter-hour noise
# while preserving the real morning/evening structure. 0 disables it.
_SMOOTH_SIGMA_SLOTS = _env_float('CONSUMPTION_SMOOTH_SIGMA_SLOTS', 1.2)


def _local_idx(dt_local: datetime) -> int:
    return dt_local.weekday() * _SLOTS_PER_DAY + dt_local.hour * 4 + dt_local.minute // 15


def _smooth_profile_circular(profile: np.ndarray, counts: np.ndarray,
                             sigma: float) -> np.ndarray:
    """Gaussian-smooth the weekly profile along the time-of-day axis.

    The 672-slot array is treated as circular (Sunday 23:45 wraps to Monday 00:00 —
    adjacent indices are temporally adjacent), so the smoothing has no seam. Slots that
    were never observed are excluded via a normalised mask, so they neither contribute
    to nor are dragged toward zero by their neighbours. Returns a smoothed COPY; the
    stored raw profile (and its variance/bands) is left untouched.
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
    num  = _circ_conv(profile * mask)
    den  = _circ_conv(mask)
    return np.where(den > 0, num / den, profile)


class ConsumptionModel:
    def __init__(self, alpha: float = _DEFAULT_ALPHA):
        self.alpha    = alpha
        self.profile  = np.zeros(_SLOTS_TOTAL, dtype=float)   # W per slot (EWM mean)
        self.variance = np.zeros(_SLOTS_TOTAL, dtype=float)   # EWM variance (W²)
        self.counts   = np.zeros(_SLOTS_TOTAL, dtype=float)   # effective observation count
        self.bias     = 0.0                                   # W — EWM of (actual − forecast)

    # ── seeding from DB history ───────────────────────────────────────────────

    def seed_from_polls(
        self,
        records: list[tuple[float, float]],
        half_life_days: float = _HALF_LIFE_DAYS,
    ) -> int:
        """
        Build profile from actual house_load poll records: [(ts_utc, watts), ...].

        Uses exponential time-decay so recent observations count more than old ones.
        Only overwrites slots that have ≥ 3 observations in the provided records;
        sparse slots retain their current values so the model degrades gracefully
        during the first days when the DB is thin.

        Each slot's values are winsorised to a robust range (median + K·MAD-σ, and an
        absolute ceiling) BEFORE the weighted mean, so glitch spikes carried in by the
        unsanitised batt/meter channels can't inflate that slot's profile.

        Returns the number of slots updated.
        """
        if not records:
            return 0

        now_ts = max(ts for ts, _ in records)
        decay  = math.log(2) / (half_life_days * 86400)

        # Bucket each slot's (weight, value) so we can compute a robust per-slot cap
        # before averaging — a single weighted-mean pass can't reject outliers.
        slot_w: dict[int, list[float]] = {}
        slot_x: dict[int, list[float]] = {}
        for ts, w in records:
            if w is None or w < 0 or w > _MAX_SLOT_W:
                continue
            idx = _local_idx(datetime.fromtimestamp(ts, tz=_TZ))
            slot_w.setdefault(idx, []).append(math.exp(-decay * (now_ts - ts)))
            slot_x.setdefault(idx, []).append(float(w))

        updated = 0
        for idx, xs in slot_x.items():
            if len(xs) < 3:
                continue
            x_arr = np.asarray(xs, dtype=float)
            w_arr = np.asarray(slot_w[idx], dtype=float)
            med   = float(np.median(x_arr))
            # 1.4826·MAD ≈ σ for normal data; floor the spread so a genuinely tight
            # slot isn't clipped to a hair, and cap at the absolute physical ceiling.
            mad    = float(np.median(np.abs(x_arr - med)))
            spread = max(1.4826 * mad, _OUTLIER_SPREAD_FLOOR_W)
            cap    = min(_MAX_SLOT_W, med + _OUTLIER_K * spread)
            x_clip = np.minimum(x_arr, cap)
            ws     = float(w_arr.sum())
            mean   = float((w_arr * x_clip).sum() / ws)
            var    = max(0.0, float((w_arr * x_clip * x_clip).sum() / ws - mean * mean))
            self.profile[idx]  = max(0.0, mean)
            self.variance[idx] = var
            self.counts[idx]   = min(float(len(xs)), float(_SEED_CAP))
            updated += 1

        return updated

    # ── legacy Excel seed (kept for back-compat) ──────────────────────────────

    def seed_from_excel(self, excel_path: str) -> int:
        """
        Seed the profile from a MeterData.xlsx file (D06 column = net grid import).
        NOTE: D06 is net import, not house load — prefer seed_from_polls() instead.
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
                _TZ, ambiguous='infer', nonexistent='shift_forward'
            )
        except Exception:
            d06['local'] = d06['From_date'].dt.tz_localize('UTC')

        d06['idx'] = (d06['local'].dt.weekday * _SLOTS_PER_DAY
                      + d06['local'].dt.hour * 4
                      + d06['local'].dt.minute // 15)
        d06['w']   = d06['Volume'] * 4000.0  # kWh/15min → W

        profile_mean = d06.groupby('idx')['w'].mean()
        profile_std  = d06.groupby('idx')['w'].std().fillna(0)
        profile_cnt  = d06.groupby('idx')['w'].count()

        self.profile[profile_mean.index.values]   = profile_mean.values
        self.variance[profile_std.index.values]   = profile_std.values ** 2
        self.counts[profile_cnt.index.values]     = np.minimum(
            profile_cnt.values.astype(float), float(_SEED_CAP)
        )

        n = int(d06['idx'].count())
        log.info('ConsumptionModel: seeded %d D06 rows, %d unique slots', n, len(profile_mean))
        return n

    # ── online update ─────────────────────────────────────────────────────────

    def update(self, ts_utc: float, watts: float):
        """
        Update profile with one completed 15-min slot average.
        Call once per slot, not on every Modbus sample.
        Uses EWM for both mean and variance so the model adapts to changing patterns.
        """
        dt_local = datetime.fromtimestamp(ts_utc, tz=_TZ)
        idx = _local_idx(dt_local)
        x   = max(0.0, watts)
        # Reject glitch spikes before they enter the EWM (and the bias): house_load
        # carries unsanitised batt/meter noise, so a single multi-kW blip must not
        # yank this slot's mean. Absolute ceiling always; once the slot has some
        # history, also clip to profile + K·σ (same winsorising as the seed).
        x = min(x, _MAX_SLOT_W)
        if self.counts[idx] >= _UPDATE_OUTLIER_MIN_COUNT:
            std = math.sqrt(max(0.0, float(self.variance[idx])))
            x   = min(x, float(self.profile[idx]) + _OUTLIER_K * max(std, _OUTLIER_SPREAD_FLOOR_W))
        # Bias update against the UNCORRECTED forecast for this slot (profile
        # mean with the standby floor, before the profile absorbs x). Measuring
        # against the raw model — not the bias-adjusted output — makes the EWM
        # converge directly to the model's mean residual instead of integrating.
        served = max(float(self.profile[idx]), _MIN_STANDBY_W) if self.counts[idx] >= 1 \
            else _MIN_STANDBY_W
        err = x - served
        self.bias = (1 - _BIAS_ALPHA) * self.bias + _BIAS_ALPHA * err
        self.bias = max(-_BIAS_CLAMP_W, min(_BIAS_CLAMP_W, self.bias))
        if self.counts[idx] < 1:
            self.profile[idx]  = x
            self.variance[idx] = 0.0
        else:
            old_mean           = self.profile[idx]
            self.profile[idx]  = (1 - self.alpha) * old_mean + self.alpha * x
            # EWM variance — tracks spread around the running mean
            self.variance[idx] = (1 - self.alpha) * (
                self.variance[idx] + self.alpha * (x - old_mean) ** 2
            )
        self.counts[idx] = min(self.counts[idx] + 1, 1000)

    # ── prediction ───────────────────────────────────────────────────────────

    def predict(self, from_ts_utc: float, n_slots: int = 96) -> list[dict]:
        """
        Generate n_slots × 15-min predictions starting from from_ts_utc.

        Returns list of {'ts_ms': int, 'w': float|None, 'p10_w': float|None, 'p90_w': float|None}.
        Every slot returns at least the standby floor (_MIN_STANDBY_W) for 'w', even
        with no observations — the house always draws standby, so a None/0 W forecast
        is never physical.  p10/p90 use ±1.28σ (normal approximation) and are omitted
        when count < 5.
        """
        slot_sec = 900  # 15 min
        t   = int(from_ts_utc / slot_sec) * slot_sec
        out = []
        # Forecast off the temporally-smoothed profile so the expected curve reflects
        # the smooth underlying load rather than per-slot sampling noise. Variance/bands
        # stay on the raw per-slot spread (they describe real dispersion, not estimator
        # noise), so p10/p90 still bracket the smoothed mean sensibly.
        sm_profile = _smooth_profile_circular(self.profile, self.counts, _SMOOTH_SIGMA_SLOTS)
        for i in range(n_slots):
            ts_utc_i = t + i * slot_sec
            dt_local = datetime.fromtimestamp(ts_utc_i, tz=_TZ)
            idx      = _local_idx(dt_local)
            if self.counts[idx] > 0:
                # Global bias corrector shifts the whole forecast; the floor
                # still applies afterwards (the house never draws less than
                # standby, whatever the corrector says).
                mean = float(sm_profile[idx]) + self.bias
                w    = round(max(mean, _MIN_STANDBY_W), 1)
                if self.counts[idx] >= 5 and self.variance[idx] > 0:
                    std  = math.sqrt(float(self.variance[idx]))
                    p10  = round(max(_MIN_STANDBY_W, mean - 1.28 * std), 1)
                    p90  = round(max(w, mean + 1.28 * std), 1)
                else:
                    p10 = p90 = None
            else:
                # No data for this slot yet — standby baseline plus bias.
                w   = round(max(_MIN_STANDBY_W + self.bias, _MIN_STANDBY_W), 1)
                p10 = p90 = None
            out.append({'ts_ms': ts_utc_i * 1000, 'w': w, 'p10_w': p10, 'p90_w': p90})
        return out

    @property
    def coverage(self) -> int:
        """Number of slots with at least one observation."""
        return int(np.sum(self.counts > 0))

    # ── persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path):
        path.write_text(json.dumps({
            'alpha':    self.alpha,
            'profile':  self.profile.tolist(),
            'variance': self.variance.tolist(),
            'counts':   self.counts.tolist(),
            'bias':     self.bias,
        }))

    @classmethod
    def load(cls, path: Path) -> ConsumptionModel:
        d = json.loads(path.read_text())
        m = cls(alpha=d.get('alpha', _DEFAULT_ALPHA))
        m.profile  = np.array(d['profile'],  dtype=float)
        m.variance = np.array(d.get('variance', [0.0] * _SLOTS_TOTAL), dtype=float)
        m.counts   = np.array(d['counts'],   dtype=float)
        m.bias     = max(-_BIAS_CLAMP_W, min(_BIAS_CLAMP_W, float(d.get('bias', 0.0))))
        return m
