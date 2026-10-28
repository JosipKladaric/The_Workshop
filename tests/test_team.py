"""Team inventory must describe actual prompts, not an unrelated UI copy."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from cabin.engine import Engine, ROLES
from cabin.server import Application, Server
from cabin.store import Store, now
from cabin.team import Team, CONTEXTS


class RecordingModel:
    def __init__(self, reply):
        self.reply = reply
        self.messages = []

    def generate(self, model, messages, schema=None, context=None):
        self.messages = messages
        return self.reply, 1


class TeamPromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.team = Team(self.store)

    def test_catalog_uses_effective_prompt_for_every_execution_context(self):
        catalog = self.team.catalog()
        self.assertEqual(9, len(catalog['members']))
        self.assertNotIn('assistant', ROLES)
        for member in catalog['members']:
            for context in member['contexts']:
                self.assertEqual(self.team.prompt(member['id'], context['id']), context['instructions'])
                self.assertEqual(CONTEXTS[context['id']][1], context['tools'])

    def test_custom_identity_and_skill_reach_real_planning_request(self):
        self.store.execute('INSERT INTO projects(id,name,brief,created,model) VALUES (?,?,?,?,?)',
                           ('demo', 'Demo', 'Build greeting', now(), 'local-model'))
        self.team.save_role('manager', {'instructions': 'Manager uses acceptance evidence.'})
        self.team.save_skill({'id': 'local-conventions', 'name': 'Local conventions',
                              'description': 'Use local conventions in planning.',
                              'instructions': 'Every roadmap must mention the local naming convention.',
                              'roles': ['manager'], 'enabled': True})
        engine = Engine(self.store)
        model = RecordingModel(json.dumps({'tasks': [{'title': 'Implement', 'description': 'Acceptance: greeting exists.', 'role': 'programmer'}]}))
        engine.ollama = model
        engine.plan('demo')
        system = model.messages[0]['content']
        self.assertTrue(system.startswith('Manager uses acceptance evidence.'))
        self.assertIn('local naming convention', system)
        session = self.store.load_session('demo', None, 'manager')
        self.assertEqual(system, session[0]['content'])
        self.assertEqual(model.reply, session[-1]['content'])
        self.team.save_skill({'id': 'local-conventions', 'enabled': False})
        engine.plan('demo')
        self.assertNotIn('local naming convention', model.messages[0]['content'])

    def test_disabled_skill_and_identity_reset_preserve_context_constraints(self):
        original_fingerprint = self.team.fingerprint()
        self.team.save_role('qa', {'instructions': 'Check carefully.'})
        self.assertNotEqual(original_fingerprint, self.team.fingerprint())
        self.team.save_role('qa', {'reset': True})
        self.assertEqual(original_fingerprint, self.team.fingerprint())
        original = self.team.identity('qa')
        self.assertIn('correctness', original)
        self.team.save_skill({'id': 'acceptance-review', 'enabled': False})
        prompt = self.team.prompt('qa', 'review')
        self.assertIn('read-only evidence', prompt)
        self.assertNotIn('SKILL: Acceptance review', prompt)
        self.team.save_skill({'id': 'acceptance-review', 'reset': True})
        self.assertIn('SKILL: Acceptance review', self.team.prompt('qa', 'review'))

    def test_registry_rejects_invalid_roles_and_unbounded_skills(self):
        with self.assertRaises(ValueError):
            self.team.save_role('invented', {'instructions': 'Nothing'})
        with self.assertRaises(ValueError):
            self.team.save_role('qa', {'instructions': ''})
        with self.assertRaises(ValueError):
            self.team.save_skill({'name': 'Broken', 'description': 'Broken assignment', 'instructions': 'Do work', 'roles': ['invented']})


class TeamApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name)/'data', 'team-test-password')
        self.server = Server(('127.0.0.1', 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        token, session = self.app.authenticate('team-test-password', 'test')
        self.cookie, self.csrf = 'cabin_session='+token, session['csrf']
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.temp.cleanup()

    def request(self, path, body=None, auth=True):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers.update(Cookie=self.cookie, **{'X-CSRF-Token': self.csrf})
        conn.request('GET' if body is None else 'POST', path, json.dumps(body) if body is not None else None, headers)
        response = conn.getresponse()
        status, payload = response.status, json.loads(response.read())
        conn.close()
        return status, payload

    def test_inventory_and_instruction_edit_are_authenticated_and_persistent(self):
        self.assertEqual(401, self.request('/api/team', auth=False)[0])
        status, catalog = self.request('/api/team')
        self.assertEqual(200, status)
        self.assertEqual(9, len(catalog['members']))
        status, _ = self.request('/api/team/roles/programmer', {'instructions': 'Use project naming conventions.'})
        self.assertEqual(200, status)
        self.assertEqual('Use project naming conventions.', self.app.engine.team.identity('programmer'))
        status, _ = self.request('/api/skills', {'id': 'local-style', 'name': 'Local style', 'description': 'Use house style.', 'instructions': 'Read conventions first.', 'roles': ['programmer'], 'enabled': True})
        self.assertEqual(200, status)
        self.assertIn('Read conventions first.', self.app.engine.team.prompt('programmer', 'autonomous'))

    def test_edits_are_rejected_while_worker_or_async_operation_is_active(self):
        self.app.engine.current = {'project_id': 'x', 'role': 'programmer'}
        self.assertEqual(409, self.request('/api/team/roles/programmer', {'instructions': 'New prompt'})[0])
        self.app.engine.current = None
        self.app.operations['busy'] = {'status': 'running'}
        self.assertEqual(409, self.request('/api/skills', {'id': 'acceptance-review', 'enabled': False})[0])
        self.assertTrue(next(s for s in self.app.engine.team.skills() if s['id']=='acceptance-review')['enabled'])


if __name__ == '__main__':
    unittest.main()
