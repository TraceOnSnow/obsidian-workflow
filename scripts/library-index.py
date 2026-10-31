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
            '!/(^|\\/)(copilot|assets|config|templates)(\\/|$)/.matches(file.path)',
            'kind == "post" || (file.inFolder("发表稿") && !kind)',
        ]},
        'formulas': {'entry': 'file.asLink(if(title, title, file.name))'},
        'properties': {'formula.entry': {'displayName': '文章'},
                       'file.folder': {'displayName': '所在文件夹'},
                       'file.mtime': {'displayName': '修改时间'}},
        'views': [{'type': 'table', 'name': '全部文章',
                   'order': ['formula.entry', 'file.folder', 'file.mtime'],
                   'sort': [{'property': 'file.name', 'direction': 'ASC'}]},
                  {'type': 'table', 'name': '待写稿',
                   'filters': 'file.inFolder("发表稿")',
                   'order': ['formula.entry', 'file.folder', 'file.mtime']},
                  {'type': 'table', 'name': '已发表存档',
                   'filters': 'file.inFolder("90_归档/已发表文章")',
                   'order': ['formula.entry', 'file.folder', 'file.mtime']}],
    }, allow_unicode=True, sort_keys=False)


def generate(library):
    state, indexed = _index(library)
    catalog_active = enabled(state)
    works_entry = state['work_catalog']['path'] if catalog_active else 'works/索引.md'

    header = [MARKER, '# 网站内容', '',
              '这里是私人内容库中的作品与文章目录。编辑正文只保存当前稿；网站使用明确选定的发布版本。', '',
              f'- [作品目录]({works_entry})：实时查看作品，编辑评分、进度和短感受。',
              '- [文章目录](发表稿/索引.md)：实时查看文章与新稿。',
              '- [发布与同步状态](发布与同步状态.md)：区分网站公开内容、后台草稿和库内笔记，查看未发布修改与冲突。',
              '- [日常使用](使用说明.md) · [原有笔记](笔记目录.md) · [首页](首页.md)', '',
              '目录随文件自动更新。网站是否成功同步请查看同步回执；受保护文章保留原稿，需单独处理发布。', '']
    work_lines = [MARKER, '# 作品目录', '', '[返回网站内容](../网站内容.md)', '',
                  '> [!tip] 直接在表格里编辑',
                  '> 双击评分、进度或短感受单元格即可修改；表格和作品笔记使用同一份属性。点击作品名称打开正文。', '',
                  '> [!info]- 评分、进度与发布说明',
                  '> - 切换视图可查看全部、进行中、待体验、已完成、未评分，或按类型分组；也可搜索、筛选和排序。',
                  '> - 评分采用百分制，填写 0–100，通常用整数（85 即 85 分）；已有小数精度可保留。留空表示未评分，0 是有效评分。',
                  '> - 进度沿用网站字段：`planned` 待体验 · `in-progress` 进行中 · `completed` 已完成 · `on-hold` 暂停 · `dropped` 已搁置；不确定时可留空。',
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
                  '表格包含各处的独立文章，包括归档文章；`发表稿/` 内的普通 Markdown 新稿也会显示。可搜索、按所在文件夹筛选，或点击文章名称继续编辑。', '',
                  '新建表格另有“待写稿”和“已发表存档”视图，按所在目录收纳；实际是否已发表以上方网站观察为准。已有表格的视图、列宽与布局保持。', '',
                  '新增、删除、移动与重命名文章后，Obsidian 自动更新此目录；普通新稿无需先注册身份。', '',
                  '私人笔记与独立 Post 各自修改，互不自动覆盖。公开稿归档只整理库内位置，不会撤回网站；编辑保存当前稿，明确发表后才更新网站。', '',
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
