"""Daily time report from per-day activity aggregates kept with the Vault.

A device writes one JSON line per day into ``data/activitywatch/<yyyy-MM>.jsonl``
(see ``scripts/activitywatch-export.ps1``)::

    {"date": "2026-11-16", "device": "PC", "tz": "+08:00", "active_seconds": 22320,
     "hours": [0, ... 24 values], "apps": [{"app": "Code.exe", "seconds": 7800}]}

The library only reads those files. ``time_report`` summarizes selected days and,
with ``apply``, replaces the body of one heading in each daily note; every other
part of the note stays as the owner wrote it. Days that may still be running on
the recording device are left alone unless explicitly included.

Optional settings in ``config/time-tracking.json``::

    {"version": 1, "data_folder": "data/activitywatch", "daily_folder": "10_个人记录/日记",
     "heading": "时间", "template": "templates/每日笔记.md", "top_apps": 8,
     "app_names": {"Code.exe": "VS Code"},
     "categories": [{"name": "编程", "apps": ["Code.exe", "WindowsTerminal.exe"]}]}
"""
from datetime import date as _date, datetime, timedelta, timezone
import json
from pathlib import PurePosixPath
import re

from .core import LibraryError, _hash, _read, _split

SETTINGS = 'config/time-tracking.json'
DEFAULTS = {'data_folder': 'data/activitywatch', 'daily_folder': '10_个人记录/日记', 'heading': '时间',
            'template': 'templates/每日笔记.md', 'top_apps': 8, 'app_names': {}, 'categories': []}
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
_TZ = re.compile(r'([+-])(\d{2}):(\d{2})')


def load_settings(library):
    settings = dict(DEFAULTS)
    try:
        document = json.loads(library._path(SETTINGS).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return settings
    except (OSError, ValueError):
        raise LibraryError('Time tracking settings are not readable JSON') from None
    if not isinstance(document, dict) or document.get('version') != 1 or set(document) - set(DEFAULTS) - {'version'}:
        raise LibraryError('Time tracking settings need {"version": 1} and known keys only')
    settings.update({key: value for key, value in document.items() if key != 'version'})
    for key in ('data_folder', 'daily_folder', 'template'):
        value = settings[key]
        if (not isinstance(value, str) or not value or '\\' in value or PurePosixPath(value).is_absolute()
                or any(part.startswith('.') for part in PurePosixPath(value).parts)):
            raise LibraryError(f'Invalid {key} in time tracking settings')
    if not isinstance(settings['heading'], str) or not settings['heading'].strip() or '\n' in settings['heading']:
        raise LibraryError('Invalid heading in time tracking settings')
    if not isinstance(settings['top_apps'], int) or not 1 <= settings['top_apps'] <= 50:
        raise LibraryError('top_apps must be 1..50')
    names, categories = settings['app_names'], settings['categories']
    if not isinstance(names, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in names.items()):
        raise LibraryError('app_names must map application names to display names')
    if not isinstance(categories, list) or not all(
            isinstance(item, dict) and isinstance(item.get('name'), str) and item['name']
            and isinstance(item.get('apps'), list) and all(isinstance(app, str) for app in item['apps'])
            for item in categories):
        raise LibraryError('categories need a name and a list of apps each')
    return settings


def _record(raw):
    """Validated day record, or None for a line this reader does not understand."""
    if not isinstance(raw, dict) or not isinstance(raw.get('date'), str) or not _DATE.fullmatch(raw['date']):
        return None
    try:
        _date.fromisoformat(raw['date'])
    except ValueError:
        return None
    device, seconds, hours, apps = raw.get('device'), raw.get('active_seconds'), raw.get('hours'), raw.get('apps')
    if not isinstance(device, str) or not device or not _count(seconds):
        return None
    if not isinstance(hours, list) or len(hours) != 24 or not all(_count(value) for value in hours):
        hours = None
    rows = []
    for item in apps if isinstance(apps, list) else []:
        if isinstance(item, dict) and isinstance(item.get('app'), str) and item['app'] and _count(item.get('seconds')):
            rows.append((item['app'], item['seconds']))
    tz = raw.get('tz') if isinstance(raw.get('tz'), str) and _TZ.fullmatch(raw['tz']) else None
    return {'date': raw['date'], 'device': device, 'tz': tz, 'active_seconds': seconds, 'hours': hours, 'apps': rows}


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 90000


def load_days(library, settings):
    """{date: {device: record}} from every monthly file; a later line replaces an earlier one."""
    folder = library._path(settings['data_folder'])
    days, skipped = {}, 0
    if not folder.is_dir():
        return days, skipped
    for path in sorted(folder.glob('*.jsonl')):
        try:
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        except (OSError, UnicodeDecodeError):
            raise LibraryError(f'Unreadable activity file: {path.name}') from None
        for line in lines:
            if not line.strip():
                continue
            try:
                record = _record(json.loads(line))
            except ValueError:
                record = None
            if record is None:
                skipped += 1
            else:
                days.setdefault(record['date'], {})[record['device']] = record
    return days, skipped


def _duration(seconds):
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f'{hours} 小时 {minutes} 分'
    return f'{hours} 小时' if hours else f'{minutes} 分'


def _span(hours):
    active = [index for index, value in enumerate(hours or []) if value >= 60]
    return f'{active[0]}:00–{active[-1] + 1}:00' if active else None


def summarize(day, records, settings):
    """Deterministic figures for one day across devices."""
    names = settings['app_names']
    category_of = {app: item['name'] for item in settings['categories'] for app in item['apps']}
    apps, categories, listed = {}, {}, 0
    devices = []
    for device in sorted(records):
        record = records[device]
        devices.append({'device': device, 'active_seconds': record['active_seconds'], 'span': _span(record['hours'])})
        for app, seconds in record['apps']:
            listed += seconds
            label = names.get(app, app)
            apps[label] = apps.get(label, 0) + seconds
            if category_of:
                name = category_of.get(app, '未分类')
                categories[name] = categories.get(name, 0) + seconds
    total = sum(item['active_seconds'] for item in devices)
    ranked = sorted(apps.items(), key=lambda item: (-item[1], item[0]))
    top = ranked[:settings['top_apps']]
    other = total - sum(seconds for _, seconds in top)
    order = [item['name'] for item in settings['categories']] + ['未分类']
    return {'date': day, 'active_seconds': total, 'devices': devices,
            'apps': [{'app': app, 'seconds': seconds} for app, seconds in top],
            'other_seconds': max(0, other),
            'categories': [{'name': name, 'seconds': categories[name]} for name in order if categories.get(name)]}


def render(summary):
    """Markdown body for the heading; plain figures only."""
    lines = []
    for item in summary['devices']:
        span = f'，{item["span"]} 之间' if item['span'] else ''
        lines.append(f'- {item["device"]}：活跃 {_duration(item["active_seconds"])}{span}')
    if len(summary['devices']) > 1:
        lines.append(f'- 合计：{_duration(summary["active_seconds"])}')
    if summary['categories']:
        lines += ['', '| 分类 | 时长 |', '| --- | --- |']
        lines += [f'| {_cell(item["name"])} | {_duration(item["seconds"])} |' for item in summary['categories']]
    if summary['apps']:
        lines += ['', '| 应用 | 时长 |', '| --- | --- |']
        lines += [f'| {_cell(item["app"])} | {_duration(item["seconds"])} |' for item in summary['apps']]
        if summary['other_seconds'] >= 60:
            lines.append(f'| 其他 | {_duration(summary["other_seconds"])} |')
    return '\n'.join(lines)


def _cell(text):
    return text.replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ')


def replace_section(markdown, heading, content):
    """Replace the body under ``## heading``; None when the note has no such heading."""
    lines = markdown.split('\n')
    start = next((index for index, line in enumerate(lines) if line.rstrip() == f'## {heading}'), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(lines)) if re.match(r'#{1,2} ', lines[index])), len(lines))
    tail = lines[end:]
    section = [lines[start], '', *content.split('\n')] + (['', *tail] if tail else [''])
    return '\n'.join(lines[:start] + section)


def _still_running(day, records, now):
    """True when the day has not ended yet on a device that recorded it."""
    for record in records.values():
        offset = timezone.utc
        if record['tz']:
            sign, hours, minutes = _TZ.fullmatch(record['tz']).groups()
            offset = timezone((1 if sign == '+' else -1) * timedelta(hours=int(hours), minutes=int(minutes)))
        if day >= now.astimezone(offset).date().isoformat():
            return True
    return False


def _daily_note(library, path, *, create_missing, template, title):
    """Managed daily note at ``path``; registers an existing plain file in place."""
    target = library._path(path)
    if not target.is_file():
        if not create_missing:
            return None, 'no-note'
        try:
            body = _split(_read(library._path(template)))[1]
        except OSError:
            raise LibraryError(f'Daily note template is missing: {template}') from None
        return library.create(body, title=title, path=path), 'created'
    note = next((item for item in library.list_notes() if item['path'] == path), None)
    if note is not None:
        return library.get(note['id']), 'existing'
    registered = library.register(path, apply=True, expected_hash=_hash(_read(target)))
    return library.get(registered['id']), 'registered'


def time_report(library, *, dates=None, days=7, apply=False, create_missing=False, include_running=False, now=None):
    """Summaries for selected days; ``apply`` writes the heading body in each daily note."""
    library.validate()
    if not 1 <= days <= 366:
        raise LibraryError('Use days 1..366')
    for value in dates or []:
        if not _DATE.fullmatch(value):
            raise LibraryError('Dates are YYYY-MM-DD')
    settings = load_settings(library)
    recorded, skipped = load_days(library, settings)
    selected = sorted(dict.fromkeys(dates)) if dates else sorted(recorded)[-days:]
    now = now or datetime.now(timezone.utc)
    rows = []
    for day in selected:
        if day not in recorded:
            rows.append({'date': day, 'action': 'no-data'})
            continue
        summary = summarize(day, recorded[day], settings)
        content = render(summary)
        path = f'{settings["daily_folder"].rstrip("/")}/{day}.md'
        row = {**summary, 'path': path, 'markdown': content}
        if not include_running and _still_running(day, recorded[day], now):
            rows.append({**row, 'action': 'day-not-finished'})
            continue
        if not apply:
            target = library._path(path)
            if not target.is_file():
                action = 'would-create' if create_missing else 'no-note'
            else:
                updated = replace_section(_read(target), settings['heading'], content)
                action = 'no-heading' if updated is None else 'unchanged' if updated == _read(target) else 'would-update'
            rows.append({**row, 'action': action})
            continue
        note, origin = _daily_note(library, path, create_missing=create_missing,
                                   template=settings['template'], title=day)
        if note is None:
            rows.append({**row, 'action': origin})
            continue
        updated = replace_section(note['markdown'], settings['heading'], content)
        if updated is None:
            action = 'no-heading'
        elif updated == note['markdown']:
            action = 'unchanged'
        else:
            library.replace(note['id'], updated, expected_hash=note['sha256'])
            action = 'updated'
        rows.append({**row, 'id': note['id'], 'note': origin, 'action': action})
    return {'applied': apply, 'days': rows, 'skipped_lines': skipped,
            'recorded_days': len(recorded), 'heading': settings['heading']}
