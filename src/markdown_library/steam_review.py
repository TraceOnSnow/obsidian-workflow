"""Deterministic Steam pasted-page review; no network, publication or deletion.

Steam dates seed experience ends; a last run never implies completing a game.
Missing achievement text is an observation, never proof of zero achievements.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import re
import unicodedata

from .core import LibraryError, _split


def title_key(value):
    value = unicodedata.normalize('NFKC', value.replace('™', '').replace('®', '')).casefold()
    return ''.join(c for c in value if c.isalnum())


def parse_paste(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    entries, seen = [], set()
    for index, line in enumerate(lines):
        if line != '总游戏时间':
            continue
        if index < 2 or lines[index - 2] not in {lines[index - 1], lines[index - 1] * 2}:
            raise LibraryError('Unrecognized Steam title boundary')
        title = lines[index - 1]
        if title in seen:
            raise LibraryError('Duplicate Steam title requires review')
        seen.add(title)
        try:
            end = lines.index('我的游戏内容', index)
            chunk = lines[index:end]
            if len(chunk) < 4 or chunk[2] != '最后运行日期':
                raise ValueError()
            if not re.fullmatch(r'[\d.,]+\s*(小时|分钟)', chunk[1]):
                raise ValueError()
            achievement = chunk[chunk.index('成就') + 1] if '成就' in chunk else None
            if achievement is not None:
                match = re.fullmatch(r'(\d+)\s*/\s*(\d+)', achievement)
                if not match:
                    raise ValueError()
                unlocked, total = map(int, match.groups())
                if not 0 <= unlocked <= total or not total:
                    raise ValueError()
            else:
                unlocked = total = None
        except (IndexError, ValueError):
            raise LibraryError(f'Incomplete or invalid Steam entry: {title}') from None
        entries.append({'steam_title': title, 'playtime': chunk[1], 'date_raw': chunk[3],
                        'unlocked': unlocked, 'total': total})
    if not entries:
        raise LibraryError('No complete Steam entries found')
    return entries


def resolve_date(value, reference):
    if isinstance(reference, str):
        reference = date.fromisoformat(reference)
    if value in {'今天', '昨天', '前天'}:
        return (reference - timedelta(days={'今天': 0, '昨天': 1, '前天': 2}[value])).isoformat()
    full = re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日', value)
    short = re.fullmatch(r'(\d{1,2})月(\d{1,2})日', value)
    if full:
        result = date(*map(int, full.groups()))
    elif short:
        result = date(reference.year, *map(int, short.groups()))
    else:
        raise LibraryError(f'Unknown Steam date: {value}')
    if result > reference:
        raise LibraryError(f'Steam date is later than the confirmed reference date: {value}')
    return result.isoformat()


def review(entries, notes, legacy_rows, baselines, aliases, reference):
    games = {n['id']: n for n in notes if n['properties']['kind'] == 'work'
             and n['properties'].get('type') == 'game'}
    remote = {str(b['remote_id']): note_id for note_id, b in baselines.items()
              if b.get('collection') == 'works' and note_id in games}
    names, observations = defaultdict(set), {}
    for note_id, note in games.items():
        for name in [note['properties']['title'], *(note['properties'].get('aliases') or [])]:
            names[title_key(name)].add(note_id)
    # These are retained import observations, not the live website database.
    for row in legacy_rows:
        note_id = remote.get(row.get('Payload_ID', ''))
        if not note_id:
            continue
        if note_id in observations:
            raise LibraryError('Multiple legacy Steam rows map to one Work; review before proceeding')
        raw = row['已解锁成就']
        unlocked = int(raw) if raw else None
        total = int(row['成就总数']) if row['成就总数'] else None
        observations[note_id] = {'steam_title': row['游戏名称'], 'playtime': row['原始游玩时长'],
            'unlocked': unlocked, 'total': total, 'source': 'retained_import'}
        names[title_key(row['游戏名称'])].add(note_id)
    assigned, matched, unmatched = set(), [], []
    for item in entries:
        current = dict(item)
        current['lastPlayedOn'] = resolve_date(item['date_raw'], reference)
        hits = names.get(title_key(item['steam_title']), set())
        if item['steam_title'] in aliases:
            chosen = aliases[item['steam_title']]
            if chosen not in games or (hits and hits != {chosen}):
                raise LibraryError('Reviewed alias conflicts with current library identity')
            hits = {chosen}
        if len(hits) != 1:
            unmatched.append(current)
            continue
        note_id = next(iter(hits))
        if note_id in assigned:
            raise LibraryError('Multiple pasted entries map to one Work; dates cannot be merged implicitly')
        assigned.add(note_id)
        current.update(id=note_id, path=games[note_id]['path'], title=games[note_id]['properties']['title'])
        previous = observations.get(note_id)
        # A page omitting the achievement widget cannot erase prior positive evidence.
        current['prior_unlocked'] = previous['unlocked'] if previous else None
        current['has_achievement_evidence'] = bool((current['unlocked'] or 0) > 0 or
                                                (current['prior_unlocked'] or 0) > 0)
        observations[note_id] = {**current, 'source': 'current_paste'}
        matched.append(current)
    known, candidates, unknown = [], [], []
    for note_id, note in games.items():
        observation = observations.get(note_id)
        value = {'id': note_id, 'path': note['path'], 'title': note['properties']['title']}
        value['has_personal_content'] = bool(note['properties'].get('rating') is not None
            or note['properties'].get('note') or note['properties'].get('article')
            or _split(note['markdown'])[1].strip())
        if not observation:
            unknown.append(value)
            continue
        value.update(observation)
        if observation.get('has_achievement_evidence', (observation.get('unlocked') or 0) > 0):
            known.append(value)
        else:
            candidates.append(value)
    return {'reference_date': str(reference), 'pasted_count': len(entries),
        'pasted_with_achievements': sum((e['unlocked'] or 0) > 0 for e in entries),
        'pasted_without_achievement_text': sum(e['unlocked'] is None for e in entries),
        'matched': matched, 'unmatched': unmatched, 'keep': known,
        'candidates': candidates, 'unknown': unknown}


def replace_last_played(markdown, value):
    """Change only one scalar property; preserve all other YAML text and body."""
    properties, _ = _split(markdown)
    previous = properties.get('lastPlayedOn')
    if previous:
        try:
            previous_date = date.fromisoformat(previous)
        except (ValueError, TypeError):
            raise LibraryError('Existing lastPlayedOn is not an exact date; review it first') from None
        if previous_date >= date.fromisoformat(value):
            return markdown
    lines = markdown.splitlines(keepends=True)
    if lines[0].strip() != '---':
        raise LibraryError('Work frontmatter is required')
    end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() in {'---', '...'})
    newline = '\r\n' if lines[0].endswith('\r\n') else '\n'
    import yaml
    node = yaml.compose(''.join(lines[1:end]))
    matches = [(key, val) for key, val in node.value if key.value == 'lastPlayedOn']
    if len(matches) > 1:
        raise LibraryError('Duplicate lastPlayedOn property')
    updated = f"lastPlayedOn: '{value}'{newline}"
    if matches:
        key, val = matches[0]
        # Scalar YAML ranges may include trailing newline; replacing the whole
        # property preserves everything outside that property's source lines.
        start = key.start_mark.line + 1
        stop = val.end_mark.line + 1 + (1 if val.end_mark.column else 0)
        lines[start:stop] = [updated]
    else:
        lines.insert(end, updated)
    result = ''.join(lines)
    checked, _ = _split(result)
    assert checked == {**properties, 'lastPlayedOn': value}
    return result
