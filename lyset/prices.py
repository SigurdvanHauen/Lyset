"""
Danish electricity prices — Strømligning API (stromligning.dk).

Fetches the bare Nord Pool spot price for the correct DSO tariff zone via postal
code lookup (15-minute resolution for current/historical data, 1-hour for
forecasts) and builds the IMPORT price from the user's own contract fees rather
than Strømligning's generic all-in figure:

    import = (spot + tillæg + transportbetaling + elafgift) × (1 + VAT)

Every fee is editable in Settings → Electricity prices so it can be kept in sync
with the provider. Export payout = bare spot (electricity.value, already the raw
spot — not tax-inclusive) minus small feed-in/balance tariffs — matching Vindstød's
surplus settlement, where elafgift and VAT do NOT apply.

Environment variables:
  STROMLIGNING_API_KEY       — API key, Bearer scheme (required)
  STROMLIGNING_POSTAL_CODE   — Danish postal code for DSO auto-lookup (default: 5500)
  STROMLIGNING_SUPPLIER_ID   — Override DSO lookup with a known supplier ID
  PRICE_TILLAEG_ORE          — Supplier markup on spot, øre/kWh (default: 10.00)
  PRICE_TRANSPORT_ORE        — Net transport tariff, øre/kWh (default: 28.93)
  PRICE_ELAFGIFT_ORE         — Electricity tax, øre/kWh excl. VAT (default: 1.00)
  PRICE_VAT_PCT              — VAT applied to spot + fees, % (default: 25)
  PRICE_EXPORT_FEE           — DKK/kWh feed-in/balance tariffs deducted from export (default: 0.033375)
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


def _parse_iso_utc_ms(date_str: str) -> Optional[int]:
    """
    Parse an ISO-8601 timestamp to UTC epoch milliseconds, RESPECTING any
    timezone offset the string carries.

    Strømligning sends slot timestamps that may be UTC ('...Z') or carry a
    local offset ('...+02:00').  The offset MUST be honoured: dropping it and
    assuming UTC shifts every price slot by the offset (2 h in Danish summer),
    which makes the controller act on the wrong hour's price.  A naive string
    (no offset) is assumed to be UTC.
    """
    s = date_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(date_str[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

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
    # details.electricity.value IS the bare Nord Pool spot price (it goes negative
    # midday on solar-glut days — proof it does NOT bundle elafgift, which is a
    # ≥0 consumption tax). The export payout is this spot minus small feed-in/
    # balance tariffs. Vindstød "salg af overskydende el": Energinet indfødning
    # 0.00625 + balance 0.006625 + netselskab indfødning 0.0105 + Vindstød balance
    # 0.01 ≈ 0.033375. Netselskab indfødningstarif varies by net area.
    export_fee = _env_float('PRICE_EXPORT_FEE', 0.033375)
    # IMPORT price is built from the user's own contract fees (Settings → Electricity
    # prices), not Strømligning's generic price.total — the provider's actual
    # per-kWh line items are added to the bare spot and VAT applied to the whole
    # thing. Fees are entered in øre/kWh (matching the bill) → convert to DKK.
    import_add_dkk = (
        _env_float('PRICE_TILLAEG_ORE',   10.00)   # supplier markup on spot
        + _env_float('PRICE_TRANSPORT_ORE', 28.93)  # net transport tariff
        + _env_float('PRICE_ELAFGIFT_ORE',   1.00)  # electricity tax (excl. VAT)
    ) / 100.0
    vat_mult = 1.0 + _env_float('PRICE_VAT_PCT', 25.0) / 100.0
    out = []
    for rec in records:
        date_str = rec.get('date', '')
        if not date_str:
            continue
        # Parse to UTC epoch ms, honouring any timezone offset in the string.
        ts_ms = _parse_iso_utc_ms(date_str)
        if ts_ms is None:
            continue

        elec  = rec.get('details', {}).get('electricity', {})
        spot_price = elec.get('value')               # bare Nord Pool spot, excl. VAT
        if spot_price is None:
            continue

        # Real import price the user pays: (spot + supplier markup + transport tariff
        # + elafgift) × VAT. Everything is per-kWh; spot can be negative but the fixed
        # fees usually keep the all-in import positive.
        import_price = (spot_price + import_add_dkk) * vat_mult

        # Export payout (what Vindstød actually pays): the bare spot minus the small
        # feed-in/balance tariffs. Elafgift and VAT are NOT part of export settlement
        # (Vindstød: "Netydelse og elafgift er ikke en del af afregning med salg af
        # strøm"). Naturally goes negative when the spot itself is negative.
        export_price = round(spot_price - export_fee, 4)

        out.append({
            'ts':         ts_ms,
            'import':     round(import_price, 4),
            'export':     export_price,
            'spot_est':   round(spot_price, 4),   # bare spot, for tooltip
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
