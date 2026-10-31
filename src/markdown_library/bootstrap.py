"""Import existing Payload content as local working files, never publications.

Preview is read-only. Apply calls only local Library APIs; the optional client is
used solely for complete draft/base collection reads. No sync baseline is made.
"""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .core import LibraryError, _hash, _plain, _render, _split
from .sync import POST_FIELDS, WORK_FIELDS, _remote_documents, content_hash


class BootstrapError(LibraryError):
    pass


def _target(value):
    if not isinstance(value, str):
        raise BootstrapError("An explicit source target origin is required")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise BootstrapError("Source target must be an HTTP(S) origin without credentials, path or query")
    try:
        port = parsed.port
    except ValueError:
        raise BootstrapError("Invalid source target port") from None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = "" if port is None or (parsed.scheme, port) in {("https", 443), ("http", 80)} else f":{port}"
    return parsed.scheme.lower() + "://" + host + suffix


def source_id(target, collection, remote_id):
    """An opaque stable key for the exact target/collection/document identity."""
    return "payload-bootstrap:" + content_hash({"target": _target(target), "collection": collection, "remote_id": str(remote_id)})


def _shape(row, collection):
    if not isinstance(row.get("title"), str) or not row["title"].strip():
        raise BootstrapError("Remote record has no nonempty title; review this incomplete draft")
    fields = (WORK_FIELDS | {"generateSlug"}) if collection == "works" else (POST_FIELDS | {"protectedContent"})
    props = {key: deepcopy(value) for key, value in row.items() if key in fields - {"title", "markdown", "_status"}}
    for name in ("article", "cover"):
        if isinstance(props.get(name), dict):
            if "id" not in props[name]:
                raise BootstrapError("Populated relationship lacks an ID")
            props[name] = props[name]["id"]
    if "_status" in row:
        props["payload_observed_status"] = row["_status"]
    markdown = row.get("markdown", "")
    if markdown is None:
        props["payload_markdown_is_null"] = True
        markdown = ""
    if not isinstance(markdown, str):
        raise BootstrapError("Remote Markdown must be text or null")
    ignored = sorted(set(row) - fields - {"id", "createdAt", "updatedAt", "markdown"})
    properties = _plain(props)
    if collection == "works" and "rating" in properties:
        from .ratings import validate_rating
        validate_rating(properties["rating"])
    return {"title": row["title"], "properties": properties, "body": markdown,
            "source_hash": content_hash({"title": row["title"], "properties": properties, "body": markdown}),
            "ignored_fields": ignored}


def _documents(value):
    try:
        rows = _remote_documents(value)
        for row in rows:
            remote_id = row.get("id")
            if isinstance(remote_id, bool) or not isinstance(remote_id, (str, int)) or not str(remote_id).strip():
                raise ValueError("Invalid remote ID")
            _plain(row)
        return rows
    except (TypeError, ValueError) as exc:
        raise BootstrapError("Invalid or incomplete remote collection snapshot") from exc


def _read_source(snapshot, client, target):
    if (snapshot is None) == (client is None):
        raise BootstrapError("Pass exactly one complete snapshot or read-only client")
    if client is not None:
        origin = _target(client.target)
        if target is not None and _target(target) != origin:
            raise BootstrapError("Explicit target differs from configured client")
        latest = {name: _documents(client.list_collection(name, draft=True)) for name in ("posts", "works")}
        base = {name: _documents(client.list_collection(name, draft=False)) for name in ("posts", "works")}
        return origin, latest, base
    if not isinstance(snapshot, dict) or any(name not in snapshot for name in ("works", "posts")):
        raise BootstrapError("Snapshot must explicitly contain complete works and posts collections")
    from .ratings import convert_fields
    scale = snapshot.get("rating_scale")
    if type(scale) is not int or scale not in {10, 100}:
        raise BootstrapError("Offline snapshots require explicit rating_scale: 10 or 100")
    snapshot = deepcopy(snapshot)
    snapshot["works"] = [convert_fields(row, source_scale=scale) for row in _documents(snapshot["works"])]
    if isinstance(snapshot.get("published"), dict) and "works" in snapshot["published"]:
        snapshot["published"]["works"] = [convert_fields(row, source_scale=scale) for row in _documents(snapshot["published"]["works"])]
    embedded = snapshot.get("target")
    if target is not None and embedded is not None and _target(target) != _target(embedded):
        raise BootstrapError("Explicit target differs from snapshot target")
    origin = _target(target if target is not None else embedded)
    latest = {name: _documents(snapshot[name]) for name in ("posts", "works")}
    published = snapshot.get("published", {})
    if not isinstance(published, dict):
        raise BootstrapError("Snapshot published variants must be a mapping")
    base = {name: _documents(published[name]) if name in published else None for name in ("posts", "works")}
    for name in ("posts", "works"):
        if base[name] is not None and set(str(row["id"]) for row in base[name]) - set(str(row["id"]) for row in latest[name]):
            raise BootstrapError("Base snapshot contains IDs missing from latest snapshot")
    return origin, latest, base


def _index(library):
    """Read the managed index without _scan's checkpoint/history side effects."""
    state = library._read_state()
    if library._path(".markdown-library/transaction.json").exists():
        raise BootstrapError("An interrupted library transaction requires ordinary library recovery before bootstrap")
    notes = {}
    for directory, names, files in os.walk(library.vault, followlinks=False):
        for name in list(names):
            candidate = Path(directory) / name
            if name.startswith("."):
                names.remove(name)
            elif candidate.is_symlink():
                raise BootstrapError("Symlink directory in Vault")
        for filename in files:
            if filename.startswith(".") or not filename.lower().endswith(".md"):
                continue
            relative = (Path(directory) / filename).relative_to(library.vault).as_posix()
            markdown = library._path(relative).read_bytes().decode("utf-8")
            properties, _ = _split(markdown)
            note_id = properties.get("id")
            if not isinstance(note_id, str) or (note_id not in state["notes"] and not (properties.get("schema_version") == 1 and "kind" in properties)):
                continue
            library._validate_properties(properties)
            if note_id in notes:
                raise BootstrapError("Duplicate stable library ID")
            notes[note_id] = {"id": note_id, "path": relative, "markdown": markdown, "properties": properties,
                              "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest()}
    from .work_catalog import enabled, scan_overlay
    if enabled(state):
        notes = scan_overlay(library, state, notes)
    return state, notes


class _Receipts:
    relative = ".markdown-library/bootstrap-state.json"

    def __init__(self, library):
        self.library = library

    def load(self):
        filename = self.library._path(self.relative)
        if not filename.exists():
            return {"schema_version": 1, "imports": {}}
        try:
            value = json.loads(filename.read_bytes().decode("utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("imports"), dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise BootstrapError("Invalid bootstrap receipt state; preserve and review it") from None

    def save(self, state):
        self.library._atomic(self.relative, json.dumps(state, ensure_ascii=False, allow_nan=False, indent=2))

    @contextmanager
    def lock(self, apply):
        if not apply:
            yield
            return
        filename = self.library._path(".markdown-library/bootstrap.lock")
        fd = os.open(filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield


def _mappings(library, origin, library_state, notes, receipts):
    mappings = {}
    for key, entry in receipts["imports"].items():
        if entry.get("target") != origin:
            continue
        if key != source_id(origin, entry.get("collection"), entry.get("remote_id")):
            raise BootstrapError("Bootstrap receipt identity is inconsistent")
        mappings[key] = entry["id"]
    for key, entry in library_state["sources"].items():
        if not key.startswith("payload-bootstrap:"):
            continue
        source = entry.get("source", {})
        if source.get("target") != origin:
            continue
        expected = source_id(origin, source.get("collection"), source.get("remote_id"))
        if key != expected or (key in mappings and mappings[key] != entry["id"]):
            raise BootstrapError("Conflicting bootstrap and library source mappings")
        mappings[key] = entry["id"]
    sync_file = library._path(".markdown-library/sync-state.json")
    if sync_file.exists():
        sync = json.loads(sync_file.read_bytes().decode("utf-8"))
        if sync.get("target") == origin:
            for note_id, baseline in sync.get("baselines", {}).items():
                key = source_id(origin, baseline["collection"], baseline["remote_id"])
                if key in mappings and mappings[key] != note_id:
                    raise BootstrapError("Bootstrap conflicts with an existing sync mapping")
                mappings[key] = note_id
    for key, note_id in mappings.items():
        record = library_state["notes"].get(note_id, {})
        if record.get("purged"):
            from .core import _purge_proof
            proof = _purge_proof(record.get("remote_proof"))
            if (not record.get("deleted") or proof["target"] != origin
                    or key != source_id(origin, proof["collection"], proof["remote_id"])):
                raise BootstrapError("Permanent-deletion source mapping requires review")
            continue
        if note_id not in notes or library_state["notes"].get(note_id, {}).get("deleted"):
            raise BootstrapError("An imported or mapped note is missing/deleted; bootstrap never recreates it")
    return mappings


def _linked(shape, post_ids):
    result = deepcopy(shape)
    article = result["properties"].get("article")
    if article is not None and str(article) in post_ids:
        result["properties"]["article"] = post_ids[str(article)]
    return result


def _markdown(shape, kind, note_id=None):
    properties = deepcopy(shape["properties"])
    if note_id is not None:
        properties.update({"id": note_id, "kind": kind, "title": shape["title"], "schema_version": 1})
    return _render(properties, shape["body"])


def _retained_source(origin, collection, remote, shape):
    return {"type": "payload-base-checkpoint", "target": origin, "collection": collection,
            "remote_id": remote["id"], "source_status": remote.get("_status"), "source_hash": shape["source_hash"]}


def _unchanged_checkpoint(library, library_state, note, receipt, source_hash, base_markdown, base_source):
    """Reuse only an unchanged current file plus an actually verified checkpoint.

    _index has already read every current file and checked paths/identities. This
    shortcut does not cache filesystem facts across commands or repair corruption.
    """
    record = library_state["notes"].get(note["id"], {})
    if (note["sha256"] != receipt.get("imported_sha256")
            or receipt.get("last_seen_source_hash") != source_hash
            or record.get("sha256") != note["sha256"] or record.get("path") != note["path"]
            or record.get("kind") != note["properties"]["kind"] or record.get("deleted")):
        return False
    expected = _hash(base_markdown)
    if receipt.get("base_revision_sha256") != expected:
        return False
    if not any(entry.get("sha256") == expected and entry.get("action") == "retained"
               and entry.get("source") == base_source for entry in record.get("history", [])):
        return False
    checkpoint = library._path(f".markdown-library/objects/{expected}.md")
    try:
        actual = checkpoint.read_bytes()
    except FileNotFoundError:
        return False
    # A mismatch follows the ordinary retain_revision path, which fails visibly.
    return hashlib.sha256(actual).hexdigest() == expected


def bootstrap(library, *, snapshot=None, client=None, target=None, apply=False):
    """Preview or import local working files; remote APIs are read-only always."""
    library.validate()
    origin, latest, bases = _read_source(snapshot, client, target)
    store = _Receipts(library)
    with store.lock(apply):
        receipts = store.load()
        library_state, notes = _index(library)
        mappings = _mappings(library, origin, library_state, notes, receipts)
        base_by_id = {name: {str(row["id"]): row for row in (bases[name] or [])} for name in ("posts", "works")}
        shapes = {}
        for collection in ("posts", "works"):
            for row in latest[collection]:
                shapes[(collection, str(row["id"]))] = _shape(row, collection)
                if str(row["id"]) in base_by_id[collection]:
                    _shape(base_by_id[collection][str(row["id"])], collection)
                note_id = mappings.get(source_id(origin, collection, row["id"]))
                if note_id and library_state["notes"].get(note_id, {}).get("purged"):
                    raise BootstrapError("Permanently deleted remote identity reappeared; bootstrap never recreates it")
                expected_kind = "post" if collection == "posts" else "work"
                if note_id and notes[note_id]["properties"]["kind"] != expected_kind:
                    raise BootstrapError("Remote mapping points to a different local document kind")
        post_ids = {str(row["id"]): mappings.get(source_id(origin, "posts", row["id"])) for row in latest["posts"]}
        post_ids = {key: value for key, value in post_ids.items() if value is not None}
        results = []
        for collection in ("posts", "works"):
            kind = "post" if collection == "posts" else "work"
            for remote in latest[collection]:
                remote_id = str(remote["id"])
                key = source_id(origin, collection, remote_id)
                shape = shapes[(collection, remote_id)]
                seed = library_state["sources"].get(key, {}).get("source", {})
                previous = receipts["imports"].get(key, {})
                imported_hash = previous.get("imported_source_hash", seed.get("source_hash"))
                note_id = mappings.get(key)
                result = {"collection": collection, "remote_id": remote["id"], "source_id": key,
                          "action": "existing" if note_id else "import", "applied": False,
                          "source_status": remote.get("_status"), "base_read": bases[collection] is not None,
                          "has_base_variant": remote_id in base_by_id[collection],
                          "selected_for_publication": False, "ignored_fields": shape["ignored_fields"]}
                if imported_hash is not None and imported_hash != shape["source_hash"]:
                    result["source_changed"] = True
                if note_id:
                    if notes[note_id]["properties"]["kind"] != kind:
                        raise BootstrapError("Remote mapping points to a different local document kind")
                    result.update({"id": note_id, "path": notes[note_id]["path"], "sha256": notes[note_id]["sha256"]})
                if collection == "works" and shape["properties"].get("article") is not None and str(shape["properties"]["article"]) not in post_ids:
                    result["relationship_notice"] = "Related Post will map on apply if present; otherwise its remote ID is preserved"
                if apply:
                    base = base_by_id[collection].get(remote_id)
                    base_shape = _linked(_shape(base, collection), post_ids) if base is not None else None
                    base_markdown = _markdown(base_shape, kind, note_id) if base_shape is not None and note_id is not None else None
                    base_source = _retained_source(origin, collection, base, base_shape) if base_shape is not None else None
                    if (note_id is not None and base_markdown is not None
                            and _unchanged_checkpoint(library, library_state, notes[note_id], previous,
                                                      shape["source_hash"], base_markdown, base_source)):
                        result["base_revision_sha256"] = previous["base_revision_sha256"]
                        result["reused_verified_checkpoint"] = True
                        results.append(result)
                        continue
                    provenance = {"type": "payload-bootstrap", "target": origin, "collection": collection,
                                  "remote_id": remote["id"], "source_hash": shape["source_hash"],
                                  "source_status": remote.get("_status"), "source_updated_at": remote.get("updatedAt")}
                    if note_id is None:
                        imported = library.import_note(_markdown(_linked(shape, post_ids), kind), source_id=key,
                                                       title=shape["title"], kind=kind, source=provenance)
                        note_id = imported["id"]
                        mappings[key] = note_id
                        result["action"] = "imported" if imported["outcome"] == "created" else "existing"
                    else:
                        imported = library.get(note_id)
                    notes[note_id] = imported
                    if collection == "posts":
                        post_ids[remote_id] = note_id
                    receipt = {**previous, "target": origin, "collection": collection, "remote_id": remote["id"],
                               "id": note_id, "imported_source_hash": imported_hash or shape["source_hash"],
                               "imported_sha256": previous.get("imported_sha256", imported["sha256"]),
                               "last_seen_source_hash": shape["source_hash"], "source_status": remote.get("_status"),
                               "base_read": bases[collection] is not None, "selected_for_publication": False}
                    receipts["imports"][key] = receipt
                    store.save(receipts)
                    if base is not None:
                        # Newly imported Post identities may now resolve a Work.
                        base_shape = _linked(_shape(base, collection), post_ids)
                        retained = library.retain_revision(note_id, _markdown(base_shape, kind, note_id), expected_hash=imported["sha256"],
                                                           source=_retained_source(origin, collection, base, base_shape))
                        receipt["base_revision_sha256"] = retained["sha256"]
                        receipt["base_status"] = base.get("_status")
                        store.save(receipts)
                        result["base_revision_sha256"] = retained["sha256"]
                    result.update({"id": note_id, "path": imported["path"], "sha256": imported["sha256"], "applied": result["action"] == "imported"})
                results.append(result)
        return {"schema_version": 1, "mode": "local-import" if apply else "read-only-preview", "target": origin,
                "remote_writes": False, "publication_selected": False, "sync_baselines_advanced": False,
                "counts": {"posts": len(latest["posts"]), "works": len(latest["works"])}, "results": results}
