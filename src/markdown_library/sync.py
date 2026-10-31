"""Offline, deterministic publication planning. This module never contacts Payload.

Only durable, explicitly selected publication snapshots enter this boundary. A
plan is not an execution receipt; use observed_baseline only after a successful
write has been independently read back (or explicitly reviewed adoption).
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from .publication_navigation import strip_publication_navigation


WORK_FIELDS = frozenset({
    "title", "slug", "type", "rating", "status", "tags", "note", "article",
    "experiences", "aliases", "cover", "coverUrl", "releaseDate", "creators",
    "platforms", "description", "sources", "visibility", "_status",
})
POST_FIELDS = frozenset({
    "title", "markdown", "link", "locale", "publishedAt", "description", "tags",
    "cover", "categories", "frontmatter", "_status",
})
PUBLIC_THEME_FIELDS = frozenset({
    "subtitle", "catalog", "sticky", "tocNumbering", "excludeFromSummary", "math",
    "quiz", "keywords", "colophon", "works", "updated",
})
ROW_FIELDS = {
    "experiences": {"id", "startedOn", "endedOn", "status", "platform", "progress", "note"},
    "sources": {"id", "provider", "externalId", "url", "fetchedAt"},
}
KINDS = {"work": "works", "post": "posts"}


def _payload_date(value: Any) -> Any:
    """Payload Date fields persist instants with JavaScript millisecond precision.

    Experience dates are text and deliberately never pass through this function.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value  # Server validation can report unsupported date input.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def content_hash(value: Any) -> str:
    """Hash JSON data independently of mapping insertion order and file paths."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _project(values: Mapping[str, Any], collection: str) -> dict[str, Any]:
    allowed = WORK_FIELDS if collection == "works" else POST_FIELDS
    result = {key: deepcopy(value) for key, value in values.items() if key in allowed}
    if collection == "works" and "rating" in result:
        from .ratings import validate_rating
        validate_rating(result["rating"])
    for name in ("publishedAt", "releaseDate"):
        if name in result:
            result[name] = _payload_date(result[name])
    # depth=0 is recommended; tolerate populated relationship objects without
    # forwarding unrelated/private fields from the referenced document.
    for name in ("article", "cover"):
        if isinstance(result.get(name), dict):
            result[name] = result[name].get("id")
    for name, fields in ROW_FIELDS.items():
        if name in result:
            if result[name] is None:
                continue
            if not isinstance(result[name], list):
                raise ValueError(f"{name} must be a list")
            if any(not isinstance(row, Mapping) for row in result[name]):
                raise ValueError(f"{name} rows must be objects")
            result[name] = [{k: v for k, v in row.items() if k in fields}
                            for row in result[name]]
            if name == "sources":
                for row in result[name]:
                    if "fetchedAt" in row:
                        row["fetchedAt"] = _payload_date(row["fetchedAt"])
    content_hash(result)  # Reject non-JSON values and NaN before producing a plan.
    return result


def export_publication(publication: Mapping[str, Any]) -> dict[str, Any]:
    """Return the selected public fields, never the current working file.

    Work Markdown and arbitrary properties/frontmatter are deliberately absent.
    A selected work's structured `note`/`description` remain explicit public data.
    """
    library_id = publication.get("id")
    if not isinstance(library_id, str) or not library_id:
        raise ValueError("publication requires a stable string id")
    kind = publication.get("kind")
    if kind not in KINDS:
        raise ValueError("publication kind must be work or post")
    if publication.get("state") not in {"published", "withdrawn", "deleted"}:
        raise ValueError("publication state must be published, withdrawn or deleted")
    snapshot = publication.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("publication requires its retained selected snapshot")
    if snapshot.get("id", library_id) != library_id or snapshot.get("kind", kind) != kind:
        raise ValueError("publication and snapshot identities differ")
    properties = snapshot.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError("snapshot properties must be an object")
    collection = KINDS[kind]
    fields = _project(properties, collection)
    fields["title"] = snapshot.get("title", properties.get("title"))
    if not isinstance(fields["title"], str) or not fields["title"].strip():
        raise ValueError("selected publication requires a title")
    if kind == "work":
        fields["visibility"] = "public"
        fields["_status"] = "published"
        if not fields.get("slug") or not fields.get("type"):
            raise ValueError("selected work requires a stable slug and explicit type")
        identity = {"slug": fields["slug"]}
    else:
        markdown = snapshot.get("markdown")
        if not isinstance(markdown, str):
            raise ValueError("selected post requires markdown")
        theme = fields.get("frontmatter")
        if theme is not None and not isinstance(theme, Mapping):
            raise ValueError("post frontmatter must be an object or null")
        if (properties.get("protectedContent") or properties.get("password")
                or (isinstance(theme, Mapping) and theme.get("password"))
                or re.search(r"^[ \t]*:{3,}[ \t]*encrypted\b", markdown, re.MULTILINE)):
            raise ValueError("protected posts require a deliberate supported publication flow")
        if isinstance(theme, Mapping) and set(theme) - PUBLIC_THEME_FIELDS:
            raise ValueError("unknown post theme options require explicit review: "
                             + ", ".join(sorted(set(theme) - PUBLIC_THEME_FIELDS)))
        fields["markdown"] = strip_publication_navigation(markdown)
        fields["locale"] = fields.get("locale", "zh")
        fields["_status"] = "published"
        if not fields.get("link"):
            raise ValueError("selected post requires a stable link")
        identity = {"locale": fields["locale"], "link": fields["link"]}
    return {"id": library_id, "collection": collection, "fields": fields,
            "identity": identity, "content_hash": content_hash(fields)}


def _remote_documents(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        if value.get("hasNextPage"):
            raise ValueError("remote snapshot is incomplete: fetch all pages first")
        if "totalDocs" in value and value["totalDocs"] != len(value.get("docs", [])):
            raise ValueError("remote snapshot is incomplete: totalDocs does not match docs")
        value = value.get("docs", [])
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError("remote collection must be a list or standard Payload docs response")
    documents = [deepcopy(dict(row)) for row in value]
    ids = [str(row.get("id")) for row in documents]
    if any(row.get("id") is None for row in documents) or len(ids) != len(set(ids)):
        raise ValueError("remote records require unique ids")
    return documents


def _remote_hash(remote: Mapping[str, Any], collection: str) -> str:
    fields = _project(remote, collection)
    if collection == "posts":
        # These website-only fields are never outgoing library metadata. Their
        # changes still count as concurrent Admin changes and require review.
        for name in ("frontmatter", "protectedContent", "password"):
            if name in remote:
                fields[name] = deepcopy(remote[name])
    return content_hash(fields)


def _matches(remote: Mapping[str, Any], fields: Mapping[str, Any], collection: str) -> bool:
    projection = _project(remote, collection)
    return _contains(projection, fields)


def _contains(actual: Any, selected: Any) -> bool:
    """Allow server-filled defaults/row ids without losing selected row order."""
    if isinstance(selected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _contains(actual[key], value) for key, value in selected.items())
    if isinstance(selected, list):
        return isinstance(actual, list) and len(actual) == len(selected) and all(
            _contains(left, right) for left, right in zip(actual, selected))
    return actual == selected


def _draft(export: Mapping[str, Any], remote: Mapping[str, Any]) -> dict[str, Any]:
    fields = _project(remote, export["collection"])
    fields.pop("visibility", None)
    fields.pop("_status", None)
    title = fields.pop("title", "")
    result = {"id": export["id"], "kind": "work" if export["collection"] == "works" else "post",
              "state": "draft", "title": title, "properties": fields}
    if export["collection"] == "posts":
        result["markdown"] = fields.pop("markdown", "")
    return result


def _action(export: Mapping[str, Any], action: str, reason: str,
            remote: Mapping[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    result = {
        "id": export["id"], "collection": export["collection"], "action": action,
        "reason": reason, "applied": False, "payload": {},
        "publication_hash": export["content_hash"], "identity": deepcopy(export["identity"]),
        "remote_id": remote.get("id") if remote else None,
        "expected_remote_version": remote.get("updatedAt") if remote else None,
        "remote_hash": _remote_hash(remote, export["collection"]) if remote else None,
        "blocked_by": [], "ready_for_executor": False,
        # A stable reconciliation key, not a Payload idempotency guarantee.
        "operation_key": content_hash({"id": export["id"], "collection": export["collection"],
                                       "publication_hash": export["content_hash"], "action": action}),
    }
    result.update(extra)
    return result


def plan_sync(
    publications: Iterable[Mapping[str, Any]],
    baselines: Mapping[str, Mapping[str, Any]],
    remote_records: Mapping[str, Any],
    *, capabilities: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Compare selected snapshots, successful observations, and complete remotes.

    Baselines are keyed by stable library id. Their shape comes from
    observed_baseline(). No baseline is advanced and no state is written here.
    Works publication is capability-blocked until native drafts/version history
    and the website client/publisher have been implemented and verified in WSL.
    """
    capabilities = dict(capabilities or {})
    documents = {collection: _remote_documents(remote_records.get(collection, []))
                 for collection in ("works", "posts")}
    selected = list(publications)
    exports = [export_publication(record) for record in selected]
    if len({item["id"] for item in exports}) != len(exports):
        raise ValueError("duplicate library publication id")
    identities = [(item["collection"], content_hash(item["identity"])) for item in exports]
    if len(set(identities)) != len(identities):
        raise ValueError("multiple publications claim the same website identity")
    claims = [(base.get("collection"), str(base.get("remote_id"))) for base in baselines.values()]
    if len(claims) != len(set(claims)):
        raise ValueError("multiple baselines claim the same website record")
    actions = []
    for publication, export in sorted(zip(selected, exports), key=lambda pair: pair[1]["id"]):
        collection, library_id = export["collection"], export["id"]
        baseline = baselines.get(library_id)
        if baseline and (baseline.get("id") != library_id or baseline.get("collection") != collection):
            raise ValueError("baseline identity or collection mismatch")
        candidates = [row for row in documents[collection]
                      if all(row.get(key, "zh" if key == "locale" else None) == value
                             for key, value in export["identity"].items())]
        remote = next((row for row in documents[collection]
                       if baseline and str(row["id"]) == str(baseline["remote_id"])), None)
        if baseline and remote is None:
            actions.append(_action(export, "conflict", "mapped-remote-missing-review-restoration"))
            continue
        if not baseline:
            if candidates:
                actions.append(_action(export, "adopt-review", "existing-identity-without-shared-baseline",
                                       candidates[0] if len(candidates) == 1 else None,
                                       candidate_ids=sorted([row["id"] for row in candidates], key=str)))
                continue
            if publication["state"] != "published":
                actions.append(_action(export, "unchanged", "unpublished-intent-without-remote"))
                continue
            item = _action(export, "create", "explicit-selected-publication", payload=deepcopy(export["fields"]))
        else:
            assert remote is not None
            if any(str(row["id"]) != str(remote["id"]) for row in candidates):
                actions.append(_action(export, "conflict", "website-identity-owned-by-another-record", remote))
                continue
            remote_hash = _remote_hash(remote, collection)
            local_changed = (export["content_hash"] != baseline.get("local_hash")
                             or publication["state"] != baseline.get("state"))
            remote_changed = remote_hash != baseline.get("remote_hash")
            state = publication["state"]
            prior_fields = baseline.get("local_fields")
            removed_fields = sorted(set(prior_fields) - set(export["fields"])) if isinstance(prior_fields, Mapping) else []
            if state == "published" and local_changed and prior_fields is None:
                unknown_omissions = sorted(set(_project(remote, collection)) - set(export["fields"]) - {"_status", "visibility"})
                if unknown_omissions:
                    actions.append(_action(export, "conflict", "property-removal-baseline-values-required", remote,
                                           fields_requiring_review=unknown_omissions))
                    continue
            if state == "published" and local_changed and removed_fields:
                actions.append(_action(export, "conflict", "property-removal-requires-review", remote,
                                       fields_requiring_review=removed_fields))
                continue
            if state in {"withdrawn", "deleted"}:
                fields = {"visibility": "private", "_status": "draft"} if collection == "works" else {"_status": "draft"}
                already_private = _matches(remote, fields, collection)
                if remote_changed and not already_private:
                    item = _action(export, "conflict", "remote-changed-before-withdrawal", remote)
                elif state == "deleted":
                    item = _action(export, "unchanged" if already_private else "unpublish",
                                   "confirmed-delete-withdrawal-observed" if already_private else "confirmed-delete-unpublish-first",
                                   remote, payload={} if already_private else fields, retain_history=True,
                                   permanent_delete_requires_review=True, review_action="delete-review")
                elif already_private:
                    item = _action(export, "unchanged", "withdrawal-observed", remote)
                else:
                    item = _action(export, "unpublish", "explicit-durable-withdrawal", remote, payload=fields)
            elif remote_changed and local_changed:
                item = _action(export, "conflict", "local-and-remote-changed", remote)
            elif remote_changed:
                item = _action(export, "reverse-change", "remote-only-change-to-library-draft", remote,
                               library_draft=_draft(export, remote), publishes=False,
                               website_only_fields_requiring_review={
                                   name: deepcopy(remote[name]) for name in ("frontmatter", "protectedContent")
                                   if collection == "posts" and name in remote})
            elif local_changed:
                # Only send changed selected fields. Absent keys are not resets.
                fields = {key: deepcopy(value) for key, value in export["fields"].items()
                          if not _contains(_project(remote, collection).get(key), value) or key not in remote}
                if not fields and prior_fields is None:
                    actions.append(_action(export, "conflict", "baseline-values-required-for-empty-update", remote))
                    continue
                # Never erase existing Payload array-row identities by accident.
                risky_arrays = [name for name in ROW_FIELDS if name in fields
                                and any(row.get("id") for row in (remote.get(name) or []))
                                and (not fields[name] or not all(row.get("id") for row in fields[name]))]
                if risky_arrays:
                    item = _action(export, "conflict", "array-row-identity-review", remote,
                                   fields_requiring_review=sorted(risky_arrays))
                else:
                    item = _action(export, "update", "new-explicit-selected-publication", remote, payload=fields)
            else:
                item = _action(export, "unchanged", "selected-publication-and-remote-unchanged", remote)
        blockers = []
        if item["action"] in {"create", "update"} and collection == "works" and not capabilities.get("works_drafts_and_versions", False):
            blockers.append("works_drafts_and_versions_and_client_publisher_support_required")
        if item["payload"] and item["remote_id"] is not None and not item["expected_remote_version"]:
            blockers.append("remote_updatedAt_required")
        # No operation is applied by this module. Capability checks describe
        # whether an eventual executor may consider the proposed mutation.
        item["blocked_by"] = blockers
        item["ready_for_executor"] = bool(item["payload"]) and not blockers and item["action"] in {"create", "update", "unpublish"}
        actions.append(item)
    # A missing local file may mean rename, offline sync, or an unmounted Vault.
    # Keep mappings/history and never infer a tombstone from absence.
    # Imported drafts deliberately have no selected publication. Their absence
    # here is expected and says nothing about the visible local working file.
    missing = sorted(note_id for note_id in set(baselines) - {item["id"] for item in exports}
                     if baselines[note_id].get("state") != "imported-draft")
    return {"schema_version": 1, "applied": False, "mode": "offline-plan-only",
            "actions": actions, "missing_local_ids": missing,
            "warnings": ["Missing local/remote records never authorize deletion.",
                         "No Payload writes or library draft writes have been performed."]}


def _observed_fields(remote, collection):
    from .merge import remote_fields
    return remote_fields(remote, collection)


def observed_baseline(publication: Mapping[str, Any], remote: Mapping[str, Any]) -> dict[str, Any]:
    """Build state from a successful read-back or explicitly reviewed adoption.

    This is intentionally separate from planning. The caller must persist this
    receipt only after verifying the actual website result; a plan alone cannot
    supply a valid observed record. All selected fields must match for publish.
    """
    export = export_publication(publication)
    if remote.get("id") is None or not remote.get("updatedAt"):
        raise ValueError("successful observation requires remote id and updatedAt")
    if not all(remote.get(key) == value for key, value in export["identity"].items()):
        raise ValueError("observed remote identity does not match selected publication")
    if publication["state"] == "published":
        expected = export["fields"]
    else:
        expected = {"visibility": "private", "_status": "draft"} if export["collection"] == "works" else {"_status": "draft"}
    if not _matches(remote, expected, export["collection"]):
        raise ValueError("remote observation does not match the selected publication or withdrawal")
    return {"id": export["id"], "collection": export["collection"], "remote_id": remote["id"],
            "local_hash": export["content_hash"], "remote_hash": _remote_hash(remote, export["collection"]),
            "remote_version": remote["updatedAt"], "state": publication["state"],
            "identity": deepcopy(export["identity"]), "evidence": "observed-matching-remote",
            "local_fields": deepcopy(export["fields"]), "remote_fields": _observed_fields(remote, export["collection"])}


def observed_imported_baseline(publication: Mapping[str, Any], remote: Mapping[str, Any], *,
                               allow_published_observation: bool = False) -> dict[str, Any]:
    """Validate a nonpublic import observation without asserting publication.

    The caller must additionally verify bootstrap provenance, original file
    hash, and independent latest/base observations before retaining this receipt.
    """
    export = export_publication(publication)
    collection = export["collection"]
    if remote.get("id") is None or not remote.get("updatedAt"):
        raise ValueError("import observation requires remote id and updatedAt")
    if remote.get("_status") not in {"draft", "published"}:
        raise ValueError("import observation requires a known draft status")
    if not (allow_published_observation or remote.get("_status") == "draft" or
            (collection == "works" and remote.get("visibility") == "private")):
        raise ValueError("published public records require explicit publication adoption")
    expected = deepcopy(export["fields"])
    expected["_status"] = remote["_status"]
    if collection == "works":
        if remote.get("visibility") not in {"private", "public"}:
            raise ValueError("imported Work requires known visibility")
        expected["visibility"] = remote["visibility"]
    if not _matches(remote, expected, collection):
        raise ValueError("import observation does not match all local mapped fields")
    if not all(remote.get(key) == value for key, value in export["identity"].items()):
        raise ValueError("imported remote identity differs")
    return {"id": export["id"], "collection": collection, "remote_id": remote["id"],
            "local_hash": export["content_hash"], "remote_hash": _remote_hash(remote, collection),
            "remote_version": remote["updatedAt"], "state": "imported-draft",
            "identity": deepcopy(export["identity"]), "evidence": "observed-matching-bootstrap-import",
            "observed_status": remote["_status"], "observed_visibility": remote.get("visibility"),
            "local_fields": deepcopy(expected), "remote_fields": _observed_fields(remote, collection)}
