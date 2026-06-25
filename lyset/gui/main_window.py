"""
Main application window.

Layout:
  ┌─────────────────────────────────────┐
  │  toolbar: connection settings + status│
  ├─────────────────────────────────────┤
  │  tab bar:  Dashboard  │  Plots      │
  ├─────────────────────────────────────┤
  │  (tab content)                      │
  └─────────────────────────────────────┘
"""

from __future__ import annotations
import csv
import time
import os
from datetime import datetime
from typing import Optional

import logging

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox,
    QPushButton, QStatusBar, QFrame, QFileDialog, QMessageBox,
    QGroupBox, QToolBar, QSizePolicy, QTextEdit,
)
from PySide6.QtCore import Qt, QTimer, Slot, QObject, Signal
from PySide6.QtGui import QIcon, QColor, QPalette, QTextCursor, QFont

from ..modbus_client import ModbusWorker
from .dashboard import DashboardPanel
from .plots import PlotPanel


# ── Qt logging handler that writes to a QTextEdit ────────────────────────────

class _LogSignalBridge(QObject):
    """Lives in the GUI thread; receives log strings from any thread via queued signal."""
    record_ready = Signal(str, str)  # (colour, escaped_text)


class _QtLogHandler(logging.Handler):
    """
    Thread-safe log handler.  `emit()` may be called from any thread; it posts
    the message through a Qt queued signal so the QTextEdit is only touched from
    the GUI thread.
    """

    COLOURS = {
        logging.DEBUG:    '#6B7280',
        logging.INFO:     '#E2E8F4',
        logging.WARNING:  '#F59E0B',
        logging.ERROR:    '#F87171',
        logging.CRITICAL: '#C084FC',
    }

    def __init__(self, text_edit: QTextEdit):
        super().__init__()
        self._te = text_edit
        self._bridge = _LogSignalBridge()
        # Connection is made in the GUI thread → delivery is always GUI thread
        self._bridge.record_ready.connect(self._append_html)
        self.setFormatter(logging.Formatter(
            '%(asctime)s  %(levelname)-8s  %(name)s — %(message)s',
            datefmt='%H:%M:%S',
        ))

    def emit(self, record: logging.LogRecord):
        try:
            colour = self.COLOURS.get(record.levelno, '#E2E8F4')
            text = self.format(record).replace('&', '&amp;').replace('<', '&lt;')
            # Signal emission is thread-safe; Qt queues it to the GUI thread
            self._bridge.record_ready.emit(colour, text)
        except Exception:
            pass

    @Slot(str, str)
    def _append_html(self, colour: str, text: str):
        self._te.append(f'<span style="color:{colour};font-family:monospace">{text}</span>')
        self._te.moveCursor(QTextCursor.MoveOperation.End)


# ── Connection toolbar widget ─────────────────────────────────────────────────

class ConnectionBar(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        layout.addWidget(QLabel('Host:'))
        self.host_edit = QLineEdit('192.168.1.185')
        self.host_edit.setFixedWidth(140)
        layout.addWidget(self.host_edit)

        layout.addWidget(QLabel('Port:'))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(502)
        self.port_spin.setFixedWidth(70)
        layout.addWidget(self.port_spin)

        layout.addWidget(QLabel('Slave ID:'))
        self.slave_spin = QSpinBox()
        self.slave_spin.setRange(0, 255)
        self.slave_spin.setValue(1)
        self.slave_spin.setFixedWidth(55)
        layout.addWidget(self.slave_spin)

        layout.addWidget(QLabel('Poll (s):'))
        self.poll_spin = QDoubleSpinBox()
        self.poll_spin.setRange(1.0, 60.0)
        self.poll_spin.setSingleStep(1.0)
        self.poll_spin.setValue(5.0)
        self.poll_spin.setFixedWidth(60)
        layout.addWidget(self.poll_spin)

        self.connect_btn = QPushButton('Connect')
        self.connect_btn.setFixedWidth(90)
        self.connect_btn.setCheckable(True)
        layout.addWidget(self.connect_btn)

        self.status_lbl = QLabel('Disconnected')
        self.status_lbl.setStyleSheet('color: #8892AA; font-style: italic;')
        layout.addWidget(self.status_lbl)

        layout.addStretch()

        self.export_btn = QPushButton('Export CSV…')
        layout.addWidget(self.export_btn)

    def set_connected(self, ok: bool, msg: str):
        self.status_lbl.setText(msg)
        colour = '#34D399' if ok else '#F87171'
        self.status_lbl.setStyleSheet(f'color: {colour}; font-weight: 600;')
        self.connect_btn.setText('Disconnect' if ok else 'Connect')
        self.connect_btn.setChecked(ok)


# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle('Lyset — SUN2000 Monitor')
        self.resize(1280, 840)
        self._worker: Optional[ModbusWorker] = None
        self._snapshots: list[dict] = []  # for CSV export

        self._build_ui()
        self._apply_stylesheet()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Connection bar
        self._conn_bar = ConnectionBar()
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet('background: #E0E0E0;')
        root.addWidget(self._conn_bar)
        root.addWidget(sep)

        # Tab widget
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        root.addWidget(self._tabs)

        self._dashboard = DashboardPanel()
        self._plots = PlotPanel()
        self._log_view = self._build_log_tab()

        self._tabs.addTab(self._dashboard, 'Dashboard')
        self._tabs.addTab(self._plots, 'Plots')
        self._tabs.addTab(self._log_view, 'Log')

        # Route all lyset + pymodbus log output into the Log tab
        handler = _QtLogHandler(self._log_view)
        handler.setLevel(logging.DEBUG)
        for name in ('lyset', 'pymodbus'):
            logger = logging.getLogger(name)
            logger.setLevel(logging.DEBUG)
            logger.addHandler(handler)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage('Ready')

        self._last_update_lbl = QLabel()
        self._status_bar.addPermanentWidget(self._last_update_lbl)

        # Wire up signals
        self._conn_bar.connect_btn.clicked.connect(self._on_connect_clicked)
        self._conn_bar.export_btn.clicked.connect(self._export_csv)

    def _build_log_tab(self) -> QTextEdit:
        te = QTextEdit()
        te.setReadOnly(True)
        te.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        font = QFont('Consolas', 10)
        font.setStyleHint(QFont.StyleHint.Monospace)
        te.setFont(font)
        te.setStyleSheet('background: #1E1E1E; color: #D4D4D4; border: none;')
        te.append('<span style="color:#9E9E9E">— Log console ready —</span>')
        return te

    def _apply_stylesheet(self):
        self.setStyleSheet('''
            /* ── Base ── */
            QWidget      { color: #E2E8F4; font-size: 13px; background: transparent; }
            QMainWindow  { background: #0F1117; }

            /* ── Connection bar ── */
            ConnectionBar {
                background: #161924;
                border-bottom: 1px solid #2D3555;
            }
            QLabel { color: #8892AA; background: transparent; }

            /* ── Input fields ── */
            QLineEdit, QSpinBox, QDoubleSpinBox {
                color: #E2E8F4;
                background: #1C2032;
                border: 1px solid #2D3555;
                border-radius: 6px;
                padding: 4px 8px;
                selection-background-color: #4F8EF7;
                selection-color: #FFFFFF;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
                border-color: #4F8EF7;
            }
            QSpinBox::up-button, QSpinBox::down-button,
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
                background: #252A3D; border: none; width: 16px;
            }
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow   { image: none; border: none; }
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow{ image: none; border: none; }

            /* ── Buttons ── */
            QPushButton {
                color: #E2E8F4;
                background: #1C2032;
                border: 1px solid #2D3555;
                border-radius: 6px;
                padding: 5px 16px;
                font-weight: 600;
            }
            QPushButton:hover   { background: #252A3D; border-color: #4F8EF7; color: #4F8EF7; }
            QPushButton:checked { background: #4F8EF7; color: #FFFFFF; border-color: #4F8EF7; }
            QPushButton:pressed { background: #3D71C8; color: #FFFFFF; }

            /* ── Tabs ── */
            QTabWidget::pane { border: none; background: #0F1117; }
            QTabBar           { background: #161924; }
            QTabBar::tab {
                color: #8892AA;
                background: #161924;
                padding: 10px 24px;
                font-size: 13px;
                font-weight: 600;
                border: none;
                border-bottom: 3px solid transparent;
            }
            QTabBar::tab:selected { color: #E2E8F4; border-bottom: 3px solid #4F8EF7; }
            QTabBar::tab:hover    { color: #E2E8F4; background: #1C2032; }

            /* ── Status bar ── */
            QStatusBar {
                color: #8892AA;
                background: #161924;
                border-top: 1px solid #2D3555;
                font-size: 12px;
            }

            /* ── Scroll bars ── */
            QScrollBar:vertical {
                width: 6px; background: #0F1117; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #2D3555; border-radius: 3px; min-height: 30px;
            }
            QScrollBar::handle:vertical:hover { background: #4F8EF7; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

            /* ── ComboBox ── */
            QComboBox {
                color: #E2E8F4;
                background: #1C2032;
                border: 1px solid #2D3555;
                border-radius: 6px;
                padding: 4px 24px 4px 8px;
            }
            QComboBox:hover  { border-color: #4F8EF7; }
            QComboBox::drop-down { border: none; width: 20px; }
            QComboBox QAbstractItemView {
                color: #E2E8F4;
                background: #1C2032;
                border: 1px solid #2D3555;
                selection-background-color: #4F8EF7;
            }

            /* ── Log console ── */
            QTextEdit { background: #0A0D14; border: none; color: #D4D4D4; }
        ''')

    # ── Slots ─────────────────────────────────────────────────────────────────

    @Slot()
    def _on_connect_clicked(self):
        if self._worker and self._worker.isRunning():
            self._stop_worker()
        else:
            self._start_worker()

    def _start_worker(self):
        bar = self._conn_bar
        self._worker = ModbusWorker(
            host=bar.host_edit.text().strip(),
            port=bar.port_spin.value(),
            slave_id=bar.slave_spin.value(),
            poll_interval=bar.poll_spin.value(),
        )
        self._worker.data_ready.connect(self._on_data)
        self._worker.connection_changed.connect(self._on_connection_changed)
        self._worker.error.connect(self._on_error)
        self._worker.start()
        self._status_bar.showMessage('Connecting…')

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None
        self._conn_bar.set_connected(False, 'Disconnected')
        self._status_bar.showMessage('Disconnected')

    @Slot(dict)
    def _on_data(self, data: dict):
        self._snapshots.append(data)
        # Keep at most 24 h of snapshots in memory (adjust as needed)
        if len(self._snapshots) > 17280:
            self._snapshots = self._snapshots[-17280:]

        self._dashboard.update_data(data)
        self._plots.ingest(data)

        ts = datetime.fromtimestamp(data.get('_timestamp', time.time()))
        self._last_update_lbl.setText(f'Last update: {ts:%H:%M:%S}')

    @Slot(bool, str)
    def _on_connection_changed(self, ok: bool, msg: str):
        self._conn_bar.set_connected(ok, msg)
        self._status_bar.showMessage(msg)
        colour = '#34D399' if ok else '#F87171'
        self._log_view.append(
            f'<span style="color:{colour}; font-family:monospace">'
            f'[connection] {msg}</span>'
        )

    @Slot(str)
    def _on_error(self, msg: str):
        self._status_bar.showMessage(f'Error: {msg}', 5000)
        self._log_view.append(
            f'<span style="color:#B71C1C; font-family:monospace">[error] {msg}</span>'
        )

    @Slot()
    def _export_csv(self):
        if not self._snapshots:
            QMessageBox.information(self, 'No data', 'No data has been collected yet.')
            return

        path, _ = QFileDialog.getSaveFileName(
            self, 'Export CSV', f'lyset_{datetime.now():%Y%m%d_%H%M%S}.csv', 'CSV files (*.csv)'
        )
        if not path:
            return

        # Collect all keys (excluding private ones)
        all_keys = sorted({k for snap in self._snapshots for k in snap if not k.startswith('_')})

        try:
            with open(path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['timestamp'] + all_keys)
                for snap in self._snapshots:
                    ts = datetime.fromtimestamp(snap.get('_timestamp', 0)).isoformat()
                    writer.writerow([ts] + [snap.get(k, '') for k in all_keys])
            QMessageBox.information(self, 'Exported', f'Saved {len(self._snapshots)} rows to:\n{path}')
        except OSError as exc:
            QMessageBox.critical(self, 'Export failed', str(exc))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._stop_worker()
        event.accept()
