"""Host inspection and portable data operations, independent of HTTP."""
import base64
import ctypes
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import zipfile
import threading
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path, PurePosixPath


def system_health():
    from .metrics import sample_resources
    metrics = sample_resources()
    disk = shutil.disk_usage(Path.cwd())
    return {**metrics, 'cpu_count': os.cpu_count(), 'disk_free_bytes': disk.free,
            'disk_total_bytes': disk.total,
            'memory_total_bytes': int(metrics['ram_total_mb']*1048576) if metrics['ram_total_mb'] is not None else None,
            'memory_available_bytes': int(metrics['ram_available_mb']*1048576) if metrics['ram_available_mb'] is not None else None,
            'gpu_error': '' if metrics['gpu'] else 'GPU metrics unavailable; install the NVIDIA driver tools to enable them.'}

def export_backup(store, maximum=512 * 1024 * 1024):
    """Call only under all project locks with project execution stopped."""
    output = io.BytesIO()
    total = 0
    with tempfile.TemporaryDirectory() as temporary:
        db_copy = Path(temporary) / 'cabin.db'
        with closing(sqlite3.connect(store.root / 'cabin.db')) as source, closing(sqlite3.connect(db_copy)) as target:
            source.backup(target)
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(db_copy, 'cabin.db')
            for base, dirs, names in os.walk(store.root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in ('backups',) and not (Path(base)/d).is_symlink()]
                for name in names:
                    path = Path(base) / name
                    relative = path.relative_to(store.root).as_posix()
                    if path.is_symlink() or relative in ('cabin.db', 'cabin.db-wal', 'cabin.db-shm', '.cabin.lock'):
                        continue
                    total += path.stat().st_size
                    if total > maximum:
                        raise ValueError('Backup exceeds the 512 MB browser limit. Stop The Workshop and copy the data folder directly.')
                    archive.write(path, relative)
            archive.writestr('WORKSHOP-BACKUP.json', json.dumps({'format': 1, 'created_unix': time.time(),
                                  'restore': 'Stop The Workshop, extract into an empty data directory, then start with --data-dir.'}, indent=2))
    return output.getvalue()


def validated_import(encoded):
    """Validate all members before extracting anything; return (name, data) pairs."""
    if not isinstance(encoded, str) or len(encoded) > 28_000_000:
        raise ValueError('Import a ZIP archive of at most 20 MB.')
    try:
        raw = base64.b64decode(encoded, validate=True)
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (ValueError, zipfile.BadZipFile) as exc:
        raise ValueError('The import must be a valid base64-encoded ZIP archive.') from exc
    entries, total = [], 0
    with archive:
        if len(archive.infolist()) > 3000:
            raise ValueError('Import is limited to 3,000 files.')
        for member in archive.infolist():
            name = member.filename
            path = PurePosixPath(name)
            if not name or '\\' in name or ':' in name or '\x00' in name or path.is_absolute() or '..' in path.parts:
                raise ValueError('Archive contains an unsafe path.')
            if any(part.lower().rstrip('. ') == '.git' for part in path.parts):
                raise ValueError('Remove .git metadata before importing; The Workshop creates a fresh repository.')
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError('Symbolic links are not allowed in project imports.')
            if member.flag_bits & 1:
                raise ValueError('Encrypted ZIP members are not supported.')
            total += member.file_size
            if total > 80_000_000 or member.file_size > 10_000_000:
                raise ValueError('Imported files exceed the 80 MB expanded / 10 MB per-file limit.')
            if member.is_dir():
                continue
            entries.append((path.as_posix(), archive.read(member)))
    if not entries:
        raise ValueError('The archive has no files.')
    # Common export ZIPs wrap their source in one directory.
    first_parts = [PurePosixPath(n).parts for n, _ in entries]
    if all(len(p) > 1 and p[0] == first_parts[0][0] for p in first_parts):
        entries = [('/'.join(PurePosixPath(n).parts[1:]), content) for n, content in entries]
    names = [name.casefold() for name, _ in entries]
    if len(set(names)) != len(names):
        raise ValueError('Archive contains duplicate or case-colliding paths.')
    return entries


class BackupScheduler:
    """Optional idle-time backups; scheduling never interrupts active work."""
    def __init__(self, app, clock=time.time):
        self.app, self.clock = app, clock
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._loop, name='workshop-backups', daemon=True)
        self.next_attempt = 0

    def status(self):
        store = self.app.store
        hours = int(store.setting('backup_interval_hours', '0'))
        last = float(store.setting('backup_last_success', '0'))
        return dict(last_success=last or None, error=store.setting('backup_error'),
                    next_due=(last+hours*3600) if hours else None,
                    interval_hours=hours, keep=int(store.setting('backup_keep', '5')))

    def tick(self):
        status, current = self.status(), self.clock()
        if not status['interval_hours'] or current < self.next_attempt:
            return False
        if status['last_success'] and current < status['next_due']:
            return False
        # Poll busy projects without noisy events or repeated error notifications.
        self.next_attempt = current + 60
        try:
            payload = self.app.backup()
            directory = self.app.store.root / 'backups'
            if directory.is_symlink():
                raise ValueError('Backup directory must not be a symbolic link.')
            root = directory.resolve()
            if not root.is_relative_to(self.app.store.root.resolve()):
                raise ValueError('Backup destination must stay inside the data directory.')
            root.mkdir(exist_ok=True)
            stamp = datetime.fromtimestamp(current, timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            destination = root / ('workshop-' + stamp + '.zip')
            temporary = root / ('workshop-' + stamp + '.partial')
            temporary.write_bytes(payload)
            temporary.replace(destination)
            owned = sorted((p for p in root.glob('workshop-*.zip') if p.is_file() and not p.is_symlink()
                            and len(p.name) == len('workshop-20261028T010000Z.zip')
                            and p.name[9:17].isdigit() and p.name[18:24].isdigit()), key=lambda p: p.name, reverse=True)
            for path in owned[status['keep']:]:
                checked = path.resolve()
                if checked.parent != root:
                    raise ValueError('Backup retention target escaped the backup directory.')
                checked.unlink()
            self.app.store.set_setting('backup_last_success', str(current))
            self.app.store.set_setting('backup_error', '')
            self.app.store.event(None, 'system', 'Automatic backup saved: ' + destination.name)
            return True
        except Exception as exc:
            if getattr(exc, 'status', None) == 409:
                return False
            message = 'Automatic backup failed: ' + str(exc)
            previous = self.app.store.setting('backup_error')
            self.app.store.set_setting('backup_error', message)
            if previous != message:
                from .store import now
                self.app.store.execute('INSERT INTO notifications(kind,message,created) VALUES (?,?,?)', ('backup', message, now()))
                self.app.store.event(None, 'system', message)
            return False

    def _loop(self):
        while not self.stopping.wait(30):
            self.tick()
