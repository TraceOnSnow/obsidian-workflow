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
ROOTS = ['00_收件箱', '05_想法池', '10_个人记录', '20_项目与构想', '25_游戏设计', '30_知识与参考', '90_归档']
# Only these working folders get a generated page and live Base (2026-11-13).
# Topic folders are browsed in Obsidian's file explorer and the all-notes Base;
# Works and 发表稿 keep their own index generator.
PAGE_FOLDERS = ('00_收件箱', '05_想法池')
BASE_NAME = '实时目录.base'
INBOX_BASE = '00_收件箱/待整理（实时）.base'
ALL_BASE = '全部笔记.base'
RESERVED_ROOTS = {'works', '发表稿', 'posts', 'notes', 'copilot', 'assets', 'config', 'templates'}
UTILITY_NAMES = ('索引', '首页', '笔记目录', '网站内容', '发布与同步状态', '使用说明',
                 'Copilot库内助手', 'AGENTS', 'CLAUDE', 'HANDOFF', 'PLAN', 'README', 'SKILL')


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
    parents = set(ROOTS)
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
                    # An old generated index must not keep an otherwise empty
                    # category alive after its drafts return to Inbox. A manual
                    # index is content in its own right and remains discoverable.
                    if not path.read_text(encoding='utf-8').startswith(MARKER):
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
            if '主题导航' in tags and not relative.startswith(('00_收件箱/', 'notes/', '90_归档/')):
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

    for parent in PAGE_FOLDERS:
        base = INBOX_BASE if parent == '00_收件箱' else parent + '/' + BASE_NAME
        lines = [MARKER, '# ' + Path(parent).name, '', '[[首页|首页]] · [[笔记目录|全部主题]]', '']
        if parent == '00_收件箱':
            lines += ['这里保留仍需辨认、核对或梳理的材料。本人认领的初步想法可以进入想法池；完整成稿再归入主题目录，不把纯AI方案自动认领为本人想法。', '']
        if parent == '05_想法池':
            lines += ['这里保留已经认领、值得以后继续探索，但暂时不准备推进的想法。无需排期或补齐实现方案；想继续时再拿出来讨论。待核对材料仍在Inbox，完整文章和阶段设计留在主题目录。', '']
        featured = {path: title for path, title in maps.items() if str(Path(path).parent) == parent}
        curated = {}
        # Retain manually chosen reading links (not just tagged map notes).
        # Links into Inbox are working material, not formal reading entrances.
        existing = vault / parent / '索引.md'
        if existing.is_file():
            current = existing.read_text(encoding='utf-8')
            if current.startswith(MARKER) and '## 从这里开始读\n' in current:
                section = current.split('## 从这里开始读\n', 1)[1].split('\n## ', 1)[0]
                for line in section.splitlines():
                    if not line.startswith('- [[') or not line.endswith(']]'):
                        continue
                    target, separator, label = line[4:-2].partition('|')
                    path = target.removesuffix('.md') + '.md'
                    if (separator and not Path(path).is_absolute() and '..' not in Path(path).parts
                            and not target.startswith(('00_收件箱/', 'notes/'))
                            and (vault / path).is_file()):
                        curated.setdefault(path, label)
        featured = {**curated, **{path: title for path, title in featured.items() if path not in curated}}
        if featured:
            lines += ['## 从这里开始读', '']
            lines += ['- ' + wiki(path, title) for path, title in featured.items()]
            lines.append('')
        lines += ['## 实时目录', '', f'![[{base}]]', '',
                  '新增、删除、重命名或移动笔记后，此目录由 Obsidian 自动更新。默认包含子文件夹；切换“按文件夹”查看归属。', '']
        outputs[parent + '/索引.md'] = '\n'.join(lines)
        install_base(base, native_base(topic_scope(parent), inbox=parent == '00_收件箱'))

    outputs['首页.md'] = '\n'.join([
        MARKER, '# 首页', '', '从这里进入主题、待整理材料和独立发表稿。', '',
        '## 日常入口', '',
        '- [[00_收件箱/索引|收件箱]]：需要本人补充、确认或继续梳理的材料。',
        '- [[05_想法池/索引|想法池]]：已经认领、暂未排期的想法。',
        '- [[works/索引|作品表格]]：维护评分、进度与短感受。',
        '- [[笔记目录|全部笔记]]：按文件夹查看个人记录、项目、游戏设计、知识参考和归档。', '',
        '主题文件夹直接在左侧文件栏浏览，不另设目录页。', '',
        '## 写作与发布', '',
        '- [[templates/README|记录模板]]：笔试、面试与感想正文的起点。',
        '- [[使用说明|使用说明]] · [[Copilot库内助手|Copilot 库内助手]]',
        '- [[发布与同步状态|发布与同步状态]] · [[发表稿/索引|全部网站文章]]',
        '- [[笔记目录|全部主题与实时笔记目录]]', '',
        '私人原稿留在主题目录，独立发表稿统一放在发表稿文件夹，两稿各自修改。发表完成后位置不变；网站使用明确发布的版本，公开状态见发布与同步状态。', ''])
    if maps:
        section = '## 按问题串读\n\n' + '\n'.join(
            '- ' + wiki(path, title) for path, title in sorted(maps.items(), key=lambda pair: pair[1])) + '\n\n'
        outputs['首页.md'] = outputs['首页.md'].replace('## 写作与发布\n', section + '## 写作与发布\n', 1)
    # Home is curated after installation. Its maintained source and guarded
    # distribution own future changes; topic refresh must not erase user edits.
    home = vault / '首页.md'
    if home.is_file() and home.read_text().startswith(MARKER):
        outputs['首页.md'] = home.read_text()
    outputs['笔记目录.md'] = (MARKER + '\n# 全部笔记\n\n[[首页|返回首页]]\n\n'
        '- [[00_收件箱/索引|收件箱]]\n- [[05_想法池/索引|想法池]]\n- [[works/索引|作品表格]]\n- [[发表稿/索引|网站文章]]\n\n'
        '主题文件夹直接在左侧文件栏浏览；下方切换“按文件夹”可按实际路径查看。\n\n'
        '## 全部笔记（实时）\n\n' + f'![[{ALL_BASE}]]\n')
    install_base(ALL_BASE, native_base())
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
