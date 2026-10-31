"""Guarded daily-card selection for explicitly migrated editable Work catalogs.

Migration records the current daily values as an origin. Only subsequent edits
of those fields authorize a new selection, and only an already selected, mapped,
public Work is eligible. Website metadata comes from fresh verified remote
observations. The existing transport owns all network mutations and receipts.
"""
from copy import deepcopy

from .core import LibraryError, _now
from .sync import content_hash, export_publication, _remote_hash
from .transport import _Store, _resolve_relationships, sync_once

DAILY_FIELDS = frozenset({'rating', 'note', 'status', 'experiences'})


def daily_origin(note_or_properties):
    """Retain sparse current values at migration; do not select or publish them."""
    properties = note_or_properties.get('properties', note_or_properties)
    fields = {key: deepcopy(properties[key]) for key in DAILY_FIELDS if key in properties}
    return {'fields': fields, 'fields_hash': content_hash(fields)}


def _origin_fields(origin):
    if (not isinstance(origin, dict) or not isinstance(origin.get('fields'), dict)
            or set(origin['fields']) - DAILY_FIELDS
            or content_hash(origin['fields']) != origin.get('fields_hash')):
        raise LibraryError('Catalog daily origin is missing or invalid; preserve state and review')
    return origin['fields']


def _changed(origin, note):
    from .merge import slot
    current = daily_origin(note)['fields']
    return sorted(key for key in DAILY_FIELDS if slot(origin, key) != slot(current, key))


def _same_observation(before, after):
    return (str(before.get('id')) == str(after.get('id'))
            and before.get('updatedAt') == after.get('updatedAt')
            and _remote_hash(before, 'works') == _remote_hash(after, 'works')
            and before.get('deletedAt') == after.get('deletedAt'))


def _fresh_pair(client, baseline, latest, formal):
    from .review import _check_identity
    fresh_latest = client.get('works', baseline['remote_id'], draft=True)
    fresh_formal = client.get('works', baseline['remote_id'], draft=False)
    _check_identity(baseline, fresh_latest)
    _check_identity(baseline, fresh_formal)
    if not (_same_observation(latest, fresh_latest) and _same_observation(formal, fresh_formal)):
        raise LibraryError('Catalog remote observation changed; review again')


def _update_origin(library, note_id, note, previous, *, received=(), select=None,
                   expected_selection=None):
    """Commit origin/selection together while guarding current logical Work SHA."""
    from .merge import slot, _set
    with library._lock():
        state = library._load()
        notes = library._scan(state)
        current = notes.get(note_id)
        config = state.get('work_catalog', {})
        origins = config.get('daily_origins', {})
        if (not current or current['sha256'] != note['sha256']
                or origins.get(note_id) != previous):
            raise LibraryError('Catalog row changed during synchronization; review again')
        if select is not None:
            if state['publications'].get(note_id) != expected_selection:
                raise LibraryError('Publication selection changed during catalog synchronization')
            state['publications'][note_id] = deepcopy(select)
            origins[note_id] = daily_origin(current)
        else:
            fields = deepcopy(_origin_fields(previous))
            current_fields = daily_origin(current)['fields']
            for key in set(received) & DAILY_FIELDS:
                _set(fields, key, slot(current_fields, key))
            origins[note_id] = {'fields': fields, 'fields_hash': content_hash(fields)}
        from .work_catalog import assert_storage
        assert_storage(library, current)
        library._save(state)
        return deepcopy(origins[note_id])


def _prepare_catalog(library, client, *, apply, accept_remote, only_ids):
    from .bootstrap import _index
    from .merge import remote_fields
    from .review import prepare_review, accept_review, _check_identity

    store = _Store(library, client.target)
    outcomes = []
    with store.locked():
        sync_state = store.load()
        library_state, notes = _index(library)
        config = library_state.get('work_catalog', {})
        origins = config.get('daily_origins', {})
        rows = config.get('rows', {})
        candidates = [note_id for note_id in sorted(rows)
                      if note_id in notes and notes[note_id].get('catalog_backed')
                      and notes[note_id]['properties'].get('kind') == 'work'
                      and note_id in sync_state['baselines']
                      and (only_ids is None or note_id in only_ids)]
        if not candidates:
            return outcomes
        # Complete snapshots keep a normal timer run bounded to collection reads.
        latest_rows = {str(row['id']): row for row in client.list_collection('works', draft=True, trash=True)}
        formal_rows = {str(row['id']): row for row in client.list_collection('works', draft=False, trash=True)}
        for note_id in candidates:
            note = notes[note_id]
            selected = library_state['publications'].get(note_id)
            baseline = sync_state['baselines'][note_id]
            if baseline.get('collection') != 'works':
                continue
            # New/private/unselected records never acquire publication intent here.
            if (not selected or selected.get('kind') != 'work' or selected.get('state') != 'published'
                    or baseline.get('state') != 'published'):
                continue
            outcome = {'id': note_id, 'status': 'unchanged', 'applied': False, 'phase': 'catalog-daily-selection'}
            try:
                previous = deepcopy(origins.get(note_id))
                if previous is None:
                    # A newly added row is not implicitly authorized for publication.
                    continue
                origin = _origin_fields(previous)
                changed = _changed(origin, note)
                if note_id in sync_state['pending'] or note_id in sync_state.get('lifecycle_pending', {}):
                    if changed:
                        outcome.update(status='reconciliation-required', reason='catalog-selection-awaits-pending-outcome')
                        outcomes.append(outcome)
                    continue
                latest = latest_rows.get(str(baseline['remote_id']))
                formal = formal_rows.get(str(baseline['remote_id']))
                if latest is None or formal is None:
                    if changed:
                        outcome.update(status='conflict', reason='catalog-mapped-remote-missing')
                        outcomes.append(outcome)
                    continue
                _check_identity(baseline, latest)
                _check_identity(baseline, formal)
                accepted = sync_state['reverse_imports'].get(note_id)
                comparison_hash = (accepted or {}).get('remote_hash', baseline.get('remote_hash'))
                remote_changed = _remote_hash(latest, 'works') != comparison_hash
                if not changed and not remote_changed:
                    continue
                # Always review current, selection, verified ancestors and both
                # remote views before forming any replacement publication.
                item = prepare_review(library, sync_state, note, latest, formal=formal,
                                      notes=notes, library_state=library_state)
                if not item['merge']['base_available'] or item['merge']['conflicts']:
                    # Existing remote conflicts belong to the ordinary sync
                    # review. With no new daily edit there is no catalog
                    # selection to reject or additional incident to report.
                    if changed:
                        outcome.update(status='conflict', reason='catalog-three-way-review-required',
                                       fields_requiring_review=[entry['field'] for entry in item['merge']['conflicts']])
                        outcomes.append(outcome)
                    continue
                if remote_changed:
                    if not (apply and accept_remote):
                        outcome.update(status='remote-draft-available', reason='catalog-remote-observation-must-be-accepted-first')
                        outcomes.append(outcome)
                        continue
                    _fresh_pair(client, baseline, latest, formal)
                    accepted_result = accept_review(library, store, sync_state, note, latest, item, apply=True)
                    note = library.get(note_id)
                    if note['sha256'] != accepted_result['sha256']:
                        raise LibraryError('Catalog row changed after remote import')
                    # Only remotely received fields advance their local origin;
                    # independent local daily edits remain eligible below.
                    previous = _update_origin(library, note_id, note, previous,
                                              received=accepted_result['received_fields'])
                    library_state, notes = _index(library)
                    note = notes[note_id]
                    origins = library_state['work_catalog']['daily_origins']
                    origin = _origin_fields(previous)
                    changed = _changed(origin, note)
                    outcome.update(status='remote-draft-imported', applied=True)
                    if not changed:
                        outcomes.append(outcome)
                        continue
                if not changed:
                    continue
                outcome['daily_fields'] = changed
                # A website draft may include unrelated unpublished work. Do not
                # promote it, alter visibility, or publish any associated Post.
                if any(row.get('_status') != 'published' or row.get('visibility') != 'public'
                       for row in (latest, formal)):
                    outcome.update(status='blocked', reason='catalog-requires-current-and-formal-public-work')
                    outcomes.append(outcome)
                    continue
                if _remote_hash(latest, 'works') != _remote_hash(formal, 'works'):
                    outcome.update(status='blocked', reason='catalog-latest-and-formal-differ')
                    outcomes.append(outcome)
                    continue
                # Never replace a previously selected but unobserved intent.
                selected_export = export_publication(_resolve_relationships(selected, sync_state['baselines']))
                if selected_export['content_hash'] != baseline.get('local_hash'):
                    outcome.update(status='blocked', reason='catalog-existing-selection-awaits-observation')
                    outcomes.append(outcome)
                    continue
                fields = remote_fields(latest, 'works')
                for key in changed:
                    # Empty cells are explicit daily-field clears. Missing
                    # website metadata never becomes a clear operation.
                    fields[key] = deepcopy(note['properties'].get(key, [] if key == 'experiences' else None))
                selection = {
                    'id': note_id, 'kind': 'work', 'state': 'published', 'sha256': note['sha256'],
                    'snapshot': {'id': note_id, 'kind': 'work', 'title': latest['title'], 'properties': fields},
                    'published_at': _now(),
                }
                export_publication(selection)  # Validate before durable intent.
                if apply:
                    _fresh_pair(client, baseline, latest, formal)
                    _update_origin(library, note_id, note, previous, select=selection,
                                   expected_selection=selected)
                    library_state, notes = _index(library)
                    origins = library_state['work_catalog']['daily_origins']
                outcome.update(status='catalog-selected' if apply else 'catalog-selection-available', applied=apply)
                outcomes.append(outcome)
            except (LibraryError, ValueError, KeyError, TypeError):
                # Remote prose and credentials must never enter routine receipts.
                outcome.update(status='conflict', reason='catalog-observation-or-row-changed-review-required')
                outcomes.append(outcome)
    return outcomes


def _acknowledge_imports(library, results):
    """Remote imports by the ordinary transport are observations, not table edits."""
    for result in results:
        if result.get('status') != 'remote-draft-imported' or not result.get('received_fields'):
            continue
        note_id = result['id']
        state = library._read_state()
        config = state.get('work_catalog', {})
        origin = config.get('daily_origins', {}).get(note_id)
        if origin is None or note_id not in config.get('rows', {}):
            continue
        note = library.get(note_id)
        if note['sha256'] != result.get('sha256'):
            continue
        _update_origin(library, note_id, note, origin, received=result['received_fields'])


def sync_catalog(library, client, *, apply=False, works_drafts_and_versions=False,
                 accept_remote=False, resume_pending=(), only_ids=None):
    """Run catalog preflight, then the existing guarded publication transport.

    Inactive libraries use exactly their existing sync behavior. Unknown pending
    writes are never resumed automatically. All private metadata and bodies stay
    outside the daily-card authorization boundary.
    """
    active = library._read_state().get('work_catalog')
    if not active:
        return sync_once(library, client, apply=apply, works_drafts_and_versions=works_drafts_and_versions,
                         accept_remote=accept_remote, resume_pending=resume_pending, only_ids=only_ids)
    if only_ids is not None:
        if (not isinstance(only_ids, (list, tuple, set, frozenset)) or not only_ids
                or any(not isinstance(value, str) or not value for value in only_ids)):
            raise LibraryError('only_ids must be a nonempty collection of library IDs')
        if set(only_ids) - library._read_state()['notes'].keys():
            raise LibraryError('Sync scope contains unknown library IDs')
        if set(resume_pending) - set(only_ids):
            raise LibraryError('Pending resumes must be inside the sync scope')
    selections = _prepare_catalog(library, client, apply=apply, accept_remote=accept_remote, only_ids=only_ids)
    receipt = sync_once(library, client, apply=apply, works_drafts_and_versions=works_drafts_and_versions,
                        accept_remote=accept_remote, resume_pending=resume_pending, only_ids=only_ids)
    if apply:
        _acknowledge_imports(library, receipt['results'])
    receipt['catalog_selection'] = {'results': selections, 'new_publications': False,
                                    'associated_posts_selected': False}
    unresolved = {'conflict', 'blocked', 'reconciliation-required'}
    # Catalog preflight and the executor can observe the same existing issue.
    # Keep detailed preflight evidence separately without counting it twice in
    # the ordinary per-ID result stream used by the timer/status dashboard.
    reported = {row['id'] for row in receipt['results'] if row['status'] in unresolved}
    for row in selections:
        if row['status'] in unresolved and row['id'] not in reported:
            receipt['results'].append(row)
            reported.add(row['id'])
    return receipt
