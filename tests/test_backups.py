import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cabin.operations import BackupScheduler
from cabin.server import ApiError
from cabin.store import Store


class ScheduledBackupTests(unittest.TestCase):
    def test_disabled_due_busy_and_owned_retention(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary)/'data')
            moment = [1800000000.0]
            calls = []
            def backup():
                calls.append(True)
                return b'backup bytes'
            app = SimpleNamespace(store=store, backup=backup)
            scheduler = BackupScheduler(app, clock=lambda: moment[0])
            self.assertFalse(scheduler.tick())
            self.assertFalse(calls)
            store.set_setting('backup_interval_hours', '1')
            store.set_setting('backup_keep', '1')
            self.assertTrue(scheduler.tick())
            root = store.root/'backups'
            (root/'my-personal-backup.zip').write_bytes(b'keep')
            self.assertFalse(scheduler.tick())
            moment[0] += 3601
            self.assertTrue(scheduler.tick())
            self.assertEqual(len(list(root.glob('workshop-*.zip'))), 1)
            self.assertTrue((root/'my-personal-backup.zip').exists())
            def busy(): raise ApiError('Pause projects', 409)
            app.backup = busy; moment[0] += 3601
            self.assertFalse(scheduler.tick())
            self.assertEqual(store.rows('SELECT * FROM notifications'), [])

    def test_failures_notify_once_and_recovery_clears_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary)/'data')
            store.set_setting('backup_interval_hours', '1')
            moment = [1800000000.0]
            def failed(): raise OSError('disk full')
            app = SimpleNamespace(store=store, backup=failed)
            scheduler = BackupScheduler(app, clock=lambda: moment[0])
            self.assertFalse(scheduler.tick())
            self.assertIn('disk full', scheduler.status()['error'])
            moment[0] += 61
            self.assertFalse(scheduler.tick())
            self.assertEqual(len(store.rows('SELECT * FROM notifications')), 1)
            app.backup=lambda:b'ok'; moment[0] += 61
            self.assertTrue(scheduler.tick())
            self.assertEqual(scheduler.status()['error'], '')


if __name__ == '__main__': unittest.main()
