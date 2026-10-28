import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cabin.company import validate_dependencies
from cabin.store import Store, now
from cabin.metrics import ResourceMonitor, sample_resources


class CompanyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        for pid in ('a', 'b'):
            self.store.execute('INSERT INTO projects(id,name,brief,created) VALUES (?,?,?,?)', (pid, pid, 'Build', now()))

    def task(self, pid='a', deps='[]'):
        return self.store.execute('INSERT INTO tasks(project_id,title,description,created,depends_on) VALUES (?,?,?,?,?)',
                                  (pid, 'Task', 'Acceptance', now(), deps))

    def test_group_chat_is_persistent_targeted_and_references_validated(self):
        tid = self.task()
        first = self.store.post_message('a', 'programmer', 'qa', 'handoff', 'Candidate ready', tid, 'a' * 40)
        second = self.store.post_message('a', 'qa', 'debugger', 'findings', 'Repair edge case', tid, reply_to=first)
        self.assertIn('Candidate ready', self.store.company_context('a', tid, 'qa'))
        self.assertIn('Repair edge case', self.store.company_context('a', tid, 'debugger'))
        with self.assertRaises(ValueError):
            self.store.post_message('b', 'user', 'all', 'request', 'Wrong project', tid)
        with self.assertRaises(ValueError):
            self.store.post_message('b', 'user', 'all', 'request', 'Wrong reply', reply_to=second)
        self.assertEqual(2, len(Store(self.tmp.name).rows('SELECT * FROM company_messages')))

    def test_blocker_creates_notification_once(self):
        self.store.post_message('a', 'manager', 'user', 'blocker', 'Missing compiler')
        notices = self.store.snapshot()['notifications']
        self.assertEqual(1, len(notices))
        self.assertEqual(0, notices[0]['seen'])

    def test_dependencies_reject_cycles_and_cross_project_edges(self):
        first, second, other = self.task(), self.task(), self.task('b')
        deps = validate_dependencies(self.store, 'a', second, [first])
        self.store.execute('UPDATE tasks SET depends_on=? WHERE id=?', (deps, second))
        for candidate in ([second], [other], [first], [second, second]):
            with self.subTest(deps=candidate), self.assertRaises(ValueError):
                validate_dependencies(self.store, 'a', first, candidate)

    def test_role_sessions_replace_and_resume_independently(self):
        tid = self.task()
        messages = [{'role': 'user', 'content': 'task'}]
        self.store.save_session('a', tid, 'programmer', messages)
        self.store.save_session('a', tid, 'qa', [{'role': 'user', 'content': 'review'}])
        messages.append({'role': 'assistant', 'content': 'change'})
        self.store.save_session('a', tid, 'programmer', messages)
        self.assertEqual(messages, self.store.load_session('a', tid, 'programmer'))
        self.assertEqual('review', self.store.load_session('a', tid, 'qa')[0]['content'])
        self.assertEqual(2, len(self.store.snapshot()['sessions']))

    def test_restart_respects_opt_in_recovery(self):
        self.store.execute("UPDATE projects SET status='running',auto_resume=CASE WHEN id='a' THEN 1 ELSE 0 END")
        tid = self.task()
        self.store.execute("UPDATE tasks SET status='running',stage='qa',branch='workshop/task-1' WHERE id=?", (tid,))
        recovered = Store(self.tmp.name)
        self.assertEqual('running', recovered.project('a')['status'])
        self.assertEqual('paused', recovered.project('b')['status'])
        task = recovered.rows('SELECT * FROM tasks WHERE id=?', (tid,))[0]
        self.assertEqual(('queued', 'qa', 'workshop/task-1'), (task['status'], task['stage'], task['branch']))

    def test_resource_measurements_are_optional_and_labelled(self):
        sample = sample_resources()
        self.assertIn('ram_total_mb', sample)
        self.assertIn('gpu', sample)
        with ResourceMonitor() as monitor:
            self.assertGreaterEqual(monitor.snapshot()['samples'], 1)
        self.assertIn('host/device', monitor.snapshot()['scope'])


if __name__ == '__main__':
    unittest.main()
