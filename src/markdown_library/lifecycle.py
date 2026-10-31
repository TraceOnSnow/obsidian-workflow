"""Explicit, recoverable deletion over Payload's standard native Trash API.

A missing record is never a tombstone. Receipts retain mappings; uncertain writes
are read back before retry. Restore always produces an unpublished working draft.
"""
from copy import deepcopy
import inspect
import json

from .core import LibraryError, _hash, _now, _purge_proof, _read, _split
from .sync import _project, _remote_hash, content_hash


def _call(method, *args, **kwargs):
    # Existing offline fixture/adapter clients need not implement Trash queries.
    if 'trash' not in inspect.signature(method).parameters:
        kwargs.pop('trash', None)
    return method(*args, **kwargs)


def _content(row, collection):
    fields = _project(row, collection)
    fields.pop('_status', None)
    fields.pop('visibility', None)
    return fields


def _local(library, note_id, record):
    if not record:
        raise LibraryError('Mapped library identity is missing from retained state; review integrity before lifecycle changes')
    if record.get('deleted'):
        markdown = _read(library._path(record['trash']))
        return {'sha256': _hash(markdown), 'markdown': markdown, 'path': record['path'], 'deleted': True}
    from .work_catalog import enabled
    if record.get('kind') == 'work' and enabled(library._read_state()):
        from .bootstrap import _index
        note = _index(library)[1].get(note_id)
        if note is None:
            raise LibraryError('Catalog row is missing; no deletion inferred')
        library._assert_note_storage(note)
        return {'sha256': note['sha256'], 'markdown': note['markdown'], 'path': note['path'], 'deleted': False}
    paths = library._registration_identity_paths(note_id)
    if len(paths) != 1:
        raise LibraryError('Local working file is missing or identity is ambiguous; no deletion inferred')
    markdown = _read(library._path(paths[0]))
    properties, _ = _split(markdown)
    library._validate_properties(properties)
    return {'sha256': _hash(markdown), 'markdown': markdown, 'path': paths[0], 'deleted': False}


def _expected_local(baseline, state, note_id, library_state):
    return (state.get('reverse_imports', {}).get(note_id, {}).get('local_hash')
            or baseline.get('draft_sha256') or baseline.get('imported_sha256')
            or library_state['publications'].get(note_id, {}).get('sha256'))


def _review_token(note_id, current, remote, baseline, intent):
    return content_hash({'id': note_id, 'local': current['sha256'], 'deleted': current['deleted'],
                         'remote': remote, 'baseline': baseline, 'intent': intent})


def _save_remote_evidence(library, note_id, collection, remote):
    digest = content_hash(remote)
    library._atomic(f'.markdown-library/lifecycle/{note_id}/{digest}.json',
                    json.dumps({'collection': collection, 'document': remote}, ensure_ascii=False, indent=2))


def _observe(library, store, state, note_id, baseline, remote, current, token):
    deleted = bool(remote.get('deletedAt'))
    state.setdefault('lifecycle', {})[note_id] = {
        'token': token, 'deleted': deleted, 'remote_hash': _remote_hash(remote, baseline['collection']),
        'content_hash': content_hash(_content(remote, baseline['collection'])),
        'remote_version': remote['updatedAt'], 'local_sha256': current['sha256'],
        'observed_at': _now(), 'deleted_at': remote.get('deletedAt'),
    }
    state.setdefault('lifecycle_pending', {}).pop(note_id, None)
    if deleted:
        baseline.update({'state': 'deleted', 'remote_hash': _remote_hash(remote, baseline['collection']),
                         'remote_version': remote['updatedAt']})
    else:
        baseline.update({'remote_hash': _remote_hash(remote, baseline['collection']),
                         'remote_version': remote['updatedAt'], 'draft_sha256': current['sha256']})
        # Restoration is an observation of a private draft, never publication.
        if note_id not in library._read_state()['publications']:
            baseline['state'] = 'imported-draft'
        else:
            baseline['state'] = 'withdrawn'
        state.get('reverse_imports', {}).pop(note_id, None)
        from .merge import local_fields, remote_fields
        properties, _ = _split(current['markdown'])
        baseline['remote_fields'] = remote_fields(remote, baseline['collection'])
        baseline['local_fields'] = local_fields({'properties': properties, 'markdown': current['markdown']},
                                              baseline['collection'], state['baselines'], library=library)
        baseline['local_fields_hash'] = content_hash(baseline['local_fields'])
    store.save(state)


def sync_lifecycle(library, client, store, state, records, *, apply=False, accept_remote=False, decisions=None, only_ids=None):
    """Return (actions, results, handled_ids), using the caller's complete snapshot.

    `decisions` is only populated by resolve_lifecycle after guarded explicit
    review. The ordinary timer never resolves deletion-versus-edit conflicts.
    """
    library_state = library._read_state()
    intents = library_state['notes']
    receipts = state.get('lifecycle', {})
    actions, results, handled = [], [], set()
    for note_id, baseline in sorted(state['baselines'].items()):
        if only_ids is not None and note_id not in only_ids:
            continue
        collection = baseline['collection']
        record = intents.get(note_id, {})
        intent = record.get('lifecycle', {})
        receipt = receipts.get(note_id, {})
        remote = next((row for row in records[collection] if str(row['id']) == str(baseline['remote_id'])), None)
        remote_deleted = bool(remote and remote.get('deletedAt'))
        needs_restore = intent.get('intent') == 'restore' and intent.get('token') != receipt.get('token')
        if not (record.get('deleted') or remote_deleted or receipt.get('deleted') or needs_restore):
            continue
        handled.add(note_id)
        action = {'id': note_id, 'collection': collection, 'remote_id': baseline['remote_id'],
                  'action': 'conflict', 'reason': 'lifecycle-review-required', 'applied': False,
                  'payload': {}, 'blocked_by': [], 'ready_for_executor': False}
        result = {'id': note_id, 'status': 'conflict', 'applied': False}
        actions.append(action); results.append(result)
        try:
            if record.get('purged'):
                proof = _purge_proof(record.get('remote_proof'))
                if (not record.get('deleted') or proof['target'] != state.get('target')
                        or proof['collection'] != collection
                        or proof['remote_id'] != str(baseline['remote_id'])
                        or proof['previous_remote_version'] != baseline.get('remote_version')
                        or baseline.get('state') != 'deleted'
                        or not receipt.get('deleted') or receipt.get('token') != intent.get('token')
                        or receipt.get('local_sha256') != record.get('sha256')):
                    raise LibraryError('Permanent-deletion tombstone does not match the retained mapping')
                if remote is not None:
                    raise LibraryError('Permanently deleted remote reappeared; no automatic restore or recreation')
                if (decisions or {}).get(note_id):
                    raise LibraryError('Permanent deletion cannot be restored through lifecycle resolution')
                action.update({'action': 'unchanged', 'reason': 'explicit-permanent-deletion-observed'})
                result['status'] = 'purged'
                continue
            if remote is None:
                raise LibraryError('Mapped remote is missing; preserve mapping and review restoration; no delete or recreation inferred')
            if not remote.get('updatedAt'):
                raise LibraryError('Remote lifecycle requires updatedAt')
            if 'deletedAt' not in remote:
                raise LibraryError('Native Trash capability is not observed; upgrade and migrate the backend before deletion propagation')
            current = _local(library, note_id, record)
            token = _review_token(note_id, current, remote, baseline, intent)
            action.update({'review_token': token, 'expected_hash': current['sha256'],
                           'expected_remote_version': remote['updatedAt'],
                           'local_deleted': current['deleted'], 'remote_deleted': remote_deleted})
            decision = (decisions or {}).get(note_id)
            if decision and decision['review_token'] != token:
                raise LibraryError('Lifecycle review is stale; review again')
            if note_id in state.get('pending', {}):
                raise LibraryError('A publication write outcome is pending; reconcile it before deletion')
            pending = state.get('lifecycle_pending', {}).get(note_id)
            if pending:
                if remote_deleted == pending['deleted'] and content_hash(_content(remote, collection)) == pending['content_hash']:
                    formal = _call(client.get, collection, baseline['remote_id'], draft=False, trash=True)
                    if (bool(formal.get('deletedAt')) != pending['deleted']
                            or content_hash(_content(formal, collection)) != pending['content_hash']
                            or (not pending['deleted'] and (formal.get('_status') != 'draft' or remote.get('_status') != 'draft'))
                            or (not pending['deleted'] and collection == 'works' and (formal.get('visibility') != 'private' or remote.get('visibility') != 'private'))):
                        raise LibraryError('Uncertain lifecycle write is not independently observed in both latest and formal records; no retry')
                    if apply:
                        _observe(library, store, state, note_id, baseline, remote, current, pending['token'])
                    action.update({'action': 'unchanged', 'reason': 'uncertain-lifecycle-write-independently-observed'})
                    result['status'] = 'reconciled' if apply else 'matching-observation'
                    continue
                raise LibraryError('Previous lifecycle write is uncertain; outcome differs and no write was retried')
            if decision:
                want_deleted = decision['decision'] == 'delete'
                origin = 'review'
            elif current['deleted']:
                if remote_deleted:
                    want_deleted, origin = True, 'converged'
                elif receipt.get('deleted'):
                    want_deleted, origin = False, 'website'
                elif intent.get('origin') == 'website':
                    raise LibraryError('Website deletion event changed before observation; review required')
                else:
                    want_deleted, origin = True, 'local'
            elif needs_restore:
                want_deleted, origin = False, 'local'
            elif remote_deleted:
                want_deleted, origin = True, 'website'
            else:
                raise LibraryError('Lifecycle states diverged; explicit review required')
            expected_remote = (state.get('reverse_imports', {}).get(note_id, {}).get('remote_hash')
                               or receipt.get('remote_hash') or baseline['remote_hash'])
            if not decision and origin == 'converged':
                if _remote_hash(remote, collection) != expected_remote:
                    raise LibraryError('Remote edited while local content is deleted; explicit lifecycle review required')
                if receipt.get('deleted') and receipt.get('token') == intent.get('token'):
                    action.update({'action': 'unchanged', 'reason': 'recoverable-deletion-already-observed'})
                    result['status'] = 'unchanged'
                    continue
            if not decision and origin == 'local' and _remote_hash(remote, collection) != expected_remote:
                raise LibraryError('Remote edited after the shared observation; explicit delete/restore versus edit review required')
            if not decision and origin == 'website' and not current['deleted']:
                expected_local = _expected_local(baseline, state, note_id, library_state)
                if current['sha256'] != expected_local:
                    raise LibraryError('Local edited before website deletion; explicit delete versus edit review required')
                if _remote_hash(remote, collection) != expected_remote:
                    raise LibraryError('Remote content changed with deletion; retain and review both drafts')
            if not want_deleted and origin == 'website' and (remote.get('_status') != 'draft' or (collection == 'works' and remote.get('visibility') != 'private')):
                raise LibraryError('Remote restoration must be an unpublished private draft; publication requires separate review')
            if not decision and origin == 'website' and current['deleted']:
                expected_content = receipt.get('content_hash')
                if expected_content and content_hash(_content(remote, collection)) != expected_content:
                    raise LibraryError('Remote restored with content changes; review retained local trash and website draft')
            action.update({'action': 'delete' if want_deleted else 'restore',
                           'reason': f'explicit-{origin}-recoverable-lifecycle',
                           'ready_for_executor': origin != 'website' or accept_remote})
            result['status'] = 'lifecycle-available'
            if not apply or (origin == 'website' and not accept_remote):
                continue
            # Re-read immediately before mutation; never advance from a plan.
            fresh = _call(client.get, collection, baseline['remote_id'], draft=True, trash=True)
            if fresh != remote:
                raise LibraryError('Remote changed during lifecycle operation; review again')
            latest_local = _local(library, note_id, record)
            if latest_local['sha256'] != current['sha256'] or latest_local['deleted'] != current['deleted']:
                raise LibraryError('Local changed during lifecycle operation; review again')
            _save_remote_evidence(library, note_id, collection, remote)
            if current['deleted'] != want_deleted:
                if want_deleted:
                    library.delete(note_id, expected_hash=current['sha256'], origin='website',
                                   event={'remote_id': baseline['remote_id'], 'version': remote['updatedAt'], 'deleted_at': remote.get('deletedAt')})
                else:
                    library.recover(note_id, expected_hash=current['sha256'], origin='website' if origin == 'website' else 'local')
                record = library._read_state()['notes'][note_id]
                current = _local(library, note_id, record)
                intent = record['lifecycle']
            if remote_deleted != want_deleted:
                if not hasattr(client, 'update'):
                    raise LibraryError('Client does not support native Trash updates')
                base = _call(client.get, collection, baseline['remote_id'], draft=False, trash=True)
                fresh = _call(client.get, collection, baseline['remote_id'], draft=True, trash=True)
                if fresh != remote:
                    raise LibraryError('Remote changed while checking base version; review again')
                fields = _project(remote, collection)
                fields['deletedAt'] = _now() if want_deleted else None
                if not want_deleted:
                    fields['_status'] = 'draft'
                    if collection == 'works': fields['visibility'] = 'private'
                state.setdefault('lifecycle_pending', {})[note_id] = {
                    'token': intent.get('token'), 'deleted': want_deleted,
                    'content_hash': content_hash(_content(remote, collection)), 'remote_id': baseline['remote_id']}
                store.save(state)
                _call(client.update, collection, baseline['remote_id'], fields,
                      expected_version=remote.get('updatedAt') if want_deleted else base.get('updatedAt'), draft=False, trash=True)
            observed = _call(client.get, collection, baseline['remote_id'], draft=True, trash=True)
            formal = _call(client.get, collection, baseline['remote_id'], draft=False, trash=True)
            for row in (observed, formal):
                if (bool(row.get('deletedAt')) != want_deleted
                        or content_hash(_content(row, collection)) != content_hash(_content(remote, collection))
                        or (not want_deleted and row.get('_status') != 'draft')
                        or (not want_deleted and collection == 'works' and row.get('visibility') != 'private')):
                    raise LibraryError('Independent lifecycle read-back differs; outcome remains pending')
            _observe(library, store, state, note_id, baseline, observed, current, intent.get('token'))
            result.update({'status': 'deleted-observed' if want_deleted else 'restored-draft-observed', 'applied': True})
        except LibraryError as exc:
            action.update({'action': 'conflict', 'reason': str(exc), 'ready_for_executor': False})
            result.update({'status': 'reconciliation-required' if note_id in state.get('lifecycle_pending', {}) else 'conflict', 'reason': str(exc)})
    return actions, results, handled


def review_lifecycle(library, client, note_ids=None):
    from .transport import _Store
    store = _Store(library, client.target)
    state = store.load()
    records = {name: _call(client.list_collection, name, draft=True, trash=True) for name in ('works', 'posts')}
    actions, _, _ = sync_lifecycle(library, client, store, state, records)
    return {'mode': 'read-only-lifecycle-review', 'items': [row for row in actions if note_ids is None or row['id'] in note_ids]}


def resolve_lifecycle(library, client, note_id, decision, *, expected_hash, expected_remote_version, expected_review_token, apply=False):
    """Explicit conflict choice guarded by the exact local/remote review token."""
    from .transport import _Store
    if decision not in {'delete', 'restore'}:
        raise LibraryError('Lifecycle decision must be delete or restore')
    store = _Store(library, client.target)
    with store.locked():
        state = store.load()
        records = {name: _call(client.list_collection, name, draft=True, trash=True) for name in ('works', 'posts')}
        preview, _, _ = sync_lifecycle(library, client, store, state, records)
        item = next((row for row in preview if row['id'] == note_id), None)
        if not item or (item.get('expected_hash'), item.get('expected_remote_version'), item.get('review_token')) != (expected_hash, expected_remote_version, expected_review_token):
            raise LibraryError('Lifecycle review is stale or unavailable; review again')
        actions, results, _ = sync_lifecycle(library, client, store, state, records, apply=apply, accept_remote=True,
                                          decisions={note_id: {'decision': decision, 'review_token': expected_review_token}}, only_ids={note_id})
        return {'mode': 'resolve-lifecycle', 'action': next(row for row in actions if row['id'] == note_id),
                'result': next(row for row in results if row['id'] == note_id)}
