#!/usr/bin/env python3
"""Build Markdown navigation, preserving the active Work storage and user views."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path

import yaml

from markdown_library.bootstrap import _index
from markdown_library.core import Library, LibraryError
from markdown_library.work_catalog import enabled

MARKER = '<!-- markdown-library:index:v1; generated navigation -->'
BASE_PATH = 'works/作品表格.base'
POST_BASE_PATH = '发表稿/文章表格.base'
LEGACY_PATH = '90_归档/整理记录/作品目录（旧快照）.md'
BASE_TEMPLATE = Path(__file__).resolve().parents[1] / 'assets/works-table.base'


def post_base():
    """Native membership follows files, including ordinary untyped post drafts."""
    return '# markdown-library:post-base:v1; installed once, user editable\n' + yaml.safe_dump({
        'filters': {'and': [
            'file.ext == "md"',
            '!/(^|\\/)\\./.matches(file.path)',
            '!/(^|\\/)(索引|AGENTS|README|SKILL)\\.md$/.matches(file.path)',
            '!/(^|\\/)(copilot|assets|config|templates|TaskNotes)(\\/|$)/.matches(file.path)',
            'kind == "post" || (file.inFolder("发表稿") && !kind)',
        ]},
        'formulas': {'entry': 'file.asLink(if(title, title, file.name))'},
        'properties': {'formula.entry': {'displayName': '文章'},
                       'file.folder': {'displayName': '所在文件夹'},
                       'file.mtime': {'displayName': '修改时间'}},
        'views': [{'type': 'table', 'name': '全部文章',
                   'order': ['formula.entry', 'file.folder', 'file.mtime'],
                   'sort': [{'property': 'file.name', 'direction': 'ASC'}]}],
    }, allow_unicode=True, sort_keys=False)


def generate(library):
    state, indexed = _index(library)
    catalog_active = enabled(state)
    works_entry = state['work_catalog']['path'] if catalog_active else 'works/索引.md'

    header = [MARKER, '# 网站内容', '',
              '[[首页|返回首页]] · [[使用说明|操作说明]]', '',
              f'- [作品目录]({works_entry})：实时查看作品，编辑评分、进度和短感受。',
              '- [文章目录](发表稿/索引.md)：统一维护 `发表稿/` 中的草稿和已发表稿，发表后位置不变。',
              '- [发布与同步状态](发布与同步状态.md)：区分网站公开内容、后台草稿和库内笔记，查看未发布修改与冲突。',
              '']
    work_lines = [MARKER, '# 作品目录', '', '[返回网站内容](../网站内容.md)', '',
                  '> [!tip] 直接在表格里编辑',
                  '> 双击评分、进度或短感受单元格即可修改；表格和作品笔记使用同一份属性。点击作品名称打开正文。', '',
                  '> [!info]- 评分、进度与发布说明',
                  '> - 切换视图可查看全部、进行中、完成、未评分，或按类型分组；也可搜索、筛选和排序。',
                  '> - 评分采用百分制，填写 0–100，通常用整数（85 即 85 分）；已有小数精度可保留。留空表示未评分，0 是有效评分。',
                  '> - 进度沿用网站字段：`in-progress` 进行中 · `completed` 完成（游戏显示通关、动漫与电视剧追完、书读完）· `on-hold` 搁置 · `dropped` 弃坑；电影和桌游不填状态，不确定时可留空。',
                  '> - 修改保存为当前稿，并由官方Sync同步。长感想直接写在作品正文；准备关联文章草稿后，明确发布才更新网站。',
                  '> - “按体验结束”展示体验记录的最近结束日期；这是公式列，修改日期请打开作品原文。', '',
                  f'![[{BASE_PATH}#全部作品]]', '',
                  f'也可以[[{BASE_PATH}|单独打开作品表格]]，获得更大的编辑空间。列宽、视图、筛选和排序可自行调整，目录工具保留已有表格设置。', '']
    if catalog_active:
        catalog_path = state['work_catalog']['path']
        work_lines = [MARKER, '# 作品目录', '', '[返回网站内容](../网站内容.md)', '',
                      '> [!tip] 在作品总表里编辑',
                      '> 作品名称、类型、评分、进度、短评和体验日期统一在总表维护；点击“感想”列打开已有私人正文。', '',
                      '> [!info]- 评分、日期与发布说明',
                      '> - 评分采用百分制，填写 0–100，通常用整数（85 即 85 分）；已有小数精度可保留。留空表示未评分，0 是有效评分。',
                      '> - 开始与结束日期可直接修改，保留已有日期精度；多次体验记录和补充字段继续保留。',
                      '> - 已公开作品的评分、进度、短评和体验日期通过原同步守卫更新网站卡片；两端同时修改时保留冲突。',
                      '> - 新增作品不会自动首次公开，私人感想不会随作品卡片公开；独立文章仍需明确发表。', '',
                      f'![[{catalog_path}]]', '',
                      f'[[{catalog_path}|单独打开作品总表]]，可搜索、筛选、排序和调整列宽；已有视图和布局保留。', '']
    post_lines = [MARKER, '# 文章目录', '', '[返回网站内容](../网站内容.md)', '',
                  '![[发布与同步状态#文章发表]]', '',
                  '## 全部文章与待写稿', '',
                  f'![[{POST_BASE_PATH}]]', '',
                  '独立发表稿统一放在 `发表稿/`，未发表和已发表的稿件都在这里维护。可搜索或点击文章名称继续编辑。', '',
                  '实际是否已发表、是否有未发布修改，以上方网站观察为准；不再用所在文件夹区分发表状态。已有表格的视图、列宽与布局保持。', '',
                  '新增、删除、移动与重命名文章后，Obsidian 自动更新此目录；普通新稿无需先注册身份。', '',
                  '私人原稿留在主题目录，与独立发表稿各自修改。发表后文件仍留在这里；是否已发表、有无未发布修改，以发布与同步状态为准。修改发表稿后明确发布，才更新对应网站文章。', '',
                  '完整网站观察与同步情况见[[发布与同步状态]]。', '']
    outputs = {'网站内容.md': '\n'.join(header), 'works/索引.md': '\n'.join(work_lines),
               '发表稿/索引.md': '\n'.join(post_lines)}
    # Native Obsidian saves widths, sorting and user-created views into this file.
    # Install once: later index refreshes must not replace those personal settings.
    if not catalog_active and not library._path(BASE_PATH).exists():
        outputs[BASE_PATH] = BASE_TEMPLATE.read_text(encoding='utf-8')
    if not library._path(POST_BASE_PATH).exists():
        outputs[POST_BASE_PATH] = post_base()
    return outputs, Counter(n['properties']['kind'] for n in indexed.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    library = Library(args.vault)
    # A preview does not create a lock, history, identity or publication.
    def prepare():
        outputs, counts = generate(library)
        for relative in outputs:
            target = library._path(relative)
            if relative not in {BASE_PATH, POST_BASE_PATH} and target.exists() and not target.read_text(encoding='utf-8').startswith(MARKER + '\n'):
                raise LibraryError(f'Existing user navigation retained: {relative}')
        return outputs, counts

    def create_new(relative, content):
        target = library._path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        library._check_components(target.parent)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return True

    if args.apply:
        with library._lock():
            outputs, counts = prepare()
            previous = library._path('works/索引.md')
            if (not enabled(library._read_state()) and previous.exists()
                    and '![[works/作品表格.base' not in previous.read_text(encoding='utf-8')):
                create_new(LEGACY_PATH, previous.read_text(encoding='utf-8'))
            changed = [relative for relative, content in outputs.items()
                       if not library._path(relative).exists() or library._path(relative).read_text(encoding='utf-8') != content]
            for relative in changed:
                if relative in {BASE_PATH, POST_BASE_PATH}:
                    create_new(relative, outputs[relative])
                else:
                    library._atomic(relative, outputs[relative])
    else:
        outputs, counts = prepare()
        changed = [relative for relative, content in outputs.items()
                   if not library._path(relative).exists() or library._path(relative).read_text(encoding='utf-8') != content]
    print(json.dumps({'applied': args.apply, 'managed_counts': counts, 'changed_indexes': changed}, ensure_ascii=False))


if __name__ == '__main__':
    main()
