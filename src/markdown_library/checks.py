"""Mechanical editorial checks driven by a rules file kept with the Vault.

The engine is generic: the rules (which phrases, headings or names are not
allowed where) are the owner's private policy and live in the Vault, by default
at ``config/editorial-checks.json``. Checks only read; they never edit a note.

Rules file::

    {"version": 1, "rules": [
      {"id": "no-disclaimer", "type": "forbid", "pattern": "未核实",
       "message": "...", "kinds": ["note", "post"],
       "folders": ["10_个人记录/"], "exclude_folders": ["00_收件箱/"],
       "path_pattern": "面经", "ignore_line_pattern": "^\\[\\[.*\\|私人原稿\\]\\]$"}
    ]}

``path_pattern`` narrows a rule to matching paths. ``ignore_line_pattern`` skips
body lines a line-based rule should not look at, such as a private backlink.

Rule types:

``forbid``               pattern must not match any body line outside fenced code
``forbid-in-code-span``  pattern must not match the text of any inline code span
``require``              pattern must match at least one body line outside fenced code
``filename``             the file name without ``.md`` must match the pattern
"""
import json
from pathlib import PurePosixPath
import re

from .bootstrap import _index
from .core import LibraryError, _split

DEFAULT_RULES = 'config/editorial-checks.json'
TYPES = ('forbid', 'forbid-in-code-span', 'require', 'filename')
_SPAN = re.compile(r'(`+)(.+?)\1')


def load_rules(library, path=None):
    try:
        text = (library._path(DEFAULT_RULES) if path is None else path).read_text(encoding='utf-8')
        document = json.loads(text)
    except FileNotFoundError:
        raise LibraryError('No editorial rules file; create config/editorial-checks.json or pass --rules') from None
    except (OSError, ValueError):
        raise LibraryError('Editorial rules file is not readable JSON') from None
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('rules'), list):
        raise LibraryError('Editorial rules need {"version": 1, "rules": [...]}')
    rules, seen = [], set()
    for raw in document['rules']:
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or raw['id'] in seen:
            raise LibraryError('Every editorial rule needs a unique string id')
        seen.add(raw['id'])
        unknown = set(raw) - {'id', 'type', 'pattern', 'message', 'kinds', 'folders', 'exclude_folders', 'path_pattern',
                             'ignore_line_pattern'}
        if unknown or raw.get('type') not in TYPES or not isinstance(raw.get('pattern'), str) or not raw['pattern']:
            raise LibraryError(f'Invalid editorial rule {raw["id"]}')
        for name in ('kinds', 'folders', 'exclude_folders'):
            value = raw.get(name, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise LibraryError(f'Invalid {name} in editorial rule {raw["id"]}')
        try:
            rules.append(raw | {'_pattern': re.compile(raw['pattern']),
                                '_path': re.compile(raw['path_pattern']) if raw.get('path_pattern') else None,
                                '_ignore': re.compile(raw['ignore_line_pattern'])
                                           if raw.get('ignore_line_pattern') else None})
        except (re.error, TypeError):
            raise LibraryError(f'Invalid regular expression in editorial rule {raw["id"]}') from None
    return rules


def _applies(rule, note):
    path, kind = note['path'], note['properties']['kind']
    if rule.get('kinds') and kind not in rule['kinds']:
        return False
    if rule.get('folders') and not any(path.startswith(folder) for folder in rule['folders']):
        return False
    if any(path.startswith(folder) for folder in rule.get('exclude_folders', [])):
        return False
    return rule['_path'] is None or bool(rule['_path'].search(path))


def _prose_lines(markdown):
    """(line number in the whole note, text) for body lines outside fenced code."""
    try:
        body = _split(markdown)[1]
    except Exception:
        body = markdown
    first = markdown[:len(markdown) - len(body)].count('\n') + 1 if markdown.endswith(body) else 1
    fence = None
    for number, line in enumerate(body.split('\n'), first):
        marker = re.match(r'\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            yield number, line


def _excerpt(text, start, end, radius=30):
    left, right = max(0, start - radius), min(len(text), end + radius)
    return ('…' if left else '') + text[left:right].strip() + ('…' if right < len(text) else '')


def check_notes(library, *, ids=None, folder=None, kind=None, rules_path=None, limit=200):
    """Report rule violations in current notes. Read-only."""
    library.validate()
    if kind not in (None, 'note', 'work', 'post') or not 1 <= limit <= 1000:
        raise LibraryError('Use kind note/work/post and limit 1..1000')
    if folder is not None and (not folder or '\\' in folder or PurePosixPath(folder).is_absolute()
                               or any(part.startswith('.') for part in PurePosixPath(folder).parts)):
        raise LibraryError('Invalid visible folder scope')
    rules = load_rules(library, rules_path)
    prefix = folder.rstrip('/') + '/' if folder else None
    with library._lock():
        notes = _index(library)[1]
        if ids:
            selected = [library._required(notes, note_id) for note_id in dict.fromkeys(ids)]
        else:
            selected = list(notes.values())
        selected = sorted((n for n in selected if (kind is None or n['properties']['kind'] == kind)
                           and (prefix is None or n['path'].startswith(prefix))), key=lambda n: n['path'])
    findings, counts = [], {}

    def report(note, rule, line, excerpt):
        counts[rule['id']] = counts.get(rule['id'], 0) + 1
        if len(findings) < limit:
            findings.append({'id': note['id'], 'path': note['path'], 'rule': rule['id'],
                             'message': rule.get('message', ''), 'line': line, 'excerpt': excerpt})

    for note in selected:
        active = [rule for rule in rules if _applies(rule, note)]
        if not active:
            continue
        lines = list(_prose_lines(note['markdown']))
        for rule in active:
            pattern = rule['_pattern']
            ignored = rule['_ignore']
            considered = lines if ignored is None else [row for row in lines if not ignored.search(row[1])]
            if rule['type'] == 'filename':
                name = PurePosixPath(note['path']).name.removesuffix('.md')
                if not pattern.search(name):
                    report(note, rule, None, name[:80])
            elif rule['type'] == 'require':
                if not any(pattern.search(text) for _, text in considered):
                    report(note, rule, None, '')
            elif rule['type'] == 'forbid':
                for number, text in considered:
                    for match in pattern.finditer(text):
                        report(note, rule, number, _excerpt(text, match.start(), match.end()))
            else:
                for number, text in considered:
                    for span in _SPAN.finditer(text):
                        if pattern.search(span.group(2)):
                            report(note, rule, number, span.group(0)[:80])
    total = sum(counts.values())
    return {'rules': len(rules), 'checked': len(selected), 'clean': total == 0, 'total': total,
            'counts': dict(sorted(counts.items())), 'findings': findings, 'truncated': total > len(findings)}
