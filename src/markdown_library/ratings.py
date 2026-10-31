"""Canonical 0–100 ratings and explicit, lossless legacy scale conversion.

No value-based guessing: low current values mean low scores on the 100 scale.
History bytes are immutable; their retained scale determines recovery projection.
"""
from copy import deepcopy
from decimal import Decimal
import json
import math

import yaml

from .core import LibraryError, _hash, _now, _read, _split


RATING_SCALE = 100


def validate_rating(value, *, scale=RATING_SCALE):
    if value is None:
        return value
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= scale or not math.isfinite(value)):
        raise LibraryError(f'rating must be a finite number from 0 to {scale}, or null')
    return value


def convert_rating(value, *, source_scale):
    if type(source_scale) is not int or source_scale not in {10, 100}:
        raise LibraryError('Explicit rating scale must be 10 or 100')
    validate_rating(value, scale=source_scale)
    if value is None or source_scale == RATING_SCALE:
        return value
    number = Decimal(str(value)) * Decimal(RATING_SCALE) / Decimal(source_scale)
    return int(number) if number == number.to_integral_value() else float(number)


def convert_fields(fields, *, source_scale):
    result = deepcopy(fields)
    if 'rating' in result:
        result['rating'] = convert_rating(result['rating'], source_scale=source_scale)
    return result


def convert_markdown(markdown, *, source_scale):
    """Replace the scalar rating only; preserve body, YAML, comments and CRLF."""
    properties, _ = _split(markdown)
    if properties.get('kind') != 'work' or 'rating' not in properties:
        return markdown
    value = convert_rating(properties['rating'], source_scale=source_scale)
    if value == properties['rating']:
        return markdown
    text = markdown.removeprefix('\ufeff')
    lines = text.splitlines(keepends=True)
    end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() in {'---', '...'})
    source = ''.join(lines[1:end])
    node = yaml.compose(source)
    matches = [(key, val) for key, val in node.value if key.value == 'rating']
    if len(matches) != 1 or not isinstance(matches[0][1], yaml.ScalarNode):
        raise LibraryError('Legacy rating requires one unambiguous scalar YAML property')
    val = matches[0][1]
    replacement = json.dumps(value, allow_nan=False)
    source = source[:val.start_mark.index] + replacement + source[val.end_mark.index:]
    prefix = '\ufeff' if markdown.startswith('\ufeff') else ''
    return prefix + lines[0] + source + ''.join(lines[end:])


def revision_scale(state, note_id, digest, *, rating=None):
    record = state['notes'].get(note_id, {})
    matches = [row for row in record.get('history', []) if row['sha256'] == digest]
    explicit = {row['rating_scale'] for row in matches if 'rating_scale' in row}
    if len(explicit) > 1:
        # Identical historical/current bytes can have distinct scale meanings.
        # Zero/null are the only ratings invariant under the legacy conversion.
        if rating is None or rating == 0:
            return 100
        raise LibraryError('Ambiguous historical rating scale for this SHA; preserve both interpretations and review')
    return next(iter(explicit), state.get('rating_scale', 10))


def canonical_revision(library, note_id, digest, markdown, *, state=None):
    state = library._read_state() if state is None else state
    if state.get('rating_scale') != 100:
        return markdown
    return convert_markdown(markdown, source_scale=revision_scale(
        state, note_id, digest, rating=_split(markdown)[0].get('rating')))


def migrate_ratings(library, *, source_scale=10, apply=False, expected_token=None):
    """Guarded local migration; never publishes, resolves conflicts, or contacts APIs.

    Caller coordinates the remote migration and pauses existing writers. Active
    pending writes are refused. All changes, including sync state and new objects,
    share the existing crash-recoverable journal. Original objects are untouched.
    """
    from .bootstrap import _index
    from .sync import content_hash, _remote_hash
    from .transport import _Store

    if type(source_scale) is not int or source_scale not in {10, 100}:
        raise LibraryError('Explicit rating scale must be 10 or 100')

    def prepare():
        old, notes = _index(library)
        sync_path = library._path('.markdown-library/sync-state.json')
        raw_sync = _read(sync_path) if sync_path.exists() else None
        sync = json.loads(raw_sync) if raw_sync is not None else None
        if old.get('rating_scale') == 100:
            if sync is not None and sync.get('rating_scale', 100) != 100:
                raise LibraryError('Library and sync rating scales disagree')
            return old, [], [], content_hash({'state': old, 'sync': raw_sync}), True
        if old.get('rating_scale', source_scale) != source_scale:
            raise LibraryError('Source rating scale differs from retained state')
        if library._path('.markdown-library/transaction.json').exists():
            raise LibraryError('Recover the interrupted library transaction before rating migration')
        if sync is not None and (sync.get('pending') or sync.get('lifecycle_pending')):
            raise LibraryError('Resolve pending website writes before rating migration')
        state = deepcopy(old)
        state['rating_scale'] = 100
        writes, items, objects, mapped = [], [], {}, {}
        for record in state['notes'].values():
            for row in record.get('history', []):
                row.setdefault('rating_scale', source_scale)

        def retain(note_id, digest, raw, *, current=False):
            if _hash(raw) != digest:
                raise LibraryError('Corrupted rating migration history object')
            migrated = convert_markdown(raw, source_scale=source_scale)
            converted = _hash(migrated)
            mapped[digest] = converted
            objects[digest] = raw
            objects[converted] = migrated
            history = state['notes'][note_id].setdefault('history', [])
            if not any(row['sha256'] == digest for row in history):
                history.append({'sha256': digest, 'action': 'rating-migration-source', 'rating_scale': source_scale, 'observed_at': _now()})
            if not any(row['sha256'] == converted and row.get('rating_scale') == 100 for row in history):
                history.append({'sha256': converted, 'action': 'rating-scale-migrated', 'rating_scale': 100, 'observed_at': _now()})
            if current:
                state['notes'][note_id]['sha256'] = converted
            return migrated, converted

        def map_digest(note_id, digest):
            if not digest:
                return digest
            if digest in mapped:
                return mapped[digest]
            filename = library._path(f'.markdown-library/objects/{digest}.md')
            if not filename.is_file():
                raise LibraryError('Observed rating revision object is missing; migration requires review')
            return retain(note_id, digest, _read(filename))[1]

        for note_id, note in sorted(notes.items()):
            if note['properties']['kind'] != 'work':
                continue
            migrated, digest = retain(note_id, note['sha256'], note['markdown'], current=True)
            if migrated != note['markdown']:
                writes.append((note['path'], migrated, note['sha256']))
            items.append({'id': note_id, 'path': note['path'], 'before': note['properties'].get('rating'),
                          'after': _split(migrated)[0].get('rating'), 'sha256': digest})
        for note_id, selected in state['publications'].items():
            if selected['kind'] != 'work':
                continue
            selected['snapshot']['properties'] = convert_fields(selected['snapshot']['properties'], source_scale=source_scale)
            selected['sha256'] = map_digest(note_id, selected['sha256'])
            selected['rating_scale'] = 100
        if sync is not None:
            # Each side is transformed independently; genuine rating conflicts stay conflicts.
            for note_id, baseline in sync.get('baselines', {}).items():
                if baseline.get('collection') != 'works' or old['notes'].get(note_id, {}).get('purged'):
                    continue
                from .merge import recover_base
                local, remote, reason = recover_base(library, note_id, baseline, None, sync['baselines'],
                                                   library_state=old, notes=notes)
                if local is None or remote is None:
                    raise LibraryError(f'Cannot prove rating baseline for {note_id}: {reason}')
                baseline['local_fields'], baseline['remote_fields'] = local, remote
                if _remote_hash(remote, 'works') != baseline.get('remote_hash'):
                    raise LibraryError('Unverified remote baseline during rating migration')
                validation = deepcopy(local)
                if baseline.get('state') == 'imported-draft':
                    validation.update({'_status': 'published', 'visibility': 'public'})
                if ((baseline.get('local_fields_hash') and content_hash(local) != baseline['local_fields_hash'])
                        or (not baseline.get('local_fields_hash') and content_hash(validation) != baseline.get('local_hash'))):
                    raise LibraryError('Unverified local baseline during rating migration')
                old_hash = baseline.get('local_hash')
                candidates = [local, validation]
                old_selected = old['publications'].get(note_id)
                if old_selected:
                    from .sync import export_publication
                    candidates.append(export_publication(old_selected)['fields'])
                proven_local = next((fields for fields in candidates if content_hash(fields) == old_hash), None)
                if proven_local is None:
                    raise LibraryError(f'Cannot prove selected rating baseline for {note_id}')
                baseline['local_fields'] = convert_fields(local, source_scale=source_scale)
                baseline['remote_fields'] = convert_fields(remote, source_scale=source_scale)
                validation = deepcopy(baseline['local_fields'])
                if baseline.get('state') == 'imported-draft':
                    validation.update({'_status': 'published', 'visibility': 'public'})
                baseline['local_hash'] = content_hash(convert_fields(proven_local, source_scale=source_scale))
                baseline['remote_hash'] = _remote_hash(baseline['remote_fields'], 'works')
                if 'local_fields_hash' in baseline:
                    baseline['local_fields_hash'] = content_hash(baseline['local_fields'])
                for key in ('draft_sha256', 'imported_sha256'):
                    if key in baseline:
                        baseline[key] = map_digest(note_id, baseline[key])
                accepted = sync.get('reverse_imports', {}).get(note_id)
                if accepted:
                    for key in ('local_fields', 'remote_fields'):
                        if not isinstance(accepted.get(key), dict):
                            raise LibraryError('Accepted rating observation has no verified fields; review before migration')
                    if (_remote_hash(accepted['remote_fields'], 'works') != accepted.get('remote_hash')
                            or content_hash(accepted['local_fields']) != accepted.get('local_fields_hash')):
                        raise LibraryError('Unverified accepted observation during rating migration')
                    for key in ('local_fields', 'remote_fields'):
                        accepted[key] = convert_fields(accepted[key], source_scale=source_scale)
                    accepted['local_fields_hash'] = content_hash(accepted['local_fields'])
                    accepted['remote_hash'] = _remote_hash(accepted['remote_fields'], 'works')
                    accepted['local_hash'] = map_digest(note_id, accepted['local_hash'])
                    old_selection = old['publications'].get(note_id)
                    if accepted.get('selection_hash'):
                        if not old_selection or accepted['selection_hash'] != content_hash(old_selection):
                            raise LibraryError('Accepted observation refers to another selection; review before migration')
                        accepted['selection_hash'] = content_hash(state['publications'][note_id])
            sync['rating_scale'] = 100
            writes.append(('.markdown-library/sync-state.json', json.dumps(sync, ensure_ascii=False, indent=2, allow_nan=False),
                           _hash(raw_sync)))
        for digest, markdown in objects.items():
            relative = f'.markdown-library/objects/{digest}.md'
            existing = library._path(relative)
            if existing.exists():
                if _hash(_read(existing)) != digest:
                    raise LibraryError('Corrupted retained rating history object')
            else:
                writes.append((relative, markdown, None))
        token = content_hash({'state': old, 'files': {key: value['sha256'] for key, value in notes.items()},
                              'sync': raw_sync, 'objects': sorted(objects), 'source_scale': source_scale})
        return state, writes, items, token, False

    if not apply:
        state, writes, items, token, done = prepare()
    else:
        sync_path = library._path('.markdown-library/sync-state.json')
        # The same lock order as ordinary sync: sync lock, then library lock.
        from contextlib import nullcontext
        target = json.loads(_read(sync_path))['target'] if sync_path.exists() else None
        guard = _Store(library, target).locked() if target else nullcontext()
        with guard, library._lock():
            state, writes, items, token, done = prepare()
            if not done:
                if not expected_token or expected_token != token:
                    raise LibraryError('Rating migration preview expired; preview again')
                library._commit(state, writes)
    return {'applied': apply and not done, 'already_migrated': done, 'rating_scale': 100,
            'source_scale': source_scale, 'expected_token': token, 'items': items,
            'changed_current': sum(row['before'] != row['after'] for row in items),
            'remote_writes': False, 'publication_selected': False}
