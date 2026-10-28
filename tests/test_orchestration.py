"""Exercise real Git isolation and deterministic multi-role handoffs."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cabin.engine import Engine, git
from cabin.store import Store, now


def action(kind, path='', content='', summary='Implementation completed.'):
    return json.dumps(dict(action=kind, path=path, content=content, summary=summary))


class FakeModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.roles = []

    def generate(self, model, messages, schema=None, context=None):
        self.roles.append(messages[0]['content'])
        return next(self.replies), 10


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'data')
        self.store.set_setting('model', 'fake')
        self.store.execute('INSERT INTO projects(id,name,brief,status,created,autonomy,max_steps,max_attempts,checks) VALUES (?,?,?,?,?,?,?,?,?)',
                           ('demo', 'Demo', 'Write hello.txt with hello.', 'running', now(), 1, 6, 3,
                            json.dumps([[sys.executable, '-c', "from pathlib import Path; assert Path('hello.txt').read_text()=='hello'"]])))
        self.folder = self.store.folder('demo')
        self.folder.mkdir()
        (self.folder / 'README.md').write_text('Demo')
        git(self.folder, 'init')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-m', 'Initial')
        self.base = git(self.folder, 'rev-parse', 'HEAD')
        self.tid = self.store.execute('INSERT INTO tasks(project_id,title,description,role,created) VALUES (?,?,?,?,?)',
                                      ('demo', 'Greeting', 'Acceptance: hello.txt contains hello.', 'programmer', now()))
        self.engine = Engine(self.store)

    def task(self):
        return self.store.rows('SELECT * FROM tasks WHERE id=?', (self.tid,))[0]

    def work(self):
        self.engine.work(self.store.project('demo'), self.task())

    def implementation(self):
        self.engine.ollama = FakeModel([action('write', 'hello.txt', 'hello'), action('finish')])
        self.work()

    def test_candidate_stays_isolated_until_qa_review_and_exact_check_pass(self):
        self.engine.ollama = FakeModel([
            action('write', 'hello.txt', 'hello'), action('finish'),
            json.dumps(dict(verdict='approve', findings='Functional check supports acceptance.')),
            json.dumps(dict(verdict='approve', findings='Candidate matches the task.')),
        ])
        self.work()
        self.assertFalse((self.folder / 'hello.txt').exists())
        self.assertEqual('qa', self.task()['stage'])
        self.assertEqual(self.base, git(self.folder, 'rev-parse', 'HEAD'))
        self.work()
        self.assertEqual('reviewer', self.task()['stage'])
        self.work()
        self.assertEqual('integrating', self.task()['stage'])
        self.assertFalse((self.folder / 'hello.txt').exists())
        self.work()
        self.assertEqual('done', self.task()['status'])
        self.assertEqual('integrated', self.task()['stage'])
        self.assertEqual('hello', (self.folder / 'hello.txt').read_text())
        self.assertEqual(self.task()['candidate_revision'], git(self.folder, 'rev-parse', 'HEAD'))
        self.assertTrue(self.store.rows("SELECT * FROM company_messages WHERE sender='qa'"))
        self.assertTrue(self.store.rows("SELECT * FROM agent_sessions WHERE role='reviewer'"))

    def test_no_checks_still_requires_human_acceptance_after_model_reviews(self):
        self.store.execute("UPDATE projects SET checks='[]' WHERE id='demo'")
        self.engine.ollama = FakeModel([
            action('write', 'hello.txt', 'hello'), action('finish'),
            json.dumps(dict(verdict='approve', findings='Looks correct.')),
            json.dumps(dict(verdict='approve', findings='No visible defect.')),
        ])
        self.work(); self.work(); self.work()
        self.assertEqual('review', self.task()['status'])
        self.assertFalse((self.folder / 'hello.txt').exists())
        self.engine.integrate_task('demo', self.tid, manual=True)
        self.assertEqual('hello', (self.folder / 'hello.txt').read_text())

    def test_review_changes_route_to_debugger_without_modifying_main(self):
        self.engine.ollama = FakeModel([
            action('write', 'hello.txt', 'hello'), action('finish'),
            json.dumps(dict(verdict='changes', findings='Need regression tests covering the greeting.')),
        ])
        self.work(); self.work()
        self.assertEqual('debugger', self.task()['stage'])
        self.assertEqual('queued', self.task()['status'])
        self.assertIn('regression', self.task()['result'])
        self.assertFalse((self.folder / 'hello.txt').exists())

    def test_manager_triages_failed_implementation_with_bounded_debugger_handoff(self):
        self.store.execute('UPDATE projects SET max_steps=1 WHERE id=?', ('demo',))
        self.engine.ollama = FakeModel([
            action('list'),
            json.dumps(dict(decision='retry', reason='The implementation did not reach a result.', guidance='Write the greeting file, then finish.')),
        ])
        self.work()
        self.assertEqual('debugger', self.task()['stage'])
        self.assertIn('MANAGER GUIDANCE', self.task()['result'])
        self.assertTrue(self.store.rows("SELECT * FROM company_messages WHERE sender='manager' AND recipient='debugger'"))

    def test_priority_respects_dependencies_and_blocked_tasks_do_not_starve_independent_work(self):
        high = self.store.execute('INSERT INTO tasks(project_id,title,description,role,created,priority,depends_on) VALUES (?,?,?,?,?,?,?)',
                                  ('demo', 'Dependent', 'Depends on greeting.', 'documentation', now(), 10, json.dumps([self.tid])))
        other = self.store.execute('INSERT INTO tasks(project_id,title,description,role,created,priority) VALUES (?,?,?,?,?,?)',
                                   ('demo', 'Independent', 'Independent task.', 'programmer', now(), 5))
        self.assertEqual(other, self.engine.runnable_task(self.store.project('demo'))['id'])
        self.store.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.tid,))
        self.assertEqual(other, self.engine.runnable_task(self.store.project('demo'))['id'])
        self.store.execute("UPDATE tasks SET status='done',stage='integrated' WHERE id=?", (self.tid,))
        self.assertEqual(high, self.engine.runnable_task(self.store.project('demo'))['id'])

    def test_main_advance_prevents_stale_candidate_integration(self):
        self.implementation()
        (self.folder / 'manual.txt').write_text('Human change')
        git(self.folder, 'add', '-A')
        git(self.folder, 'commit', '-m', 'Human change')
        with self.assertRaisesRegex(ValueError, 'advanced'):
            self.engine.integrate_task('demo', self.tid, manual=True)
        self.assertFalse((self.folder / 'hello.txt').exists())

    def test_token_budget_pauses_before_another_model_call(self):
        self.store.execute('UPDATE projects SET max_tokens=1 WHERE id=?', ('demo',))
        self.store.execute('INSERT INTO runs(project_id,model,role,status,seconds,tokens,detail,created) VALUES (?,?,?,?,?,?,?,?)',
                           ('demo', 'fake', 'programmer', 'queued', 1, 2, 'Prior usage', now()))
        self.engine.ollama = FakeModel([])
        self.work()
        self.assertEqual('paused', self.store.project('demo')['status'])
        self.assertFalse(self.engine.ollama.roles)

    def test_group_requests_get_bounded_specialist_reply_as_tool_evidence(self):
        self.engine.ollama = FakeModel([
            action('message', 'qa', 'Please verify greeting content.', 'request'),
            'Run the configured acceptance command against hello.txt, then submit the candidate.',
            action('write', 'hello.txt', 'hello'), action('finish'),
        ])
        self.work()
        rows = self.store.rows("SELECT * FROM company_messages WHERE sender='programmer' AND recipient='qa' AND kind='request'")
        self.assertEqual(1, len(rows))
        self.assertEqual(self.tid, rows[0]['task_id'])
        self.assertEqual('qa', self.task()['stage'])
        replies = self.store.rows('SELECT * FROM company_messages WHERE reply_to=?', (rows[0]['id'],))
        self.assertEqual('qa', replies[0]['sender'])
        transcript = self.store.load_session('demo', self.tid, 'programmer')
        self.assertTrue(any('qa replied:' in m.get('content', '') for m in transcript))

    def test_direct_company_request_gets_one_readonly_reply_with_thread_link(self):
        mid = self.store.post_message('demo', 'user', 'qa', 'request', 'What evidence is still missing?', self.tid)
        self.engine.ollama = FakeModel(['No run evidence exists yet; implement and run the acceptance check.'])
        result = self.engine.company_reply('demo', mid)
        reply = self.store.rows('SELECT * FROM company_messages WHERE id=?', (result['message_id'],))[0]
        self.assertEqual(mid, reply['reply_to'])
        self.assertEqual('qa', reply['sender'])
        self.assertEqual('user', reply['recipient'])
        self.assertTrue(self.engine.company_reply('demo', mid)['already_answered'])
        self.assertFalse((self.folder / 'hello.txt').exists())

    def test_reopen_integrated_task_archives_prior_workspace_and_starts_from_new_main(self):
        self.implementation()
        self.engine.integrate_task('demo', self.tid, manual=True)
        self.store.execute("UPDATE projects SET status='paused' WHERE id='demo'")
        old_branch = self.task()['branch']
        self.engine.reopen_task('demo', self.tid)
        self.assertEqual('', self.task()['branch'])
        self.assertEqual('implementation', self.task()['stage'])
        self.assertTrue(list((self.store.root / 'worktrees' / 'demo').glob('*-archive-*')))
        self.store.execute("UPDATE projects SET status='running' WHERE id='demo'")
        self.engine.ollama = FakeModel([action('write', 'hello.txt', 'hello'), action('finish')])
        self.work()
        self.assertNotEqual(old_branch, self.task()['branch'])
        self.assertEqual(git(self.folder, 'rev-parse', 'HEAD'), self.task()['base_revision'])

    def test_relocated_backup_repairs_worktree_without_mutating_original(self):
        self.implementation()
        old_task = self.task()
        old_pointer = (self.engine.task_workspace('demo', self.tid) / '.git').read_bytes()
        copied = Path(self.temp.name) / 'restored'
        shutil.copytree(self.store.root, copied)
        restored = Store(copied)
        recovered_engine = Engine(restored)
        recovered_task = restored.rows('SELECT * FROM tasks WHERE id=?', (self.tid,))[0]
        new_folder = recovered_engine.workspaces.prepare(restored.project('demo'), recovered_task)
        self.assertEqual(old_task['candidate_revision'], git(new_folder, 'rev-parse', 'HEAD'))
        self.assertEqual(old_pointer, (self.engine.task_workspace('demo', self.tid) / '.git').read_bytes())
        self.assertIn(new_folder.resolve().as_posix(), git(restored.folder('demo'), 'worktree', 'list', '--porcelain').replace('\\', '/'))

    def test_autonomous_roadmap_omission_defaults_to_ordered_dependencies(self):
        self.store.execute("UPDATE projects SET status='paused' WHERE id='demo'")
        self.engine.ollama = FakeModel([json.dumps({'tasks': [
            dict(title='Build', description='Implement behaviour.', role='programmer'),
            dict(title='Document', description='Document the implemented behaviour.', role='documentation'),
        ]})])
        self.engine.plan('demo')
        tasks = self.store.rows('SELECT * FROM tasks WHERE id<>? ORDER BY id', (self.tid,))
        self.assertEqual([], json.loads(tasks[0]['depends_on']))
        self.assertEqual([tasks[0]['id']], json.loads(tasks[1]['depends_on']))

    def test_unavailable_visual_tool_returns_evidence_without_claiming_inspection(self):
        self.engine.ollama = FakeModel([
            action('screenshot', 'index.html'), action('write', 'hello.txt', 'hello'), action('finish'),
        ])
        with patch('cabin.visual.render_html', side_effect=ValueError('No Chromium available')):
            self.work()
        transcript = self.store.load_session('demo', self.tid, 'programmer')
        self.assertTrue(any('Screenshot unavailable' in m.get('content', '') for m in transcript))
        self.assertEqual('qa', self.task()['stage'])


if __name__ == '__main__':
    unittest.main()
