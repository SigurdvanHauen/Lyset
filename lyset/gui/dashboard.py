"""
Live dashboard — dark-themed value cards that update on every poll.
"""
from __future__ import annotations
from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QGridLayout, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QSizePolicy, QScrollArea,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

# ── Colour palette ────────────────────────────────────────────────────────────

C = {
    'bg':         '#0F1117',
    'surface':    '#1C2032',
    'surface2':   '#252A3D',
    'border':     '#2D3555',
    'text':       '#E2E8F4',
    'muted':      '#8892AA',
    # semantic accents
    'pv':      '#F59E0B',   # amber
    'grid':    '#60A5FA',   # blue
    'battery': '#34D399',   # emerald
    'load':    '#F87171',   # red
    'energy':  '#A78BFA',   # purple
    'temp':    '#FB923C',   # orange
    'status':  '#94A3B8',   # slate
}

SECTION_STYLE = (
    f'color:{C["muted"]}; font-size:11px; font-weight:700; letter-spacing:1px;'
    f'border-bottom:1px solid {C["border"]}; padding-bottom:5px; text-transform:uppercase;'
)


class ValueCard(QFrame):
    """Single metric display card."""

    def __init__(self, title: str, unit: str, accent: str, fmt: str = '.1f', parent=None):
        super().__init__(parent)
        self._fmt = fmt
        self._unit = unit

        self.setStyleSheet(f'''
            ValueCard {{
                background: {C["surface"]};
                border: 1px solid {C["border"]};
                border-top: 3px solid {accent};
                border-radius: 8px;
            }}
        ''')
        self.setMinimumWidth(145)
        self.setMinimumHeight(90)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(3)

        self._title_lbl = QLabel(title)
        self._title_lbl.setStyleSheet(f'color:{C["muted"]}; font-size:11px; font-weight:600;'
                                       f'border:none; background:transparent;')

        self._value_lbl = QLabel('—')
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        self._value_lbl.setFont(font)
        self._value_lbl.setStyleSheet(f'color:{accent}; border:none; background:transparent;')

        self._unit_lbl = QLabel(unit)
        self._unit_lbl.setStyleSheet(f'color:{C["muted"]}; font-size:11px;'
                                      f'border:none; background:transparent;')

        layout.addWidget(self._title_lbl)
        layout.addWidget(self._value_lbl)
        layout.addWidget(self._unit_lbl)

    def update_value(self, value: Optional[float | str]):
        if value is None:
            self._value_lbl.setText('—')
        elif isinstance(value, str):
            self._value_lbl.setText(value[:28])
        else:
            self._value_lbl.setText(f'{value:{self._fmt}}')


class DashboardPanel(QScrollArea):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(f'background:{C["bg"]}; border:none;')

        container = QWidget()
        container.setStyleSheet(f'background:{C["bg"]};')
        self.setWidget(container)

        root = QVBoxLayout(container)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(20)

        self.cards: dict[str, ValueCard] = {}

        # ── Power Flow ────────────────────────────────────────────────────────
        root.addWidget(self._section('Power Flow'))
        g = QGridLayout(); g.setSpacing(10)
        self._row(g, [
            ('active_power',      'PV Output',      'W',   'pv',      '.0f'),
            ('meter_active_power','Grid',            'W',   'grid',    '.0f'),
            ('batt_power',        'Battery',         'W',   'battery', '.0f'),
            ('house_load',        'House Load',      'W',   'load',    '.0f'),
        ], cols=4)
        root.addLayout(g)

        # ── PV Strings ────────────────────────────────────────────────────────
        root.addWidget(self._section('PV Strings'))
        g = QGridLayout(); g.setSpacing(10)
        self._row(g, [
            ('pv1_voltage', 'String 1 Voltage', 'V',  'pv', '.1f'),
            ('pv1_current', 'String 1 Current', 'A',  'pv', '.2f'),
            ('pv1_power',   'String 1 Power',   'W',  'pv', '.0f'),
            ('pv2_voltage', 'String 2 Voltage', 'V',  'pv', '.1f'),
            ('pv2_current', 'String 2 Current', 'A',  'pv', '.2f'),
            ('pv2_power',   'String 2 Power',   'W',  'pv', '.0f'),
        ], cols=6)
        root.addLayout(g)

        # ── Grid ──────────────────────────────────────────────────────────────
        root.addWidget(self._section('Grid'))
        g = QGridLayout(); g.setSpacing(10)
        self._row(g, [
            ('grid_voltage_a', 'Voltage L1',    'V',  'grid', '.1f'),
            ('grid_voltage_b', 'Voltage L2',    'V',  'grid', '.1f'),
            ('grid_voltage_c', 'Voltage L3',    'V',  'grid', '.1f'),
            ('grid_frequency', 'Frequency',     'Hz', 'grid', '.2f'),
            ('power_factor',   'Power Factor',  '',   'grid', '.3f'),
        ], cols=5)
        root.addLayout(g)

        # ── Battery ───────────────────────────────────────────────────────────
        root.addWidget(self._section('Battery (LUNA2000)'))
        g = QGridLayout(); g.setSpacing(10)
        self._row(g, [
            ('batt_soc',          'State of Charge', '%',  'battery', '.1f'),
            ('batt_soh',          'State of Health', '%',  'battery', '.1f'),
            ('batt_temperature',  'Temperature',     '°C', 'battery', '.1f'),
            ('batt_bus_voltage',  'Bus Voltage',     'V',  'battery', '.1f'),
            ('batt_rated_capacity','Rated Capacity', 'kWh','battery', '.1f'),
            ('batt_status_label', 'Status',          '',   'status',  ''),
        ], cols=6)
        root.addLayout(g)

        # ── Energy Totals ─────────────────────────────────────────────────────
        root.addWidget(self._section('Energy Totals'))
        g = QGridLayout(); g.setSpacing(10)
        self._row(g, [
            ('daily_yield',         'Today Yield',    'kWh', 'energy', '.2f'),
            ('total_yield',         'Total Yield',    'kWh', 'energy', '.1f'),
            ('meter_export_energy', 'Grid Exported',  'kWh', 'energy', '.1f'),
            ('meter_import_energy', 'Grid Imported',  'kWh', 'energy', '.1f'),
            ('batt_charge_total',   'Batt Charged',   'kWh', 'battery','.1f'),
            ('batt_discharge_total','Batt Discharged','kWh', 'battery','.1f'),
        ], cols=6)
        root.addLayout(g)

        # ── Device ────────────────────────────────────────────────────────────
        root.addWidget(self._section('Device'))
        g = QGridLayout(); g.setSpacing(10)
        self._row(g, [
            ('internal_temp',        'Inverter Temp', '°C', 'temp',   '.1f'),
            ('inverter_state_label', 'State',         '',   'status', ''),
        ], cols=4)
        root.addLayout(g)

        root.addStretch()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text.upper())
        lbl.setStyleSheet(SECTION_STYLE)
        return lbl

    def _row(self, grid: QGridLayout, specs: list, cols: int):
        for i, (key, title, unit, theme, fmt) in enumerate(specs):
            accent = C.get(theme, C['status'])
            card = ValueCard(title, unit, accent, fmt)
            self.cards[key] = card
            grid.addWidget(card, i // cols, i % cols)

    def update_data(self, data: dict):
        for key, card in self.cards.items():
            if key in data:
                card.update_value(data[key])
