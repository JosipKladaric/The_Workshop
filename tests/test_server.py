"""HTTP integration coverage with real sessions, SQLite and temporary Git repos."""
import http.client
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from cabin.engine import git
from cabin.server import Application, Server


@unittest.skipUnless(shutil.which('git'), 'Git must be installed')
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = Application(Path(cls.temp.name) / 'data', password='test-password-123')
        cls.server = Server(('127.0.0.1', 0), cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.cookie = ''
        cls.csrf = ''
        status, data, headers = cls.request('POST', '/api/login', {'password': 'test-password-123'}, authenticated=False)
        assert status == 200, data
        cls.cookie = headers['Set-Cookie'].split(';', 1)[0]
        cls.csrf = data['csrf']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    @classmethod
    def request(cls, method, path, body=None, authenticated=True, headers=None):
        request_headers = {'Content-Type': 'application/json'}
        if authenticated:
            request_headers.update(Cookie=cls.cookie, **{'X-CSRF-Token': cls.csrf})
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection(*cls.server.server_address, timeout=15)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=request_headers)
        response = conn.getresponse()
        result = response.read()
        response_headers = dict(response.getheaders())
        status = response.status
        conn.close()
        if response_headers.get('Content-Type', '').startswith('application/json'):
            result = json.loads(result)
        return status, result, response_headers

    def project(self):
        status, result, _ = self.request('POST', '/api/projects', {'name': 'HTTP test', 'brief': 'Create a working greeting.'})
        self.assertEqual(201, status, result)
        return result['project']['id']

    def test_unauthenticated_read_and_write_are_denied(self):
        self.assertEqual(401, self.request('GET', '/api/state', authenticated=False)[0])
        self.assertEqual(401, self.request('POST', '/api/projects', {'name': 'Forbidden'}, authenticated=False)[0])

    def test_public_session_has_no_token_for_anonymous_browser(self):
        status, result, _ = self.request('GET', '/api/session', authenticated=False)
        self.assertEqual(200, status)
        self.assertFalse(result['authenticated'])
        self.assertIsNone(result['csrf'])

    def test_cookie_is_http_only_and_strict_same_site(self):
        status, _, headers = self.request('POST', '/api/login', {'password': 'test-password-123'}, authenticated=False)
        self.assertEqual(200, status)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])

    def test_mutations_require_csrf_and_same_origin(self):
        self.assertEqual(403, self.request('POST', '/api/projects', {'name': 'Denied'}, headers={'X-CSRF-Token': ''})[0])
        self.assertEqual(403, self.request('POST', '/api/projects', {'name': 'Denied'}, headers={'Origin': 'http://attacker.invalid'})[0])
        self.assertEqual(403, self.request('POST', '/api/projects', {'name': 'Denied'}, headers={'Sec-Fetch-Site': 'cross-site'})[0])

    def test_settings_never_expose_password_hash(self):
        status, result, _ = self.request('GET', '/api/state')
        self.assertEqual(200, status)
        self.assertNotIn('password_hash', json.dumps(result))

    def test_model_address_rejects_public_internet_and_credentials(self):
        for url in ['http://8.8.8.8:11434', 'http://user:pass@127.0.0.1:11434', 'file:///etc/passwd']:
            with self.subTest(url=url):
                self.assertEqual(400, self.request('POST', '/api/settings', {'ollama_url': url})[0])

    def test_project_task_lifecycle_and_unfinished_acceptance_rejected(self):
        pid = self.project()
        status, result, _ = self.request('POST', f'/api/projects/{pid}/tasks', {'title': 'Implement', 'description': 'Acceptance: useful output.', 'role': 'programmer'})
        self.assertEqual(201, status, result)
        tid = result['task']['id']
        self.assertEqual(400, self.request('POST', f'/api/tasks/{tid}', {'status': 'done'})[0])
        self.assertEqual(200, self.request('POST', f'/api/tasks/{tid}', {'description': 'Revised acceptance criteria.'})[0])
        self.app.store.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        status, result, _ = self.request('POST', f'/api/tasks/{tid}', {'status': 'accepted'})
        self.assertEqual(200, status, result)
        self.assertEqual('done', result['task']['status'])

    def test_file_endpoints_reject_traversal_and_git_metadata(self):
        pid = self.project()
        for path in ['..%2F..%2Fcabin.db', '.git%2Fconfig', '%2Fetc%2Fpasswd']:
            with self.subTest(path=path):
                self.assertEqual(400, self.request('GET', f'/api/projects/{pid}/file?path={path}')[0])

    def test_download_excludes_secrets_and_git_and_has_manifest(self):
        pid = self.project()
        folder = self.app.store.folder(pid)
        (folder / '.env').write_text('SECRET=hidden')
        (folder / 'private.key').write_text('secret')
        (folder / '.env.example').write_text('TOKEN=')
        status, payload, _ = self.request('GET', f'/api/projects/{pid}/download')
        self.assertEqual(200, status)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            self.assertIn('README.md', names)
            self.assertIn('.env.example', names)
            self.assertIn('WORKSHOP-EXPORT.json', names)
            self.assertNotIn('.env', names)
            self.assertNotIn('private.key', names)
            self.assertFalse(any(n.startswith('.git/') for n in names))

    def test_restore_preserves_history_and_invalidates_previous_verification(self):
        pid = self.project()
        folder = self.app.store.folder(pid)
        original = git(folder, 'rev-parse', 'HEAD')
        original_text = (folder / 'README.md').read_text()
        (folder / 'README.md').write_text('Changed')
        self.request('POST', f'/api/projects/{pid}/checkpoint', {'message': 'Changed documentation'})
        changed = git(folder, 'rev-parse', 'HEAD')
        self.app.store.execute("INSERT INTO tasks(project_id,title,description,role,status,created) VALUES (?,?,?,'qa','verified','now')", (pid, 'Tested', 'Was tested'))
        status, result, _ = self.request('POST', f'/api/projects/{pid}/restore', {'revision': original})
        self.assertEqual(200, status, result)
        self.assertEqual(original_text, (folder / 'README.md').read_text())
        self.assertEqual(changed, git(folder, 'rev-parse', changed + '^{commit}'))
        self.assertNotEqual(original, result['revision'])
        self.assertEqual('review', self.app.store.rows('SELECT status FROM tasks WHERE project_id=?', (pid,))[0]['status'])

    def test_unknown_routes_return_404_without_source_leak(self):
        self.assertEqual(404, self.request('GET', '/api/does-not-exist')[0])
        self.assertEqual(404, self.request('GET', '/%2e%2e/server.py')[0])

    def test_evaluation_projects_are_accessible_through_normal_project_routes(self):
        status, result, _ = self.request('POST', '/api/evaluations', {'models': ['fake-test-model'], 'repeats': 1})
        self.assertEqual(202, status, result)
        from cabin.evaluation import BENCHMARKS
        self.assertEqual(len(BENCHMARKS), len(result['projects']))
        for pid in result['projects']:
            with self.subTest(project=pid):
                self.assertEqual(200, self.request('GET', f'/api/projects/{pid}')[0])
                self.assertEqual(200, self.request('POST', f'/api/projects/{pid}/pause', {})[0])
                self.assertEqual(200, self.request('GET', f'/api/projects/{pid}/files')[0])
        status, summary, _ = self.request('GET', '/api/evaluations')
        self.assertEqual(200, status)
        self.assertTrue(summary['leaderboard'])
        self.assertIsNone(summary['leaderboard'][0]['success_rate'])

    def test_invalid_fractional_limits_do_not_create_project(self):
        status, result, _ = self.request('POST', '/api/projects', {'name': 'Invalid', 'max_steps': 1.5})
        self.assertEqual(400, status, result)

    def test_diff_includes_untracked_files(self):
        pid = self.project()
        (self.app.store.folder(pid) / 'new-feature.txt').write_text('new feature')
        status, result, _ = self.request('GET', f'/api/projects/{pid}/diff')
        self.assertEqual(200, status)
        self.assertIn('new-feature.txt', result['diff'])

    def test_manual_verification_saves_candidate_and_records_exact_revision(self):
        pid = self.project()
        folder = self.app.store.folder(pid)
        (folder / 'greeting.txt').write_text('hello')
        self.app.store.execute('UPDATE projects SET checks=? WHERE id=?',
                               (json.dumps([[sys.executable, '-c', "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello'"]]), pid))
        result = self.app.check_project(pid)
        self.assertEqual('verified', result['status'], result)
        self.assertEqual('hello', git(folder, 'show', result['revision'] + ':greeting.txt'))
        run = self.app.store.rows('SELECT * FROM runs WHERE project_id=?', (pid,))[0]
        self.assertEqual(result['revision'], run['revision'])
        self.assertIsNone(run['task_id'])

    def test_checks_that_modify_files_do_not_verify_modified_workspace(self):
        pid = self.project()
        self.app.store.execute('UPDATE projects SET checks=? WHERE id=?',
                               (json.dumps([[sys.executable, '-c', "from pathlib import Path; Path('generated.txt').write_text('changed')"]]), pid))
        result = self.app.check_project(pid)
        self.assertEqual('review', result['status'], result)
        self.assertIn('changed the workspace', result['detail'])


if __name__ == '__main__':
    unittest.main()
