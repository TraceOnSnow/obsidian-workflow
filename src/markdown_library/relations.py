"""Native Obsidian relationship navigation with stable IDs at the API boundary.

Work.article is the editable relationship. It accepts the original library ID or
a quoted wikilink; publication snapshots always retain the stable Post ID.
Post.related_works is a derived, private navigation index, never a website field.
"""

from copy import deepcopy
from pathlib import PurePosixPath
import re
from urllib.parse import unquote

import yaml

from .core import LibraryError, _hash, _read, _split


def _notes(library, notes):
    if notes is not None:
        return notes
    from .bootstrap import _index
    return _index(library)[1]


def _wiki_target(value):
    match = re.fullmatch(r"\[\[([^\r\n]+)\]\]", value.strip())
    if not match:
        raise LibraryError("Article must be a Post ID or one quoted Obsidian wikilink")
    target = unquote(match.group(1).split("|", 1)[0].split("#", 1)[0]).strip()
    path = PurePosixPath(target)
    if not target or path.is_absolute() or ".." in path.parts or "\\" in target:
        raise LibraryError("Article wikilink must point to a visible note inside the Vault")
    if any(part.startswith(".") for part in path.parts):
        raise LibraryError("Article wikilink cannot point into private metadata")
    return target.removesuffix(".md")


def _retained_posts(library):
    """A verified tombstone preserves relationship identity while in Trash."""
    result = {}
    for note_id, record in library._read_state()["notes"].items():
        if not record.get("deleted") or record.get("kind") != "post" or not record.get("trash"):
            continue
        target = library._path(record["trash"])
        if not target.is_file():
            continue
        markdown = _read(target)
        properties, _ = _split(markdown)
        if (_hash(markdown) == record.get("sha256") and properties.get("id") == note_id
                and properties.get("kind") == "post"):
            result[note_id] = {"id": note_id, "path": record["path"], "properties": properties}
    return result


def canonical_article(library, value, notes=None):
    """Resolve an editable link to identity; never guess by a displayed title."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise LibraryError("Article requires one Post relationship")
    indexed = _notes(library, notes)
    if str(value) in indexed:
        note = indexed[str(value)]
        if note["properties"]["kind"] != "post":
            raise LibraryError("Article relationship must point to a Post")
        return note["id"]
    retained = _retained_posts(library)
    if str(value) in retained:
        return str(value)
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        # Preserve legacy standard-API numeric relationships. They are displayed
        # as links only when an already-observed local mapping proves identity.
        return value
    if not value.strip().startswith("[["):
        raise LibraryError("Linked Post is missing; restore it or choose another Post")
    target = _wiki_target(value)
    exact = [note for note in indexed.values()
             if note["path"].removesuffix(".md") == target]
    candidates = exact or ([note for note in indexed.values()
                            if PurePosixPath(note["path"]).stem == target]
                           if "/" not in target else [])
    if not candidates:
        candidates = [note for note in retained.values() if note["path"].removesuffix(".md") == target]
        if not candidates and "/" not in target:
            candidates = [note for note in retained.values() if PurePosixPath(note["path"]).stem == target]
    if len(candidates) != 1:
        raise LibraryError("Article wikilink is missing or ambiguous; use its full Vault path")
    if candidates[0]["properties"]["kind"] != "post":
        raise LibraryError("Article relationship must point to a Post")
    return candidates[0]["id"]


def wikilink(note):
    target = ((note.get("storage_path") or note["catalog_path"]) if note.get("catalog_backed") else note["path"]).removesuffix(".md")
    if any(char in target for char in "[]|\r\n"):
        raise LibraryError("Relationship path cannot be represented as an Obsidian wikilink")
    title = re.sub(r"[\[\]|\r\n]", " ", note["properties"]["title"]).strip()
    return f"[[{target}|{title}]]"


def display_article(library, value, notes=None):
    indexed = _notes(library, notes)
    canonical = canonical_article(library, value, notes=indexed)
    if canonical is None:
        return None
    note = indexed.get(str(canonical)) or _retained_posts(library).get(str(canonical))
    if note is None and (isinstance(canonical, int) or str(canonical).isdigit()):
        import json
        filename = library._path(".markdown-library/sync-state.json")
        if filename.is_file():
            state = json.loads(filename.read_text(encoding="utf-8"))
            matches = [key for key, row in state.get("baselines", {}).items()
                       if row.get("collection") == "posts"
                       and str(row.get("remote_id")) == str(canonical)]
            if len(matches) == 1:
                note = indexed.get(matches[0])
    return wikilink(note) if note else value


def linkify_properties(library, properties, notes=None):
    result = deepcopy(properties)
    if result.get("article") is not None:
        result["article"] = display_article(library, result["article"], notes=notes)
    return result


def patch_properties(markdown, updates):
    """Change just named YAML entries; preserve the complete body byte for byte."""
    prefix = "\ufeff" if markdown.startswith("\ufeff") else ""
    text = markdown[len(prefix):]
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise LibraryError("Managed relationship editing requires existing frontmatter")
    end = next((i for i, line in enumerate(lines[1:], 1)
                if line.strip() in {"---", "..."}), None)
    if end is None:
        raise LibraryError("Unclosed YAML frontmatter")
    source = "".join(lines[1:end])
    node = yaml.compose(source)
    if not isinstance(node, yaml.MappingNode):
        raise LibraryError("Relationship frontmatter must be a mapping")
    keys = [key.value for key, _ in node.value]
    if len(keys) != len(set(keys)):
        raise LibraryError("Duplicate properties require review before relationship editing")
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    edits = []
    remaining = dict(updates)
    for key, value in node.value:
        if key.value not in remaining:
            continue
        update = remaining.pop(key.value)
        replacement = yaml.safe_dump({key.value: update}, allow_unicode=True, sort_keys=False)
        replacement = replacement.replace("\n", newline)
        start, stop = key.start_mark.index, value.end_mark.index
        # Scalar end marks precede their line ending. Include that ending only;
        # trailing comments elsewhere in the frontmatter remain untouched.
        if source[stop:stop + 2] == "\r\n":
            stop += 2
        elif source[stop:stop + 1] == "\n":
            stop += 1
        elif source[stop:stop + 1] == " ":
            # Keep same-line comments by moving them onto their own line.
            tail = source[stop:source.find("\n", stop) if "\n" in source[stop:] else len(source)]
            if tail.lstrip().startswith("#"):
                replacement += tail.lstrip().rstrip("\r") + newline
                stop += len(tail)
                if source[stop:stop + 2] == "\r\n":
                    stop += 2
                elif source[stop:stop + 1] == "\n":
                    stop += 1
        edits.append((start, stop, replacement))
    for start, stop, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[stop:]
    if remaining:
        if source and not source.endswith(("\n", "\r")):
            source += newline
        source += yaml.safe_dump(remaining, allow_unicode=True, sort_keys=False).replace("\n", newline)
    result = prefix + lines[0] + source + "".join(lines[end:])
    if _split(result)[1] != _split(markdown)[1]:
        raise LibraryError("Relationship update unexpectedly changed the note body")
    return result


def refresh_relations(library, *, apply=False):
    """Preview or refresh relationship links without changing publication intent."""
    from .bootstrap import _index

    def prepare(state, indexed):
        reverse = {}
        changes, issues = [], []
        for note in indexed.values():
            if note["properties"]["kind"] != "work":
                continue
            from .work_profile import website_properties, profile
            properties = website_properties(library, note, state)
            if properties.get('article') is None:
                continue
            try:
                canonical = canonical_article(library, properties["article"], notes=indexed)
                display = display_article(library, properties["article"], notes=indexed)
                if str(canonical) not in indexed and isinstance(display, str) and display.startswith("[["):
                    canonical = canonical_article(library, display, notes=indexed)
                if canonical is not None and str(canonical) in indexed:
                    reverse.setdefault(str(canonical), []).append(note)
                if display != properties["article"] and profile(library, note['id'], state) is None:
                    changes.append((note, {"article": display}))
            except LibraryError as exc:
                issues.append({"id": note["id"], "path": note["path"], "reason": str(exc)})
        for note in indexed.values():
            if note["properties"]["kind"] != "post":
                continue
            related = sorted(reverse.get(note["id"], []), key=lambda row: row["path"])
            desired = [wikilink(work) for work in related]
            if (desired or "related_works" in note["properties"]) and note["properties"].get("related_works") != desired:
                changes.append((note, {"related_works": desired}))
        operations, updated = [], []
        for note, updates in changes:
            markdown = patch_properties(note["markdown"], updates)
            properties, _ = _split(markdown)
            replacement = {**note, "markdown": markdown, "properties": properties, "sha256": _hash(markdown)}
            operations.append((note["path"], markdown, note["sha256"]))
            updated.append(replacement)
        return operations, updated, issues

    library.validate()
    if apply:
        with library._lock():
            state = library._load()
            indexed = library._scan(state)
            operations, updated, issues = prepare(state, indexed)
            # A broken relationship must not erase otherwise useful reverse
            # navigation. Fix its identity before refreshing the entire index.
            if issues:
                return {"applied": False, "changed": [], "issues": issues}
            for note in updated:
                library._remember(state, note, "relationship-navigation")
            if operations:
                library._commit(state, operations)
    else:
        state, indexed = _index(library)
        operations, updated, issues = prepare(state, indexed)
    from .work_profile import website_properties
    return {"applied": bool(apply), "changed": [{"id": note["id"], "path": note["path"]} for note in updated],
            "issues": issues, "relationships": sum(1 for note in indexed.values()
                if note["properties"]["kind"] == "work" and website_properties(library, note, state).get("article") is not None)}
