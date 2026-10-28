"""Full stack integration using an actual HTTP Ollama-compatible fake service."""
import http.client
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cabin.engine import git
from cabin.server import Application, Server


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.payloads.append(payload)
        schema = payload.get('format', {})
        if 'tasks' in schema.get('properties', {}):
            content = {'tasks': [{'title': 'Write greeting', 'description': 'Acceptance: greeting.txt contains hello.', 'role': 'programmer'}]}
        elif 'verdict' in schema.get('properties', {}):
            content = {'verdict': 'approve', 'findings': 'The candidate writes the required greeting and the configured check passed.'}
        elif 'findings' in schema.get('properties', {}):
            content = {'findings': []}
        else:
            index = self.server.worker_calls
            self.server.worker_calls += 1
            content = dict(action='write' if index == 0 else 'finish', path='greeting.txt' if index == 0 else '',
                           content='hello' if index == 0 else '', summary='Created greeting.')
        data = json.dumps({'message': {'role': 'assistant', 'content': json.dumps(content)}, 'eval_count': 7}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(shutil.which('git'), 'Git must be installed')
class EndToEndTests(unittest.TestCase):
    autonomy = False

    def test_brief_to_roadmap_worker_verification_ready_and_download(self):
        with tempfile.TemporaryDirectory() as root:
            model = ThreadingHTTPServer(('127.0.0.1', 0), ModelHandler)
            model.payloads, model.worker_calls = [], 0
            model_thread = threading.Thread(target=model.serve_forever, daemon=True)
            model_thread.start()
            app = Application(Path(root) / 'data', password='integration-password-123')
            app.store.set_setting('ollama_url', 'http://127.0.0.1:' + str(model.server_port))
            app.store.set_setting('model', 'fake-coder')
            server = Server(('127.0.0.1', 0), app)
            web_thread = threading.Thread(target=server.serve_forever, daemon=True)
            web_thread.start()
            cookie, csrf = '', ''

            def request(method, path, body=None):
                conn = http.client.HTTPConnection(*server.server_address, timeout=30)
                conn.request(method, path, body=json.dumps(body) if body is not None else None,
                             headers={'Content-Type': 'application/json', 'Cookie': cookie, 'X-CSRF-Token': csrf})
                response = conn.getresponse()
                payload, headers, status = response.read(), dict(response.getheaders()), response.status
                conn.close()
                if headers.get('Content-Type', '').startswith('application/json'):
                    payload = json.loads(payload)
                self.assertLess(status, 400, payload)
                return payload, headers

            def wait_for(predicate, description, timeout=45):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(0.05)
                self.fail('Timed out waiting for ' + description + ': ' + repr(app.store.snapshot()))

            try:
                session, headers = request('POST', '/api/login', {'password': 'integration-password-123'})
                cookie, csrf = headers['Set-Cookie'].split(';', 1)[0], session['csrf']
                created, _ = request('POST', '/api/projects', {
                    'name': 'Integration project', 'brief': 'Write greeting.txt containing hello.',
                    'checks': [[sys.executable, '-c', "from pathlib import Path; assert Path('greeting.txt').read_text() == 'hello'"]],
                    'max_steps': 5, 'max_attempts': 1, 'autonomy': self.autonomy})
                pid = created['project']['id']
                planned, _ = request('POST', f'/api/projects/{pid}/plan', {})
                job_id = planned['operation']['id']
                wait_for(lambda: app.operations[job_id]['status'] != 'running', 'roadmap')
                self.assertEqual('done', app.operations[job_id]['status'], app.operations[job_id])
                tasks = app.store.rows('SELECT * FROM tasks WHERE project_id=?', (pid,))
                self.assertEqual(1, len(tasks))
                app.engine.thread.start()
                request('POST', f'/api/projects/{pid}/start', {})
                wait_for(lambda: app.store.project(pid)['status'] == 'ready', 'verified project')
                task = app.store.rows('SELECT * FROM tasks WHERE project_id=?', (pid,))[0]
                self.assertEqual('done' if self.autonomy else 'verified', task['status'])
                if self.autonomy:
                    self.assertEqual('integrated', task['stage'])
                    team, _ = request('GET', f'/api/projects/{pid}/company')
                    self.assertTrue(app.store.rows("SELECT id FROM company_messages WHERE project_id=? AND sender='qa'", (pid,)))
                    self.assertTrue(app.store.rows("SELECT id FROM company_messages WHERE project_id=? AND sender='reviewer'", (pid,)))
                run = app.store.rows('SELECT * FROM runs WHERE project_id=?', (pid,))[0]
                self.assertEqual('hello', git(app.store.folder(pid), 'show', run['revision'] + ':greeting.txt'))
                payload, _ = request('GET', f'/api/projects/{pid}/download')
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    self.assertEqual(b'hello', archive.read('greeting.txt'))
                    manifest = json.loads(archive.read('WORKSHOP-EXPORT.json'))
                    self.assertEqual(run['revision'], manifest['revision'])
                workflow_calls = [p for p in model.payloads if 'findings' not in p.get('format',{}).get('properties',{}) or 'verdict' in p.get('format',{}).get('properties',{})]
                self.assertEqual(5 if self.autonomy else 3, len(workflow_calls))
                self.assertTrue(all(p['model'] == 'fake-coder' for p in model.payloads))
            finally:
                app.engine.stopping.set()
                if app.engine.thread.is_alive():
                    app.engine.thread.join(timeout=10)
                server.shutdown()
                server.server_close()
                web_thread.join(timeout=5)
                model.shutdown()
                model.server_close()
                model_thread.join(timeout=5)


class AutonomousEndToEndTests(EndToEndTests):
    autonomy = True


if __name__ == '__main__':
    unittest.main()
