"""Task worktrees keep incomplete model edits away from accepted project files."""
import uuid
from pathlib import Path


class Workspaces:
    def __init__(self, store, git):
        self.store, self.git = store, git
        self.root = store.root / 'worktrees'
        self.root.mkdir(exist_ok=True)

    def path(self, pid, task_id):
        self.store.project(pid)
        if type(task_id) is not int or task_id < 1:
            raise ValueError('Invalid task id')
        return self.root / pid / str(task_id)

    def prepare(self, project, task):
        main = self.store.folder(project['id'])
        folder = self.path(project['id'], task['id'])
        branch = task.get('branch') or f"workshop/task-{task['id']}-{uuid.uuid4().hex[:8]}"
        if task.get('branch'):
            if not folder.is_dir():
                raise ValueError('Task worktree is missing. Restore its backup before resuming.')
            if not folder.resolve().is_relative_to(self.root.resolve()):
                raise ValueError('Task worktree must remain inside this data directory')
            # Restore portable backups without relying on the original PC path.
            self.git(main, 'worktree', 'repair', str(folder))
            if self.git(folder, 'branch', '--show-current') != branch:
                raise ValueError('Task worktree branch changed; inspect it before resuming.')
            return folder
        if self.git(main, 'status', '--porcelain'):
            raise ValueError('Main workspace has uncommitted changes. Save a checkpoint before starting isolated work.')
        base = self.git(main, 'rev-parse', 'HEAD')
        folder.parent.mkdir(parents=True, exist_ok=True)
        self.git(main, 'worktree', 'add', '-b', branch, str(folder), base)
        self.store.execute('UPDATE tasks SET branch=?,base_revision=? WHERE id=?', (branch, base, task['id']))
        return folder

    def archive(self, project, task):
        folder = self.path(project['id'], task['id'])
        if not task.get('branch') or not folder.is_dir():
            return
        main = self.store.folder(project['id'])
        self.git(main, 'worktree', 'repair', str(folder))
        archive = folder.with_name(folder.name + '-archive-' + uuid.uuid4().hex[:8])
        self.git(main, 'worktree', 'move', str(folder), str(archive))

    def integrate(self, project, task):
        main = self.store.folder(project['id'])
        folder = self.path(project['id'], task['id'])
        candidate = task.get('candidate_revision', '')
        if not candidate or not task.get('branch') or not folder.is_dir():
            raise ValueError('Task has no saved candidate to integrate.')
        self.git(main, 'worktree', 'repair', str(folder))
        if self.git(main, 'status', '--porcelain') or self.git(folder, 'status', '--porcelain'):
            raise ValueError('Save or inspect uncommitted changes before integrating this candidate.')
        if self.git(folder, 'rev-parse', 'HEAD') != candidate:
            raise ValueError('Task files changed since review. Reopen and verify the latest candidate.')
        if self.git(main, 'rev-parse', 'HEAD') != task['base_revision']:
            raise ValueError('Main project advanced after this task started. Rebase/review the candidate before integration.')
        self.git(main, 'merge', '--ff-only', candidate)
        return candidate

    def diff(self, task, folder):
        base = task.get('base_revision')
        if not base:
            return ''
        return self.git(folder, 'diff', '--no-ext-diff', '--no-textconv', base, 'HEAD', '--')[:30000]
