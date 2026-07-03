"""
Solar forecast calibration — learns a per-hour-of-day correction factor for the
raw Solcast forecast from the site's own history.

Why: Solcast models the site from declared azimuth/tilt/capacity, but the real
roof has fixed quirks (orientation mismatch, shading, soiling) that show up as a
*stable, hour-shaped* bias. Measured on this site over 2026-06-26..07-02: actual
PV averaged 85 % of forecast overall, with mornings at ~70-80 % and mid-afternoon
(15-16h) as low as 63-71 % while evenings ran at ~100-110 %. Weather error is
symmetric noise around that shape and averages out in the EWM; the shape itself
is what this class learns.

Mechanics: 24 hourly factors, each an EWM starting at 1.0, updated with the
ABSOLUTE ratio r = actual / raw_forecast per completed 30-min slot:
    f ← (1 − α) · f + α · min(max(r, R_MIN), R_MAX)
apply() stamps every record with its uncalibrated value (raw_w, persisted in
the solar_forecast table) precisely so learning can use it. Learning against
the raw value keeps each observation independent — a residual scheme (ratio vs
the stored *calibrated* forecast) compounds multiplicatively when a backlog of
slots stored under the same old factor is consumed in one catch-up pass, and
overshoots (measured: 20 slots at ratio 0.7 drove the factor to 0.40).
Rows predating the raw_w column fall back to pv_w — they were stored
uncalibrated, so pv_w IS the raw value. Slots with a raw forecast below
MIN_LEARN_W are skipped — a 20 W dawn forecast makes the ratio pure noise.
Each slot is consumed once (learned_until_ms cursor), so restarts and repeated
fetches don't double-count. State persists as JSON next to the consumption
model.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_TZ = ZoneInfo('Europe/Copenhagen')

ALPHA        = 0.15    # EWM rate per 30-min observation (~2 obs/hour-bucket/day)
MIN_LEARN_W  = 150.0   # skip slots with a smaller stored forecast (ratio = noise)
FACTOR_MIN   = 0.25    # hard clamp on the learned factor
FACTOR_MAX   = 2.0
RATIO_MIN    = 0.2     # clamp on a single observation so one freak slot
RATIO_MAX    = 3.0     # (inverter offline, snow) can't jerk the factor


class SolarCalibration:
    def __init__(self):
        self.factors:          list[float] = [1.0] * 24   # per local hour of day
        self.counts:           list[int]   = [0] * 24     # observations consumed
        self.learned_until_ms: int         = 0            # newest slot learned

    @staticmethod
    def _hour_of(ts_ms: int) -> int:
        """Local hour of the CENTER of the 30-min period ending at ts_ms."""
        return datetime.fromtimestamp((ts_ms - 900_000) / 1000, tz=_TZ).hour

    # ── learning ──────────────────────────────────────────────────────────────

    def learn(self, forecast_rows: list[dict], actual_by_ts: dict[int, float],
              skip_ts: set[int] | None = None) -> int:
        """
        Consume past (raw forecast, actual PV) pairs newer than the cursor.

        forecast_rows: [{ts_ms, pv_w, raw_w}, ...] as stored in the
        solar_forecast table (period_end UTC ms). raw_w is the uncalibrated
        forecast; rows from before the column existed fall back to pv_w.
        actual_by_ts:  {period_end_ms: avg actual PV W} from poll history.
        skip_ts: period_end_ms of slots to advance the cursor past WITHOUT
        learning — used to drop curtailed slots (zero-export windows), where the
        measured PV was clipped to load + battery and would bias the factor down.
        Returns the number of slots consumed.
        """
        skip = skip_ts or set()
        used = 0
        max_seen = self.learned_until_ms
        for r in sorted(forecast_rows, key=lambda x: x['ts_ms']):
            ts = r['ts_ms']
            fc = r.get('raw_w')
            if fc is None:
                fc = r.get('pv_w')
            if ts <= self.learned_until_ms or fc is None or fc < MIN_LEARN_W:
                continue
            if ts in skip:
                # Curtailed slot: skip learning but still advance the cursor so
                # it isn't reconsidered — the clipped PV is not the sun's fault.
                max_seen = max(max_seen, ts)
                continue
            act = actual_by_ts.get(ts)
            if act is None:
                continue
            ratio = min(max(act / fc, RATIO_MIN), RATIO_MAX)
            h = self._hour_of(ts)
            f = (1.0 - ALPHA) * self.factors[h] + ALPHA * ratio
            self.factors[h] = min(max(f, FACTOR_MIN), FACTOR_MAX)
            self.counts[h] += 1
            used += 1
            max_seen = max(max_seen, ts)
        self.learned_until_ms = max_seen
        return used

    # ── application ───────────────────────────────────────────────────────────

    def apply(self, records: list[dict]) -> list[dict]:
        """Return a copy of Solcast records with pv_w/p10_w/p90_w scaled by the
        factor of each record's local hour. The uncalibrated pv_w is preserved
        as raw_w (persisted to the DB) so later learning passes can compute the
        absolute actual/raw ratio. Untouched keys pass through."""
        out = []
        for r in records:
            f = self.factors[self._hour_of(r['ts_ms'])]
            c = dict(r)
            c['raw_w'] = c.get('pv_w')
            for k in ('pv_w', 'p10_w', 'p90_w'):
                if c.get(k) is not None:
                    c[k] = round(c[k] * f, 1)
            out.append(c)
        return out

    def summary(self) -> str:
        active = [(h, f) for h, (f, n) in enumerate(zip(self.factors, self.counts)) if n > 0]
        if not active:
            return 'uncalibrated (factors 1.0)'
        return ', '.join(f'{h:02d}h×{f:.2f}' for h, f in active)

    def state(self) -> dict:
        return {
            'factors': [round(f, 3) for f in self.factors],
            'counts':  list(self.counts),
            'learned_until_ms': self.learned_until_ms,
        }

    # ── persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path):
        path.write_text(json.dumps(self.state()))

    @classmethod
    def load(cls, path: Path) -> 'SolarCalibration':
        d = json.loads(path.read_text())
        c = cls()
        factors = d.get('factors')
        counts  = d.get('counts')
        if isinstance(factors, list) and len(factors) == 24:
            c.factors = [min(max(float(f), FACTOR_MIN), FACTOR_MAX) for f in factors]
        if isinstance(counts, list) and len(counts) == 24:
            c.counts = [int(n) for n in counts]
        c.learned_until_ms = int(d.get('learned_until_ms', 0))
        return c
