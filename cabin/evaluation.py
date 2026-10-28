"""Repeatable offline coding evaluations, isolated from user projects."""
import json
import sys
import uuid
from .engine import git
from .store import now

SUITE_VERSION = '2'
BENCHMARKS = [
    dict(key='slug', title='Repair a slug formatter', category='debugging',
         brief='Repair slugify(text) in solution.py. Lowercase text, trim surrounding whitespace, turn each consecutive run of whitespace or hyphens into one hyphen, remove all characters except ASCII letters, digits and hyphens, and remove leading/trailing hyphens. Preserve digits. Write tests if useful. Keep the public function name.',
         initial='def slugify(text):\n    return text.lower().replace(" ", "-")\n',
         tests='''import unittest
from solution import slugify
class Acceptance(unittest.TestCase):
 def test_examples(self):
  for raw, expected in [(" Hello   World! ","hello-world"),("A---B","a-b"),("  ",""),("v2 release","v2-release"),("a\\tb\\nc","a-b-c"),("--hi--","hi"),("Café!","caf")]:
   with self.subTest(raw=raw): self.assertEqual(slugify(raw),expected)
'''),
    dict(key='intervals', title='Implement interval merging', category='implementation',
         brief='Implement merge_intervals(intervals) in solution.py. Input is a list of [start,end] integer pairs where start <= end. Return sorted merged intervals. Merge touching intervals. Handle empty input. Do not mutate the input or its nested lists. Preserve the public function name.',
         initial='def merge_intervals(intervals):\n    raise NotImplementedError\n',
         tests='''import unittest, copy
from solution import merge_intervals
class Acceptance(unittest.TestCase):
 def test_cases(self):
  for raw, expected in [([],[]),([[3,4],[1,2],[2,3]],[[1,4]]),([[1,10],[2,3],[12,12]],[[1,10],[12,12]]),([[-3,-1],[-1,2]], [[-3,2]]),([[1,1],[1,1]],[[1,1]])]:
   original=copy.deepcopy(raw)
   with self.subTest(raw=raw):
    self.assertEqual(merge_intervals(raw),expected)
    self.assertEqual(raw,original)
'''),
    dict(key='cache', title='Implement an LRU cache', category='state-management',
         brief='Implement LRUCache(capacity) in solution.py using the Python standard library. capacity must be a positive integer (reject booleans and nonintegers with ValueError). get(key) returns the value or -1 when missing, and marks hits as recently used. put(key,value) inserts or updates, marks the key recently used and evicts the least recently used key when over capacity. None is a valid value. Keep get and put O(1) average time.',
         initial='class LRUCache:\n    def __init__(self, capacity):\n        pass\n\n    def get(self, key):\n        return -1\n\n    def put(self, key, value):\n        pass\n',
         tests='''import unittest
from solution import LRUCache
class Acceptance(unittest.TestCase):
 def test_eviction(self):
  c=LRUCache(2); c.put('a',1); c.put('b',2)
  self.assertEqual(c.get('a'),1)
  c.put('c',3); self.assertEqual(c.get('b'),-1)
  c.put('a',4); c.put('d',5)
  self.assertEqual(c.get('c'),-1); self.assertEqual(c.get('a'),4)
 def test_none_and_one(self):
  c=LRUCache(1); c.put('a',None); self.assertIsNone(c.get('a'))
  c.put('b',2); self.assertEqual(c.get('a'),-1); self.assertEqual(c.get('b'),2)
 def test_invalid_capacity(self):
  for n in [0,-1,True,1.5,'2']:
   with self.subTest(n=n), self.assertRaises(ValueError): LRUCache(n)
'''),
    dict(key='journal', title='Build a persistent journal', category='file-io',
         brief='Implement add_entry(path, title) and list_entries(path) in solution.py using standard-library JSON Lines. add_entry appends one JSON object {"title": title} per line encoded as UTF-8, preserving earlier entries. Reject non-string or whitespace-only titles with ValueError and leave the file unchanged. Preserve the exact valid title. list_entries returns all objects in file order, or [] for a missing file. Do not depend on the current working directory.',
         initial='def add_entry(path, title):\n    raise NotImplementedError\n\ndef list_entries(path):\n    return []\n',
         tests='''import unittest, tempfile
from pathlib import Path
from solution import add_entry, list_entries
class Acceptance(unittest.TestCase):
 def test_persistence(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'entries.jsonl'
   self.assertEqual(list_entries(p),[])
   add_entry(p,'First'); add_entry(p,'Šuma 🌲')
   self.assertEqual(list_entries(p),[{'title':'First'},{'title':'Šuma 🌲'}])
   before=p.read_bytes()
   for bad in ['', '   ', None, 123]:
    with self.assertRaises(ValueError): add_entry(p,bad)
    self.assertEqual(p.read_bytes(),before)
'''),
    dict(key='pricing', title='Repair a multi-file pricing module', category='multi-file',
         brief='Fix money conversion across solution.py and pricing.py. pricing.dollars_to_cents(value) must accept strings, ints and Decimal values, convert dollars to integer cents using Decimal ROUND_HALF_UP, including negative values. solution.total_cents(values) must use that helper and sum the rounded per-item cents. Avoid binary float rounding errors. Do not change public function names.',
         initial='from pricing import dollars_to_cents\n\ndef total_cents(values):\n    return dollars_to_cents(sum(float(v) for v in values))\n',
         files={'pricing.py': 'def dollars_to_cents(value):\n    return int(float(value)*100)\n'},
         tests='''import unittest
from decimal import Decimal
from pricing import dollars_to_cents
from solution import total_cents
class Acceptance(unittest.TestCase):
 def test_conversion(self):
  for value, expected in [('1.005',101),('-1.005',-101),(Decimal('0.29'),29),(2,200)]:
   self.assertEqual(dollars_to_cents(value),expected)
 def test_total_rounds_items(self):
  self.assertEqual(total_cents(['0.005','0.005']),2)
  self.assertEqual(total_cents([]),0)
  self.assertEqual(total_cents(['1.005','-1.005',Decimal('2.10')]),210)
'''),
    dict(key='index', title='Replace repeated scans with an index', category='optimisation',
         brief='Implement build_index(rows) and lookup(index, key) in solution.py. Each row is a dict with a "key" field. build_index returns a dict mapping each key to its matching original row objects in input order. Preserve duplicate keys and do not mutate rows. lookup returns the list for the key, or [] if absent. lookup must use direct dictionary access rather than scanning the index. Empty input is valid.',
         initial='def build_index(rows):\n    return {}\n\ndef lookup(index, key):\n    return []\n',
         tests='''import unittest, copy
from solution import build_index, lookup
class NoScan(dict):
 def __iter__(self): raise AssertionError('lookup scanned dictionary')
 def items(self): raise AssertionError('lookup scanned dictionary')
 def values(self): raise AssertionError('lookup scanned dictionary')
class Acceptance(unittest.TestCase):
 def test_duplicates_and_identity(self):
  rows=[{'key':'a','v':1},{'key':'b','v':2},{'key':'a','v':3}]; before=copy.deepcopy(rows)
  index=build_index(rows)
  self.assertIsInstance(index,dict); self.assertEqual(lookup(index,'a'),[rows[0],rows[2]])
  self.assertIs(lookup(index,'a')[0],rows[0]); self.assertEqual(rows,before)
  self.assertEqual(lookup(NoScan(index),'b'),[rows[1]])
  self.assertEqual(lookup(index,'missing'),[]); self.assertEqual(build_index([]),{})
'''),
]


class Evaluation:
    def __init__(self, store, engine):
        self.store, self.engine = store, engine
        with store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS evaluations (
                id INTEGER PRIMARY KEY, project_id TEXT NOT NULL UNIQUE,
                benchmark TEXT NOT NULL, suite TEXT NOT NULL, model TEXT NOT NULL,
                repeat INTEGER NOT NULL, config TEXT NOT NULL, created TEXT NOT NULL);''')

    def start(self, models, repeats=1, benchmark_keys=None):
        if not isinstance(models, list) or not 1 <= len(models) <= 8 or any(not isinstance(m, str) or not m.strip() or len(m) > 200 for m in models):
            raise ValueError('Select one to eight installed models')
        if type(repeats) is not int or not 1 <= repeats <= 5:
            raise ValueError('Repeats must be between one and five')
        cases = BENCHMARKS
        if benchmark_keys is not None:
            known = {b['key'] for b in BENCHMARKS}
            if not isinstance(benchmark_keys, list) or not benchmark_keys or any(not isinstance(k, str) or k not in known for k in benchmark_keys):
                raise ValueError('Choose at least one known benchmark')
            cases = [b for b in BENCHMARKS if b['key'] in benchmark_keys]
        config = dict(context=int(self.store.setting('context', '8192')), temperature=0.2,
                      num_predict=2048, max_steps=20, max_attempts=1, suite=SUITE_VERSION,
                      python=sys.version.split()[0], mode='bounded-worker', benchmarks=[b['key'] for b in cases],
                      team_fingerprint=self.engine.team.fingerprint())
        ids = []
        for model in dict.fromkeys(models):
            for repeat in range(1, repeats + 1):
                for case in cases:
                    pid = uuid.uuid4().hex[:12]
                    folder = self.store.root / 'projects' / pid
                    folder.mkdir()
                    (folder / 'solution.py').write_text(case['initial'], encoding='utf-8')
                    for name, content in case.get('files', {}).items():
                        target = folder / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding='utf-8')
                    (folder / '.gitignore').write_text('__pycache__/\n*.pyc\n', encoding='utf-8')
                    git(folder, 'init')
                    git(folder, 'add', '-A')
                    git(folder, 'commit', '-m', 'Evaluation baseline')
                    hidden = self.store.root / 'evaluations' / pid
                    hidden.mkdir(parents=True)
                    # Acceptance files live outside the model-accessible project folder.
                    runner = hidden / 'acceptance.py'
                    runner.write_text('import sys, os\nsys.path.insert(0, os.getcwd())\n' + case['tests'] + '\nif __name__ == "__main__": unittest.main()\n', encoding='utf-8')
                    checks = json.dumps([[sys.executable, str(runner)]])
                    with self.store.db() as db:
                        db.execute('INSERT INTO projects(id,name,brief,status,created,checks,max_steps,max_attempts,model) VALUES (?,?,?,?,?,?,?,?,?)',
                                   (pid, f"Eval / {case['key']} / {model} / {repeat}", case['brief'], 'paused', now(), checks, 20, 1, model))
                        db.execute('INSERT INTO tasks(project_id,title,description,role,created) VALUES (?,?,?,?,?)',
                                   (pid, case['title'], case['brief'], 'programmer', now()))
                        db.execute('INSERT INTO evaluations(project_id,benchmark,suite,model,repeat,config,created) VALUES (?,?,?,?,?,?,?)',
                                   (pid, case['key'], SUITE_VERSION, model, repeat, json.dumps(config), now()))
                        db.execute('UPDATE projects SET context=? WHERE id=?', (config['context'], pid))
                    ids.append(pid)
                    self.store.event(pid, 'evaluation', 'Fresh evaluation queued. Acceptance tests are outside the agent workspace. Functional checks do not prove algorithmic complexity.')
        for pid in ids:
            self.store.execute("UPDATE projects SET status='running' WHERE id=?", (pid,))
        return ids

    def summary(self):
        records = self.store.rows('''SELECT e.*, p.status, t.status AS task_status,
                    r.seconds, r.tokens, r.revision, r.detail, r.status AS run_status, r.resource_peak, r.config AS runtime_config
                    FROM evaluations e JOIN projects p ON p.id=e.project_id
                    JOIN tasks t ON t.project_id=p.id
                    LEFT JOIN runs r ON r.id=(SELECT MAX(id) FROM runs WHERE task_id=t.id)
                    ORDER BY e.id DESC''')
        groups = {}
        for row in records:
            config = json.loads(row['config'])
            runtime = json.loads(row['runtime_config'] or '{}')
            if runtime.get('team_fingerprint'):
                config['team_fingerprint'] = runtime['team_fingerprint']
            row['config'] = json.dumps(config, sort_keys=True)
            # Keep different harness configurations separate on the leaderboard.
            key = (row['model'], row['config'])
            entry = groups.setdefault(key, dict(model=row['model'], config=json.loads(row['config']),
                                              total=0, completed=0, passed=0, seconds=0, successful_seconds=0, tokens=0,
                                              peak_gpu_memory_mb=None, peak_host_ram_mb=None, categories={}))
            entry['total'] += 1
            if row['run_status'] in ('verified', 'blocked', 'review', 'done'):
                entry['completed'] += 1
                entry['seconds'] += row['seconds'] or 0
                entry['tokens'] += row['tokens'] or 0
                resources = json.loads(row['resource_peak'] or '{}')
                for source, target in [('gpu_memory_mb', 'peak_gpu_memory_mb'), ('host_ram_used_mb', 'peak_host_ram_mb')]:
                    if resources.get(source) is not None:
                        entry[target] = max(entry.get(target) or 0, resources[source])
                category = next((b['category'] for b in BENCHMARKS if b['key'] == row['benchmark']), 'legacy')
                counts = entry['categories'].setdefault(category, dict(completed=0, passed=0))
                counts['completed'] += 1
                counts['passed'] += int(row['run_status'] == 'verified' and bool(row['revision']))
            if row['run_status'] == 'verified' and row['revision']:
                entry['passed'] += 1
                entry['successful_seconds'] += row['seconds'] or 0
        leaderboard = list(groups.values())
        for item in leaderboard:
            item['success_rate'] = round(item['passed'] / item['completed'] * 100, 1) if item['completed'] else None
            item['average_success_seconds'] = round(item['successful_seconds'] / item['passed'], 2) if item['passed'] else None
        leaderboard.sort(key=lambda x: (-(x['success_rate'] or 0), x['average_success_seconds'] or float('inf')))
        return dict(benchmarks=[{k: c[k] for k in ('key', 'title', 'category')} for c in BENCHMARKS],
                    runs=records, leaderboard=leaderboard, suite=SUITE_VERSION)
