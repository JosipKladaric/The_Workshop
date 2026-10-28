"""Exercise the worker with a deterministic fake model; no Ollama required."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from cabin.engine import Engine, files, git, safe_path, run_check
from cabin.store import Store, now


def action(kind, path='', content='', summary='Completed the assigned task.'):
    return json.dumps(dict(action=kind, path=path, content=content, summary=summary))


class FakeModel:
    def __init__(self, replies, on_call=None):
        self.replies = iter(replies)
        self.calls = 0
        self.on_call = on_call

    def generate(self, model, messages, schema=None, context=None):
        self.calls += 1
        if self.on_call:
            self.on_call(self.calls)
        return next(self.replies), 5


@unittest.skipUnless(shutil.which('git'), 'Git must be installed')
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'data')
        self.store.set_setting('model', 'fake-coder')
        self.store.execute('INSERT INTO projects(id,name,brief,status,created,max_steps,max_attempts) VALUES (?,?,?,?,?,?,?)',
                           ('demo', 'Demo', 'Create a text file.', 'running', now(), 5, 2))
        self.folder = self.store.folder('demo')
        self.folder.mkdir()
        git(self.folder, 'init')
        (self.folder / 'README.md').write_text('Demo\n', encoding='utf-8')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-m', 'Initial project')
        self.task_id = self.store.execute('INSERT INTO tasks(project_id,title,description,role,created) VALUES (?,?,?,?,?)',
                                          ('demo', 'Create greeting', 'Write hello.txt containing hello.', 'programmer', now()))
        self.engine = Engine(self.store)

    def task(self):
        return self.store.rows('SELECT * FROM tasks WHERE id=?', (self.task_id,))[0]

    def work(self):
        self.engine.work(self.store.project('demo'), self.task())

    def test_completed_work_without_checks_requires_review(self):
        self.engine.ollama = FakeModel([action('write', 'hello.txt', 'hello'), action('finish')])
        self.work()
        self.assertEqual('hello', (self.folder / 'hello.txt').read_text())
        self.assertEqual('review', self.task()['status'])
        run = self.store.rows('SELECT * FROM runs')[0]
        self.assertEqual(git(self.folder, 'rev-parse', 'HEAD'), run['revision'])
        self.assertEqual('hello', git(self.folder, 'show', run['revision'] + ':hello.txt'))
        self.assertEqual(10, run['tokens'])

    def test_passed_check_is_bound_to_recorded_revision(self):
        checks = [[sys.executable, '-c', "from pathlib import Path; assert Path('hello.txt').read_text() == 'hello'"]]
        self.store.execute('UPDATE projects SET checks=? WHERE id=?', (json.dumps(checks), 'demo'))
        self.engine.ollama = FakeModel([action('write', 'hello.txt', 'hello'), action('finish')])
        self.work()
        self.assertEqual('verified', self.task()['status'])
        run = self.store.rows('SELECT * FROM runs')[0]
        self.assertTrue(run['revision'])
        self.assertEqual('hello', git(self.folder, 'show', run['revision'] + ':hello.txt'))

    def test_failed_checks_never_mark_verified_and_attempts_are_bounded(self):
        checks = [[sys.executable, '-c', 'raise SystemExit(1)']]
        self.store.execute('UPDATE projects SET checks=?,max_steps=1 WHERE id=?', (json.dumps(checks), 'demo'))
        initial_revision = git(self.folder, 'rev-parse', 'HEAD')
        self.engine.ollama = FakeModel([action('finish'), action('finish')])
        self.work()
        self.assertEqual('queued', self.task()['status'])
        self.work()
        self.assertEqual('blocked', self.task()['status'])
        self.assertEqual(2, self.task()['attempts'])
        self.assertEqual('blocked', self.store.project('demo')['status'])
        # Failed candidates remain recoverable in history, but never carry a
        # successful verification record.
        self.assertNotEqual(initial_revision, git(self.folder, 'rev-parse', 'HEAD'))
        self.assertEqual(initial_revision, git(self.folder, 'rev-parse', initial_revision + '^{commit}'))
        self.assertTrue(all(not run['revision'] for run in self.store.rows('SELECT * FROM runs')))

    def test_pause_discards_a_model_response_before_file_write(self):
        def pause(_):
            self.store.execute("UPDATE projects SET status='paused' WHERE id='demo'")
        self.engine.ollama = FakeModel([action('write', 'hello.txt', 'unexpected')], pause)
        self.work()
        self.assertFalse((self.folder / 'hello.txt').exists())
        self.assertEqual('queued', self.task()['status'])
        self.assertEqual('paused', self.store.project('demo')['status'])

    def test_exhausted_step_budget_stops_model_calls(self):
        self.store.execute('UPDATE projects SET max_steps=2,max_attempts=1 WHERE id=?', ('demo',))
        self.engine.ollama = FakeModel([action('list')] * 10)
        self.work()
        self.assertEqual(2, self.engine.ollama.calls)
        self.assertEqual('blocked', self.task()['status'])

    def test_invalid_json_is_recorded_and_retryable(self):
        self.engine.ollama = FakeModel(['this is not JSON'])
        self.work()
        self.assertEqual('queued', self.task()['status'])
        self.assertTrue(self.task()['result'])
        self.assertEqual(1, len(self.store.rows('SELECT * FROM runs')))

    def test_path_escape_from_model_never_writes_outside_project(self):
        self.engine.ollama = FakeModel([action('write', '../outside.txt', 'bad')])
        self.work()
        self.assertFalse((self.folder.parent / 'outside.txt').exists())
        self.assertNotEqual('verified', self.task()['status'])

    def test_plan_persists_ordered_roles_and_acceptance_text(self):
        tasks = [dict(title='Implement', description='Acceptance: greeting is hello.', role='programmer'),
                 dict(title='Document', description='Acceptance: README explains usage.', role='documentation')]
        self.engine.ollama = FakeModel([json.dumps({'tasks': tasks})])
        self.store.execute("UPDATE projects SET status='paused' WHERE id='demo'")
        self.engine.plan('demo')
        saved = self.store.rows('SELECT * FROM tasks WHERE id<>? ORDER BY id', (self.task_id,))
        self.assertEqual([t['title'] for t in tasks], [t['title'] for t in saved])
        self.assertEqual([t['role'] for t in tasks], [t['role'] for t in saved])

    def test_qa_can_raise_reproducible_followup_issue(self):
        self.engine.ollama = FakeModel([
            action('issue', content='Reproduce: open missing file. Acceptance: clear error.', summary='Missing file crashes'),
            action('finish')])
        self.work()
        issues = self.store.rows('SELECT * FROM tasks WHERE id<>?', (self.task_id,))
        self.assertEqual(1, len(issues))
        self.assertEqual('debugger', issues[0]['role'])
        self.assertEqual('blocked', issues[0]['status'])
        self.assertIn('Reproduce:', issues[0]['description'])

    def test_delete_is_saved_in_checkpoint(self):
        self.engine.ollama = FakeModel([action('delete', 'README.md'), action('finish')])
        self.work()
        self.assertFalse((self.folder / 'README.md').exists())
        self.assertNotIn('README.md', git(self.folder, 'ls-tree', '--name-only', 'HEAD'))

    def test_plan_rejects_invalid_batches_without_partial_insert(self):
        for tasks in [[], [dict(title='One', description='Valid', role='programmer'),
                           dict(title='', description='Invalid', role='qa')]]:
            with self.subTest(tasks=tasks):
                self.engine.ollama = FakeModel([json.dumps({'tasks': tasks})])
                with self.assertRaises(ValueError):
                    self.engine.plan('demo')
                self.assertEqual(1, len(self.store.rows('SELECT * FROM tasks')))

    def test_checks_that_modify_candidate_require_review_of_original_revision(self):
        checks = [[sys.executable, '-c', "from pathlib import Path; Path('hello.txt').write_text('modified by checks')"]]
        self.store.execute('UPDATE projects SET checks=? WHERE id=?', (json.dumps(checks), 'demo'))
        self.engine.ollama = FakeModel([action('write', 'hello.txt', 'original candidate'), action('finish')])
        self.work()
        self.assertEqual('review', self.task()['status'])
        run = self.store.rows('SELECT * FROM runs')[0]
        self.assertEqual('original candidate', git(self.folder, 'show', run['revision'] + ':hello.txt'))
        self.assertEqual('modified by checks', (self.folder / 'hello.txt').read_text())
        self.assertIn('Checks changed the workspace', run['detail'])


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'project'
        self.folder.mkdir()

    def test_file_paths_reject_traversal_absolute_escape_and_git_metadata(self):
        for path in ['../escape.txt', str(self.folder.parent / 'outside.txt'), '.git/config',
                     'nested/.git/config', '.GIT/config', '.', 'file.txt:stream', 'src\\main.py']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_path(self.folder, path)
        self.assertEqual(self.folder / 'src' / 'main.py', safe_path(self.folder, 'src/main.py'))

    def test_file_inventory_omits_dependencies_and_git(self):
        for name in ['.git', 'node_modules', '.venv', '__pycache__']:
            directory = self.folder / name
            directory.mkdir()
            (directory / 'secret').write_text('excluded')
        (self.folder / 'main.py').write_text('print(1)')
        self.assertEqual(['main.py'], files(self.folder))

    def test_symlink_escape_is_rejected_when_supported(self):
        outside = self.folder.parent / 'outside'
        outside.mkdir()
        try:
            (self.folder / 'linked').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Creating symbolic links is not permitted on this system')
        with self.assertRaises(ValueError):
            safe_path(self.folder, 'linked/escape.txt')
        self.assertEqual([], files(self.folder))


class CheckRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_failed_command_reports_stderr_and_exit_status(self):
        passed, output = run_check([sys.executable, '-c', "import sys; print('failure evidence',file=sys.stderr); sys.exit(2)"], self.folder)
        self.assertFalse(passed)
        self.assertIn('failure evidence', output)

    def test_timeout_stops_check(self):
        passed, output = run_check([sys.executable, '-c', 'import time; time.sleep(30)'], self.folder, timeout=0.15)
        self.assertFalse(passed)
        self.assertIn('timed out', output)

    def test_excessive_output_is_bounded(self):
        passed, output = run_check([sys.executable, '-c', "import os; x=b'x'*65536\nwhile True: os.write(1,x)"], self.folder, timeout=5)
        self.assertFalse(passed)
        self.assertIn('output limit', output)
        self.assertLess(len(output), 17000)

    def test_command_arguments_are_passed_without_shell_expansion(self):
        passed, output = run_check([sys.executable, '-c', 'import sys; print(sys.argv[1])', 'literal && $(whoami)'], self.folder)
        self.assertTrue(passed)
        self.assertEqual('literal && $(whoami)', output.strip())

    def test_invalid_command_shapes_are_rejected(self):
        for command in ['', [], [''], [12], ['python', '\x00']]:
            with self.subTest(command=command), self.assertRaises(ValueError):
                run_check(command, self.folder)


class StoreRecoveryTests(unittest.TestCase):
    def test_restart_recovers_inflight_work_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            store.execute('INSERT INTO projects(id,name,brief,status,created) VALUES (?,?,?,?,?)',
                          ('demo', 'Demo', 'brief', 'running', now()))
            store.execute('INSERT INTO tasks(project_id,title,description,status,created) VALUES (?,?,?,?,?)',
                          ('demo', 'Task', 'Description', 'running', now()))
            recovered = Store(root)
            self.assertEqual('paused', recovered.project('demo')['status'])
            self.assertEqual('queued', recovered.rows('SELECT * FROM tasks')[0]['status'])


if __name__ == '__main__':
    unittest.main()
