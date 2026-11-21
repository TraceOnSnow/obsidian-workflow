"""Read a job-search tracker note and regenerate its stage summary.

Event lines keep the readable timeline form, with a fixed stage word and a
fixed result word so a program can count them:

    - 10-28 贝塔科技｜游戏策划｜二面 — 挂；原定10-27顺延至此
    - 09-28~ 科大讯飞｜AI Agent 应用研发工程师｜笔试 — 没消息
    - 10-26 [[path/复盘|柠檬微趣｜AI应用开发工程师｜AI面]] — 过

The date is MM-DD (``~`` marks an approximate day) with an optional
parenthesised qualifier. The first ``｜`` segment is the company, the last is
the stage, anything between is the role. After `` — `` comes one result word,
optionally followed by ``；`` and free notes. Lines that do not parse are
reported, never guessed. The summary between the two markers is generated;
everything else in the note belongs to its author.
"""
import re
from collections import Counter

from .core import LibraryError

STAGES = ('简历', '测评', '笔试', 'AI面', '一面', '二面', '三面', '主管面', 'HR面', '沟通')
RESULTS = ('过', '挂', '没消息', '等结果', '结束', '已邀请', '未参加', '待安排')
ATTENDED = {'过', '挂', '没消息', '等结果'}
GROUPS = (('笔试', ('笔试',)), ('AI 面试', ('AI面',)), ('一面', ('一面',)), ('二面', ('二面',)),
          ('三面 / 主管 / HR', ('三面', '主管面', 'HR面')))
FINAL_LABELS = {'笔试': '笔试挂', 'AI面': 'AI面挂', '一面': '一面挂', '二面': '二面挂',
                '三面': '三面挂', '主管面': '主管面挂', 'HR面': 'HR面挂'}
ORDER = {stage: index for index, stage in enumerate(STAGES)}
START = '<!-- 阶段性总结：程序生成，勿手改 -->'
END = '<!-- 阶段性总结结束 -->'

_LINE = re.compile(r'^- (?P<date>\d\d-\d\d)(?P<approx>~)?(?:（[^）]*）)? (?P<body>.+?) — (?P<result>[^；。]+)(?:[；。](?P<note>.*))?$')
_LINK = re.compile(r'^\[\[[^\]|]+\|(?P<alias>[^\]]+)\]\]$')


def parse_line(line):
    """Return an event dict, None for a non-event line, or raise for a malformed event."""
    if not re.match(r'^- \d\d-\d\d', line):
        return None
    match = _LINE.match(line.rstrip())
    if not match:
        raise LibraryError('格式不符：' + line)
    body = match['body']
    link = _LINK.match(body)
    segments = [part.strip() for part in (link['alias'] if link else body).split('｜')]
    if len(segments) < 2:
        raise LibraryError('缺少公司或环节：' + line)
    stage, result = segments[-1], match['result'].strip()
    if stage not in STAGES:
        raise LibraryError(f'环节“{stage}”不在 {"/".join(STAGES)} 中：' + line)
    if result not in RESULTS:
        raise LibraryError(f'结果“{result}”不在 {"/".join(RESULTS)} 中：' + line)
    return {'date': match['date'], 'approximate': bool(match['approx']), 'company': segments[0],
            'role': '｜'.join(segments[1:-1]), 'stage': stage, 'result': result,
            'note': (match['note'] or '').strip(), 'linked': bool(link)}


def parse(markdown):
    events, problems = [], []
    for number, line in enumerate(markdown.split(START)[0].splitlines(), 1):
        try:
            event = parse_line(line)
        except LibraryError as exc:
            problems.append({'line': number, 'error': str(exc)})
            continue
        if event:
            events.append(event)
    return events, problems


def _names(events):
    counts = Counter(event['company'] for event in events)
    seen, names = set(), []
    for event in events:
        if event['company'] not in seen:
            seen.add(event['company'])
            names.append(event['company'] + (f' ×{counts[event["company"]]}' if counts[event['company']] > 1 else ''))
    return '、'.join(names)


def _applications(events):
    """Group events by company and role, the unit the summary counts."""
    groups = {}
    for event in events:
        groups.setdefault((event['company'], event['role']), []).append(event)
    return groups


def summarize(events, *, as_of):
    lines = [START, '', f'## 阶段性总结（截至{as_of}）', '',
             '按环节统计，不计测评。同一家公司的不同岗位分开算；结果按每行记录的结果词统计。', '']
    ended = [e for e in events if e['stage'] == '简历' and e['result'] in {'结束', '挂'}]
    lines += [f'**简历阶段结束：{len(ended)}**', '- ' + (_names(ended) or '无'), '']
    for label, stages in GROUPS:
        rows = [e for e in events if e['stage'] in stages and e['result'] in ATTENDED]
        lines.append(f'**{label}：{len(rows)}**')
        for result in ('过', '挂', '没消息', '等结果'):
            chosen = [e for e in rows if e['result'] == result]
            if chosen:
                lines.append(f'- {result} {len(chosen)}：{_names(chosen)}')
        lines.append('')
    # Progressive view: each company only counts at the furthest round it reached;
    # several roles that all reached that round are shown as ×N.
    final, furthest = {}, {}
    for application in _applications(events).values():
        attended = [e for e in application if e['result'] in ATTENDED]
        if attended:
            last = max(attended, key=lambda e: (ORDER[e['stage']], e['date']))
            company = last['company']
            furthest[company] = max(furthest.get(company, -1), ORDER[last['stage']])
            final.setdefault(company, []).append(last)
    buckets = {}
    for company, lasts in final.items():
        for last in lasts:
            if ORDER[last['stage']] == furthest[company]:
                bucket = '还在流程里' if last['result'] in {'过', '等结果'} else FINAL_LABELS[last['stage']]
                buckets.setdefault(bucket, []).append(last)
    final = buckets
    lines += ['**按最后一轮（每家公司只算走到最远的一轮，没消息也算挂）**']
    for bucket in ['还在流程里', 'HR面挂', '主管面挂', '三面挂', '二面挂', '一面挂', 'AI面挂', '笔试挂']:
        if bucket in final:
            lines.append(f'- {bucket}：{_names(final[bucket])}')
    lines += ['', END]
    return '\n'.join(lines)


def regenerate(markdown, *, as_of):
    events, problems = parse(markdown)
    if problems:
        return None, events, problems
    summary = summarize(events, as_of=as_of)
    if START in markdown:
        if markdown.count(START) != 1 or markdown.count(END) != 1 or markdown.index(END) < markdown.index(START):
            raise LibraryError('总结标记不完整或重复')
        head, rest = markdown.split(START, 1)
        tail = rest.split(END, 1)[1]
        return head + summary + tail, events, problems
    return markdown.rstrip('\n') + '\n\n' + summary + '\n', events, problems


def tracker_summary(library, note_id, *, as_of, apply=False):
    note = library.get(note_id)
    updated, events, problems = regenerate(note['markdown'], as_of=as_of)
    result = {'id': note_id, 'events': len(events), 'problems': problems, 'applied': False}
    if problems:
        return result | {'status': 'needs_review'}
    result |= {'changed': updated != note['markdown'], 'summary': summarize(events, as_of=as_of)}
    if apply and result['changed']:
        saved = library.replace(note_id, updated, expected_hash=note['sha256'])
        result |= {'applied': True, 'sha256': saved['sha256']}
    return result | {'status': 'applied' if apply else 'preview'}
