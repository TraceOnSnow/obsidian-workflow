"""Bounded note and provenance reads; no checkpoint or hidden-state access by callers."""
import copy
import json
import math
from pathlib import Path, PurePosixPath
import re
from datetime import datetime, timezone
from hashlib import sha256

from .bootstrap import _index
from .core import LibraryError, _hash, _split


def summary(note):
    return {key: note[key] for key in ('id', 'path', 'sha256')} | {
        'kind': note['properties']['kind'], 'title': note['properties']['title']}


MAX_QUERIES = 10


def search_notes(library, query='', *, kind=None, folder=None, limit=20, offset=0, queries=None):
    """One query returns a page; ``queries`` runs up to MAX_QUERIES separate
    searches over a single index read and lock, one result block per query."""
    library.validate()
    if kind not in (None, 'note', 'work', 'post') or not 1 <= limit <= 50 or offset < 0:
        raise LibraryError('Use kind note/work/post, limit 1..50, and nonnegative offset')
    if queries is not None and (query or not 1 <= len(queries) <= MAX_QUERIES
                                or not all(isinstance(q, str) for q in queries)):
        raise LibraryError(f'Pass either query or 1..{MAX_QUERIES} queries')
    if any(len(q) > 500 for q in (queries or [query])) or (folder is not None and (not folder or '\\' in folder or
            PurePosixPath(folder).is_absolute() or any(p.startswith('.') for p in PurePosixPath(folder).parts))):
        raise LibraryError('Invalid query or visible folder scope')
    prefix = folder.rstrip('/') + '/' if folder else None
    with library._lock():
        notes = sorted(_index(library)[1].values(), key=lambda n: n['path'])
        scoped = [n for n in notes if (kind is None or n['properties']['kind'] == kind)
                  and (prefix is None or n['path'].startswith(prefix))]
        texts = None
        results = []
        for text in (queries if queries is not None else [query]):
            terms = list(dict.fromkeys(text.casefold().split()))
            if terms and texts is None:
                texts = {n['id']: n['markdown'].casefold() for n in notes}
            hits = [n for n in scoped if all(term in texts[n['id']] for term in terms)]
            if terms:
                # Every managed note is already in memory for this read, so rank in
                # place instead of keeping a derived index that could go stale.
                scores = _relevance(notes, texts, terms)
                hits.sort(key=lambda n: (-scores[n['id']], n['path']))
            results.append({'total': len(hits), 'offset': offset,
                'items': [summary(n) | ({'snippet': _snippet(n['markdown'], terms)} if terms else {})
                          for n in hits[offset:offset + limit]],
                'next_offset': offset + limit if offset + limit < len(hits) else None})
    if queries is None:
        return results[0]
    return {'results': [{'query': q} | r for q, r in zip(queries, results)]}


MAX_BATCH_NOTES = 20
MAX_BATCH_BYTES = 1024 * 1024


def get_notes(library, note_ids):
    """Full current drafts for several stable IDs under one lock. Unknown IDs are
    reported per item; notes beyond the byte budget are listed for a later call."""
    if not isinstance(note_ids, list) or not 1 <= len(note_ids) <= MAX_BATCH_NOTES:
        raise LibraryError(f'Pass 1..{MAX_BATCH_NOTES} note IDs')
    ids = list(dict.fromkeys(note_ids))
    with library._lock():
        # Same logical view as Library.get (Works merge table fields), one scan.
        state = library._load()
        index = library._scan(state)
        library._save(state)
        notes, missing, deferred, used = [], [], [], 0
        for note_id in ids:
            note = index.get(note_id) if isinstance(note_id, str) else None
            if note is None:
                missing.append(note_id)
                continue
            size = len(note['markdown'].encode('utf-8'))
            if notes and used + size > MAX_BATCH_BYTES:
                deferred.append(note_id)
                continue
            used += size
            notes.append(copy.deepcopy(note))
    return {'notes': notes, 'missing': missing, 'deferred': deferred}


def _relevance(notes, texts, terms, *, k=1.2, b=0.75, title_weight=3.0):
    """BM25 over substring counts, so Chinese needs no word segmentation."""
    average = sum(len(text) for text in texts.values()) / max(len(texts), 1) or 1
    scores = dict.fromkeys(texts, 0.0)
    for term in terms:
        containing = sum(term in text for text in texts.values())
        rarity = math.log(1 + (len(texts) - containing + 0.5) / (containing + 0.5))
        for note in notes:
            text = texts[note['id']]
            count = text.count(term)
            if not count:
                continue
            frequency = count * (k + 1) / (count + k * (1 - b + b * len(text) / average))
            title = title_weight if term in str(note['properties']['title']).casefold() else 0.0
            scores[note['id']] += rarity * (frequency + title)
    return scores


def _snippet(markdown, terms, *, radius=60):
    """A short single-line excerpt around the earliest term in the body."""
    try:
        body = _split(markdown)[1]
    except Exception:
        body = markdown
    folded = body.casefold()
    found = [(folded.find(term), len(term)) for term in terms if term in folded]
    if not found or len(folded) != len(body):
        return ' '.join(body.split())[:2 * radius]
    start, length = min(found)
    left, right = max(0, start - radius), min(len(body), start + length + radius)
    return ('…' if left else '') + ' '.join(body[left:right].split()) + ('…' if right < len(body) else '')


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
