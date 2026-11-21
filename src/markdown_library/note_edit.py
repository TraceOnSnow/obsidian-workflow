"""Exact text edits on one managed note, optionally followed by a guarded rename.

Each edit names text copied from the current note and how many times it must
occur, so an edit only lands on the version it was written against. The whole
replacement still goes through Library.replace, and the optional rename through
the batch move that repairs links.
"""
from pathlib import PurePosixPath

from .core import LibraryError

REQUEST_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object', 'additionalProperties': False, 'required': ['edits'],
    'properties': {
        'expected_hash': {'type': 'string', 'minLength': 1,
                          'description': 'Optional; when omitted the exact old texts and counts are the guard'},
        'edits': {'type': 'array', 'maxItems': 64, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['old', 'new'],
            'properties': {'old': {'type': 'string', 'minLength': 1}, 'new': {'type': 'string'},
                           'count': {'type': 'integer', 'minimum': 1, 'default': 1}}}},
        'rename': {'type': 'object', 'additionalProperties': False, 'required': ['destination'],
                   'properties': {'destination': {'type': 'string', 'minLength': 1}}},
    },
}


def apply_edits(markdown, edits):
    """Apply edits in order; every old text must occur exactly `count` times."""
    if not isinstance(edits, list) or len(edits) > 64:
        raise LibraryError('edits must be a list of at most 64 items')
    for index, edit in enumerate(edits, 1):
        if (not isinstance(edit, dict) or set(edit) - {'old', 'new', 'count'} or not {'old', 'new'} <= set(edit)
                or not isinstance(edit['old'], str) or not edit['old'] or not isinstance(edit['new'], str)):
            raise LibraryError(f'Edit {index} requires nonempty old text and new text')
        count = edit.get('count', 1)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise LibraryError(f'Edit {index} count must be a positive integer')
        found = markdown.count(edit['old'])
        if found != count:
            raise LibraryError(f'Edit {index}: old text occurs {found} times, expected {count}: {edit["old"][:80]!r}')
        markdown = markdown.replace(edit['old'], edit['new'])
    return markdown


def edit_note(library, note_id, request, *, apply=False):
    if not isinstance(request, dict) or set(request) - {'edits', 'expected_hash', 'rename'} or 'edits' not in request:
        raise LibraryError('Edit request requires edits, with optional expected_hash and rename')
    rename = request.get('rename')
    if rename is not None and (not isinstance(rename, dict) or set(rename) != {'destination'}
                               or not isinstance(rename['destination'], str)):
        raise LibraryError('rename requires only a destination path')
    if not request['edits'] and rename is None:
        raise LibraryError('Nothing to change')
    note = library.get(note_id)
    expected = request.get('expected_hash') or note['sha256']
    if expected != note['sha256']:
        raise LibraryError('Revision conflict: expected SHA256 does not match current file')
    updated = apply_edits(note['markdown'], request['edits'])
    result = {'id': note_id, 'path': note['path'], 'previous_sha256': note['sha256'],
              'changed': updated != note['markdown'], 'applied': apply}
    if rename is not None:
        destination = rename['destination']
        if not destination.endswith('.md'):
            destination += '.md'
        if '/' not in destination:
            destination = str(PurePosixPath(note['path']).parent / destination)
        result['destination'] = destination
    if not apply:
        import difflib
        diff = list(difflib.unified_diff(note['markdown'].splitlines(), updated.splitlines(),
                                         lineterm='', n=0))[2:]
        return result | {'status': 'preview', 'diff': diff[:200], 'diff_truncated': len(diff) > 200}
    if result['changed']:
        saved = library.replace(note_id, updated, expected_hash=note['sha256'])
        result['sha256'] = saved['sha256']
    else:
        result['sha256'] = note['sha256']
    if rename is not None and result['destination'] != note['path']:
        from .organize import organize
        move = {'operation': 'move', 'moves': [
            {'note_id': note_id, 'destination': result['destination'], 'expected_hash': result['sha256']}]}
        preview = organize(library, move)
        moved = organize(library, move, apply=True, expected_token=preview['token'])
        row = moved['moves'][0]
        result.update(path=row['path'], sha256=row['sha256'], links_updated=moved.get('links_updated'))
    return result | {'status': 'applied'}
