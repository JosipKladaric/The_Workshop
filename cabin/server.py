"""Authenticated local HTTP API and portable control-panel host.

No network service besides Ollama is required. Model output never becomes a
shell command. Configured project checks are explicitly trusted host programs.
"""
import hashlib
import hmac
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from contextlib import ExitStack
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .engine import Engine, ROLES, files, git, safe_path
from .store import Store, now
from .company import validate_dependencies, PARTICIPANTS, MESSAGE_KINDS
from .operations import BackupScheduler, export_backup, system_health, validated_import

MAX_BODY = 1024 * 1024
SESSION_SECONDS = 12 * 60 * 60
STATIC = Path(__file__).resolve().parent / 'static'
ASSISTANT_SCHEMA = {'type': 'object', 'properties': {
    'action': {'type': 'string', 'enum': ['reply', 'status', 'create_project', 'update_project', 'add_task', 'update_task', 'plan', 'start', 'pause', 'message']},
    'arguments': {'type': 'string'}, 'reply': {'type': 'string'}},
    'required': ['action', 'arguments', 'reply']}


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def text(value, name, limit=20000, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ApiError(f'{name} must be text' + (' and cannot be empty' if required else '') + f' (maximum {limit} characters).')
    return value.strip() if required else value


def integer(value, name, low, high):
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise ApiError(f'{name} must be an integer between {low} and {high}.')
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ApiError(f'{name} must be an integer between {low} and {high}.')
    if result < low or result > high:
        raise ApiError(f'{name} must be between {low} and {high}.')
    return result


def boolean(value, name):
    if type(value) not in (bool, int) or value not in (0, 1):
        raise ApiError(name + ' must be a boolean.')
    return int(value)


def checks(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ApiError('Checks must be JSON arrays of arguments, for example [["python", "-m", "unittest"]].')
    if not isinstance(value, list) or len(value) > 20:
        raise ApiError('Checks must be a list of at most 20 command argument lists.')
    for cmd in value:
        if not isinstance(cmd, list) or not 1 <= len(cmd) <= 50:
            raise ApiError('Each check must be a nonempty command argument list.')
        if any(not isinstance(arg, str) or not arg or len(arg) > 2000 or '\x00' in arg for arg in cmd):
            raise ApiError('Check arguments must be nonempty strings.')
    return json.dumps(value)


def local_url(value):
    value = text(value, 'Ollama address', 300, True).rstrip('/')
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError()
        port = parsed.port or 80
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not (ipaddress.ip_address(item[4][0]).is_private or ipaddress.ip_address(item[4][0]).is_loopback) for item in addresses):
            raise ValueError()
    except (ValueError, OSError):
        raise ApiError('Use an HTTP Ollama address on localhost or your private local network.')
    return value


class Application:
    def __init__(self, data_dir, password=None):
        self.store = Store(data_dir)
        self.engine = Engine(self.store)
        from .evaluation import Evaluation
        self.evaluation = Evaluation(self.store, self.engine)
        self.sessions = {}
        self.login_attempts = {}
        self.operations = {}
        self.guard = threading.RLock()
        self.project_locks = {}
        self.generated_password = None
        self.started = time.monotonic()
        self._health_cache = (0, {})
        self.preview_tokens = {}
        self.backups = BackupScheduler(self)
        self.job_threads = set()
        self.engine.operations_busy = lambda: any(job['status'] == 'running' for job in list(self.operations.values()))
        if password or not self.store.setting('password_hash'):
            generated = not password
            password = password or secrets.token_urlsafe(18)
            if len(password) < 12:
                raise ValueError('The panel password must contain at least 12 characters.')
            salt = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 300000).hex()
            self.store.set_setting('password_hash', salt + ':' + digest)
            self.generated_password = password if generated else None

    def authenticate(self, password, address):
        with self.guard:
            recent = [t for t in self.login_attempts.get(address, []) if time.time() - t < 60]
            if len(recent) >= 10:
                raise ApiError('Too many login attempts. Try again in a minute.', 429)
            self.login_attempts[address] = recent + [time.time()]
        salt, expected = self.store.setting('password_hash').split(':')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 300000).hex()
        if not hmac.compare_digest(expected, actual):
            raise ApiError('Incorrect password.', 401)
        token = secrets.token_urlsafe(32)
        session = {'csrf': secrets.token_urlsafe(32), 'expires': time.time() + SESSION_SECONDS}
        with self.guard:
            self.sessions = {k: v for k, v in self.sessions.items() if v['expires'] > time.time()}
            self.sessions[token] = session
            self.login_attempts.pop(address, None)
        return token, session

    def session(self, token):
        with self.guard:
            session = self.sessions.get(token)
            if not session or session['expires'] < time.time():
                self.sessions.pop(token, None)
                return None
            return session

    def settings(self):
        return {'ollama_url': self.store.setting('ollama_url', 'http://127.0.0.1:11434'),
                'model': self.store.setting('model'), 'context': int(self.store.setting('context', '8192')),
                'backup_interval_hours': int(self.store.setting('backup_interval_hours', '0')),
                'backup_keep': int(self.store.setting('backup_keep', '5')),
                'idle_audit_enabled': self.store.setting('idle_audit_enabled', '1') == '1',
                'idle_audit_model_enabled': self.store.setting('idle_audit_model_enabled', '1') == '1',
                'idle_interval_seconds': int(self.store.setting('idle_interval_seconds', '60'))}

    @contextmanager
    def project_lock(self, pid):
        if hasattr(self.engine, 'project_lock'):
            lock = self.engine.project_lock(pid)
        else:
            with self.guard:
                lock = self.project_locks.setdefault(pid, threading.RLock())
        if not lock.acquire(blocking=False):
            raise ApiError('The worker is using this project. Pause it and wait for its current action to finish.', 409)
        try:
            yield
        finally:
            lock.release()

    def async_job(self, pid, kind, action):
        with self.guard:
            if any(j['project_id'] == pid and j['kind'] == kind and j['status'] == 'running' for j in self.operations.values()):
                raise ApiError(f'{kind.title()} is already running for this project.', 409)
            jid = uuid.uuid4().hex
            job = {'id': jid, 'project_id': pid, 'kind': kind, 'status': 'running', 'result': None, 'error': '', 'created': now()}
            self.operations[jid] = job
            if len(self.operations) > 100:
                for old in list(self.operations):
                    if self.operations[old]['status'] != 'running':
                        del self.operations[old]
                        break
        def run():
            try:
                if kind == 'chat':
                    with self.engine.lock.as_interactive():
                        result = action()
                else:
                    result = action()
                with self.guard:
                    job.update(status='done', result=result)
            except Exception as exc:
                with self.guard:
                    job.update(status='failed', error=str(exc))
                self.store.event(pid, 'system', f'{kind.title()} failed: {exc}')
            finally:
                if kind == 'chat':
                    self.engine.lock.release_reservation()
                with self.guard:
                    self.job_threads.discard(threading.current_thread())
        thread = threading.Thread(target=run, name='workshop-' + kind, daemon=True)
        with self.guard:
            self.job_threads.add(thread)
        if kind == 'chat':
            self.engine.lock.reserve()
        try:
            thread.start()
        except Exception:
            if kind == 'chat':
                self.engine.lock.release_reservation()
            with self.guard:
                self.job_threads.discard(thread)
                job.update(status='failed', error='Could not start the requested operation.')
            raise
        return {'operation': dict(job)}

    def ensure_idle(self, pid, allow_operation=None):
        project = self.store.project(pid)
        if project['status'] == 'running':
            raise ApiError('Pause the project before changing its configuration or files.', 409)
        with self.guard:
            if any(j['project_id'] == pid and j['status'] == 'running' and j['kind'] in ('plan', 'check') and j['kind'] != allow_operation for j in self.operations.values()):
                raise ApiError('Wait for project planning or verification to finish.', 409)
        return project

    def create_project(self, body):
        name = text(body.get('name'), 'Project name', 100, True)
        brief = text(body.get('brief', ''), 'Project brief', 50000)
        pid = uuid.uuid4().hex[:16]
        check_list = checks(body.get('checks', []))
        steps = integer(body.get('max_steps', 30), 'Step budget', 1, 200)
        attempts = integer(body.get('max_attempts', 3), 'Retry budget', 1, 10)
        context = integer(body.get('context', 0), 'Project context', 0, 131072)
        model = text(body.get('model', ''), 'Model', 200)
        autonomy = boolean(body.get('autonomy', True), 'Autonomous workflow')
        auto_resume = boolean(body.get('auto_resume', False), 'Automatic resume')
        runtime = integer(body.get('max_runtime', 3600), 'Runtime budget', 60, 604800)
        tokens = integer(body.get('max_tokens', 100000), 'Token budget', 1000, 10000000)
        commands = checks(body.get('commands', []))
        memory = text(body.get('memory', ''), 'Project memory', 50000)
        folder = self.store.root / 'projects' / pid
        folder.mkdir()
        (folder / 'README.md').write_text(f'# {name}\n\n{brief}\n', encoding='utf-8')
        (folder / '.gitignore').write_text('.env\n.env.*\n!.env.example\n__pycache__/\n*.pyc\nnode_modules/\n.venv/\n', encoding='utf-8')
        try:
            git(folder, 'init')
            git(folder, 'add', '-A')
            git(folder, 'commit', '-m', 'Create project workspace')
        except Exception:
            # Leave files recoverable, but do not register an unusable project.
            raise ApiError('Could not initialise Git. Install Git and ensure it is on PATH.', 503)
        self.store.execute('INSERT INTO projects(id,name,brief,created,checks,max_steps,max_attempts,model,context,autonomy,auto_resume,max_runtime,max_tokens,commands,memory) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (pid, name, brief, now(), check_list, steps, attempts, model, context, autonomy, auto_resume, runtime, tokens, commands, memory))
        self.store.event(pid, 'assistant', 'Project created. Refine the brief, generate a roadmap, then start work.')
        return {'project': self.store.project(pid)}

    def edit_project(self, pid, body):
        self.ensure_idle(pid)
        values = {}
        for key, limit in [('name', 100), ('brief', 50000), ('model', 200), ('memory', 50000)]:
            if key in body:
                values[key] = text(body[key], key, limit, key == 'name')
        if 'checks' in body:
            values['checks'] = checks(body['checks'])
        if 'commands' in body:
            values['commands'] = checks(body['commands'])
        for key in ('autonomy', 'auto_resume'):
            if key in body:
                values[key] = boolean(body[key], key)
        for key, low, high in [('max_steps', 1, 200), ('max_attempts', 1, 10), ('context', 0, 131072), ('max_runtime', 60, 604800), ('max_tokens', 1000, 10000000)]:
            if key in body:
                values[key] = integer(body[key], key, low, high)
        if not values:
            raise ApiError('No project settings supplied.')
        with self.project_lock(pid):
            self.store.execute('UPDATE projects SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), pid))
        return {'project': self.store.project(pid)}

    def set_status(self, pid, value):
        project = self.store.project(pid)
        if value not in ('running', 'paused', 'archived', 'draft'):
            raise ApiError('Choose running, paused, archived, or draft.')
        if value == 'running':
            if not self.engine.model(project):
                raise ApiError('Choose a model in Settings or Project settings first.')
            with self.project_lock(pid):
                self.ensure_idle(pid)
                if not self.store.rows("SELECT id FROM tasks WHERE project_id=? AND status='queued'", (pid,)):
                    raise ApiError('Add or retry a task before starting.')
                self.store.execute('UPDATE projects SET status=? WHERE id=?', (value, pid))
        else:
            self.store.execute('UPDATE projects SET status=? WHERE id=?', (value, pid))
        self.store.event(pid, 'manager', 'Project ' + value + ('. Current action will stop at a safe checkpoint.' if value == 'paused' else '.'))
        return {'project': self.store.project(pid)}

    def add_task(self, pid, body):
        self.ensure_idle(pid)
        role = body.get('role', 'programmer')
        if role not in ROLES:
            raise ApiError('Unknown specialist role.')
        title = text(body.get('title'), 'Task title', 200, True)
        description = text(body.get('description', ''), 'Task description', 20000)
        priority = integer(body.get('priority', 0), 'Priority', -100, 100)
        dependencies = validate_dependencies(self.store, pid, None, body.get('depends_on', []))
        with self.project_lock(pid):
            tid = self.store.execute('INSERT INTO tasks(project_id,title,description,role,created,priority,depends_on) VALUES (?,?,?,?,?,?,?)',
                                     (pid, title, description, role, now(), priority, dependencies))
        self.store.event(pid, 'manager', 'Added task: ' + title)
        return {'task': self.store.rows('SELECT * FROM tasks WHERE id=?', (tid,))[0]}

    def edit_task(self, tid, body):
        rows = self.store.rows('SELECT * FROM tasks WHERE id=?', (tid,))
        if not rows:
            raise ApiError('Task not found.', 404)
        task = rows[0]
        pid = task['project_id']
        self.ensure_idle(pid)
        values = {}
        for key, limit in [('title', 200), ('description', 20000)]:
            if key in body:
                values[key] = text(body[key], key, limit, key == 'title')
        if 'role' in body:
            if body['role'] not in ROLES:
                raise ApiError('Unknown specialist role.')
            values['role'] = body['role']
        if 'priority' in body:
            values['priority'] = integer(body['priority'], 'Priority', -100, 100)
        if 'depends_on' in body:
            values['depends_on'] = validate_dependencies(self.store, pid, tid, body['depends_on'])
        if 'status' in body:
            status = body['status']
            if status == 'accepted':
                status = 'done'
            if status not in ('queued', 'done', 'blocked', 'review'):
                raise ApiError('Task status must be queued, done, blocked, or review.')
            if status == 'done' and task['status'] not in ('review', 'verified', 'done'):
                raise ApiError('Only completed work can be accepted. Retry unfinished work first.')
            values['status'] = status
            if status == 'queued':
                values['attempts'] = 0
        if not values:
            raise ApiError('No task changes supplied.')
        with self.project_lock(pid):
            if values.get('status') == 'queued' and self.store.project(pid).get('autonomy') and hasattr(self.engine, 'reopen_task'):
                self.engine.reopen_task(pid, tid)
                values.pop('status', None)
                values.pop('attempts', None)
            if values.get('status') == 'done' and task.get('candidate_revision') and task.get('branch'):
                self.engine.integrate_task(pid, tid, manual=True)
                values.pop('status')
            if not values:
                return {'task': self.store.rows('SELECT * FROM tasks WHERE id=?', (tid,))[0]}
            self.store.execute('UPDATE tasks SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), tid))
        self.store.event(pid, 'manager', f"Updated task #{tid}: {values.get('status', task['status'])}")
        return {'task': self.store.rows('SELECT * FROM tasks WHERE id=?', (tid,))[0]}

    def assistant_action(self, pid, action, arguments):
        if not isinstance(arguments, dict):
            raise ApiError('Assistant tool arguments must be an object.')
        selected = arguments.get('project_id') or (pid if pid != '__assistant__' else None)
        if pid != '__assistant__' and selected != pid:
            raise ApiError('Project chat can only act on its own project.')
        if action == 'create_project':
            if pid != '__assistant__':
                raise ApiError('Use the main assistant to create another project.')
            # Credentials and execution commands are always configured directly by the human.
            return self.create_project({key: arguments[key] for key in ('name', 'brief', 'model') if key in arguments})
        if not selected:
            if action == 'status':
                return {'projects': self.store.rows('SELECT id,name,status,brief FROM projects ORDER BY created DESC')}
            raise ApiError('Choose a project_id from the current project list.')
        project = self.store.project(selected)
        if action == 'status':
            return {'project': project, 'tasks': self.store.rows('SELECT * FROM tasks WHERE project_id=? ORDER BY priority DESC,id', (selected,)),
                    'company': self.store.company_context(selected)}
        if action == 'update_project':
            return self.edit_project(selected, {key: arguments[key] for key in ('name', 'brief', 'memory') if key in arguments})
        if action == 'add_task':
            return self.add_task(selected, arguments)
        if action == 'update_task':
            tid = integer(arguments.get('task_id'), 'Task ID', 1, 2**31)
            task = self.store.rows('SELECT project_id FROM tasks WHERE id=?', (tid,))
            if not task or task[0]['project_id'] != selected:
                raise ApiError('Task does not belong to this project.')
            if arguments.get('status') in ('accepted', 'done'):
                raise ApiError('Human acceptance is available through the explicit task review control.')
            return self.edit_task(tid, arguments)
        if action == 'plan':
            self.ensure_idle(selected)
            # A background worker can be yielding while holding its workspace
            # lock. Never wait for that lock from an interactive tool turn.
            with self.project_lock(selected):
                self.engine.plan(selected)
            return {'tasks': self.store.rows('SELECT * FROM tasks WHERE project_id=? ORDER BY id', (selected,))}
        if action in ('start', 'pause'):
            return self.set_status(selected, 'running' if action == 'start' else 'paused')
        if action == 'message':
            return self.company_post(selected, arguments, sender='assistant')
        raise ApiError('Unknown assistant tool.')

    def chat(self, pid, message):
        project = self.store.project(pid) if pid != '__assistant__' else None
        message = text(message, 'Message', 16000, True)
        # Store user input synchronously so refreshing or losing the phone connection
        # cannot erase it while the model is busy.
        def respond():
            history = self.store.rows('SELECT role,content FROM messages WHERE project_id=? ORDER BY id DESC LIMIT 12', (pid,))[::-1]
            context = {'role': 'system', 'content': self.engine.team.prompt('assistant', 'assistant')}
            if project:
                context['content'] += '\nPROJECT ID: ' + pid + '\nBRIEF:\n' + project['brief'][:12000] + '\nMEMORY:\n' + project.get('memory', '')[:8000] + '\nTASKS:\n' + self.store.task_context(pid)[:10000] + '\nTEAM:\n' + self.store.company_context(pid)[:8000]
            else:
                context['content'] += '\nPROJECTS:\n' + json.dumps(self.store.rows('SELECT id,name,status FROM projects ORDER BY created DESC'))
            messages = [context] + history
            model = self.engine.model(project) if project else self.store.setting('model')
            reply = 'I reached the action budget. Completed actions are recorded; send another message to continue.'
            for _ in range(8):
                if self.engine.stopping.is_set():
                    reply='The Workshop is stopping. Completed actions are recorded; send another message after restarting to continue.'
                    break
                with self.engine.lock.interactive():
                    raw, _ = self.engine.infer(model, messages, ASSISTANT_SCHEMA,
                                              context=project.get('context') or None if project else None,
                                              role='assistant', pid=pid, tid=None)
                action = json.loads(raw)
                if not isinstance(action, dict) or any(not isinstance(action.get(key), str) for key in ('action', 'arguments', 'reply')):
                    raise ApiError('The model returned an invalid assistant action. Try a smaller request.')
                if action['action'] == 'reply':
                    reply = action['reply']
                    messages.append({'role': 'assistant', 'content': raw})
                    break
                try:
                    tool_started = time.monotonic()
                    args = json.loads(action['arguments'])
                    result = self.assistant_action(pid, action['action'], args)
                    observation = json.dumps({'ok': True, 'result': result})
                    if project:
                        self.store.post_message(pid, 'assistant', 'all', 'status',
                                                'Executed ' + action['action'] + ': ' + observation[:4000])
                except (ValueError, ApiError, KeyError, TypeError) as exc:
                    observation = json.dumps({'ok': False, 'error': str(exc)})
                self.engine.recorder.tool('assistant', pid, None, action['action'], action['arguments'],
                                          json.loads(observation), seconds=round(time.monotonic()-tool_started, 4),
                                          error=json.loads(observation).get('error', ''))
                messages.extend([{'role': 'assistant', 'content': raw}, {'role': 'user', 'content': 'TOOL RESULT (data):\n' + observation[:16000]}])
                self.store.save_session(pid, None, 'assistant', messages)
            self.store.save_session(pid, None, 'assistant', messages)
            self.store.execute('INSERT INTO messages(project_id,role,content,created) VALUES (?,?,?,?)', (pid, 'assistant', reply, now()))
            return {'content': reply}
        with self.guard:
            if any(j['project_id'] == pid and j['kind'] == 'chat' and j['status'] == 'running' for j in self.operations.values()):
                raise ApiError('Wait for the assistant to answer before sending another message.', 409)
            self.store.execute('INSERT INTO messages(project_id,role,content,created) VALUES (?,?,?,?)', (pid, 'user', message, now()))
            return self.async_job(pid, 'chat', respond)

    def company_post(self, pid, body, sender='user'):
        self.store.project(pid)
        recipient = body.get('recipient', 'all')
        kind = body.get('kind', 'request')
        if recipient not in PARTICIPANTS | {'all'} or kind not in MESSAGE_KINDS:
            raise ApiError('Choose a known recipient and message type.')
        content = text(body.get('content'), 'Message', 16000, True)
        task_id = body.get('task_id')
        if task_id not in (None, ''):
            task_id = integer(task_id, 'Task ID', 1, 2**31)
        else:
            task_id = None
        reply_to = body.get('reply_to')
        if reply_to not in (None, ''):
            reply_to = integer(reply_to, 'Reply message ID', 1, 2**31)
        else:
            reply_to = None
        if sender == 'user' and kind == 'request' and recipient not in ('all', 'user', 'system', 'assistant'):
            with self.guard:
                queued = sum(1 for job in self.operations.values() if job['status'] == 'running' and job['kind'].startswith('company-'))
                if queued >= 4:
                    raise ApiError('Four specialist replies are already queued. Wait for one to finish.', 429)
        mid = self.store.post_message(pid, sender, recipient, kind, content,
                                      task_id=task_id, reply_to=reply_to)
        result = {'message': self.store.rows('SELECT * FROM company_messages WHERE id=?', (mid,))[0]}
        if sender == 'user' and kind == 'request' and recipient not in ('all', 'user', 'system', 'assistant') and hasattr(self.engine, 'company_reply'):
            result.update(self.async_job(pid, 'company-' + str(mid), lambda: self.engine.company_reply(pid, mid)))
        return result

    def health(self):
        if time.monotonic() - self._health_cache[0] > 10:
            self._health_cache = (time.monotonic(), system_health())
        return {'engine': self.engine.health() if hasattr(self.engine, 'health') else {'alive': self.engine.thread.is_alive()},
                'system': self._health_cache[1], 'uptime_seconds': round(time.monotonic()-self.started),
                'projects': self.store.rows('SELECT id,name,status FROM projects'),
                'operations': [dict(v) for v in self.operations.values() if v['status'] == 'running'],
                'backup': self.backups.status(),
                'ollama_url': self.settings()['ollama_url']}

    def execution_history(self, query):
        clauses, args = [], []
        for name in ('project_id', 'role', 'kind'):
            value = query.get(name, [''])[0]
            if value:
                clauses.append(name + '=?')
                args.append(text(value, name, 200, True))
        task = query.get('task_id', [''])[0]
        if task:
            clauses.append('task_id=?')
            args.append(integer(task, 'Task ID', 1, 2**31))
        after = integer(query.get('after', ['0'])[0], 'History cursor', 0, 2**63-1)
        clauses.append('id>?'); args.append(after)
        limit = integer(query.get('limit', ['100'])[0], 'History page size', 1, 100)
        args.append(limit)
        rows = self.store.rows('SELECT id,project_id,task_id,role,kind,created,instructions_fingerprint,session_id FROM execution_history WHERE ' + ' AND '.join(clauses) + ' ORDER BY id LIMIT ?', args)
        return {'entries': rows, 'next_after': rows[-1]['id'] if rows else after, 'limit': limit}

    def idle_status(self):
        return {'settings': {key: value for key, value in self.settings().items() if key.startswith('idle_')},
                'engine': self.engine.health(),
                'approved_projects': self.store.rows('SELECT id,name,status FROM projects WHERE idle_approved=1'),
                'policy': 'User chat first, active projects next, evidence audits while idle, then explicitly approved queued maintenance. Completed projects are not edited automatically.'}

    def approve_idle(self, pid, body):
        project = self.ensure_idle(pid)
        approved = boolean(body.get('approved', False), 'Idle approval')
        if approved:
            if not self.store.rows("SELECT id FROM tasks WHERE project_id=? AND status='queued' LIMIT 1", (pid,)):
                raise ApiError('Queue specific maintenance tasks before approving idle work.')
            if not json.loads(project['checks']):
                raise ApiError('Configure acceptance checks before approving unattended maintenance.')
            if hasattr(self.engine, '_budget') and self.engine._budget(project):
                raise ApiError('Increase the exhausted project budget before approving maintenance.')
        with self.project_lock(pid):
            self.store.execute('UPDATE projects SET idle_approved=? WHERE id=?', (approved, pid))
        self.store.event(pid, 'user', 'Approved queued idle maintenance.' if approved else 'Revoked idle maintenance approval.')
        return {'project': self.store.project(pid)}

    def ensure_team_idle(self):
        """Instruction edits must not change an in-flight project's behaviour."""
        if self.engine.current or self.store.rows("SELECT id FROM projects WHERE status='running' LIMIT 1"):
            raise ApiError('Pause all projects before changing team instructions or skills.', 409)
        if any(operation['status'] == 'running' for operation in self.operations.values()):
            raise ApiError('Wait for active assistant and specialist operations before changing team instructions or skills.', 409)

    def backup(self):
        with self.guard, ExitStack() as locks:
            if any(operation['status'] == 'running' for operation in self.operations.values()):
                raise ApiError('Wait for assistant, planning, and verification operations to finish before backing up.', 409)
            projects = self.store.rows('SELECT id,status FROM projects')
            if any(p['status'] == 'running' for p in projects):
                raise ApiError('Pause all projects before creating a consistent backup.', 409)
            for project in projects:
                locks.enter_context(self.project_lock(project['id']))
            return export_backup(self.store)

    def import_project(self, body):
        entries = validated_import(body.get('archive'))
        result = self.create_project(body)
        pid, folder = result['project']['id'], self.store.folder(result['project']['id'])
        with self.project_lock(pid):
            for name, content in entries:
                path = safe_path(folder, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            git(folder, 'add', '-A')
            git(folder, 'commit', '--allow-empty', '-m', 'Import project source')
        self.store.event(pid, 'user', f'Imported {len(entries)} files from a ZIP archive.')
        return result

    def history(self, pid):
        folder = self.store.folder(pid)
        raw = git(folder, 'log', '-80', '--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s')
        commits = []
        for line in raw.splitlines():
            parts = line.split('\x1f', 4)
            if len(parts) == 5:
                revision, short, author, date, subject = parts
                commits.append(dict(revision=revision, hash=revision, short=short, author=author, date=date, subject=subject))
        return {'commits': commits, 'status': git(folder, 'status', '--short')}

    def revision(self, folder, revision):
        if not isinstance(revision, str) or not re.fullmatch('[a-fA-F0-9]{7,40}', revision):
            raise ApiError('Choose a revision from project history.')
        return git(folder, 'rev-parse', '--verify', revision + '^{commit}')

    def checkpoint(self, pid, message):
        self.ensure_idle(pid)
        with self.project_lock(pid):
            folder = self.store.folder(pid)
            git(folder, 'add', '-A')
            git(folder, 'commit', '--allow-empty', '-m', text(message or 'Manual checkpoint', 'Checkpoint message', 500, True))
            revision = git(folder, 'rev-parse', 'HEAD')
        self.store.event(pid, 'manager', 'Saved checkpoint ' + revision[:8])
        return {'revision': revision}

    def check_project(self, pid):
        """Save the candidate, then attach fresh checks to that exact revision."""
        self.ensure_idle(pid, allow_operation='check')
        with self.project_lock(pid):
            project = self.ensure_idle(pid, allow_operation='check')
            folder = self.store.folder(pid)
            if not json.loads(project['checks']):
                raise ApiError('Configure at least one project check before running verification.')
            git(folder, 'add', '-A')
            if git(folder, 'status', '--porcelain'):
                git(folder, 'commit', '-m', 'Save candidate before manual verification')
            revision = git(folder, 'rev-parse', 'HEAD')
            started = time.monotonic()
            passed, detail = self.engine.checks(project, folder)
            clean = not git(folder, 'status', '--porcelain')
            status = 'verified' if passed and clean else 'review' if passed else 'blocked'
            if passed and not clean:
                detail += '\nChecks changed the workspace. Results do not verify the modified files; inspect changes and run checks again.'
            self.store.execute('INSERT INTO runs(project_id,task_id,model,role,status,seconds,tokens,revision,detail,created) VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (pid, None, self.engine.model(project), 'qa', status, round(time.monotonic()-started, 2), 0, revision, detail, now()))
            self.store.event(pid, 'qa', f'Manual verification of {revision[:12]}: {status}\n{detail}')
            return {'status': status, 'revision': revision, 'detail': detail}

    def restore(self, pid, revision):
        self.ensure_idle(pid)
        with self.project_lock(pid):
            folder = self.store.folder(pid)
            revision = self.revision(folder, revision)
            # Preserve the current tree first. Restore is a new commit, never a
            # history rewrite, and uncommitted work remains recoverable.
            git(folder, 'add', '-A')
            git(folder, 'commit', '--allow-empty', '-m', 'Checkpoint before restore')
            git(folder, 'restore', '--source=' + revision, '--staged', '--worktree', '--', '.')
            git(folder, 'commit', '--allow-empty', '-m', 'Restore files from ' + revision[:12])
            new_revision = git(folder, 'rev-parse', 'HEAD')
            self.store.execute("UPDATE tasks SET status='review', result=result || ? WHERE project_id=? AND status IN ('verified','done')", ('\nProject restored; previous verification is stale. Review or retry.', pid))
            self.store.execute("UPDATE projects SET status='review' WHERE id=?", (pid,))
        self.store.event(pid, 'manager', 'Restored files from ' + revision[:12] + '; previous task verification requires review.')
        return {'revision': new_revision}

    def download(self, pid):
        self.ensure_idle(pid)
        with self.project_lock(pid):
            folder = self.store.folder(pid)
            output = io.BytesIO()
            total = 0
            with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name in files(folder):
                    p = safe_path(folder, name)
                    parts = Path(name).parts
                    if any(part.startswith('.env') and part != '.env.example' for part in parts) or p.suffix.lower() in ('.pem', '.key', '.p12', '.pfx'):
                        continue
                    size = p.stat().st_size
                    total += size
                    if total > 200 * 1024 * 1024:
                        raise ApiError('Project exceeds the 200 MB web download limit. Copy its workspace folder directly.', 413)
                    archive.write(p, name)
                manifest = {'project': self.store.project(pid), 'revision': git(folder, 'rev-parse', 'HEAD'), 'exported': now(),
                            'tasks': self.store.rows('SELECT * FROM tasks WHERE project_id=? ORDER BY id', (pid,))}
                archive.writestr('WORKSHOP-EXPORT.json', json.dumps(manifest, indent=2))
            return output.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = 'TheWorkshop/0.2'

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # Never log passwords, cookies, request bodies, or query strings.
        path = re.sub(r'(/preview/[a-f0-9]{12,16}/)[A-Za-z0-9_-]{32}/', r'\1[preview]/', urlsplit(self.path).path)
        print(f'[{now()}] {self.client_address[0]} {self.command} {path} {args[1] if len(args)>1 else ""}', flush=True)

    def send(self, payload, status=200, content_type='application/json; charset=utf-8', headers=None):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', (headers or {}).get('X-Frame-Options', 'DENY'))
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', (headers or {}).get('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"))
        for key, value in (headers or {}).items():
            if key in ('X-Frame-Options', 'Content-Security-Policy'):
                continue
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def cookie_token(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            return cookies['cabin_session'].value if 'cabin_session' in cookies else ''
        except Exception:
            return ''

    def origin_check(self):
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise ApiError('Cross-site requests are not allowed.', 403)
        origin = self.headers.get('Origin')
        if origin:
            expected = 'http://' + self.headers.get('Host', '')
            if origin != expected:
                raise ApiError('Request origin does not match this control panel.', 403)

    def body(self):
        if self.headers.get('Transfer-Encoding'):
            raise ApiError('Chunked requests are not supported.', 400)
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ApiError('Invalid content length.')
        maximum = 29_000_000 if urlsplit(self.path).path in ('/api/import', '/api/projects/import') else MAX_BODY
        if size < 0 or size > maximum:
            raise ApiError('Request is too large.', 413)
        if self.headers.get_content_type() != 'application/json':
            raise ApiError('Send application/json.', 415)
        try:
            value = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            raise ApiError('Invalid JSON body.')
        if not isinstance(value, dict):
            raise ApiError('JSON body must be an object.')
        return value

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def do_PATCH(self):
        self.handle_request(True)

    def handle_request(self, mutation):
        try:
            if mutation:
                # Keep snapshot backups consistent with every HTTP-side mutation.
                with self.app.guard:
                    self.route(mutation)
            else:
                self.route(mutation)
        except ApiError as exc:
            self.send({'error': str(exc)}, exc.status)
        except (ValueError, KeyError, FileNotFoundError, UnicodeError) as exc:
            self.send({'error': str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.app.store.event(None, 'system', 'HTTP error: ' + repr(exc))
            self.send({'error': 'The operation failed. See Activity or the server log for details.'}, 500)

    def route(self, mutation):
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip('/') or '/'
        query = parse_qs(parsed.query)
        app, store = self.app, self.app.store
        session = app.session(self.cookie_token())
        if not mutation and path == '/api/session':
            return self.send({'authenticated': bool(session), 'csrf': session['csrf'] if session else None})
        if mutation:
            self.origin_check()
            body = self.body()
            if path == '/api/login':
                token, session = app.authenticate(text(body.get('password', ''), 'Password', 500), self.client_address[0])
                return self.send({'csrf': session['csrf']}, headers={'Set-Cookie': f'cabin_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}'})
            if not session:
                raise ApiError('Sign in to your workshop.', 401)
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                raise ApiError('Session token missing or expired. Refresh and sign in again.', 403)
        if path.startswith('/api/') and not session:
            raise ApiError('Sign in to your workshop.', 401)
        if mutation and path == '/api/logout':
            with app.guard:
                app.sessions.pop(self.cookie_token(), None)
            return self.send({'ok': True}, headers={'Set-Cookie': 'cabin_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
        if not mutation and path == '/api/state':
            state = store.snapshot()
            with app.guard:
                state['operations'] = [dict(v) for v in app.operations.values()]
            state.update(settings=app.settings(), csrf=session['csrf'], roles=list(ROLES))
            return self.send(state)
        if not mutation and path == '/api/health':
            return self.send(app.health())
        if not mutation and path == '/api/history':
            return self.send(app.execution_history(query))
        history_entry = re.fullmatch(r'/api/history/(\d+)', path)
        if not mutation and history_entry:
            rows = store.rows('SELECT * FROM execution_history WHERE id=?', (int(history_entry[1]),))
            if not rows:
                raise ApiError('History entry not found.', 404)
            entry = rows[0]
            from .history import payload_of
            entry['payload'] = payload_of(store, entry)
            return self.send({'entry': entry})
        if not mutation and path == '/api/audits':
            status = query.get('status', [''])[0]
            if status and status not in ('open', 'acknowledged', 'dismissed'):
                raise ApiError('Choose open, acknowledged, or dismissed reports.')
            rows = store.rows('SELECT * FROM audit_reports' + (' WHERE status=?' if status else '') + ' ORDER BY id DESC LIMIT 100', (status,) if status else ())
            for row in rows:
                row['evidence_ids'] = json.loads(row['evidence_ids'])
            return self.send({'reports': rows})
        audit_entry = re.fullmatch(r'/api/audits/(\d+)', path)
        if mutation and audit_entry:
            status = body.get('status')
            if status not in ('open', 'acknowledged', 'dismissed'):
                raise ApiError('Choose open, acknowledged, or dismissed.')
            aid = int(audit_entry[1])
            if not store.rows('SELECT id FROM audit_reports WHERE id=?', (aid,)):
                raise ApiError('Audit report not found.', 404)
            store.execute('UPDATE audit_reports SET status=? WHERE id=?', (status, aid))
            return self.send({'ok': True})
        if not mutation and path == '/api/idle':
            return self.send(app.idle_status())
        if not mutation and path == '/api/team':
            catalog = app.engine.team.catalog()
            catalog['current'] = app.engine.current
            for member in catalog.get('members', []):
                member['active'] = ([dict(app.engine.current)] if app.engine.current and app.engine.current.get('role') == member['id'] else [])
            return self.send(catalog)
        team_role = re.fullmatch('/api/team/roles/([a-z]+)', path)
        if mutation and team_role:
            app.ensure_team_idle()
            app.engine.team.save_role(team_role[1], body)
            store.event(None, 'user', 'Updated team identity: ' + team_role[1])
            return self.send(app.engine.team.catalog())
        if mutation and path == '/api/skills':
            app.ensure_team_idle()
            app.engine.team.save_skill(body)
            store.event(None, 'user', 'Updated team skill inventory.')
            return self.send(app.engine.team.catalog())
        if path == '/api/assistant':
            if mutation:
                return self.send(app.chat('__assistant__', body.get('message', '')), 202)
            return self.send({'messages': store.rows("SELECT * FROM messages WHERE project_id='__assistant__' ORDER BY id")})
        if path == '/api/backup':
            return self.send(app.backup(), content_type='application/zip', headers={'Content-Disposition': 'attachment; filename="The-Workshop-backup.zip"'})
        if mutation and path in ('/api/import', '/api/projects/import'):
            return self.send(app.import_project(body), 201)
        notification_match = re.fullmatch(r'/api/notifications/(\d+)', path)
        if mutation and notification_match:
            nid = int(notification_match[1])
            if not store.rows('SELECT id FROM notifications WHERE id=?', (nid,)):
                raise ApiError('Notification not found.', 404)
            store.execute('UPDATE notifications SET seen=? WHERE id=?', (int(bool(body.get('read', True))), nid))
            return self.send({'ok': True})
        if path == '/api/settings':
            if mutation:
                settings = {}
                if 'ollama_url' in body:
                    settings['ollama_url'] = local_url(body['ollama_url'])
                if 'model' in body:
                    settings['model'] = text(body['model'], 'Model', 200)
                if 'context' in body:
                    settings['context'] = str(integer(body['context'], 'Context length', 1024, 131072))
                if 'backup_interval_hours' in body:
                    settings['backup_interval_hours'] = str(integer(body['backup_interval_hours'], 'Backup interval', 0, 168))
                if 'backup_keep' in body:
                    settings['backup_keep'] = str(integer(body['backup_keep'], 'Backup retention', 1, 30))
                for name in ('idle_audit_enabled', 'idle_audit_model_enabled'):
                    if name in body:
                        settings[name] = str(boolean(body[name], name))
                if 'idle_interval_seconds' in body:
                    settings['idle_interval_seconds'] = str(integer(body['idle_interval_seconds'], 'Idle audit interval', 60, 3600))
                with store.db() as db:
                    for key, value in settings.items():
                        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, value))
            return self.send({'settings': app.settings()})
        if not mutation and path == '/api/models':
            return self.send(app.engine.ollama.request('/api/tags'))
        if path == '/api/evaluations':
            if mutation:
                models = body.get('models', [])
                if not isinstance(models, list) or not 1 <= len(models) <= 10 or any(not isinstance(m, str) or not m.strip() or len(m) > 200 for m in models):
                    raise ApiError('Choose between one and ten model names.')
                repeats = integer(body.get('repeats', 1), 'Repeats', 1, 5)
                return self.send({'projects': app.evaluation.start(models, repeats, body.get('benchmark_keys'))}, 202)
            return self.send(app.evaluation.summary())
        if mutation and path == '/api/projects':
            return self.send(app.create_project(body), 201)
        task_match = re.fullmatch(r'/api/tasks/(\d+)', path)
        if mutation and task_match:
            return self.send(app.edit_task(int(task_match[1]), body))
        match = re.fullmatch('/api/projects/([a-f0-9]{12,16})(?:/([a-z-]+))?', path)
        if match:
            pid, action = match.groups()
            project = store.project(pid)
            if not action:
                return self.send(app.edit_project(pid, body) if mutation else {'project': project})
            if mutation:
                if action == 'idle-approval':
                    return self.send(app.approve_idle(pid, body))
                if action in ('start', 'pause', 'status'):
                    return self.send(app.set_status(pid, {'start': 'running', 'pause': 'paused'}.get(action, body.get('status'))))
                if action == 'tasks':
                    return self.send(app.add_task(pid, body), 201)
                if action == 'plan':
                    app.ensure_idle(pid)
                    if not project['brief'].strip():
                        raise ApiError('Write a project brief first.')
                    return self.send(app.async_job(pid, 'plan', lambda: app.engine.plan(pid)), 202)
                if action == 'chat':
                    return self.send(app.chat(pid, body.get('message', body.get('content', ''))), 202)
                if action == 'company':
                    return self.send(app.company_post(pid, body), 201)
                if action == 'screenshot':
                    app.ensure_idle(pid)
                    from .visual import render_html
                    width = integer(body.get('width', 1280), 'Screenshot width', 320, 2560)
                    height = integer(body.get('height', 900), 'Screenshot height', 320, 2560)
                    name = text(body.get('path', 'index.html'), 'HTML file', 500, True)
                    candidate_task = body.get('task_id')
                    if candidate_task is not None:
                        candidate_task = integer(candidate_task, 'Task ID', 1, 2**31)
                        if not store.rows('SELECT id FROM tasks WHERE id=? AND project_id=?', (candidate_task, pid)):
                            raise ApiError('Task not found in this project.', 404)
                    def screenshot():
                        with app.project_lock(pid):
                            app.ensure_idle(pid)
                            folder = app.engine.task_workspace(pid, candidate_task) if candidate_task else store.folder(pid)
                            destination = store.root / 'screenshots' / pid / 'preview.png'
                            render_html(folder, name, destination, width, height)
                            store.event(pid, 'designer', f'Rendered {name} at {width}×{height}.')
                            return {'url': f'/api/projects/{pid}/screenshot', 'width': width, 'height': height}
                    return self.send(app.async_job(pid, 'screenshot', screenshot), 202)
                if action == 'checkpoint':
                    return self.send(app.checkpoint(pid, body.get('message', 'Manual checkpoint')))
                if action == 'check':
                    app.ensure_idle(pid)
                    return self.send(app.async_job(pid, 'check', lambda: app.check_project(pid)), 202)
                if action == 'restore':
                    return self.send(app.restore(pid, body.get('revision', '')))
            else:
                folder = store.folder(pid)
                task_id = query.get('task_id', [''])[0]
                candidate = None
                if task_id:
                    tid = integer(task_id, 'Task ID', 1, 2**31)
                    rows = store.rows('SELECT * FROM tasks WHERE id=? AND project_id=?', (tid, pid))
                    if not rows:
                        raise ApiError('Task not found in this project.', 404)
                    candidate = rows[0]
                    folder = app.engine.task_workspace(pid, tid)
                if action == 'company':
                    return self.send({'messages': store.rows('SELECT * FROM company_messages WHERE project_id=? ORDER BY id DESC LIMIT 300', (pid,))[::-1]})
                if action == 'sessions':
                    return self.send({'sessions': store.rows('SELECT * FROM agent_sessions WHERE project_id=? ORDER BY updated DESC LIMIT 100', (pid,))})
                if action == 'screenshot':
                    image = store.root / 'screenshots' / pid / 'preview.png'
                    if not image.is_file():
                        raise ApiError('Render a screenshot before opening it.', 404)
                    return self.send(image.read_bytes(), content_type='image/png')
                if action == 'messages':
                    return self.send({'messages': store.rows('SELECT * FROM messages WHERE project_id=? ORDER BY id', (pid,))})
                if action == 'files':
                    return self.send({'files': files(folder)})
                if action == 'file':
                    name = query.get('path', [''])[0]
                    p = safe_path(folder, name)
                    if p.stat().st_size > 1024 * 1024:
                        raise ApiError('File is too large to display.', 413)
                    return self.send({'path': name, 'content': p.read_text(encoding='utf-8')})
                if action == 'history':
                    return self.send(app.history(pid))
                if action == 'diff':
                    revision = query.get('revision', [''])[0]
                    if revision:
                        revision = app.revision(folder, revision)
                        diff = git(folder, 'show', '--format=fuller', '--no-ext-diff', '--no-textconv', revision, '--')
                    else:
                        base = candidate['base_revision'] if candidate and candidate['base_revision'] else 'HEAD'
                        diff = git(folder, 'diff', base, '--no-ext-diff', '--no-textconv', '--')
                        untracked = git(folder, 'ls-files', '--others', '--exclude-standard')
                        if untracked:
                            diff += '\n\nNew files not yet checkpointed:\n' + untracked
                    return self.send({'diff': diff[:500000]})
                if action == 'download':
                    return self.send(app.download(pid), content_type='application/zip', headers={'Content-Disposition': f'attachment; filename="workshop-{pid}.zip"'})
        preview = re.fullmatch(r'/preview/([a-f0-9]{12,16})/(.+)', path)
        if not mutation and preview:
            pid, name = preview.groups()
            capability, separator, asset = name.partition('/')
            with app.guard:
                access = app.preview_tokens.get(capability)
            if access and access['project_id'] == pid and access['expires'] > time.time() and app.session(access['session']):
                name = asset
            else:
                if not session:
                    raise ApiError('Sign in before previewing a project.', 401)
                # A sandboxed iframe has an opaque origin: subresources may omit
                # SameSite cookies. Scope an unguessable asset capability to this
                # project and this live login, rather than weakening the sandbox.
                candidate = safe_path(store.folder(pid), unquote(name))
                if not candidate.is_file():
                    raise ApiError('Preview file not found.', 404)
                token = secrets.token_urlsafe(24)
                with app.guard:
                    app.preview_tokens = {key: value for key, value in app.preview_tokens.items() if value['expires'] > time.time() and app.session(value['session'])}
                    if len(app.preview_tokens) >= 100:
                        app.preview_tokens.pop(next(iter(app.preview_tokens)))
                    app.preview_tokens[token] = {'project_id': pid, 'session': self.cookie_token(), 'expires': time.time()+900}
                return self.send(b'', status=302, content_type='text/html', headers={'Location': f'/preview/{pid}/{token}/' + quote(unquote(name), safe='/')})
            folder = store.folder(pid)
            p = safe_path(folder, unquote(name))
            allowed = {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.txt', '.woff', '.woff2', '.ttf', '.otf'}
            if p.suffix.lower() not in allowed or any(part.startswith('.') for part in p.relative_to(folder.resolve()).parts):
                raise ApiError('Only public static assets can be previewed.', 403)
            if p.stat().st_size > 5_000_000:
                raise ApiError('Preview asset exceeds 5 MB.', 413)
            kind = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
            host = self.headers.get('Host', '')
            source = 'http://' + host if re.fullmatch(r'[A-Za-z0-9.:\-\[\]]+', host) else "'self'"
            return self.send(p.read_bytes(), content_type=kind, headers={
                'X-Frame-Options': 'SAMEORIGIN',
                'Content-Security-Policy': f"sandbox allow-scripts; default-src 'none'; script-src {source} 'unsafe-inline'; style-src {source} 'unsafe-inline'; img-src {source} data:; font-src {source}; connect-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"})
        if not mutation and not path.startswith('/api/'):
            name = 'index.html' if path == '/' else unquote(path.lstrip('/'))
            if name.startswith('static/'):
                name = name[len('static/'):]
            file = (STATIC / name).resolve()
            if file.is_relative_to(STATIC.resolve()) and file.is_file():
                return self.send(file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        raise ApiError('Not found.', 404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        super().__init__(address, Handler)
        self.app = app

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(30)
        return sock, address


def serve(data_dir, host='127.0.0.1', port=8080, password=None):
    app = Application(data_dir, password)
    server = Server((host, port), app)
    print(f'\nThe Workshop: http://{host if host != "0.0.0.0" else "localhost"}:{port}', flush=True)
    if host == '0.0.0.0':
        try:
            addresses = sorted({v[4][0] for v in socket.getaddrinfo(socket.gethostname(), port, family=socket.AF_INET) if not v[4][0].startswith('127.')})
            for addr in addresses:
                print(f'Phone / local network: http://{addr}:{port}', flush=True)
        except OSError:
            pass
    if app.generated_password:
        print('Panel password: ' + app.generated_password + '\nKeep this password private. It is shown only when created/reset.', flush=True)
    print('Data directory: ' + str(app.store.root), flush=True)
    print('Press Ctrl+C to stop. Active projects pause on the next start.', flush=True)
    app.engine.thread.start()
    app.backups.thread.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('\nStopping workshop...', flush=True)
    finally:
        app.engine.stopping.set()
        app.backups.stopping.set()
        store = app.store
        store.execute("UPDATE projects SET status='paused' WHERE status='running' AND auto_resume=0")
        server.server_close()
        app.backups.thread.join(timeout=10)
        # Keep the data-directory lock until the worker's current bounded check
        # or model request finishes; do not race a new instance with old writes.
        if app.engine.thread.is_alive():
            print('Waiting for the current bounded worker action to stop safely...', flush=True)
            app.engine.thread.join(timeout=310)
        with app.guard:
            remaining = list(app.job_threads)
        for thread in remaining:
            thread.join(timeout=310)
        if hasattr(app.engine, 'browser'):
            app.engine.browser.close()

