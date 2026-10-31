"""Conservative field comparison for local drafts and observed Payload data.

Missing fields have explicit presence; a hash without retained values is never a
common ancestor. Work bodies never enter this module's public projection.
"""
from copy import deepcopy
import re

from .core import LibraryError, _split
from .publication_navigation import strip_publication_navigation
from .sync import _project, _remote_hash, content_hash

OBSERVED = frozenset({'_status', 'visibility'})
REVIEW = frozenset({'article', 'cover', 'frontmatter', 'protectedContent', 'password',
                    'slug', 'link', 'locale', 'type'})


def remote_fields(row, collection):
    result = _project(row, collection)
    if collection == 'posts':
        for key in ('protectedContent', 'password'):
            if key in row:
                result[key] = deepcopy(row[key])
    return result


def local_fields(note, collection, baselines=None, *, library=None, notes=None, include_retained=True):
    properties = note['properties']
    if collection == 'works' and library is not None and include_retained:
        from .work_profile import website_properties
        properties = website_properties(library, note)
    fields = remote_fields(properties, collection)
    if collection == 'posts':
        fields['markdown'] = strip_publication_navigation(_split(note['markdown'])[1])
        if note['properties'].get('payload_markdown_is_null') and not fields['markdown']:
            fields['markdown'] = None
    if collection == 'works' and fields.get('article') is not None and library is not None:
        from .relations import canonical_article
        fields['article'] = canonical_article(library, fields['article'], notes=notes)
    if collection == 'works' and baselines:
        article = fields.get('article')
        if isinstance(article, str) and article in baselines:
            relation = baselines[article]
            if relation.get('collection') == 'posts':
                fields['article'] = relation['remote_id']
    return fields


def slot(fields, key):
    return {'present': key in fields, **({'value': deepcopy(fields[key])} if key in fields else {})}


def _set(fields, key, value):
    if value['present']:
        fields[key] = deepcopy(value.get('value'))
    else:
        fields.pop(key, None)


def merge_fields(base_local, base_remote, local, remote):
    """Compare against each side's verified observation (server defaults differ).

    Compound values are atomic and require review if received from Payload.
    Local-only compound edits are retained, without approving them for publishing.
    """
    known = base_local is not None and base_remote is not None
    merged, conflicts, received, kept = deepcopy(local), [], [], []
    local_changes, remote_changes = [], []
    for key in sorted((set(local) | set(remote) | set(base_remote or {}) | set(base_local or {})) - OBSERVED):
        left, right = slot(local, key), slot(remote, key)
        before_left = slot(base_local or {}, key)
        before_right = slot(base_remote or {}, key)
        # Absent server defaults were never owned by the local snapshot.
        if known and key not in base_local and key not in local:
            left = before_right
        # A local value deliberately retained against an accepted remote value
        # remains an unpublished local change until the two sides converge.
        local_changed = left != before_left or (known and before_left != before_right)
        if known and key not in base_local and (key not in local or left == before_right):
            local_changed = False
        remote_changed = right != before_right
        if known and local_changed: local_changes.append(key)
        if known and remote_changed: remote_changes.append(key)
        if left == right:
            continue
        if known and not remote_changed:
            if local_changed:
                kept.append(key)
            continue
        compound = any(isinstance(value.get('value'), (list, dict)) for value in (left, right, before_right))
        if not known:
            reason = 'base-unavailable'
        elif (key in REVIEW or compound or
              (key == 'markdown' and isinstance(right.get('value'), str) and
               re.search(r'^[ \t]*:{3,}[ \t]*encrypted\b', right['value'], re.MULTILINE))):
            reason = 'structured-or-protected-field-review'
        elif local_changed:
            reason = 'same-field-changed'
        else:
            _set(merged, key, right)
            received.append(key)
            continue
        conflicts.append({'field': key, 'reason': reason,
                          'base': before_right if known else {'available': False},
                          'local': slot(local, key), 'remote': right})
    return {'status': 'conflict' if conflicts else 'mergeable', 'base_available': known,
            'fields': merged, 'conflicts': conflicts, 'received_fields': received,
            'retained_local_fields': kept, 'local_changed_fields': local_changes if known else None,
            'remote_changed_fields': remote_changes if known else None}


def recover_base(library, note_id, baseline, accepted, baselines, *, library_state=None, notes=None):
    """Recover only exact, hash-proven local ancestors; never invent remote values."""
    if accepted and accepted.get('remote_fields') is not None and accepted.get('local_fields') is not None:
        if (_remote_hash(accepted['remote_fields'], baseline['collection']) == accepted.get('remote_hash')
                and content_hash(accepted['local_fields']) == accepted.get('local_fields_hash')):
            return deepcopy(accepted['local_fields']), deepcopy(accepted['remote_fields']), 'accepted-observation'
        return None, None, 'accepted-base-values-unverified'
    if accepted and accepted.get('remote_hash') != baseline.get('remote_hash'):
        return None, None, 'accepted-base-values-unavailable'
    if baseline.get('remote_fields') is not None and baseline.get('local_fields') is not None:
        validation_fields = deepcopy(baseline['local_fields'])
        if baseline.get('state') == 'imported-draft':
            validation_fields['_status'] = 'published'
            if baseline['collection'] == 'works': validation_fields['visibility'] = 'public'
        valid_local = (content_hash(baseline['local_fields']) == baseline['local_fields_hash']
                       if baseline.get('local_fields_hash') else content_hash(validation_fields) == baseline.get('local_hash'))
        if (_remote_hash(baseline['remote_fields'], baseline['collection']) == baseline.get('remote_hash')
                and valid_local):
            return deepcopy(baseline['local_fields']), deepcopy(baseline['remote_fields']), 'retained-observation'
        return None, None, 'retained-base-values-unverified'
    collection = baseline['collection']
    # Published snapshots and retained history may prove old projections. Both
    # hashes must match; matching only the selection does not prove server defaults.
    state = library_state if library_state is not None else library._read_state()
    candidates = []
    publication = state['publications'].get(note_id)
    if publication:
        snapshot = publication.get('snapshot', {})
        fields = remote_fields(snapshot.get('properties', {}), collection)
        fields['title'] = snapshot.get('title')
        if collection == 'posts':
            fields['markdown'] = strip_publication_navigation(snapshot.get('markdown', ''))
        fields['_status'] = 'published'
        if collection == 'works': fields['visibility'] = 'public'
        candidates.append(fields)
    for revision in state['notes'].get(note_id, {}).get('history', []):
        digest = revision['sha256']
        path = library._path(f'.markdown-library/objects/{digest}.md')
        if not path.exists(): continue
        from .core import _hash, _read
        markdown = _read(path)
        if _hash(markdown) != digest: continue
        try:
            from .ratings import canonical_revision
            markdown = canonical_revision(library, note_id, digest, markdown, state=state)
            properties, _ = _split(markdown)
            if properties.get('id') != note_id: continue
            fields = local_fields({'id': note_id, 'properties': properties, 'markdown': markdown}, collection, baselines,
                                 library=library, notes=notes, include_retained=False)
        except LibraryError:
            # Old wikilinks may stop resolving; a reused historical SHA may
            # also have ambiguous rating scales. Neither proves this ancestor.
            # An old wikilink can stop resolving after its Post is archived.
            # It cannot prove this ancestor; keep checking other exact hashes.
            # Current draft relationships are still validated by the caller.
            continue
        fields['_status'] = baseline.get('observed_status', 'published')
        if collection == 'works': fields['visibility'] = baseline.get('observed_visibility', 'public')
        candidates.append(fields)
    for fields in candidates:
        # Both ancestor projections must be provable; no fake sparse defaults.
        selected_fields = _project(fields, collection)
        selected_fields['_status'] = 'published'
        if collection == 'works': selected_fields['visibility'] = 'public'
        if content_hash(selected_fields) == baseline.get('local_hash') and _remote_hash(fields, collection) == baseline.get('remote_hash'):
            return deepcopy(selected_fields), deepcopy(fields), 'exact-history-hashes'
    return None, None, 'base-unavailable'


def render_merged(current, fields, collection, baselines, remote, *, library=None, notes=None):
    from .core import _render
    from .sync import POST_FIELDS, WORK_FIELDS
    properties, body = _split(current['markdown'])
    managed = (WORK_FIELDS if collection == 'works' else POST_FIELDS | {'protectedContent', 'password'}) - OBSERVED
    # Relationships remain stable library identities when a proven mapping exists.
    fields = deepcopy(fields)
    previous = properties
    if collection == 'works' and library is not None:
        from .work_profile import website_properties
        previous = website_properties(library, current)
    if collection == 'works' and fields.get('article') is not None:
        matches = [note_id for note_id, base in baselines.items()
                   if base.get('collection') == 'posts' and str(base.get('remote_id')) == str(fields['article'])]
        if len(matches) == 1: fields['article'] = matches[0]
        elif fields.get('article') != previous.get('article'):
            raise ValueError('Relationship requires a proven library identity')
        if library is not None:
            from .relations import display_article
            fields['article'] = display_article(library, fields['article'], notes=notes)
    for key in managed - {'markdown'}:
        if key in fields: properties[key] = deepcopy(fields[key])
        else: properties.pop(key, None)
    compact = False
    if collection == 'works' and library is not None:
        from .work_profile import profile, WEBSITE_ONLY
        compact = profile(library, current['id']) is not None
        if compact:
            for key in WEBSITE_ONLY:
                properties.pop(key, None)
    if '_status' in remote and not compact:
        properties['payload_observed_status'] = remote['_status']
    if collection == 'posts':
        body = fields.get('markdown') or ''
        if fields.get('markdown') is None and 'markdown' in fields:
            properties['payload_markdown_is_null'] = True
        else:
            properties.pop('payload_markdown_is_null', None)
    return _render(properties, body)
