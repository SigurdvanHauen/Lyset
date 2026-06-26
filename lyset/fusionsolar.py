"""
Huawei FusionSolar web-portal API — EV charger monitoring.

Direct implementation of the login/API flow, no third-party FusionSolar
library. Credentials are read from environment variables:

  FUSIONSOLAR_USER         — FusionSolar login email
  FUSIONSOLAR_PASS         — FusionSolar login password
  FUSIONSOLAR_SUBDOMAIN    — portal subdomain (default: eu5)
  FUSIONSOLAR_POLL_INTERVAL — seconds between polls (default: 120)
"""

from __future__ import annotations

import base64
import logging
import os
import time
import threading
import urllib.parse
from typing import Optional, Callable

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

log = logging.getLogger(__name__)

_DEFAULT_SUBDOMAIN   = 'eu5'
_POLL_INTERVAL       = 120.0


# ── Crypto ────────────────────────────────────────────────────────────────────

def _encrypt_password(key_data: dict, password: str) -> str:
    """RSA-OAEP-SHA384 encryption, matching Huawei FusionSolar JS client."""
    if not key_data.get('enableEncrypt'):
        return password

    public_key = serialization.load_pem_public_key(
        key_data['pubKey'].encode(), backend=default_backend()
    )
    value_encode = urllib.parse.quote(password)
    encrypt_value = ''

    for i in range(len(value_encode) // 270 + 1):
        chunk = value_encode[i * 270:(i + 1) * 270]
        if not chunk:
            break
        encrypted = public_key.encrypt(
            chunk.encode(),
            asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=hashes.SHA384()),
                algorithm=hashes.SHA384(),
                label=None,
            ),
        )
        if encrypt_value:
            encrypt_value += '00000001'
        encrypt_value += base64.b64encode(encrypted).decode('utf-8')

    return encrypt_value + key_data.get('version', '')


# ── Session ───────────────────────────────────────────────────────────────────

class FusionSolarSession:
    """Minimal FusionSolar web-portal client."""

    # Login always hits eu5 regardless of region
    _LOGIN_HOST = 'https://eu5.fusionsolar.huawei.com'

    def __init__(self, username: str, password: str, subdomain: str = _DEFAULT_SUBDOMAIN):
        self._username   = username
        self._password   = password
        self._subdomain  = subdomain
        self._session    = requests.Session()
        self._session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
            )
        })
        self.company_id: Optional[str] = None

    @property
    def _base(self) -> str:
        return f'https://{self._subdomain}.fusionsolar.huawei.com'

    def login(self) -> None:
        """Log in and populate session cookies + CSRF token. Raises on failure."""
        # 1. Get public key / encryption config
        r = self._session.get(f'{self._LOGIN_HOST}/unisso/pubkey', timeout=15)
        r.raise_for_status()
        key_data = r.json()

        # 2. Build login request
        if key_data.get('enableEncrypt'):
            url    = f'{self._LOGIN_HOST}/unisso/v3/validateUser.action'
            params = {
                'timeStamp': key_data['timeStamp'],
                'nonce':     os.urandom(16).hex(),
            }
            password = _encrypt_password(key_data, self._password)
        else:
            url    = f'{self._LOGIN_HOST}/unisso/v2/validateUser.action'
            params = {
                'decision': 1,
                'service':  f'{self._base}/unisess/v1/auth?service=/netecowebext/home/index.html#/LOGIN',
            }
            password = self._password

        r = self._session.post(
            url, params=params, timeout=15,
            json={'organizationName': '', 'username': self._username, 'password': password},
        )
        r.raise_for_status()
        resp = r.json()

        # 3. Handle two-step login (errorCode 470 redirects to finalise session)
        if resp.get('errorCode') == '470':
            target = f"{self._LOGIN_HOST}{resp['respMultiRegionName'][1]}"
            self._session.get(target, timeout=15).raise_for_status()
        elif resp.get('errorMsg'):
            raise RuntimeError(f"Login rejected: {resp['errorMsg']}")

        # 4. Fetch CSRF token (roarand). Not all regions support this endpoint —
        #    skip silently if it returns 4xx.
        try:
            r2 = self._session.get(
                f'{self._base}/rest/dpcloud/auth/v1/keep-alive', timeout=10
            )
            if r2.status_code == 200:
                d2 = r2.json()
                if d2.get('payload'):
                    self._session.headers['roarand'] = d2['payload']
                    log.debug('FusionSolar: roarand CSRF token set')
        except Exception as exc:
            log.debug('FusionSolar: keep-alive skipped (%s)', exc)

        # 5. Verify login + get company DN (needed for device queries)
        r3 = self._session.get(
            f'{self._base}/rest/neteco/web/organization/v2/company/current',
            params={'_': _ts()}, timeout=15,
        )
        if r3.status_code in (400, 500):
            raise RuntimeError('company/current failed — wrong subdomain?')
        r3.raise_for_status()
        d3 = r3.json()
        if 'data' not in d3:
            raise RuntimeError('company/current returned no data — subdomain may be wrong')
        self.company_id = d3['data'].get('moDn') or d3['data'].get('id')
        log.info('FusionSolar: logged in, company_id=%s', self.company_id)

    def get_station_list(self) -> list[dict]:
        r = self._session.post(
            f'{self._base}/rest/pvms/web/station/v1/station/station-list',
            timeout=20,
            json={
                'curPage': 1, 'pageSize': 100,
                'gridConnectedTime': '',
                'queryTime': _ts(),
                'timeZone': 2,
                'sortId': 'createTime', 'sortDir': 'DESC',
                'locale': 'en_US',
            },
        )
        r.raise_for_status()
        d = r.json()
        if not d.get('success'):
            log.warning('FusionSolar: station-list not success: %s', d)
        return d.get('data', {}).get('list', [])

    def get_devices_for_station(self, station_dn: str) -> list[dict]:
        r = self._session.get(
            f'{self._base}/rest/pvms/web/device/v1/device/station-device',
            params={'stationDn': station_dn, '_': _ts()}, timeout=20,
        )
        r.raise_for_status()
        d = r.json()
        devices = d.get('data', d.get('list', []))
        return devices if isinstance(devices, list) else []

    def get_device_real_kpi(self, device_dn: str, dev_type_id) -> Optional[dict]:
        r = self._session.get(
            f'{self._base}/rest/pvms/web/device/v1/device/real-kpi',
            params={'deviceDn': device_dn, 'devTypeId': dev_type_id, '_': _ts()},
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()
        kpis = d.get('data', d.get('kpis', {}))
        if isinstance(kpis, list):
            return kpis[0] if kpis else None
        return kpis if isinstance(kpis, dict) and kpis else None


def _ts() -> int:
    return round(time.time() * 1000)


# ── Worker thread ─────────────────────────────────────────────────────────────

# Known EV charger devTypeId values (from Huawei community threads)
_EV_TYPE_IDS = {38, 39, 47}

class ChargerWorker(threading.Thread):
    """
    Background thread that polls FusionSolar for EV charger data.

    Callbacks (called from worker thread):
        on_data(dict)           — latest charger KPI snapshot
        on_status(str, bool)    — human-readable message + ok flag
    """

    def __init__(
        self,
        username:      str,
        password:      str,
        subdomain:     str   = _DEFAULT_SUBDOMAIN,
        poll_interval: float = _POLL_INTERVAL,
        on_data:   Optional[Callable[[dict], None]] = None,
        on_status: Optional[Callable[[str, bool], None]] = None,
    ):
        super().__init__(daemon=True, name='ChargerWorker')
        self._username     = username
        self._password     = password
        self._subdomain    = subdomain
        self.poll_interval = poll_interval
        self._on_data      = on_data   or (lambda d: None)
        self._on_status    = on_status or (lambda msg, ok: None)
        self._running      = False
        self._fs: Optional[FusionSolarSession] = None

    def stop(self):
        self._running = False

    # ── Login ─────────────────────────────────────────────────────────────────

    def _login(self) -> bool:
        try:
            log.info('FusionSolar: logging in as %s (subdomain=%s)', self._username, self._subdomain)
            fs = FusionSolarSession(self._username, self._password, self._subdomain)
            fs.login()
            self._fs = fs
            return True
        except Exception as exc:
            log.error('FusionSolar: login failed — %s', exc)
            self._fs = None
            return False

    # ── Discovery + poll ──────────────────────────────────────────────────────

    def _discover_and_poll(self) -> Optional[dict]:
        fs = self._fs

        # Get station list
        try:
            stations = fs.get_station_list()
            log.info('FusionSolar: %d station(s)', len(stations))
        except Exception as exc:
            log.warning('FusionSolar: station-list failed — %s', exc)
            return None

        if not stations:
            log.warning('FusionSolar: no stations found')
            return None

        # Find charger across all stations
        charger = None
        charger_dn = None
        charger_type = None

        for station in stations:
            sdn = station.get('dn', '')
            try:
                devices = fs.get_devices_for_station(sdn)
                log.info('FusionSolar: station %s — %d device(s)', sdn, len(devices))
                for d in devices:
                    tid = d.get('devTypeId')
                    log.info('  devTypeId=%-4s  dn=%-36s  name=%s',
                             tid, d.get('dn', '?'), d.get('devName', '?'))
                    if charger is None:
                        if tid in _EV_TYPE_IDS:
                            charger = d
                        else:
                            name = (d.get('devName') or '').lower()
                            if any(k in name for k in ('charg', 'ev', 'wallbox', 'scharger')):
                                charger = d
            except Exception as exc:
                log.warning('FusionSolar: device list failed for station %s — %s', sdn, exc)

        if charger is None:
            log.warning('FusionSolar: no EV charger found (checked devTypeId in %s and name match)', _EV_TYPE_IDS)
            return None

        charger_dn   = charger.get('dn', '')
        charger_type = charger.get('devTypeId', '')
        log.info('FusionSolar: charger found — typeId=%s dn=%s name=%s',
                 charger_type, charger_dn, charger.get('devName', '?'))

        # Poll real-time KPI
        try:
            kpis = fs.get_device_real_kpi(charger_dn, charger_type)
            if kpis:
                log.debug('FusionSolar: charger fields: %s', sorted(kpis.keys()))
                return kpis
            log.warning('FusionSolar: charger KPI response had no data')
            return None
        except Exception as exc:
            log.error('FusionSolar: charger KPI fetch failed — %s', exc)
            return None

    # ── Thread main ───────────────────────────────────────────────────────────

    def run(self):
        self._running = True
        self._on_status('FusionSolar: connecting…', True)

        while self._running:
            if self._fs is None:
                if not self._login():
                    self._on_status('FusionSolar: login failed — check Log tab', False)
                    self._sleep(60)
                    continue

            try:
                data = self._discover_and_poll()
                if data:
                    self._on_status('FusionSolar: OK', True)
                    self._on_data(data)
                else:
                    self._on_status('FusionSolar: no charger data — check Log tab', False)
            except Exception as exc:
                log.error('FusionSolar: poll error — %s', exc, exc_info=True)
                self._on_status(f'FusionSolar error: {exc}', False)
                self._fs = None  # force re-login next cycle

            self._sleep(self.poll_interval)

    def _sleep(self, seconds: float):
        deadline = time.time() + seconds
        while self._running and time.time() < deadline:
            time.sleep(1)


# ── Factory ───────────────────────────────────────────────────────────────────

def worker_from_env(**kwargs) -> Optional[ChargerWorker]:
    """Build a ChargerWorker from environment variables, or None if not configured."""
    user = os.getenv('FUSIONSOLAR_USER', '').strip()
    pw   = os.getenv('FUSIONSOLAR_PASS', '').strip()
    if not user or not pw:
        log.info('FusionSolar: FUSIONSOLAR_USER/PASS not set — EV charger polling disabled')
        return None

    subdomain = os.getenv('FUSIONSOLAR_SUBDOMAIN', _DEFAULT_SUBDOMAIN).strip()
    interval  = float(os.getenv('FUSIONSOLAR_POLL_INTERVAL', str(_POLL_INTERVAL)))

    log.info('FusionSolar: worker configured (subdomain=%s, interval=%.0fs)', subdomain, interval)
    return ChargerWorker(
        username=user, password=pw, subdomain=subdomain,
        poll_interval=interval, **kwargs,
    )
