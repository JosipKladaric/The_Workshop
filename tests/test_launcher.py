import tempfile
import unittest
from pathlib import Path

from run import instance_lock


class LauncherTests(unittest.TestCase):
    def test_same_data_directory_cannot_be_opened_twice_and_lock_releases(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            with instance_lock(folder):
                with self.assertRaises(SystemExit):
                    with instance_lock(folder):
                        self.fail('Second writer acquired the same data directory')
            with instance_lock(folder):
                self.assertTrue((folder / '.cabin.lock').exists())


if __name__ == '__main__':
    unittest.main()
