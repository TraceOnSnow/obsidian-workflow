"""Guarded native navigation refresh shared by local and MCP operations."""
import importlib.util
import json
from pathlib import Path, PurePosixPath

from .bootstrap import _index
from .core import LibraryError, _hash


def refresh_navigation(library, *, apply=True, expected_token=None, scopes=None):
    library.validate()
    script = Path(__file__).resolve().parents[2] / 'scripts/library-navigation.py'
    spec = importlib.util.spec_from_file_location('library_topic_navigation', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    selected = set()
    for scope in scopes or []:
        path = PurePosixPath(scope)
        if path.is_absolute() or not path.parts or not scope or '\\' in scope or any(p.startswith('.') for p in path.parts):
            raise LibraryError('Navigation scopes must be visible relative folders')
        selected.update(str(parent) for parent in [path, *path.parents] if str(parent) != '.')
    with library._lock():
        state, notes = _index(library)
        outputs = module.generate(library.vault, list(notes.values()))
        writes, preserved = [], []
        for relative, content in outputs.items():
            folder = str(PurePosixPath(relative).parent)
            if selected and folder not in selected:
                continue
            if relative.startswith('Blogs/') and not (library.vault / 'Blogs').exists():
                continue
            target = library._path(relative)
            previous = target.read_text() if target.exists() else None
            if previous == content:
                continue
            if previous is not None and (relative.endswith('.base') or not previous.startswith(module.MARKER)):
                preserved.append(relative)
                continue
            writes.append((relative, content, _hash(previous) if previous is not None else None))
        plan = {'vault': str(library.vault.resolve()), 'generator': _hash(script.read_text()),
                'writes': writes, 'preserved': preserved, 'scopes': sorted(selected)}
        token = _hash(json.dumps(plan, sort_keys=True, ensure_ascii=False))
        if not apply:
            return {'applied': False, 'state': 'preview', 'token': token,
                    'changed': [p for p, _, _ in writes], 'preserved': preserved}
        if expected_token is not None and token != expected_token:
            raise LibraryError('Navigation preview changed; preview again before applying')
        if writes:
            library._commit(state, writes)
        verified = all(library._path(p).read_text() == text for p, text, _ in writes)
        return {'applied': True, 'state': 'refreshed' if verified else 'needs_review',
                'changed': [p for p, _, _ in writes], 'preserved': preserved, 'readback_verified': verified}


def after_organization(library, result):
    """A saved operation must remain distinguishable from a failed directory refresh."""
    if not result.get('applied') or not result.get('readback_verified'):
        return result
    scopes = None
    if 'moves' in result:
        scopes = sorted({str(PurePosixPath(row[key]).parent)
                         for row in result['moves'] for key in ('previous_path', 'path')})
    else:
        scopes = sorted({str(PurePosixPath(result['path']).parent),
                         *(str(PurePosixPath(row['path']).parent) for row in result.get('sources', []))})
    try:
        navigation = refresh_navigation(library, scopes=scopes)
    except (LibraryError, OSError, ValueError, ImportError):
        navigation = {'state': 'needs_refresh', 'message':
                      'Content saved; use the navigation command to preview and retry only directory refresh.'}
    return result | {'navigation': navigation}
