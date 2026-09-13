"""
NasBackupWorker — periodically backs up the history DB to a NAS over SMB, then
prunes old rows from the LIVE database so it stays small on disk.

Runs as a daemon thread, independent of the poll/eval loops, following the
same worker pattern as SolcastWorker/EVChargerWorker (see solcast.py,
ev_charger.py): worker_from_env() returns None — and the feature stays fully
off — until NAS_HOST/NAS_SHARE/NAS_USERNAME/NAS_PASSWORD are all configured.

SAFETY ORDERING: prune_old_history() only runs after a successful SMB upload
of that cycle's snapshot. A NAS outage, wrong credentials, or a network blip
just means the local DB keeps growing until the next successful cycle —
never data loss.

Each cycle uploads a FULL, dated snapshot (lyset_history_YYYY-MM-DD.db) rather
than overwriting one fixed filename or incrementally archiving only the
about-to-be-pruned rows. That's deliberately simple: since local pruning
shrinks the live DB over time but Lyset never deletes anything on the NAS
side, the very first snapshot (taken before any pruning has happened) already
holds the complete history from before this feature was ever turned on, and
every later snapshot holds everything since. Full history is preserved by
construction, at the cost of some NAS storage — which is the whole point of
moving it off the local disk. If the NAS fills up, that's managed on the NAS
/ by the user, not by this worker.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import store

log = logging.getLogger(__name__)

_MIN_INTERVAL_S = 3600.0    # floor — never back up more than once an hour
_MIN_RETENTION_DAYS = 1.0   # floor — refuse to prune same-day data


def _upload_smb(local_path: Path, host: str, share: str, remote_dir: str,
                remote_name: str, username: str, password: str, port: int = 445):
    """Upload local_path to \\\\host\\share\\remote_dir\\remote_name, creating
    remote_dir (and any missing parent segments) if needed. Uses pysmb — a
    pure-Python SMB2/3 client — rather than an OS-level mount, so this needs no
    system changes on the host (cifs-utils, fstab, root) beyond `pip install`.

    is_direct_tcp=True is required for port 445 (what virtually every modern
    NAS uses by default — 139 is the legacy NetBIOS-only port, often disabled).
    Without it pysmb defaults to the NetBIOS session-service handshake and the
    connection fails against a 445-only server.
    """
    from smb.SMBConnection import SMBConnection

    conn = SMBConnection(username, password, 'lyset', host, use_ntlm_v2=True,
                         is_direct_tcp=True)
    if not conn.connect(host, port, timeout=30):
        raise ConnectionError(f'SMB connect/auth to {host}:{port} failed')
    try:
        remote_dir = remote_dir.strip('/\\')
        built = ''
        for part in ([p for p in remote_dir.split('/') if p] if remote_dir else []):
            built = f'{built}/{part}' if built else part
            try:
                conn.createDirectory(share, built)
            except Exception:
                pass  # already exists — pysmb has no "if not exists" check
        remote_path = f'{remote_dir}/{remote_name}' if remote_dir else remote_name
        with open(local_path, 'rb') as f:
            conn.storeFile(share, remote_path, f)
    finally:
        conn.close()


class NasBackupWorker(threading.Thread):
    """Background thread: snapshots the history DB, uploads it to a NAS share
    over SMB, then prunes local rows older than retention_days. Disabled
    (never started) unless NAS credentials are configured — see
    worker_from_env()."""

    def __init__(
        self,
        host: str,
        share: str,
        username: str,
        password: str,
        remote_dir: str = 'Lyset',
        interval: float = 86400.0,
        retention_days: float = 60.0,
    ):
        super().__init__(daemon=True, name='NasBackupWorker')
        self._host = host
        self._share = share
        self._username = username
        self._password = password
        self._remote_dir = remote_dir
        self._interval = max(interval, _MIN_INTERVAL_S)
        self._retention_days = max(retention_days, _MIN_RETENTION_DAYS)
        self._running = False

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        # One-time cleanup of ev_probe_log — a dead artifact from early EV-
        # charger exploration (see ev_charger.py's module docstring); no
        # current code creates or reads it.
        try:
            store.drop_table_if_exists('ev_probe_log')
        except Exception as exc:
            log.warning('NasBackup: ev_probe_log cleanup failed — %s', exc)

        while self._running:
            try:
                self._cycle()
            except Exception as exc:
                log.error('NasBackup: cycle failed — local data NOT pruned — %s',
                         exc, exc_info=True)
            self._sleep(self._interval)

    def _sleep(self, seconds: float):
        deadline = time.time() + seconds
        while self._running and time.time() < deadline:
            time.sleep(1)

    def _cycle(self):
        remote_name = f'lyset_history_{datetime.now().strftime("%Y-%m-%d")}.db'

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_path = Path(tmp) / remote_name
            store.backup_to_file(str(snapshot_path))
            size_mb = snapshot_path.stat().st_size / 1e6
            log.info('NasBackup: snapshot %.1f MB — uploading to \\\\%s\\%s\\%s\\%s',
                     size_mb, self._host, self._share, self._remote_dir, remote_name)
            _upload_smb(snapshot_path, self._host, self._share, self._remote_dir,
                       remote_name, self._username, self._password)

        log.info('NasBackup: upload OK — pruning local rows older than %.0f days',
                 self._retention_days)
        cutoff_s = time.time() - self._retention_days * 86400.0
        deleted = store.prune_old_history(cutoff_s)
        log.info('NasBackup: pruned %s', deleted)


def worker_from_env(**kwargs) -> Optional['NasBackupWorker']:
    """Build a NasBackupWorker from environment variables, or None if the NAS
    isn't fully configured."""
    host     = os.getenv('NAS_HOST', '').strip()
    share    = os.getenv('NAS_SHARE', '').strip()
    username = os.getenv('NAS_USERNAME', '').strip()
    password = os.getenv('NAS_PASSWORD', '')
    if not host or not share or not username or not password:
        log.info('NasBackup: NAS_HOST/NAS_SHARE/NAS_USERNAME/NAS_PASSWORD not fully '
                 'set — NAS backup disabled')
        return None

    remote_dir = os.getenv('NAS_REMOTE_DIR', 'Lyset').strip() or 'Lyset'
    try:
        interval = float(os.getenv('NAS_BACKUP_INTERVAL_S', '86400'))
    except (TypeError, ValueError):
        interval = 86400.0
    try:
        retention_days = float(os.getenv('NAS_RETENTION_DAYS', '60'))
    except (TypeError, ValueError):
        retention_days = 60.0

    return NasBackupWorker(
        host=host, share=share, username=username, password=password,
        remote_dir=remote_dir, interval=interval, retention_days=retention_days,
        **kwargs,
    )
