"""Portable The Workshop launcher. Python 3.11+ and Git are the only prerequisites."""
import argparse
import getpass
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def instance_lock(data_dir):
    """Hold an OS-released lock so crash recovery never races another server."""
    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = (data_dir / '.cabin.lock').open('a+b')
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'1')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SystemExit('Another The Workshop instance is already using this data directory. Stop it or choose --data-dir.') from exc
        yield
    finally:
        # Closing the descriptor releases the lock even after an exception.
        handle.close()


def main():
    if sys.version_info < (3, 11):
        raise SystemExit('The Workshop requires Python 3.11 or newer.')
    parser = argparse.ArgumentParser(description='The Workshop — your offline AI development studio')
    parser.add_argument('--host', default='127.0.0.1', help='Use 0.0.0.0 to allow devices on your local Wi-Fi')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--data-dir', '--data', type=Path, default=Path(__file__).resolve().parent / 'data')
    parser.add_argument('--reset-password', action='store_true', help='Set a new panel password and invalidate existing sessions')
    parser.add_argument('--backup', type=Path, help='With the server stopped, save a complete backup ZIP and exit')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    if not shutil.which('git'):
        raise SystemExit('Git is required. Install Git, then restart this terminal.')
    password = os.environ.get('CABIN_PASSWORD')
    if args.reset_password:
        password = getpass.getpass('New password (at least 12 characters): ')
        if password != getpass.getpass('Confirm password: '):
            raise SystemExit('Passwords do not match.')
    with instance_lock(args.data_dir):
        if args.backup:
            from cabin.store import Store
            from cabin.operations import export_backup
            if args.backup.resolve().is_relative_to(args.data_dir.resolve()):
                raise SystemExit('Choose a backup destination outside the data directory.')
            args.backup.parent.mkdir(parents=True, exist_ok=True)
            with args.backup.open('xb') as output:
                output.write(export_backup(Store(args.data_dir)))
            print('Backup saved: ' + str(args.backup.resolve()))
            return
        from cabin.server import serve
        serve(args.data_dir, args.host, args.port, password)


if __name__ == '__main__':
    main()
