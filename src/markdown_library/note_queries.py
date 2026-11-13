"""Bounded note and provenance reads; no checkpoint or hidden-state access by callers."""
import json
from pathlib import Path, PurePosixPath
import re
from datetime import datetime, timezone
from hashlib import sha256

from .bootstrap import _index
from .core import LibraryError, _hash


def summary(note):
    return {key: note[key] for key in ('id', 'path', 'sha256')} | {
        'kind': note['properties']['kind'], 'title': note['properties']['title']}


def search_notes(library, query='', *, kind=None, folder=None, limit=20, offset=0):
    library.validate()
    if kind not in (None, 'note', 'work', 'post') or not 1 <= limit <= 50 or offset < 0:
        raise LibraryError('Use kind note/work/post, limit 1..50, and nonnegative offset')
    if len(query) > 500 or (folder is not None and (not folder or '\\' in folder or
            PurePosixPath(folder).is_absolute() or any(p.startswith('.') for p in PurePosixPath(folder).parts))):
        raise LibraryError('Invalid query or visible folder scope')
    prefix = folder.rstrip('/') + '/' if folder else None
    with library._lock():
        notes = sorted(_index(library)[1].values(), key=lambda n: n['path'])
        hits = [n for n in notes if query.casefold() in n['markdown'].casefold()
                and (kind is None or n['properties']['kind'] == kind)
                and (prefix is None or n['path'].startswith(prefix))]
    return {'total': len(hits), 'offset': offset, 'items': [summary(n) for n in hits[offset:offset + limit]],
            'next_offset': offset + limit if offset + limit < len(hits) else None}


def note_sources(library, note_id):
    library.validate()
    with library._lock():
        state, notes = _index(library)
        note = library._required(notes, note_id)
        if state['notes'].get(note_id, {}).get('deleted'):
            raise LibraryError('Deleted identity reappeared outside recover')
        rows = []
        for key, row in state['sources'].items():
            if not isinstance(row, dict):
                raise LibraryError('Invalid source record; preserve state and inspect locally')
            if row.get('id') != note_id:
                continue
            source = row.get('source', {})
            if not isinstance(source, dict):
                raise LibraryError('Invalid source metadata')
            rows.append({'source_ref': key, 'source': {name: source[name] for name in
                ('type', 'item_id', 'job_id', 'conversation_id', 'message_ids', 'source_message_ids') if name in source},
                'original_library_id': row.get('original_library_id'),
                'merge_redirects': row.get('merge_redirects', [])})
        return summary(note) | {'sources': rows}


def source_messages(library, note_id, *, workbench, job_id=None, role=None,
                    offset=0, limit=10, max_chars=20000, text_offset=0):
    provenance = note_sources(library, note_id)
    jobs = sorted({row['source']['job_id'] for row in provenance['sources'] if row['source'].get('job_id')})
    if job_id is None:
        if len(jobs) != 1:
            return {'id': note_id, 'available_jobs': jobs, 'source_available': bool(jobs),
                    'selection_required': len(jobs) > 1, 'messages': []}
        job_id = jobs[0]
    if job_id not in jobs or not re.fullmatch(r'[a-f0-9]{64}', job_id):
        raise LibraryError('Choose a recorded source job for this note')
    if (role not in (None, 'user', 'assistant') or offset < 0 or not 1 <= limit <= 50
            or not 1000 <= max_chars <= 100000 or text_offset < 0 or (text_offset and limit != 1)):
        raise LibraryError('Invalid source message pagination')
    root = Path(workbench)
    if not root.is_absolute() or not root.is_dir():
        raise LibraryError('Source workspace must be an explicit existing absolute directory')
    path = root / 'jobs' / job_id / 'input.json'
    if not path.resolve().is_relative_to(root.resolve()):
        raise LibraryError('Source input escapes the selected workspace')
    if path.stat().st_size > 32 * 1024 * 1024:
        raise LibraryError('Source input exceeds the local read limit')
    raw = path.read_bytes().decode('utf-8')
    material = json.loads(raw)
    source_times = _source_times(material)
    messages = material.get('messages')
    if not isinstance(messages, list) or any(not isinstance(m, dict) or not isinstance(m.get('text'), str) for m in messages):
        raise LibraryError('Source input has no supported recorded-message array')
    filtered = [m for m in messages if role is None or m.get('role') == role]
    items, remaining, continuation = [], max_chars, None
    for index in range(offset, min(len(filtered), offset + limit)):
        message = filtered[index]
        start = text_offset if index == offset else 0
        if start > len(message['text']):
            raise LibraryError('Text offset exceeds the selected message')
        text = message['text'][start:start + remaining]
        truncated = start + len(text) < len(message['text'])
        items.append({name: message[name] for name in ('id', 'role', 'source_message_id') if name in message} |
                     {'text': text, 'text_offset': start, 'text_truncated': truncated,
                      'created_at': source_times['messages'].get(message.get('source_message_id'))})
        remaining -= len(text)
        continuation = {'offset': index, 'text_offset': start + len(text)} if truncated else (
            {'offset': index + 1, 'text_offset': 0} if index + 1 < len(filtered) else None)
        if truncated or remaining == 0:
            break
    return {'id': note_id, 'job_id': job_id, 'artifact': 'recorded-workbench-input', 'input_sha256': _hash(raw),
            'original_export_reverified': False, 'total': len(filtered), 'offset': offset,
            'source_timestamps': {k: v for k, v in source_times.items() if k != 'messages'},
            'evidence_message_ids': material.get('personal_evidence_ids', []), 'messages': items, 'next': continuation}


def _source_times(material):
    """Read timestamps only from the recorded package after its byte-hash check."""
    result = {'status': 'unavailable', 'messages': {}}
    source = material.get('source', {})
    if not isinstance(source, dict):
        return result
    location, digest = source.get('source_path'), source.get('byte_sha256')
    if not isinstance(location, str) or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        return result
    path = Path(location)
    if not path.is_absolute():
        return result
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            return result
        content = path.read_bytes()
        if sha256(content).hexdigest() != digest:
            return result | {'status': 'source_hash_mismatch'}
        package = json.loads(content)
        raw = package.get('raw', {})
        if not isinstance(raw, dict) or not isinstance(raw.get('mapping'), dict):
            return result
        identity = raw.get('id', raw.get('conversation_id'))
        if identity is not None and identity != source.get('conversation_id'):
            return result | {'status': 'conversation_mismatch'}
        wanted = {m.get('source_message_id') for m in material.get('messages', []) if isinstance(m, dict)}
        def stamp(value):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    return datetime.fromtimestamp(value, timezone.utc).isoformat()
                except (ValueError, OverflowError, OSError):
                    pass
            return None
        times = {}
        for node in raw['mapping'].values():
            message = node.get('message') if isinstance(node, dict) else None
            if isinstance(message, dict) and message.get('id') in wanted:
                times[message['id']] = stamp(message.get('create_time'))
        return {'status': 'recorded_package_hash_verified', 'conversation_created_at': stamp(raw.get('create_time')),
                'conversation_updated_at': stamp(raw.get('update_time')), 'messages': times}
    except (OSError, ValueError, TypeError, AttributeError):
        return result
