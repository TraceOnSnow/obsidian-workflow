"""Explicit, reversible migration from individual Work records to one table."""
from copy import deepcopy
import re
from pathlib import Path
import tempfile

import yaml

from .bootstrap import _index
from .core import LibraryError, _hash, _read, _render, _split
from .catalog_sync import daily_origin
from .mcp_actions import _rewrite
from .sync import content_hash
from .work_catalog import (DEFAULT_PATH, build_catalog, catalog_entry, enabled,
                           has_review_body, parse_catalog, review_markdown, row_from_note,
                           scan_overlay)
from .work_review import review_body

ARCHIVE = '90_归档/历史资料/作品记录/旧草稿与整理建议.md'
OLD_BASE = 'works/作品表格.base'
BASE_ARCHIVE = '90_归档/历史资料/作品记录/作品表格（迁移前）.base'
MEDIA = {'game': '游戏', 'anime': '动画', 'book': '书籍', 'movie': '电影',
         'tv': '剧集', 'manga': '日漫', 'comic': '漫画', 'boardgame': '桌游'}
REVIEW_TITLES = {'Afterimage': '心渊梦境', 'Animal Well': '动物井',
    "Baldur's Gate III": '博德之门3', 'Big Walk': '大步走',
    'Dark Souls III': '黑暗之魂3', 'Dark Souls: Remastered': '黑暗之魂1',
    'Divinity: Original Sin 2': '神界：原罪2',
    'Ender Lilies: Quietus of the Knights': '终焉的莉莉丝',
    'Oxygen Not Included': '缺氧', 'Palworld': '幻兽帕鲁',
    'Plague Inc: Evolved': '瘟疫公司', 'Slay the Spire': '杀戮尖塔',
    'Tiny Civilization': '方寸文明', 'WUCHANG: Fallen Feathers': '明末：渊虚之羽'}


def _review(note):
    body = review_body(_split(note['markdown'])[1])
    navigation = re.search(r'<!-- markdown-library:publication-nav:start -->.*?<!-- markdown-library:publication-nav:end -->', note['markdown'], re.S)
    nav = navigation[0] if navigation else ''
    if '暂拟观点待确认' in body:
        return '', nav, body
    removed = []
    # Retain personal observations; previous AI analysis stays only in the archive.
    for heading in ('当时 AI 补充的解释（未经验证，仅作参考）',
                    '当时 AI 的补充（未确认）', '关于评测整理的讨论'):
        pattern = r'^## ' + re.escape(heading) + r'\n.*?(?=^## |\Z)'
        match = re.search(pattern, body, re.M | re.S)
        if match:
            removed.append(match[0])
            body = body[:match.start()] + body[match.end():]
    if note['properties']['title'] == 'Big Walk':
        remainder = re.search(r'^## 未决状态\n.*?(?=^## |\Z)', body, re.M | re.S)
        if remainder:
            removed.append(remainder[0])
            body = body[:remainder.start()] + body[remainder.end():]
    if nav:
        body = re.sub(r'^发表稿：\[\[[^\n]+\]\]\s*$', '', body, flags=re.M)
    if not has_review_body(body):
        return '', nav, '\n\n'.join(removed)
    title = REVIEW_TITLES.get(note['properties']['title'], note['properties']['title'])
    if not re.search(r'^# ', body, re.M):
        body = '# ' + title + '\n\n' + body.strip()
    body = re.sub(r'^# 私人记录$', '# ' + title, body, flags=re.M)
    return body.strip() + '\n', nav, '\n\n'.join(removed)


def _document(records, previous_base):
    doc = parse_catalog(build_catalog(records)).document
    fields = {field['id']: field for field in doc['fields']}
    fields['type'].update(type='select', options=[{'id': key, 'label': value, 'color': 'blue'} for key, value in MEDIA.items()])
    fields['status'].update(type='select', options=[{'id': key, 'label': value, 'color': color} for key, value, color in [
        ('planned', '待体验', 'gray'), ('in-progress', '进行中', 'blue'),
        ('completed', '已完成', 'green'), ('on-hold', '暂停', 'yellow'), ('dropped', '已搁置', 'red')]])
    fields['review']['name'] = '感想'
    doc['fields'].append({'id': 'article', 'name': '发表稿', 'type': 'text', 'width': 180})
    field_map = {'formula.work': 'title', 'file.name': 'title', 'rating': 'rating',
                 'note.rating': 'rating', 'status': 'status', 'note.status': 'status',
                 'note.note': 'note', 'formula.media': 'type', 'formula.experienceEnd': 'endedOn'}
    widths = {'title': 260, 'rating': 90, 'status': 120, 'note': 300, 'type': 100,
              'startedOn': 120, 'endedOn': 120, 'review': 170}
    native = yaml.safe_load(previous_base) if previous_base else {}
    for view in native.get('views', []):
        for name, width in view.get('columnSize', {}).items():
            if name in field_map:
                widths[field_map[name]] = width
    for key, field in fields.items():
        field['width'] = widths[key]
    views = []
    for index, source in enumerate(native.get('views') or [{'name': '全部作品'}]):
        view = {'id': 'view-' + str(index), 'name': source['name'], 'filters': {'mode': 'and', 'conditions': []}}
        conditions = view['filters']['conditions']
        filters = str(source.get('filters', ''))
        status = re.search(r'status == "([^"]+)"', filters)
        if status:
            conditions.append({'id': 'status', 'fieldId': 'status', 'operator': 'eq', 'value': [status[1]]})
        if 'rating.isEmpty()' in filters:
            conditions.append({'id': 'rating', 'fieldId': 'rating', 'operator': 'isEmpty'})
        if 'experienceEnd.isEmpty' in filters:
            conditions.append({'id': 'date', 'fieldId': 'endedOn', 'operator': 'isNotEmpty'})
        if 'formula.media' in filters and '游戏' in filters:
            conditions.append({'id': 'type', 'fieldId': 'type', 'operator': 'eq', 'value': ['game']})
        visible = list(dict.fromkeys(field_map[key] for key in source.get('order', []) if key in field_map))
        if visible:
            view['visibleFieldIds'] = list(dict.fromkeys(visible + ['review', 'article']))
        sorting = source.get('sort', [])
        if sorting and sorting[0].get('property') in field_map:
            view['sort'] = {'fieldId': field_map[sorting[0]['property']], 'direction': sorting[0].get('direction', 'ASC').lower()}
        group = source.get('groupBy') or {}
        if group.get('property') in field_map:
            view['group'] = {'fieldId': field_map[group['property']], 'direction': group.get('direction', 'ASC').lower()}
        views.append(view)
    views.append({'id': 'with-review', 'name': '有感想', 'filters': {'mode': 'and', 'conditions': [
        {'id': 'review', 'fieldId': 'review', 'operator': 'isNotEmpty'}]}})
    doc['views'] = views
    doc['defaultViewId'] = views[0]['id']
    return doc


def _plan(library):
    state, notes = _index(library)
    if enabled(state):
        return {'applied': False, 'already_migrated': True, 'path': state['work_catalog']['path']}, None
    works = [note for note in notes.values() if note['properties']['kind'] == 'work']
    if not works:
        raise LibraryError('There are no Work records to migrate')
    files = {}
    for path in library.vault.rglob('*'):
        if path.is_file() and not any(part.startswith('.') for part in path.relative_to(library.vault).parts) and path.suffix in {'.md', '.base'}:
            relative = path.relative_to(library.vault).as_posix()
            files[relative] = _read(library._path(relative))
    for destination in (DEFAULT_PATH, ARCHIVE, BASE_ARCHIVE):
        if destination in files:
            raise LibraryError('Migration destination is occupied: ' + destination)
    config = {'schema_version': 1, 'path': DEFAULT_PATH, 'rows': {}, 'daily_origins': {}, 'review_root': 'works/感想'}
    rows, reviews, archive, redirects = [], [], [], {}
    targets = set(files)
    for note in sorted(works, key=lambda item: (item['properties'].get('type', ''), item['properties']['title'].casefold(), item['id'])):
        body, nav, old_analysis = _review(note)
        title = note['properties']['title']
        review_path = None
        if body:
            basename = REVIEW_TITLES.get(title, title)
            basename = re.sub(r'[\\/:*?"<>|#\[\]%\r\n]', ' ', basename).strip() or note['id']
            review_path = 'works/感想/' + MEDIA.get(note['properties'].get('type'), '其他') + '/' + basename + '.md'
            if review_path in targets:
                review_path = review_path[:-3] + '-' + note['id'][:8] + '.md'
            if review_path in targets:
                raise LibraryError('Review destination is occupied')
            targets.add(review_path)
        logical = {**note, 'markdown': _render(note['properties'], body + ('\n' + nav if nav else ''))}
        config['rows'][note['id']] = catalog_entry(logical, review_path)
        config['daily_origins'][note['id']] = daily_origin(note)
        rows.append(row_from_note(logical, review_path))
        if review_path:
            reviews.append((note['path'], review_path, review_markdown(logical)))
        if old_analysis:
            archive.append((note['path'], '## ' + title + '\n\n' + old_analysis.strip()))
        redirects[note['path']] = review_path or DEFAULT_PATH
    if OLD_BASE in files:
        redirects[OLD_BASE] = DEFAULT_PATH
    document = _document(rows, files.get(OLD_BASE))
    table = build_catalog(rows, document)
    link_paths = {path.relative_to(library.vault).as_posix() for path in library.vault.rglob('*')
                  if path.is_file() and not any(part.startswith('.') for part in path.relative_to(library.vault).parts)}
    moved_reviews = [(path, _rewrite(text, old, path, redirects, link_paths)[0], None) for old, path, text in reviews]
    writes = [(DEFAULT_PATH, table, None)] + moved_reviews
    removes = [(note['path'], note['sha256']) for note in works]
    for path, text in files.items():
        if path in redirects or path == 'works/索引.md' or not path.endswith('.md'):
            continue
        updated, count = _rewrite(text, path, path, redirects, link_paths)
        if count:
            writes.append((path, updated, _hash(text)))
    index = '<!-- markdown-library:index:v1; generated navigation -->\n# 作品\n\n[[works/作品总表|打开作品总表]]\n\n在总表里修改评分、进度、短评和体验日期，点击“感想”列打开正文。\n\n![[works/作品总表]]\n'
    writes.append(('works/索引.md', index, _hash(files['works/索引.md']) if 'works/索引.md' in files else None))
    if archive:
        archived_sections = [_rewrite(text, old, ARCHIVE, redirects, link_paths)[0] for old, text in archive]
        writes.append((ARCHIVE, '# 旧草稿与整理建议\n\n历史AI草稿和整理建议，保留供查找。\n\n' + '\n\n'.join(archived_sections) + '\n', None))
    if OLD_BASE in files:
        writes.append((BASE_ARCHIVE, files[OLD_BASE], None))
        removes.append((OLD_BASE, _hash(files[OLD_BASE])))
    token = content_hash({'state': state, 'files': {path: _hash(text) for path, text in files.items()},
                          'writes': writes, 'removes': removes, 'config': config})
    summary = {'applied': False, 'already_migrated': False, 'path': DEFAULT_PATH, 'works': len(works),
               'review_files': len(reviews), 'archived_draft_sections': len(archive), 'views': len(document['views']),
               'linked_files_updated': sum(path not in {DEFAULT_PATH, ARCHIVE, BASE_ARCHIVE, 'works/索引.md'} for path, _, expected in writes if expected is not None),
               'token': token, 'website_writes': 0}
    return summary, (state, notes, works, config, writes, removes, files)


def _validate_plan(library, state, works, config, writes):
    """Reconstruct every planned Work before removing any original file."""
    planned_state = deepcopy(state)
    planned_state['work_catalog'] = deepcopy(config)
    with tempfile.TemporaryDirectory(prefix='work-catalog-validation-') as directory:
        root = Path(directory)
        required = {config['path']} | {entry['review_path'] for entry in config['rows'].values()
                                      if entry.get('review_path')}
        for relative, text, _ in writes:
            if relative in required:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding='utf-8')
        class PlannedStorage:
            _validate_properties = staticmethod(library._validate_properties)
            def _path(self, relative):
                return root / relative
        current = scan_overlay(PlannedStorage(), planned_state, {})
        for note in works:
            if current[note['id']]['properties'] != note['properties']:
                raise LibraryError('Planned Work fields differ: ' + note['id'])


def migrate_work_catalog(library, *, apply=False, expected_token=None):
    library.validate()
    if not apply:
        return _plan(library)[0]
    if not expected_token:
        raise LibraryError('Migration requires its current preview token')
    with library._lock():
        library._load()
        summary, plan = _plan(library)
        if plan is None:
            return summary
        if summary['token'] != expected_token:
            raise LibraryError('Migration preview expired; preserve current edits and preview again')
        state, notes, works, config, writes, removes, files = plan
        publication_before = deepcopy(state['publications'])
        _validate_plan(library, state, works, config, writes)
        for path, original in files.items():
            if _hash(_read(library._path(path))) != _hash(original):
                raise LibraryError('Migration input changed: ' + path)
        for note in works:
            library._remember(state, note, 'work-catalog-migration-original')
        state['work_catalog'] = config
        library._commit(state, writes, removes, storage_resolved=True)
        current = library._scan(state)
        library._save(state)
        for note in works:
            if current[note['id']]['properties'] != note['properties']:
                raise LibraryError('Migrated Work fields differ: ' + note['id'])
        if publication_before != library._read_state()['publications']:
            raise LibraryError('Publication selections changed during migration')
        return {**summary, 'applied': True}
