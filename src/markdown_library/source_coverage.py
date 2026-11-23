"""Record that an existing note already covers an upstream chat source.

Only the private source mapping changes: the note body, path and hash stay as
they are. Preview is the default; apply needs the preview token and the note's
current hash, and the mapping is read back after the guarded commit.
"""

import copy
import json
import re

from .core import LibraryError, _hash, _plain

COVERED_TYPE = 'chatgpt-source-first-covered-v1'
_SHA = re.compile(r'[0-9a-f]{64}')
_FIELDS = {'type', 'account_key', 'conversation_id', 'evidence', 'decision', 'actor', 'input_source'}
_EVIDENCE_FIELDS = {'source_message_id', 'role', 'text_sha256'}
# Optional workbench reference fields, kept as given; they do not change identity.
_EVIDENCE_OPTIONAL = {'id', 'primary'}

REQUEST_SCHEMA = {
    'type': COVERED_TYPE,
    'account_key': 'upstream account key (text)',
    'conversation_id': 'upstream conversation id (text)',
    'evidence': [{'source_message_id': 'message id', 'role': 'user | assistant',
                  'text_sha256': '64 lowercase hex: sha256 of the exact message text',
                  'id': 'optional workbench reference such as m001', 'primary': 'optional boolean'}],
    'decision': 'optional: why the current note already covers these messages',
    'actor': 'optional: who decided; default manual-editorial',
    'input_source': 'optional: the upstream source mapping (account, conversation, path, hashes), kept as given',
}


def _text(value, name, limit=500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(c in value for c in '\r\n\x00'):
        raise LibraryError(f'{name} must be nonempty single-line text up to {limit} characters')
    return value


def normalize_source(source):
    """Validate the covered-source request; evidence order does not change identity."""
    source = _plain(source)
    if not isinstance(source, dict) or set(source) - _FIELDS:
        raise LibraryError('Source must be a mapping with only: ' + ', '.join(sorted(_FIELDS)))
    if source.get('type') != COVERED_TYPE:
        raise LibraryError(f'Source type must be {COVERED_TYPE}')
    evidence = source.get('evidence')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 200:
        raise LibraryError('evidence requires 1..200 message references')
    refs = []
    for ref in evidence:
        if not isinstance(ref, dict) or not _EVIDENCE_FIELDS <= set(ref) or set(ref) - _EVIDENCE_FIELDS - _EVIDENCE_OPTIONAL:
            raise LibraryError('Each evidence item requires source_message_id, role and text_sha256 (optional id, primary)')
        if ref['role'] not in ('user', 'assistant') or not isinstance(ref['text_sha256'], str) \
                or not _SHA.fullmatch(ref['text_sha256']):
            raise LibraryError('Evidence role must be user/assistant and text_sha256 64 lowercase hex')
        item = {'source_message_id': _text(ref['source_message_id'], 'source_message_id', 200),
                'role': ref['role'], 'text_sha256': ref['text_sha256']}
        if 'id' in ref:
            item['id'] = _text(ref['id'], 'evidence id', 100)
        if 'primary' in ref:
            if not isinstance(ref['primary'], bool):
                raise LibraryError('evidence primary must be true or false')
            item['primary'] = ref['primary']
        refs.append(item)
    refs.sort(key=lambda r: (r['source_message_id'], r['role'], r['text_sha256']))
    if len({r['source_message_id'] for r in refs}) != len(refs):
        raise LibraryError('Each source message may appear only once in evidence')
    if not any(r['role'] == 'user' for r in refs):
        raise LibraryError('Covered evidence must include at least one of your own (user) messages')
    record = {'type': COVERED_TYPE, 'actor': _text(source.get('actor', 'manual-editorial'), 'actor', 100),
              'account_key': _text(source.get('account_key'), 'account_key', 200),
              'conversation_id': _text(source.get('conversation_id'), 'conversation_id', 200),
              'evidence': refs}
    if 'decision' in source:
        decision = source['decision']
        if not isinstance(decision, str) or not decision.strip() or len(decision) > 2000 or '\x00' in decision:
            raise LibraryError('decision must be nonempty text up to 2000 characters')
        record['decision'] = decision
    if 'input_source' in source:
        upstream = source['input_source']
        if not isinstance(upstream, dict) or len(json.dumps(upstream, ensure_ascii=False)) > 20000:
            raise LibraryError('input_source must be a mapping up to 20000 characters')
        if any(upstream.get(key) not in (None, record[key]) for key in ('account_key', 'conversation_id')):
            raise LibraryError('input_source names a different account or conversation')
        record['input_source'] = upstream
    return record


def source_ref(record):
    evidence = [{key: ref[key] for key in sorted(_EVIDENCE_FIELDS)} for ref in record['evidence']]
    identity = {'account_key': record['account_key'], 'conversation_id': record['conversation_id'], 'evidence': evidence}
    return COVERED_TYPE + ':' + _hash(json.dumps(identity, ensure_ascii=False, sort_keys=True))


def _related(state, record, ref):
    """Other registrations naming the same conversation or the same messages."""
    ids = {r['source_message_id'] for r in record['evidence']}
    digests = {r['text_sha256'] for r in record['evidence']}
    related = []
    for key, row in sorted(state['sources'].items()):
        if key == ref or not isinstance(row, dict):
            continue
        source = row.get('source')
        encoded = json.dumps(source, ensure_ascii=False) if isinstance(source, dict) else ''
        messages = sorted(m for m in ids if m in encoded)
        hashes = sorted(d for d in digests if d in encoded)
        same = isinstance(source, dict) and source.get('conversation_id') == record['conversation_id']
        if same or messages or hashes:
            related.append({'source_ref': key, 'note_id': row.get('id'), 'same_conversation': same,
                            'matched_message_ids': messages, 'matched_text_sha256': hashes})
    return related


def cover_source(library, note_id, source, *, expected_hash, apply=False, expected_token=None):
    from .bootstrap import _index
    if not expected_hash:
        raise LibraryError('cover-source requires the note\'s current --expected-hash')
    if apply and not expected_token:
        raise LibraryError('Apply requires the token returned by a current preview')
    record = normalize_source(source)
    ref = source_ref(record)
    library.validate()
    with library._lock():
        state, notes = _index(library)
        if state.get('rating_scale') != 100:
            raise LibraryError('Legacy rating scale requires explicit migration before library operations')
        note = library._required(notes, note_id)
        if state['notes'].get(note_id, {}).get('deleted'):
            raise LibraryError('Deleted identity reappeared outside recover')
        library._expect(note, expected_hash)
        if note['properties']['kind'] == 'post':
            raise LibraryError('Register source coverage on the private note or Work, not on a Post')
        previous = state['sources'].get(ref)
        if previous is not None:
            if not isinstance(previous, dict) or previous.get('id') != note_id:
                raise LibraryError(f'Source {ref} is already registered to another note: {previous.get("id") if isinstance(previous, dict) else "invalid"}')
        entry = {'id': note_id, 'source': record}
        token = _hash(json.dumps({'version': 1, 'operation': 'cover-source', 'vault': str(library.vault.resolve()),
                                  'note_id': note_id, 'sha256': note['sha256'], 'source_ref': ref,
                                  'entry': entry, 'previous': previous}, ensure_ascii=False, sort_keys=True))
        result = {'id': note_id, 'path': note['path'], 'sha256': note['sha256'],
                  'title': note['properties']['title'], 'source_ref': ref,
                  'record': copy.deepcopy(previous if previous is not None else entry),
                  'outcome': 'existing' if previous is not None else 'new',
                  'related_sources': _related(state, record, ref),
                  'token': token, 'applied': False, 'body_unchanged': True, 'publication_selected': False}
        if not apply:
            return result | {'status': 'preview'}
        if expected_token != token:
            raise LibraryError('Source coverage preview changed; preview again before applying')
        if previous is not None:
            return result | {'status': 'unchanged', 'readback_verified': True}
        state['sources'][ref] = entry
        # Only the private mapping changes, so no Work catalog storage is involved.
        library._commit(state, storage_resolved=True)
        try:
            actual_state, actual_notes = _index(library)
            verified = (actual_state['sources'].get(ref) == entry
                        and actual_notes.get(note_id, {}).get('sha256') == note['sha256'])
        except (OSError, LibraryError):
            verified = False
        return result | {'applied': True, 'status': 'completed' if verified else 'needs_review',
                         'readback_verified': verified}
