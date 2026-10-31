#!/usr/bin/env python3
"""Review Steam paste plus retained IDs; batch-seed empty experience end dates."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from markdown_library import Library
from markdown_library.core import _split
from markdown_library.steam_review import parse_paste, review
from markdown_library.work_profile import compact_works, website_properties


def hours(value):
    import re
    amount = float(re.search(r'[\d.]+', value.replace(',', '')).group())
    return amount / 60 if 'minute' in value or '分钟' in value else amount


def report_body(plan, changed):
    body = ['# Steam 游戏清理待确认', '',
        f"核对日期：{plan['reference_date']}。本次粘贴 {plan['pasted_count']} 款；"
        f"{plan['pasted_with_achievements']} 款显示成就；"
        f"{plan['pasted_without_achievement_text']} 款未显示成就条目。", '',
        f"结合上次导入保留的 CSV 与稳定网站 ID，库内有成就证据的 {len(plan['keep'])} 款保留；"
        f"未显示成就、待你选择的 {len(plan['candidates'])} 款列在下方；"
        f"缺少 Steam 证据的 {len(plan['unknown'])} 款另列，不能当成零成就。", '',
        f"已用 Steam 最后运行日期补全 {changed} 款的体验结束日期，仅保存当前稿；没有删除、撤回或发布作品。"
        '已有结束日期优先保留，可以凭记忆微调；结束体验不自动代表通关。评分、状态、短感受和正文保留。', '',
        '> 未显示成就 ≠ 已确认零成就：有些游戏可能没有成就机制或页面未展示。'
        '有成就也不能单独证明实际游玩程度。时长只作你的挑选参考。', '',
        '## 如何选择', '',
        '勾选想保留的作品，或告诉我编号／名称；等你明确确认后，才按确认范围可恢复删除。'
        '勾选本身不会触发程序删除或发布。表格中的作品链接打开同一份作品笔记。', '']
    candidates = sorted(plan['candidates'], key=lambda x: (-hours(x['playtime']), x['title']))
    for number, item in enumerate(candidates, 1):
        item['review_number'] = f'C{number:03}'
    groups = [('优先确认：时长较长或有个人记录',
               [x for x in candidates if hours(x['playtime']) >= 15 or x['has_personal_content']]),
              ('其他待清理作品',
               [x for x in candidates if hours(x['playtime']) < 15 and not x['has_personal_content']])]
    for heading, items in groups:
        body += [f'## {heading}（{len(items)} 款）', '']
        for item in items:
            title = item['title'].replace('|', '／')
            link = f"[[{item['path'][:-3]}|{title}]]"
            source = '本次清单' if item['source'] == 'current_paste' else '上次导入 CSV'
            caution = '；有评分／感受／文章或私人正文，需留意' if item['has_personal_content'] else ''
            last = f"；最后运行 {item['lastPlayedOn']}" if item.get('lastPlayedOn') else ''
            body.append(f"- [ ] **保留 {item['review_number']}** {link} — {item['playtime']}{last}；{source}{caution}")
        body.append('')
    body += [f"## 缺少 Steam 证据，不列入删除候选（{len(plan['unknown'])} 款）", '']
    body += [f"- [[{x['path'][:-3]}|{x['title']}]]" for x in sorted(plan['unknown'], key=lambda x:x['title'])]
    body += ['', f"## 本次粘贴尚未匹配到库内作品（{len(plan['unmatched'])} 款）", '',
        '这些项目没有改名、合并、新建或删除。可能未曾收录，也可能存在名称／版本差异；需要补充时另行处理。', '']
    for x in plan['unmatched']:
        achievement = f"{x['unlocked']}/{x['total']}" if x['unlocked'] is not None else '未显示'
        body.append(f"- {x['steam_title']} — {x['playtime']}；成就 {achievement}；最后运行 {x['lastPlayedOn']}")
    body += ['', '## 本轮证据范围', '',
        '新清单优先用于日期；上次导入 CSV 保留的正成就证据不会被本次空白抹掉。'
        '两个来源都没有显示成就时，才进入待选清单。库中未出现在本次粘贴里的游戏，'
        '只有能通过既有导入 ID 找回原 CSV 成就记录时才分类。其他游戏不推断为零成就。', '']
    return '\n'.join(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--paste', type=Path, required=True)
    parser.add_argument('--legacy-csv', type=Path, required=True)
    parser.add_argument('--aliases', type=Path, help='Explicitly reviewed paste-title to stable-library-ID JSON')
    parser.add_argument('--reference-date', required=True)
    parser.add_argument('--output', type=Path, required=True, help='Private evidence directory outside the Vault')
    parser.add_argument('--report-path', default='works/整理/Steam游戏清理待确认.md',
                        help='New library note path; never overwrites an existing checklist')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.vault.resolve()):
        raise ValueError('Evidence must live outside the Vault')
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.chmod(0o700)
    library = Library(args.vault)
    notes = library.list_notes()
    original = {n['id']:n for n in notes}
    sync = json.loads((args.vault / '.markdown-library/sync-state.json').read_text())
    state_before = json.loads((args.vault / '.markdown-library/state.json').read_text())
    aliases = json.loads(args.aliases.read_text()) if args.aliases else {}
    with args.legacy_csv.open(encoding='utf-8-sig') as source:
        legacy = list(csv.DictReader(source))
    review_notes = [{**note, 'properties': website_properties(library, note)} for note in notes]
    plan = review(parse_paste(args.paste.read_text()), review_notes, legacy, sync['baselines'], aliases, args.reference_date)
    plan['source_sha256'] = hashlib.sha256(args.paste.read_bytes()).hexdigest()
    plan['legacy_sha256'] = hashlib.sha256(args.legacy_csv.read_bytes()).hexdigest()
    dates = {entry['id']: entry['lastPlayedOn'] for entry in plan['matched']}
    batch = compact_works(library, seed_steam=True, steam_dates=dates, note_ids=list(dates))
    changes = [{'id': item['id'], 'path': item['path'], 'expected_hash': original[item['id']]['sha256'],
                'endedOn': item['steam_end']['endedOn']}
               for item in batch['items'] if (item['steam_end'] or {}).get('status') == 'seeded']
    plan['date_target'] = 'experiences[].endedOn'
    plan['experience_date_review'] = [item for item in batch['items']
                                     if (item['steam_end'] or {}).get('status') == 'needs-review']
    plan['date_changes'] = changes
    (args.output/'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n')
    (args.output/'plan.json').chmod(0o600)
    if not args.apply:
        print(json.dumps({'apply':False,'date_updates':len(changes),'keep':len(plan['keep']),
            'candidates':len(plan['candidates']),'unknown':len(plan['unknown']),
            'unmatched_paste':len(plan['unmatched'])}))
        return
    # Validate report construction and collisions before changing any Work.
    body = report_body(plan,len(changes))
    report_path = args.report_path
    library._path(report_path)
    existing = next((n for n in notes if n['path']==report_path),None)
    if not existing and (args.vault/report_path).exists():
        raise ValueError('Unmanaged file occupies review path; preserve it and choose a new --report-path')
    # Exact original files are an extra batch rollback record; Library.replace
    # separately retains before/after history and enforces the current SHA.
    backups = args.output/'before'
    backups.mkdir(mode=0o700,exist_ok=True)
    for change in changes:
        note = original[change['id']]
        saved = backups/(note['id']+'.md')
        if saved.exists() and saved.read_bytes() != note['markdown'].encode():
            raise ValueError('Previous batch backup differs; use a fresh evidence directory')
        saved.write_bytes(note['markdown'].encode())
        saved.chmod(0o600)
    applied_batch = compact_works(library, apply=True, expected_token=batch['expected_token'],
                                  seed_steam=True, steam_dates=dates, note_ids=list(dates))
    applied = []
    for item in applied_batch['items']:
        original_note = original[item['id']]
        disk = (args.vault/item['path']).read_bytes()
        assert hashlib.sha256(disk).hexdigest() == item['sha256']
        assert _split(disk.decode())[1] == _split(original_note['markdown'])[1]
        if (item['steam_end'] or {}).get('status') == 'seeded':
            applied.append({'id':item['id'],'before':original_note['sha256'],'after':item['sha256']})
    (args.output/'applied.json').write_text(json.dumps(applied,indent=2)+'\n')
    (args.output/'applied.json').chmod(0o600)
    state_after = json.loads((args.vault / '.markdown-library/state.json').read_text())
    assert state_after['publications'] == state_before['publications']
    for note_id, entry in state_before['notes'].items():
        assert {k:v for k,v in entry.items() if k not in {'history','path','sha256','work_profile'}} == {
            k:v for k,v in state_after['notes'][note_id].items() if k not in {'history','path','sha256','work_profile'}}
    if existing:
        # A successful rerun must preserve hand-checked choices and report text.
        report = existing
    else:
        report = library.create(body,title='Steam游戏清理待确认',kind='note',path=report_path)
    # A note property update must never select a new publication or deletion.
    summary = {'apply':True,'date_updates':len(applied),'keep':len(plan['keep']),
        'candidates':len(plan['candidates']),'unknown':len(plan['unknown']),
        'unmatched_paste':len(plan['unmatched']),'report_path':report['path'],
        'existing_checklist_preserved': bool(existing),
        'publication_and_lifecycle_state_unchanged':True,'website_published':False,'deleted':0}
    (args.output/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (args.output/'result.json').chmod(0o600)
    print(json.dumps(summary,ensure_ascii=False))


if __name__ == '__main__':
    main()
