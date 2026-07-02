"""
EV charger discovery/experimentation — SCharger-22KT-S0 (FusionCharge).

This is the backend for the "EV Charger" tab, a development area for figuring
out how to get charging visibility into Lyset. History that shapes what's
here:

  Direct Modbus TCP to the charger (port 502, documented by Huawei) and
  Modbus via the inverter's SDongle (as a sub-device, the way wlcrs/
  huawei-solar-lib reads SChargers hanging off an EMMA) were both tried and
  both failed conclusively: the charger accepts a TCP connection but drops it
  the instant a real Modbus request arrives, on every device id tried,
  matching what other SCharger owners report (HA forum, evcc #10262 — "only
  its managing dongle may be Modbus master"). That code has been removed;
  don't re-add it without new evidence the firmware behaves differently.

  The remaining path is the FusionSolar cloud — the same login the FusionSolar
  app/website uses (fusion-solar-py), NOT the Northbound OpenAPI (that needs a
  separate account grant from Huawei support and may not be available on a
  residential plant).

PROBE: login, list plants, list ALL devices — bypassing fusion_solar_py's
  get_device_ids(), which pre-filters to a hardcoded set of mocType codes
  that may not include the SCharger — then fetch real-time data for whatever
  device looks like a charger. This determines the exact device id and field
  names to poll going forward; it does not itself set up ongoing polling.

Every step emits timestamped log events through a callback (server pushes
them over the WebSocket and persists to the ev_probe_log table) so progress
is visible live and the run can be exported for offline analysis.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

TESTS = [
    ('c1_login',       'C1 · FusionSolar cloud login'),
    ('c2_plants',       'C2 · Plant list (cloud)'),
    ('c3_devices',      'C3 · Full device list (cloud, unfiltered)'),
    ('c4_charger_kpi',  'C4 · Charger real-time data (cloud)'),
]


class EVProbe:
    def __init__(self, on_event: Callable[[dict], None]):
        self._on_event = on_event
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.run_ts: float = 0.0
        self.log_lines: list[dict] = []
        self.results: dict[str, dict] = {}
        self._reset_results()

    # ── event plumbing ────────────────────────────────────────────────────────

    def _reset_results(self):
        self.results = {tid: {'test': tid, 'name': name, 'status': 'pending', 'detail': ''}
                        for tid, name in TESTS}

    def _emit(self, ev: dict):
        ev['t'] = time.time()
        try:
            self._on_event(ev)
        except Exception as exc:  # never let a UI hiccup kill the probe
            log.warning('EVProbe event callback failed: %s', exc)

    def _log(self, level: str, msg: str):
        entry = {'kind': 'log', 'level': level, 'msg': msg}
        self.log_lines.append({'t': time.time(), 'level': level, 'msg': msg})
        log.info('EVProbe: %s', msg)
        self._emit(entry)

    def _result(self, tid: str, status: str, detail: str = ''):
        r = self.results[tid]
        r['status'] = status
        if detail:
            r['detail'] = detail
        self._emit({'kind': 'result', **r})

    def state(self) -> dict:
        return {
            'running': self.running,
            'run_ts':  self.run_ts,
            'results': [self.results[tid] for tid, _ in TESTS],
            'log':     self.log_lines[-800:],
        }

    # ── public control ────────────────────────────────────────────────────────

    def start(self, fs_username: str, fs_password: str,
              fs_subdomain: str = 'region01eu5') -> bool:
        """Kick off a probe run in a background thread. Returns False if one is
        already running."""
        if self.running:
            return False
        self.running = True
        self.run_ts = time.time()
        self.log_lines = []
        self._reset_results()
        self._thread = threading.Thread(
            target=self._run, args=(fs_username, fs_password, fs_subdomain),
            daemon=True, name='EVProbe',
        )
        self._thread.start()
        return True

    # ── probe implementation ──────────────────────────────────────────────────

    def _run(self, fs_username: str, fs_password: str, fs_subdomain: str):
        self._emit({'kind': 'status', 'running': True})
        try:
            self._log('info', '=== EV charger cloud probe started ===')
            self._probe_cloud(fs_username, fs_password, fs_subdomain)
            self._log('info', '=== Probe finished ===')
            passed = [r['name'] for r in self.results.values() if r['status'] == 'pass']
            if passed:
                self._log('ok', 'WORKING: ' + '; '.join(passed))
            else:
                self._log('warn', 'No working path found. See the log above for the specific reason.')
        except Exception as exc:
            self._log('error', f'Probe crashed: {exc}')
            log.exception('EVProbe crashed')
        finally:
            self.running = False
            self._emit({'kind': 'status', 'running': False})

    # ---- cloud probe ----

    def _probe_cloud(self, username: str, password: str, subdomain: str):
        self._result('c1_login', 'running')
        if not username or not password:
            for tid in ('c1_login', 'c2_plants', 'c3_devices', 'c4_charger_kpi'):
                self._result(tid, 'skipped', 'no FusionSolar credentials configured')
            self._log('warn', 'C1: skipped — set FusionSolar username/password in Settings → '
                              'EV charger (experimental) to run this probe.')
            return

        try:
            from fusion_solar_py.client import FusionSolarClient
            from fusion_solar_py.exceptions import (
                AuthenticationException, CaptchaRequiredException, FusionSolarException,
            )
        except ImportError:
            self._result('c1_login', 'fail', 'fusion-solar-py not installed')
            for tid in ('c2_plants', 'c3_devices', 'c4_charger_kpi'):
                self._result(tid, 'skipped', 'login unavailable')
            self._log('error', 'C1: fusion-solar-py package missing — '
                              'pip install -r requirements.txt on the server, then restart.')
            return

        self._log('info', f'C1: logging into FusionSolar cloud as {username} '
                          f'(subdomain {subdomain})…')
        # fusion_solar_py's requests calls carry no timeout — guard the whole
        # login+query sequence with a blunt socket-level default so a stalled
        # Huawei endpoint can't hang this probe thread forever.
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(20)
        client = None
        try:
            client = FusionSolarClient(username, password, huawei_subdomain=subdomain)
            self._result('c1_login', 'pass', 'session established')
            self._log('ok', 'C1: logged in.')
        except CaptchaRequiredException as exc:
            self._result('c1_login', 'fail', 'captcha required')
            self._log('error', f'C1: Huawei is demanding a CAPTCHA ({exc}). Log into '
                              'fusionsolar.huawei.com in a browser once from this network '
                              'to clear it, then retry.')
        except AuthenticationException as exc:
            self._result('c1_login', 'fail', str(exc))
            self._log('error', f'C1: login rejected — {exc}')
        except FusionSolarException as exc:
            self._result('c1_login', 'fail', str(exc))
            self._log('error', f'C1: {exc}')
        except Exception as exc:
            self._result('c1_login', 'fail', f'{type(exc).__name__}: {exc}')
            self._log('error', f'C1: login failed — {type(exc).__name__}: {exc}')
        finally:
            socket.setdefaulttimeout(old_timeout)

        if client is None:
            for tid in ('c2_plants', 'c3_devices', 'c4_charger_kpi'):
                self._result(tid, 'skipped', 'not logged in')
            return

        try:
            self._probe_cloud_body(client)
        finally:
            try:
                client.log_out()
                self._log('info', 'C1: logged out of the FusionSolar cloud session.')
            except Exception:
                pass

    def _probe_cloud_body(self, client):
        # C2 — plants
        self._result('c2_plants', 'running')
        try:
            stations = client.get_station_list()
            for s in stations:
                self._log('ok', f"C2: plant '{s.get('stationName', '?')}' dn={s.get('dn')}")
            self._result('c2_plants', 'pass' if stations else 'fail', f'{len(stations)} plant(s)')
        except Exception as exc:
            self._result('c2_plants', 'fail', f'{type(exc).__name__}: {exc}')
            self._log('error', f'C2: {type(exc).__name__}: {exc}')

        # C3 — full device list, bypassing the library's narrow mocTypes filter
        # (get_device_ids() hardcodes a set of type codes that may not include
        # the SCharger) so an EV charger of any type code still shows up.
        self._result('c3_devices', 'running')
        devices: list[dict] = []
        try:
            r = client._session.get(
                url=f'https://{client._huawei_subdomain}.fusionsolar.huawei.com'
                    '/rest/neteco/web/config/device/v1/device-list',
                params={'conditionParams.parentDn': client._company_id,
                       '_': int(time.time() * 1000)},
            )
            r.raise_for_status()
            data = r.json().get('data', [])
            for d in data:
                info = {
                    'dn': d.get('dn'), 'mocType': d.get('mocType'),
                    'mocTypeName': d.get('mocTypeName'),
                    'name': d.get('name') or d.get('aliasName'),
                }
                devices.append(info)
                self._log('ok', f"C3: dn={info['dn']} mocType={info['mocType']} "
                                f"type='{info['mocTypeName']}' name='{info['name']}'")
            self._result('c3_devices', 'pass' if devices else 'fail', f'{len(devices)} device(s)')
        except Exception as exc:
            self._log('warn', f'C3: broad device query failed ({type(exc).__name__}: {exc}), '
                              'falling back to the library\'s narrow default query…')
            try:
                devices = [{'dn': d['deviceDn'], 'mocType': None, 'mocTypeName': d['type'], 'name': None}
                          for d in client.get_device_ids()]
                for d in devices:
                    self._log('ok', f"C3 (fallback): dn={d['dn']} type='{d['mocTypeName']}'")
                self._result('c3_devices', 'pass' if devices else 'fail',
                             f'{len(devices)} device(s) (fallback query)')
            except Exception as exc2:
                self._result('c3_devices', 'fail', f'{type(exc2).__name__}: {exc2}')
                self._log('error', f'C3: fallback query also failed — {exc2}')

        # C4 — real-time data for anything that looks like the charger
        self._result('c4_charger_kpi', 'running')
        candidates = [d for d in devices if d.get('mocTypeName')
                     and 'charg' in d['mocTypeName'].lower()]
        if not candidates:
            self._result('c4_charger_kpi', 'fail',
                         'no device type matched "charg*" — see C3 log for the full device list')
            self._log('warn', 'C4: no obvious charger device found. Check the C3 device list '
                              'above for anything that might be the SCharger under an '
                              'unexpected type name.')
            return
        hits = 0
        for d in candidates:
            try:
                data = client.get_real_time_data(d['dn'])
                self._log('ok', f"C4: real-time data for {d['name'] or d['mocTypeName']} "
                                f"(dn={d['dn']}): {json.dumps(data)[:500]}")
                hits += 1
            except Exception as exc:
                self._log('error', f"C4: dn={d['dn']} real-time fetch failed — "
                                  f"{type(exc).__name__}: {exc}")
        self._result('c4_charger_kpi', 'pass' if hits else 'fail',
                     f'{hits}/{len(candidates)} charger device(s) read')
