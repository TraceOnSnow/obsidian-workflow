#!/usr/bin/env python3
"""Install native live topic navigation without moving content or publishing it.

Markdown pages contain stable category entrances and curated reading maps only.
Article membership is evaluated by Obsidian Bases, never a generated inventory.
"""
import argparse
import json
import os
from pathlib import Path

import yaml

from markdown_library.core import Library, LibraryError, _split
from markdown_library.bootstrap import _index

MARKER = '<!-- markdown-library:topic-navigation:v1 -->'
BASE_MARKER = '# markdown-library:topic-base:v1; installed once, user editable'
ROOTS = ['00_收件箱', '10_个人记录', '20_项目与构想', '30_知识与参考', '90_归档']
BASE_NAME = '实时目录.base'
INBOX_BASE = '00_收件箱/待整理（实时）.base'
ALL_BASE = '全部笔记.base'
RESERVED_ROOTS = {'works', '发表稿', 'posts', 'notes', 'copilot', 'assets', 'config', 'templates'}
UTILITY_NAMES = ('索引', '首页', '笔记目录', '网站内容', '发布与同步状态', '使用说明',
                 'Copilot库内助手', 'AGENTS', 'HANDOFF', 'PLAN', 'README', 'SKILL')
# Keep the existing article topic routes, evaluated live only for the posts
# namespace. A post moved into a topic folder follows its physical destination.
POST_ROUTES = [
    ('30_知识与参考/算法与编程/Codeforces', 'formula.title.startsWith("Codeforces")'),
    ('30_知识与参考/算法与编程/OA 准备', 'formula.title.contains("Python OA")'),
    ('30_知识与参考/技术与工具', 'formula.title.contains("Markdown")'),
    ('90_归档/历史实践', 'formula.title.contains("博客的工作流")'),
    ('10_个人记录/作品感想', 'true'),
]


def wiki(path, title):
    return f"[[{str(path).removesuffix('.md')}|{str(title).replace('|', '／')}]]"


def literal(value):
    return json.dumps(str(value), ensure_ascii=False)


def content_filters():
    """Native predicates shared by all views; ordinary Markdown needs no kind."""
    return [
        'file.ext == "md"',
        '!/(^|\\/)\\./.matches(file.path)',
        '!/(^|\\/)(copilot|assets|config|templates)(\\/|$)/.matches(file.path)',
        '!/(^|\\/)(' + '|'.join(UTILITY_NAMES) + ')\\.md$/.matches(file.path)',
        'kind != "work"',
        '!file.inFolder("works")',
    ]


def topic_scope(parent):
    scopes = [f'file.inFolder({literal(parent)})']
    if parent == '00_收件箱':
        scopes += ['file.inFolder("notes")', 'file.folder == ""']
    previous = []
    for route, condition in POST_ROUTES:
        if route == parent or route.startswith(parent + '/'):
            scopes.append('(file.inFolder("发表稿") && kind == "post" && '
                          + ' && '.join([f'!({p})' for p in previous] + [condition]) + ')')
        previous.append(condition)
    return {'or': scopes}


def native_base(scope=None, *, inbox=False):
    filters = content_filters()
    if scope is not None:
        filters.append(scope)
    views = [{
        'type': 'table', 'name': '全部笔记',
        'order': ['formula.entry', 'file.folder', 'file.mtime'],
        'sort': [{'property': 'file.mtime' if inbox else 'formula.title',
                  'direction': 'DESC' if inbox else 'ASC'}],
    }, {
        'type': 'table', 'name': '按文件夹',
        'groupBy': {'property': 'file.folder', 'direction': 'ASC'},
        'order': ['formula.entry', 'file.mtime'],
        'sort': [{'property': 'formula.title', 'direction': 'ASC'}],
    }]
    document = {
        'filters': {'and': filters},
        'formulas': {'title': 'if(title, title, file.name)',
                     'entry': 'file.asLink(formula.title)'},
        'properties': {'formula.entry': {'displayName': '笔记'},
                       'file.folder': {'displayName': '所在文件夹'},
                       'file.mtime': {'displayName': '修改时间'}},
        'views': views,
    }
    return BASE_MARKER + '\n' + yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def discover(vault, items):
    """Find category structure and curated maps, including unmanaged Markdown.

    Folder discovery installs convenient child entrances. It does not define
    Base membership: future files/subfolders appear in parent views immediately.
    """
    parents = set(ROOTS) | {route for route, _ in POST_ROUTES}
    maps = {}
    indexed = {item['path']: item['properties'] for item in items}
    for directory, names, files in os.walk(vault, followlinks=False):
        names[:] = sorted(name for name in names if not name.startswith('.')
                          and not (Path(directory) / name).is_symlink()
                          and name not in {'copilot', 'assets', 'config', 'templates'}
                          and not (Path(directory) == vault and name in RESERVED_ROOTS))
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or name.startswith('.') or path.suffix.lower() != '.md':
                continue
            relative = path.relative_to(vault).as_posix()
            if name == '索引.md':
                if path.parent != vault:
                    parents.add(path.parent.relative_to(vault).as_posix())
                continue
            if path.stem in UTILITY_NAMES:
                continue
            properties = indexed.get(relative)
            if properties is None:
                try:
                    properties, _ = _split(path.read_text(encoding='utf-8'))
                except (OSError, UnicodeError, LibraryError):
                    properties = {}
            if properties.get('kind') == 'work':
                continue
            if path.parent != vault:
                parents.add(path.parent.relative_to(vault).as_posix())
            tags = properties.get('tags') or []
            if isinstance(tags, str):
                tags = [tags]
            if '主题导航' in tags:
                maps[relative] = properties.get('title') or path.stem
    for parent in list(parents):
        node = Path(parent)
        while len(node.parts) > 1:
            node = node.parent
            parents.add(node.as_posix())
    return sorted(parents), maps


def generate(vault, items):
    """Return the existing path->text contract used by CLI and MCP refresh."""
    vault = Path(vault)
    parents, maps = discover(vault, items)
    outputs = {}

    def install_base(relative, content):
        # Native Obsidian rewrites YAML comments and personal view settings.
        # Existing files are always left byte-for-byte intact, marker or no marker.
        if not (vault / relative).exists():
            outputs[relative] = content

    for parent in parents:
        base = INBOX_BASE if parent == '00_收件箱' else parent + '/' + BASE_NAME
        lines = [MARKER, '# ' + Path(parent).name, '', '[[首页|首页]] · [[笔记目录|全部主题]]', '']
        if parent == '00_收件箱':
            lines += ['新稿默认进入 `00_收件箱/`；收件箱或库根目录中的普通新稿会自动出现，确定主题后移动到相应文件夹。', '']
        if parent.startswith('90_归档'):
            lines += ['这里保留历史记录。文中的配置和操作描述属于当时背景，使用前核对当前环境。', '']
        children = [p for p in parents if str(Path(p).parent) == parent]
        lines += ['- ' + wiki(p + '/索引.md', Path(p).name) for p in children]
        if children:
            lines.append('')
        featured = [(path, title) for path, title in maps.items() if str(Path(path).parent) == parent]
        if featured:
            lines += ['## 从这里开始读', '']
            lines += ['- ' + wiki(path, title) for path, title in sorted(featured, key=lambda pair: pair[1])]
            lines.append('')
        lines += ['## 实时目录', '', f'![[{base}]]', '',
                  '新增、删除、重命名或移动笔记后，此目录由 Obsidian 自动更新。默认包含子文件夹；切换“按文件夹”查看归属。', '']
        if parent == '20_项目与构想':
            lines += ['同一项目的构想、设计和实践记录分别保留，由项目子目录汇总；未确认方案继续保留未确认标记。', '']
        outputs[parent + '/索引.md'] = '\n'.join(lines)
        install_base(base, native_base(topic_scope(parent), inbox=parent == '00_收件箱'))

    outputs['首页.md'] = '\n'.join([
        MARKER, '# 首页', '', '从主题找内容；新材料先入收件箱，保存与网站发布分别操作。', '',
        '## 日常入口', '',
        '- [[00_收件箱/索引|收件箱]]：查看新入库、尚未归类的材料。',
        '- [[10_个人记录/索引|个人记录]]：求职与实习、日记、生活思考和作品感想。',
        '- [[20_项目与构想/索引|项目与构想]]：按项目查看构想、设计和实践。',
        '- [[30_知识与参考/索引|知识与参考]]：算法、模板、技术备忘和参考资料。',
        '- [[works/索引|作品表格]]：维护评分、进度与短感受。',
        '- [[90_归档/索引|归档]]：历史资料与旧实践。', '',
        '## 写作与发布', '',
        '- [[使用说明|使用说明]] · [[Copilot库内助手|Copilot 库内助手]]',
        '- [[发布与同步状态|发布与同步状态]] · [[发表稿/索引|全部网站文章]]',
        '- [[笔记目录|全部主题与实时笔记目录]]', '',
        '文章可以同时出现在主题导航和网站文章目录中，正文只有一份。文件夹不决定公开状态，网站使用明确选定的发布版本。', ''])
    if maps:
        section = '## 按问题串读\n\n' + '\n'.join(
            '- ' + wiki(path, title) for path, title in sorted(maps.items(), key=lambda pair: pair[1])) + '\n\n'
        outputs['首页.md'] = outputs['首页.md'].replace('## 写作与发布\n', section + '## 写作与发布\n', 1)
    extra_roots = sorted(parent for parent in parents if '/' not in parent and parent not in ROOTS and parent != 'Blogs')
    outputs['笔记目录.md'] = MARKER + '\n# 全部主题\n\n[[首页|返回首页]]\n\n' + '\n'.join(
        '- ' + wiki(root + '/索引.md', Path(root).name.split('_', 1)[-1]) for root in ROOTS + extra_roots
    ) + '\n- [[works/索引|作品表格]]\n- [[发表稿/索引|网站文章]]\n\n## 全部笔记（实时）\n\n' + f'![[{ALL_BASE}]]\n'
    install_base(ALL_BASE, native_base())
    # A legacy entrance remains stable, and any later ordinary Blogs content is
    # still visible through its live Base rather than a frozen migration claim.
    blog_base = 'Blogs/' + BASE_NAME
    outputs['Blogs/索引.md'] = MARKER + '\n# Blogs\n\n[[首页|返回首页]] · [[笔记目录|全部主题]]\n\n- [[30_知识与参考/索引|竞赛文章、模板与技术参考]]\n- [[10_个人记录/求职与实习/索引|笔试与面试记录]]\n- [[90_归档/索引|历史博客实践]]\n\n' + f'![[{blog_base}]]\n\n旧配图继续保留原位置，已有网站文章保留身份、关联和发布版本。\n'
    install_base(blog_base, native_base(topic_scope('Blogs')))
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--adopt-existing', action='store_true', help='Replace previously backed-up legacy navigation once')
    args = parser.parse_args()
    library = Library(args.vault)

    def prepare():
        _, indexed = _index(library)
        outputs = generate(args.vault, list(indexed.values()))
        writes = []
        for relative, content in outputs.items():
            target = library._path(relative)
            before = target.read_text() if target.exists() else None
            if before == content:
                continue
            if relative.endswith('.base') and before is not None:
                continue
            if before is not None and not before.startswith(MARKER) and not args.adopt_existing:
                raise RuntimeError('Existing navigation retained: ' + relative)
            writes.append((relative, content, before))
        return writes

    if args.apply:
        with library._lock():
            writes = prepare()
            for relative, content, before in writes:
                assert (library._path(relative).read_text() if library._path(relative).exists() else None) == before, relative
                library._atomic(relative, content)
    else:
        writes = prepare()
    print(json.dumps({'applied': args.apply, 'changed': [relative for relative, _, _ in writes]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
