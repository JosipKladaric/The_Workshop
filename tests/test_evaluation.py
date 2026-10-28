import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cabin.engine import Engine, files, git, safe_path
from cabin.evaluation import BENCHMARKS, Evaluation
from cabin.store import Store, now


SOLUTIONS = {
    'journal': '''import json
from pathlib import Path
def add_entry(path, title):
    if not isinstance(title, str) or not title.strip(): raise ValueError('title')
    with Path(path).open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'title':title}, ensure_ascii=False)+'\\n')
def list_entries(path):
    p=Path(path)
    return [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines()] if p.exists() else []
''',
    'pricing': '''from pricing import dollars_to_cents
def total_cents(values): return sum(dollars_to_cents(v) for v in values)
''',
    'index': '''def build_index(rows):
    result={}
    for row in rows: result.setdefault(row['key'],[]).append(row)
    return result
def lookup(index, key): return index.get(key, [])
''',
    'slug': '''import re
def slugify(text):
    text = re.sub(r'[\\s-]+', '-', text.lower().strip())
    return re.sub(r'[^a-z0-9-]', '', text).strip('-')
''',
    'intervals': '''def merge_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
''',
    'cache': '''from collections import OrderedDict
class LRUCache:
    def __init__(self, capacity):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('positive integer required')
        self.capacity = capacity
        self.values = OrderedDict()
    def get(self, key):
        if key not in self.values:
            return -1
        self.values.move_to_end(key)
        return self.values[key]
    def put(self, key, value):
        self.values[key] = value
        self.values.move_to_end(key)
        if len(self.values) > self.capacity:
            self.values.popitem(last=False)
''',
}


@unittest.skipUnless(shutil.which('git'), 'Git must be installed')
class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.engine = Engine(self.store)
        self.evaluation = Evaluation(self.store, self.engine)

    def test_known_good_solutions_pass_external_acceptance_checks(self):
        ids = self.evaluation.start(['fake-coder'])
        self.assertEqual(len(BENCHMARKS), len(ids))
        for pid in ids:
            record = self.store.rows('SELECT * FROM evaluations WHERE project_id=?', (pid,))[0]
            project = self.store.project(pid)
            folder = self.store.folder(pid)
            with self.subTest(benchmark=record['benchmark']):
                initial_pass, _ = self.engine.checks(project, folder)
                self.assertFalse(initial_pass)
                (folder / 'solution.py').write_text(SOLUTIONS[record['benchmark']], encoding='utf-8')
                if record['benchmark'] == 'pricing':
                    (folder / 'pricing.py').write_text('from decimal import Decimal, ROUND_HALF_UP\ndef dollars_to_cents(value): return int((Decimal(str(value))*100).quantize(Decimal(1),rounding=ROUND_HALF_UP))\n', encoding='utf-8')
                passed, output = self.engine.checks(project, folder)
                self.assertTrue(passed, output)
                self.assertNotIn('acceptance.py', files(folder))
                runner = Path(json.loads(project['checks'])[0][1])
                self.assertFalse(runner.is_relative_to(folder))
                with self.assertRaises(ValueError):
                    safe_path(folder, runner.as_posix())

    def test_repeat_queues_fresh_workspaces_and_records_exact_context(self):
        self.store.set_setting('context', '4096')
        ids = self.evaluation.start(['coder', 'coder'], repeats=2)
        self.assertEqual(len(BENCHMARKS) * 2, len(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(p['status'] == 'running' for p in self.store.rows('SELECT * FROM projects')))
        self.assertTrue(all(p['context'] == 4096 for p in self.store.rows('SELECT * FROM projects')))
        configs = [json.loads(e['config']) for e in self.store.rows('SELECT * FROM evaluations')]
        self.assertTrue(all(c['context'] == 4096 and c['max_attempts'] == 1 for c in configs))
        summary = self.evaluation.summary()['leaderboard'][0]
        self.assertEqual(0, summary['completed'])
        self.assertIsNone(summary['success_rate'])

    def test_summary_denominator_excludes_pending_and_requires_revision_for_pass(self):
        ids = self.evaluation.start(['coder'])
        for index, pid in enumerate(ids[:2]):
            task = self.store.rows('SELECT * FROM tasks WHERE project_id=?', (pid,))[0]
            status = 'verified' if index == 0 else 'blocked'
            self.store.execute('UPDATE tasks SET status=? WHERE id=?', (status, task['id']))
            self.store.execute('INSERT INTO runs(project_id,task_id,model,role,status,seconds,tokens,revision,detail,created) VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (pid, task['id'], 'coder', 'programmer', status, 10 + index, 100, 'a' * 40 if index == 0 else '', 'evidence', now()))
        summary = self.evaluation.summary()['leaderboard'][0]
        self.assertEqual(len(BENCHMARKS), summary['total'])
        self.assertEqual(2, summary['completed'])
        self.assertEqual(1, summary['passed'])
        self.assertEqual(50.0, summary['success_rate'])
        self.assertEqual(10.0, summary['average_success_seconds'])
        self.store.execute("UPDATE runs SET revision='' WHERE status='verified'")
        self.assertEqual(0, self.evaluation.summary()['leaderboard'][0]['passed'])

    def test_invalid_config_does_not_create_projects(self):
        for models, repeats in [([], 1), ([''], 1), (['coder'], 0), (['coder'], True), ('coder', 1)]:
            with self.subTest(models=models, repeats=repeats), self.assertRaises(ValueError):
                self.evaluation.start(models, repeats)
        self.assertEqual([], self.store.rows('SELECT * FROM projects'))

    def test_selected_cases_are_part_of_comparison_configuration(self):
        ids = self.evaluation.start(['coder'], benchmark_keys=['journal', 'pricing'])
        self.assertEqual(2, len(ids))
        config = self.evaluation.summary()['leaderboard'][0]['config']
        self.assertEqual(['journal', 'pricing'], config['benchmarks'])
        self.assertEqual('2', config['suite'])
        with self.assertRaises(ValueError):
            self.evaluation.start(['coder'], benchmark_keys=['unknown'])


if __name__ == '__main__':
    unittest.main()
