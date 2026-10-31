"""Read-only synchronization review and explicit guarded current-draft resolution.

No function in this module selects publication or calls a remote write API.
Values are included only when the caller explicitly requests private review.
"""
from copy import deepcopy

from .bootstrap import _index
from .core import LibraryError, _hash, _split
from .merge import (local_fields, merge_fields, recover_base, remote_fields,
                    render_merged, _set)
from .sync import _remote_hash, content_hash, export_publication
from .transport import _Store, TransportError
from .lifecycle import _call, sync_lifecycle


def prepare_review(library, state, note, remote, *, formal=None, notes=None, library_state=None):
    note_id = note['id']
    baseline = state['baselines'][note_id]
    collection = baseline['collection']
    library_state = library._read_state() if library_state is None else library_state
    selected = library_state['publications'].get(note_id)
    accepted = state['reverse_imports'].get(note_id)
    left, right, evidence = recover_base(library, note_id, baseline, accepted, state['baselines'], library_state=library_state, notes=notes)
    local = local_fields(note, collection, state['baselines'], library=library, notes=notes)
    incoming = remote_fields(remote, collection)
    merge = merge_fields(left, right, local, incoming)
    selection = {'state': selected.get('state'), 'sha256': selected.get('sha256'),
                 'record_hash': content_hash(selected)} if selected else None
    selection_fields = None
    if selected:
        try:
            selected_export = export_publication(selected)
            selection['fields_hash'] = selected_export['content_hash']
            selection_fields = deepcopy(selected_export['fields'])
            article = selection_fields.get('article')
            if isinstance(article, str) and article in state['baselines']:
                selection_fields['article'] = state['baselines'][article]['remote_id']
        except ValueError:
            selection['fields_hash'] = None
    comparable = lambda fields: {key: value for key, value in fields.items() if key not in {'_status', 'visibility'}}
    difference = lambda before, after: sorted(key for key in set(before) | set(after) if (key in before) != (key in after) or before.get(key) != after.get(key))
    selection_current = comparable(local)
    # False protection observations are metadata, rather than publishable edits.
    if selection_fields is not None:
        for key in ('protectedContent', 'password'):
            if key not in selection_fields and not selection_current.get(key): selection_current.pop(key, None)
    selection_difference = difference(comparable(selection_fields), selection_current) if selection_fields is not None else None
    observed_file_hash = ((accepted or {}).get('local_hash') or baseline.get('draft_sha256')
                          or baseline.get('imported_sha256') or (selected or {}).get('sha256'))
    payload = lambda row: {'remote_id': row.get('id'), 'version': row.get('updatedAt'),
                          'fields_hash': _remote_hash(row, collection),
                          'state': row.get('_status'), 'visibility': row.get('visibility'),
                          'deleted_at': row.get('deletedAt')} if row is not None else None
    result = {'id': note_id, 'collection': collection,
              'current': {'sha256': note['sha256'], 'fields_hash': content_hash(local)},
              'selection': selection, 'payload_latest': payload(remote), 'payload_formal': payload(formal),
              'baseline': {'state': baseline.get('state'), 'remote_hash': baseline.get('remote_hash'),
                           'remote_version': baseline.get('remote_version'), 'local_hash': baseline.get('local_hash'),
                           'ancestor_evidence': evidence},
              'accepted_observation': {key: deepcopy(accepted[key]) for key in
                                       ('remote_hash', 'remote_version', 'local_hash', 'selection_hash') if accepted and key in accepted},
              'pending': note_id in state['pending'], 'merge': merge,
              'comparisons': {'current_vs_selection': selection_difference,
                              'current_has_unselected_fields': bool(selection_difference) if selection_difference is not None else None,
                              'current_file_differs_selection': note['sha256'] != selected.get('sha256') if selected else None,
                              'current_file_differs_observation': note['sha256'] != observed_file_hash if observed_file_hash else None,
                              'latest_differs_formal': _remote_hash(remote, collection) != _remote_hash(formal, collection) if formal is not None else None,
                              'latest_differs_baseline': _remote_hash(remote, collection) != baseline.get('remote_hash'),
                              'latest_differs_accepted': _remote_hash(remote, collection) != accepted.get('remote_hash') if accepted else None}}
    token = {'current': note['sha256'], 'current_fields_hash': content_hash(local),
             'selection': selection, 'baseline': baseline,
             'accepted': accepted, 'pending': state['pending'].get(note_id),
             'latest': result['payload_latest'], 'formal': result['payload_formal']}
    result['review_token'] = content_hash(token)
    return result


def _redact(item, include_values):
    result = deepcopy(item)
    fields = result['merge'].pop('fields')
    result['merge']['proposed_fields_hash'] = content_hash(fields)
    if include_values: result['merge']['proposed_fields'] = fields
    if not include_values:
        for conflict in result['merge']['conflicts']:
            for side in ('base', 'local', 'remote'):
                if 'value' in conflict[side]:
                    value = conflict[side].pop('value')
                    conflict[side]['value_hash'] = content_hash(value)
    return result


def review_sync(library, client, note_ids=None, *, include_values=False):
    """Read current/selection/latest/formal/baseline without checkpoint writes.

    Whole-library reviews use complete collection pagination, not two GETs per
    document. Explicit short ID lists retain precise independent document reads.
    """
    state = _Store(library, client.target).load()
    library_state, notes = _index(library)
    requested = ([note_ids] if isinstance(note_ids, str) else list(note_ids)) if note_ids is not None else sorted(state['baselines'])
    snapshots = None
    if requested and (note_ids is None or len(requested) > 8):
        snapshots = {}
        for draft in (True, False):
            snapshots[draft] = {}
            for collection in ('works', 'posts'):
                rows = _call(client.list_collection, collection, draft=draft, trash=True)
                snapshots[draft][collection] = {str(row['id']): row for row in rows}
    items = []
    for note_id in requested:
        baseline = state['baselines'].get(note_id)
        if not baseline:
            items.append({'id': note_id, 'status': 'unmapped', 'current': {'sha256': notes[note_id]['sha256']} if note_id in notes else None})
            continue
        note = notes.get(note_id)
        record = library_state['notes'].get(note_id, {})
        if record.get('purged'):
            collection, remote_id = baseline['collection'], baseline['remote_id']
            if snapshots is not None:
                latest = snapshots[True][collection].get(str(remote_id))
                formal = snapshots[False][collection].get(str(remote_id))
            else:
                latest = _call(client.get, collection, remote_id, draft=True, trash=True)
                formal = _call(client.get, collection, remote_id, draft=False, trash=True)
            observed = {'works': [], 'posts': []}
            observed[collection] = [row for row in (latest, formal) if row is not None]
            _, lifecycle_results, _ = sync_lifecycle(
                library, client, _Store(library, client.target), state, observed, only_ids={note_id})
            result = lifecycle_results[0]
            items.append({'id': note_id,
                          'status': 'purged' if result['status'] == 'purged' else 'purged-review',
                          'baseline': {'state': baseline.get('state')},
                          **({'reason': result['reason']} if result.get('reason') else {})})
            continue
        if not note:
            items.append({'id': note_id, 'status': 'local-missing-review', 'baseline': {'state': baseline.get('state')}})
            continue
        collection, remote_id = baseline['collection'], baseline['remote_id']
        if snapshots is not None:
            remote = snapshots[True][collection].get(str(remote_id))
            formal = snapshots[False][collection].get(str(remote_id))
        else:
            remote = _call(client.get, collection, remote_id, draft=True, trash=True)
            formal = _call(client.get, collection, remote_id, draft=False, trash=True)
        if remote is None or formal is None:
            items.append({'id': note_id, 'status': 'mapped-remote-missing-review', 'current': {'sha256': note['sha256']}})
            continue
        if remote.get('deletedAt') or formal.get('deletedAt'):
            items.append({'id': note_id, 'status': 'deleted-review', 'current': {'sha256': note['sha256']},
                          'payload_latest': {'remote_id': remote_id, 'version': remote.get('updatedAt'), 'deleted_at': remote.get('deletedAt')},
                          'payload_formal': {'remote_id': remote_id, 'version': formal.get('updatedAt'), 'deleted_at': formal.get('deletedAt')}})
            continue
        _check_identity(baseline, remote)
        _check_identity(baseline, formal)
        items.append(_redact(prepare_review(library, state, note, remote, formal=formal, notes=notes, library_state=library_state), include_values))
    return {'schema_version': 1, 'mode': 'read-only-review', 'remote_writes': False,
            'publication_selected': False, 'items': items}


def _check_identity(baseline, row):
    if str(row.get('id')) != str(baseline['remote_id']) or not row.get('updatedAt'):
        raise TransportError('Remote observation lacks the mapped identity or version')
    if row.get('deletedAt'):
        raise TransportError('Deleted remote requires lifecycle review')


def accept_review(library, store, state, note, remote, item, *, resolutions=None, apply=False):
    """Internal primitive; caller holds synchronization lock when applying."""
    if item['pending']:
        raise TransportError('Pending write requires reconciliation before resolving a draft')
    selected = library._read_state()['publications'].get(note['id'])
    reviewed_selection = item.get('selection')
    if content_hash(selected) != (reviewed_selection['record_hash'] if reviewed_selection else content_hash(None)):
        raise TransportError('Publication selection changed during draft resolution; review again')
    if selected and selected.get('state') in {'deleted', 'withdrawn'}:
        raise TransportError('Withdrawal or deletion intent requires lifecycle reconciliation')
    merged = deepcopy(item['merge']['fields'])
    choices = dict(resolutions or {})
    required = {conflict['field'] for conflict in item['merge']['conflicts']}
    if set(choices) != required:
        raise TransportError('Resolutions must name exactly all conflicting fields')
    for conflict in item['merge']['conflicts']:
        choice = choices[conflict['field']]
        if isinstance(choice, str) and choice in {'base', 'local', 'remote'}:
            value = conflict[choice]
            if value.get('available') is False:
                raise TransportError('The common ancestor is unavailable; base cannot be selected')
        elif isinstance(choice, dict) and isinstance(choice.get('present'), bool) and set(choice) <= {'present', 'value'}:
            if choice['present'] and 'value' not in choice:
                raise TransportError('A present resolution requires a value')
            content_hash(choice)
            value = choice
        else:
            raise TransportError('Resolution requires local, remote, base, or an explicit presence/value')
        _set(merged, conflict['field'], value)
    markdown = render_merged(note, merged, item['collection'], state['baselines'], remote, library=library)
    properties, _ = _split(markdown)
    library._validate_properties(properties)
    if properties.get('id') != note['id'] or properties.get('kind') != note['properties']['kind']:
        raise TransportError('Resolution must preserve identity and kind')
    result = {'id': note['id'], 'status': 'resolution-available', 'applied': False,
              'sha256': _hash(markdown), 'remote_write': False, 'publication_selected': False,
              'received_fields': item['merge']['received_fields'], 'resolved_fields': sorted(choices)}
    if apply:
        extra = {}
        if item['collection'] == 'works':
            from .work_profile import profile, WEBSITE_ONLY
            retained = profile(library, note['id'])
            if retained is not None:
                retained = {key: value for key, value in retained.items()
                            if key not in WEBSITE_ONLY or key not in remote_fields(retained, 'works')}
                retained.update({key: value for key, value in merged.items() if key in WEBSITE_ONLY})
                if '_status' in remote:
                    retained['payload_observed_status'] = remote['_status']
                extra['website_metadata'] = retained
        imported = library.replace(note['id'], markdown, expected_hash=note['sha256'], **extra)
        observed = {'remote_hash': _remote_hash(remote, item['collection']), 'remote_version': remote['updatedAt'],
                    'remote_fields': remote_fields(remote, item['collection']),
                    'local_fields': local_fields(imported, item['collection'], state['baselines'], library=library),
                    'local_hash': imported['sha256'], 'selection_hash': content_hash(selected) if selected else None}
        observed['local_fields_hash'] = content_hash(observed['local_fields'])
        state['reverse_imports'][note['id']] = observed
        store.save(state)
        result.update({'status': 'remote-draft-imported', 'applied': True})
    return result


def resolve_sync(library, client, note_id, resolutions, *, expected_hash,
                 expected_remote_version, expected_review_token, apply=False):
    """Resolve a reviewed conflict with guards for current, selection and remote."""
    store = _Store(library, client.target)
    from contextlib import nullcontext
    with store.locked() if apply else nullcontext():
        state = store.load()
        library_state, notes = _index(library)
        note, baseline = notes.get(note_id), state['baselines'].get(note_id)
        if not note or not baseline:
            raise TransportError('Resolution requires an existing current draft and mapping')
        remote = client.get(baseline['collection'], baseline['remote_id'], draft=True)
        formal = client.get(baseline['collection'], baseline['remote_id'], draft=False)
        _check_identity(baseline, remote)
        _check_identity(baseline, formal)
        item = prepare_review(library, state, note, remote, formal=formal, notes=notes, library_state=library_state)
        if (note['sha256'] != expected_hash or remote.get('updatedAt') != expected_remote_version
                or not expected_review_token or item['review_token'] != expected_review_token):
            raise TransportError('Review expired: current draft, selection, baseline or remote changed')
        if apply:
            # Re-read after composing review; catch remote edits during review reads.
            fresh_formal = client.get(baseline['collection'], baseline['remote_id'], draft=False)
            latest = client.get(baseline['collection'], baseline['remote_id'], draft=True)
            _check_identity(baseline, fresh_formal)
            _check_identity(baseline, latest)
            if (latest.get('updatedAt') != remote.get('updatedAt')
                    or _remote_hash(latest, baseline['collection']) != _remote_hash(remote, baseline['collection'])
                    or fresh_formal.get('updatedAt') != formal.get('updatedAt')
                    or _remote_hash(fresh_formal, baseline['collection']) != _remote_hash(formal, baseline['collection'])):
                raise TransportError('Remote latest or formal changed during resolution; review again')
        return accept_review(library, store, state, note, remote, item, resolutions=resolutions, apply=apply)
