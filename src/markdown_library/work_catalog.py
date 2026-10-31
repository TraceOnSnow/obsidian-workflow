"""Opt-in Super Table storage for Works, with ordinary Markdown review bodies.

The table is the editable source of personal fields.  Retained configuration holds
only lossless representation details (including additional experience rows), not
another set of current Work files.  Logical revisions remain per Work; physical
transactions compare and swap the complete table and any affected review files.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
import re

from .core import LibraryError, _hash, _read, _render, _split
from .ratings import validate_rating


DEFAULT_PATH = 'works/作品总表.md'
TABLE_ID = 'personal-works'


def _today(config):
    return datetime.now(ZoneInfo(config.get('default_timezone', 'America/Chicago'))).date().isoformat()


def creation_defaults(properties, config):
    if config.get('default_status'):
        properties.setdefault('status', config['default_status'])
    if config.get('default_end_today') and 'experiences' not in properties:
        properties['experiences'] = [{'endedOn': _today(config)}]


DAILY_FIELDS = frozenset({'title', 'type', 'rating', 'status', 'note', 'experiences'})
SCALAR_FIELDS = ('title', 'type', 'rating', 'status', 'note')
FIELD_TYPES = {'title': 'text', 'type': 'text', 'rating': 'number', 'status': 'text',
               'note': 'text', 'startedOn': 'text', 'endedOn': 'text', 'review': 'text'}
FIELD_NAMES = {'title': '作品', 'type': '类型', 'rating': '评分', 'status': '状态',
               'note': '短评', 'startedOn': '开始日期', 'endedOn': '结束日期', 'review': '正文'}
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_FENCE = re.compile(r'^([ \t]*`{3,})super-table[ \t]*\r?\n', re.MULTILINE)


@dataclass
class Catalog:
    document: dict
    prefix: str = '# 作品总表\n\n'
    suffix: str = '\n'
    opening: str = '```super-table\n'
    closing: str = '```'


def enabled(state):
    if 'work_catalog' not in state:
        return False
    config = state['work_catalog']
    if (not isinstance(config, dict) or type(config.get('schema_version')) is not int
            or config['schema_version'] != 1 or not isinstance(config.get('path'), str)
            or not isinstance(config.get('rows'), dict)):
        raise LibraryError('Invalid Work catalog configuration; preserve state and review')
    return True


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def _valid_date(value):
    if value is None or value == '':
        return
    if isinstance(value, str) and 'T' in value:
        try:
            datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise LibraryError('Work timestamp must be valid ISO text') from exc
        return
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}(?:-\d{2})?(?:-\d{2})?', value):
        raise LibraryError('Work dates must be ISO dates (YYYY, YYYY-MM or YYYY-MM-DD), or empty')
    try:
        date.fromisoformat(value + ('-01-01' if len(value) == 4 else '-01' if len(value) == 7 else ''))
    except ValueError as exc:
        raise LibraryError('Invalid Work calendar date') from exc


def _validate_row(row):
    if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not _ID.fullmatch(row['id']):
        raise LibraryError('Work catalog rows require safe stable IDs')
    values = row.get('values')
    if not isinstance(values, dict) or not FIELD_TYPES.keys() <= values.keys():
        raise LibraryError('Work catalog row is missing required cells')
    if not isinstance(values['title'], str):
        raise LibraryError('Work catalog title must be text')
    for name in ('type', 'status', 'note', 'review'):
        if values[name] is not None and not isinstance(values[name], str):
            raise LibraryError(f'Work catalog {name} must be text or null')
    # The native number editor stores a cleared cell as "".  Keep that source
    # representation intact while projecting it as the ordinary unknown rating.
    validate_rating(None if values['rating'] == '' else values['rating'])
    for name in ('startedOn', 'endedOn'):
        _valid_date(values[name])
    _review_path(values['review'])


def _validate_document(document):
    if (not isinstance(document, dict) or type(document.get('schemaVersion')) is not int
            or document['schemaVersion'] != 1 or document.get('tableId') != TABLE_ID):
        raise LibraryError('Unsupported Work catalog Super Table schema or identity')
    for key in ('fields', 'records', 'views'):
        if not isinstance(document.get(key), list):
            raise LibraryError(f'Work catalog {key} must be a list')
    fields = {}
    for field in document['fields']:
        if (not isinstance(field, dict) or not isinstance(field.get('id'), str)
                or field['id'] in fields):
            raise LibraryError('Duplicate or invalid Work catalog field')
        fields[field['id']] = field
    if not FIELD_TYPES.keys() <= fields.keys():
        raise LibraryError('Work catalog required columns were removed')
    for name, kind in FIELD_TYPES.items():
        # Native single-select controls store their label as a string too.
        allowed = {kind} | ({'select', 'status'} if name in {'type', 'status'} else set())
        if name in {'startedOn', 'endedOn'}:
            allowed.add('date')
        if fields[name].get('type') not in allowed:
            raise LibraryError(f'Work catalog column {name} has an incompatible type')
    ids = set()
    for row in document['records']:
        _validate_row(row)
        if row['id'] in ids:
            raise LibraryError('Duplicate Work catalog stable ID: ' + row['id'])
        ids.add(row['id'])
    views = set()
    for view in document['views']:
        if (not isinstance(view, dict) or not isinstance(view.get('id'), str)
                or view['id'] in views or not isinstance(view.get('name'), str)
                or not view['name'].strip()):
            raise LibraryError('Duplicate or invalid Work catalog view')
        filters = view.get('filters')
        if (not isinstance(filters, dict) or filters.get('mode') not in {'and', 'or'}
                or not isinstance(filters.get('conditions'), list)):
            raise LibraryError('Invalid Work catalog view filters')
        views.add(view['id'])
    if not views or document.get('defaultViewId') not in views:
        raise LibraryError('Work catalog default view is missing')


def parse_catalog(markdown):
    """Parse exactly one native block, rejecting corruption without normalizing it."""
    if not isinstance(markdown, str):
        raise LibraryError('Work catalog Markdown must be text')
    matches = list(_FENCE.finditer(markdown))
    if len(matches) != 1:
        raise LibraryError('Work catalog requires exactly one super-table block')
    match = matches[0]
    ticks = match.group(1).strip()
    closing = re.search(r'^' + re.escape(ticks) + r'[ \t]*(?=\r?$)', markdown[match.end():], re.MULTILINE)
    if closing is None:
        raise LibraryError('Work catalog super-table block is unclosed')
    start, end = match.end() + closing.start(), match.end() + closing.end()
    try:
        document = json.loads(markdown[match.end():start], object_pairs_hook=_unique_json,
                              parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError) as exc:
        raise LibraryError('Invalid Work catalog JSON: ' + str(exc)) from exc
    _validate_document(document)
    return Catalog(document, markdown[:match.start()], markdown[end:], match.group(), markdown[start:end])


def build_catalog(records, document=None):
    """Replace records while retaining user columns, views, widths and unknown data."""
    if isinstance(document, Catalog):
        catalog = deepcopy(document)
    else:
        content = deepcopy(document) if document is not None else {
            'schemaVersion': 1, 'tableId': TABLE_ID,
            'fields': [{'id': key, 'name': FIELD_NAMES[key], 'type': kind}
                       for key, kind in FIELD_TYPES.items()],
            'records': [], 'views': [{'id': 'all', 'name': '全部作品',
                                      'filters': {'mode': 'and', 'conditions': []}}],
            'defaultViewId': 'all',
        }
        catalog = Catalog(content)
    catalog.document['records'] = deepcopy(list(records))
    _validate_document(catalog.document)
    newline = '\r\n' if catalog.opening.endswith('\r\n') else '\n'
    source = json.dumps(catalog.document, ensure_ascii=False, indent=2, allow_nan=False)
    return catalog.prefix + catalog.opening + source.replace('\n', newline) + newline + catalog.closing + catalog.suffix


def _review_path(value):
    if value is None or value == '':
        return None
    match = re.fullmatch(r'\[\[([^\]\n|]+)(?:\|[^\]\n]*)?\]\]', value)
    if not match:
        raise LibraryError('Work review cell must be empty or one Markdown wikilink')
    relative = match[1]
    if not relative.endswith('.md'):
        relative += '.md'
    from pathlib import PurePosixPath
    path = PurePosixPath(relative)
    if (path.is_absolute() or '..' in path.parts or '#' in relative or '\\' in relative
            or any(part.startswith('.') for part in path.parts)):
        raise LibraryError('Work review link must use a safe visible Markdown path')
    return path.as_posix()


def has_review_body(markdown_or_body):
    """Only managed navigation and title-only placeholders can lack a review file."""
    from .publication_navigation import strip_publication_navigation
    body = _split(markdown_or_body)[1]
    try:
        body = strip_publication_navigation(body)
    except ValueError as exc:
        raise LibraryError('Invalid Work publication navigation') from exc
    return bool(re.sub(r'^\s*#{1,6}\s+[^\n]*(?:\n|$)', '', body, flags=re.MULTILINE).strip())


def row_from_note(note, review_path=None):
    properties = note['properties']
    experiences = deepcopy(properties.get('experiences') or [])
    if not isinstance(experiences, list) or any(not isinstance(item, dict) for item in experiences):
        raise LibraryError('Work experiences must remain a list of mappings')
    first = experiences[0] if experiences else {}
    row = {'id': note['id'], 'values': {key: deepcopy(properties.get(key)) for key in SCALAR_FIELDS}}
    if row['values']['rating'] == '':
        row['values']['rating'] = None
    row['values'].update({key: first.get(key) for key in ('startedOn', 'endedOn')})
    row['values']['review'] = '[[' + review_path + ']]' if review_path else None
    navigation = re.search(r'<!-- markdown-library:publication-nav:start -->(.*?)<!-- markdown-library:publication-nav:end -->', note['markdown'], re.S)
    article = re.search(r'\[\[[^\]\n]+\]\]', navigation[1]) if navigation else None
    row['values']['article'] = article[0] if article else None
    _validate_row(row)
    return row


def catalog_entry(note, review_path=None):
    body = _split(note['markdown'])[1]
    if review_path is None and has_review_body(body):
        raise LibraryError('A substantive Work review requires a linked Markdown file')
    return {'properties': deepcopy(note['properties']), 'review_path': review_path,
            'body': '' if review_path else body}


def review_markdown(note):
    properties = {key: deepcopy(value) for key, value in note['properties'].items()
                  if key not in DAILY_FIELDS or key == 'title'}
    return _render(properties, _split(note['markdown'])[1])


def backing_path(state, note_id):
    config = state['work_catalog']
    return config['rows'].get(note_id, {}).get('review_path') or config['path'] + '#work-' + note_id


def is_catalog_note(note):
    return bool(note.get('catalog_backed'))


def _read_catalog(library, state):
    path = state['work_catalog']['path']
    destination = library._path(path)
    if not destination.is_file():
        raise LibraryError('Work catalog file is missing; absence is not a deletion')
    markdown = _read(destination)
    return parse_catalog(markdown), _hash(markdown)


def _physical_review(library, path, note_id):
    destination = library._path(path)
    if not destination.is_file():
        raise LibraryError(f'Work review is missing; preserve catalog and recover or rename: {note_id}')
    markdown = _read(destination)
    properties, _ = _split(markdown)
    library._validate_properties(properties)
    if properties['id'] != note_id or properties['kind'] != 'work':
        raise LibraryError('Work review link points to another identity')
    if (set(properties) & DAILY_FIELDS) - {'title'}:
        raise LibraryError('Work review duplicates catalog daily fields; resolve both versions')
    return {'id': note_id, 'path': path, 'markdown': markdown, 'properties': properties,
            'sha256': _hash(markdown)}


def _logical(library, state, row, physical=None):
    config = state['work_catalog']
    note_id, values = row['id'], row['values']
    retained = config['rows'].get(note_id, {})
    original = deepcopy(retained.get('properties', {}))
    if original and (original.get('id') != note_id or original.get('kind') != 'work'):
        raise LibraryError('Work catalog retained identity differs')
    properties = deepcopy(original)
    properties.update({'id': note_id, 'kind': 'work', 'schema_version': 1})
    for key in SCALAR_FIELDS:
        value = None if key == 'rating' and values[key] == '' else values[key]
        if key in original or value is not None:
            properties[key] = deepcopy(value)
    raw_experiences = deepcopy(original.get('experiences'))
    experiences = raw_experiences
    if experiences is not None and (not isinstance(experiences, list)
                                   or any(not isinstance(item, dict) for item in experiences)):
        raise LibraryError('Invalid retained Work experiences')
    experiences = experiences or []
    if not experiences and any(values[key] not in {None, ''} for key in ('startedOn', 'endedOn')):
        experiences.append({})
    if experiences:
        for key in ('startedOn', 'endedOn'):
            if key in experiences[0] or values[key] not in {None, ''}:
                experiences[0][key] = values[key]
    if 'experiences' in original or experiences:
        properties['experiences'] = (None if raw_experiences is None and not experiences
                                     else experiences)
    path = _review_path(values['review'])
    if (path and retained.get('review_link') == path and retained.get('review_path')
            and retained['review_path'] != path):
        path = retained['review_path']
    if physical is not None:
        if physical['properties']['kind'] != 'work':
            raise LibraryError('Work catalog ID collides with another kind')
        # A native rename keeps the stable identity and updates only the link.
        if path is None:
            raise LibraryError('Work review exists without a catalog review link')
        path = physical['path']
        physical = _physical_review(library, path, note_id)
    elif path:
        physical = _physical_review(library, path, note_id)
    elif retained.get('review_path'):
        raise LibraryError('Work review link was removed; absence is not a deletion')
    if physical:
        # Review-local unknown properties also remain ordinary editable data.
        properties.update({key: deepcopy(value) for key, value in physical['properties'].items()
                           if key not in DAILY_FIELDS and key not in {'id', 'kind', 'schema_version'}})
        body = _split(physical['markdown'])[1]
    else:
        body = retained.get('body', '')
        if not isinstance(body, str) or has_review_body(body):
            raise LibraryError('Substantive Work body cannot be retained as hidden catalog text')
    library._validate_properties(properties)
    markdown = _render(properties, body)
    storage_path = physical['path'] if physical else config['path']
    return {'id': note_id, 'path': path or config['path'] + '#work-' + note_id,
            'properties': properties, 'markdown': markdown, 'sha256': _hash(markdown),
            'catalog_backed': True, 'catalog_path': config['path'], 'storage_path': storage_path,
            'storage_sha256': physical['sha256'] if physical else None}


def scan_overlay(library, state, found, *, apply_defaults=False):
    if not enabled(state):
        return found
    catalog, catalog_hash = _read_catalog(library, state)
    config = state['work_catalog']
    records = {row['id']: row for row in catalog.document['records']}
    for note_id, note in found.items():
        if note['properties'].get('kind') == 'work' and note_id not in records:
            raise LibraryError('Physical Work has no catalog row; resolve identity before editing: ' + note_id)
    for note_id in config['rows']:
        if note_id not in records and not state['notes'].get(note_id, {}).get('deleted'):
            raise LibraryError('Known Work catalog row is missing; absence is not a deletion: ' + note_id)
    result = dict(found)
    defaulted = False
    for note_id, row in records.items():
        # Native add-row creates the complete row before the title is typed.
        # It has no library identity yet, so leave it editable in the table
        # without registering a Work or giving it publication/deletion intent.
        if (not row['values']['title'].strip() and note_id not in config['rows']
                and note_id not in state['notes'] and note_id not in found):
            continue
        known = state['notes'].get(note_id, {})
        if known.get('kind') not in {None, 'work'} or known.get('unregistered') or known.get('purged'):
            raise LibraryError('Work catalog ID collides with retained library identity: ' + note_id)
        if known.get('deleted'):
            raise LibraryError('Deleted Work catalog row reappeared outside recover: ' + note_id)
        if apply_defaults and note_id not in config['rows'] and note_id not in state['notes']:
            if config.get('default_status') and row['values']['status'] in {None, ''}:
                row['values']['status'] = config['default_status']
                defaulted = True
            if config.get('default_end_today') and row['values']['endedOn'] in {None, ''}:
                row['values']['endedOn'] = _today(config)
                defaulted = True
        note = _logical(library, state, row, found.get(note_id))
        note['catalog_sha256'] = catalog_hash
        if note['storage_path'] == config['path']:
            note['storage_sha256'] = catalog_hash
        result[note_id] = note
        config['rows'][note_id] = catalog_entry(note, note['path'] if '#work-' not in note['path'] else None)
        config['rows'][note_id]['review_link'] = _review_path(row['values']['review'])
    if defaulted:
        content = build_catalog(catalog.document['records'], catalog)
        library._commit(state, [(config['path'], content, catalog_hash)], storage_resolved=True)
        for note in result.values():
            if note.get('catalog_backed'):
                note['catalog_sha256'] = _hash(content)
                if note['storage_path'] == config['path']:
                    note['storage_sha256'] = _hash(content)
    return result


def storage_hash(library, state, note):
    """Return the current logical hash for an early guard; commit guards again."""
    if not enabled(state) or not is_catalog_note(note):
        destination = library._path(note['path'])
        return _hash(_read(destination)) if destination.exists() else None
    catalog, _ = _read_catalog(library, state)
    row = next((row for row in catalog.document['records'] if row['id'] == note['id']), None)
    return _logical(library, state, row)['sha256'] if row else None


def assert_storage(library, note):
    """Guard the physical versions captured by scan, without acquiring a lock."""
    if not is_catalog_note(note):
        return
    pairs = [(note['catalog_path'], note['catalog_sha256'])]
    if note['storage_path'] != note['catalog_path']:
        pairs.append((note['storage_path'], note['storage_sha256']))
    for relative, expected in pairs:
        destination = library._path(relative)
        actual = _hash(_read(destination)) if destination.is_file() else None
        if actual != expected:
            raise LibraryError('Revision conflict: Work catalog storage changed: ' + relative)


def expand_operations(library, state, writes=None, removes=None):
    """Translate logical Work operations once, before the ordinary journal is made."""
    writes, removes = list(writes or []), list(removes or [])
    if not enabled(state):
        return writes, removes
    catalog, catalog_hash = _read_catalog(library, state)
    config = state['work_catalog']
    rows = {row['id']: deepcopy(row) for row in catalog.document['records']}
    physical_writes, physical_removes = [], []
    changed = False
    touched = set()
    paths = {backing_path(state, note_id): note_id for note_id in config['rows']}
    # Remembering the desired revision can already have changed record.path.
    paths.update({record.get('path'): note_id for note_id, record in state['notes'].items()
                  if note_id in config['rows']})
    work_writes = []
    for relative, markdown, expected in writes:
        if relative == config['path']:
            raise LibraryError('Direct catalog replacement requires the explicit storage transaction')
        properties, _ = _split(markdown)
        note_id = properties.get('id')
        managed = (properties.get('kind') == 'work' and note_id in config['rows']
                   and not relative.startswith('.'))
        if not managed:
            physical_writes.append((relative, markdown, expected))
        else:
            work_writes.append((relative, markdown, expected, properties, note_id))
    creations = {note_id for _, _, expected, _, note_id in work_writes if expected is None}
    for note_id in config['rows']:
        if (note_id not in rows and note_id not in creations
                and not state['notes'].get(note_id, {}).get('deleted')):
            raise LibraryError('Known Work catalog row is missing; absence is not a deletion: ' + note_id)
    for note_id, row in rows.items():
        if (not row['values']['title'].strip()
                and (note_id in config['rows'] or note_id in state['notes'])):
            raise LibraryError('Known Work catalog title must remain nonempty: ' + note_id)
    # Check all logical expectations before changing configuration or writing.
    current = {}
    for relative, markdown, expected, properties, note_id in work_writes:
        if note_id in touched:
            raise LibraryError('Multiple writes to one Work in a transaction')
        touched.add(note_id)
        old = _logical(library, state, rows[note_id]) if note_id in rows else None
        if (old['sha256'] if old else None) != expected:
            raise LibraryError('Revision conflict: Work catalog row changed: ' + note_id)
        current[note_id] = old
    for relative, expected in removes:
        note_id = paths.get(relative)
        if note_id is None:
            physical_removes.append((relative, expected))
            continue
        if note_id in touched:
            # A move writes the logical revision at a new physical review path.
            if state['notes'].get(note_id, {}).get('deleted'):
                raise LibraryError('Conflicting Work write and delete')
            old = current[note_id]
            if old and old['sha256'] != expected:
                raise LibraryError('Revision conflict: Work review move changed')
            continue
        if not state['notes'].get(note_id, {}).get('deleted'):
            raise LibraryError('Removing a Work row requires an explicit lifecycle deletion')
        if note_id not in rows:
            if expected is not None:
                raise LibraryError('Work catalog row is already missing')
            continue
        old = _logical(library, state, rows[note_id])
        if old['sha256'] != expected:
            raise LibraryError('Revision conflict: Work catalog row changed before deletion')
        if old['path'] != config['path'] + '#work-' + note_id:
            physical_removes.append((old['path'], old['storage_sha256']))
        del rows[note_id]
        changed = True
    for relative, markdown, expected, properties, note_id in work_writes:
        library._validate_properties(properties)
        old = current[note_id]
        note = {'id': note_id, 'path': relative, 'markdown': markdown, 'properties': properties}
        review_path = None
        if has_review_body(markdown):
            review_path = (relative if relative != config['path'] + '#work-' + note_id
                           else config['rows'][note_id].get('review_path')
                           or config.get('review_root', 'works/感想') + '/' + note_id + '.md')
            destination = library._path(review_path)
            previous_review = old and old['path'] != config['path'] + '#work-' + note_id
            if destination.exists() and (not previous_review or review_path != old['path']):
                raise LibraryError('Refusing to overwrite an occupied Work review path')
            expected_physical = old['storage_sha256'] if previous_review and review_path == old['path'] else None
            physical_writes.append((review_path, review_markdown(note), expected_physical))
        elif old and old['path'] != config['path'] + '#work-' + note_id:
            physical_removes.append((old['path'], old['storage_sha256']))
        if old and old['path'] != config['path'] + '#work-' + note_id and review_path != old['path']:
            removal = (old['path'], old['storage_sha256'])
            if removal not in physical_removes:
                physical_removes.append(removal)
        replacement = row_from_note(note, review_path)
        row = rows.get(note_id, {'id': note_id, 'values': {}})
        row['values'].update(replacement['values'])
        rows[note_id] = row
        config['rows'][note_id] = catalog_entry(note, review_path)
        changed = True
    if changed:
        # Keep existing order and unrelated native rows; append only new IDs.
        order = [row['id'] for row in catalog.document['records']]
        order.extend(note_id for note_id in rows if note_id not in order)
        content = build_catalog([rows[note_id] for note_id in order if note_id in rows], catalog)
        physical_writes.append((config['path'], content, catalog_hash))
    return physical_writes, physical_removes
