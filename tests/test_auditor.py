import json
import tempfile
import threading
import unittest
from types import SimpleNamespace

from cabin.auditor import OperationsAnalyst
from cabin.history import Recorder
from cabin.scheduler import PriorityGate
from cabin.store import Store, now


class FakeModel:
    def __init__(self, findings):
        self.findings, self.calls = findings, 0

    def generate(self, model, messages, schema=None):
        self.calls += 1
        return json.dumps({'findings':self.findings}), 12


class AuditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.store.set_setting('idle_audit_model_enabled','0')
        self.store.execute('INSERT INTO projects(id,name,brief,created) VALUES (?,?,?,?)', ('p','Project','Build',now()))
        self.engine = SimpleNamespace(current=None, lock=PriorityGate())
        self.clock = [1000.0]
        self.auditor = OperationsAnalyst(self.store,self.engine,clock=lambda:self.clock[0])
        self.recorder = Recorder(self.store)

    def test_repeated_failures_create_evidence_linked_report_once(self):
        for _ in range(3):
            self.recorder.tool('debugger','p',1,'check',{},'Checks failed: AssertionError expected 2')
        result = self.auditor.tick()
        self.assertEqual('analysed',result['status'])
        reports = self.store.rows('SELECT * FROM audit_reports')
        failure = next(r for r in reports if r['type']=='repeated-failure')
        self.assertEqual([1,2,3],json.loads(failure['evidence_ids']))
        self.assertIn('reproduce',failure['recommendation'])
        self.assertEqual(len(reports),len(self.store.rows('SELECT * FROM notifications')))
        self.clock[0] += 61
        self.recorder.tool('debugger','p',1,'check',{},'Checks failed: AssertionError expected 2')
        self.auditor.tick()
        self.assertEqual(len(reports),len(self.store.rows('SELECT * FROM audit_reports')))
        self.assertEqual('draft',self.store.project('p')['status'])

    def test_write_oscillation_and_missing_tool_are_observed_without_code_changes(self):
        for content in ['A','B','A','B']:
            self.recorder.tool('programmer','p',1,'write',{'path':'main.py','content':content},'saved')
        self.recorder.tool('designer','p',2,'screenshot',{'path':'index.html'},'Screenshot unavailable: No local Chromium')
        self.auditor.tick()
        types = {r['type'] for r in self.store.rows('SELECT * FROM audit_reports')}
        self.assertIn('oscillating-edits',types)
        self.assertIn('missing-capability',types)
        self.assertFalse((self.store.root/'projects'/'p'/'main.py').exists())
        self.assertEqual([],self.store.rows('SELECT * FROM tasks'))

    def test_work_and_foreground_reservations_defer_analysis(self):
        for _ in range(3): self.recorder.tool('programmer','p',1,'read',{'path':'a'},'same')
        self.engine.current = {'task_id':1}
        self.assertEqual('deferred',self.auditor.tick()['status'])
        self.engine.current = None
        self.engine.lock.reserve()
        self.assertEqual('deferred',self.auditor.tick()['status'])
        self.engine.lock.release_reservation()
        self.store.execute("UPDATE projects SET status='running' WHERE id='p'")
        self.assertEqual('deferred',self.auditor.tick()['status'])
        self.assertEqual('0',self.store.setting('audit_cursor','0'))
        self.assertFalse(self.store.rows('SELECT * FROM audit_reports'))

    def test_model_findings_require_existing_single_project_evidence(self):
        eid = self.recorder.tool('programmer','p',1,'read',{'path':'a'},'missing context')
        valid = dict(title='Missing context',observations='The observation lacks the requested file.',recommendation='Read the actual file before editing.',severity='warning',evidence_ids=[eid])
        fabricated = dict(valid,title='Invented',evidence_ids=[999])
        self.engine.ollama = FakeModel([valid,fabricated])
        self.store.set_setting('model','local')
        self.store.set_setting('idle_audit_model_enabled','1')
        self.auditor.tick()
        reports = self.store.rows('SELECT * FROM audit_reports')
        self.assertEqual(['Missing context'],[r['title'] for r in reports])
        self.assertEqual(1,self.engine.ollama.calls)
        self.assertEqual(1,len(self.store.rows("SELECT * FROM execution_history WHERE role='analyst'")))
        self.clock[0] += 61
        self.assertEqual('idle',self.auditor.tick()['status'])
        self.assertEqual(1,self.engine.ollama.calls)

    def test_cursor_batches_are_bounded_and_analyse_loop_across_boundary(self):
        for i in range(98): self.recorder.event('manager','p',1,'progress',str(i))
        for _ in range(3): self.recorder.tool('programmer','p',1,'read',{'path':'a'},'same')
        first = self.auditor.tick()
        self.assertEqual(100,first['entries'])
        self.assertEqual([],first['reports'])
        self.clock[0] += 61
        second = self.auditor.tick()
        self.assertEqual(1,second['entries'])
        report = self.store.rows('SELECT * FROM audit_reports')[0]
        self.assertEqual([99,100,101],json.loads(report['evidence_ids']))

    def test_missing_history_blob_is_reported_instead_of_silently_skipped(self):
        eid = self.store.append_history('p',1,'programmer','model',
            {'blob':'history_blobs/missing.json','sha256':'0'*64,'complete':True})
        result = self.auditor.tick()
        self.assertTrue(result['reports'])
        report = self.store.rows('SELECT * FROM audit_reports')[0]
        self.assertEqual('history-integrity',report['type'])
        self.assertEqual([eid],json.loads(report['evidence_ids']))


if __name__ == '__main__': unittest.main()
