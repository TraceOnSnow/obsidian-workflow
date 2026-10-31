"""Explicit one-shot Payload transport. No configuration or network at import.

Conditional bulk updates narrow races but are not a global transaction or a
server-enforced compare-and-swap. External Admin writers do not share our lock.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
import fcntl
import ipaddress
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import LibraryError, _hash, _read, _render, _split
from .sync import (POST_FIELDS, WORK_FIELDS, _action, _matches, _project, _remote_hash, content_hash,
                   export_publication, observed_baseline, observed_imported_baseline, plan_sync)


class TransportError(LibraryError):
    """A sanitized network or protocol error; response bodies are never included."""


class UncertainWrite(TransportError):
    """A mutation failed without a verified outcome. Do not blindly retry."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


class PayloadClient:
    def __init__(self, base_url, api_key, timeout=20, *, allow_loopback_http=False, opener=None):
        parsed = urlsplit(base_url)
        try:
            loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and allow_loopback_http and loopback))):
            raise TransportError("Expected HTTPS origin, or explicitly enabled loopback HTTP origin; no URL credentials or extra path")
        if not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key or "\r" in api_key:
            raise TransportError("An explicit API key is required")
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise TransportError("Timeout must be between 0 and 300 seconds")
        self.target = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self._opener = opener or build_opener(_NoRedirect())

    @staticmethod
    def _collection(collection):
        if collection not in {"works", "posts"}:
            raise TransportError("Unsupported Payload collection")
        return collection

    def _request(self, method, path, query=None, body=None):
        url = self.target + path + ("?" + urlencode(query) if query else "")
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8") if body is not None else None
        request = Request(url, data=encoded, method=method, headers={
            "Authorization": "content-clients API-Key " + self._api_key,
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "markdown-library/0.1",
        })
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("Expected object")
            return result
        except Exception as exc:
            # Never echo exception URLs, request headers, or server body text.
            message = f"Payload HTTP {exc.code}" if isinstance(exc, HTTPError) else "Payload request failed or returned invalid JSON"
            error = TransportError if method == "GET" else UncertainWrite
            raise error(message) from None

    def list_collection(self, collection, *, draft=True, trash=False):
        self._collection(collection)
        documents, page, seen_pages = [], 1, set()
        while True:
            if page in seen_pages or len(seen_pages) >= 10000:
                raise TransportError("Payload pagination did not terminate")
            seen_pages.add(page)
            response = self._request("GET", f"/api/{collection}", {
                "draft": str(draft).lower(), "depth": 0, "limit": 100, "page": page, "sort": "id",
                **({"trash": "true"} if trash else {}),
            })
            rows = response.get("docs")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise TransportError("Invalid Payload collection response")
            documents.extend(rows)
            if not response.get("hasNextPage"):
                if response.get("totalDocs", len(documents)) != len(documents):
                    raise TransportError("Payload pagination is incomplete")
                break
            following = response.get("nextPage", page + 1)
            if not isinstance(following, int) or following <= page:
                raise TransportError("Invalid Payload next page")
            page = following
        ids = [str(row.get("id")) for row in documents]
        if any(row.get("id") is None for row in documents) or len(ids) != len(set(ids)):
            raise TransportError("Payload pagination returned missing or duplicate IDs")
        return documents

    def get(self, collection, remote_id, *, draft=True, trash=False):
        self._collection(collection)
        return self._request("GET", f"/api/{collection}/{quote(str(remote_id), safe='')}",
                             {"draft": str(draft).lower(), "depth": 0, **({"trash": "true"} if trash else {})})

    def create(self, collection, fields):
        self._collection(collection)
        return self._request("POST", f"/api/{collection}", {"draft": "true", "depth": 0}, fields)

    def update(self, collection, remote_id, fields, *, expected_version, draft, trash=False):
        self._collection(collection)
        if not expected_version:
            raise TransportError("A remote updatedAt version is required")
        response = self._request("PATCH", f"/api/{collection}", {
            "where[and][0][id][equals]": remote_id,
            "where[and][1][updatedAt][equals]": expected_version,
            "draft": str(draft).lower(), "depth": 0, **({"trash": "true"} if trash else {}),
        }, fields)
        rows = response.get("docs")
        if not isinstance(rows, list) or len(rows) != 1 or str(rows[0].get("id")) != str(remote_id) or response.get("errors"):
            raise UncertainWrite("Payload conditional update did not confirm exactly one record")
        return rows[0]


class _Store:
    def __init__(self, library, target):
        self.library, self.target = library, target
        self.relative = ".markdown-library/sync-state.json"

    @contextmanager
    def locked(self):
        self.library.validate()
        filename = self.library._path(".markdown-library/sync.lock")
        fd = os.open(filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield self

    def load(self):
        filename = self.library._path(self.relative)
        if not filename.exists():
            return {"schema_version": 1, "rating_scale": 100, "target": self.target, "baselines": {}, "pending": {}, "reverse_imports": {}}
        try:
            state = json.loads(filename.read_bytes().decode("utf-8"))
            if (not isinstance(state, dict) or state.get("schema_version") != 1 or state.get("target") != self.target
                    or any(not isinstance(state.get(key), dict) for key in ("baselines", "pending", "reverse_imports"))
                    or any(key in state and not isinstance(state[key], dict) for key in ("lifecycle", "lifecycle_pending"))):
                raise ValueError()
            return state
        except (ValueError, UnicodeError):
            raise TransportError("Invalid sync state or changed target; preserve existing state and review explicitly") from None

    def save(self, state):
        state.setdefault("rating_scale", 100)
        self.library._atomic(self.relative, json.dumps(state, ensure_ascii=False, allow_nan=False, indent=2))


def _identity_matches(row, identity):
    return all(row.get(key) == value for key, value in identity.items())


def _draft_fields(fields, collection, *, creating=False):
    result = deepcopy(fields)
    result["_status"] = "draft"
    if collection == "works" and creating:
        result["visibility"] = "private"
    return result


def _fresh(client, action):
    latest = client.get(action["collection"], action["remote_id"], draft=True)
    if (latest.get("updatedAt") != action["expected_remote_version"]
            or _remote_hash(latest, action["collection"]) != action["remote_hash"]):
        raise TransportError("Remote changed after planning; generate a new plan")
    return latest


def _selected_unchanged(library, publication):
    current = next((row for row in library.publications() if row["id"] == publication["id"]), None)
    original_hash = publication.get("_source_record_hash", content_hash(publication))
    if content_hash(current) != original_hash:
        raise TransportError("Selected publication changed during sync; generate a new plan")


def _resolve_relationships(publication, baselines):
    resolved = deepcopy(publication)
    resolved["_source_record_hash"] = content_hash(publication)
    if publication["kind"] == "work":
        properties = resolved["snapshot"]["properties"]
        article = properties.get("article")
        if isinstance(article, str) and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", article):
            baseline = baselines.get(article)
            withdrawing = publication["state"] in {"withdrawn", "deleted"}
            if (not baseline or baseline.get("collection") != "posts"
                    or (not withdrawing and baseline.get("state") != "published")):
                raise TransportError("Linked library Post requires an observed published website baseline")
            properties["article"] = baseline["remote_id"]
    return resolved


def _verify_both(client, publication, remote_id):
    collection = export_publication(publication)["collection"]
    latest = client.get(collection, remote_id, draft=True)
    base = client.get(collection, remote_id, draft=False)
    required_status = "published" if publication["state"] == "published" else "draft"
    if latest.get("_status") != required_status or base.get("_status") != required_status:
        raise TransportError("Independent draft and base publication states do not match")
    if collection == "posts" and (latest.get("protectedContent") or base.get("protectedContent")):
        raise TransportError("Protected remote post requires a supported deliberate publication flow")
    receipt = observed_baseline(publication, latest)
    observed_baseline(publication, base)
    return receipt


def _stage_matches(row, expected, collection):
    fields = deepcopy(expected)
    required = fields.pop("_status", None)
    return row.get("_status") == required and _matches(row, fields, collection)


def _publish_draft(library, client, publication, pending):
    export = export_publication(publication)
    collection, remote_id = export["collection"], pending["remote_id"]
    latest = client.get(collection, remote_id, draft=True)
    if latest.get("updatedAt") != pending["draft_version"] or _remote_hash(latest, collection) != pending["draft_hash"]:
        raise TransportError("Saved draft changed before publication; reconciliation required")
    expected = _draft_fields(export["fields"], collection, creating=pending.get("creating", False))
    if not _stage_matches(latest, expected, collection):
        raise TransportError("Saved draft does not match selected fields")
    payload = _project(latest, collection)
    payload.update({"_status": "published"})
    if collection == "works":
        payload["visibility"] = "public"
    else:
        # Reject retained unsupported protection/theme metadata before publication.
        export_publication({"id": publication["id"], "kind": "post", "state": "published",
                            "snapshot": {"title": payload.get("title"), "markdown": payload.get("markdown", ""),
                                         "properties": {**payload, "protectedContent": latest.get("protectedContent")}}})
    base = client.get(collection, remote_id, draft=False)
    confirmed = client.get(collection, remote_id, draft=True)
    if confirmed.get("updatedAt") != latest.get("updatedAt") or _remote_hash(confirmed, collection) != _remote_hash(latest, collection):
        raise TransportError("Draft changed while checking underlying base version")
    _selected_unchanged(library, publication)
    client.update(collection, remote_id, payload, expected_version=base.get("updatedAt"), draft=False)
    return _verify_both(client, publication, remote_id)


def _reverse_draft(library, publication, action, *, expected_hash=None):
    current = library.get(publication["id"])
    expected_hash = expected_hash or publication["sha256"]
    if current["sha256"] != expected_hash:
        raise TransportError("Current local draft differs from selected publication; review or merge remote changes")
    draft = action["library_draft"]
    properties, private_body = _split(current["markdown"])
    managed = WORK_FIELDS if publication["kind"] == "work" else (POST_FIELDS | {"protectedContent"})
    for field in managed | {"payload_observed_status", "payload_markdown_is_null"}:
        properties.pop(field, None)
    properties.update(deepcopy(draft["properties"]))
    properties["title"] = draft["title"]
    # Website-only protection metadata is retained privately in the local draft.
    properties.update(deepcopy(action.get("website_only_fields_requiring_review", {})))
    body = draft["markdown"] if publication["kind"] == "post" else private_body
    extra = {}
    if publication['kind'] == 'work':
        from .work_profile import profile, WEBSITE_ONLY
        retained = profile(library, current['id'])
        if retained is not None:
            # Legacy reverse-change path must honor the same compact boundary
            # as field-level resolution; metadata never returns to visible YAML.
            retained = {key: value for key, value in retained.items()
                        if key not in WORK_FIELDS}
            retained.update({key: properties.pop(key) for key in list(properties) if key in WEBSITE_ONLY})
            extra['website_metadata'] = retained
    return library.replace(current["id"], _render(properties, body), expected_hash=expected_hash, **extra)


def _bootstrap_claims(library, receipt_state):
    claims = []
    for entry in library._read_state()["sources"].values():
        source = entry.get("source", {})
        if source.get("type") == "payload-bootstrap":
            claims.append({**source, "id": entry["id"]})
    claims.extend(receipt_state["imports"].values())
    return claims


def _adoption_source_matches(library, receipt_state, target, note_id, collection, remote_id):
    from .bootstrap import _target
    for claim in _bootstrap_claims(library, receipt_state):
        origin = _target(claim.get("target"))
        same_document = claim.get("id") == note_id
        same_remote = (origin == target and claim.get("collection") == collection
                       and str(claim.get("remote_id")) == str(remote_id))
        if same_document and not same_remote:
            raise TransportError("Bootstrap source target, collection or remote ID differs; adoption rejected")
        if same_remote and not same_document:
            raise TransportError("This remote record is already claimed by another imported library document")


def adopt_existing(library, client, note_ids, *, apply=False):
    """Explicitly map only selected matching publications to existing records.

    This method never selects a publication or calls a remote mutation. Preview
    can independently verify observations but only apply stores local baselines.
    Existing baselines/pending receipts are never overwritten by adoption.
    """
    from .bootstrap import _Receipts, _target
    if isinstance(note_ids, str):
        note_ids = [note_ids]
    requested = list(dict.fromkeys(note_ids))
    if not requested or any(not isinstance(note_id, str) or not note_id for note_id in requested):
        raise TransportError("Adoption requires explicit nonempty library IDs")
    store = _Store(library, client.target)
    bootstrap_receipts = _Receipts(library)
    with store.locked(), bootstrap_receipts.lock(apply):
        state = store.load()
        receipts = bootstrap_receipts.load()
        origin = _target(client.target)
        selected = {record["id"]: record for record in library.publications()}
        records = {collection: client.list_collection(collection, draft=True) for collection in ("posts", "works")}
        results = []
        comparison = deepcopy(state["baselines"])
        # An explicitly requested Post must be observed before its Work relation.
        ordered = sorted(requested, key=lambda note_id: (selected.get(note_id, {}).get("kind") != "post", note_id))
        for note_id in ordered:
            result = {"id": note_id, "status": "rejected", "remote_write": False, "baseline_saved": False}
            try:
                if note_id in state["pending"]:
                    raise TransportError("Pending write outcome requires reconciliation; adoption cannot replace it")
                if note_id in state["baselines"]:
                    raise TransportError("An existing baseline must be reconciled normally; adoption cannot overwrite it")
                publication = selected.get(note_id)
                if not publication or publication["state"] != "published":
                    raise TransportError("No explicitly selected published snapshot exists for this library ID")
                resolved = _resolve_relationships(publication, comparison)
                export = export_publication(resolved)
                collection = export["collection"]
                candidates = [row for row in records[collection] if _identity_matches(row, export["identity"])]
                if len(candidates) != 1:
                    raise TransportError("Remote identity is missing or ambiguous; adoption requires exactly one candidate")
                remote_id = candidates[0].get("id")
                if remote_id is None:
                    raise TransportError("Remote candidate lacks an ID")
                for other_id, other in selected.items():
                    if other_id == note_id or other["state"] != "published" or other["kind"] != publication["kind"]:
                        continue
                    try:
                        other_export = export_publication(other)
                    except ValueError:
                        continue
                    if other_export["identity"] == export["identity"]:
                        raise TransportError("Another selected library publication claims the same website identity")
                if any(base.get("collection") == collection and str(base.get("remote_id")) == str(remote_id)
                       for base in comparison.values()):
                    raise TransportError("Another library baseline already claims this remote record")
                for other_id, pending in state["pending"].items():
                    pending_identity = pending.get("identity", {})
                    pending_collection = pending.get("collection") or ("works" if "slug" in pending_identity else "posts")
                    if pending_collection == collection and (str(pending.get("remote_id")) == str(remote_id)
                                                            or pending_identity == export["identity"]):
                        raise TransportError("Another pending write already claims this remote record")
                _adoption_source_matches(library, receipts, origin, note_id, collection, remote_id)
                latest = client.get(collection, remote_id, draft=True)
                base = client.get(collection, remote_id, draft=False)
                if (str(latest.get("id")) != str(remote_id) or str(base.get("id")) != str(remote_id)
                        or not latest.get("updatedAt") or not base.get("updatedAt")):
                    raise TransportError("Independent observations lack the expected remote ID or version")
                if latest.get("_status") != "published" or base.get("_status") != "published":
                    raise TransportError("Independent latest and base must both already be published")
                if _remote_hash(latest, collection) != _remote_hash(base, collection):
                    raise TransportError("Remote latest and base contain different mapped fields")
                for observed in (latest, base):
                    projected = _project(observed, collection)
                    if any(key not in projected or projected[key] != value for key, value in export["fields"].items()):
                        raise TransportError("Selected fields do not exactly match the observed remote content")
                if collection == "posts":
                    # A protection flag can be stale; inspect raw theme/body too.
                    for row in (latest, base):
                        export_publication({"id": note_id, "kind": "post", "state": "published",
                                            "snapshot": {"title": row.get("title"), "markdown": row.get("markdown", ""),
                                                         "properties": row}})
                baseline = observed_baseline(resolved, latest)
                observed_baseline(resolved, base)
                _selected_unchanged(library, resolved)
                if apply:
                    # Library/source identity cannot change silently during review.
                    _adoption_source_matches(library, bootstrap_receipts.load(), origin, note_id, collection, remote_id)
                    state["baselines"][note_id] = baseline
                    state["reverse_imports"].pop(note_id, None)
                    store.save(state)
                comparison[note_id] = baseline
                result.update({"status": "adopted" if apply else "adoptable", "collection": collection,
                               "remote_id": remote_id, "baseline_saved": apply})
            except (ValueError, LibraryError) as exc:
                result["reason"] = str(exc) if isinstance(exc, TransportError) else "Selected or observed content failed adoption validation"
            results.append(result)
        return {"schema_version": 1, "mode": "adopt-local-baseline" if apply else "adoption-preview",
                "remote_writes": False, "publication_selected": False, "results": results}


def map_imported(library, client, note_ids, *, apply=False):
    """Observe explicitly requested unchanged bootstrap drafts; never publish.

    Only apply writes local mapping receipts. Future publication still requires
    an explicit selected snapshot and the ordinary relationship/publish checks.
    """
    from .bootstrap import (_Receipts, _index, _linked, _markdown, _mappings,
                            _shape, _target, source_id)
    requested = [note_ids] if isinstance(note_ids, str) else list(note_ids)
    if not requested or any(not isinstance(value, str) or not value for value in requested):
        raise TransportError("Imported mapping requires explicit nonempty library IDs")
    requested = list(dict.fromkeys(requested))
    library.validate()
    store, receipts_store = _Store(library, client.target), _Receipts(library)
    with (store.locked() if apply else nullcontext()), receipts_store.lock(apply), \
            (library._lock() if apply else nullcontext()):
        state, receipts = store.load(), receipts_store.load()
        library_state, notes = _index(library)
        origin = _target(client.target)
        mappings = _mappings(library, origin, library_state, notes, receipts)
        post_ids = {}
        for claim in _bootstrap_claims(library, receipts):
            if _target(claim.get("target")) == origin and claim.get("collection") == "posts":
                post_ids[str(claim["remote_id"])] = claim["id"]
        remote_records = {collection: client.list_collection(collection, draft=True)
                          for collection in ("posts", "works")}
        results = []
        for note_id in requested:
            result = {"id": note_id, "status": "rejected", "remote_write": False,
                      "baseline_saved": False, "publication_selected": False}
            try:
                if note_id in state["pending"] or note_id in state["baselines"]:
                    raise TransportError("Existing baseline or pending write must be reconciled normally; mapping cannot overwrite it")
                if note_id in library_state["publications"]:
                    raise TransportError("Selected publication history requires the ordinary adoption or reconciliation flow")
                note = notes.get(note_id)
                if note is None or library_state["notes"].get(note_id, {}).get("deleted"):
                    raise TransportError("Imported current draft is missing or deleted")
                sources = [(key, entry) for key, entry in library_state["sources"].items()
                           if entry.get("id") == note_id and entry.get("source", {}).get("type") == "payload-bootstrap"]
                if len(sources) != 1:
                    raise TransportError("Exactly one original bootstrap source is required")
                key, entry = sources[0]
                source = entry["source"]
                collection, remote_id = source.get("collection"), source.get("remote_id")
                if collection not in {"works", "posts"} or remote_id is None:
                    raise TransportError("Bootstrap source lacks a supported remote identity")
                expected_key = source_id(origin, collection, remote_id)
                receipt = receipts["imports"].get(expected_key)
                if (key != expected_key or _target(source.get("target")) != origin
                        or mappings.get(expected_key) != note_id or not receipt
                        or receipt.get("id") != note_id or receipt.get("target") != origin
                        or receipt.get("collection") != collection or str(receipt.get("remote_id")) != str(remote_id)):
                    raise TransportError("Bootstrap source and receipt target or remote identity differ")
                _adoption_source_matches(library, receipts, origin, note_id, collection, remote_id)
                if note["sha256"] != receipt.get("imported_sha256"):
                    raise TransportError("Local current draft differs from the original bootstrap import")
                if note["properties"]["kind"] != ("work" if collection == "works" else "post"):
                    raise TransportError("Imported current draft kind differs from source collection")
                latest = client.get(collection, remote_id, draft=True)
                base = client.get(collection, remote_id, draft=False)
                source_version = source.get("source_updated_at")
                if (not source_version or any(str(row.get("id")) != str(remote_id)
                        or row.get("updatedAt") != source_version for row in (latest, base))):
                    raise TransportError("Independent latest/base identity or version differs from the original import")
                source_hash = source.get("source_hash")
                if (not source_hash or receipt.get("imported_source_hash") != source_hash
                        or any(_shape(row, collection)["source_hash"] != source_hash for row in (latest, base))
                        or _remote_hash(latest, collection) != _remote_hash(base, collection)):
                    raise TransportError("Independent latest/base content or status differs from the original import")
                shape = _linked(_shape(latest, collection), post_ids)
                if _hash(_markdown(shape, note["properties"]["kind"], note_id)) != note["sha256"]:
                    raise TransportError("Imported current draft does not exactly match bootstrap field and relationship mapping")
                # This temporary validation value is never selected or persisted
                # as a publication. Draft relationships use proven source IDs;
                # _resolve_relationships still guards the real publication flow.
                properties = deepcopy(note["properties"])
                if collection == "works" and properties.get("article") is not None:
                    reverse_posts = {value: remote for remote, value in post_ids.items()}
                    article = properties["article"]
                    if not isinstance(article, str) or article not in reverse_posts:
                        raise TransportError("Imported Work relationship requires a proven bootstrap Post library identity")
                    remote_article = latest.get("article")
                    if isinstance(remote_article, dict):
                        remote_article = remote_article.get("id")
                    if str(remote_article) != reverse_posts[article]:
                        raise TransportError("Imported Work relationship source differs from observed remote ID")
                    properties["article"] = remote_article
                publication = {"id": note_id, "kind": note["properties"]["kind"], "state": "published",
                               "snapshot": {"id": note_id, "kind": note["properties"]["kind"],
                                            "title": properties["title"], "properties": properties,
                                            "markdown": _split(note["markdown"])[1]}}
                export = export_publication(publication)
                candidates = [row for row in remote_records[collection] if _identity_matches(row, export["identity"])]
                if len(candidates) != 1 or str(candidates[0].get("id")) != str(remote_id):
                    raise TransportError("Imported website identity is missing, ambiguous, or belongs to another record")
                for other_id, selected in library_state["publications"].items():
                    if other_id != note_id and selected.get("kind") == publication["kind"]:
                        if export_publication(selected)["identity"] == export["identity"]:
                            raise TransportError("Another selected library publication claims this website identity")
                if any(other.get("collection") == collection and str(other.get("remote_id")) == str(remote_id)
                       for other in state["baselines"].values()):
                    raise TransportError("Another baseline already claims this remote record")
                for pending in state["pending"].values():
                    identity = pending.get("identity", {})
                    pending_collection = pending.get("collection") or ("works" if "slug" in identity else "posts")
                    if pending_collection == collection and (str(pending.get("remote_id")) == str(remote_id)
                                                            or identity == export["identity"]):
                        raise TransportError("A pending write already claims this remote record")
                baseline = observed_imported_baseline(publication, latest)
                observed_imported_baseline(publication, base)
                baseline.update({"imported_sha256": note["sha256"], "bootstrap_source_id": key,
                                 "imported_source_hash": source_hash})
                library._assert_note_storage(note)
                if apply:
                    state["baselines"][note_id] = baseline
                    state["reverse_imports"].pop(note_id, None)
                    store.save(state)
                result.update({"status": "mapped-imported-draft" if apply else "mappable-imported-draft",
                               "collection": collection, "remote_id": remote_id,
                               "baseline_saved": apply, "mapping_state": "imported-draft"})
            except (ValueError, LibraryError) as exc:
                result["reason"] = str(exc) if isinstance(exc, TransportError) else "Imported or observed content failed mapping validation"
            results.append(result)
        return {"schema_version": 1, "mode": "map-imported-local" if apply else "imported-mapping-preview",
                "remote_writes": False, "publication_selected": False, "results": results}


def _sync_imported_drafts(library, client, store, state, records, selected_ids, *, apply, accept_remote, only_ids=None):
    """Merge received draft fields without changing publication selection."""
    from .bootstrap import _index
    from .review import prepare_review, accept_review, _check_identity, _redact
    _, notes = _index(library)
    actions, results = [], []
    for note_id, baseline in sorted(state["baselines"].items()):
        if only_ids is not None and note_id not in only_ids:
            continue
        if baseline.get("state") != "imported-draft" or note_id in selected_ids:
            continue
        collection = baseline["collection"]
        remote = next((row for row in records[collection] if str(row["id"]) == str(baseline["remote_id"])), None)
        export = {"id": note_id, "collection": collection, "content_hash": baseline["local_hash"], "identity": baseline["identity"]}
        action = _action(export, "unchanged", "imported-draft-and-remote-unchanged", remote, publishes=False, imported_draft=True)
        result = {"id": note_id, "status": "unchanged", "applied": False, "remote_write": False}
        try:
            current = notes.get(note_id)
            if not current or remote is None:
                raise TransportError("Mapped local or remote draft is missing; no deletion inferred")
            if note_id in state["pending"]:
                raise TransportError("Pending write requires reconciliation before importing a draft")
            accepted = state["reverse_imports"].get(note_id, {})
            local_changed = current["sha256"] != accepted.get("local_hash", baseline.get("draft_sha256", baseline.get("imported_sha256")))
            remote_changed = _remote_hash(remote, collection) != accepted.get("remote_hash", baseline["remote_hash"])
            if remote_changed:
                latest = _fresh(client, action)
                formal = client.get(collection, baseline["remote_id"], draft=False)
                _check_identity(baseline, latest)
                _check_identity(baseline, formal)
                item = prepare_review(library, state, current, latest, formal=formal)
                action["review"] = _redact(item, False)
                if item["merge"]["conflicts"]:
                    action.update({"action": "conflict", "reason": "field-merge-requires-review"})
                    result.update({"status": "conflict", "reason": action["reason"], "review": action["review"]})
                else:
                    action.update({"action": "reverse-change", "reason": "remote-fields-merge-to-current-draft"})
                    result["status"] = "remote-draft-available"
                    if apply and accept_remote:
                        _fresh(client, action)
                        if note_id in library._read_state()["publications"]:
                            raise TransportError("Publication was selected during observation; replan")
                        result = accept_review(library, store, state, current, latest, item, apply=True)
            elif local_changed:
                action.update({"action": "local-draft", "reason": "local-draft-kept-without-publication-selection"})
                result["status"] = "local-draft"
        except (ValueError, LibraryError) as exc:
            action.update({"action": "conflict", "reason": "imported-draft-observation-requires-review"})
            result.update({"status": "conflict", "reason": str(exc) if isinstance(exc, TransportError) else "Imported draft requires review"})
        actions.append(action)
        results.append(result)
    return actions, results


def sync_once(library, client, *, apply=False, works_drafts_and_versions=False,
              accept_remote=False, resume_pending=(), only_ids=None):
    """Read/plan by default. Explicit apply executes only safe planned actions.

    Pending uncertain creates are reconciled by stable identity, never re-POSTed.
    Continuing a discovered unpublished draft requires its explicit library ID
    in resume_pending. Reverse changes require accept_remote as well as apply.
    only_ids limits publication, lifecycle and imported-draft actions together;
    complete remote records/baselines remain available for identity checks.
    """
    if only_ids is not None:
        if (not isinstance(only_ids, (list, tuple, set, frozenset)) or not only_ids
                or any(not isinstance(value, str) or not value for value in only_ids)):
            raise TransportError("only_ids must be a nonempty collection of library IDs")
        only_ids = frozenset(only_ids)
        if only_ids - library._read_state()["notes"].keys():
            raise TransportError("Sync scope contains unknown library IDs")
        if set(resume_pending) - only_ids:
            raise TransportError("Pending resumes must be inside the sync scope")
    store = _Store(library, client.target)
    with store.locked():
        state = store.load()
        publications = library.publications()
        if only_ids is not None:
            publications = [row for row in publications if row["id"] in only_ids]
        from .lifecycle import sync_lifecycle, _call
        records = {name: _call(client.list_collection, name, draft=True, trash=True) for name in ("works", "posts")}
        lifecycle_actions, lifecycle_results, handled_ids = sync_lifecycle(
            library, client, store, state, records, apply=apply, accept_remote=accept_remote, only_ids=only_ids)
        publications = [row for row in publications if row["id"] not in handled_ids]
        records = {name: [row for row in rows if not row.get("deletedAt")] for name, rows in records.items()}
        planning_baselines = {key: value for key, value in state["baselines"].items() if key not in handled_ids}
        from .bootstrap import _index
        library_state, indexed_notes = _index(library)
        # Legacy receipts may recover exact ancestor values from retained local
        # objects. Hydration here is in-memory; an offline plan never invents or
        # saves missing ancestor values merely because the latest fields match.
        from .merge import recover_base
        for publication in publications:
            note_id = publication["id"]
            baseline = planning_baselines.get(note_id)
            if (baseline and baseline.get("local_fields") is None and publication["state"] == "published"
                    and export_publication(publication)["content_hash"] != baseline.get("local_hash")):
                left, right, evidence = recover_base(library, note_id, baseline, None, state["baselines"],
                                                     library_state=library_state, notes=indexed_notes)
                if left is not None:
                    planning_baselines[note_id] = {**baseline, "local_fields": left, "remote_fields": right,
                                                   "ancestor_evidence": evidence}
        plan = plan_sync(publications, planning_baselines, records,
                         capabilities={"works_drafts_and_versions": works_drafts_and_versions})
        results = list(lifecycle_results)
        by_id = {row["id"]: row for row in publications}
        # Post observations make their numeric Payload IDs available to Works in
        # the same batch. Never send a library UUID as a Payload relationship.
        ordered = sorted(list(enumerate(plan["actions"])), key=lambda pair: (by_id[pair[1]["id"]]["kind"] != "post", pair[1]["id"]))
        for action_index, action in ordered:
            note_id = action["id"]
            try:
                publication = _resolve_relationships(by_id[note_id], state["baselines"])
            except TransportError:
                results.append({"id": note_id, "status": "blocked", "applied": False,
                                "reason": "Linked library Post has no observed published website ID"})
                continue
            action = plan_sync([publication], planning_baselines, records,
                               capabilities={"works_drafts_and_versions": works_drafts_and_versions})["actions"][0]
            plan["actions"][action_index] = action
            pending = state["pending"].get(note_id)
            if pending:
                export = export_publication(publication)
                candidates = [row for row in records[action["collection"]] if _identity_matches(row, pending["identity"])]
                result = {"id": note_id, "status": "reconciliation-required", "candidate_ids": [row["id"] for row in candidates], "applied": False}
                try:
                    if pending["publication_hash"] != export["content_hash"] or pending["state"] != publication["state"]:
                        raise TransportError("Publication changed while a previous write outcome remains pending")
                    if len(candidates) != 1:
                        raise TransportError("Pending identity is absent or ambiguous; no write retried")
                    remote_id = candidates[0]["id"]
                    try:
                        baseline = _verify_both(client, publication, remote_id)
                    except (ValueError, TransportError):
                        if not (apply and note_id in resume_pending and publication["state"] == "published"):
                            raise TransportError("Pending write not independently observed as published; review and explicitly resume this ID") from None
                        if action["collection"] == "works" and not works_drafts_and_versions:
                            raise TransportError("Works draft/version capability is required")
                        latest = client.get(action["collection"], remote_id, draft=True)
                        pending.update({"remote_id": remote_id, "draft_version": latest.get("updatedAt"), "draft_hash": _remote_hash(latest, action["collection"])})
                        store.save(state)
                        baseline = _publish_draft(library, client, publication, pending)
                    if apply:
                        state["baselines"][note_id] = baseline
                        state["reverse_imports"].pop(note_id, None)
                        del state["pending"][note_id]
                        store.save(state)
                    result.update({"status": "reconciled" if apply else "matching-observation", "remote_id": remote_id})
                except (ValueError, LibraryError) as exc:
                    # Do not expose remote body, exception URLs, credentials, or private prose.
                    result["reason"] = str(exc) if isinstance(exc, TransportError) else "Pending outcome requires review; no automatic write retry"
                results.append(result)
                continue
            can_converge = (note_id in state["baselines"] and publication["state"] == "published"
                            and (action["reason"] == "local-and-remote-changed"
                                 or (action["action"] == "update" and not action["payload"])))
            if can_converge:
                try:
                    _selected_unchanged(library, publication)
                    receipt = _verify_both(client, publication, action["remote_id"])
                    if apply:
                        state["baselines"][note_id] = receipt
                        state["reverse_imports"].pop(note_id, None)
                        store.save(state)
                    results.append({"id": note_id, "status": "reconciled" if apply else "converged-awaiting-apply",
                                    "applied": False, "remote_id": action["remote_id"]})
                    continue
                except (ValueError, LibraryError):
                    pass
            accepted = state["reverse_imports"].get(note_id)
            baseline = state["baselines"].get(note_id)
            comparison_hash = (accepted or {}).get("remote_hash", (baseline or {}).get("remote_hash"))
            if (baseline and publication["state"] == "published" and action["remote_id"] is not None
                    and action["remote_hash"] != comparison_hash
                    and action["reason"] not in {"website-identity-owned-by-another-record", "property-removal-requires-review", "baseline-values-required-for-empty-update", "property-removal-baseline-values-required"}):
                from .review import prepare_review, accept_review, _check_identity, _redact
                action.pop("library_draft", None)
                action.pop("website_only_fields_requiring_review", None)
                result = {"id": note_id, "status": "remote-draft-available", "applied": False}
                try:
                    current = indexed_notes.get(note_id)
                    if current is None:
                        raise TransportError("Mapped current draft is missing; no deletion inferred")
                    latest = _fresh(client, action)
                    formal = client.get(action["collection"], action["remote_id"], draft=False)
                    _check_identity(baseline, latest)
                    _check_identity(baseline, formal)
                    item = prepare_review(library, state, current, latest, formal=formal)
                    action.update({"action": "conflict" if item["merge"]["conflicts"] else "reverse-change",
                                   "reason": "field-merge-requires-review" if item["merge"]["conflicts"] else "remote-fields-merge-to-current-draft",
                                   "payload": {}, "ready_for_executor": False, "review": _redact(item, False)})
                    if item["merge"]["conflicts"]:
                        result.update({"status": "conflict", "review": action["review"]})
                    elif apply and accept_remote:
                        _fresh(client, action)
                        _selected_unchanged(library, publication)
                        result = accept_review(library, store, state, current, latest, item, apply=True)
                except (ValueError, LibraryError):
                    action.update({"action": "conflict", "reason": "draft-merge-observation-requires-review", "payload": {}, "ready_for_executor": False})
                    result.update({"status": "conflict", "reason": action["reason"]})
                plan["actions"][action_index] = action
                results.append(result)
                continue
            expected_current_hash = publication["sha256"]
            selection_hash = publication.get("_source_record_hash", content_hash(publication))
            baseline = state["baselines"].get(note_id, {})
            # A retained remote draft is an observation of current, independent
            # of the unchanged selected snapshot and successful publish baseline.
            # Older receipts predate selection_hash; their unchanged selection
            # can still be identified by the durable selected-content baseline.
            same_selection = accepted and publication["state"] == "published" and (
                accepted.get("selection_hash") == selection_hash
                or (not accepted.get("selection_hash")
                    and publication["state"] == baseline.get("state")
                    and export_publication(publication)["content_hash"] == baseline.get("local_hash")))
            if same_selection:
                try:
                    _selected_unchanged(library, publication)
                    current = indexed_notes.get(note_id)
                    if current is None:
                        raise TransportError("Mapped current draft is missing")
                    if accepted["remote_hash"] == action["remote_hash"]:
                        local_changed = current["sha256"] != accepted["local_hash"]
                        action.update({"action": "local-draft" if local_changed else "unchanged",
                                       "reason": "local-draft-after-remote-import" if local_changed else "accepted-remote-draft-retained",
                                       "payload": {}, "ready_for_executor": False})
                        plan["actions"][action_index] = action
                        results.append({"id": note_id, "status": action["action"], "applied": False,
                                        "reason": action["reason"]})
                        continue
                    if current["sha256"] != accepted["local_hash"]:
                        raise TransportError("Local and remote changed after the accepted remote draft")
                    comparison = deepcopy(planning_baselines)
                    comparison[note_id]["remote_hash"] = accepted["remote_hash"]
                    action = plan_sync([publication], comparison, records,
                                       capabilities={"works_drafts_and_versions": works_drafts_and_versions})["actions"][0]
                    plan["actions"][action_index] = action
                    expected_current_hash = accepted["local_hash"]
                except LibraryError:
                    action.update({"action": "conflict", "reason": "local-and-remote-changed-after-draft-import",
                                   "payload": {}, "ready_for_executor": False})
                    plan["actions"][action_index] = action
                    results.append({"id": note_id, "status": "conflict", "applied": False,
                                    "reason": action["reason"]})
                    continue
            if (action["reason"] == "local-and-remote-changed" and accepted
                    and accepted["remote_hash"] == action["remote_hash"]):
                # A reviewed remote draft is the comparison point for the next
                # explicit local publish, but is not a successful publication
                # baseline. Keep durable baseline unchanged until read-back.
                comparison = deepcopy(planning_baselines)
                comparison[note_id]["remote_hash"] = accepted["remote_hash"]
                action = plan_sync([publication], comparison, records,
                                   capabilities={"works_drafts_and_versions": works_drafts_and_versions})["actions"][0]
                plan["actions"][action_index] = action
            if action["action"] == "reverse-change":
                result = {"id": note_id, "status": "remote-draft-available", "applied": False}
                try:
                    current = library.get(note_id)
                    if current["sha256"] != expected_current_hash:
                        raise TransportError("Local draft changed")
                    if apply and accept_remote:
                        _fresh(client, action)
                        _selected_unchanged(library, publication)
                        imported = _reverse_draft(library, publication, action, expected_hash=expected_current_hash)
                        state["reverse_imports"][note_id] = {"remote_hash": action["remote_hash"],
                                                             "local_hash": imported["sha256"],
                                                             "selection_hash": selection_hash}
                        store.save(state)
                        result.update({"status": "remote-draft-imported", "applied": True})
                except LibraryError:
                    result.update({"status": "conflict", "reason": "Local draft or remote changed; merge required"})
                results.append(result)
                continue
            if action["action"] == "unchanged" and publication["state"] == "published":
                current = indexed_notes.get(note_id)
                if current is not None and current["sha256"] != publication["sha256"]:
                    action.update({"action": "local-draft", "reason": "current-draft-differs-selected-publication"})
                    plan["actions"][action_index] = action
            if not apply or not action["ready_for_executor"]:
                results.append({"id": note_id, "status": action["action"], "applied": False, "blocked_by": action["blocked_by"]})
                continue
            export = export_publication(publication)
            intent = {"operation_key": action["operation_key"], "publication_hash": export["content_hash"],
                      "identity": export["identity"], "state": publication["state"], "phase": "intent",
                      "creating": action["action"] == "create", "remote_id": action["remote_id"]}
            try:
                _selected_unchanged(library, publication)
                latest = _fresh(client, action) if action["remote_id"] is not None else None
                if latest is None:
                    if any(_identity_matches(row, export["identity"]) for row in client.list_collection(action["collection"], draft=True)):
                        raise TransportError("Identity appeared after planning; adoption review required")
                state["pending"][note_id] = intent
                store.save(state)  # Durable intent always precedes the first mutation.
                if action["action"] == "unpublish":
                    base = client.get(action["collection"], action["remote_id"], draft=False)
                    latest = _fresh(client, action)
                    payload = _project(latest, action["collection"])
                    payload.update(action["payload"])
                    payload["_status"] = "draft"
                    client.update(action["collection"], action["remote_id"], payload,
                                  expected_version=base.get("updatedAt"), draft=False)
                    baseline = _verify_both(client, publication, action["remote_id"])
                else:
                    draft_fields = _draft_fields(action["payload"], action["collection"], creating=intent["creating"])
                    if intent["creating"]:
                        created = client.create(action["collection"], draft_fields)
                        remote_id = created.get("doc", created).get("id")
                        if remote_id is None:
                            raise UncertainWrite("Create response lacks a remote ID")
                    else:
                        remote_id = action["remote_id"]
                        client.update(action["collection"], remote_id, draft_fields,
                                      expected_version=latest.get("updatedAt"), draft=True)
                    saved = client.get(action["collection"], remote_id, draft=True)
                    expected = _draft_fields(export["fields"], action["collection"], creating=intent["creating"])
                    if not _stage_matches(saved, expected, action["collection"]):
                        raise UncertainWrite("Independent saved draft does not match selected fields")
                    intent.update({"phase": "draft-saved", "remote_id": remote_id,
                                   "draft_version": saved.get("updatedAt"), "draft_hash": _remote_hash(saved, action["collection"])})
                    store.save(state)
                    baseline = _publish_draft(library, client, publication, intent)
                state["baselines"][note_id] = baseline
                state["reverse_imports"].pop(note_id, None)
                del state["pending"][note_id]
                store.save(state)
                results.append({"id": note_id, "status": "observed", "applied": True, "remote_id": baseline["remote_id"]})
            except (ValueError, LibraryError) as exc:
                if note_id in state["pending"]:
                    state["pending"][note_id]["phase"] = "reconciliation-required"
                    store.save(state)
                results.append({"id": note_id, "status": "reconciliation-required" if note_id in state["pending"] else "conflict",
                                "applied": False, "reason": str(exc) if isinstance(exc, TransportError) else "Local validation rejected completion; review remote state before continuing"})
        draft_actions, draft_results = _sync_imported_drafts(
            library, client, store, state, records, set(by_id) | handled_ids, apply=apply, accept_remote=accept_remote,
            only_ids=only_ids)
        plan["actions"].extend(lifecycle_actions)
        plan["actions"].extend(draft_actions)
        results.extend(draft_results)
        # Independently created Admin records have no stable local identity yet.
        # Surface them for explicit bootstrap instead of reporting an apparently
        # complete round while silently ignoring new website content.
        claimed = {(row["collection"], str(row["remote_id"])) for row in state["baselines"].values()}
        plan["unmapped_remote_records"] = [
            {"collection": collection, "remote_id": row["id"]}
            for collection in ("posts", "works") for row in records[collection]
            if (collection, str(row["id"])) not in claimed]
        # Internal reverse plans can contain remote draft prose. Default sync
        # receipts expose comparison hashes; values require explicit sync-review.
        for action in plan["actions"]:
            action.pop("library_draft", None)
            action.pop("website_only_fields_requiring_review", None)
        return {"schema_version": 1, "mode": "one-shot-apply" if apply else "read-and-plan", "plan": plan,
                "results": results,
                "pending_ids": sorted(note_id for note_id in state["pending"] if only_ids is None or note_id in only_ids),
                "scope_ids": sorted(only_ids) if only_ids is not None else None,
                "warning": "Conditional requests narrow races; no server-wide atomic compare-and-swap is guaranteed."}


def review_sync(library, client, note_ids=None, *, include_values=False):
    """Public transport entry point; implementation remains independently read-only."""
    from .review import review_sync as review
    return review(library, client, note_ids, include_values=include_values)


def resolve_sync(library, client, note_id, resolutions, *, expected_hash,
                 expected_remote_version, expected_review_token, apply=False):
    from .review import resolve_sync as resolve
    return resolve(library, client, note_id, resolutions, expected_hash=expected_hash,
                   expected_remote_version=expected_remote_version,
                   expected_review_token=expected_review_token, apply=apply)
