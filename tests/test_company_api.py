"""Company coordination, assistant tools and portable operations contracts."""
import base64
import http.client
import io
import json
import shutil
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from cabin.server import Application, ApiError, Server
from cabin.operations import validated_import


def encoded_zip(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return base64.b64encode(output.getvalue()).decode()


@unittest.skipUnless(shutil.which('git'), 'Git is required')
class CompanyApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = Application(Path(cls.temp.name)/'data', 'company-test-password')
        cls.server = Server(('127.0.0.1', 0), cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        token, session = cls.app.authenticate('company-test-password', 'test')
        cls.cookie, cls.csrf = 'cabin_session='+token, session['csrf']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(5)
        cls.temp.cleanup()

    def request(self, method, path, body=None, auth=True):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers.update(Cookie=self.cookie, **{'X-CSRF-Token': self.csrf})
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse(); data = response.read(); status=response.status
        headers = dict(response.getheaders()); conn.close()
        if headers.get('Content-Type', '').startswith('application/json'):
            data = json.loads(data)
        return status, data, headers

    def project(self):
        return self.app.create_project({'name': 'Company API test', 'brief': 'Acceptance: a useful test.', 'autonomy': False})['project']['id']

    def test_company_messages_preserve_task_thread_and_cannot_impersonate_agent(self):
        pid = self.project()
        tid = self.app.add_task(pid, {'title': 'Implement greeting'})['task']['id']
        status, result, _ = self.request('POST', f'/api/projects/{pid}/company',
                                         {'content': 'Please investigate.', 'sender': 'manager', 'recipient': 'qa', 'kind': 'request', 'task_id': tid})
        self.assertEqual(status, 201, result)
        self.assertEqual(result['message']['sender'], 'user')
        first = result['message']['id']
        status, result, _ = self.request('POST', f'/api/projects/{pid}/company',
                                         {'content': 'Additional context.', 'recipient': 'all', 'kind': 'findings', 'task_id': tid, 'reply_to': first})
        self.assertEqual(status, 201, result)
        self.assertEqual(result['message']['reply_to'], first)
        self.assertEqual(len(self.request('GET', f'/api/projects/{pid}/company')[1]['messages']), 2)

    def test_company_cross_project_task_and_thread_references_rejected(self):
        first, second = self.project(), self.project()
        tid = self.app.add_task(first, {'title': 'Other task'})['task']['id']
        result = self.request('POST', f'/api/projects/{second}/company', {'content': 'Cross project', 'task_id': tid})
        self.assertEqual(result[0], 400, result)

    def test_dependency_cycles_and_cross_project_edges_are_rejected(self):
        pid = self.project()
        first = self.app.add_task(pid, {'title': 'First', 'priority': 4})['task']['id']
        second = self.app.add_task(pid, {'title': 'Second', 'depends_on': [first]})['task']['id']
        with self.assertRaises(ValueError):
            self.app.edit_task(first, {'depends_on': [second]})
        other = self.app.add_task(self.project(), {'title': 'Other'})['task']['id']
        with self.assertRaises(ValueError):
            self.app.edit_task(first, {'depends_on': [other]})
        self.assertEqual(self.app.store.rows('SELECT depends_on FROM tasks WHERE id=?', (first,))[0]['depends_on'], '[]')

    def test_import_prevalidates_paths_and_creates_fresh_repository(self):
        before = len(self.app.store.rows('SELECT id FROM projects'))
        for name in ['../escape.txt', '.git/config', 'C:/escape', '.GIT /config']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.app.import_project({'name': 'Bad archive', 'archive': encoded_zip({name:'bad'})})
        self.assertEqual(before, len(self.app.store.rows('SELECT id FROM projects')))
        result = self.app.import_project({'name': 'Imported', 'archive': encoded_zip({'source/index.html':'<h1>Hello</h1>', 'source/site.css':'h1{color:blue}'})})
        folder = self.app.store.folder(result['project']['id'])
        self.assertEqual((folder/'index.html').read_text(), '<h1>Hello</h1>')
        self.assertTrue((folder/'.git').is_dir())

    def test_preview_is_authenticated_and_sandboxed(self):
        pid = self.project(); (self.app.store.folder(pid)/'index.html').write_text('<h1>Hello</h1>')
        self.assertEqual(self.request('GET', f'/preview/{pid}/index.html', auth=False)[0], 401)
        status, _, headers = self.request('GET', f'/preview/{pid}/index.html')
        self.assertEqual(status, 302)
        location = headers['Location']
        status, payload, headers = self.request('GET', location, auth=False)
        self.assertEqual(status, 200)
        self.assertIn(b'<h1>Hello</h1>', payload)
        self.assertIn('sandbox allow-scripts;', headers['Content-Security-Policy'])
        self.assertNotIn('allow-same-origin', headers['Content-Security-Policy'])
        self.assertIn("connect-src 'none'", headers['Content-Security-Policy'])
        (self.app.store.folder(pid)/'style.css').write_text('body{color:blue}')
        asset = location.rsplit('/',1)[0]+'/style.css'
        self.assertEqual(self.request('GET', asset, auth=False)[0], 200)
        capability = location.split('/')[3]
        self.app.preview_tokens[capability]['expires'] = 0
        self.assertEqual(self.request('GET', asset, auth=False)[0], 401)
        self.assertEqual(self.request('GET', f'/preview/{pid}/.git/config')[0], 400)

    def test_backup_has_consistent_database_and_git_history_and_blocks_running_projects(self):
        pid = self.project()
        status, payload, _ = self.request('GET', '/api/backup')
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIn('cabin.db', archive.namelist())
            self.assertIn(f'projects/{pid}/.git/HEAD', archive.namelist())
            self.assertNotIn('.cabin.lock', archive.namelist())
        self.app.store.execute("UPDATE projects SET status='running' WHERE id=?", (pid,))
        self.assertEqual(self.request('GET', '/api/backup')[0], 409)
        self.app.store.execute("UPDATE projects SET status='paused' WHERE id=?", (pid,))

    def test_assistant_uses_observed_tool_results_and_persists_actions(self):
        pid = self.project()
        self.app.store.set_setting('model', 'fake-model')
        class Fake:
            calls = 0
            def generate(inner, model, messages, schema=None, context=None):
                inner.calls += 1
                if inner.calls == 1:
                    return json.dumps({'action':'add_task', 'arguments':json.dumps({'title':'Written via assistant', 'description':'Acceptance: saved task.'}), 'reply':''}), 1
                self.assertIn('Written via assistant', messages[-1]['content'])
                return json.dumps({'action':'reply', 'arguments':'{}', 'reply':'Created the task.'}), 1
        old = self.app.engine.ollama
        self.app.engine.ollama = Fake()
        try:
            result = self.app.chat(pid, 'Create a task called Written via assistant.')
            jid = result['operation']['id']
            deadline = time.monotonic()+10
            while self.app.operations[jid]['status'] == 'running' and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertEqual(self.app.operations[jid]['status'], 'done', self.app.operations[jid])
            self.assertEqual(self.app.store.rows('SELECT title FROM tasks WHERE project_id=?', (pid,))[0]['title'], 'Written via assistant')
            self.assertTrue(self.app.store.load_session(pid, None, 'assistant'))
        finally:
            self.app.engine.ollama = old

    def test_project_assistant_cannot_accept_its_own_work_or_modify_other_project(self):
        pid, other = self.project(), self.project()
        tid = self.app.add_task(pid, {'title': 'Implementation'})['task']['id']
        with self.assertRaises(ApiError):
            self.app.assistant_action(pid, 'update_task', {'task_id':tid, 'status':'accepted'})
        with self.assertRaises(ApiError):
            self.app.assistant_action(pid, 'update_project', {'project_id':other, 'brief':'Wrong project'})


if __name__ == '__main__':
    unittest.main()
