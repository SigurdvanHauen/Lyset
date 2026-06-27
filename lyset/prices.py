"""
Danish electricity prices — Strømligning API (stromligning.dk).

Fetches all-in electricity prices for the correct DSO tariff zone via postal
code lookup. Returns 15-minute resolution for current/historical data and
1-hour resolution for forecasts. Import price includes spot, network tariffs,
electricity tax, and VAT. Export price is estimated as the spot component
(electricity.value) minus the standard elafgift — configurable via env.

Environment variables:
  STROMLIGNING_API_KEY       — API key, Bearer scheme (required)
  STROMLIGNING_POSTAL_CODE   — Danish postal code for DSO auto-lookup (default: 5500)
  STROMLIGNING_SUPPLIER_ID   — Override DSO lookup with a known supplier ID
  PRICE_ELAFGIFT             — Elafgift DKK/kWh excl. VAT for export calc (default: 0.763)
  PRICE_POLL_INTERVAL        — Seconds between refreshes (default: 1800)
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

_BASE             = 'https://stromligning.dk/api'
_DEFAULT_POLL     = 1800.0   # 30 min
_DEFAULT_POSTAL   = '5500'


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


def find_supplier(api_key: str, postal_code: str) -> Optional[str]:
    """Return the first supplier ID for a Danish postal code, or None."""
    r = _session(api_key).get(
        f'{_BASE}/suppliers/find',
        params={'postalCode': int(postal_code)},
        timeout=15,
    )
    if r.status_code == 404:
        log.warning('Prices: no DSO found for postal code %s', postal_code)
        return None
    r.raise_for_status()
    data = r.json()
    if data:
        sid = data[0]['id']
        log.info('Prices: using supplier %s (%s)', sid, data[0].get('companyName', '?'))
        return sid
    return None


def fetch_prices(api_key: str, supplier_id: str) -> list[dict]:
    """Fetch all-in prices (yesterday → tomorrow + forecast) from Strømligning."""
    r = _session(api_key).get(
        f'{_BASE}/prices',
        params={
            'supplierId': supplier_id,
            'forecast':   'true',
        },
        timeout=20,
    )
    r.raise_for_status()
    body = r.json()
    return body.get('prices', [])


def _parse_records(records: list[dict]) -> list[dict]:
    """Convert raw API records to the internal price format."""
    elafgift = _env_float('PRICE_ELAFGIFT', 0.763)
    out = []
    for rec in records:
        date_str = rec.get('date', '')
        if not date_str:
            continue
        # Parse ISO UTC timestamp — trim to seconds first so this works on
        # Python < 3.11 which doesn't handle milliseconds in fromisoformat.
        try:
            dt    = datetime.strptime(date_str[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
            ts_ms = int(dt.timestamp()) * 1000
        except Exception:
            continue

        price = rec.get('price', {})
        elec  = rec.get('details', {}).get('electricity', {})

        import_price = price.get('total')            # DKK/kWh incl. VAT (all-in)
        elec_value   = elec.get('value')             # spot + elafgift, excl. VAT

        if import_price is None or elec_value is None:
            continue

        # Export ≈ spot price only (net-metering); subtract elafgift from the
        # electricity component (elafgift is bundled into electricity.value).
        # Can be negative during low/negative spot periods — keep the real value
        # so the UI can show when exporting costs money (IntelliCharge.ai trigger).
        export_price = round(elec_value - elafgift, 4)

        out.append({
            'ts':         ts_ms,
            'import':     round(import_price, 4),
            'export':     export_price,
            'spot_est':   round(elec_value, 4),   # spot + elafgift for tooltip
            'resolution': rec.get('resolution', '1h'),
            'forecast':   rec.get('forecast', False),
        })
    return out


class PriceWorker(threading.Thread):
    """Background thread — polls Strømligning and fires on_prices / on_status."""

    def __init__(
        self,
        api_key:       str,
        supplier_id:   Optional[str]  = None,
        postal_code:   str            = _DEFAULT_POSTAL,
        poll_interval: float          = _DEFAULT_POLL,
        on_prices:  Optional[Callable[[list[dict]], None]] = None,
        on_status:  Optional[Callable[[str, bool], None]]  = None,
    ):
        super().__init__(daemon=True, name='PriceWorker')
        self._api_key      = api_key
        self._supplier_id  = supplier_id
        self._postal_code  = postal_code
        self.poll_interval = poll_interval
        self._on_prices    = on_prices or (lambda d: None)
        self._on_status    = on_status or (lambda m, ok: None)
        self._running      = False

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        # Resolve DSO supplier on first run if not explicitly set
        if not self._supplier_id:
            try:
                self._supplier_id = find_supplier(self._api_key, self._postal_code)
            except Exception as exc:
                log.error('Prices: supplier lookup failed — %s', exc)
        if not self._supplier_id:
            self._on_status('Prices: no DSO found — check STROMLIGNING_POSTAL_CODE', False)
            return
        while self._running:
            self._fetch_once()
            self._sleep(self.poll_interval)

    def _fetch_once(self):
        try:
            raw     = fetch_prices(self._api_key, self._supplier_id)
            prices  = _parse_records(raw)
            if not prices:
                self._on_status('Prices: no records returned', False)
                return
            n_fore = sum(1 for p in prices if p['forecast'])
            log.info('Prices: %d records (%d forecast) from %s',
                     len(prices), n_fore, self._supplier_id)
            self._on_status(
                f'Prices: {len(prices)} records, {n_fore} forecast (Strømligning)', True
            )
            self._on_prices(prices)
        except Exception as exc:
            log.error('PriceWorker: %s', exc)
            self._on_status(f'Prices: error — {exc}', False)

    def _sleep(self, seconds: float):
        deadline = time.time() + seconds
        while self._running and time.time() < deadline:
            time.sleep(1)


def worker_from_env(**kwargs) -> Optional[PriceWorker]:
    """Build a PriceWorker from environment variables, or None if not configured."""
    api_key = os.getenv('STROMLIGNING_API_KEY', '').strip()
    if not api_key:
        log.info('Prices: STROMLIGNING_API_KEY not set — price polling disabled')
        return None

    supplier_id = os.getenv('STROMLIGNING_SUPPLIER_ID', '').strip() or None
    postal_code = os.getenv('STROMLIGNING_POSTAL_CODE', _DEFAULT_POSTAL).strip()
    interval    = _env_float('PRICE_POLL_INTERVAL', _DEFAULT_POLL)

    log.info('Prices: configured (postal=%s, supplier=%s, interval=%.0fs)',
             postal_code, supplier_id or 'auto', interval)
    return PriceWorker(
        api_key=api_key, supplier_id=supplier_id,
        postal_code=postal_code, poll_interval=interval, **kwargs,
    )
