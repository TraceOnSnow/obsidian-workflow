"""Narrow, recoverable library actions used by the server MCP adapter.

No website client or publication operation belongs here. All working-file
changes and tombstones share the library's existing guarded journal.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
from urllib.parse import quote, unquote
from uuid import uuid4

from .core import Library, LibraryError, _hash, _now, _read, _registration_frontmatter, _split


_ROOTS = {"00_收件箱", "05_想法池", "notes", "10_个人记录", "20_项目与构想", "25_游戏设计", "30_知识与参考", "90_归档"}
_NAVIGATION = {"首页", "索引", "目录", "使用说明", "网站内容", "发布与同步状态", "AGENTS", "SKILL", "README", "Copilot库内助手"}
_RESERVED_NAMES = {name.casefold() for name in _NAVIGATION}
_CONFIG_FOLDERS = {"copilot", "templates", "assets", "config"}
_WIKI = re.compile(r"(?<!\\)\[\[([^\]\n]+)\]\]")
_URL = r"<[^>\n]+>|(?:[^\s()<>\\]|\\.|\((?:[^\s()<>\\]|\\.)*\))+"
_MARKDOWN = re.compile(
    r"(?P<prefix>(?<!\\)\[[^\]\n]*\]\([ \t]*)"
    r"(?P<url>" + _URL + r")"
    r"(?P<tail>(?:[ \t]+(?:\"[^\"\n]*\"|'[^'\n]*'|\([^\n)]*\)))?[ \t]*\))",
    re.MULTILINE,
)
_REFERENCE = re.compile(r"(?P<prefix>^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*)(?P<url>" + _URL
                        + r")(?P<tail>)(?=[ \t]*(?:$|[\"'(]))", re.MULTILINE)


def _frontmatter(markdown):
    """Validate duplicate keys as well as the ordinary property representation."""
    return _registration_frontmatter(markdown)[0]


def _outside_code(markdown, transform):
    """Keep fenced and inline code byte-for-byte, including unterminated fences."""
    result, fence = [], None
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            result.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            fence = marker[1]
            result.append(line)
        else:
            # Matching backtick runs are inline code, never live links.
            pieces = re.split(r"(`+)(.*?)(\1)(?!`)", line)
            if len(pieces) == 1:
                result.append(transform(line))
            else:
                result.append("".join(transform(piece) if index % 4 == 0 else piece
                                      for index, piece in enumerate(pieces)))
    return "".join(result)


def _stem(path):
    return path[:-3] if path.lower().endswith(".md") else path


def _resolve_relative(path, owner):
    result = posixpath.normpath(posixpath.join(posixpath.dirname(owner), path))
    if result == ".." or result.startswith("../") or result.startswith("/"):
        raise LibraryError("Local link escapes the Vault; review links before moving")
    return result


def _rewrite(markdown, old_owner, new_owner, redirects, paths):
    """Rewrite resolved destinations only; ambiguous short names fail closed."""
    count = 0

    def wiki(match):
        nonlocal count
        content = match[1]
        destination, separator, alias = content.partition("|")
        # Table cells escape the alias delimiter as \|.  The backslash is
        # Markdown syntax, not part of the note path or heading anchor.
        if separator and destination.endswith("\\"):
            destination = destination[:-1]
            separator = "\\|"
        raw, anchor_separator, anchor = destination.partition("#")
        decoded = unquote(raw)
        if not decoded:
            return match[0]
        if decoded.startswith(("./", "../")):
            try:
                target = _stem(_resolve_relative(decoded, old_owner))
            except LibraryError:
                if old_owner == new_owner:
                    return match[0]
                raise
            candidates = [path for path in paths if _stem(path) == target]
        else:
            target = _stem(decoded)
            # A partial path can resolve by suffix in Obsidian. Do not guess
            # between identical basenames (or identical path suffixes).
            candidates = [path for path in paths if _stem(path) == target or _stem(path).endswith("/" + target)]
        affected = [path for path in candidates if path in redirects]
        if (affected and len(candidates) != 1) or (old_owner != new_owner and len(candidates) > 1):
            raise LibraryError(f"Ambiguous wikilink in {old_owner}: {raw}; use a full Vault path")
        if not affected and not (old_owner != new_owner and decoded.startswith(("./", "../"))):
            return match[0]
        if len(candidates) != 1:
            raise LibraryError(f"Unresolved relative wikilink in {old_owner}: {raw}")
        replacement = redirects.get(candidates[0], candidates[0])
        if not decoded.lower().endswith(".md"):
            replacement = _stem(replacement)
        count += 1
        return "[[" + replacement + (anchor_separator + anchor if anchor_separator else "") + (separator + alias if separator else "") + "]]"

    def markdown_link(match):
        nonlocal count
        original = match["url"]
        angled = original.startswith("<")
        url = original[1:-1] if angled else original
        raw, anchor_separator, anchor = url.partition("#")
        if not raw or raw.startswith(("/", "\\")) or re.match(r"^[a-zA-Z][\w+.-]*:", raw):
            return match[0]
        raw_path, query_separator, query = raw.partition("?")
        decoded = unquote(re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~])", r"\1", raw_path))
        if "\\" in decoded:
            raise LibraryError(f"Unsupported local link in {old_owner}: {raw}")
        try:
            resolved = _resolve_relative(decoded, old_owner)
        except LibraryError:
            if old_owner == new_owner:
                return match[0]
            raise
        target = resolved if resolved in paths else resolved + ".md" if resolved + ".md" in paths else resolved
        if target not in redirects and old_owner == new_owner:
            return match[0]
        desired = redirects.get(target, resolved)
        replacement = posixpath.relpath(desired, posixpath.dirname(new_owner) or ".")
        if target in redirects and not decoded.lower().endswith(".md"):
            replacement = _stem(replacement)
        replacement = quote(replacement, safe="/-._~" if not angled else "/-._~ ")
        replacement += (query_separator + query if query_separator else "") + (anchor_separator + anchor if anchor_separator else "")
        if angled:
            replacement = "<" + replacement + ">"
        count += 1
        return match["prefix"] + replacement + match["tail"]

    def transform(text):
        matches = list(_MARKDOWN.finditer(text)) + list(_REFERENCE.finditer(text))
        for opening in re.finditer(r"(?<!\\)\]\([ \t]*|^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*", text):
            if any(match.start() <= opening.start() < match.end() for match in matches):
                continue
            fragment = unquote(text[opening.end():]).replace("\\", "")
            if fragment.startswith(("#", "/")) or re.match(r"^[a-zA-Z][\w+.-]*:", fragment):
                continue
            if old_owner != new_owner or any(_stem(PurePosixPath(path).name) in fragment for path in redirects):
                raise LibraryError(f"Unsupported local Markdown link in {old_owner}; review links before organizing")
        return _REFERENCE.sub(markdown_link, _MARKDOWN.sub(markdown_link, _WIKI.sub(wiki, text)))

    rewritten = _outside_code(markdown, transform)
    return rewritten, count


class LibraryActions:
    def __init__(self, library: Library):
        self.library = library

    def _snapshot(self):
        """Observe files without creating checkpoints during a preview."""
        from .bootstrap import _index
        state, notes = _index(self.library)
        if any(state['notes'].get(note_id, {}).get('deleted') for note_id in notes):
            raise LibraryError('Deleted identity reappeared outside recover')
        parsed = {}
        for directory, names, filenames in os.walk(self.library.vault, followlinks=False):
            names[:] = [name for name in names if not name.startswith('.')]
            for name in filenames:
                if name.startswith('.') or not name.lower().endswith('.md'):
                    continue
                relative = (Path(directory) / name).relative_to(self.library.vault).as_posix()
                markdown = _read(self.library._path(relative))
                parsed[relative] = (_hash(markdown), _split(markdown)[0])
        self.library._parsed_files = parsed
        return state, notes, self._files(notes)

    def _token(self, request, state, files):
        return _hash(json.dumps({'version': 1, 'vault': str(self.library.vault.resolve()),
            'request': request, 'files': {p: f['sha256'] for p, f in files.items()},
            'sources': state['sources'], 'publications': state['publications']},
            ensure_ascii=False, sort_keys=True))

    def _retain(self, state, notes, updated, source_ids):
        for note_id in set(updated) | set(source_ids):
            self.library._remember(state, notes[note_id], 'observed')
        for note in updated.values():
            self.library._remember(state, note, 'mcp-organized')

    def _readback(self, updated):
        try:
            from .bootstrap import _index
            actual = _index(self.library)[1] if any(n.get('catalog_backed') for n in updated.values()) else {}
            return all((actual.get(n['id'], {}).get('sha256') if n.get('catalog_backed') else
                        _hash(_read(self.library._path(n['path'])))) == n['sha256'] for n in updated.values())
        except (OSError, LibraryError):
            return False

    def _destination(self, destination):
        if not isinstance(destination, str) or not destination or "\\" in destination:
            raise LibraryError("Move requires a safe relative Markdown destination")
        path = PurePosixPath(destination)
        if (path.is_absolute() or len(path.parts) < 2 or path.parts[0] not in _ROOTS
                or any(part.startswith(".") for part in path.parts)
                or any(part.casefold() in _CONFIG_FOLDERS for part in path.parts)
                or not destination.endswith(".md") or path.stem.casefold() in _RESERVED_NAMES
                or any(char in destination for char in "[]|#?%\r\n\x00")):
            raise LibraryError("Move destination must be a visible .md in an allowed note folder, excluding navigation/configuration")
        self.library._path(destination)
        if self.library._path(destination).exists():
            raise LibraryError("Move destination is occupied")
        for parent in self.library._path(destination).parents:
            if parent == self.library.vault:
                break
            if parent.exists() and not parent.is_dir():
                raise LibraryError("Move destination parent is occupied by a file")
        return path.as_posix()

    def _files(self, notes):
        files = {}
        for relative, (digest, _) in self.library._parsed_files.items():
            markdown = _read(self.library._path(relative))
            self.library._expect({"sha256": _hash(markdown)}, digest)
            files[relative] = {"markdown": markdown, "sha256": digest}
        for note in notes.values():
            if note.get("catalog_backed"):
                self.library._assert_note_storage(note)
                if note["path"] not in files:
                    files[note["path"]] = {"markdown": note["markdown"], "sha256": note["sha256"], "virtual": True}
            else:
                self.library._expect(files[note["path"]], note["sha256"])
        return files

    def _changes(self, state, notes, files, redirects, *, replacement=None, removed=(), remember=True):
        writes, links_updated, updated = [], 0, {}
        by_path = {note["path"]: note for note in notes.values()}
        for path, file in files.items():
            if path in removed:
                continue
            destination = redirects.get(path, path)
            note = by_path.get(path)
            original_markdown = note["markdown"] if note and note.get("catalog_backed") else file["markdown"]
            markdown = replacement.get(path, original_markdown) if replacement else original_markdown
            markdown, count = _rewrite(markdown, path, destination, redirects, set(files))
            links_updated += count
            if markdown == original_markdown and destination == path and not (replacement and path in replacement):
                continue
            properties = _frontmatter(markdown)
            if note:
                self.library._validate_properties(properties)
                if any(properties[key] != note["properties"][key] for key in ("id", "kind")):
                    raise LibraryError("Link repair must preserve document identity")
                updated[note["id"]] = {**note, "path": destination, "markdown": markdown,
                                       "sha256": _hash(markdown), "properties": properties}
            expected = note["sha256"] if note and note.get("catalog_backed") else file["sha256"]
            writes.append((destination, markdown, None if destination != path else expected))
        # Guard the source of a move and every rewritten navigation/content file
        # before retaining history or creating a transaction journal.
        affected = {path for path, _, _ in writes} | set(redirects) | set(removed)
        for path in affected & files.keys():
            if files[path].get("virtual"):
                self.library._assert_note_storage(by_path[path])
            else:
                self.library._expect({"sha256": _hash(_read(self.library._path(path)))}, files[path]["sha256"])
        if remember:
            for note in updated.values():
                self.library._remember(state, note, "mcp-organized")
        return writes, updated, links_updated

    def move(self, note_id, destination, expected_hash) -> dict:
        result = self.move_many([{'note_id': note_id, 'destination': destination,
                                 'expected_hash': expected_hash}])
        return result['moves'][0] | {key: result[key] for key in
            ('links_updated', 'publication_selected', 'readback_verified', 'status')}

    def move_many(self, moves, *, apply=True, expected_token=None):
        if not isinstance(moves, list) or not 1 <= len(moves) <= 100:
            raise LibraryError('Move batch requires 1..100 explicit notes')
        library = self.library
        with library._lock():
            state, notes, files = self._snapshot()
            redirects, ids, destinations = {}, set(), set()
            selected = []
            for row in moves:
                if (not isinstance(row, dict) or set(row) != {'note_id', 'destination', 'expected_hash'}
                        or any(not isinstance(value, str) for value in row.values())):
                    raise LibraryError('Each move requires note_id, destination and expected_hash only')
                note = library._required(notes, row['note_id'])
                library._expect(note, row['expected_hash'])
                if note['properties']['kind'] != 'note':
                    raise LibraryError('Only notes can be moved')
                if (PurePosixPath(note['path']).stem.casefold() in _RESERVED_NAMES
                        or any(p.casefold() in _CONFIG_FOLDERS for p in PurePosixPath(note['path']).parts)):
                    raise LibraryError('Navigation/configuration files cannot be moved')
                destination = self._destination(row['destination'])
                if note['id'] in ids or destination in destinations:
                    raise LibraryError('Duplicate move identity or destination')
                ids.add(note['id']); destinations.add(destination)
                redirects[note['path']] = destination
                selected.append(note)
            if any(str(parent) in destinations for destination in destinations
                   for parent in PurePosixPath(destination).parents):
                raise LibraryError('A batch destination cannot be another destination parent')
            writes, updated, links = self._changes(state, notes, files, redirects, remember=False)
            token = self._token({'operation': 'move', 'moves': moves}, state, files)
            result = {'applied': apply, 'token': token, 'moves': [
                {'id': n['id'], 'previous_path': n['path'], 'path': updated[n['id']]['path'],
                 'sha256': updated[n['id']]['sha256'], 'content_unchanged': n['markdown'] == updated[n['id']]['markdown']}
                for n in selected], 'links_updated': links, 'publication_selected': False}
            if not apply:
                return result | {'status': 'preview'}
            if expected_token is not None and expected_token != token:
                raise LibraryError('Organization preview changed; preview again before applying')
            self._retain(state, notes, updated, ids)
            library._commit(state, writes, [(n['path'], n['sha256']) for n in selected])
            verified = self._readback(updated) and all(not library._path(n['path']).exists() for n in selected)
            return result | {'status': 'completed' if verified else 'needs_review', 'readback_verified': verified}

    def _unmapped_sources(self, state, source_ids):
        forbidden = set(state["publications"])
        for key, row in state["sources"].items():
            if not isinstance(row, dict):
                raise LibraryError("Cannot verify library source mappings")
            source = row.get("source", {})
            if key.startswith("payload-bootstrap:") or (isinstance(source, dict) and source.get("collection") in {"works", "posts"}):
                forbidden.add(row.get("id"))
        for name in ("sync-state.json", "bootstrap-state.json"):
            path = self.library._path(".markdown-library/" + name)
            if not path.exists():
                continue
            try:
                data = json.loads(_read(path))
                if name == "sync-state.json":
                    if not isinstance(data.get("baselines"), dict) or not isinstance(data.get("pending", {}), dict):
                        raise ValueError("Invalid sync mapping")
                    forbidden.update(data["baselines"])
                    forbidden.update(data.get("pending", {}))
                else:
                    if not isinstance(data.get("imports"), dict):
                        raise ValueError("Invalid bootstrap mapping")
                    forbidden.update(row["id"] for row in data["imports"].values())
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise LibraryError("Cannot verify website source mappings") from exc
        if forbidden.intersection(source_ids):
            raise LibraryError("Cannot merge a published or website-mapped source")

    def merge(self, target_id, source_ids: list[str], markdown: str, expected_hashes: dict[str, str], *, allow_work_target=False,
              apply=True, expected_token=None) -> dict:
        if (not isinstance(source_ids, list) or not source_ids or not all(isinstance(item, str) for item in source_ids)
                or len(source_ids) != len(set(source_ids)) or target_id in source_ids):
            raise LibraryError("Merge requires distinct source IDs excluding the target")
        if not isinstance(expected_hashes, dict):
            raise LibraryError("Merge requires expected SHA256 for target and every source")
        library = self.library
        with library._lock():
            state, notes, files = self._snapshot()
            target = library._required(notes, target_id)
            sources = [library._required(notes, note_id) for note_id in source_ids]
            for note in [target, *sources]:
                library._expect(note, expected_hashes.get(note["id"]))
            target_kinds = {'note', 'post', 'work'} if allow_work_target else {'note', 'post'}
            if target["properties"]["kind"] not in target_kinds or any(note["properties"]["kind"] != "note" for note in sources):
                raise LibraryError("Merge requires note sources and a note or post target")
            if json.dumps(_frontmatter(markdown), sort_keys=True) != json.dumps(target["properties"], sort_keys=True):
                raise LibraryError("Merged Markdown must preserve all target properties, including id and kind")
            self._unmapped_sources(state, source_ids)
            redirects = {note["path"]: target["path"] for note in sources}
            writes, updated, links = self._changes(state, notes, files, redirects,
                                                   replacement={target["path"]: markdown}, removed=set(redirects), remember=False)
            current = updated.get(target_id, target)
            if json.dumps(current["properties"], sort_keys=True) != json.dumps(target["properties"], sort_keys=True):
                raise LibraryError("Link redirection would change target properties; review the merge")
            token = self._token({'operation': 'merge', 'target_id': target_id, 'source_ids': source_ids,
                                 'markdown': markdown, 'expected_hashes': expected_hashes}, state, files)
            if not apply:
                return {'applied': False, 'status': 'preview', 'token': token, 'target_id': target_id,
                        'path': target['path'], 'source_ids': source_ids, 'sha256': current['sha256'],
                        'links_updated': links, 'publication_selected': False}
            if expected_token is not None and token != expected_token:
                raise LibraryError('Organization preview changed; preview again before applying')
            self._retain(state, notes, updated, source_ids)
            moment = _now()
            provenance = []
            for source in sources:
                trash = f".markdown-library/trash/{source['id']}.md"
                previous = library._path(trash)
                digest = _hash(_read(previous)) if previous.exists() else None
                writes.append((trash, source["markdown"], digest))
                record = state["notes"][source["id"]]
                record.update({"deleted": True, "trash": trash, "deleted_at": moment, "merged_into": target_id,
                               "lifecycle": {"intent": "delete", "token": str(uuid4()), "origin": "local",
                                             "sha256": source["sha256"], "requested_at": moment,
                                             "event": {"operation": "merge", "target_id": target_id}}})
                provenance.append({"id": source["id"], "path": source["path"], "sha256": source["sha256"]})
            # import_note resolves this index before considering new content.
            # Keep an imported source idempotent after its original note enters
            # Trash: direct it to the surviving draft, retaining all provenance
            # and every intermediate redirect for chained merges.
            by_id = {source["id"]: source for source in sources}
            for row in state["sources"].values():
                source = by_id.get(row.get("id"))
                if source is None:
                    continue
                if not isinstance(row.get("source"), dict) or not isinstance(row.get("merge_redirects", []), list):
                    raise LibraryError("Cannot safely redirect this import source mapping")
                row.setdefault("original_library_id", source["id"])
                row.setdefault("merge_redirects", []).append({
                    "from_id": source["id"], "to_id": target_id, "at": moment,
                    "path": source["path"], "sha256": source["sha256"],
                })
                row["id"] = target_id
            state["notes"][target_id].setdefault("merges", []).append({"at": moment, "sources": provenance,
                                                                        "sha256": current["sha256"]})
            library._commit(state, writes, [(note["path"], note["sha256"]) for note in sources])
            try:
                recoverable = all(not library._path(n['path']).exists() and
                    _hash(_read(library._path(f".markdown-library/trash/{n['id']}.md"))) == n['sha256']
                    for n in sources)
            except (LibraryError, OSError):
                recoverable = False
            verified = self._readback(updated) and recoverable
            return {"id": target_id, "target_id": target_id, "path": target["path"], "sha256": current["sha256"],
                    "source_ids": list(source_ids), "sources": copy.deepcopy(provenance),
                    "links_updated": links, "publication_selected": False, 'applied': True, 'token': token,
                    'readback_verified': verified, 'sources_recoverable': recoverable,
                    'status': 'completed' if verified else 'needs_review'}
