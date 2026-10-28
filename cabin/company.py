"""Shared company communication contracts and dependency validation."""
import json
import re

PARTICIPANTS = frozenset({'assistant', 'manager', 'programmer', 'designer', 'debugger',
                          'qa', 'reviewer', 'documentation', 'analyst', 'system', 'user'})
MESSAGE_KINDS = frozenset({'request', 'findings', 'blocker', 'handoff', 'decision', 'status'})


def validate_message(store, pid, sender, recipient, kind, content, task_id=None, revision='', reply_to=None):
    store.project(pid)
    if sender not in PARTICIPANTS or recipient not in PARTICIPANTS | {'all'}:
        raise ValueError('Choose a known company participant or all recipients')
    if kind not in MESSAGE_KINDS:
        raise ValueError('Choose request, findings, blocker, handoff, decision, or status')
    if not isinstance(content, str) or not content.strip() or len(content) > 16000:
        raise ValueError('Company messages require 1–16000 characters')
    if task_id is not None:
        if type(task_id) is not int or not store.rows('SELECT id FROM tasks WHERE id=? AND project_id=?', (task_id, pid)):
            raise ValueError('Referenced task must belong to this project')
    if not isinstance(revision, str) or revision and not re.fullmatch('[a-fA-F0-9]{7,40}', revision):
        raise ValueError('Revision must be a Git commit identifier')
    if reply_to is not None:
        if type(reply_to) is not int or not store.rows('SELECT id FROM company_messages WHERE id=? AND project_id=?', (reply_to, pid)):
            raise ValueError('Reply must reference a message from this project')


def validate_dependencies(store, pid, task_id, depends_on):
    """Validate project membership and reject cycles before editing the graph."""
    if not isinstance(depends_on, list) or len(depends_on) > 100 or any(type(n) is not int for n in depends_on):
        raise ValueError('Dependencies must be a list of task IDs')
    if len(set(depends_on)) != len(depends_on) or task_id in depends_on:
        raise ValueError('Dependencies must be unique and cannot include the task itself')
    tasks = store.rows('SELECT id,depends_on FROM tasks WHERE project_id=?', (pid,))
    graph = {t['id']: json.loads(t['depends_on']) for t in tasks}
    if any(n not in graph for n in depends_on):
        raise ValueError('Dependencies must belong to this project')
    graph[task_id] = depends_on
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            raise ValueError('Task dependencies would create a cycle')
        if node in visited:
            return
        visiting.add(node)
        for parent in graph.get(node, []):
            visit(parent)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)
    return json.dumps(depends_on)
