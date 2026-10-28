"""Exact, bounded replacement patches that reject ambiguous or stale edits."""
import hashlib
import json
from .engine import safe_path


def patch_file(folder, name, specification):
    if isinstance(specification, str):
        try:
            specification = json.loads(specification)
        except ValueError as exc:
            raise ValueError('Patch content must be JSON {old,new,sha256?}') from exc
    if not isinstance(specification, dict):
        raise ValueError('Patch content must be an object')
    old, new = specification.get('old'), specification.get('new')
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise ValueError('Patch requires nonempty old text and new replacement text')
    path = safe_path(folder, name)
    if not path.is_file() or path.stat().st_size > 500000:
        raise ValueError('Patch requires an existing file smaller than 500 KB')
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if specification.get('sha256') and specification['sha256'] != digest:
        raise ValueError('Patch rejected: file changed since it was read')
    text = raw.decode('utf-8')
    if text.count(old) != 1:
        raise ValueError('Patch rejected: old text must match exactly one location')
    result = text.replace(old, new, 1)
    if len(result.encode('utf-8')) > 500000:
        raise ValueError('Patched file exceeds 500 KB')
    temporary = path.with_name(path.name+'.workshop-patch')
    temporary.write_bytes(result.encode('utf-8'))
    temporary.replace(path)
    return dict(path=name,previous_sha256=digest,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                message='Applied one exact replacement')
