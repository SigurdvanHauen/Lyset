"""
Modbus TCP client for the Huawei SUN2000 inverter.

Runs in a background QThread and emits a snapshot dict on each successful poll.
The inverter's built-in Modbus server typically listens on port 6607, slave ID 1
(older firmware) or slave ID 0 (some newer units).  Port 502 is also common when
accessed through an SDongle/SdongleA.
"""

import struct
import time
import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from .register_map import REGISTERS, Register, INVERTER_STATES, BATTERY_STATUSES

log = logging.getLogger(__name__)


def _decode_register(raw_regs: list[int], reg: Register) -> Optional[float | str]:
    """Convert a list of raw 16-bit register values into an engineering value."""
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


class ModbusWorker(QThread):
    """
    Background thread that polls the inverter and emits fresh data.

    Signals
    -------
    data_ready(dict)        – emitted after every successful poll
    connection_changed(bool, str) – True/False + status message
    error(str)              – non-fatal read error
    """

    data_ready = Signal(dict)
    connection_changed = Signal(bool, str)
    error = Signal(str)

    def __init__(
        self,
        host: str = '192.168.1.100',
        port: int = 6607,
        slave_id: int = 1,
        poll_interval: float = 5.0,
        parent=None,
    ):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.poll_interval = poll_interval
        self._running = False
        self._client: Optional[ModbusTcpClient] = None

    # ── public control ────────────────────────────────────────────────────────

    def stop(self):
        self._running = False

    def update_settings(self, host: str, port: int, slave_id: int, poll_interval: float):
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.poll_interval = poll_interval

    # ── internal helpers ──────────────────────────────────────────────────────

    def _connect(self) -> bool:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        try:
            self._client = ModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=5,
                retries=1,
            )
            ok = self._client.connect()
            if ok:
                log.info('Connected to %s:%s slave=%s', self.host, self.port, self.slave_id)
                self.connection_changed.emit(True, f'Connected to {self.host}:{self.port}')
            else:
                msg = (f'TCP connect failed — {self.host}:{self.port}. '
                       f'Most likely Home Assistant has the only Modbus slot. '
                       f'Disable the huawei_solar/Modbus integration in HA and retry.')
                log.warning(msg)
                self.connection_changed.emit(False, msg)
            return ok
        except Exception as exc:
            msg = f'Connection error: {exc}'
            log.error(msg)
            self.connection_changed.emit(False, msg)
            return False

    def _read_registers_batch(self, regs: list[Register]) -> dict:
        """
        Read a list of registers, batching contiguous blocks into single requests
        to reduce round-trips (Modbus max read is 125 registers per request).
        """
        if not regs:
            return {}

        # Sort by address so we can batch contiguous ranges
        sorted_regs = sorted(regs, key=lambda r: r.address)

        # Build batches: merge registers within a 10-register gap to reduce requests
        MAX_GAP = 10
        MAX_BATCH = 100  # stay well under the 125-register limit

        batches: list[tuple[int, int]] = []  # (start_addr, length)
        batch_start = sorted_regs[0].address
        batch_end = batch_start + sorted_regs[0].count

        for reg in sorted_regs[1:]:
            if reg.address - batch_end <= MAX_GAP and (reg.address + reg.count - batch_start) <= MAX_BATCH:
                batch_end = max(batch_end, reg.address + reg.count)
            else:
                batches.append((batch_start, batch_end - batch_start))
                batch_start = reg.address
                batch_end = batch_start + reg.count
        batches.append((batch_start, batch_end - batch_start))

        # Read each batch and collect raw register values
        raw: dict[int, int] = {}
        for start, length in batches:
            try:
                result = self._client.read_holding_registers(
                    address=start,
                    count=length,
                    device_id=self.slave_id,
                )
                if result.isError():
                    log.warning('Modbus error reading %d+%d: %s', start, length, result)
                    continue
                for i, val in enumerate(result.registers):
                    raw[start + i] = val
            except (ModbusException, ConnectionError, OSError) as exc:
                log.warning('Read error at %d+%d: %s', start, length, exc)
                # Re-raise so _poll() can mark the connection as lost
                raise

        # Decode each register
        data: dict = {}
        for reg in regs:
            reg_raw = [raw.get(reg.address + i, 0) for i in range(reg.count)]
            if all(v == 0 for v in reg_raw) and reg.address not in raw:
                continue  # register wasn't returned at all
            val = _decode_register(reg_raw, reg)
            if val is not None:
                data[reg.key] = val

        return data

    def _poll(self) -> Optional[dict]:
        try:
            # Split registers into two groups: inverter (3xxxx) and battery/meter (37xxx)
            inv_regs = [r for r in REGISTERS if r.address < 37000]
            ext_regs = [r for r in REGISTERS if r.address >= 37000]

            data: dict = {}
            data.update(self._read_registers_batch(inv_regs))
            data.update(self._read_registers_batch(ext_regs))

            # Derive computed values
            if 'pv1_voltage' in data and 'pv1_current' in data:
                data['pv1_power'] = round(data['pv1_voltage'] * data['pv1_current'], 1)
            if 'pv2_voltage' in data and 'pv2_current' in data:
                data['pv2_power'] = round(data['pv2_voltage'] * data['pv2_current'], 1)

            # House load = PV production - grid export + grid import + battery discharge
            # meter_active_power: positive = export, negative = import
            # house = PV − exported_to_grid − charged_to_battery
            # (batt_power > 0 = charging, meter_active_power > 0 = export)
            if 'active_power' in data and 'meter_active_power' in data:
                batt = data.get('batt_power', 0.0)
                data['house_load'] = round(
                    data['active_power'] - data['meter_active_power'] - batt, 1
                )

            # Human-readable state labels
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
            self.error.emit(str(exc))
            return None

    # ── thread main ───────────────────────────────────────────────────────────

    def run(self):
        try:
            self._run_loop()
        except Exception as exc:
            log.critical('Worker crashed: %s', exc, exc_info=True)
            self.connection_changed.emit(False, f'Worker crashed: {exc}')

    def _run_loop(self):
        self._running = True
        connected = False

        while self._running:
            if not connected:
                connected = self._connect()
                if not connected:
                    # Back off 10 s before retrying
                    for _ in range(100):
                        if not self._running:
                            break
                        time.sleep(0.1)
                    continue

            data = self._poll()
            if data:
                self.data_ready.emit(data)
            else:
                connected = False
                self.connection_changed.emit(False, 'Connection lost — retrying…')

            # Sleep in small increments so stop() is responsive
            deadline = time.time() + self.poll_interval
            while self._running and time.time() < deadline:
                time.sleep(0.1)

        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self.connection_changed.emit(False, 'Disconnected')
