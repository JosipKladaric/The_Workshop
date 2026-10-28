import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from cabin.scheduler import PriorityGate
from cabin.editing import patch_file


class PriorityTests(unittest.TestCase):
    def test_reserved_chat_runs_before_next_background_request(self):
        gate=PriorityGate()
        sequence=[]
        gate.acquire()
        gate.reserve()
        def worker():
            with gate:
                sequence.append('background')
        def assistant():
            try:
                with gate.as_interactive():
                    with gate:
                        sequence.append('chat')
                    # Nested project planning inherits foreground priority.
                    with gate:
                        sequence.append('plan')
            finally:
                gate.release_reservation()
        work=threading.Thread(target=worker)
        chat=threading.Thread(target=assistant)
        work.start(); chat.start(); gate.release()
        work.join(2); chat.join(2)
        self.assertFalse(work.is_alive())
        self.assertFalse(chat.is_alive())
        self.assertEqual(['chat','plan','background'],sequence)

    def test_checkpoint_yields_until_entire_chat_turn_releases_reservation(self):
        gate=PriorityGate(); gate.reserve()
        done=threading.Event()
        thread=threading.Thread(target=lambda:(gate.checkpoint(),done.set()))
        thread.start()
        self.assertFalse(done.wait(.05))
        gate.release_reservation()
        self.assertTrue(done.wait(1))
        thread.join(1)

    def test_idle_nonblocking_acquisition_defers_to_pending_chat(self):
        gate=PriorityGate(); gate.reserve()
        self.assertFalse(gate.acquire(blocking=False))
        with gate.interactive():
            self.assertTrue(gate.acquire(blocking=False))
            gate.release()
        gate.release_reservation()
        self.assertTrue(gate.acquire(blocking=False));gate.release()


class EditingTests(unittest.TestCase):
    def test_exact_patch_preserves_other_content_and_records_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'app.py';p.write_bytes(b'first\r\nold\r\nlast\r\n')
            digest=hashlib.sha256(p.read_bytes()).hexdigest()
            result=patch_file(root,'app.py',{'old':'old','new':'new','sha256':digest})
            self.assertEqual(b'first\r\nnew\r\nlast\r\n',p.read_bytes())
            self.assertEqual(digest,result['previous_sha256'])

    def test_ambiguous_stale_and_outside_patch_leave_files_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);p=root/'app.py';p.write_text('old old')
            for name,spec in [('app.py',{'old':'old','new':'new'}),('app.py',{'old':'old old','new':'new','sha256':'x'}),('../app.py',{'old':'x','new':'y'})]:
                with self.subTest(name=name),self.assertRaises(ValueError):patch_file(root,name,spec)
            self.assertEqual('old old',p.read_text())


if __name__=='__main__':unittest.main()
