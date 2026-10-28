"""Public contracts for durable evidence, readonly audit reports and idle policy."""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from cabin.server import Application, Server
from cabin.store import now


class ObservabilityApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.app = Application(Path(cls.temporary.name)/'data', 'observability-password')
        cls.server = Server(('127.0.0.1', 0), cls.app)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        token, session = cls.app.authenticate('observability-password', 'tests')
        cls.cookie, cls.csrf = 'cabin_session='+token, session['csrf']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(5)
        if hasattr(cls.app.engine, 'browser'):
            cls.app.engine.browser.close()
        cls.temporary.cleanup()

    def request(self, method, path, body=None, auth=True):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers.update(Cookie=self.cookie, **{'X-CSRF-Token': self.csrf})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=20)
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        status, result = response.status, json.loads(response.read())
        connection.close()
        return status, result

    def test_history_auth_filters_cursor_and_complete_detail(self):
        store = self.app.store
        first = store.append_history(None, None, 'programmer', 'tool', {'action': 'read', 'result': 'observed'}, fingerprint='abc')
        second = store.append_history(None, None, 'qa', 'model', {'messages': [{'role': 'user', 'content': 'Inspect'}], 'response': 'Finding'}, fingerprint='xyz')
        self.assertEqual(401, self.request('GET', '/api/history', auth=False)[0])
        status, result = self.request('GET', '/api/history?role=qa&after='+str(first))
        self.assertEqual(200, status)
        self.assertEqual([second], [row['id'] for row in result['entries']])
        self.assertNotIn('payload', result['entries'][0])
        status, detail = self.request('GET', '/api/history/'+str(first))
        self.assertEqual('observed', detail['entry']['payload']['result'])
        self.assertEqual(400, self.request('GET', '/api/history?limit=1000')[0])

    def test_audit_reports_change_acknowledgement_without_changing_evidence(self):
        store = self.app.store
        hid = store.append_history(None, None, 'debugger', 'tool', {'action': 'check', 'result': 'Failure'})
        aid = store.execute('INSERT INTO audit_reports(project_id,task_id,type,severity,title,evidence_ids,observations,recommendation,fingerprint,created) VALUES (?,?,?,?,?,?,?,?,?,?)',
                             (None, None, 'loop', 'medium', 'Repeated check', json.dumps([hid]), 'Same failure repeated.', 'Investigate cause.', 'api-test-'+str(hid), now()))
        status, result = self.request('GET', '/api/audits')
        row = next(row for row in result['reports'] if row['id']==aid)
        self.assertEqual([hid], row['evidence_ids'])
        self.assertEqual(200, self.request('POST', '/api/audits/'+str(aid), {'status':'acknowledged'})[0])
        self.assertEqual(400, self.request('POST', '/api/audits/'+str(aid), {'status':'deleted'})[0])
        self.assertEqual('Same failure repeated.', store.rows('SELECT observations FROM audit_reports WHERE id=?',(aid,))[0]['observations'])

    def test_idle_settings_validate_and_roundtrip(self):
        status, result = self.request('POST', '/api/settings', {'idle_audit_enabled':False,'idle_audit_model_enabled':False,'idle_interval_seconds':120})
        self.assertEqual(200, status)
        self.assertFalse(result['settings']['idle_audit_enabled'])
        self.assertEqual(120,result['settings']['idle_interval_seconds'])
        self.assertEqual(400,self.request('POST','/api/settings',{'idle_interval_seconds':1})[0])
        status, result=self.request('GET','/api/idle')
        self.assertEqual(200,status)
        self.assertIn('approved_projects',result)

    def test_idle_approval_requires_explicit_queued_work_and_checks(self):
        project=self.app.create_project({'name':'Maintenance','brief':'Maintain only queued scope.','autonomy':False})['project']
        pid=project['id']
        path='/api/projects/'+pid+'/idle-approval'
        self.assertEqual(400,self.request('POST',path,{'approved':True})[0])
        self.app.add_task(pid,{'title':'Run checks','description':'Acceptance: checks pass.','role':'qa'})
        self.assertEqual(400,self.request('POST',path,{'approved':True})[0])
        self.app.edit_project(pid,{'checks':[[sys.executable,'-c','pass']]})
        status,result=self.request('POST',path,{'approved':True})
        self.assertEqual(200,status)
        self.assertEqual(1,result['project']['idle_approved'])
        self.assertEqual(200,self.request('POST',path,{'approved':False})[0])
        self.assertEqual(0,self.app.store.project(pid)['idle_approved'])


if __name__=='__main__':
    unittest.main()
