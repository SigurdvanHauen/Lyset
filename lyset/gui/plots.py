"""
Real-time strip-chart panel using pyqtgraph.

Shows the last N minutes of data as scrolling time-series plots.
"""

from __future__ import annotations
import time
from collections import deque
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QSpinBox, QFrame, QSizePolicy,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

pg.setConfigOptions(antialias=True, background='#0F1117', foreground='#8892AA')


# ── Colours for each series ───────────────────────────────────────────────────

SERIES_STYLES: dict[str, dict] = {
    'active_power':       {'color': '#F59E0B', 'name': 'PV Output (W)'},
    'pv1_power':          {'color': '#FCD34D', 'name': 'String 1 (W)'},
    'pv2_power':          {'color': '#FBBF24', 'name': 'String 2 (W)'},
    'meter_active_power': {'color': '#60A5FA', 'name': 'Grid (W)'},
    'batt_power':         {'color': '#34D399', 'name': 'Battery (W)'},
    'house_load':         {'color': '#F87171', 'name': 'House Load (W)'},
    'batt_soc':           {'color': '#6EE7B7', 'name': 'Battery SOC (%)'},
    'internal_temp':      {'color': '#FB923C', 'name': 'Inv. Temp (°C)'},
    'grid_voltage_a':     {'color': '#93C5FD', 'name': 'Grid V L1 (V)'},
    'grid_frequency':     {'color': '#7DD3FC', 'name': 'Grid Freq (Hz)'},
}

# Which series appear in which plot (can share a plot if same units)
PLOT_GROUPS: list[dict] = [
    {
        'title': 'Power Flow',
        'unit': 'W',
        'series': ['active_power', 'meter_active_power', 'batt_power', 'house_load'],
        'zero_line': True,
    },
    {
        'title': 'PV Strings',
        'unit': 'W',
        'series': ['pv1_power', 'pv2_power'],
        'zero_line': False,
    },
    {
        'title': 'Battery SOC',
        'unit': '%',
        'series': ['batt_soc'],
        'zero_line': False,
        'y_range': (0, 100),
    },
    {
        'title': 'Grid Voltage & Frequency',
        'unit': 'mixed',
        'series': ['grid_voltage_a', 'grid_frequency'],
        'zero_line': False,
    },
]


class SeriesBuffer:
    """Ring buffer of (timestamp, value) pairs."""

    def __init__(self, maxlen: int = 3600):
        self._t: deque[float] = deque(maxlen=maxlen)
        self._v: deque[float] = deque(maxlen=maxlen)

    def append(self, t: float, v: float):
        self._t.append(t)
        self._v.append(v)

    def arrays(self, window_s: float) -> tuple[np.ndarray, np.ndarray]:
        if not self._t:
            return np.array([]), np.array([])
        cutoff = time.time() - window_s
        t = np.array(self._t)
        v = np.array(self._v)
        mask = t >= cutoff
        return t[mask], v[mask]

    def clear(self):
        self._t.clear()
        self._v.clear()


class PlotGroup(QWidget):
    """One plot with multiple series and a legend."""

    def __init__(self, config: dict, buffers: dict[str, SeriesBuffer], parent=None):
        super().__init__(parent)
        self._config = config
        self._buffers = buffers
        self._curves: dict[str, pg.PlotDataItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(0)

        self._plot_widget = pg.PlotWidget(title=config['title'])
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.setLabel('left', config['unit'])
        self._plot_widget.setLabel('bottom', 'Time', units='s ago')
        self._plot_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        if config.get('zero_line'):
            zero = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('#3D4466', width=1, style=Qt.PenStyle.DashLine))
            self._plot_widget.addItem(zero)

        if 'y_range' in config:
            self._plot_widget.setYRange(*config['y_range'])

        legend = self._plot_widget.addLegend(offset=(10, 10))

        for key in config['series']:
            style = SERIES_STYLES.get(key, {'color': '#757575', 'name': key})
            pen = pg.mkPen(color=style['color'], width=2)
            curve = self._plot_widget.plot([], [], pen=pen, name=style['name'])
            self._curves[key] = curve

        layout.addWidget(self._plot_widget)

    def refresh(self, window_s: float):
        now = time.time()
        for key, curve in self._curves.items():
            buf = self._buffers.get(key)
            if buf is None:
                continue
            t_arr, v_arr = buf.arrays(window_s)
            if len(t_arr) == 0:
                curve.setData([], [])
            else:
                # Express x axis as "seconds ago" (negative = past)
                curve.setData(-(now - t_arr), v_arr)

        # Auto-range x to window
        self._plot_widget.setXRange(-window_s, 0, padding=0.02)


class PlotPanel(QWidget):
    """Panel containing all PlotGroup widgets and window controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._window_s = 600  # default: last 10 minutes
        self._buffers: dict[str, SeriesBuffer] = {
            key: SeriesBuffer(maxlen=7200) for key in SERIES_STYLES
        }

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ── Control bar ───────────────────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel('Window:'))

        self._window_combo = QComboBox()
        for label, secs in [('5 min', 300), ('15 min', 900), ('30 min', 1800),
                             ('1 h', 3600), ('3 h', 10800), ('Today', 86400)]:
            self._window_combo.addItem(label, secs)
        self._window_combo.setCurrentIndex(1)  # 15 min default
        self._window_s = 900
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)
        ctrl.addWidget(self._window_combo)
        ctrl.addStretch()
        root.addLayout(ctrl)

        # ── Plot groups ───────────────────────────────────────────────────────
        self._groups: list[PlotGroup] = []
        for cfg in PLOT_GROUPS:
            grp = PlotGroup(cfg, self._buffers)
            grp.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._groups.append(grp)
            root.addWidget(grp)

    # ── public ────────────────────────────────────────────────────────────────

    def ingest(self, data: dict):
        """Store a new snapshot in all relevant buffers."""
        t = data.get('_timestamp', time.time())
        for key in self._buffers:
            if key in data and isinstance(data[key], (int, float)):
                self._buffers[key].append(t, float(data[key]))
        self.refresh()

    def refresh(self):
        for grp in self._groups:
            grp.refresh(self._window_s)

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_window_changed(self, _idx: int):
        self._window_s = self._window_combo.currentData()
        self.refresh()
