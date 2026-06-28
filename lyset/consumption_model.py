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


def _local_idx(dt_local: datetime) -> int:
    return dt_local.weekday() * _SLOTS_PER_DAY + dt_local.hour * 4 + dt_local.minute // 15


class ConsumptionModel:
    def __init__(self, alpha: float = _DEFAULT_ALPHA):
        self.alpha    = alpha
        self.profile  = np.zeros(_SLOTS_TOTAL, dtype=float)   # W per slot (EWM mean)
        self.variance = np.zeros(_SLOTS_TOTAL, dtype=float)   # EWM variance (W²)
        self.counts   = np.zeros(_SLOTS_TOTAL, dtype=float)   # effective observation count

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

        Returns the number of slots updated.
        """
        if not records:
            return 0

        now_ts = max(ts for ts, _ in records)
        decay  = math.log(2) / (half_life_days * 86400)

        w_sum   = np.zeros(_SLOTS_TOTAL)
        wx_sum  = np.zeros(_SLOTS_TOTAL)
        wx2_sum = np.zeros(_SLOTS_TOTAL)
        cnt     = np.zeros(_SLOTS_TOTAL, dtype=int)

        for ts, w in records:
            if w is None or w < 0:
                continue
            dt_local = datetime.fromtimestamp(ts, tz=_TZ)
            idx    = _local_idx(dt_local)
            weight = math.exp(-decay * (now_ts - ts))
            w_sum[idx]   += weight
            wx_sum[idx]  += weight * w
            wx2_sum[idx] += weight * w * w
            cnt[idx]     += 1

        updated = 0
        for idx in range(_SLOTS_TOTAL):
            if cnt[idx] < 3:
                continue
            ws   = w_sum[idx]
            mean = wx_sum[idx] / ws
            var  = max(0.0, wx2_sum[idx] / ws - mean * mean)
            self.profile[idx]  = max(0.0, mean)
            self.variance[idx] = var
            self.counts[idx]   = min(float(cnt[idx]), float(_SEED_CAP))
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
        Slots with no observations return w=p10_w=p90_w=None.
        p10/p90 use ±1.28σ (normal approximation) and are omitted when count < 5.
        """
        slot_sec = 900  # 15 min
        t   = int(from_ts_utc / slot_sec) * slot_sec
        out = []
        for i in range(n_slots):
            ts_utc_i = t + i * slot_sec
            dt_local = datetime.fromtimestamp(ts_utc_i, tz=_TZ)
            idx      = _local_idx(dt_local)
            if self.counts[idx] > 0:
                mean = float(self.profile[idx])
                w    = round(mean, 1)
                if self.counts[idx] >= 5 and self.variance[idx] > 0:
                    std  = math.sqrt(float(self.variance[idx]))
                    p10  = round(max(0.0, mean - 1.28 * std), 1)
                    p90  = round(mean + 1.28 * std, 1)
                else:
                    p10 = p90 = None
            else:
                w = p10 = p90 = None
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
        }))

    @classmethod
    def load(cls, path: Path) -> ConsumptionModel:
        d = json.loads(path.read_text())
        m = cls(alpha=d.get('alpha', _DEFAULT_ALPHA))
        m.profile  = np.array(d['profile'],  dtype=float)
        m.variance = np.array(d.get('variance', [0.0] * _SLOTS_TOTAL), dtype=float)
        m.counts   = np.array(d['counts'],   dtype=float)
        return m
