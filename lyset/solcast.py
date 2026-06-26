"""
Solcast solar production forecast — rooftop site API.

Fetches pv_estimate (kW) for 30-min periods across one or more sites,
sums them, and converts to Watts so values are directly comparable with
the active_power register (W).

Free Hobbyist plan: 10 API calls/day total.
  1 site  → 10 fetches/day max → SOLCAST_POLL_INTERVAL ≥ 8640 s
  2 sites → 5 fetches/day max → SOLCAST_POLL_INTERVAL ≥ 17280 s
Default is 21600 s (6 h), which keeps 2 sites well within the limit.

Environment variables:
  SOLCAST_API_KEY       — Bearer API key (required)
  SOLCAST_RESOURCE_ID   — Comma-separated site UUID(s). Auto-discovered from
                          all registered sites if omitted (costs 1 call/startup).
  SOLCAST_POLL_INTERVAL — Seconds between refreshes (default: 21600 = 6 h)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

_BASE         = 'https://api.solcast.com.au'
_DEFAULT_POLL = 21600.0  # 6 h — safe for 2 sites on free tier


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'Authorization': f'Bearer {api_key}',
        'Accept':        'application/json',
    })
    return s


def find_resource_ids(api_key: str) -> list[str]:
    """Return resource IDs for all registered rooftop sites."""
    r = _session(api_key).get(f'{_BASE}/rooftop_sites', timeout=15)
    if r.status_code == 404:
        log.warning('Solcast: no rooftop sites found')
        return []
    r.raise_for_status()
    data  = r.json()
    sites = data.get('rooftop_sites', data if isinstance(data, list) else [])
    ids   = []
    for site in sites:
        rid = site.get('resource_id') or site.get('id')
        if rid:
            log.info('Solcast: found site "%s" (%s)', site.get('name', '?'), rid)
            ids.append(rid)
    if ids:
        log.info('Solcast: set SOLCAST_RESOURCE_ID=%s to skip auto-discovery on restarts',
                 ','.join(ids))
    return ids


def fetch_forecast(api_key: str, resource_id: str, hours: int = 48) -> list[dict]:
    """Fetch pv forecast records for a single rooftop site."""
    r = _session(api_key).get(
        f'{_BASE}/rooftop_sites/{resource_id}/forecasts',
        params={'format': 'json', 'hours': hours},
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get('forecasts', [])


def _parse_forecast(records: list[dict]) -> list[dict]:
    """Convert raw Solcast records: kW → W, period_end → ts_ms."""
    out = []
    for rec in records:
        period_end = rec.get('period_end', '')
        if not period_end:
            continue
        try:
            dt    = datetime.strptime(period_end[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp()) * 1000
        except Exception:
            continue

        pv_kw  = rec.get('pv_estimate')
        p10_kw = rec.get('pv_estimate10')
        p90_kw = rec.get('pv_estimate90')

        if pv_kw is None:
            continue

        out.append({
            'ts_ms': ts_ms,
            'pv_w':  round(pv_kw  * 1000, 1),
            'p10_w': round(p10_kw * 1000, 1) if p10_kw is not None else None,
            'p90_w': round(p90_kw * 1000, 1) if p90_kw is not None else None,
        })
    return out


def _combine(site_records: list[list[dict]]) -> list[dict]:
    """Sum pv_w/p10_w/p90_w across multiple sites, aligned by ts_ms."""
    totals: dict[int, dict] = {}
    has_bands = True
    for records in site_records:
        for r in records:
            ts = r['ts_ms']
            if ts not in totals:
                totals[ts] = {'ts_ms': ts, 'pv_w': 0.0, 'p10_w': 0.0, 'p90_w': 0.0}
            totals[ts]['pv_w'] += r['pv_w']
            if r.get('p10_w') is not None:
                totals[ts]['p10_w'] += r['p10_w']
            else:
                has_bands = False
            if r.get('p90_w') is not None:
                totals[ts]['p90_w'] += r['p90_w']
            else:
                has_bands = False

    out = sorted(totals.values(), key=lambda x: x['ts_ms'])
    if not has_bands:
        for r in out:
            r['p10_w'] = None
            r['p90_w'] = None
    return out


class SolcastWorker(threading.Thread):
    """Background thread: polls Solcast for all configured sites and fires on_forecast."""

    def __init__(
        self,
        api_key:       str,
        resource_ids:  Optional[list[str]] = None,
        poll_interval: float               = _DEFAULT_POLL,
        on_forecast:   Optional[Callable[[list[dict]], None]] = None,
        on_status:     Optional[Callable[[str, bool], None]]  = None,
    ):
        super().__init__(daemon=True, name='SolcastWorker')
        self._api_key      = api_key
        self._resource_ids = resource_ids or []
        self.poll_interval = poll_interval
        self._on_forecast  = on_forecast or (lambda d: None)
        self._on_status    = on_status   or (lambda m, ok: None)
        self._running      = False

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        if not self._resource_ids:
            try:
                self._resource_ids = find_resource_ids(self._api_key)
            except Exception as exc:
                log.error('Solcast: site discovery failed — %s', exc)
        if not self._resource_ids:
            self._on_status('Solcast: no sites found — register at toolkit.solcast.com.au', False)
            return

        n = len(self._resource_ids)
        calls_per_day = (86400 / self.poll_interval) * n
        if calls_per_day > 9:
            log.warning(
                'Solcast: %d site(s) × %.0f polls/day ≈ %.0f API calls/day '
                '(free tier: 10). Increase SOLCAST_POLL_INTERVAL to ≥ %.0f s.',
                n, 86400 / self.poll_interval, calls_per_day,
                86400 / (9 / n),
            )

        while self._running:
            extra = self._fetch_once()
            self._sleep(self.poll_interval + extra)

    def _fetch_once(self) -> float:
        """Fetch and push forecast. Returns extra seconds to sleep on rate-limit, else 0."""
        try:
            all_site_records = []
            for rid in self._resource_ids:
                raw     = fetch_forecast(self._api_key, rid)
                records = _parse_forecast(raw)
                all_site_records.append(records)
                log.debug('Solcast: site %s → %d periods', rid, len(records))

            combined = _combine(all_site_records)
            if not combined:
                self._on_status('Solcast: no forecast records returned', False)
                return 0

            n = len(self._resource_ids)
            log.info('Solcast: %d combined periods from %d site(s)', len(combined), n)
            self._on_status(f'Solcast: {len(combined)} periods, {n} site(s) summed', True)
            self._on_forecast(combined)
            return 0
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get('Retry-After', 86400))
                reset_dt    = datetime.fromtimestamp(time.time() + retry_after)
                log.warning(
                    'Solcast: rate limit hit (10 calls/day free tier). '
                    'Retry after %s. Extra sleep: %d s.',
                    reset_dt.strftime('%H:%M:%S'), retry_after,
                )
                self._on_status(
                    f'Solcast: rate limit — retry after {reset_dt.strftime("%H:%M")}', False
                )
                return retry_after
            log.error('SolcastWorker: %s', exc)
            self._on_status(f'Solcast: error — {exc}', False)
            return 0
        except Exception as exc:
            log.error('SolcastWorker: %s', exc)
            self._on_status(f'Solcast: error — {exc}', False)
            return 0

    def _sleep(self, seconds: float):
        deadline = time.time() + seconds
        while self._running and time.time() < deadline:
            time.sleep(1)


def worker_from_env(**kwargs) -> Optional[SolcastWorker]:
    """Build a SolcastWorker from environment variables, or None if not configured."""
    api_key = os.getenv('SOLCAST_API_KEY', '').strip()
    if not api_key:
        log.info('Solcast: SOLCAST_API_KEY not set — solar forecast disabled')
        return None

    rid_env      = os.getenv('SOLCAST_RESOURCE_ID', '').strip()
    resource_ids = [r.strip() for r in rid_env.split(',') if r.strip()] if rid_env else []
    interval     = _env_float('SOLCAST_POLL_INTERVAL', _DEFAULT_POLL)

    log.info('Solcast: configured (%s, interval=%.0fs)',
             f'{len(resource_ids)} site(s) from env' if resource_ids else 'auto-discover',
             interval)
    return SolcastWorker(
        api_key=api_key, resource_ids=resource_ids,
        poll_interval=interval, **kwargs,
    )
