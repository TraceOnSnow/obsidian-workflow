"""A fixed, local note-to-article workflow; no model or frontend polling."""

from copy import deepcopy
from datetime import datetime
import fcntl
import json
import os
import re
import shlex
import stat
from urllib.parse import quote

from .bootstrap import _index
from .core import LibraryError, _hash, _render, _split
from .publication_navigation import strip_publication_navigation as strip, set_publication_navigation
from .sync import export_publication, PUBLIC_THEME_FIELDS
from .transport import sync_once, _verify_both, _Store, _selected_unchanged


REQUEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "PublishArticleRequest", "type": "object", "additionalProperties": False,
    "required": ["title"],
    "oneOf": [
        {"required": ["source_note_id", "expected_source_hash", "published_at"], "not": {"required": ["post_id"]}},
        {"required": ["post_id", "expected_post_hash"], "not": {"anyOf": [
            {"required": ["source_note_id"]}, {"required": ["expected_source_hash"]}]}},
    ],
    "properties": {
        "post_id": {"type": "string", "minLength": 1},
        "source_note_id": {"type": "string", "minLength": 1},
        "expected_source_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "expected_post_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "title": {"type": "string", "minLength": 1},
        "published_at": {"type": "string", "format": "date-time"},
        "public_markdown": {"type": "string", "minLength": 1},
        "link": {"type": "string", "pattern": "^[a-z0-9]+(?:[-/][a-z0-9]+)*$"},
        "locale": {"type": "string", "pattern": "^[a-z]{2}(?:-[A-Za-z0-9]+)*$"},
        "description": {"type": "string"},
        "categories": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "replacements": {"type": "object", "additionalProperties": {"type": "string"}},
        "forbidden_terms": {"type": "array", "items": {"type": "string", "minLength": 1}},
    },
}


def validate_request(request):
    if not isinstance(request, dict) or set(request) - REQUEST_SCHEMA['properties'].keys():
        raise LibraryError('Article request must be an object with supported fields only')
    if 'title' not in request or ('source_note_id' in request) == ('post_id' in request):
        raise LibraryError('Article request requires title and exactly one of source_note_id or post_id')
    required = ({'source_note_id', 'expected_source_hash', 'published_at'} if 'source_note_id' in request
                else {'post_id', 'expected_post_hash'})
    if required - request.keys():
        raise LibraryError('Article request is missing its input hash or publication date')
    if 'post_id' in request and 'expected_source_hash' in request:
        raise LibraryError('Post updates use expected_post_hash, not a private source hash')
    for key, value in request.items():
        spec = REQUEST_SCHEMA['properties'][key]
        if spec['type'] == 'string':
            if not isinstance(value, str) or '\x00' in value or ('minLength' in spec and not value.strip()):
                raise LibraryError(f'Invalid article field: {key}')
            if 'pattern' in spec and not re.fullmatch(spec['pattern'], value):
                raise LibraryError(f'Invalid article field: {key}')
        elif spec['type'] == 'array':
            if not isinstance(value, list) or any(not isinstance(x, str) or '\x00' in x for x in value):
                raise LibraryError(f'Invalid article field: {key}')
        elif (not isinstance(value, dict)
              or any(not isinstance(k, str) or not k or not isinstance(v, str)
                     or '\x00' in k + v for k, v in value.items())):
            raise LibraryError(f'Invalid article field: {key}')
    if any(not term.strip() for term in request.get('forbidden_terms', [])):
        raise LibraryError('Forbidden terms must be nonempty')
    if any(c in request['title'] for c in '\r\n'):
        raise LibraryError('Article title must be one line')
    if 'published_at' in request:
        try:
            moment = datetime.fromisoformat(request['published_at'].replace('Z', '+00:00'))
            if moment.tzinfo is None or 'T' not in request['published_at']:
                raise ValueError()
        except ValueError:
            raise LibraryError('published_at requires an ISO datetime with an explicit timezone') from None


def client_settings(filename, *, url=None, api_key_env='HOMEPAGE_API_KEY'):
    """Read an explicitly chosen private dotenv file as data, never shell code."""
    values = dict(os.environ)
    if filename:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        with os.fdopen(os.open(filename, flags)) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise LibraryError('Client env file must be an owner-only regular file')
            for line in stream:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                match = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)', line)
                if not match:
                    raise LibraryError('Invalid client env file')
                parts = shlex.split(match[2], comments=True)
                if len(parts) > 1:
                    raise LibraryError('Invalid client env file')
                values[match[1]] = parts[0] if parts else ''
    origin, key = url or values.get('HOMEPAGE_URL'), values.get(api_key_env)
    if not origin or not key:
        raise LibraryError('Article publication requires a configured origin and named API key')
    return origin, key


def _guard(library, note, expected):
    if note['sha256'] == expected:
        return
    # A successful previous call may have added only the managed navigation.
    old = library._path('.markdown-library/objects/' + expected + '.md')
    if old.is_file():
        original = old.read_text()
        if _hash(original) == expected and strip(original) == strip(note['markdown']):
            return
    raise LibraryError('Article input changed; read the current draft again')


class _ApprovedClient:
    """Keep the approved selection pinned even if another caller selects a draft.

    The existing transport checks its own loaded selection; this extra boundary
    ties that selection to the article this command was authorized to send.
    """
    def __init__(self, library, client, selected):
        self.library, self.client, self.selected = library, client, selected
        self.target = client.target

    def list_collection(self, *args, **kwargs):
        _selected_unchanged(self.library, self.selected)
        return self.client.list_collection(*args, **kwargs)

    def get(self, *args, **kwargs):
        _selected_unchanged(self.library, self.selected)
        return self.client.get(*args, **kwargs)

    def create(self, *args, **kwargs):
        _selected_unchanged(self.library, self.selected)
        return self.client.create(*args, **kwargs)

    def update(self, *args, **kwargs):
        _selected_unchanged(self.library, self.selected)
        return self.client.update(*args, **kwargs)


def _plan(library, request, state, notes):
    source, key = None, None
    if 'post_id' in request:
        post = library._required(notes, request['post_id'])
        try:
            _guard(library, post, request['expected_post_hash'])
        except LibraryError:
            # A prior call may have saved the exact requested edit before a
            # network timeout. Permit reconciliation, never an unrelated edit.
            retained = library._path('.markdown-library/objects/' + request['expected_post_hash'] + '.md')
            if not retained.is_file() or _hash(original := retained.read_text()) != request['expected_post_hash']:
                raise
            properties, _ = _split(original)
            old = {**post, 'markdown': original, 'sha256': request['expected_post_hash'], 'properties': properties}
            planned = _plan(library, request, state, {**notes, post['id']: old})
            if strip(_render(planned['properties'], planned['body'])) != strip(post['markdown']):
                raise LibraryError('Post changed beyond the requested edit; read the current draft again')
            return {**planned, 'post': post, 'changed': False}
    else:
        source = library._required(notes, request['source_note_id'])
        if source['properties']['kind'] != 'note':
            raise LibraryError('Private source must be a note; Work reviews use prepare-work-review')
        _guard(library, source, request['expected_source_hash'])
        key = 'publication-copy:' + source['id']
        candidates = {row['id'] for row in state['sources'].values()
                      if any(row.get('source', {}).get(field) == source['id']
                             for field in ('note_id', 'private_source_id', 'source_note_id'))}
        if key in state['sources']:
            candidates.add(state['sources'][key]['id'])
        if len(candidates) > 1:
            raise LibraryError('Multiple independent Posts refer to this source; resolve the relationship first')
        post = library._required(notes, next(iter(candidates))) if candidates else None
    if post and post['properties']['kind'] != 'post':
        raise LibraryError('Publication source must refer to an existing Post')
    properties = deepcopy(post['properties']) if post else {}
    properties['title'] = request['title']
    if 'published_at' in request:
        properties['publishedAt'] = request['published_at']
    for field in ('link', 'locale', 'description', 'categories', 'tags'):
        if field in request:
            properties[field] = deepcopy(request[field])
    properties.setdefault('link', 'article-' + (source or post)['id'])
    properties.setdefault('locale', 'zh')
    theme = properties.get('frontmatter') or {}
    if not isinstance(theme, dict):
        raise LibraryError('Post theme frontmatter must be a mapping')
    if properties.get('protectedContent') or properties.get('password') or theme.get('password'):
        raise LibraryError('Protected Posts require a supported deliberate publication flow')
    if set(theme) - PUBLIC_THEME_FIELDS:
        raise LibraryError('Unsupported Post theme options require explicit review')
    body = strip(request.get('public_markdown', _split((source or post)['markdown'])[1]))
    if body.startswith('---\n') or body.startswith('---\r\n'):
        raise LibraryError('public_markdown must be a body, without frontmatter')
    replacements = request.get('replacements', {})
    if replacements:
        pattern = re.compile('|'.join(re.escape(k) for k in sorted(replacements, key=len, reverse=True)))
        body = pattern.sub(lambda m: replacements[m[0]], body)
    body = re.sub(r'\A((?:[ \t]*\r?\n)*)# [^\r\n]*',
                  lambda m: m[1] + '# ' + request['title'], body, count=1)
    if not body.strip() or len(body.encode('utf-8')) > 512 * 1024:
        raise LibraryError('Public body must be nonempty and at most 512 KiB')
    if re.search(r'^[ \t]*:{3,}[ \t]*encrypted\b', body, re.M):
        raise LibraryError('Encrypted content requires a supported deliberate publication flow')
    projected = export_publication({'id': post['id'] if post else source['id'], 'kind': 'post',
                                   'state': 'published', 'snapshot': {'title': request['title'],
                                   'properties': properties, 'markdown': body}})
    public_text = json.dumps(projected, ensure_ascii=False).casefold()
    if any(term.casefold() in public_text for term in request.get('forbidden_terms', [])):
        raise LibraryError('Public projection still contains a forbidden term')
    for note in notes.values():
        if (note['properties']['kind'] == 'post' and (not post or note['id'] != post['id'])
                and note['properties'].get('link') == properties['link']
                and note['properties'].get('locale', 'zh') == properties['locale']):
            raise LibraryError('Another Post owns this public URL; do not create a duplicate')
    changed = not post or (strip(_render(properties, body)) != strip(post['markdown']))
    return {'source': source, 'post': post, 'key': key, 'properties': properties,
            'body': body, 'changed': changed}


def _finish(library, source_id, post_id, url):
    """One local transaction repairs moved links and the two one-line entries."""
    from .mcp_actions import LibraryActions
    from .overview import _wiki
    with library._lock():
        state = library._load()
        notes = library._scan(state)
        post = library._required(notes, post_id)
        destination = '90_归档/已发表文章/' + post['path'].rsplit('/', 1)[-1]
        if post['path'] != destination:
            if library._path(destination).exists():
                raise LibraryError('Article archive destination is occupied')
            actions = LibraryActions(library)
            writes, updated, _ = actions._changes(state, notes, actions._files(notes), {post['path']: destination})
        else:
            writes, updated = [], {}
        view = {**notes, **updated}
        post = view[post_id]
        def link(note, label):
            return _wiki(note).rsplit('|', 1)[0] + '|' + label + ']]'
        entries = {}
        # Retain all proven original-source backlinks of an existing Post.
        originals = {source_id} if source_id else set()
        for row in state['sources'].values():
            if row['id'] == post_id:
                originals.update(value for field in ('note_id', 'private_source_id', 'source_note_id', 'work_id')
                                 if (value := row.get('source', {}).get(field)) in view)
        for original_id in originals:
            # Catalog-backed Works retain their separate relationship tool.
            if view[original_id]['properties']['kind'] != 'note':
                continue
            other_posts = {row['id'] for row in state['sources'].values()
                           if row['id'] != post_id and any(row.get('source', {}).get(field) == original_id
                               for field in ('note_id', 'private_source_id', 'source_note_id'))}
            if not other_posts:
                entries[original_id] = '已发表 · [网站文章](' + url + ') · ' + link(post, '发表稿')
        entries[post_id] = '已发表 · [网站文章](' + url + ')'
        if originals:
            entries[post_id] += ' · ' + ' · '.join(link(view[key], '私人原稿') for key in sorted(originals))
        for key, footer in entries.items():
            note = view[key]
            markdown = set_publication_navigation(note['markdown'], footer)
            if markdown == note['markdown']:
                continue
            original = notes[key]
            exists = next((i for i, w in enumerate(writes) if w[0] == note['path']), None)
            if exists is not None:
                writes[exists] = (note['path'], markdown, writes[exists][2])
            else:
                writes.append((note['path'], markdown, original['sha256']))
            library._remember(state, {**note, 'markdown': markdown, 'sha256': _hash(markdown)}, 'article-published-navigation')
        removals = [(notes[post_id]['path'], notes[post_id]['sha256'])] if notes[post_id]['path'] != destination else []
        if writes:
            library._commit(state, writes, removals)


def publish_article(library, request, *, client=None, apply=False):
    """Preview offline, or prepare/select/sync/verify/finish one approved article."""
    validate_request(request)
    library.validate()
    if not apply:
        state, notes = _index(library)
        plan = _plan(library, request, state, notes)
        post = plan['post']
        return {'status': 'preview', 'remote_writes': False, 'source_note_id': request.get('source_note_id'),
                'post_id': post['id'] if post else None,
                'expected_post_hash': post['sha256'] if post else None,
                'requires_post_hash': bool(post and plan['changed']),
                'title': request['title'], 'properties': plan['properties'], 'public_markdown': plan['body']}
    if client is None:
        raise LibraryError('Article apply requires an explicit configured Payload client')
    # Serialize tool invocations across the network phase; never hold the Vault
    # lock while waiting for the website. Ordinary sync retains its own locks.
    lockfile = library._path('.markdown-library/article-publish.lock')
    with os.fdopen(os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as operation:
        try:
            fcntl.flock(operation, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LibraryError('Another article publication is running; reconcile its result first') from None
        with library._lock():
            state = library._load()
            notes = library._scan(state)
            plan = _plan(library, request, state, notes)
            post = plan['post']
            if post and plan['changed']:
                expected = request.get('expected_post_hash')
                if not expected:
                    raise LibraryError('Replacing an independent Post requires expected_post_hash')
                _guard(library, post, expected)
            writes = []
            if not post:
                post = library._create(state, plan['body'], title=request['title'], kind='post',
                                       properties=plan['properties'],
                                       path='发表稿/article-' + plan['source']['id'] + '.md')
                writes.append((post['path'], post['markdown'], None))
            elif plan['changed']:
                markdown = _render(plan['properties'], plan['body'])
                updated = {**post, 'markdown': markdown, 'properties': plan['properties'], 'sha256': _hash(markdown)}
                library._remember(state, updated, 'article-prepared')
                writes.append((post['path'], markdown, post['sha256']))
                post = updated
            if plan['key'] is not None and plan['key'] not in state['sources']:
                state['sources'][plan['key']] = {'id': post['id'], 'source': {
                    'type': 'publication-copy', 'note_id': plan['source']['id'],
                    'source_sha256': plan['source']['sha256']}}
            library._commit(state, writes)
        selected = library.publish(post['id'], expected_hash=post['sha256'])
        client = _ApprovedClient(library, client, selected)
        result = sync_once(library, client, apply=True, only_ids=[post['id']])
        row = next((row for row in result['results'] if row['id'] == post['id']), {})
        if (result.get('pending_ids') or row.get('status') not in {'observed', 'unchanged', 'local-draft'}
                or row.get('blocked_by')):
            return {'status': 'needs_review', 'api_verified': False, 'post_id': post['id'],
                    'sync_status': row.get('status'), 'pending_ids': result.get('pending_ids', [])}
        baseline = _Store(library, client.target).load()['baselines'].get(post['id'])
        if not baseline:
            raise LibraryError('Publication has no observed remote identity; reconcile before retrying')
        _verify_both(client, selected, baseline['remote_id'])
        # A concurrent edit must not be silently selected by this operation.
        current_selection = next(p for p in library.publications() if p['id'] == post['id'])
        if export_publication(current_selection) != export_publication(selected):
            raise LibraryError('Article selection changed during publication; reconcile before reporting success')
        identity = export_publication(selected)['identity']
        locale = identity.get('locale', 'zh')
        url = client.target + ('' if locale == 'zh' else '/' + quote(locale, safe='')) + '/post/' + quote(identity['link'], safe='/')
        answer = {'status': 'published', 'api_verified': True, 'post_id': post['id'],
                  'remote_id': baseline['remote_id'], 'url': url, 'frontend': 'asynchronous',
                  'local_maintenance': 'complete'}
        try:
            _finish(library, request.get('source_note_id'), post['id'], url)
        except (LibraryError, OSError, ValueError, KeyError):
            # Remote publication cannot be rolled back by a local-link failure.
            answer['local_maintenance'] = 'needs_review'
        return answer
