"""
Control panel — write registers for the SUN2000 inverter + LUNA2000 battery.

All addresses and value ranges verified against a live SUN2000-6KTL-M1 (SDT-MAN-002).

Write-register map used here:
  47086          Storage working mode (U16)
  47075-47076    Max charge power (U32, W)
  47077-47078    Max discharge power (U32, W)
  47087          Grid-charge enable (U16: 0=off, 1=on)
  47079-47080    Grid-charge power limit (U32, W)
  47088          Charge cut-off SOC (U16, /10, %)
  47089          Discharge cut-off SOC (U16, /10, %)
  47098-47099    Forced charge/discharge power (U32, W)
  47100          Forced command (U16: 0=stop, 1=charge, 2=discharge)
  40525          Active-power control mode (U16) — may give error on some firmware
  40526          Active-power percentage (U16, /10, %)
  40527-40528    Active-power fixed value (I32, W)
  40200          Device on/off (U16: 0=on, 1=shutdown)
"""

from __future__ import annotations
from typing import Optional, TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QGroupBox, QComboBox, QSpinBox, QDoubleSpinBox,
    QPushButton, QCheckBox, QFrame, QScrollArea, QSizePolicy,
    QMessageBox, QSpacerItem,
)
from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QFont

if TYPE_CHECKING:
    from ..modbus_client import ModbusWorker

# ── palette (matches the rest of the app) ────────────────────────────────────
C = {
    'bg':      '#0F1117',
    'surface': '#1C2032',
    'border':  '#2D3555',
    'text':    '#E2E8F4',
    'muted':   '#8892AA',
    'accent':  '#4F8EF7',
    'success': '#34D399',
    'warning': '#F59E0B',
    'danger':  '#F87171',
}

WORKING_MODES = {
    0: 'Maximise self-consumption',
    1: 'Fully fed to grid',
    2: 'Time-of-use (TOU)',
    3: 'Time-of-use (TOU) Pro',
    4: 'Fixed charge/discharge',
    5: 'Forced charge/discharge',
}

ACTIVE_POWER_MODES = {
    0: 'No limit (unlimited)',
    1: 'Limit by percentage',
    2: 'Limit by fixed watt value',
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _group(title: str) -> QGroupBox:
    g = QGroupBox(title)
    g.setStyleSheet(f'''
        QGroupBox {{
            color: {C["muted"]};
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
            text-transform: uppercase;
            border: 1px solid {C["border"]};
            border-radius: 8px;
            margin-top: 10px;
            padding: 12px 10px 10px 10px;
            background: {C["surface"]};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 6px;
            background: {C["bg"]};
        }}
    ''')
    return g


def _label(text: str, colour: str = None) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f'color:{colour or C["text"]}; background:transparent;')
    return lbl


def _spin(lo: int, hi: int, val: int, suffix: str = '') -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(val)
    if suffix:
        s.setSuffix(f' {suffix}')
    s.setFixedWidth(110)
    return s


def _btn(text: str, colour: str = C['accent']) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(f'''
        QPushButton {{
            color: {colour};
            background: transparent;
            border: 1px solid {colour};
            border-radius: 6px;
            padding: 6px 18px;
            font-weight: 700;
        }}
        QPushButton:hover  {{ background: {colour}22; }}
        QPushButton:pressed{{ background: {colour}44; }}
        QPushButton:disabled{{ color:#555; border-color:#555; }}
    ''')
    return b


def _confirm(parent: QWidget, title: str, msg: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(msg)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    box.setStyleSheet(f'background:{C["surface"]}; color:{C["text"]};')
    return box.exec() == QMessageBox.StandardButton.Yes


# ── Individual sections ───────────────────────────────────────────────────────

class _WorkingModeSection(QGroupBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._group_init('Storage Working Mode')
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 14, 10, 10)
        lay.setSpacing(10)

        self._combo = QComboBox()
        for k, v in WORKING_MODES.items():
            self._combo.addItem(v, k)
        self._combo.setMinimumWidth(280)

        lay.addWidget(_label('Mode:'))
        lay.addWidget(self._combo)
        lay.addStretch()
        self._apply = _btn('Apply')
        lay.addWidget(self._apply)

    def _group_init(self, title):
        self.setTitle(title)
        self._apply_group_style()

    def _apply_group_style(self):
        self.setStyleSheet(f'''
            QGroupBox {{
                color:{C["muted"]}; font-size:11px; font-weight:700;
                letter-spacing:1px; border:1px solid {C["border"]};
                border-radius:8px; margin-top:10px;
                padding:12px 10px 10px; background:{C["surface"]};
            }}
            QGroupBox::title {{
                subcontrol-origin:margin; left:10px;
                padding:0 6px; background:{C["bg"]};
            }}
        ''')

    def set_mode(self, code: int):
        idx = self._combo.findData(code)
        if idx >= 0:
            self._combo.setCurrentIndex(idx)

    def current_mode(self) -> int:
        return self._combo.currentData()

    @property
    def apply_btn(self): return self._apply


class ControlPanel(QScrollArea):
    """Full control tab — all writable Modbus registers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f'background:{C["bg"]}; border:none;')
        self._worker: Optional['ModbusWorker'] = None

        container = QWidget()
        container.setStyleSheet(f'background:{C["bg"]};')
        self.setWidget(container)
        root = QVBoxLayout(container)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        # ── Status banner ─────────────────────────────────────────────────────
        self._status_lbl = QLabel('Not connected — connect to the inverter first.')
        self._status_lbl.setStyleSheet(
            f'color:{C["warning"]}; background:{C["surface"]}; '
            f'border:1px solid {C["border"]}; border-radius:6px; padding:8px 12px;'
        )
        root.addWidget(self._status_lbl)

        # ── Working mode ──────────────────────────────────────────────────────
        g = _group('Storage Working Mode')
        gl = QHBoxLayout(g)
        gl.setSpacing(10)
        self._mode_combo = QComboBox()
        for k, v in WORKING_MODES.items():
            self._mode_combo.addItem(v, k)
        self._mode_combo.setMinimumWidth(260)
        gl.addWidget(_label('Mode:'))
        gl.addWidget(self._mode_combo)
        gl.addStretch()
        self._mode_apply = _btn('Apply')
        self._mode_apply.clicked.connect(self._apply_working_mode)
        gl.addWidget(self._mode_apply)
        root.addWidget(g)

        # ── Power limits ──────────────────────────────────────────────────────
        g = _group('Battery Power Limits')
        gl = QGridLayout(g)
        gl.setSpacing(10)
        gl.addWidget(_label('Max charge power:'), 0, 0)
        self._max_chg = _spin(0, 5000, 2500, 'W')
        gl.addWidget(self._max_chg, 0, 1)
        gl.addWidget(_label('Max discharge power:'), 1, 0)
        self._max_dchg = _spin(0, 5000, 2500, 'W')
        gl.addWidget(self._max_dchg, 1, 1)
        self._limits_apply = _btn('Apply')
        self._limits_apply.clicked.connect(self._apply_power_limits)
        gl.addWidget(self._limits_apply, 0, 2, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        gl.setColumnStretch(3, 1)
        root.addWidget(g)

        # ── SOC targets ───────────────────────────────────────────────────────
        g = _group('SOC Targets')
        gl = QGridLayout(g)
        gl.setSpacing(10)
        gl.addWidget(_label('Charge cut-off (stop charging at):'), 0, 0)
        self._chg_soc = _spin(0, 100, 100, '%')
        gl.addWidget(self._chg_soc, 0, 1)
        gl.addWidget(_label('Discharge cut-off (stop discharging at):'), 1, 0)
        self._dchg_soc = _spin(0, 100, 10, '%')
        gl.addWidget(self._dchg_soc, 1, 1)
        self._soc_apply = _btn('Apply')
        self._soc_apply.clicked.connect(self._apply_soc_targets)
        gl.addWidget(self._soc_apply, 0, 2, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        gl.setColumnStretch(3, 1)
        root.addWidget(g)

        # ── Forced charge / discharge ─────────────────────────────────────────
        g = _group('Forced Charge / Discharge')
        gl = QHBoxLayout(g)
        gl.setSpacing(10)
        gl.addWidget(_label('Power:'))
        self._force_pwr = _spin(0, 5000, 2500, 'W')
        gl.addWidget(self._force_pwr)
        gl.addSpacing(20)
        self._force_chg = _btn('⚡  Force Charge',   C['success'])
        self._force_dchg = _btn('⬇  Force Discharge', C['warning'])
        self._force_stop = _btn('■  Stop',            C['danger'])
        for b in (self._force_chg, self._force_dchg, self._force_stop):
            gl.addWidget(b)
        gl.addStretch()
        self._force_chg.clicked.connect(lambda: self._force_command(1))
        self._force_dchg.clicked.connect(lambda: self._force_command(2))
        self._force_stop.clicked.connect(lambda: self._force_command(0))
        root.addWidget(g)

        # ── Grid charge ───────────────────────────────────────────────────────
        g = _group('Grid Charge  (charge battery from grid)')
        gl = QGridLayout(g)
        gl.setSpacing(10)
        self._grid_chg_en = QCheckBox('Enable grid charge')
        self._grid_chg_en.setStyleSheet(f'color:{C["text"]};')
        gl.addWidget(self._grid_chg_en, 0, 0)
        gl.addWidget(_label('Power limit:'), 1, 0)
        self._grid_chg_pwr = _spin(0, 5000, 1000, 'W')
        gl.addWidget(self._grid_chg_pwr, 1, 1)
        self._grid_apply = _btn('Apply')
        self._grid_apply.clicked.connect(self._apply_grid_charge)
        gl.addWidget(self._grid_apply, 0, 2, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        gl.setColumnStretch(3, 1)
        root.addWidget(g)

        # ── Active power curtailment ──────────────────────────────────────────
        g = _group('Active Power Curtailment  (limit PV production)')
        gl = QGridLayout(g)
        gl.setSpacing(10)
        gl.addWidget(_label('Mode:'), 0, 0)
        self._pwr_mode_combo = QComboBox()
        for k, v in ACTIVE_POWER_MODES.items():
            self._pwr_mode_combo.addItem(v, k)
        self._pwr_mode_combo.setMinimumWidth(220)
        self._pwr_mode_combo.currentIndexChanged.connect(self._update_pwr_inputs)
        gl.addWidget(self._pwr_mode_combo, 0, 1, 1, 2)

        gl.addWidget(_label('Percentage:'), 1, 0)
        self._pwr_pct = _spin(0, 100, 100, '%')
        gl.addWidget(self._pwr_pct, 1, 1)

        gl.addWidget(_label('Fixed value:'), 2, 0)
        self._pwr_w = _spin(0, 6000, 6000, 'W')
        gl.addWidget(self._pwr_w, 2, 1)

        note = _label('⚠  Register 40525 — may return "illegal address" on some firmware.',
                       C['warning'])
        note.setWordWrap(True)
        gl.addWidget(note, 3, 0, 1, 3)

        self._pwr_apply = _btn('Apply')
        self._pwr_apply.clicked.connect(self._apply_active_power)
        gl.addWidget(self._pwr_apply, 0, 3, 4, 1, Qt.AlignmentFlag.AlignVCenter)
        gl.setColumnStretch(4, 1)
        self._update_pwr_inputs()
        root.addWidget(g)

        # ── Reactive power ────────────────────────────────────────────────────
        g = _group('Reactive Power / Power Factor')
        gl = QGridLayout(g)
        gl.setSpacing(10)

        gl.addWidget(_label('Target power factor:'), 0, 0)
        self._pf_spin = QDoubleSpinBox()
        self._pf_spin.setRange(-1.0, 1.0)
        self._pf_spin.setSingleStep(0.01)
        self._pf_spin.setValue(1.0)
        self._pf_spin.setDecimals(3)
        self._pf_spin.setFixedWidth(110)
        gl.addWidget(self._pf_spin, 0, 1)

        note2 = _label('Writes to 40122 (PF setpoint, /1000). Check grid code before changing.',
                        C['muted'])
        note2.setWordWrap(True)
        gl.addWidget(note2, 1, 0, 1, 3)

        self._pf_apply = _btn('Apply')
        self._pf_apply.clicked.connect(self._apply_power_factor)
        gl.addWidget(self._pf_apply, 0, 3, 2, 1, Qt.AlignmentFlag.AlignVCenter)
        gl.setColumnStretch(4, 1)
        root.addWidget(g)

        # ── Device ────────────────────────────────────────────────────────────
        g = _group('Device')
        gl = QHBoxLayout(g)
        gl.setSpacing(10)
        self._shutdown_btn = _btn('⏻  Shutdown Inverter', C['danger'])
        self._shutdown_btn.clicked.connect(self._do_shutdown)
        gl.addWidget(self._shutdown_btn)
        gl.addWidget(_label('Sends 1 to register 40200. Use the physical switch to restart.',
                            C['muted']))
        gl.addStretch()
        root.addWidget(g)

        root.addStretch()

        # Disable all controls until worker connects
        self._set_controls_enabled(False)

    # ── Connection ────────────────────────────────────────────────────────────

    def set_worker(self, worker: Optional['ModbusWorker']):
        if self._worker:
            try:
                self._worker.write_result.disconnect(self._on_write_result)
                self._worker.connection_changed.disconnect(self._on_conn)
            except Exception:
                pass
        self._worker = worker
        if worker:
            worker.write_result.connect(self._on_write_result)
            worker.connection_changed.connect(self._on_conn)
            self._refresh_from_inverter()
        else:
            self._set_controls_enabled(False)
            self._status('Not connected — connect to the inverter first.', C['warning'])

    @Slot(bool, str)
    def _on_conn(self, ok: bool, msg: str):
        self._set_controls_enabled(ok)
        if ok:
            self._status('Connected — values loaded from inverter.', C['success'])
            QTimer.singleShot(500, self._refresh_from_inverter)
        else:
            self._status('Disconnected.', C['danger'])

    def _set_controls_enabled(self, en: bool):
        for w in (self._mode_apply, self._limits_apply, self._soc_apply,
                  self._force_chg, self._force_dchg, self._force_stop,
                  self._grid_apply, self._pwr_apply, self._pf_apply,
                  self._shutdown_btn):
            w.setEnabled(en)

    # ── Read current values back from inverter ────────────────────────────────

    def _refresh_from_inverter(self):
        if not self._worker:
            return
        w = self._worker

        mode = w.read_u16_now(47086)
        if mode is not None:
            idx = self._mode_combo.findData(mode)
            if idx >= 0:
                self._mode_combo.setCurrentIndex(idx)

        max_chg = w.read_u32_now(47075)
        if max_chg is not None:
            self._max_chg.setValue(int(max_chg))

        max_dchg = w.read_u32_now(47077)
        if max_dchg is not None:
            self._max_dchg.setValue(int(max_dchg))

        chg_soc = w.read_u16_now(47088)
        if chg_soc is not None:
            self._chg_soc.setValue(int(chg_soc // 10))

        dchg_soc = w.read_u16_now(47089)
        if dchg_soc is not None:
            self._dchg_soc.setValue(int(dchg_soc // 10))

        grid_en = w.read_u16_now(47087)
        if grid_en is not None:
            self._grid_chg_en.setChecked(bool(grid_en))

        grid_pwr = w.read_u32_now(47079)
        if grid_pwr is not None:
            self._grid_chg_pwr.setValue(int(grid_pwr))

    # ── Write handlers ────────────────────────────────────────────────────────

    def _apply_working_mode(self):
        if not self._worker:
            return
        code = self._mode_combo.currentData()
        name = self._mode_combo.currentText()
        self._worker.write_u16(47086, code, f'Working mode → {name}')

    def _apply_power_limits(self):
        if not self._worker:
            return
        self._worker.write_u32(47075, self._max_chg.value(),  'Max charge power')
        self._worker.write_u32(47077, self._max_dchg.value(), 'Max discharge power')

    def _apply_soc_targets(self):
        if not self._worker:
            return
        self._worker.write_u16(47088, self._chg_soc.value()  * 10, 'Charge cut-off SOC')
        self._worker.write_u16(47089, self._dchg_soc.value() * 10, 'Discharge cut-off SOC')

    def _force_command(self, cmd: int):
        if not self._worker:
            return
        labels = {0: 'Stop forced mode', 1: 'Force CHARGE', 2: 'Force DISCHARGE'}
        colours = {0: C['muted'], 1: C['success'], 2: C['warning']}
        label = labels[cmd]
        if cmd != 0:
            if not _confirm(self, 'Confirm', f'{label} at {self._force_pwr.value()} W?'):
                return
            self._worker.write_u32(47098, self._force_pwr.value(), 'Forced power')
        self._worker.write_u16(47100, cmd, label)
        self._status(f'Sent: {label}', colours[cmd])

    def _apply_grid_charge(self):
        if not self._worker:
            return
        en = 1 if self._grid_chg_en.isChecked() else 0
        self._worker.write_u16(47087, en, 'Grid charge enable')
        self._worker.write_u32(47079, self._grid_chg_pwr.value(), 'Grid charge power')

    def _apply_active_power(self):
        if not self._worker:
            return
        mode = self._pwr_mode_combo.currentData()
        self._worker.write_u16(40525, mode, 'Active power mode')
        if mode == 1:
            self._worker.write_u16(40526, self._pwr_pct.value() * 10, 'Active power %')
        elif mode == 2:
            self._worker.write_i32(40527, self._pwr_w.value(), 'Active power W')

    def _apply_power_factor(self):
        if not self._worker:
            return
        raw = int(self._pf_spin.value() * 1000)
        self._worker.write_u16(40122, raw & 0xFFFF, f'Power factor → {self._pf_spin.value():.3f}')

    def _do_shutdown(self):
        if not self._worker:
            return
        if _confirm(self, 'Shutdown inverter?',
                    'This will send a remote shutdown command to the inverter.\n'
                    'The inverter will stop generating and disconnect from the grid.\n\n'
                    'Use the physical DC switch or wait for sunrise to restart.\n\n'
                    'Continue?'):
            self._worker.write_u16(40200, 1, 'Device shutdown')

    # ── Helpers ───────────────────────────────────────────────────────────────

    @Slot(bool, str)
    def _on_write_result(self, ok: bool, msg: str):
        colour = C['success'] if ok else C['danger']
        self._status(msg, colour)

    def _status(self, msg: str, colour: str = C['text']):
        self._status_lbl.setText(msg)
        self._status_lbl.setStyleSheet(
            f'color:{colour}; background:{C["surface"]}; '
            f'border:1px solid {C["border"]}; border-radius:6px; padding:8px 12px;'
        )

    def _update_pwr_inputs(self):
        mode = self._pwr_mode_combo.currentData()
        self._pwr_pct.setEnabled(mode == 1)
        self._pwr_w.setEnabled(mode == 2)
