"""Small editable Work files with website metadata retained behind their stable ID.

The private profile is part of the existing journaled library state and backups.
Removing website-only YAML is a representation change, never a remote clear.
"""
from copy import deepcopy
from datetime import date

from .core import LibraryError, _hash, _render, _split
from .sync import content_hash


WEBSITE_ONLY = frozenset({
    'slug', 'aliases', 'tags', 'cover', 'coverUrl', 'releaseDate', 'creators',
    'platforms', 'description', 'sources', 'generateSlug', 'visibility',
    'payload_observed_status', 'article', 'lastPlayedOn',
})


def profile(library, note_id, state=None):
    if state is None:
        # A review can project hundreds of Works. Read shared JSON once per
        # exact atomic-file version, rather than parsing it for every field.
        stamp = library._path('.markdown-library/state.json').stat()
        signature = (stamp.st_ino, stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns)
        cached = getattr(library, '_work_profile_cache', None)
        if cached is None or cached[0] != signature:
            current = library._read_state()
            values = {key: {'work_profile': row['work_profile']}
                      for key, row in current['notes'].items() if 'work_profile' in row}
            cached = library._work_profile_cache = (signature, {'notes': values})
        state = cached[1]
    value = state['notes'].get(note_id, {}).get('work_profile')
    if value is None:
        return None
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('fields'), dict)
            or set(value['fields']) - WEBSITE_ONLY
            or content_hash(value['fields']) != value.get('fields_hash')):
        raise LibraryError('Invalid retained Work metadata; preserve state and review')
    return deepcopy(value['fields'])


def set_profile(state, note_id, fields):
    fields = {key: deepcopy(value) for key, value in fields.items() if key in WEBSITE_ONLY}
    state['notes'][note_id]['work_profile'] = {
        'schema_version': 1, 'fields': fields, 'fields_hash': content_hash(fields)}


def website_properties(library, note, state=None):
    if note['properties']['kind'] != 'work':
        return deepcopy(note['properties'])
    note_id = note.get('id', note['properties'].get('id'))
    if not isinstance(note_id, str) or not note_id:
        raise LibraryError('Work projection requires its stable identity')
    retained = profile(library, note_id, state)
    return {**(retained or {}), **deepcopy(note['properties'])}


def seed_steam_end(properties):
    """Seed an empty, unambiguous experience end, preserving hand-edited dates."""
    last = properties.get('lastPlayedOn')
    if not last:
        return None
    try:
        if not isinstance(last, str) or date.fromisoformat(last).isoformat() != last:
            raise ValueError()
    except ValueError:
        return {'status': 'needs-review', 'reason': 'invalid-steam-date'}
    raw_rows = properties.get('experiences')
    rows = deepcopy([] if raw_rows is None else raw_rows)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return {'status': 'needs-review', 'reason': 'invalid-experience-rows'}
    if not rows:
        properties['experiences'] = [{'endedOn': last}]
        return {'status': 'seeded', 'endedOn': last}
    if len(rows) != 1:
        return {'status': 'needs-review', 'reason': 'multiple-experiences'}
    if rows[0].get('endedOn'):
        return {'status': 'preserved-existing', 'endedOn': rows[0]['endedOn']}
    start = rows[0].get('startedOn')
    if start:
        import re
        valid = isinstance(start, str) and re.fullmatch(r'\d{4}(?:-\d{2})?(?:-\d{2})?', start)
        try:
            if not valid:
                raise ValueError()
            date.fromisoformat(start + ('-01-01' if len(start) == 4 else '-01' if len(start) == 7 else ''))
        except ValueError:
            return {'status': 'needs-review', 'reason': 'invalid-start-date'}
        if start > last:
            return {'status': 'needs-review', 'reason': 'end-before-start'}
    rows[0]['endedOn'] = last
    properties['experiences'] = rows
    return {'status': 'seeded', 'endedOn': last}


def compact_works(library, *, apply=False, expected_token=None, seed_steam=False,
                  steam_dates=None, note_ids=None):
    """One guarded batch; current drafts only, no selection or website writes."""
    from .bootstrap import _index
    steam_dates = dict(steam_dates or {})
    wanted = set(note_ids) if note_ids is not None else None
    if steam_dates and not seed_steam:
        raise LibraryError('Steam date imports require explicit end-date seeding')

    def prepare(state, notes):
        changes, outcomes, guards = [], [], []
        for note in sorted(notes.values(), key=lambda n: n['id']):
            if note['properties']['kind'] != 'work':
                continue
            if wanted is not None and note['id'] not in wanted:
                continue
            props = deepcopy(note['properties'])
            old_profile = profile(library, note['id'], state)
            full = {**(old_profile or {}), **props}
            if note['id'] in steam_dates:
                full['lastPlayedOn'] = steam_dates[note['id']]
            seeded = seed_steam_end(full) if seed_steam else None
            if seeded and seeded['status'] == 'seeded':
                props['experiences'] = full['experiences']
            retained = {key: value for key, value in full.items() if key in WEBSITE_ONLY}
            removed = sorted(set(props) & WEBSITE_ONLY)
            for key in removed:
                props.pop(key)
            # Preserve body bytes, including a Windows newline convention.
            body = _split(note['markdown'])[1]
            markdown = _render(props, body)
            if not removed and not (seeded and seeded['status'] == 'seeded'):
                markdown = note['markdown']
            if old_profile is None or retained != old_profile or markdown != note['markdown']:
                changes.append((note, markdown, props, retained))
            guards.append({'id': note['id'], 'sha256': note['sha256'], 'metadata': old_profile})
            outcomes.append({'id': note['id'], 'path': note['path'], 'removed': removed,
                             'steam_end': seeded, 'sha256': _hash(markdown)})
        if (wanted is not None and {g['id'] for g in guards} != wanted) or set(steam_dates) - {g['id'] for g in guards}:
            raise LibraryError('Steam batch contains missing, deleted or non-Work identities')
        token = content_hash({'guards': guards, 'seed_steam': seed_steam, 'steam_dates': steam_dates})
        return changes, outcomes, token

    if not apply:
        state, notes = _index(library)
        changes, outcomes, token = prepare(state, notes)
    else:
        with library._lock():
            state = library._load()
            notes = library._scan(state)
            changes, outcomes, token = prepare(state, notes)
            if not expected_token or expected_token != token:
                raise LibraryError('Work compaction preview expired; review again')
            writes = []
            for note, markdown, props, retained in changes:
                updated = {**note, 'markdown': markdown, 'properties': props, 'sha256': _hash(markdown)}
                library._remember(state, updated, 'work-properties-compacted')
                set_profile(state, note['id'], retained)
                if markdown != note['markdown']:
                    writes.append((note['path'], markdown, note['sha256']))
            if changes:
                library._commit(state, writes)
    return {'applied': apply, 'expected_token': token, 'changed': len(changes),
            'publication_selected': False, 'remote_writes': False, 'items': outcomes}
