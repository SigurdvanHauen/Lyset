"""
Weekly consumption profile model for predicting grid import.

Maintains a 7-day × 96-slot (15 min each) profile of average grid import (W).
Seeded from historical meter data (Excel D06 column), then updated online
via EMA as new Modbus readings arrive.

Slot index: weekday * 96 + hour * 4 + minute // 15  (local Europe/Copenhagen time)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

log = logging.getLogger(__name__)

_TZ           = ZoneInfo('Europe/Copenhagen')
_SLOTS_PER_DAY = 96
_SLOTS_TOTAL   = 7 * _SLOTS_PER_DAY   # 672

_DEFAULT_ALPHA = 0.05   # EMA learning rate — ~50% transition after 14 real-data updates per slot
_SEED_CAP      = 30     # max effective count after seeding (keeps model adaptable)


def _local_idx(dt_local: datetime) -> int:
    return dt_local.weekday() * _SLOTS_PER_DAY + dt_local.hour * 4 + dt_local.minute // 15


class ConsumptionModel:
    def __init__(self, alpha: float = _DEFAULT_ALPHA):
        self.alpha   = alpha
        self.profile = np.zeros(_SLOTS_TOTAL, dtype=float)   # W per slot
        self.counts  = np.zeros(_SLOTS_TOTAL, dtype=float)   # effective observation count

    # ── seeding ──────────────────────────────────────────────────────────────

    def seed_from_excel(self, excel_path: str) -> int:
        """
        Seed the profile from a MeterData.xlsx file.
        Uses the D06 column (15-min net grid import, kWh per period).
        Returns number of D06 rows processed.
        """
        import pandas as pd

        log.info('ConsumptionModel: loading Excel %s', excel_path)
        df = pd.read_excel(excel_path)
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

        d06['idx']   = d06['local'].dt.weekday * _SLOTS_PER_DAY + \
                       d06['local'].dt.hour * 4 + d06['local'].dt.minute // 15
        d06['w']     = d06['Volume'] * 4000.0  # kWh/15min → W

        profile_mean = d06.groupby('idx')['w'].mean()
        profile_cnt  = d06.groupby('idx')['w'].count()

        self.profile[profile_mean.index.values] = profile_mean.values
        self.counts[profile_cnt.index.values]   = np.minimum(profile_cnt.values.astype(float), _SEED_CAP)

        n = int(d06['idx'].count())
        log.info('ConsumptionModel: seeded %d D06 rows, %d unique slots', n, len(profile_mean))
        return n

    # ── online update ─────────────────────────────────────────────────────────

    def update(self, ts_utc: float, watts: float):
        """
        Update profile with one completed 15-min slot average.
        Call once per slot, not on every Modbus sample.
        """
        dt_local = datetime.fromtimestamp(ts_utc, tz=_TZ)
        idx = _local_idx(dt_local)
        if self.counts[idx] < 1:
            self.profile[idx] = max(0.0, watts)
        else:
            self.profile[idx] = (1 - self.alpha) * self.profile[idx] + self.alpha * max(0.0, watts)
        self.counts[idx] = min(self.counts[idx] + 1, 1000)

    # ── prediction ───────────────────────────────────────────────────────────

    def predict(self, from_ts_utc: float, n_slots: int = 96) -> list[dict]:
        """
        Generate n_slots × 15-min predictions starting from from_ts_utc.
        Returns list of {'ts_ms': int, 'w': float|None}.
        Slots with no observations return w=None.
        """
        slot_sec = 900  # 15 min
        t = int(from_ts_utc / slot_sec) * slot_sec  # snap to 15-min boundary
        out = []
        for i in range(n_slots):
            ts_utc_i = t + i * slot_sec
            dt_local  = datetime.fromtimestamp(ts_utc_i, tz=_TZ)
            idx       = _local_idx(dt_local)
            w = round(float(self.profile[idx]), 1) if self.counts[idx] > 0 else None
            out.append({'ts_ms': ts_utc_i * 1000, 'w': w})
        return out

    @property
    def coverage(self) -> int:
        """Number of slots with at least one observation."""
        return int(np.sum(self.counts > 0))

    # ── persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path):
        path.write_text(json.dumps({
            'alpha':   self.alpha,
            'profile': self.profile.tolist(),
            'counts':  self.counts.tolist(),
        }))

    @classmethod
    def load(cls, path: Path) -> ConsumptionModel:
        d = json.loads(path.read_text())
        m = cls(alpha=d.get('alpha', _DEFAULT_ALPHA))
        m.profile = np.array(d['profile'], dtype=float)
        m.counts  = np.array(d['counts'],  dtype=float)
        return m
