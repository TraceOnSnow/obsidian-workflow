"""Ordinary Markdown as working files, with private local history and publications.

The lock serializes cooperating library writers. Editors that ignore it can still
race a write, so callers should reread before editing and always pass hashes.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

import yaml


class LibraryError(Exception):
    """A visible validation, conflict, or library integrity error."""


KINDS = {"note", "work", "post"}
PUBLIC_WORK_FIELDS = {
    "slug", "type", "rating", "status", "tags", "note", "article", "experiences",
    "aliases", "cover", "coverUrl", "releaseDate", "creators", "platforms", "description", "sources",
}
PUBLIC_POST_FIELDS = {"link", "locale", "publishedAt", "description", "tags", "cover", "categories", "frontmatter"}
PUBLIC_THEME_FIELDS = {
    "subtitle", "catalog", "sticky", "tocNumbering", "excludeFromSummary", "math",
    "quiz", "keywords", "colophon", "works", "updated",
}
PUBLIC_ROWS = {
    "experiences": {"id", "startedOn", "endedOn", "status", "platform", "progress", "note"},
    "sources": {"id", "provider", "externalId", "url", "fetchedAt"},
}


def _read(path):
    # Path.read_text uses universal newlines, which would misidentify Windows edits.
    return path.read_bytes().decode("utf-8")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _purge_proof(value):
    """Validate content-free evidence supplied by the explicit remote executor.

    This local API never makes network requests or invents remote observations.
    The caller retains the actual deletion receipt identified by its digest.
    """
    common = {"target", "collection", "remote_id", "previous_remote_version", "observed_at",
              "latest_status", "formal_status", "deletion_transport", "deletion_receipt_sha256"}
    if not isinstance(value, dict):
        raise LibraryError("Permanent deletion requires remote hard-delete proof")
    local = value.get("deletion_transport") == "payload-local-api"
    extra = {"deletion_result"} if local else {"hard_delete_status"}
    if set(value) != common | extra:
        raise LibraryError("Invalid remote hard-delete proof fields")
    if local:
        valid_delete = value["deletion_result"] == "deleted"
    else:
        valid_delete = (value["deletion_transport"] == "http"
                        and type(value["hard_delete_status"]) is int
                        and value["hard_delete_status"] in {200, 204})
    if (not valid_delete or type(value["latest_status"]) is not int or value["latest_status"] != 404
            or type(value["formal_status"]) is not int or value["formal_status"] != 404
            or not isinstance(value["collection"], str) or value["collection"] not in {"works", "posts"}
            or isinstance(value["remote_id"], bool) or not isinstance(value["remote_id"], (str, int))
            or not str(value["remote_id"]).strip()
            or not isinstance(value["previous_remote_version"], str) or not value["previous_remote_version"]
            or not isinstance(value["deletion_receipt_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["deletion_receipt_sha256"])):
        raise LibraryError("Invalid remote hard-delete evidence")
    try:
        moment = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError()
        from .bootstrap import _target
        if _target(value["target"]) != value["target"]:
            raise ValueError()
    except (AttributeError, TypeError, ValueError, LibraryError) as exc:
        raise LibraryError("Invalid remote hard-delete target or timestamp") from exc
    return {**copy.deepcopy(value), "remote_id": str(value["remote_id"])}


def _plain(value, depth=0):
    """Convert YAML dates to strings and reject unsupported property values."""
    if depth > 100:
        raise LibraryError("Property nesting is too deep or recursive")
    if isinstance(value, float) and not math.isfinite(value):
        raise LibraryError("Non-finite property numbers are not supported")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _plain(item, depth + 1) for key, item in value.items()}
    raise LibraryError("Properties must contain JSON-compatible values")


def _split(markdown):
    if not isinstance(markdown, str):
        raise LibraryError("Markdown must be text")
    text = markdown.removeprefix("\ufeff")
    if text.startswith("---\n") or text.startswith("---\r\n"):
        lines = text.splitlines(keepends=True)
        end = next((i for i, line in enumerate(lines[1:], 1)
                    if line.strip() in {"---", "..."}), None)
        if end is None:
            raise LibraryError("Unclosed YAML frontmatter")
        try:
            properties = yaml.safe_load("".join(lines[1:end])) or {}
        except yaml.YAMLError as exc:
            raise LibraryError(f"Invalid YAML frontmatter: {exc}") from exc
        if not isinstance(properties, dict):
            raise LibraryError("Frontmatter must be a mapping")
        return _plain(properties), "".join(lines[end + 1:])
    return {}, markdown


def _render(properties, body):
    return "---\n" + yaml.safe_dump(properties, allow_unicode=True, sort_keys=False) + "---\n" + body


def _registration_frontmatter(markdown):
    """Inspect YAML without normalizing the user's original text."""
    text = markdown.removeprefix("\ufeff")
    if not text.startswith(("---\n", "---\r\n")):
        return {}, None
    lines = text.splitlines(keepends=True)
    end = next((i for i, line in enumerate(lines[1:], 1)
                if line.strip() in {"---", "..."}), None)
    if end is None:
        raise LibraryError("Unclosed YAML frontmatter")
    source = "".join(lines[1:end])
    try:
        node = yaml.compose(source)
        if node is not None and isinstance(node, yaml.MappingNode):
            keys = [key.value for key, _ in node.value if isinstance(key, yaml.ScalarNode)]
            if len(keys) != len(set(keys)):
                raise LibraryError("Duplicate YAML properties must be resolved before registration")
        properties, _ = _split(markdown)
    except yaml.YAMLError as exc:
        raise LibraryError(f"Invalid YAML frontmatter: {exc}") from exc
    return properties, (lines, end)


class Library:
    def __init__(self, vault):
        root = Path(vault).expanduser()
        if not root.is_absolute() or root == Path("/"):
            raise LibraryError("An explicit absolute Vault directory is required")
        if ".." in root.parts:
            raise LibraryError("Vault path must not contain '..'")
        self.vault = root
        self._check_components(root)
        self.private = root / ".markdown-library"
        self._parsed_files = {}

    @staticmethod
    def _check_components(path):
        for part in [path, *path.parents]:
            if part.is_symlink():
                raise LibraryError(f"Symlinks are not allowed: {part}")

    def _path(self, relative):
        rel = Path(relative)
        if rel.is_absolute() or not rel.parts or ".." in rel.parts:
            raise LibraryError("Expected a safe relative path inside the Vault")
        result = self.vault / rel
        self._check_components(result)
        return result

    @classmethod
    def initialize(cls, vault):
        library = cls(vault)
        library.vault.mkdir(parents=True, exist_ok=True)
        library._check_components(library.vault)
        library.private.mkdir(mode=0o700, exist_ok=True)
        with library._lock(initializing=True):
            marker = library._path(".markdown-library/marker.json")
            if marker.exists():
                library._validate_marker()
            else:
                if (library.private / "state.json").exists():
                    raise LibraryError("State exists without marker; refusing initialization")
                library._atomic(".markdown-library/state.json", json.dumps(library._empty_state()))
                library._atomic(".markdown-library/marker.json", json.dumps({"schema_version": 1, "library_id": str(uuid4())}))
        return library

    @staticmethod
    def _empty_state():
        return {"schema_version": 1, "rating_scale": 100, "notes": {}, "sources": {}, "publications": {}}

    def _validate_marker(self):
        marker = self._path(".markdown-library/marker.json")
        if not marker.is_file():
            raise LibraryError("Vault is not initialized; run init explicitly")
        try:
            data = json.loads(_read(marker))
            if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("library_id"), str):
                raise ValueError("Unsupported marker")
        except (ValueError, TypeError) as exc:
            raise LibraryError("Invalid library marker") from exc

    def validate(self):
        """Check initialization without creating a lock or modifying the Vault."""
        self._check_components(self.private)
        self._validate_marker()
        state = self._path(".markdown-library/state.json")
        if not state.is_file():
            raise LibraryError("Library state is missing")
        self._read_state()
        return self

    @contextmanager
    def _lock(self, initializing=False):
        self._check_components(self.private)
        if not initializing:
            self._validate_marker()
        lockpath = self._path(".markdown-library/lock")
        try:
            fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield
        except OSError as exc:
            raise LibraryError(f"Cannot lock library: {exc}") from exc

    def _atomic(self, relative, text):
        destination = self._path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._check_components(destination.parent)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".library-", dir=destination.parent)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            self._check_components(destination)
            os.replace(temporary, destination)
            temporary = None
            directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary:
                os.unlink(temporary)

    def _load(self):
        try:
            journal = self._path(".markdown-library/transaction.json")
            if journal.exists():
                self._replay(json.loads(_read(journal)))
            state = self._read_state()
            if state.get("rating_scale") != 100:
                raise LibraryError("Legacy rating scale requires explicit migration before library operations")
            return state
        except (ValueError, OSError) as exc:
            raise LibraryError(f"Cannot read private state: {exc}") from exc

    def _read_state(self):
        try:
            state = json.loads(_read(self._path(".markdown-library/state.json")))
            if not isinstance(state, dict) or state.get("schema_version") != 1 or any(not isinstance(state.get(key), dict) for key in ("notes", "sources", "publications")):
                raise ValueError("Unsupported state")
            return state
        except (ValueError, OSError) as exc:
            raise LibraryError(f"Cannot read private state: {exc}") from exc

    def _save(self, state):
        self._atomic(".markdown-library/state.json", json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False))

    def _commit(self, state, writes=None, removes=None, *, storage_resolved=False):
        """Journal a local mutation before touching working files.

        Interrupted transactions replay under the same lock. If an external editor
        changed a file since the transaction began, recovery fails visibly.
        """
        from .work_catalog import enabled, expand_operations
        if enabled(state) and not storage_resolved:
            writes, removes = expand_operations(self, state, writes or [], removes or [])
        operations = []
        for relative, content, expected in writes or []:
            operations.append({"path": relative, "markdown": content, "expected": expected})
        for relative, expected in removes or []:
            operations.append({"path": relative, "markdown": None, "expected": expected})
        transaction = {"state": state, "operations": operations}
        serialized = json.dumps(transaction, ensure_ascii=False, allow_nan=False)
        self._atomic(".markdown-library/transaction.json", serialized)
        self._replay(transaction)

    def _replay(self, transaction):
        for operation in transaction["operations"]:
            destination = self._path(operation["path"])
            actual = _hash(_read(destination)) if destination.exists() else None
            desired = _hash(operation["markdown"]) if operation["markdown"] is not None else None
            if actual not in {operation["expected"], desired}:
                raise LibraryError(f"Interrupted transaction conflicts with an external edit: {operation['path']}; preserve both versions and review .markdown-library/transaction.json")
        for operation in transaction["operations"]:
            destination = self._path(operation["path"])
            if operation["markdown"] is None:
                destination.unlink(missing_ok=True)
            else:
                self._atomic(operation["path"], operation["markdown"])
        self._save(transaction["state"])
        self._path(".markdown-library/transaction.json").unlink()

    def _remember(self, state, note, action):
        record = state["notes"].setdefault(note["id"], {"history": []})
        digest = note["sha256"]
        object_path = f".markdown-library/objects/{digest}.md"
        if not self._path(object_path).exists():
            self._atomic(object_path, note["markdown"])
        if not record["history"] or record["history"][-1]["sha256"] != digest:
            record["history"].append({"sha256": digest, "observed_at": _now(), "action": action, "rating_scale": state.get("rating_scale", 10)})
        record.update({"path": note["path"], "sha256": digest, "kind": note["properties"]["kind"], "deleted": False})

    def _scan(self, state):
        from .work_catalog import enabled, scan_overlay
        catalog_enabled = enabled(state)
        found = {}
        parsed_files = {}
        for directory, names, filenames in os.walk(self.vault, followlinks=False):
            for name in list(names):
                candidate = Path(directory) / name
                if name.startswith("."):
                    names.remove(name)
                elif candidate.is_symlink():
                    raise LibraryError(f"Symlink directory in Vault: {candidate}")
            for name in filenames:
                if not name.lower().endswith(".md") or name.startswith("."):
                    continue
                path = Path(directory) / name
                relative = path.relative_to(self.vault).as_posix()
                path = self._path(relative)
                try:
                    markdown = _read(path)
                except (OSError, UnicodeError) as exc:
                    raise LibraryError(f"Cannot read {relative}: {exc}") from exc
                digest = _hash(markdown)
                cached = self._parsed_files.get(relative)
                if cached is not None and cached[0] == digest:
                    properties = copy.deepcopy(cached[1])
                else:
                    properties, _ = _split(markdown)
                parsed_files[relative] = (digest, copy.deepcopy(properties))
                note_id = properties.get("id")
                if note_id is None:
                    continue
                # Obsidian plugins also use `id`; unrelated files remain unmanaged.
                if not isinstance(note_id, str) or (note_id not in state["notes"] and
                        not (properties.get("schema_version") == 1 and "kind" in properties)):
                    continue
                self._validate_properties(properties)
                if note_id in found:
                    raise LibraryError(f"Duplicate stable id {note_id}: {found[note_id]['path']} and {relative}")
                if state["notes"].get(note_id, {}).get("deleted"):
                    raise LibraryError(f"Deleted id reappeared outside recover: {note_id}")
                note = {"id": note_id, "path": relative, "sha256": digest, "markdown": markdown, "properties": properties}
                found[note_id] = note
                if not (catalog_enabled and properties.get("kind") == "work"):
                    self._remember(state, note, "observed")
        self._parsed_files = parsed_files
        if catalog_enabled:
            found = scan_overlay(self, state, found, apply_defaults=True)
            for note in found.values():
                if note.get("catalog_backed"):
                    self._remember(state, note, "observed")
                    from .work_profile import profile, set_profile
                    if profile(self, note["id"], state) is None:
                        set_profile(state, note["id"], {"slug": "work-" + note["id"], "generateSlug": False})
        return found

    def _assert_note_storage(self, note):
        """Check the physical storage captured by a logical-note read."""
        if note.get("catalog_backed"):
            from .work_catalog import assert_storage
            assert_storage(self, note)
        else:
            self._expect({"sha256": _hash(_read(self._path(note["path"])))}, note["sha256"])

    @staticmethod
    def _validate_properties(properties):
        if not isinstance(properties.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", properties["id"]):
            raise LibraryError("Stable id must be a safe nonempty string")
        if not isinstance(properties.get("kind"), str) or properties["kind"] not in KINDS:
            raise LibraryError("kind must be note, work, or post")
        if properties.get("schema_version") != 1:
            raise LibraryError("schema_version must be 1")
        if not isinstance(properties.get("title"), str) or not properties["title"].strip():
            raise LibraryError("A nonempty title is required")
        if properties.get("kind") == "work" and "rating" in properties:
            from .ratings import validate_rating
            validate_rating(properties["rating"])

    @staticmethod
    def _required(notes, note_id):
        if note_id not in notes:
            raise LibraryError(f"Note is missing or deleted: {note_id}")
        return notes[note_id]

    @staticmethod
    def _expect(note, expected_hash):
        if not expected_hash or note["sha256"] != expected_hash:
            raise LibraryError("Revision conflict: expected SHA256 does not match current file")

    def get(self, note_id):
        with self._lock():
            state = self._load()
            note = self._required(self._scan(state), note_id)
            self._save(state)
            return copy.deepcopy(note)

    def list_notes(self, query=""):
        with self._lock():
            state = self._load()
            notes = self._scan(state)
            self._save(state)
            return [copy.deepcopy(note) for note in sorted(notes.values(), key=lambda n: n["path"])
                    if query.casefold() in note["markdown"].casefold()]

    def _registration_identity_paths(self, note_id):
        """Read-only duplicate check; unlike _scan this never retains objects."""
        from .work_catalog import enabled
        if enabled(self._read_state()):
            from .bootstrap import _index
            note = _index(self)[1].get(note_id)
            return [note["path"]] if note else []
        found = []
        for directory, names, filenames in os.walk(self.vault, followlinks=False):
            for name in list(names):
                candidate = Path(directory) / name
                if name.startswith("."):
                    names.remove(name)
                elif candidate.is_symlink():
                    raise LibraryError(f"Symlink directory in Vault: {candidate}")
            for name in filenames:
                if name.startswith(".") or not name.lower().endswith(".md"):
                    continue
                relative = (Path(directory) / name).relative_to(self.vault).as_posix()
                properties, _ = _split(_read(self._path(relative)))
                if properties.get("id") == note_id:
                    found.append(relative)
        return found

    def _registration_plan(self, state, path, *, title, kind, expected_hash):
        path = Path(path).as_posix()
        if not path.lower().endswith(".md") or any(part.startswith(".") for part in Path(path).parts):
            raise LibraryError("Registration requires a visible existing .md path")
        destination = self._path(path)
        if not destination.is_file():
            raise LibraryError(f"Existing Markdown file is missing: {path}")
        markdown = _read(destination)
        digest = _hash(markdown)
        if expected_hash is not None:
            self._expect({"sha256": digest}, expected_hash)
        properties, frontmatter = _registration_frontmatter(markdown)
        note_id = properties.get("id")
        if "id" in properties:
            record = state["notes"].get(note_id) if isinstance(note_id, str) else None
            if not record or record.get("deleted") or record.get("unregistered"):
                raise LibraryError("Reserved property collision: existing id is not a registered library identity")
            self._validate_properties(properties)
            if properties["kind"] != kind or (title is not None and title != properties["title"]):
                raise LibraryError("Registration cannot overwrite existing identity, kind, or title")
            matches = self._registration_identity_paths(note_id)
            if matches != [path]:
                raise LibraryError(f"Duplicate stable id {note_id}: {matches}")
            return {"outcome": "existing", "id": note_id, "path": path, "sha256": digest,
                    "markdown": markdown, "properties": properties, "original_sha256": digest,
                    "registration_original_sha256": record.get("registration_original")}
        collisions = {"kind", "schema_version"} & properties.keys()
        if collisions:
            raise LibraryError(f"Reserved property collision: {', '.join(sorted(collisions))}")
        if "title" in properties:
            if title is not None and title != properties["title"]:
                raise LibraryError("Registration cannot overwrite an existing title")
            title = properties["title"]
        elif title is None:
            title = destination.stem
        marker = json.loads(_read(self._path(".markdown-library/marker.json")))
        note_id = str(uuid5(NAMESPACE_URL, f"markdown-library:{marker['library_id']}:{path}:{digest}"))
        previous = next(((key, row) for key, row in state["notes"].items()
                         if row.get("unregistered") and row.get("path") == path
                         and row.get("registration_original") == digest), None)
        if previous:
            note_id = previous[0]
        elif note_id in state["notes"]:
            raise LibraryError("Registration identity collides with retained library history")
        if self._registration_identity_paths(note_id):
            raise LibraryError("Registration identity already occurs in another Markdown file")
        added = {"id": note_id, "kind": kind, "schema_version": 1}
        if "title" not in properties:
            added["title"] = title
        properties = {**properties, **added}
        self._validate_properties(properties)
        newline = "\r\n" if (frontmatter and frontmatter[0][0].endswith("\r\n")) else "\n"
        if not frontmatter and "\r\n" in markdown:
            newline = "\r\n"
        additions = yaml.safe_dump(added, allow_unicode=True, sort_keys=False).replace("\n", newline)
        bom = "\ufeff" if markdown.startswith("\ufeff") else ""
        if frontmatter:
            lines, end = frontmatter
            content = bom + "".join(lines[:end]) + additions + "".join(lines[end:])
        else:
            content = bom + "---" + newline + additions + "---" + newline + markdown.removeprefix("\ufeff")
        return {"outcome": "planned", "id": note_id, "path": path, "sha256": _hash(content),
                "markdown": content, "properties": properties, "original_sha256": digest}

    def register(self, path, *, title=None, kind="note", apply=False, expected_hash=None):
        """Track one explicitly selected file in place; preview is strictly read-only."""
        if not apply:
            self.validate()
            if self._path(".markdown-library/transaction.json").exists():
                raise LibraryError("Pending library transaction; finish recovery before registration preview")
            plan = self._registration_plan(self._read_state(), path, title=title, kind=kind, expected_hash=expected_hash)
            return {**plan, "applied": False, "selected_for_publication": False}
        if not expected_hash:
            raise LibraryError("Registration apply requires expected_hash from the current original file")
        with self._lock():
            state = self._load()
            plan = self._registration_plan(state, path, title=title, kind=kind, expected_hash=expected_hash)
            if plan["outcome"] == "existing":
                return {**plan, "applied": False, "selected_for_publication": False}
            original = _read(self._path(plan["path"]))
            self._expect({"sha256": _hash(original)}, expected_hash)
            for digest in (expected_hash, plan["sha256"]):
                retained = self._path(f".markdown-library/objects/{digest}.md")
                if retained.exists() and _hash(_read(retained)) != digest:
                    raise LibraryError("Corrupted history object")
            self._remember(state, {**plan, "markdown": original, "sha256": expected_hash}, "registration-original")
            state["notes"][plan["id"]]["registration_original"] = expected_hash
            state["notes"][plan["id"]].pop("unregistered", None)
            self._remember(state, plan, "registered")
            if kind == 'work' and not plan['properties'].get('slug'):
                from .work_profile import set_profile, WEBSITE_ONLY
                retained = {key: value for key, value in plan['properties'].items() if key in WEBSITE_ONLY}
                retained.update({'slug': 'work-' + plan['id'], 'generateSlug': False})
                set_profile(state, plan['id'], retained)
            self._commit(state, [(plan["path"], plan["markdown"], expected_hash)])
            return {**plan, "outcome": "registered", "applied": True, "selected_for_publication": False}

    track_existing = register

    def restore_original(self, note_id, *, expected_hash):
        """Restore exact pre-registration bytes in place, retaining private history."""
        with self._lock():
            state = self._load()
            current = self._required(self._scan(state), note_id)
            self._expect(current, expected_hash)
            record = state["notes"][note_id]
            digest = record.get("registration_original")
            if not digest:
                raise LibraryError("This note has no pre-registration original")
            if note_id in state["publications"]:
                raise LibraryError("Resolve publication history before removing the library identity")
            original = _read(self._path(f".markdown-library/objects/{digest}.md"))
            if _hash(original) != digest:
                raise LibraryError("Corrupted history object")
            record.update({"unregistered": True, "sha256": digest})
            record["history"].append({"sha256": digest, "observed_at": _now(), "action": "registration-restored"})
            self._commit(state, [(current["path"], original, expected_hash)])
            return {"id": note_id, "path": current["path"], "sha256": digest, "unregistered": True}

    def _create(self, state, markdown, *, title, kind, properties=None, path=None):
        metadata, body = _split(markdown)
        if properties is not None and not isinstance(properties, dict):
            raise LibraryError("properties must be a mapping")
        metadata.update(_plain({} if properties is None else properties))
        note_id = str(uuid4())
        metadata.update({"id": note_id, "kind": kind, "title": title, "schema_version": 1})
        if kind == "work" and state.get("work_catalog"):
            from .work_catalog import creation_defaults
            creation_defaults(metadata, state['work_catalog'])
        self._validate_properties(metadata)
        if path is None:
            slug = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE).strip().replace(" ", "-")[:70] or "note"
            folder = {"note": "00_收件箱", "post": "发表稿", "work": "works"}[kind]
            if kind == "work" and state.get("work_catalog"):
                folder = state["work_catalog"].get("review_root", "works/感想")
            path = f"{folder}/{slug}-{note_id[:8]}.md"
        path = Path(path).as_posix()
        if any(part.startswith(".") for part in Path(path).parts) or not path.lower().endswith(".md"):
            raise LibraryError("New notes require a visible .md path")
        destination = self._path(path)
        if destination.exists():
            raise LibraryError(f"Refusing to overwrite existing path: {path}")
        retained_work = None
        if kind == 'work' and not metadata.get('slug'):
            from .work_profile import WEBSITE_ONLY
            retained_work = {key: metadata.pop(key) for key in list(metadata) if key in WEBSITE_ONLY}
            retained_work.update({'slug': 'work-' + note_id, 'generateSlug': False})
        content = _render(metadata, body)
        note = {"id": note_id, "path": path, "sha256": _hash(content), "markdown": content, "properties": metadata}
        from .work_catalog import enabled, catalog_entry, has_review_body
        if kind == "work" and enabled(state):
            review_path = path if has_review_body(content) else None
            state["work_catalog"]["rows"][note_id] = catalog_entry(note, review_path)
            note["path"] = review_path or state["work_catalog"]["path"] + "#work-" + note_id
            note["catalog_backed"] = True
        self._remember(state, note, "created")
        if retained_work is not None:
            from .work_profile import set_profile
            set_profile(state, note_id, retained_work)
        return note

    def create(self, markdown="", *, title, kind="note", properties=None, path=None):
        with self._lock():
            state = self._load()
            self._scan(state)
            note = self._create(state, markdown, title=title, kind=kind, properties=properties, path=path)
            self._commit(state, [(note["path"], note["markdown"], None)])
            return copy.deepcopy(note)

    def import_note(self, markdown, *, source_id, title, kind="note", source=None):
        if not isinstance(source_id, str) or not source_id.strip():
            raise LibraryError("source_id must be nonempty text")
        with self._lock():
            state = self._load()
            previous = state["sources"].get(source_id)
            if previous and state["notes"].get(previous["id"], {}).get("purged"):
                raise LibraryError("Source identity was permanently deleted; import cannot recreate it")
            notes = self._scan(state)
            if previous:
                note = self._required(notes, previous["id"])
                self._save(state)
                return {"outcome": "existing", **copy.deepcopy(note)}
            provenance = _plain({} if source is None else source)
            if not isinstance(provenance, dict):
                raise LibraryError("source must be a mapping")
            note = self._create(state, markdown, title=title, kind=kind)
            state["sources"][source_id] = {"id": note["id"], "source": provenance}
            self._commit(state, [(note["path"], note["markdown"], None)])
            return {"outcome": "created", **copy.deepcopy(note)}

    def replace(self, note_id, markdown, *, expected_hash, website_metadata=None):
        with self._lock():
            state = self._load()
            note = self._required(self._scan(state), note_id)
            self._expect(note, expected_hash)
            properties, _ = _split(markdown)
            self._validate_properties(properties)
            if properties["id"] != note_id:
                raise LibraryError("Replacement must preserve stable id")
            self._assert_note_storage(note)
            updated = {**note, "sha256": _hash(markdown), "markdown": markdown, "properties": properties}
            self._remember(state, updated, "replaced")
            if website_metadata is not None:
                from .work_profile import profile, set_profile
                if properties['kind'] != 'work' or profile(self, note_id, state) is None:
                    raise LibraryError('Website metadata updates require an already compacted Work')
                set_profile(state, note_id, website_metadata)
            self._commit(state, [(note["path"], markdown, expected_hash)])
            return copy.deepcopy(updated)

    def history(self, note_id):
        with self._lock():
            state = self._load()
            self._scan(state)
            if note_id not in state["notes"]:
                raise LibraryError(f"Unknown id: {note_id}")
            if state["notes"][note_id].get("purged"):
                raise LibraryError("Permanently deleted identity has no recoverable history")
            self._save(state)
            return copy.deepcopy(state["notes"][note_id]["history"])

    def history_markdown(self, note_id, sha256):
        versions = self.history(note_id)
        if sha256 not in {version["sha256"] for version in versions}:
            raise LibraryError("Unknown history revision")
        markdown = _read(self._path(f".markdown-library/objects/{sha256}.md"))
        if _hash(markdown) != sha256:
            raise LibraryError("Corrupted history object")
        return markdown

    def retain_revision(self, note_id, markdown, *, expected_hash, source=None):
        """Retain an imported checkpoint without replacing or selecting current.

        The revision must use this document's stable identity. Provenance is
        private history metadata; an imported publication is never a local
        publication selection.
        """
        with self._lock():
            state = self._load()
            current = self._required(self._scan(state), note_id)
            self._expect(current, expected_hash)
            properties, _ = _split(markdown)
            self._validate_properties(properties)
            if properties["id"] != note_id or properties["kind"] != current["properties"]["kind"]:
                raise LibraryError("Retained revision must preserve document identity and kind")
            provenance = _plain({} if source is None else source)
            if not isinstance(provenance, dict):
                raise LibraryError("Retained source must be a mapping")
            digest = _hash(markdown)
            relative = f".markdown-library/objects/{digest}.md"
            if self._path(relative).exists() and _hash(_read(self._path(relative))) != digest:
                raise LibraryError("Corrupted history object")
            if not self._path(relative).exists():
                self._atomic(relative, markdown)
            history = state["notes"][note_id]["history"]
            if not any(row["sha256"] == digest and row.get("source") == provenance for row in history):
                history.append({"sha256": digest, "observed_at": _now(), "action": "retained", "source": provenance})
            self._save(state)
            return {"id": note_id, "sha256": digest, "current_sha256": current["sha256"], "selected_for_publication": False}

    def restore(self, note_id, sha256, *, expected_hash):
        # replace rescans and preserves the current version before restoring.
        markdown = self.history_markdown(note_id, sha256)
        from .ratings import canonical_revision
        markdown = canonical_revision(self, note_id, sha256, markdown)
        properties, body = _split(markdown)
        if properties.get('kind') == 'work':
            from .work_profile import profile, WEBSITE_ONLY
            if profile(self, note_id) is not None and set(properties) & WEBSITE_ONLY:
                # Restoring personal writing must not resurrect website-only
                # YAML or roll back the latest website metadata observation.
                properties = {key: value for key, value in properties.items() if key not in WEBSITE_ONLY}
                markdown = _render(properties, body)
        return self.replace(note_id, markdown, expected_hash=expected_hash)

    def publish(self, note_id, *, expected_hash):
        with self._lock():
            state = self._load()
            notes = self._scan(state)
            note = self._required(notes, note_id)
            self._expect(note, expected_hash)
            from .work_profile import website_properties
            props = website_properties(self, note, state)
            kind = props["kind"]
            if kind not in {"work", "post"}:
                raise LibraryError("Only works and posts may be published")
            if kind == "post":
                theme = props.get("frontmatter") or {}
                body = _split(note["markdown"])[1]
                if not isinstance(theme, dict):
                    raise LibraryError("Post theme frontmatter must be a mapping")
                if props.get("protectedContent") or props.get("password") or theme.get("password") or re.search(r"^[ \t]*:{3,}[ \t]*encrypted\b", body, re.MULTILINE):
                    raise LibraryError("Protected posts cannot be published by this local prototype")
                unknown = set(theme) - PUBLIC_THEME_FIELDS
                if unknown:
                    raise LibraryError(f"Unsupported Post theme options require explicit review: {', '.join(sorted(unknown))}")
            fields = PUBLIC_WORK_FIELDS if kind == "work" else PUBLIC_POST_FIELDS
            public_properties = {key: copy.deepcopy(value) for key, value in props.items() if key in fields}
            if kind == "work" and "article" in public_properties:
                from .relations import canonical_article
                public_properties["article"] = canonical_article(self, public_properties["article"], notes=notes)
            for relationship in ("cover", "article"):
                if isinstance(public_properties.get(relationship), dict):
                    public_properties[relationship] = public_properties[relationship].get("id")
            for field, row_fields in PUBLIC_ROWS.items():
                if field in public_properties:
                    rows = public_properties[field]
                    if rows is None:
                        continue
                    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                        raise LibraryError(f"{field} must be a list of row mappings")
                    public_properties[field] = [{key: copy.deepcopy(value) for key, value in row.items() if key in row_fields} for row in rows]
            snapshot = {"id": note_id, "kind": kind, "title": props["title"],
                        "properties": public_properties}
            if kind == "post":
                snapshot["markdown"] = _split(note["markdown"])[1]
            record = {"id": note_id, "kind": kind, "state": "published", "sha256": note["sha256"],
                      "snapshot": snapshot, "published_at": _now()}
            state["publications"][note_id] = record
            self._save(state)
            return copy.deepcopy(record)

    def unpublish(self, note_id):
        with self._lock():
            state = self._load()
            record = state["publications"].get(note_id)
            if not record:
                raise LibraryError("This note has never been published")
            if record["state"] != "deleted":
                record.update({"state": "withdrawn", "withdrawn_at": _now()})
            self._save(state)
            return copy.deepcopy(record)

    def publication(self, note_id):
        with self._lock():
            record = self._load()["publications"].get(note_id)
            return copy.deepcopy(record["snapshot"]) if record and record["state"] == "published" else None

    def publications(self):
        with self._lock():
            state = self._load()
            return copy.deepcopy(sorted(state["publications"].values(), key=lambda record: record["id"]))

    def deletion_records(self):
        """Read explicit lifecycle intents without interpreting missing files."""
        self.validate()
        state = self._read_state()
        return {note_id: copy.deepcopy(record) for note_id, record in state["notes"].items()
                if record.get("lifecycle") or record.get("deleted")}

    def delete(self, note_id, *, expected_hash, origin="local", event=None):
        """Recoverable explicit delete. Missing files never create an intent."""
        if origin not in {"local", "website"}:
            raise LibraryError("Unsupported deletion origin")
        with self._lock():
            state = self._load()
            record = state["notes"].get(note_id)
            if record and record.get("purged"):
                raise LibraryError("Identity was permanently deleted")
            if record and record.get("deleted"):
                markdown = _read(self._path(record["trash"]))
                self._expect({"sha256": _hash(markdown)}, expected_hash)
                return {"id": note_id, "path": record["path"], "sha256": expected_hash,
                        "deleted": True, "already_deleted": True}
            note = self._required(self._scan(state), note_id)
            self._expect(note, expected_hash)
            trash = f".markdown-library/trash/{note_id}.md"
            previous_trash = self._path(trash)
            trash_hash = _hash(_read(previous_trash)) if previous_trash.exists() else None
            self._assert_note_storage(note)
            record = state["notes"][note_id]
            moment = _now()
            record.update({"deleted": True, "trash": trash, "deleted_at": moment,
                           "lifecycle": {"intent": "delete", "token": str(uuid4()), "origin": origin,
                                         "sha256": note["sha256"], "requested_at": moment,
                                         "event": _plain(event)}})
            if note_id in state["publications"]:
                state["publications"][note_id].update({"state": "deleted", "deleted_at": moment})
            self._commit(state, [(trash, note["markdown"], trash_hash)], [(note["path"], expected_hash)])
            return {"id": note_id, "path": note["path"], "sha256": note["sha256"], "deleted": True}

    def confirm_missing_delete(self, note_id, *, expected_hash, apply=False):
        """Confirm a native deletion explicitly using the last retained object.

        Identity discovery distinguishes a rename from absence. Preview never
        creates a lock, history object, trash file or tombstone.
        """
        def plan(state):
            record = state["notes"].get(note_id)
            if record and record.get("purged"):
                raise LibraryError("Identity was permanently deleted")
            if not record or record.get("unregistered"):
                raise LibraryError("Unknown managed id")
            if self._registration_identity_paths(note_id):
                raise LibraryError("Identity still exists in the Vault; native rename is not a deletion")
            digest = record.get("sha256")
            self._expect({"sha256": digest}, expected_hash)
            object_path = self._path(f".markdown-library/objects/{digest}.md")
            if not object_path.is_file():
                raise LibraryError("Last retained object is missing; explicit deletion cannot be confirmed")
            markdown = _read(object_path)
            if _hash(markdown) != digest:
                raise LibraryError("Corrupted history object")
            properties, _ = _split(markdown)
            self._validate_properties(properties)
            if properties["id"] != note_id or properties["kind"] != record["kind"]:
                raise LibraryError("Retained object identity differs")
            if self._path(record["path"]).exists():
                raise LibraryError("Original path is occupied; preserve the new file and resolve the path first")
            return record, markdown
        if not apply:
            self.validate()
            if self._path(".markdown-library/transaction.json").exists():
                raise LibraryError("Pending transaction; recover it before preview")
            record, _ = plan(self._read_state())
            return {"id": note_id, "path": record["path"], "sha256": expected_hash,
                    "action": "confirm-missing-delete", "already_deleted": bool(record.get("deleted")), "applied": False}
        with self._lock():
            state = self._load()
            record, markdown = plan(state)
            if record.get("deleted"):
                return {"id": note_id, "sha256": expected_hash, "already_deleted": True, "applied": False}
            trash = f".markdown-library/trash/{note_id}.md"
            previous = self._path(trash)
            trash_hash = _hash(_read(previous)) if previous.exists() else None
            moment = _now()
            record.update({"deleted": True, "trash": trash, "deleted_at": moment,
                           "lifecycle": {"intent": "delete", "token": str(uuid4()), "origin": "confirmed-missing",
                                         "sha256": expected_hash, "requested_at": moment}})
            if note_id in state["publications"]:
                state["publications"][note_id].update({"state": "deleted", "deleted_at": moment})
            self._commit(state, [(trash, markdown, trash_hash)], [(record["path"], None)])
            return {"id": note_id, "path": record["path"], "sha256": expected_hash, "deleted": True, "applied": True}

    def recover(self, note_id, *, path=None, expected_hash, origin="local"):
        if origin not in {"local", "website"}:
            raise LibraryError("Unsupported recovery origin")
        with self._lock():
            state = self._load()
            record = state["notes"].get(note_id)
            if record and record.get("purged"):
                raise LibraryError("Identity was permanently deleted and cannot be recovered")
            if not record or not record.get("deleted"):
                raise LibraryError("Note is not soft deleted")
            markdown = _read(self._path(record["trash"]))
            self._expect({"sha256": _hash(markdown)}, expected_hash)
            destination = path or record["path"]
            from .work_catalog import enabled
            catalog_work = record.get("kind") == "work" and enabled(state) and note_id in state["work_catalog"]["rows"]
            if not catalog_work and (not destination.lower().endswith(".md") or any(part.startswith(".") for part in Path(destination).parts)):
                raise LibraryError("Recovery requires a visible Markdown path")
            if self._path(destination).exists():
                raise LibraryError("Recovery path is occupied")
            self._scan(state)
            from .ratings import canonical_revision
            markdown = canonical_revision(self, note_id, expected_hash, markdown, state=state)
            properties, _ = _split(markdown)
            self._validate_properties(properties)
            if properties["id"] != note_id:
                raise LibraryError("Trash identity differs from retained note")
            note = {"id": note_id, "path": destination, "sha256": _hash(markdown), "markdown": markdown, "properties": properties}
            self._remember(state, note, "recovered")
            record["lifecycle"] = {"intent": "restore", "token": str(uuid4()), "origin": origin,
                                   "sha256": note["sha256"], "requested_at": _now()}
            # Recovery restores the current draft, never the old publication choice.
            if note_id in state["publications"]:
                state["publications"][note_id].update({"state": "withdrawn", "withdrawn_at": _now()})
            self._commit(state, [(destination, markdown, None)])
            return copy.deepcopy(note)

    def purge(self, note_id, *, expected_hash, remote_proof, expected_token=None, apply=False):
        """Permanently remove one observed soft-deletion; preview never writes.

        Explicit callers must first hard-delete the mapped remote and retain its
        real receipt plus independent latest/formal 404 observations. Sources
        and observed mapping hashes remain; backups/Git/exports are out of scope.
        """
        proof = _purge_proof(remote_proof)

        def plan():
            self.validate()
            if self._path(".markdown-library/transaction.json").exists():
                raise LibraryError("Pending library transaction; recover it before permanent deletion")
            state = self._read_state()
            record = state["notes"].get(note_id)
            if not record or not record.get("deleted"):
                raise LibraryError("Permanent deletion requires an already soft-deleted identity")
            self._expect({"sha256": record.get("sha256")}, expected_hash)
            from .transport import _Store
            sync = _Store(self, proof["target"]).load()
            baseline = sync["baselines"].get(note_id, {})
            receipt = sync.get("lifecycle", {}).get(note_id, {})
            intent = record.get("lifecycle", {})
            if (baseline.get("collection") != proof["collection"]
                    or str(baseline.get("remote_id")) != proof["remote_id"]
                    or baseline.get("remote_version") != proof["previous_remote_version"]
                    or baseline.get("state") != "deleted"
                    or record.get("kind") != {"works": "work", "posts": "post"}[proof["collection"]]
                    or not baseline.get("identity")
                    or receipt.get("deleted") is not True
                    or intent.get("intent") != "delete" or not intent.get("token")
                    or receipt.get("token") != intent["token"]
                    or intent.get("sha256") != expected_hash
                    or receipt.get("local_sha256") != expected_hash
                    or receipt.get("remote_version") != baseline.get("remote_version")
                    or receipt.get("remote_hash") != baseline.get("remote_hash")
                    or note_id in sync.get("pending", {}) or note_id in sync.get("lifecycle_pending", {})):
                raise LibraryError("Hard-delete proof does not match an observed mapped soft-deletion")
            if record.get("purged"):
                if record.get("remote_proof") != proof:
                    raise LibraryError("Permanent-deletion proof differs from retained tombstone")
                return {"id": note_id, "action": "already-purged", "applied": False}, None
            if self._registration_identity_paths(note_id):
                raise LibraryError("Soft-deleted identity reappeared; permanent deletion refused")
            if record.get("path") and self._path(record["path"]).exists():
                raise LibraryError("Original path is occupied; preserve that file and review")
            trash = f".markdown-library/trash/{note_id}.md"
            if record.get("trash") != trash:
                raise LibraryError("Unexpected trash path; permanent deletion refused")
            self._expect({"sha256": _hash(_read(self._path(trash)))}, expected_hash)
            new_state, new_sync = copy.deepcopy(state), copy.deepcopy(sync)
            if new_state.get("work_catalog"):
                new_state["work_catalog"].get("rows", {}).pop(note_id, None)
                new_state["work_catalog"].get("daily_origins", {}).pop(note_id, None)
            new_state["publications"].pop(note_id, None)
            candidates = {expected_hash}
            candidates.update(row["sha256"] for row in record.get("history", []))
            if record.get("registration_original"):
                candidates.add(record["registration_original"])
            publication = state["publications"].get(note_id)
            if publication and publication.get("sha256"):
                candidates.add(publication["sha256"])
            if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in candidates):
                raise LibraryError("Invalid retained object digest; permanent deletion refused")
            # Preserve the real mapping/version/hashes; drop only retained field
            # payloads, never manufacture a new ordinary synchronization baseline.
            for key in ("local_fields", "remote_fields", "ancestor_evidence"):
                new_sync["baselines"][note_id].pop(key, None)
            new_sync["reverse_imports"].pop(note_id, None)
            reference_state = copy.deepcopy(new_state)
            reference_state["notes"].pop(note_id)
            reference_state["sources"] = {key: row for key, row in reference_state["sources"].items()
                                            if row.get("id") != note_id}
            reference_sync = copy.deepcopy(new_sync)
            reference_sync["baselines"].pop(note_id)
            reference_sync.get("lifecycle", {}).pop(note_id, None)
            shared = set()

            def references(value):
                if isinstance(value, dict):
                    for child in value.values(): references(child)
                elif isinstance(value, list):
                    for child in value: references(child)
                elif isinstance(value, str) and value in candidates:
                    shared.add(value)

            references(reference_state); references(reference_sync)
            bootstrap_path = self._path(".markdown-library/bootstrap-state.json")
            if bootstrap_path.exists():
                bootstrap = json.loads(_read(bootstrap_path))
                references({key: row for key, row in bootstrap.get("imports", {}).items() if row.get("id") != note_id})
            removes = [(trash, expected_hash)]
            for digest in sorted(candidates - shared):
                relative = f".markdown-library/objects/{digest}.md"
                path = self._path(relative)
                if path.exists():
                    if _hash(_read(path)) != digest:
                        raise LibraryError("Retained object was edited; permanent deletion refused")
                    removes.append((relative, digest))
            evidence = self._path(f".markdown-library/lifecycle/{note_id}")
            if evidence.exists():
                for path in sorted(evidence.iterdir()):
                    if not re.fullmatch(r"[a-f0-9]{64}\.json", path.name) or not path.is_file():
                        raise LibraryError("Unexpected lifecycle evidence file; preserve and review")
                    relative = path.relative_to(self.vault).as_posix()
                    removes.append((relative, _hash(_read(self._path(relative)))))
            token = _hash(json.dumps({"id": note_id, "record": record, "proof": proof,
                                     "baseline": baseline, "receipt": receipt, "publication": publication,
                                     "removes": removes, "shared": sorted(shared)}, sort_keys=True))
            result = {"id": note_id, "action": "purge", "applied": False, "purge_token": token,
                      "expected_hash": expected_hash, "remove_paths": [path for path, _ in removes],
                      "shared_objects_preserved": sorted(shared), "remote_proof": proof}
            return result, (new_state, new_sync, record, removes)

        if not apply:
            return plan()[0]
        if not expected_token:
            raise LibraryError("Permanent deletion requires the preview token")
        from .transport import _Store
        # Match the existing sync -> library lock order; the timer cannot race
        # mapping updates and ordinary library writers cannot alter source links.
        with _Store(self, proof["target"]).locked(), self._lock():
            result, changes = plan()
            if result["action"] == "already-purged":
                return result
            if result["purge_token"] != expected_token:
                raise LibraryError("Permanent-deletion preview is stale; preview again")
            new_state, new_sync, record, removes = changes
            new_state["notes"][note_id] = {
                "kind": record["kind"], "sha256": expected_hash, "deleted": True, "purged": True,
                "deleted_at": record["deleted_at"], "purged_at": _now(), "history": [],
                "lifecycle": {key: record["lifecycle"][key] for key in ("intent", "token", "origin", "sha256", "requested_at")},
                "remote_proof": proof,
            }
            relative = ".markdown-library/sync-state.json"
            self._commit(new_state, [(relative, json.dumps(new_sync, ensure_ascii=False, indent=2),
                                       _hash(_read(self._path(relative))))], removes)
            return {**result, "applied": True}
