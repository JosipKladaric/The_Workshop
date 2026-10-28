import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cabin.history import Recorder, payload_of
from cabin.store import Store


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.recorder = Recorder(self.store, 'session-test')

    def test_complete_model_and_tool_records_do_not_replace_previous_steps(self):
        messages = [{'role':'system','content':'Identity'}, {'role':'user','content':'Question'}]
        first = self.recorder.model('programmer', 'p', 1, messages, '{"action":"read"}', 'local', 5, 1.2)
        second = self.recorder.tool('programmer', 'p', 1, 'read', {'path':'main.py'}, 'print(1)', revision='a'*40)
        third = self.recorder.model('programmer', 'p', 1, messages, 'Done', 'local', 3, .4)
        rows = self.store.rows('SELECT * FROM execution_history ORDER BY id')
        self.assertEqual([first,second,third], [r['id'] for r in rows])
        self.assertEqual(messages, payload_of(self.store, rows[0])['messages'])
        self.assertEqual('print(1)', payload_of(self.store, rows[1])['result'])
        self.assertEqual('session-test', rows[0]['session_id'])
        self.assertEqual(64, len(rows[0]['instructions_fingerprint']))
        self.assertEqual('{"action":"read"}', payload_of(self.store, rows[0])['response'])

    def test_database_rejects_history_update_or_delete(self):
        record = self.recorder.event('manager', 'p', 1, 'handoff', {'to':'qa'})
        for sql in ('UPDATE execution_history SET role=\'user\' WHERE id=?', 'DELETE FROM execution_history WHERE id=?'):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.store.execute(sql,(record,))
        self.assertEqual(1, len(self.store.rows('SELECT * FROM execution_history')))

    def test_large_payload_preserved_in_hash_verified_blob(self):
        response = 'x' * (8*1024*1024+1)
        self.recorder.model('designer', 'p', 1, [], response, 'local', 1, .1)
        row = self.store.rows('SELECT * FROM execution_history')[0]
        manifest = json.loads(row['payload'])
        self.assertTrue(manifest['complete'])
        self.assertEqual(response, payload_of(self.store,row)['response'])
        (self.store.root/manifest['blob']).write_text('tampered', encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'integrity'):
            payload_of(self.store,row)


if __name__ == '__main__': unittest.main()
