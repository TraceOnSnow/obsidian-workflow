"""Review and distribute maintained library instructions without losing local edits."""

from hashlib import sha256
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re

from .core import LibraryError

STATE = '.markdown-library/maintenance.json'


def _digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def _source(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts:
        raise LibraryError('Maintenance sources must stay inside the project')
    resolved = (root / relative).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise LibraryError('Maintenance source is missing or outside the project: ' + relative)
    return resolved.read_text(encoding='utf-8')


def _render(root, library, entry):
    text = _source(root, entry['source'])
    mode = entry.get('render', 'copy')
    if mode == 'copy':
        return text
    if mode == 'policy':
        # The operational policy is authoritative; historical batches and server
        # implementation notes are not part of the offline writing instructions.
        text = text.split('\n## 公开身份', 1)[0]
        text = re.sub(r'\[([^\]]+)\]\((?!https?://)[^)]+\)', r'\1', text)
        return ('<!-- markdown-library:policy-projection:v1 -->\n'
                '<!-- Generated from the current editorial policy; edit the source through maintenance. -->\n'
                + text.rstrip() + '\n')
    if mode in {'index', 'navigation'}:
        spec = importlib.util.spec_from_file_location('library_maintenance_index', root / entry['source'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if mode == 'index':
            outputs, _ = module.generate(library)
        else:
            from .bootstrap import _index
            _, notes = _index(library)
            outputs = module.generate(library.vault, list(notes.values()))
        if entry['target'] not in outputs:
            raise LibraryError('Unknown generated index target')
        return outputs[entry['target']]
    raise LibraryError('Unknown maintenance renderer: ' + mode)


def maintain(library, *, project, manifest, apply=False, expected_token=None, accept_reviewed=False):
    """Preview is read-only. Apply requires the exact preview and explicit review.

    Only unchanged installed copies are safe to replace automatically. Different
    existing copies need an explicit reviewed adoption/merge, never a force sync.
    """
    library.validate()
    root = Path(project).resolve()
    config_text = _source(root, manifest)
    try:
        config = json.loads(config_text)
    except ValueError as exc:
        raise LibraryError('Invalid maintenance manifest') from exc
    if config.get('version') != 1 or not isinstance(config.get('files'), list):
        raise LibraryError('Unsupported maintenance manifest')
    with library._lock():
        state_path = library._path(STATE)
        previous_text = state_path.read_text() if state_path.exists() else None
        previous = json.loads(previous_text) if previous_text else {}
        if previous and (previous.get('version') != 1 or previous.get('manifest') != manifest):
            raise LibraryError('Maintenance baseline belongs to another manifest')
        baseline = previous.get('files', {})
        rows, writes, next_files = [], [], {}
        targets = set()
        for entry in config['files']:
            relative = entry['target']
            # Maintenance is restricted to utility documents and templates,
            # never regular notes, Works, Posts or Obsidian credentials/settings.
            fixed = {'首页.md', '笔记目录.md', '网站内容.md', '使用说明.md', 'AGENTS.md', 'CLAUDE.md',
                     'Copilot库内助手.md', '发表稿/索引.md', '00_收件箱/索引.md', '05_想法池/索引.md', '25_游戏设计/索引.md',
                     'config/editorial-workflow.json'}
            if relative not in fixed and not relative.startswith(('templates/', 'config/', 'copilot/skills/personal-library/')):
                raise LibraryError('Not a maintained utility target: ' + relative)
            if relative in targets or not (relative.endswith('.md') or relative == 'config/editorial-workflow.json'):
                raise LibraryError('Duplicate or invalid maintenance target')
            targets.add(relative)
            target = library._path(relative)
            current = target.read_text() if target.exists() else None
            desired = _render(root, library, entry)
            current_hash = _digest(current) if current is not None else None
            desired_hash = _digest(desired)
            old = baseline.get(relative)
            if current == desired:
                status = 'current'
            elif current is None and old is None:
                status = 'create'
            elif old and current_hash == old.get('installed_hash'):
                status = 'update'
            else:
                status = 'review-required'
            rows.append({'target': relative, 'source': entry['source'], 'status': status,
                         'current_hash': current_hash, 'desired_hash': desired_hash})
            if current != desired:
                if current is not None:
                    writes.append(('.markdown-library/maintenance-history/' + current_hash + '.md', current, None))
                writes.append((relative, desired, current_hash))
            next_files[relative] = {'installed_hash': desired_hash, 'source': entry['source']}
        groups, next_groups = [], {}
        for group in config.get('groups', []):
            name = group['name']
            hashes = {p: _digest(_source(root, p)) for p in group['sources']}
            if hashes != previous.get('groups', {}).get(name):
                groups.append({'name': name, 'sources': group['sources'], 'hashes': hashes,
                               'review': group.get('review', []), 'checks': group.get('checks', [])})
            next_groups[name] = hashes
        pending_path = library._path('config/规则变更待同步.md')
        pending = pending_path.read_text() if pending_path.exists() else ''
        plan = {'vault': str(library.vault.resolve()), 'manifest_hash': _digest(config_text), 'baseline_hash': _digest(previous_text or ''),
                'files': rows, 'impact_review': groups, 'pending_feedback_hash': _digest(pending)}
        token = _digest(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        needs_review = any(r['status'] == 'review-required' for r in rows)
        if apply:
            if token != expected_token:
                raise LibraryError('Maintenance preview changed; read and review the current plan again')
            if needs_review and not accept_reviewed:
                raise LibraryError('Existing edits retained; merge/review sources and explicitly accept the current preview')
            next_state = json.dumps({'version': 1, 'manifest': manifest, 'files': next_files,
                                     'groups': next_groups}, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
            if next_state != previous_text:
                writes.append((STATE, next_state, _digest(previous_text) if previous_text is not None else None))
            # Re-render before committing: a source edit during review/render
            # cannot be silently recorded as a completed propagation.
            if _source(root, manifest) != config_text or any(
                    _digest(_render(root, library, entry)) != next_files[entry['target']]['installed_hash']
                    for entry in config['files']) or any(
                    _digest(_source(root, source)) != digest
                    for group in next_groups.values() for source, digest in group.items()):
                raise LibraryError('Maintenance sources changed during apply; preview again')
            if writes:
                library._commit(library._load(), writes)
        return {'applied': apply, 'token': token, 'files': rows, 'impact_review': groups,
                'pending_feedback': bool(pending.strip()),
                'current': (apply or (not writes and not groups)) and not pending.strip(),
                'client_loaded_verified': False, 'model_calls': 0, 'remote_writes': False}
