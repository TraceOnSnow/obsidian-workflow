"""Explicitly prepare a linked Post current draft from one Work's full body.

This local operation never selects publication and never contacts the website.
"""
from copy import deepcopy
import re

from .bootstrap import _index
from .core import LibraryError, _hash, _render, _split
from .relations import canonical_article, display_article, wikilink
from .sync import content_hash
from .publication_navigation import strip_publication_navigation
from .work_profile import WEBSITE_ONLY, profile, set_profile, website_properties


FOOTER_START = '<!-- markdown-library:work-review:v1 -->'
FOOTER_END = '<!-- /markdown-library:work-review -->'
_FOOTER = re.compile(
    r'\r?\n\r?\n' + re.escape(FOOTER_START)
    + r'\r?\n关联文章：[^\r\n]*\r?\n' + re.escape(FOOTER_END) + r'\r?\n')


def review_body(body):
    """Remove only our exact footer block, preserving every other body byte."""
    body = strip_publication_navigation(body)
    if FOOTER_START not in body and FOOTER_END not in body:
        return body
    matches = list(_FOOTER.finditer(body))
    if (len(matches) != 1 or body.count(FOOTER_START) != 1
            or body.count(FOOTER_END) != 1):
        raise LibraryError('Work review footer is ambiguous; preserve it and review locally')
    block = matches[0]
    return body[:block.start()] + body[block.end():]


def _linked_post(library, value, notes):
    """Numeric website IDs require an observed, unique local Post mapping."""
    identity = canonical_article(library, value, notes=notes)
    post = notes.get(str(identity))
    if post is None:
        # display_article only turns a legacy numeric ID into a link after it
        # finds one proven mapping; canonical_article then verifies that link.
        displayed = display_article(library, value, notes=notes)
        if not isinstance(displayed, str) or not displayed.startswith('[['):
            raise LibraryError('Linked article has no provable current local Post; review before preparing')
        post = notes.get(str(canonical_article(library, displayed, notes=notes)))
    if post is None or post['properties']['kind'] != 'post':
        raise LibraryError('Linked article must be one current local Post')
    return post


def prepare_work_review(library, work_id, expected_hash, expected_post_hash=None,
                        expected_token=None, apply=False):
    """Preview, or atomically save a Work review as a linked private Post draft.

    Apply always requires the preview token and Work SHA. If an existing Post
    would change, its current SHA must also be supplied explicitly.
    """
    library.validate()

    def prepare(state, notes):
        work = library._required(notes, work_id)
        if work['properties']['kind'] != 'work':
            raise LibraryError('Review preparation requires a Work')
        library._expect(work, expected_hash)
        full = website_properties(library, work, state)
        retained = {key: deepcopy(value) for key, value in full.items() if key in WEBSITE_ONLY}
        feelings = review_body(_split(work['markdown'])[1])
        if not feelings.strip():
            raise LibraryError('Work body is empty; write the full review before preparing a Post')
        source_key = 'work-review:' + work_id
        claim = state['sources'].get(source_key)
        article = full.get('article')
        post = _linked_post(library, article, notes) if article not in (None, '') else None
        if claim:
            source_post = library._required(notes, claim['id'])
            if source_post['properties']['kind'] != 'post':
                raise LibraryError('Work review source identity does not point to a Post')
            if post is not None and post['id'] != source_post['id']:
                raise LibraryError('Work article and review source disagree; preserve both and review')
            post = source_post
        path = post['path'] if post else '发表稿/work-' + work_id + '-review.md'
        destination = library._path(path)
        if post is None and destination.exists():
            raise LibraryError('Review destination is occupied; existing content retained')
        if post:
            properties = deepcopy(post['properties'])
            related = properties.get('related_works') or []
            if not isinstance(related, list) or any(not isinstance(value, str) for value in related):
                raise LibraryError('Post related_works requires a list of links; preserve existing values')
            link = wikilink(work)
            if link not in related:
                related = related + [link]
            properties['related_works'] = related
            # Existing title, link, protection and publication metadata remain.
            markdown = _render(properties, feelings)
            if properties == post['properties'] and _split(post['markdown'])[1] == feelings:
                markdown = post['markdown']
            post_view = {**post, 'properties': properties, 'markdown': markdown, 'sha256': _hash(markdown)}
        else:
            properties = {'link': 'work-' + work_id + '-review', 'locale': 'zh',
                          'related_works': [wikilink(work)]}
            post_view = {'path': path, 'properties': {'title': full['title'], **properties}}
        newline = '\r\n' if '\r\n' in feelings else '\n'
        footer = newline * 2 + FOOTER_START + newline + '关联文章：' + wikilink(post_view) + newline + FOOTER_END + newline
        work_properties = {key: deepcopy(value) for key, value in work['properties'].items()
                           if key not in WEBSITE_ONLY}
        work_markdown = _render(work_properties, feelings + footer)
        if work_properties == work['properties'] and _split(work['markdown'])[1] == feelings + footer:
            work_markdown = work['markdown']
        token = content_hash({'work_id': work_id, 'work_sha256': work['sha256'],
                              'retained_metadata': retained, 'profile': profile(library, work_id, state),
                              'source_claim': claim, 'post': {'id': post['id'], 'path': path,
                                                               'sha256': post['sha256']} if post else None,
                              'destination': path, 'body_sha256': _hash(feelings)})
        return {'work': work, 'post': post, 'post_view': post_view, 'properties': properties,
                'retained': retained, 'source_key': source_key, 'claim': claim,
                'feelings': feelings, 'work_properties': work_properties,
                'work_markdown': work_markdown, 'token': token}

    def result(plan, *, applied=False, changed=None, created=None, saved=None):
        post = created or saved or plan['post']
        changed_post = plan['post'] is None or plan['post_view'].get('sha256') != plan['post']['sha256']
        return {'applied': applied, 'outcome': 'created' if created else ('create' if plan['post'] is None else 'update' if changed_post else 'unchanged'),
                'expected_token': plan['token'], 'publication_selected': False,
                'remote_writes': False, 'changed': changed or [],
                'work': {'id': work_id, 'path': plan['work']['path'],
                         'sha256': _hash(plan['work_markdown']) if applied else plan['work']['sha256']},
                'post': {'id': post['id'] if post else None, 'path': plan['post_view']['path'],
                         'sha256': post['sha256'] if post else None},
                'requires_post_hash': bool(plan['post'] and changed_post),
                'body_sha256': _hash(plan['feelings'])}

    if not apply:
        state, notes = _index(library)
        return result(prepare(state, notes))
    if not expected_token:
        raise LibraryError('Review preparation apply requires a fresh preview token')
    with library._lock():
        # Read-only indexing before guard validation avoids checkpoint side
        # effects on an expired preview, then the one commit journals all files.
        state, notes = _index(library)
        plan = prepare(state, notes)
        if expected_token != plan['token']:
            raise LibraryError('Work review preview expired; review the Work and Post again')
        post = plan['post']
        post_changes = post is not None and plan['post_view']['sha256'] != post['sha256']
        if post_changes and (not expected_post_hash or expected_post_hash != post['sha256']):
            raise LibraryError('Updating a linked Post draft requires its exact current expected_post_hash')
        writes = []
        created = None
        if post is None:
            created = library._create(state, _render({}, plan['feelings']), title=plan['post_view']['properties']['title'],
                                      kind='post', properties=plan['properties'], path=plan['post_view']['path'])
            post = created
            writes.append((post['path'], post['markdown'], None))
        elif post_changes:
            library._remember(state, post, 'work-review-original')
            library._remember(state, plan['post_view'], 'work-review-prepared')
            writes.append((post['path'], plan['post_view']['markdown'], post['sha256']))
            post = plan['post_view']
        retained = {**plan['retained'], 'article': post['id']}
        state_changed = profile(library, work_id, state) != retained or plan['claim'] is None
        if plan['work_markdown'] != plan['work']['markdown']:
            library._remember(state, plan['work'], 'work-review-original')
            updated = {**plan['work'], 'markdown': plan['work_markdown'], 'properties': plan['work_properties'],
                       'sha256': _hash(plan['work_markdown'])}
            library._remember(state, updated, 'work-review-linked')
            writes.append((updated['path'], updated['markdown'], plan['work']['sha256']))
        set_profile(state, work_id, retained)
        if plan['claim'] is None:
            state['sources'][plan['source_key']] = {'id': post['id'], 'source': {'type': 'work-review', 'work_id': work_id}}
        if writes or state_changed:
            library._commit(state, writes)
        return result(plan, applied=bool(writes or state_changed), changed=[path for path, _, _ in writes], created=created, saved=post)
