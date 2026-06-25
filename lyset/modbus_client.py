"""
Modbus TCP client for the Huawei SUN2000 inverter.

Runs as a background daemon thread and calls registered callbacks on each
successful poll.  All public write methods are thread-safe and enqueue their
request for execution on the next poll gap.
"""

import queue
import time
import logging
import threading
from dataclasses import dataclass
from typing import Optional, Callable

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from .register_map import REGISTERS, Register, INVERTER_STATES, BATTERY_STATUSES

log = logging.getLogger(__name__)


@dataclass
class _WriteRequest:
    address: int
    values: list
    description: str = ''


def _decode_register(raw_regs: list[int], reg: Register) -> Optional[float | str]:
    dt = reg.data_type
    try:
        if dt == 'STR':
            raw_bytes = b''.join(r.to_bytes(2, 'big') for r in raw_regs)
            return raw_bytes.decode('ascii', errors='replace').rstrip('\x00').strip()
        elif dt == 'U16':
            return raw_regs[0] / reg.gain
        elif dt == 'I16':
            val = raw_regs[0]
            if val >= 0x8000:
                val -= 0x10000
            return val / reg.gain
        elif dt == 'U32':
            val = (raw_regs[0] << 16) | raw_regs[1]
            return val / reg.gain
        elif dt == 'I32':
            val = (raw_regs[0] << 16) | raw_regs[1]
            if val >= 0x80000000:
                val -= 0x100000000
            return val / reg.gain
    except Exception as exc:
        log.debug('Decode failed for %s: %s', reg.key, exc)
    return None


class ModbusWorker(threading.Thread):
    """
    Background daemon thread that polls the SUN2000 and fires callbacks.

    Callbacks (all optional, called from the worker thread):
        on_data(dict)               – fresh snapshot after every successful poll
        on_connection(bool, str)    – True/False + status message
        on_write_result(bool, str)  – write success/failure
        on_error(str)               – non-fatal poll error
    """

    def __init__(
        self,
        host: str = '192.168.1.185',
        port: int = 502,
        slave_id: int = 1,
        poll_interval: float = 5.0,
        on_data: Optional[Callable] = None,
        on_connection: Optional[Callable] = None,
        on_write_result: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
    ):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.poll_interval = poll_interval
        self._on_data = on_data or (lambda d: None)
        self._on_connection = on_connection or (lambda ok, msg: None)
        self._on_write_result = on_write_result or (lambda ok, msg: None)
        self._on_error = on_error or (lambda msg: None)
        self._running = False
        self._client: Optional[ModbusTcpClient] = None
        self._write_queue: queue.Queue = queue.Queue()

    # ── Public control ────────────────────────────────────────────────────────

    def stop(self):
        self._running = False

    # ── Public write API (thread-safe) ────────────────────────────────────────

    def write_u16(self, address: int, value: int, description: str = ''):
        self._write_queue.put(_WriteRequest(address, [int(value)], description))

    def write_u32(self, address: int, value: int, description: str = ''):
        v = int(value) & 0xFFFFFFFF
        self._write_queue.put(_WriteRequest(address, [v >> 16, v & 0xFFFF], description))

    def write_i32(self, address: int, value: int, description: str = ''):
        if value < 0:
            value += 0x100000000
        self.write_u32(address, value, description)

    def _temp_read(self, address: int, count: int) -> Optional[list[int]]:
        """Open a short-lived connection for an on-demand register read."""
        client = self._client
        own = False
        if client is None:
            client = ModbusTcpClient(host=self.host, port=self.port, timeout=5, retries=0)
            if not client.connect():
                return None
            own = True
        try:
            r = client.read_holding_registers(address, count=count, device_id=self.slave_id)
            return r.registers if not r.isError() else None
        except Exception:
            return None
        finally:
            if own:
                try:
                    client.close()
                except Exception:
                    pass

    def read_u16_now(self, address: int) -> Optional[int]:
        regs = self._temp_read(address, 1)
        return regs[0] if regs else None

    def read_u32_now(self, address: int) -> Optional[int]:
        regs = self._temp_read(address, 2)
        return ((regs[0] << 16) | regs[1]) if regs else None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _process_writes(self):
        while not self._write_queue.empty():
            req: _WriteRequest = self._write_queue.get_nowait()
            try:
                result = self._client.write_registers(
                    req.address, req.values, device_id=self.slave_id
                )
                if result.isError():
                    msg = f'{req.description}: Modbus error {result}'
                    log.warning(msg)
                    self._on_write_result(False, msg)
                else:
                    msg = f'{req.description}: OK'
                    log.info(msg)
                    self._on_write_result(True, msg)
            except Exception as exc:
                msg = f'{req.description}: {exc}'
                log.error(msg)
                self._on_write_result(False, msg)

    def _open_tcp(self) -> bool:
        """Open a fresh TCP connection. No callbacks — state management is in _run_loop."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        try:
            self._client = ModbusTcpClient(host=self.host, port=self.port, timeout=5, retries=0)
            return self._client.connect()
        except Exception as exc:
            log.debug('TCP connect error: %s', exc)
            return False

    def _read_registers_batch(self, regs: list[Register]) -> dict:
        if not regs:
            return {}

        sorted_regs = sorted(regs, key=lambda r: r.address)

        MAX_GAP = 10
        MAX_BATCH = 100

        batches: list[tuple[int, int]] = []
        batch_start = sorted_regs[0].address
        batch_end = batch_start + sorted_regs[0].count

        for reg in sorted_regs[1:]:
            if (reg.address - batch_end <= MAX_GAP
                    and (reg.address + reg.count - batch_start) <= MAX_BATCH):
                batch_end = max(batch_end, reg.address + reg.count)
            else:
                batches.append((batch_start, batch_end - batch_start))
                batch_start = reg.address
                batch_end = batch_start + reg.count
        batches.append((batch_start, batch_end - batch_start))

        raw: dict[int, int] = {}
        for start, length in batches:
            try:
                result = self._client.read_holding_registers(
                    address=start, count=length, device_id=self.slave_id,
                )
                if result.isError():
                    log.warning('Modbus error reading %d+%d: %s', start, length, result)
                    continue
                for i, val in enumerate(result.registers):
                    raw[start + i] = val
            except Exception as exc:
                # Any error reading this batch (timeout, no response, bad register) —
                # skip it and continue with the remaining batches. TCP failures will
                # be detected on the next cycle when _open_tcp() fails.
                log.warning('Batch error at %d+%d: %s', start, length, exc)

        data: dict = {}
        for reg in regs:
            reg_raw = [raw.get(reg.address + i, 0) for i in range(reg.count)]
            if all(v == 0 for v in reg_raw) and reg.address not in raw:
                continue
            val = _decode_register(reg_raw, reg)
            if val is not None:
                data[reg.key] = val

        return data

    def _poll(self) -> Optional[dict]:
        try:
            # Device-info registers (model, serial, rated power) are static —
            # skip them in the regular poll to avoid the 5-second timeout on
            # inverters that don't expose them via the SDongle proxy.
            inv_regs = [r for r in REGISTERS if r.address < 37000 and r.group != 'Device']
            ext_regs = [r for r in REGISTERS if r.address >= 37000]

            data: dict = {}
            data.update(self._read_registers_batch(inv_regs))
            data.update(self._read_registers_batch(ext_regs))

            if 'pv1_voltage' in data and 'pv1_current' in data:
                data['pv1_power'] = round(data['pv1_voltage'] * data['pv1_current'], 1)
            if 'pv2_voltage' in data and 'pv2_current' in data:
                data['pv2_power'] = round(data['pv2_voltage'] * data['pv2_current'], 1)

            if 'active_power' in data and 'meter_active_power' in data:
                batt = data.get('batt_power', 0.0)
                data['house_load'] = round(
                    data['active_power'] - data['meter_active_power'] - batt, 1
                )

            if 'inverter_state' in data:
                code = int(data['inverter_state'])
                data['inverter_state_label'] = INVERTER_STATES.get(code, f'Unknown (0x{code:04X})')
            if 'batt_status' in data:
                code = int(data['batt_status'])
                data['batt_status_label'] = BATTERY_STATUSES.get(code, f'Unknown ({code})')

            data['_timestamp'] = time.time()
            return data

        except Exception as exc:
            log.error('Poll error: %s', exc)
            self._on_error(str(exc))
            return None

    # ── Thread main ───────────────────────────────────────────────────────────

    def run(self):
        try:
            self._run_loop()
        except Exception as exc:
            log.critical('Worker crashed: %s', exc, exc_info=True)
            self._on_connection(False, f'Worker crashed: {exc}')

    def _run_loop(self):
        self._running = True
        reported_ok = False  # tracks last UI state so we only emit on changes

        while self._running:
            # Open a fresh TCP connection every cycle. The SUN2000 drops idle
            # connections after ~30 s, causing silent hangs on a half-open socket.
            # Reconnecting each poll is the only reliable pattern for this inverter.
            if not self._open_tcp():
                if reported_ok:
                    msg = (f'TCP connect failed — {self.host}:{self.port}. '
                           f'Check that no other Modbus client (e.g. HA huawei_solar) '
                           f'holds the connection.')
                    log.warning(msg)
                    self._on_connection(False, msg)
                    reported_ok = False
                for _ in range(100):
                    if not self._running:
                        break
                    time.sleep(0.1)
                continue

            if not reported_ok:
                log.info('Connected to %s:%s slave=%s', self.host, self.port, self.slave_id)
                self._on_connection(True, f'Connected to {self.host}:{self.port}')
                reported_ok = True

            self._process_writes()
            data = self._poll()

            # Close immediately after each poll — releases the inverter's single
            # Modbus slot and prevents stale half-open sockets next cycle.
            if self._client:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

            if data:
                self._on_data(data)

            deadline = time.time() + self.poll_interval
            while self._running and time.time() < deadline:
                time.sleep(0.1)

        self._on_connection(False, 'Disconnected')
