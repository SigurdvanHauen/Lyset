"""
EV charger Modbus discovery probe — SCharger-22KT-S0 (FusionCharge).

Runs a one-shot diagnostic sequence from the EV Charger tab to establish
whether Lyset can read the charger locally, and over which path:

PROBE A — direct Modbus TCP to the charger's own IP (WiFi, static lease).
  Huawei documents Modbus TCP on port 502 for the SCharger, but community
  reports (HA forum, evcc #10262) say the charger only accepts its managing
  dongle/EMMA as Modbus master and drops third-party connections. Firmware
  moves, so we test rather than believe: port fingerprint, Modbus connect,
  identity reads, data reads, input-register variant, then a register sweep
  if anything answered.

PROBE B — through the SDongle connection the inverter poller already uses.
  wlcrs/huawei-solar-lib v1.6 reads SChargers as a sub-device (own slave id)
  when they hang off an EMMA. This charger is standalone in FusionSolar (not
  under the dongle's device list) so this is a long shot, but the test is
  cheap: try the charger registers on a range of device ids. The poller is
  PAUSED for the duration — the SDongle allows one Modbus client, and a
  second connection mid-poll desyncs transaction ids.

Register targets (sources: wlcrs/huawei-solar-lib v2 SCHARGER_REGISTERS;
evcc discussion #10262):
  30015 ESN (STR)          30031 software version (STR)   30078 model (STR)
  30076 rated power (U32 /10 kW)
  30500/30502/30504 phase voltages (U32 /10 V)
  30506 total energy charged (U32 /1000 kWh)   30508 temperature (I32 /10 °C)
  4108 charging power (U32, W?)                4110 status (U32)

Every step emits timestamped log events through a callback (server pushes
them over the WebSocket and persists to the ev_probe_log table) so progress
is visible live and the run can be exported for offline analysis.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Optional

from pymodbus.client import ModbusTcpClient

log = logging.getLogger(__name__)

_MODBUS_EXC = {
    1: 'IllegalFunction', 2: 'IllegalDataAddress', 3: 'IllegalDataValue',
    4: 'ServerDeviceFailure', 5: 'Acknowledge', 6: 'ServerDeviceBusy',
    8: 'MemoryParityError', 10: 'GatewayPathUnavailable',
    11: 'GatewayTargetNoResponse',
}

# (name, address, words, kind) — kind: 'str' | 'u32' | 'i32'
_IDENT_REGS = [
    ('model (30078)',            30078, 8,  'str'),
    ('ESN (30015)',              30015, 8,  'str'),
    ('software version (30031)', 30031, 8,  'str'),
]
_DATA_REGS = [
    ('rated power kW (30076, /10)',        30076, 2, 'u32'),
    ('phase A voltage (30500, /10)',       30500, 2, 'u32'),
    ('phase B voltage (30502, /10)',       30502, 2, 'u32'),
    ('phase C voltage (30504, /10)',       30504, 2, 'u32'),
    ('total energy kWh (30506, /1000)',    30506, 2, 'u32'),
    ('temperature °C (30508, /10)',        30508, 2, 'i32'),
    ('charging power? (4108)',             4108,  2, 'u32'),
    ('status? (4110)',                     4110,  2, 'u32'),
]
_SCAN_PORTS = [22, 80, 443, 502, 6607, 8443]
_SWEEP_RANGES = [(30000, 30120), (30500, 30520), (4090, 4130)]
_DIRECT_DEVICE_IDS = [1, 0, 2, 3, 247]
_DONGLE_DEVICE_IDS = [2, 3, 4, 5, 6, 7, 8, 9, 10, 100, 247]

TESTS = [
    ('a1_ports',   'A1 · TCP port fingerprint (direct)'),
    ('a2_connect', 'A2 · Modbus TCP connect (direct)'),
    ('a3_ident',   'A3 · Charger identity registers (direct)'),
    ('a4_data',    'A4 · Data registers: power/energy/voltage (direct)'),
    ('a5_input',   'A5 · Input registers FC04 variant (direct)'),
    ('a6_sweep',   'A6 · Register sweep (direct, only if A responded)'),
    ('b1_dongle',  'B1 · Charger as sub-device on the SDongle link'),
]


def _decode_str(words: list[int]) -> str:
    raw = b''.join(w.to_bytes(2, 'big') for w in words)
    s = raw.decode('ascii', errors='replace').rstrip('\x00').strip()
    return s if s and all(32 <= ord(ch) < 127 for ch in s) else f'<binary {raw.hex()}>'


def _decode_u32(words: list[int]) -> int:
    return (words[0] << 16) | words[1]


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

    def start(self, charger_host: str, charger_port: int, worker=None) -> bool:
        """Kick off a probe run in a background thread. Returns False if one is
        already running. `worker` is the live ModbusWorker (for probe B); pass
        None to skip B."""
        if self.running:
            return False
        self.running = True
        self.run_ts = time.time()
        self.log_lines = []
        self._reset_results()
        self._thread = threading.Thread(
            target=self._run, args=(charger_host, charger_port, worker),
            daemon=True, name='EVProbe',
        )
        self._thread.start()
        return True

    # ── probe implementation ──────────────────────────────────────────────────

    def _run(self, host: str, port: int, worker):
        self._emit({'kind': 'status', 'running': True})
        try:
            self._log('info', f'=== EV charger probe started — target {host}:{port} ===')
            responded = self._probe_a(host, port)
            self._probe_b(worker)
            self._log('info', '=== Probe finished ===')
            passed = [r['name'] for r in self.results.values() if r['status'] == 'pass']
            if passed:
                self._log('ok', 'WORKING: ' + '; '.join(passed))
            else:
                self._log('warn', 'No Modbus path to the charger worked. Next option: '
                                  'FusionSolar cloud API (read-only, ~30 s updates).')
        except Exception as exc:
            self._log('error', f'Probe crashed: {exc}')
            log.exception('EVProbe crashed')
        finally:
            self.running = False
            self._emit({'kind': 'status', 'running': False})

    # ---- helpers ----

    def _read(self, client: ModbusTcpClient, device_id: int, address: int,
              count: int, input_regs: bool = False):
        """One read. Returns (outcome, payload): ('ok', [words]) |
        ('exc', name) — device answered with a Modbus exception |
        ('dead', errstr) — timeout / connection dropped."""
        try:
            fn = client.read_input_registers if input_regs else client.read_holding_registers
            r = fn(address, count=count, device_id=device_id)
            if r.isError():
                code = getattr(r, 'exception_code', None)
                return 'exc', _MODBUS_EXC.get(code, f'exception {code}')
            return 'ok', r.registers
        except Exception as exc:
            # The SCharger reportedly drops the TCP session on unwanted reads —
            # reconnect once so one slammed door doesn't fail every later step.
            try:
                client.close()
                client.connect()
            except Exception:
                pass
            return 'dead', f'{type(exc).__name__}: {exc}'

    def _fmt(self, kind: str, words: list[int]) -> str:
        if kind == 'str':
            return repr(_decode_str(words))
        v = _decode_u32(words)
        if kind == 'i32' and v >= 0x80000000:
            v -= 0x100000000
        return f'{v} (raw {" ".join(f"{w:04X}" for w in words)})'

    # ---- probe A: direct ----

    def _probe_a(self, host: str, port: int) -> bool:
        # A1 — port fingerprint
        self._result('a1_ports', 'running')
        open_ports = []
        for p in _SCAN_PORTS:
            try:
                with socket.create_connection((host, p), timeout=2):
                    open_ports.append(p)
                    self._log('ok', f'A1: port {p} OPEN')
            except Exception:
                self._log('info', f'A1: port {p} closed/filtered')
        detail = f'open: {open_ports or "none"}'
        self._result('a1_ports', 'pass' if open_ports else 'fail', detail)
        if port not in open_ports:
            self._log('warn', f'A1: Modbus port {port} is not open — probes A2–A6 will be quick fails. '
                              'Check FusionSolar → charger → communication settings for a '
                              '"Modbus TCP" enable switch.')

        # A2 — Modbus connect
        self._result('a2_connect', 'running')
        client = ModbusTcpClient(host=host, port=port, timeout=3, retries=0)
        if not client.connect():
            self._result('a2_connect', 'fail', 'TCP connect refused/timeout')
            for tid in ('a3_ident', 'a4_data', 'a5_input', 'a6_sweep'):
                self._result(tid, 'skipped', 'no connection')
            self._log('error', 'A2: cannot open Modbus TCP connection — probe A over.')
            return False
        self._result('a2_connect', 'pass', 'TCP session established')
        self._log('ok', f'A2: connected to {host}:{port}')

        any_ok = False
        answered = False   # device sent ANY Modbus response (incl. exceptions)

        # A3 — identity strings across device ids
        self._result('a3_ident', 'running')
        good_id = None
        for did in _DIRECT_DEVICE_IDS:
            dead_streak = 0
            for name, addr, cnt, kind in _IDENT_REGS:
                out, payload = self._read(client, did, addr, cnt)
                if out == 'ok':
                    self._log('ok', f'A3: id={did} {name} = {self._fmt(kind, payload)}')
                    any_ok = answered = True
                    good_id = good_id if good_id is not None else did
                elif out == 'exc':
                    self._log('warn', f'A3: id={did} {name} → Modbus exception {payload} '
                                      '(device ANSWERED — path exists, register/id wrong)')
                    answered = True
                else:
                    self._log('info', f'A3: id={did} {name} → no response ({payload})')
                    dead_streak += 1
            if dead_streak == len(_IDENT_REGS) and did != _DIRECT_DEVICE_IDS[0]:
                self._log('info', f'A3: id={did} fully silent')
        self._result('a3_ident', 'pass' if any_ok else 'fail',
                     f'device id {good_id} answered' if any_ok else
                     ('answers with exceptions only' if answered else 'silent on all ids'))

        # A4 — data registers (on the id that worked, else id 1)
        did = good_id if good_id is not None else 1
        self._result('a4_data', 'running')
        ok_count = 0
        for name, addr, cnt, kind in _DATA_REGS:
            out, payload = self._read(client, did, addr, cnt)
            if out == 'ok':
                self._log('ok', f'A4: id={did} {name} = {self._fmt(kind, payload)}')
                ok_count += 1
                answered = True
            elif out == 'exc':
                self._log('warn', f'A4: id={did} {name} → exception {payload}')
                answered = True
            else:
                self._log('info', f'A4: id={did} {name} → no response ({payload})')
        self._result('a4_data', 'pass' if ok_count else 'fail', f'{ok_count}/{len(_DATA_REGS)} readable')
        any_ok = any_ok or ok_count > 0

        # A5 — input registers (FC04) for the two most useful addresses
        self._result('a5_input', 'running')
        ok5 = 0
        for name, addr, cnt, kind in [_DATA_REGS[6], _DATA_REGS[4]]:
            out, payload = self._read(client, did, addr, cnt, input_regs=True)
            if out == 'ok':
                self._log('ok', f'A5: FC04 id={did} {name} = {self._fmt(kind, payload)}')
                ok5 += 1
                answered = True
            elif out == 'exc':
                self._log('warn', f'A5: FC04 id={did} {name} → exception {payload}')
                answered = True
            else:
                self._log('info', f'A5: FC04 id={did} {name} → no response ({payload})')
        self._result('a5_input', 'pass' if ok5 else 'fail', f'{ok5}/2 readable')

        # A6 — sweep, only worthwhile if something responded at all
        if answered:
            self._result('a6_sweep', 'running')
            found = 0
            for lo, hi in _SWEEP_RANGES:
                self._log('info', f'A6: sweeping {lo}–{hi} (blocks of 10)…')
                for start in range(lo, hi, 10):
                    out, payload = self._read(client, did, start, 10)
                    if out == 'ok':
                        vals = ' '.join(f'{w:04X}' for w in payload)
                        self._log('ok', f'A6: {start}–{start + 9} = {vals}')
                        found += 1
                    elif out == 'exc':
                        self._log('info', f'A6: {start}–{start + 9} → {payload}')
                    time.sleep(0.15)   # be gentle — don't trip rate protection
            self._result('a6_sweep', 'pass' if found else 'fail', f'{found} readable blocks')
        else:
            self._result('a6_sweep', 'skipped', 'nothing answered in A3–A5')
            self._log('warn', 'A6: skipped — charger never sent a Modbus response. '
                              'Matches community reports (only its managing dongle may be master).')

        try:
            client.close()
        except Exception:
            pass
        return answered

    # ---- probe B: via SDongle ----

    def _probe_b(self, worker):
        self._result('b1_dongle', 'running')
        if worker is None or not worker.is_alive():
            self._result('b1_dongle', 'skipped', 'inverter poller not running')
            self._log('warn', 'B1: skipped — no live inverter connection to test through.')
            return

        self._log('info', f'B1: pausing inverter poller, probing device ids {_DONGLE_DEVICE_IDS} '
                          f'on {worker.host}:{worker.port}…')
        worker.pause()
        try:
            if not worker.wait_parked(timeout=20):
                self._result('b1_dongle', 'skipped', 'poller would not pause')
                self._log('error', 'B1: poller did not release the connection within 20 s — skipping.')
                return

            client = ModbusTcpClient(host=worker.host, port=worker.port, timeout=3, retries=0)
            if not client.connect():
                self._result('b1_dongle', 'fail', 'could not connect to dongle')
                self._log('error', 'B1: could not open connection to the SDongle.')
                return

            found = []
            for did in _DONGLE_DEVICE_IDS:
                out, payload = self._read(client, did, 30078, 8)   # charger model STR
                if out == 'ok':
                    self._log('ok', f'B1: id={did} charger model = {self._fmt("str", payload)} '
                                    '— CHARGER REACHABLE VIA DONGLE')
                    found.append(did)
                elif out == 'exc':
                    self._log('warn', f'B1: id={did} 30078 → exception {payload} '
                                      '(a device exists at this id)')
                else:
                    self._log('info', f'B1: id={did} 30078 → no response')
                out, payload = self._read(client, did, 4108, 2)
                if out == 'ok':
                    self._log('ok', f'B1: id={did} 4108 = {self._fmt("u32", payload)}')
                    if did not in found:
                        found.append(did)
                elif out == 'exc':
                    self._log('warn', f'B1: id={did} 4108 → exception {payload}')
                time.sleep(0.3)

            try:
                client.close()
            except Exception:
                pass
            self._result('b1_dongle', 'pass' if found else 'fail',
                         f'charger at device id(s) {found}' if found else
                         'no charger behind the dongle (expected — it is standalone in FusionSolar)')
        finally:
            worker.resume()
            self._log('info', 'B1: inverter poller resumed.')
