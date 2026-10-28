import json
import os
import subprocess
import threading
import time
import urllib.request
import urllib.error
import tempfile
import signal
import base64
from pathlib import Path
from .store import now
from .workspaces import Workspaces
from .metrics import ResourceMonitor
from .team import ROLES as TEAM_ROLES, Team
from .scheduler import PriorityGate
from .history import Recorder

# The assistant has a visible identity but is not a worker task assignment.
ROLES = {name: instructions for name, instructions in TEAM_ROLES.items() if name not in ('assistant','analyst')}


def git(folder, *args, check=True):
    result = subprocess.run(['git', '-c', 'user.name=The Workshop', '-c', 'user.email=workshop@localhost', *args],
                            cwd=folder, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
                            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or 'Git operation failed')
    return result.stdout.strip()


def safe_path(folder, name):
    if not isinstance(name, str) or not name or '\x00' in name or ':' in name or '\\' in name:
        raise ValueError('Use a relative project path with forward slashes')
    path = (folder / name).resolve()
    root = folder.resolve()
    if not path.is_relative_to(root) or path == root or any(p.lower().rstrip('. ') == '.git' for p in path.relative_to(root).parts):
        raise ValueError('Path must be a project file outside .git')
    return path


def run_check(command, folder, timeout=120):
    """Run trusted project code, capture bounded output, and terminate its process tree."""
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x or '\x00' in x for x in command):
        raise ValueError('Each check must be a nonempty JSON array of command arguments')
    flags = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(command, cwd=folder, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, **flags)
        deadline = time.monotonic() + timeout
        failure = ''
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                failure = f'Check timed out after {timeout} seconds.'
                break
            if os.fstat(output.fileno()).st_size > 4_000_000:
                failure = 'Check exceeded the 4 MB output limit.'
                break
            time.sleep(0.05)
        if failure:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=15)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.kill()
            proc.wait(timeout=10)
        output.seek(0, 2)
        length = output.tell()
        output.seek(max(0, length - 16000))
        text = output.read().decode('utf-8', errors='replace')
        return proc.returncode == 0 and not failure, text + ('\n' + failure if failure else '')


def files(folder):
    ignored = {'.git', 'node_modules', '.venv', '__pycache__', 'venv'}
    result = []
    for base, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ignored and not (Path(base) / d).is_symlink())
        for name in sorted(names):
            if name.lower() == '.git':
                continue
            p = Path(base) / name
            if not p.is_symlink():
                result.append(p.relative_to(folder).as_posix())
            if len(result) >= 1500:
                return result
    return result


class Ollama:
    def __init__(self, store):
        self.store = store

    def request(self, route, body=None):
        url = self.store.setting('ollama_url', 'http://127.0.0.1:11434').rstrip('/') + route
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=300 if body else 10) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ValueError('Cannot reach Ollama. Check its address, model, and that Ollama is running. ' + str(exc)) from exc

    def generate(self, model, messages, schema=None, context=None):
        if not model:
            raise ValueError('Choose a model in Settings first.')
        body = dict(model=model, messages=messages, stream=False, keep_alive='15m',
                    options=dict(num_ctx=context or int(self.store.setting('context', '8192')), num_predict=2048, temperature=0.2))
        if schema:
            body['format'] = schema
        result = self.request('/api/chat', body)
        if result.get('error'):
            raise ValueError(str(result['error']))
        return result['message']['content'], result.get('eval_count', 0)


ACTION_SCHEMA = {'type': 'object', 'properties': {
 'action': {'type': 'string', 'enum': ['list', 'read', 'write', 'patch', 'delete', 'search', 'issue', 'message', 'remember', 'screenshot', 'browser', 'run', 'check', 'finish']},
 'path': {'type': 'string'}, 'content': {'type': 'string'}, 'summary': {'type': 'string'}},
 'required': ['action', 'path', 'content', 'summary']}
PLAN_SCHEMA = {'type': 'object', 'properties': {'tasks': {'type': 'array', 'minItems': 1, 'maxItems': 12,
 'items': {'type': 'object', 'properties': {'title': {'type': 'string'}, 'description': {'type': 'string'},
 'role': {'type': 'string', 'enum': ['programmer', 'designer', 'debugger', 'qa', 'documentation']},
 'priority': {'type': 'integer', 'minimum': 0, 'maximum': 10},
 'depends_on': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}}},
 'required': ['title', 'description', 'role']}}}, 'required': ['tasks']}
REVIEW_SCHEMA = {'type': 'object', 'properties': {
 'verdict': {'type': 'string', 'enum': ['approve', 'changes', 'block']},
 'findings': {'type': 'string'}}, 'required': ['verdict', 'findings']}
TRIAGE_SCHEMA = {'type': 'object', 'properties': {
 'decision': {'type': 'string', 'enum': ['retry', 'block']},
 'reason': {'type': 'string'}, 'guidance': {'type': 'string'}},
 'required': ['decision', 'reason', 'guidance']}


class Engine:
    def __init__(self, store):
        self.store = store
        self.ollama = Ollama(store)
        self.team = Team(store)
        self.lock = PriorityGate()
        self.recorder = Recorder(store)
        from .browser import BrowserSessions
        self.browser = BrowserSessions(store)
        from .auditor import OperationsAnalyst
        self.analyst = OperationsAnalyst(store, self)
        self._project_locks = {}
        self._locks_guard = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.workspaces = Workspaces(store, git)
        self.current = None
        self.last_heartbeat = now()
        self._usage = {}

    def health(self):
        return dict(worker_alive=self.thread.is_alive(), stopping=self.stopping.is_set(),
                    current=self.current, last_heartbeat=self.last_heartbeat,
                    interactive_pending=self.interactive_pending, browser=self.browser.capabilities())

    @property
    def interactive_pending(self):
        return self.lock.interactive_pending

    def infer(self, model, messages, schema=None, context=None, *, role='system', pid=None, tid=None):
        started = time.monotonic()
        try:
            response, tokens = self.ollama.generate(model, messages, schema, context=context)
        except Exception as exc:
            self.recorder.model(role,pid,tid,messages,'',model,0,time.monotonic()-started,error=str(exc))
            raise
        self.recorder.model(role,pid,tid,messages,response,model,tokens,time.monotonic()-started)
        return response,tokens

    def task_workspace(self, pid, task_id):
        task = self.store.rows('SELECT * FROM tasks WHERE project_id=? AND id=?', (pid, task_id))
        if not task:
            raise ValueError('Task not found')
        folder = self.workspaces.path(pid, task_id)
        if task[0].get('branch') and folder.is_dir():
            return self.workspaces.prepare(self.store.project(pid), task[0])
        return self.store.folder(pid)

    def integrate_task(self, pid, task_id, manual=False):
        with self.project_lock(pid):
            task = self.store.rows('SELECT * FROM tasks WHERE project_id=? AND id=?', (pid, task_id))
            if not task:
                raise ValueError('Task not found')
            task = task[0]
            if not manual and task.get('stage') != 'integrating':
                raise ValueError('Candidate has not completed QA and review')
            revision = self.workspaces.integrate(self.store.project(pid), task)
            self.store.execute("UPDATE tasks SET status='done',stage='integrated' WHERE id=?", (task_id,))
            self.store.post_message(pid, 'manager', 'all', 'decision',
                                    ('Human accepted and integrated this task candidate.' if manual else
                                     'Integrated this candidate after AI QA, AI review, and passing configured checks.'), task_id, revision)
            return revision

    def reopen_task(self, pid, task_id):
        with self.project_lock(pid):
            project = self.store.project(pid)
            if project['status'] == 'running':
                raise ValueError('Pause the project before reopening tasks')
            tasks = self.store.rows('SELECT * FROM tasks WHERE project_id=? AND id=?', (pid, task_id))
            if not tasks:
                raise ValueError('Task not found')
            task = tasks[0]
            stale = task.get('base_revision') and git(self.store.folder(pid), 'rev-parse', 'HEAD') != task['base_revision']
            if task.get('stage') == 'integrated' or stale:
                self.workspaces.archive(project, task)
                self.store.execute("UPDATE tasks SET status='queued',stage='implementation',branch='',base_revision='',candidate_revision='',review_round=0,attempts=0 WHERE id=?", (task_id,))
                self.store.execute('DELETE FROM agent_sessions WHERE project_id=? AND task_id=?', (pid, task_id))
            else:
                stage = 'debugger' if task.get('candidate_revision') else 'implementation'
                self.store.execute("UPDATE tasks SET status='queued',stage=?,attempts=0,review_round=0 WHERE id=?", (stage, task_id))
            self._post(pid, 'manager', 'all', 'decision', 'Reopened task against the current accepted project state. Previous branch history is retained.', task_id)
            return self.store.rows('SELECT * FROM tasks WHERE id=?', (task_id,))[0]

    def company_reply(self, pid, message_id):
        project = self.store.project(pid)
        rows = self.store.rows('SELECT * FROM company_messages WHERE project_id=? AND id=?', (pid, message_id))
        if not rows:
            raise ValueError('Company message not found')
        message = rows[0]
        role = message['recipient']
        if role not in ROLES:
            raise ValueError('Choose a specialist or manager for a direct reply')
        if self.store.rows('SELECT id FROM company_messages WHERE reply_to=? AND sender=?', (message_id, role)):
            return {'already_answered': True}
        context = [{'role': 'system', 'content': self.team.prompt(role, 'company')},
                   {'role': 'user', 'content': 'BRIEF:\n' + project['brief'][:6000] + '\nMEMORY:\n' + project.get('memory', '')[-4000:] +
                    '\nROADMAP:\n' + self.store.task_context(pid) + '\nCOMPANY CONTEXT:\n' + self.store.company_context(pid, message['task_id'], role, 12) +
                    '\nREQUEST TO ANSWER:\n' + message['content'][:12000]}]
        started = time.monotonic()
        with self.lock:
            reply, tokens = self.infer(self.model(project), context, context=project.get('context') or None,role=role,pid=pid,tid=message['task_id'])
        reply = reply.strip()[:16000]
        if not reply:
            raise ValueError('Specialist returned an empty response')
        self.store.save_session(pid, None, role, context + [{'role': 'assistant', 'content': reply}])
        mid = self.store.post_message(pid, role, message['sender'], 'findings', reply, message['task_id'], message['revision'], message_id)
        self._record_role_run(project, {'id': message['task_id']}, role, started, tokens, 'message', 'Company room reply')
        return {'message_id': mid, 'content': reply}

    def _post(self, pid, sender, recipient, kind, content, task_id=None, revision=''):
        return self.store.post_message(pid, sender, recipient, kind, content, task_id, revision)

    def _budget(self, project, extra_tokens=0, extra_seconds=0):
        pid = project['id']
        rows = self.store.rows('SELECT COALESCE(SUM(tokens),0) AS tokens,COALESCE(SUM(seconds),0) AS seconds FROM runs WHERE project_id=?', (pid,))
        used = rows[0]
        if used['tokens'] + extra_tokens >= project.get('max_tokens', 100000):
            return 'Project token budget reached. Increase the budget before resuming.'
        if used['seconds'] + extra_seconds >= project.get('max_runtime', 3600):
            return 'Project runtime budget reached. Increase the budget before resuming.'
        return ''

    def runnable_task(self, project):
        tasks = self.store.rows('SELECT * FROM tasks WHERE project_id=? ORDER BY priority DESC,id', (project['id'],))
        by_id = {t['id']: t for t in tasks}
        for task in tasks:
            if task['status'] != 'queued':
                continue
            dependencies = json.loads(task.get('depends_on', '[]'))
            if all(dep in by_id and by_id[dep]['status'] in ('done', 'verified') and
                   (not project.get('autonomy') or by_id[dep].get('stage') in ('integrated', 'implementation')) for dep in dependencies):
                return task
        return None

    def project_lock(self, pid):
        with self._locks_guard:
            return self._project_locks.setdefault(pid, threading.RLock())

    def model(self, project):
        return project['model'] or self.store.setting('model')

    def plan(self, pid):
        with self.project_lock(pid):
            if self.store.project(pid)['status'] == 'running':
                raise ValueError('Pause the project before generating a roadmap')
            return self._plan(pid)

    def _plan(self, pid):
        project = self.store.project(pid)
        messages = [
            {'role': 'system', 'content': self.team.prompt('manager', 'planning')},
            {'role': 'user', 'content': project['brief'][:12000] + '\nPROJECT MEMORY:\n' + project.get('memory', '')[-4000:]}]
        with self.lock:
            text, _ = self.infer(self.model(project), messages, PLAN_SCHEMA, context=project.get('context') or None,role='manager',pid=pid)
        self.store.save_session(pid, None, 'manager', messages + [{'role': 'assistant', 'content': text}])
        tasks = json.loads(text)['tasks']
        if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12:
            raise ValueError('Manager must return between one and twelve tasks')
        for index, task in enumerate(tasks):
            if task['role'] not in ROLES or not isinstance(task['title'], str) or not task['title'].strip() or len(task['title']) > 300 or not isinstance(task['description'], str) or not task['description'].strip() or len(task['description']) > 16000:
                raise ValueError('Model returned an invalid task')
            deps = task.get('depends_on', [])
            priority = task.get('priority', 0)
            if not isinstance(deps, list) or any(type(d) is not int or not 0 <= d < index for d in deps) or type(priority) is not int or not 0 <= priority <= 10:
                raise ValueError('Manager returned invalid dependencies or priority')
        with self.store.db() as db:
            created = []
            for task in tasks:
                created.append(db.execute('INSERT INTO tasks(project_id,title,description,role,created,priority,depends_on) VALUES (?,?,?,?,?,?,?)',
                           (pid, task['title'], task['description'], task['role'], now(), task.get('priority', 0),
                            json.dumps([created[d] for d in task.get('depends_on', [len(created)-1] if project.get('autonomy') and created else [])]))).lastrowid)
        self.store.event(pid, 'manager', f'Created {len(tasks)} roadmap tasks. Review them before starting.')
        if project.get('autonomy'):
            self._post(pid, 'manager', 'all', 'decision', f'Created {len(tasks)} roadmap tasks with explicit prerequisites. Review the roadmap before starting.')

    def checks(self, project, folder):
        commands = json.loads(project['checks'])
        if not commands:
            return True, 'No automated checks configured. Human review required.'
        logs, passed = [], True
        for command in commands:
            self.lock.checkpoint()
            # No shell expansion. Checks are configured by the human, never supplied by the model.
            try:
                started=time.monotonic()
                ok, output = run_check(command, folder)
                self.recorder.tool('qa',project['id'],self.current.get('task_id') if self.current else None,'check_command',command,output,
                                   seconds=time.monotonic()-started,error='' if ok else 'Check returned failure')
                logs.append('$ ' + ' '.join(command) + '\n' + output)
                passed = passed and ok
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.recorder.tool('qa',project['id'],self.current.get('task_id') if self.current else None,'check_command',command,'',error=str(exc))
                logs.append(str(exc))
                passed = False
        return passed, '\n\n'.join(logs)

    def active(self, pid):
        return not self.stopping.is_set() and self.store.project(pid)['status'] == 'running'

    def work(self, project, task):
        self.lock.checkpoint()
        if self.stopping.is_set():
            return
        with self.project_lock(project['id']):
            if not self.active(project['id']):
                return
            project = self.store.project(project['id'])
            stage = task.get('stage', 'implementation')
            active_role = {'qa': 'qa', 'reviewer': 'reviewer', 'debugger': 'debugger', 'integrating': 'qa'}.get(stage, task['role'])
            self.current = dict(project_id=project['id'], task_id=task['id'], role=active_role, stage=stage)
            self.last_heartbeat = now()
            previous_run = self.store.rows('SELECT COALESCE(MAX(id),0) AS id FROM runs WHERE project_id=?', (project['id'],))[0]['id']
            try:
                with ResourceMonitor() as monitor:
                    if project.get('autonomy'):
                        self._autonomous_work(project, task)
                    else:
                        self._work(project, task)
            finally:
                if 'monitor' in locals():
                    config = dict(model=self.model(project), context=project.get('context') or int(self.store.setting('context', '8192')),
                                  temperature=0.2, num_predict=2048, autonomy=bool(project.get('autonomy')),
                                  team_fingerprint=self.team.fingerprint())
                    self.store.execute('UPDATE runs SET resource_peak=?,config=? WHERE project_id=? AND id>?',
                                       (json.dumps(monitor.peak), json.dumps(config), project['id'], previous_run))
                self.current = None
                self.last_heartbeat = now()

    def _work(self, project, task, folder=None, autonomous=False):
        pid, folder = project['id'], folder or self.store.folder(project['id'])
        start, tokens = time.monotonic(), 0
        consultations = 0
        ui_failures = json.loads(task.get('ui_failures','{}'))
        model = self.model(project)
        self.store.execute("UPDATE tasks SET status='running', attempts=attempts+1 WHERE id=?", (task['id'],))
        self.store.event(pid, task['role'], 'Started: ' + task['title'])
        messages = [{'role': 'system', 'content': self.team.prompt(task['role'] if task['role'] in ROLES else 'programmer', 'autonomous' if autonomous else 'worker')},
          {'role': 'user', 'content': 'PROJECT BRIEF:\n' + project['brief'][:10000] + '\nTASK:\n' + task['title'] + '\n' + task['description'] + '\nPREVIOUS ATTEMPT:\n' + task['result'][-5000:] + '\nFILES:\n' + '\n'.join(files(folder))[:12000]}]
        if autonomous:
            messages[1]['content'] += '\nPROJECT MEMORY:\n' + project.get('memory', '')[-6000:] + '\nTEAM CONTEXT:\n' + self.store.company_context(pid, task['id'], task['role'], 10)
            messages[1]['content'] += '\nALLOWED COMMANDS:\n' + project.get('commands', '[]')
            prior = self.store.load_session(pid, task['id'], task['role'])
            if prior:
                messages += prior[-8:]
        status, detail, revision = 'blocked', 'Step budget exhausted.', ''
        try:
            for step in range(project['max_steps']):
                action, tool_started = None, None
                if autonomous:
                    reason = self._budget(project, tokens, time.monotonic() - start)
                    if reason:
                        status, detail = 'review', reason
                        self.store.execute("UPDATE projects SET status='paused' WHERE id=?", (pid,))
                        break
                if not self.active(pid):
                    status, detail = 'queued', 'Paused at a safe checkpoint.'
                    break
                with self.lock:
                    if not self.active(pid):
                        status, detail = 'queued', 'Paused at a safe checkpoint.'
                        break
                    text, count = self.infer(model, messages, ACTION_SCHEMA, context=project.get('context') or None,role=task['role'],pid=pid,tid=task['id'])
                tokens += count
                self.lock.checkpoint()
                if not self.active(pid):
                    status, detail = 'queued', 'Paused; discarded pending model action.'
                    break
                action = json.loads(text)
                self.last_heartbeat = now()
                if not isinstance(action, dict) or any(not isinstance(action.get(k), str) for k in ('action', 'path', 'content', 'summary')):
                    raise ValueError('Model returned an invalid action object')
                kind = action['action']
                tool_started = time.monotonic()
                image_data = None
                if kind == 'list':
                    output = '\n'.join(files(folder))[:16000]
                elif kind == 'read':
                    path = safe_path(folder, action['path'])
                    if path.stat().st_size > 100000:
                        raise ValueError('File too large to read; keep project files small.')
                    output = path.read_text(encoding='utf-8')[:16000]
                elif kind == 'patch':
                    from .editing import patch_file
                    output = json.dumps(patch_file(folder,action['path'],action['content']))
                    self.store.event(pid,task['role'],'Patched '+action['path'])
                elif kind == 'write':
                    path = safe_path(folder, action['path'])
                    if len(action['content'].encode()) > 200000:
                        raise ValueError('File exceeds write limit')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(path.name + '.cabin-tmp')
                    temporary.write_text(action['content'], encoding='utf-8')
                    temporary.replace(path)
                    output = 'Saved ' + action['path']
                    self.store.event(pid, task['role'], output)
                elif kind == 'delete':
                    path = safe_path(folder, action['path'])
                    if not path.is_file():
                        raise ValueError('Delete requires an existing file')
                    path.unlink()
                    output = 'Deleted ' + action['path']
                    self.store.event(pid, task['role'], output)
                elif kind == 'search':
                    needle = action['content'][:500]
                    matches = []
                    for name in files(folder):
                        path = safe_path(folder, name)
                        if path.stat().st_size > 100000:
                            continue
                        try:
                            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                                if needle in line:
                                    matches.append(f'{name}:{number}: {line[:300]}')
                                if len(matches) >= 50:
                                    break
                        except UnicodeError:
                            continue
                        if len(matches) >= 50:
                            break
                    output = '\n'.join(matches) or 'No matches'
                elif kind == 'issue':
                    title = action['summary'].strip()[:300]
                    if not title:
                        raise ValueError('Issue requires a title in summary')
                    issue_id = self.store.execute("INSERT INTO tasks(project_id,title,description,role,status,created,stage,depends_on) VALUES (?,?,?,'debugger','blocked',?,?,?)",
                                                  (pid, title, action['content'][:16000], now(), 'triage' if autonomous else 'implementation', json.dumps([task['id']]) if autonomous else '[]'))
                    output = f'Raised issue #{issue_id}; the manager/user must triage it.'
                    self.store.event(pid, task['role'], output)
                elif kind == 'check':
                    _, output = self.checks(project, folder)
                    self.store.event(pid, 'qa', output)
                elif kind == 'run' and autonomous:
                    commands = json.loads(project.get('commands', '[]'))
                    try:
                        index = int(action['path'])
                    except ValueError:
                        raise ValueError('run.path must be an allowed command index')
                    if not 0 <= index < len(commands):
                        raise ValueError('Command not authorised for this project')
                    ok, output = run_check(commands[index], folder)
                    output = ('Command passed\n' if ok else 'Command failed\n') + output
                    self.store.event(pid, task['role'], output)
                elif kind == 'browser' and autonomous:
                    try:
                        args = json.loads(action['content'] or '{}')
                        result = self.browser.perform(pid,task['id'],folder,action['path'],args)
                        if action['path']=='assert_text':
                            expected=result.get('expected_text',args.get('text',''))
                            if result.get('passed') is True:
                                ui_failures.pop(expected,None)
                            else:
                                ui_failures[expected]='UI assertion failed: expected visible text '+str(expected)
                            self.store.execute('UPDATE tasks SET ui_failures=? WHERE id=?',(json.dumps(ui_failures),task['id']))
                        output = json.dumps(result)
                        if result.get('screenshot_path'):
                            try:
                                info = self.ollama.request('/api/show', {'model':model})
                                if 'vision' in info.get('capabilities',[]):
                                    image_data=base64.b64encode(Path(result['screenshot_path']).read_bytes()).decode('ascii')
                            except (ValueError,AttributeError):
                                pass
                    except Exception as exc:
                        output = 'Browser action failed: '+str(exc)
                elif kind == 'screenshot' and autonomous:
                    try:
                        from .visual import render_html
                        target = self.store.root / 'screenshots' / pid / (str(task['id']) + '.png')
                        result = render_html(folder, action['path'], target)
                        target = Path(result) if isinstance(result, (str, Path)) else target
                        vision = False
                        try:
                            info = self.ollama.request('/api/show', {'model': model})
                            vision = 'vision' in info.get('capabilities', [])
                        except (ValueError, AttributeError):
                            pass
                        if vision:
                            image_data = base64.b64encode(target.read_bytes()).decode('ascii')
                            output = 'Rendered screenshot attached for visual inspection. This is evidence for this rendered HTML only.'
                        else:
                            output = 'Screenshot captured for human inspection. This model does not advertise vision support; do not claim visual verification.'
                        self._post(pid, task['role'], 'all', 'findings', output, task['id'])
                    except Exception as exc:
                        output = 'Screenshot unavailable: ' + str(exc) + '. Do not claim visual inspection.'
                elif kind == 'message' and autonomous:
                    recipient, category = action['path'], action['summary']
                    if recipient not in ('all', 'assistant', *ROLES) or category not in ('request', 'findings', 'blocker', 'handoff', 'decision'):
                        raise ValueError('Invalid company message recipient or kind')
                    mid = self._post(pid, task['role'], recipient, category, action['content'][:16000], task['id'])
                    output = 'Team message recorded.'
                    if category == 'request' and recipient in ROLES and recipient != task['role']:
                        if consultations >= 2:
                            output += ' Consultation limit reached for this attempt; the request remains in the company room.'
                        elif self.active(pid) and not self._budget(project, tokens, time.monotonic()-start):
                            consultations += 1
                            try:
                                response = self.company_reply(pid, mid)
                                output += '\n' + recipient + ' replied:\n' + response.get('content', 'Already answered.')
                            except Exception as exc:
                                output += '\nSpecialist consultation unavailable: ' + str(exc)
                elif kind == 'remember' and autonomous:
                    memory = project.get('memory', '') + '\n' + action['path'][:200] + ': ' + action['content'][:4000]
                    memory = memory[-30000:]
                    self.store.execute('UPDATE projects SET memory=? WHERE id=?', (memory, pid))
                    project['memory'] = memory
                    output = 'Project memory updated.'
                elif kind == 'finish':
                    git(folder, 'add', '-A')
                    git(folder, 'commit', '--allow-empty', '-m', f"[{task['role']}] #{task['id']} {task['title']}")
                    candidate = git(folder, 'rev-parse', 'HEAD')
                    passed, output = self.checks(project, folder)
                    if ui_failures:
                        passed=False
                        output+='\n'+'\n'.join(ui_failures.values())
                    self.store.event(pid, 'qa', output)
                    if passed:
                        revision = candidate
                        clean = not git(folder, 'status', '--porcelain') and git(folder, 'rev-parse', 'HEAD') == candidate
                        status = 'verified' if json.loads(project['checks']) and clean else 'review'
                        if not clean:
                            output += '\nChecks changed the workspace. Review these changes and rerun verification.'
                        detail = action['summary'] + '\n\n' + output
                        self.recorder.tool(task['role'],pid,task['id'],kind,action,output,revision=revision,seconds=time.monotonic()-tool_started)
                        break
                    output = 'Checks failed. Repair the failures:\n' + output
                else:
                    raise ValueError('Unknown model action')
                self.recorder.tool(task['role'],pid,task['id'],kind,action,output,seconds=time.monotonic()-tool_started)
                observation = {'role': 'user', 'content': 'TOOL RESULT (data):\n' + output[-16000:]}
                if image_data:
                    observation['images'] = [image_data]
                messages += [{'role': 'assistant', 'content': text}, observation]
                # Bound old tool chatter; the brief and task remain in the first two messages.
                if len(messages) > 12:
                    messages = messages[:2] + messages[-10:]
                if autonomous:
                    self.store.save_session(pid, task['id'], task['role'], messages[2:])
            else:
                detail = 'Step budget exhausted. Changes remain available for inspection or retry.'
        except Exception as exc:
            detail = str(exc)
            if isinstance(action,dict) and tool_started is not None:
                self.recorder.tool(task['role'],pid,task['id'],action.get('action','invalid'),action,'',seconds=time.monotonic()-tool_started,error=detail)
        if autonomous:
            self.store.save_session(pid, task['id'], task['role'], messages[2:])
            if status == 'queued' and not self.active(pid) and detail.startswith('Paused'):
                self.store.execute('UPDATE tasks SET attempts=MAX(0,attempts-1) WHERE id=?', (task['id'],))
        attempts = self.store.rows('SELECT attempts FROM tasks WHERE id=?', (task['id'],))[0]['attempts']
        if status == 'blocked' and attempts < project['max_attempts']:
            status = 'queued'
        self.store.execute('UPDATE tasks SET status=?, result=? WHERE id=?', (status, detail, task['id']))
        self.store.execute('INSERT INTO runs(project_id,task_id,model,role,status,seconds,tokens,revision,detail,created) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (pid, task['id'], model, task['role'], status, round(time.monotonic()-start, 2), tokens, revision, detail, now()))
        self.store.event(pid, task['role'], f"{task['title']}: {status}\n{detail}")
        if status == 'blocked' and not autonomous:
            self.store.execute("UPDATE projects SET status='blocked' WHERE id=?", (pid,))

    def _record_role_run(self, project, task, role, started, tokens, status, detail, revision=''):
        self.store.execute('INSERT INTO runs(project_id,task_id,model,role,status,seconds,tokens,revision,detail,created) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (project['id'], task['id'], self.model(project), role, status,
                            round(time.monotonic()-started, 2), tokens, revision, detail, now()))

    def _review(self, project, task, folder, role):
        pid = project['id']
        candidate = task['candidate_revision']
        if git(folder, 'rev-parse', 'HEAD') != candidate or git(folder, 'status', '--porcelain'):
            raise ValueError('Candidate changed after checks. Reopen the implementation before reviewing.')
        evidence = self.store.rows('SELECT role,status,detail,revision FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 4', (task['id'],))
        prompt = ('PROJECT BRIEF:\n' + project['brief'][:6000] + '\nTASK ACCEPTANCE:\n' + task['description'][:6000] +
                  '\nPROJECT MEMORY:\n' + project.get('memory', '')[-3000:] + '\nCOMPANY CONTEXT:\n' +
                  self.store.company_context(pid, task['id'], role, 8) + '\nCHECK EVIDENCE:\n' + json.dumps(evidence)[-9000:] +
                  '\nEXACT CANDIDATE DIFF:\n' + self.workspaces.diff(task, folder))
        messages = [{'role': 'system', 'content': self.team.prompt(role, 'review')},
                    {'role': 'user', 'content': prompt}]
        started = time.monotonic()
        with self.lock:
            if not self.active(pid):
                return None
            raw, tokens = self.infer(self.model(project), messages, REVIEW_SCHEMA, context=project.get('context') or None,role=role,pid=pid,tid=task['id'])
        if not self.active(pid):
            self._record_role_run(project, task, role, started, tokens, 'queued', 'Paused; discarded pending review.', candidate)
            return None
        verdict = json.loads(raw)
        if verdict.get('verdict') not in ('approve', 'changes', 'block') or not isinstance(verdict.get('findings'), str):
            raise ValueError('Reviewer returned an invalid verdict')
        self.store.save_session(pid, task['id'], role, messages + [{'role': 'assistant', 'content': raw}])
        self._record_role_run(project, task, role, started, tokens, verdict['verdict'], verdict['findings'], candidate)
        self._post(pid, role, 'manager' if role == 'reviewer' else 'reviewer', 'findings',
                   verdict['findings'], task['id'], candidate)
        return verdict

    def _triage(self, project, task, failure):
        """One bounded manager decision per failed implementation; no recursive chatter."""
        pid = project['id']
        budget = self._budget(project)
        if budget:
            self.store.execute("UPDATE projects SET status='paused' WHERE id=?", (pid,))
            self._post(pid, 'manager', 'user', 'blocker', budget, task['id'])
            return
        if task['attempts'] >= project['max_attempts']:
            self._post(pid, 'manager', 'user', 'blocker', 'Attempt budget exhausted. ' + failure[-4000:], task['id'])
            self.store.execute("UPDATE tasks SET status='blocked' WHERE id=?", (task['id'],))
            return
        started = time.monotonic()
        messages = [{'role': 'system', 'content': self.team.prompt('manager', 'triage')},
                    {'role': 'user', 'content': 'TASK:\n' + task['title'] + '\n' + task['description'][:6000] + '\nFAILURE:\n' + failure[-8000:] +
                     '\nROADMAP:\n' + self.store.task_context(pid) + '\nTEAM:\n' + self.store.company_context(pid, task['id'], 'manager', 6)}]
        with self.lock:
            if not self.active(pid):
                return
            raw, tokens = self.infer(self.model(project), messages, TRIAGE_SCHEMA, context=project.get('context') or None,role='manager',pid=pid,tid=task['id'])
        if not self.active(pid):
            self._record_role_run(project, task, 'manager', started, tokens, 'queued', 'Paused; discarded pending triage.')
            return
        decision = json.loads(raw)
        if decision.get('decision') not in ('retry', 'block') or any(not isinstance(decision.get(k), str) for k in ('reason', 'guidance')):
            raise ValueError('Manager returned invalid triage')
        retry = decision['decision'] == 'retry'
        detail = failure[-8000:] + '\nMANAGER GUIDANCE:\n' + decision['guidance'][:4000]
        self.store.execute('UPDATE tasks SET status=?,stage=?,result=? WHERE id=?',
                           ('queued' if retry else 'blocked', 'debugger', detail, task['id']))
        self._record_role_run(project, task, 'manager', started, tokens, decision['decision'], decision['reason'])
        self.store.save_session(pid, task['id'], 'manager', messages + [{'role': 'assistant', 'content': raw}])
        self._post(pid, 'manager', 'debugger' if retry else 'user', 'handoff' if retry else 'blocker',
                   decision['reason'] + '\n' + decision['guidance'], task['id'])

    def _autonomous_work(self, project, task):
        pid = project['id']
        reason = self._budget(project)
        if reason:
            self.store.execute("UPDATE projects SET status='paused' WHERE id=?", (pid,))
            self._post(pid, 'manager', 'user', 'blocker', reason, task['id'])
            return
        folder = self.workspaces.prepare(project, task)
        task = self.store.rows('SELECT * FROM tasks WHERE id=?', (task['id'],))[0]
        stage = task.get('stage', 'implementation')
        if stage in ('implementation', 'debugger'):
            role = 'debugger' if stage == 'debugger' else task['role']
            self._post(pid, 'manager', role, 'handoff', task['title'] + '\n' + task['description'], task['id'])
            self._work(project, {**task, 'role': role}, folder, autonomous=True)
            updated = self.store.rows('SELECT * FROM tasks WHERE id=?', (task['id'],))[0]
            latest = self.store.rows('SELECT * FROM runs WHERE task_id=? ORDER BY id DESC LIMIT 1', (task['id'],))
            if not latest:
                return
            run = latest[0]
            if not self.active(pid):
                return
            if run['revision'] and updated['status'] in ('verified', 'review'):
                if git(folder, 'status', '--porcelain') or git(folder, 'rev-parse', 'HEAD') != run['revision']:
                    self.store.execute("UPDATE tasks SET status='review',stage='review',candidate_revision=? WHERE id=?", (run['revision'], task['id']))
                    self._post(pid, 'manager', 'user', 'blocker', 'Checks modified the candidate; inspect and reopen before integration.', task['id'], run['revision'])
                    return
                self.store.execute("UPDATE tasks SET status='queued',stage='qa',candidate_revision=? WHERE id=?", (run['revision'], task['id']))
                self._post(pid, role, 'qa', 'handoff', run['detail'], task['id'], run['revision'])
            else:
                self._triage(project, updated, updated['result'])
            return
        if stage in ('qa', 'reviewer'):
            self.store.execute("UPDATE tasks SET status='running' WHERE id=?", (task['id'],))
            verdict = self._review(project, task, folder, 'qa' if stage == 'qa' else 'reviewer')
            if verdict is None:
                self.store.execute("UPDATE tasks SET status='queued' WHERE id=?", (task['id'],))
                return
            if verdict['verdict'] != 'approve':
                rounds = task.get('review_round', 0) + 1
                self.store.execute('UPDATE tasks SET review_round=?,result=? WHERE id=?', (rounds, verdict['findings'], task['id']))
                if verdict['verdict'] == 'changes' and task['attempts'] < project['max_attempts'] and rounds < project['max_attempts']:
                    self.store.execute("UPDATE tasks SET status='queued',stage='debugger' WHERE id=?", (task['id'],))
                    self._post(pid, 'manager', 'debugger', 'handoff', verdict['findings'], task['id'], task['candidate_revision'])
                else:
                    self.store.execute("UPDATE tasks SET status='blocked',stage='review' WHERE id=?", (task['id'],))
                    self._post(pid, 'manager', 'user', 'blocker', verdict['findings'], task['id'], task['candidate_revision'])
                return
            if stage == 'qa':
                self.store.execute("UPDATE tasks SET status='queued',stage='reviewer' WHERE id=?", (task['id'],))
                return
            if not json.loads(project['checks']):
                self.store.execute("UPDATE tasks SET status='review',stage='review',result=result || ? WHERE id=?", ('\nAI QA and review approved, but no automated checks configured. Human acceptance required.', task['id']))
                self._post(pid, 'manager', 'user', 'request', 'Candidate needs your acceptance because no automated checks are configured.', task['id'], task['candidate_revision'])
                return
            self.store.execute("UPDATE tasks SET status='queued',stage='integrating' WHERE id=?", (task['id'],))
            return
        if stage == 'integrating':
            # Recheck the exact candidate immediately before integration.
            started = time.monotonic()
            passed, detail = self.checks(project, folder)
            self._record_role_run(project, task, 'qa', started, 0, 'passed' if passed else 'failed', detail, task['candidate_revision'])
            if not passed or git(folder, 'status', '--porcelain'):
                self.store.execute("UPDATE tasks SET status='review',stage='review',result=? WHERE id=?", (detail, task['id']))
                self._post(pid, 'qa', 'user', 'blocker', 'Final integration checks failed or modified files.\n' + detail, task['id'], task['candidate_revision'])
                return
            if not self.active(pid):
                return
            try:
                self.integrate_task(pid, task['id'])
            except ValueError as exc:
                self.store.execute("UPDATE tasks SET status='review',stage='review',result=? WHERE id=?", (str(exc), task['id']))
                self._post(pid, 'manager', 'user', 'blocker', str(exc), task['id'], task['candidate_revision'])
            return
        self.store.execute("UPDATE tasks SET status='review' WHERE id=?", (task['id'],))

    def loop(self):
        while not self.stopping.wait(1):
            self.last_heartbeat = now()
            for project in self.store.rows("SELECT * FROM projects WHERE status='running' ORDER BY created"):
                if self.stopping.is_set():
                    return
                if project.get('autonomy'):
                    issues = self.store.rows("SELECT * FROM tasks WHERE project_id=? AND status='blocked' AND stage='triage' ORDER BY priority DESC,id LIMIT 1", (project['id'],))
                    if issues:
                        try:
                            with self.project_lock(project['id']):
                                self._triage(project, issues[0], issues[0]['description'])
                        except Exception as exc:
                            self.store.execute("UPDATE tasks SET stage='review',result=? WHERE id=?", (str(exc), issues[0]['id']))
                            self._post(project['id'], 'manager', 'user', 'blocker', str(exc), issues[0]['id'])
                        continue
                task = self.runnable_task(project) if project.get('autonomy') else next(iter(self.store.rows("SELECT * FROM tasks WHERE project_id=? AND status='queued' ORDER BY id LIMIT 1", (project['id'],))), None)
                if task:
                    try:
                        self.work(project, task)
                    except Exception as exc:
                        self.store.event(project['id'], 'system', str(exc))
                        if project.get('autonomy'):
                            self.store.execute("UPDATE tasks SET status='blocked',result=? WHERE id=?", (str(exc), task['id']))
                        self.store.execute("UPDATE projects SET status='blocked' WHERE id=?", (project['id'],))
                elif not self.store.rows("SELECT id FROM tasks WHERE project_id=? AND status NOT IN ('verified','done')", (project['id'],)):
                    tasks=self.store.rows('SELECT id,title,status,stage,candidate_revision FROM tasks WHERE project_id=? ORDER BY id',(project['id'],))
                    revision=git(self.store.folder(project['id']),'rev-parse','HEAD')
                    report=json.dumps(dict(project_id=project['id'],revision=revision,tasks=tasks,
                        checks=json.loads(project['checks']),completed=now(),
                        summary='Queued tasks finished. Acceptance evidence is attached to each run; the brief still merits human review.'))
                    with self.store.db() as db:
                        changed=db.execute("UPDATE projects SET status='ready',completion_report=? WHERE id=? AND status='running'",(report,project['id'])).rowcount
                        if changed:
                            db.execute('INSERT INTO notifications(project_id,kind,message,created) VALUES (?,?,?,?)',
                                (project['id'],'completion',project['name']+' is ready for review and download.',now()))
                    if changed:
                        self._post(project['id'],'manager','user','status','Project is ready for review and download. Accepted revision: '+revision,revision=revision)
                else:
                    self.store.execute("UPDATE projects SET status='review' WHERE id=? AND status='running'", (project['id'],))
            if not self.store.rows("SELECT id FROM projects WHERE status='running' LIMIT 1") and not self.interactive_pending and not getattr(self,'operations_busy',lambda:False)():
                try:
                    self.analyst.tick()
                except Exception as exc:
                    self.store.event(None,'analyst','Idle analysis failed: '+str(exc))
                if self.interactive_pending or self.store.rows("SELECT id FROM projects WHERE status='running' LIMIT 1") or getattr(self,'operations_busy',lambda:False)():
                    continue
                # Only an explicit, one-shot user approval can start maintenance.
                approved=self.store.rows("SELECT * FROM projects WHERE idle_approved=1 AND status IN ('paused','draft','review','ready') ORDER BY created LIMIT 1")
                if approved:
                    project=approved[0]
                    if json.loads(project['checks']) and self.runnable_task(project) and not self._budget(project):
                        self.store.execute("UPDATE projects SET status='running',idle_approved=0 WHERE id=?",(project['id'],))
                        self._post(project['id'],'manager','user','status','Starting the explicitly approved idle maintenance queue. Approval is now consumed.')
