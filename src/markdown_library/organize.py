"""One reviewed request for atomic moves or a recoverable merge, never publication."""
from .core import LibraryError
from .mcp_actions import LibraryActions
from .navigation import after_organization


MOVE_ITEM = {'type': 'object', 'additionalProperties': False,
    'required': ['note_id', 'destination', 'expected_hash'],
    'properties': {name: {'type': 'string', 'minLength': 1}
                   for name in ('note_id', 'destination', 'expected_hash')}}
REQUEST_SCHEMA = {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'oneOf': [
    {'type': 'object', 'additionalProperties': False, 'required': ['operation', 'moves'],
     'properties': {'operation': {'const': 'move'},
                    'moves': {'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': MOVE_ITEM}}},
    {'type': 'object', 'additionalProperties': False,
     'required': ['operation', 'target_id', 'source_ids', 'markdown', 'expected_hashes'],
     'properties': {'operation': {'const': 'merge'}, 'target_id': {'type': 'string', 'minLength': 1},
        'source_ids': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'uniqueItems': True,
                       'items': {'type': 'string', 'minLength': 1}},
        'markdown': {'type': 'string', 'minLength': 1},
        'expected_hashes': {'type': 'object', 'additionalProperties': {'type': 'string'}}}}]}


def organize(library, request, *, apply=False, expected_token=None, operation=None):
    library.validate()
    if not isinstance(request, dict):
        raise LibraryError('Organization request must be a JSON object')
    action = request.get('operation')
    if operation is not None and action != operation:
        raise LibraryError('Request operation does not match the chosen command')
    if apply and not expected_token:
        raise LibraryError('Apply requires the token returned by a current preview')
    actions = LibraryActions(library)
    if action == 'move' and set(request) == {'operation', 'moves'}:
        result = actions.move_many(request['moves'], apply=apply, expected_token=expected_token)
    elif action == 'merge' and set(request) == {'operation', 'target_id', 'source_ids', 'markdown', 'expected_hashes'}:
        if (not isinstance(request['target_id'], str) or not request['target_id']
                or not isinstance(request['expected_hashes'], dict)
                or not isinstance(request['markdown'], str) or not request['markdown'].strip()
                or '\x00' in request['markdown'] or len(request['markdown'].encode('utf-8')) > 512 * 1024
                or not isinstance(request['source_ids'], list) or not 1 <= len(request['source_ids']) <= 20):
            raise LibraryError('Merge requires 1..20 sources and nonempty Markdown up to 512 KiB')
        result = actions.merge(request['target_id'], request['source_ids'], request['markdown'],
                               request['expected_hashes'], apply=apply, expected_token=expected_token)
    else:
        raise LibraryError('Invalid organization request; read --schema for exact fields')
    return after_organization(library, result)
