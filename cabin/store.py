import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'projects').mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, brief TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft', created TEXT NOT NULL,
              checks TEXT NOT NULL DEFAULT '[]', max_steps INTEGER NOT NULL DEFAULT 30,
              max_attempts INTEGER NOT NULL DEFAULT 3, model TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
              description TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'programmer',
              status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
              result TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, project_id TEXT, role TEXT NOT NULL,
              message TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, role TEXT NOT NULL,
              content TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
              id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, task_id INTEGER,
              model TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,
              seconds REAL NOT NULL, tokens INTEGER NOT NULL DEFAULT 0,
              revision TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS company_messages (
              id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, sender TEXT NOT NULL,
              recipient TEXT NOT NULL DEFAULT 'all', kind TEXT NOT NULL,
              content TEXT NOT NULL, task_id INTEGER, revision TEXT NOT NULL DEFAULT '',
              reply_to INTEGER, created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS company_project ON company_messages(project_id,id);
            CREATE TABLE IF NOT EXISTS agent_sessions (
              id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, task_id INTEGER NOT NULL DEFAULT 0,
              role TEXT NOT NULL, messages TEXT NOT NULL, updated TEXT NOT NULL,
              UNIQUE(project_id,task_id,role));
            CREATE TABLE IF NOT EXISTS notifications (
              id INTEGER PRIMARY KEY, project_id TEXT, task_id INTEGER,
              kind TEXT NOT NULL, message TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0,
              created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_history (
              id INTEGER PRIMARY KEY, project_id TEXT, task_id INTEGER, role TEXT NOT NULL,
              kind TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL,
              instructions_fingerprint TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS execution_history_scope ON execution_history(project_id,task_id,id);
            CREATE TRIGGER IF NOT EXISTS history_no_update BEFORE UPDATE ON execution_history
              BEGIN SELECT RAISE(ABORT,'Execution history is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS history_no_delete BEFORE DELETE ON execution_history
              BEGIN SELECT RAISE(ABORT,'Execution history is append-only'); END;
            CREATE TABLE IF NOT EXISTS audit_reports (
              id INTEGER PRIMARY KEY, project_id TEXT, task_id INTEGER, type TEXT NOT NULL,
              severity TEXT NOT NULL, title TEXT NOT NULL, evidence_ids TEXT NOT NULL,
              observations TEXT NOT NULL, recommendation TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open', fingerprint TEXT NOT NULL UNIQUE, created TEXT NOT NULL);
            ''')
            migrations = {
                'projects': {'context': 'INTEGER NOT NULL DEFAULT 0',
                    'autonomy': 'INTEGER NOT NULL DEFAULT 0', 'auto_resume': 'INTEGER NOT NULL DEFAULT 0',
                    'max_runtime': 'INTEGER NOT NULL DEFAULT 3600', 'max_tokens': 'INTEGER NOT NULL DEFAULT 100000',
                    'idle_approved': 'INTEGER NOT NULL DEFAULT 0', 'completion_report': "TEXT NOT NULL DEFAULT ''",
                    'commands': "TEXT NOT NULL DEFAULT '[]'", 'memory': "TEXT NOT NULL DEFAULT ''"},
                'runs': {'resource_peak': "TEXT NOT NULL DEFAULT '{}'", 'config': "TEXT NOT NULL DEFAULT '{}'"},
                'tasks': {'priority': 'INTEGER NOT NULL DEFAULT 0', 'depends_on': "TEXT NOT NULL DEFAULT '[]'",
                    'stage': "TEXT NOT NULL DEFAULT 'implementation'", 'branch': "TEXT NOT NULL DEFAULT ''",
                    'base_revision': "TEXT NOT NULL DEFAULT ''", 'candidate_revision': "TEXT NOT NULL DEFAULT ''",
                    'review_round': 'INTEGER NOT NULL DEFAULT 0', 'ui_failures': "TEXT NOT NULL DEFAULT '{}'"}
            }
            for table, additions in migrations.items():
                columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
                for column, declaration in additions.items():
                    if column not in columns:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
            interrupted = list(db.execute("SELECT id,auto_resume FROM projects WHERE status='running'"))
            db.execute("UPDATE projects SET status='paused' WHERE status='running' AND auto_resume=0")
            db.execute("UPDATE tasks SET status='queued' WHERE status='running'")
            for project in interrupted:
                db.execute('INSERT INTO events(project_id,role,message,created) VALUES (?,?,?,?)',
                           (project['id'], 'system', 'Recovered interrupted work; ' +
                            ('automatic resume enabled.' if project['auto_resume'] else 'paused for inspection.'), now()))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'cabin.db', timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.db() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.db() as db:
            return db.execute(sql, args).lastrowid

    def setting(self, key, default=''):
        rows = self.rows('SELECT value FROM settings WHERE key=?', (key,))
        return rows[0]['value'] if rows else default

    def set_setting(self, key, value):
        self.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, value))

    def append_history(self, pid, tid, role, kind, payload, fingerprint='', session_id=''):
        encoded = json.dumps(payload, ensure_ascii=False)
        # Preserve complete records, including large screenshots, outside prompt windows.
        # The HTTP detail endpoint can serve large records separately from list pages.
        return self.execute('INSERT INTO execution_history(project_id,task_id,role,kind,payload,created,instructions_fingerprint,session_id) VALUES (?,?,?,?,?,?,?,?)',
                            (pid, tid, role, kind, encoded, now(), fingerprint, session_id))

    def event(self, project, role, message):
        self.execute('INSERT INTO events(project_id,role,message,created) VALUES (?,?,?,?)',
                     (project, role, str(message)[:30000], now()))

    def project(self, pid):
        rows = self.rows('SELECT * FROM projects WHERE id=?', (pid,))
        if not rows:
            raise ValueError('Project not found')
        return rows[0]

    def folder(self, pid):
        self.project(pid)
        return self.root / 'projects' / pid

    def snapshot(self):
        return dict(projects=self.rows('SELECT * FROM projects ORDER BY created DESC'),
                    tasks=self.rows('SELECT * FROM tasks ORDER BY id'),
                    events=self.rows('SELECT * FROM events ORDER BY id DESC LIMIT 150'),
                    runs=self.rows('SELECT * FROM runs ORDER BY id DESC LIMIT 200'),
                    company=self.rows('SELECT * FROM company_messages ORDER BY id DESC LIMIT 200'),
                    notifications=self.rows('SELECT * FROM notifications ORDER BY id DESC LIMIT 100'),
                    sessions=self.rows('SELECT id,project_id,task_id,role,updated FROM agent_sessions ORDER BY updated DESC LIMIT 100'))

    def post_message(self, pid, sender, recipient, kind, content, task_id=None, revision='', reply_to=None):
        from .company import validate_message
        validate_message(self, pid, sender, recipient, kind, content, task_id, revision, reply_to)
        with self.db() as db:
            mid = db.execute('INSERT INTO company_messages(project_id,sender,recipient,kind,content,task_id,revision,reply_to,created) VALUES (?,?,?,?,?,?,?,?,?)',
                             (pid, sender, recipient, kind, content.strip(), task_id, revision, reply_to, now())).lastrowid
            if kind == 'blocker' or (recipient == 'user' and sender != 'user'):
                db.execute('INSERT INTO notifications(project_id,task_id,kind,message,created) VALUES (?,?,?,?,?)',
                           (pid, task_id, kind, content[:2000], now()))
        return mid

    def company_context(self, pid, task_id=None, role=None, limit=12):
        clauses, params = ['project_id=?'], [pid]
        if task_id is not None:
            clauses.append('(task_id=? OR task_id IS NULL)')
            params.append(task_id)
        if role:
            clauses.append("(recipient IN ('all',?, 'manager') OR sender=?)")
            params.extend([role, role])
        params.append(min(max(int(limit), 1), 30))
        rows = self.rows('SELECT * FROM company_messages WHERE ' + ' AND '.join(clauses) + ' ORDER BY id DESC LIMIT ?', params)[::-1]
        return '\n'.join(f"#{r['id']} {r['sender']} → {r['recipient']} [{r['kind']}] task={r['task_id']} revision={r['revision'][:12]}: {r['content'][:1200]}" for r in rows)[-12000:]

    def task_context(self, pid):
        tasks = self.rows('SELECT * FROM tasks WHERE project_id=? ORDER BY priority DESC,id LIMIT 100', (pid,))
        return '\n'.join(f"#{t['id']} [{t['status']}/{t['stage']}] {t['role']}: {t['title']} depends={t['depends_on']} result={t['result'][-500:]}" for t in tasks)[:16000]

    def save_session(self, pid, task_id, role, messages):
        encoded = json.dumps(messages, ensure_ascii=False)
        if len(encoded.encode()) > 2_000_000:
            raise ValueError('Session exceeds the 2 MB storage limit')
        self.execute('INSERT INTO agent_sessions(project_id,task_id,role,messages,updated) VALUES (?,?,?,?,?) ON CONFLICT(project_id,task_id,role) DO UPDATE SET messages=excluded.messages,updated=excluded.updated',
                     (pid, task_id or 0, role, encoded, now()))

    def load_session(self, pid, task_id, role):
        rows = self.rows('SELECT messages FROM agent_sessions WHERE project_id=? AND task_id=? AND role=?', (pid, task_id or 0, role))
        return json.loads(rows[0]['messages']) if rows else []
