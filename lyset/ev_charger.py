"""
FusionSolar cloud polling for the EV charger (SCharger-22KT-S0) — built from
what was discovered on 2026-07-02 against the real account:

  Device: type 'Charging Pile', name 'EV Charger', dn e.g. 'NE=261777682'
    (discovered dynamically each run via a broad, unfiltered device list
    query — see find_charger_device() — rather than hardcoded, since a
    factory-reset or re-pair could change the dn).

  get_real_time_data(dn) on an IDLE charger (no EV connected) returns:
    {"data": [
        {"status": 1},
        {"groupName": "Asset Information", "signals": [
            {"name": "Software Version", "realValue": "FusionCharge V100R..."},
            {"name": "Hardware Version", "realValue": "B"}]},
        {"groupName": "Basic Information", "signals": [
            {"name": "Rated Power", "realValue": "22.0", "unit": "kW"},
            {"name": "Model", "realValue": "SCharger-22KT-S0"},
            {"name": "Total Energy Charged", "realValue": "156.489", "unit": "kWh"},
            {"name": "Bluetooth Name", "realValue": "..."}]},
        {"pv2mppt": false}],
     "success": true, ...}

  NOTABLE ABSENCE: no live charging power (W), no plug/session status beyond
  the bare top-level `status` code (meaning unconfirmed — Huawei's generic
  device dashboards commonly use 0=offline/1=online/2=fault but that is NOT
  verified for this device type), no per-session energy. "Total Energy
  Charged" is a LIFETIME cumulative counter (same semantics as the inverter's
  total_yield) — "energy charged today" is derived by this app from the
  poll history's daily min/max, not served directly by the API.
  It is unknown whether more signal groups (live power, a richer status)
  appear once a session is actually active — no EV was connected in Denmark
  yet when this was written. _parse_realtime() below stores EVERY signal it
  sees (not just the ones enumerated above) precisely so a new group
  appearing during a real session is captured without a code change; check
  the `raw` field on unexpected records.

  Login is the same reverse-engineered flow the FusionSolar web app uses
  (fusion-solar-py), not the Northbound OpenAPI — there is NO officially
  documented rate limit for this path, and community reports describe a
  single-session login limit and occasional CAPTCHA challenges on unusual
  login patterns. This worker therefore polls conservatively (minutes, not
  seconds) and backs off hard on repeated failures rather than retrying
  tightly, and reuses one session (via keep_alive()/is_session_active())
  instead of logging in fresh every poll.

NO WRITE / CONTROL SUPPORT: no start/stop/limit-current endpoint for the
charger has been discovered. fusion_solar_py's only write method
(active_power_control) targets the INVERTER's dongle via a generic
"set-config-signals" call; the same endpoint might work against the
charger's dn with an unknown signal id, but guessing signal ids and writing
them to a live 22 kW charger is not something to do blind. If control is
wanted later, the safe path is capturing the real request FusionSolar's
own app sends when a human starts/stops a charge (browser dev tools while
using the app) and implementing against THAT verified endpoint.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_MIN_POLL_INTERVAL_S = 60.0     # floor — never hammer the login/API faster than this
_LOGIN_BACKOFF_S     = 900.0    # after a login failure, wait this long before retrying
_REDISCOVER_EVERY     = 20      # re-run the device-list query every N polls (dn could
                                # change after a charger re-pair; cheap to re-check rarely)


def list_devices(client) -> list[dict]:
    """Full, UNFILTERED device list for the logged-in account.

    Bypasses fusion_solar_py's get_device_ids(), which pre-filters to a
    hardcoded set of mocType codes that does not necessarily include the
    charger (device type 'Charging Pile', confirmed absent from that list
    by testing). Falls back to the library's narrow method if the broad
    query fails for any reason (API shape change, permissions, etc.).
    Each dict: {dn, mocType, mocTypeName, name}.
    """
    try:
        r = client._session.get(
            url=f'https://{client._huawei_subdomain}.fusionsolar.huawei.com'
                '/rest/neteco/web/config/device/v1/device-list',
            params={'conditionParams.parentDn': client._company_id,
                   '_': int(time.time() * 1000)},
        )
        r.raise_for_status()
        data = r.json().get('data', [])
        return [
            {'dn': d.get('dn'), 'mocType': d.get('mocType'),
             'mocTypeName': d.get('mocTypeName'), 'name': d.get('name') or d.get('aliasName')}
            for d in data
        ]
    except Exception as exc:
        log.warning('EVCharger: broad device query failed (%s), falling back to narrow query', exc)
        return [{'dn': d['deviceDn'], 'mocType': None, 'mocTypeName': d['type'], 'name': None}
                for d in client.get_device_ids()]


def find_charger_device(client) -> Optional[dict]:
    """Return the first device whose type name looks like a charger, or None."""
    for d in list_devices(client):
        if d.get('mocTypeName') and 'charg' in d['mocTypeName'].lower():
            return d
    return None


def _to_number(raw) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _snake(name: str) -> str:
    return name.strip().lower().replace(' ', '_').replace('-', '_')


def parse_realtime(payload: dict) -> dict:
    """Flatten a get_real_time_data() response into {snake_case_name: value},
    plus 'status' (top-level device status code, meaning unconfirmed) and
    'raw' (the untouched payload, so a signal this app doesn't recognise yet
    is still visible in the DB/UI rather than silently dropped)."""
    out: dict = {'raw': payload}
    for entry in payload.get('data', []):
        if not isinstance(entry, dict):
            continue
        if 'status' in entry and 'groupName' not in entry:
            out['status'] = entry['status']
            continue
        for sig in entry.get('signals', []):
            name = sig.get('name')
            if not name:
                continue
            key = _snake(name)
            val = sig.get('realValue', sig.get('value'))
            num = _to_number(val)
            out[key] = num if num is not None else val
            unit = sig.get('unit')
            if unit:
                out[f'{key}_unit'] = unit
    return out


class EVChargerWorker(threading.Thread):
    """Background thread: keeps one FusionSolar cloud session alive and polls
    the EV charger's real-time data on an interval."""

    def __init__(
        self,
        username: str,
        password: str,
        subdomain: str = 'region01eu5',
        poll_interval: float = 300.0,
        on_data: Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[str, bool], None]] = None,
    ):
        super().__init__(daemon=True, name='EVChargerWorker')
        self._username = username
        self._password = password
        self._subdomain = subdomain
        self._poll_interval = max(poll_interval, _MIN_POLL_INTERVAL_S)
        self._on_data = on_data or (lambda d: None)
        self._on_status = on_status or (lambda m, ok: None)
        self._running = False
        self._client = None
        self._charger_dn: Optional[str] = None
        self._poll_count = 0

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        while self._running:
            delay = self._poll_interval
            try:
                self._tick()
            except Exception as exc:
                log.error('EVChargerWorker: unexpected error — %s', exc, exc_info=True)
                self._on_status(f'EV charger: error — {exc}', False)
                # An error here is almost always a dead/kicked cloud session
                # that is_session_active() still vouched for (fusion_solar_py's
                # "Failed to reset session and login again"). Drop the client so
                # the next tick does a genuinely fresh login, and back off hard
                # — rapid re-login attempts are what provoke Huawei's CAPTCHA,
                # which then makes every subsequent login fail the same way.
                self._client = None
                self._charger_dn = None
                delay = max(_LOGIN_BACKOFF_S, self._poll_interval)
            self._sleep(delay)

    def _sleep(self, seconds: float):
        deadline = time.time() + seconds
        while self._running and time.time() < deadline:
            time.sleep(1)

    def _ensure_login(self) -> bool:
        from fusion_solar_py.client import FusionSolarClient
        from fusion_solar_py.exceptions import FusionSolarException

        if self._client is not None:
            try:
                if self._client.is_session_active():
                    return True
            except Exception:
                pass
            self._client = None

        try:
            self._client = FusionSolarClient(
                self._username, self._password, huawei_subdomain=self._subdomain,
            )
            self._charger_dn = None  # force re-discovery on a fresh session
            log.info('EVChargerWorker: logged in')
            self._on_status('EV charger: connected to FusionSolar cloud', True)
            return True
        except Exception as exc:
            log.warning('EVChargerWorker: login failed — %s', exc)
            self._on_status(f'EV charger: login failed — {exc}', False)
            self._client = None
            return False

    def _tick(self):
        if not self._username or not self._password:
            self._on_status('EV charger: no FusionSolar credentials configured', False)
            self._sleep(_LOGIN_BACKOFF_S - self._poll_interval)  # avoid a tight loop
            return

        if not self._ensure_login():
            self._sleep(_LOGIN_BACKOFF_S - self._poll_interval)
            return

        if self._charger_dn is None or self._poll_count % _REDISCOVER_EVERY == 0:
            device = find_charger_device(self._client)
            if device is None:
                log.warning('EVChargerWorker: no charger device found on this account')
                self._on_status('EV charger: no charging-pile device found on this FusionSolar account', False)
                self._charger_dn = None
                return
            self._charger_dn = device['dn']

        self._poll_count += 1
        payload = self._client.get_real_time_data(self._charger_dn)
        parsed = parse_realtime(payload)
        parsed['_timestamp'] = time.time()
        parsed['_dn'] = self._charger_dn
        self._on_data(parsed)
        self._on_status('EV charger: OK', True)


def worker_from_env(**kwargs) -> Optional['EVChargerWorker']:
    """Build an EVChargerWorker from environment variables, or None if the
    FusionSolar credentials aren't configured."""
    import os

    username = os.getenv('FUSIONSOLAR_USERNAME', '').strip()
    password = os.getenv('FUSIONSOLAR_PASSWORD', '')
    if not username or not password:
        log.info('EVCharger: FUSIONSOLAR_USERNAME/PASSWORD not set — charger polling disabled')
        return None

    import re
    subdomain = re.sub(r'[^A-Za-z0-9-]', '', os.getenv('FUSIONSOLAR_SUBDOMAIN', '')) or 'region01eu5'
    try:
        poll_interval = float(os.getenv('FUSIONSOLAR_EV_POLL_INTERVAL', '300'))
    except (TypeError, ValueError):
        poll_interval = 300.0

    return EVChargerWorker(
        username=username, password=password, subdomain=subdomain,
        poll_interval=poll_interval, **kwargs,
    )
