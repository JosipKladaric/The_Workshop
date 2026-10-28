"""Durable, append-only execution evidence, independent of conversation windows."""
import hashlib
import json
import uuid


class Recorder:
    def __init__(self, store, session_id=None):
        self.store = store
        self.session_id = session_id or uuid.uuid4().hex
        self._configuration = None
        self._instruction_fingerprint = ''

    def _fingerprint(self):
        from .team import Team
        configuration = (self.store.setting('team.roles','{}'), self.store.setting('team.skills','{}'))
        if configuration != self._configuration:
            self._instruction_fingerprint = Team(self.store).fingerprint()
            self._configuration = configuration
        return self._instruction_fingerprint

    def _append(self, role, pid, tid, kind, payload):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        # Large image-bearing requests stay complete without bloating SQLite rows.
        # The content-addressed blob is part of the data directory and its backups.
        if len(encoded) > 8 * 1024 * 1024:
            root = self.store.root.resolve()
            directory = root / 'history_blobs'
            directory.mkdir(exist_ok=True)
            if directory.is_symlink() or not directory.resolve().is_relative_to(root):
                raise ValueError('History blob storage must remain inside the data directory')
            digest = hashlib.sha256(encoded).hexdigest()
            target = directory / (digest + '.json')
            if target.is_symlink():
                raise ValueError('History blob cannot be a symbolic link')
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write(encoded)
            payload = {'blob': 'history_blobs/' + target.name, 'sha256': digest,
                       'bytes': len(encoded), 'format': 'json', 'complete': True}
        return self.store.append_history(pid, tid, role, kind, payload,
                                         fingerprint=self._fingerprint(), session_id=self.session_id)

    def model(self, role, pid, tid, messages, response, model, tokens, seconds, error=''):
        return self._append(role, pid, tid, 'model', dict(messages=messages, response=response,
                            model=model, tokens=tokens, seconds=seconds, error=str(error)))

    def tool(self, role, pid, tid, action, args, result, revision='', seconds=0, error=''):
        return self._append(role, pid, tid, 'tool', dict(action=action, arguments=args, result=result,
                            revision=revision, seconds=seconds, error=str(error)))

    def event(self, role, pid, tid, name, detail):
        return self._append(role, pid, tid, 'event', dict(name=name, detail=detail))


def payload_of(store, row):
    """Read complete evidence and verify any external payload against its hash."""
    payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
    if isinstance(payload, dict) and payload.get('blob'):
        path = (store.root / payload['blob']).resolve()
        directory = (store.root / 'history_blobs').resolve()
        if not path.is_relative_to(directory) or path.suffix != '.json':
            raise ValueError('Invalid history blob reference')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != payload.get('sha256'):
            raise ValueError('History blob integrity check failed')
        return json.loads(data)
    return payload
