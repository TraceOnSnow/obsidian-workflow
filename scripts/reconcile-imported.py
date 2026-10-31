#!/usr/bin/env python3
"""One-shot local import/mapping of a reviewed complete Payload snapshot.

No remote mutation, daemon, model, timer, or automatic publication selection for
private content. Public versions are selected locally only with --apply, solely
to adopt content that is independently observed as already published remotely.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from markdown_library import Library, LibraryError
from markdown_library.bootstrap import bootstrap
from markdown_library.core import _split
from markdown_library.sync import _project, _remote_hash, export_publication, observed_baseline
from markdown_library.transport import (PayloadClient, _Store, _identity_matches,
                                        _resolve_relationships, adopt_existing, map_imported)


def _discard_provisional_selection(library, note_id, selected):
    """Roll back only this call's identical local selection after failed adoption.

    This restores the absence of selection; unpublish would create a new remote
    withdrawal intent. A concurrent user selection is always preserved.
    """
    with library._lock():
        state = library._load()
        if state["publications"].get(note_id) == selected:
            del state["publications"][note_id]
            library._save(state)


def reconcile(library, client, snapshot, *, apply=False):
    if any(not isinstance(snapshot.get(name), list) for name in ("posts", "works")):
        raise LibraryError("Reconciliation requires complete plain-array snapshot collections")
    imported = bootstrap(library, snapshot=snapshot, target=client.target, apply=apply)
    bases = {name: {str(row["id"]): row for row in snapshot.get("published", {}).get(name, [])}
             for name in ("posts", "works")}
    imports = {(row["collection"], str(row["remote_id"])): row for row in imported["results"]}
    results, public, private = [], {"posts": [], "works": []}, []
    for collection in ("posts", "works"):
        for remote in snapshot[collection]:
            row = {"collection": collection, "remote_id": remote["id"]}
            item = imports[(collection, str(remote["id"]))]
            if "id" in item: row["id"] = item["id"]
            try:
                export_publication({"id": "preview", "kind": "post" if collection == "posts" else "work",
                                    "state": "published", "snapshot": {
                                        "title": remote.get("title"), "properties": remote,
                                        "markdown": remote.get("markdown", "")}})
            except ValueError:
                row.update(status="blocked", reason="protected-unknown-theme-or-incomplete-record")
                results.append(row)
                continue
            is_public = remote.get("_status") == "published" and (
                collection == "posts" or remote.get("visibility") == "public")
            if is_public:
                base = bases[collection].get(str(remote["id"]))
                if (not base or base.get("_status") != "published"
                        or _remote_hash(base, collection) != _remote_hash(remote, collection)):
                    row.update(status="blocked", reason="public-latest-and-base-diverge-or-missing")
                else:
                    row["status"] = "plan-adopt-public"
                    public[collection].append(row)
            else:
                row["status"] = "plan-map-imported-draft"
                private.append(row)
            results.append(row)
    if apply:
        store = _Store(library, client.target)
        observed_records = {name: client.list_collection(name, draft=True) for name in ("posts", "works")}
        for collection in ("posts", "works"):
            selected = []
            by_id, provisional = {}, {}
            for row in public[collection]:
                note_id = row["id"]
                if note_id in store.load()["baselines"]:
                    row["status"] = "existing-baseline"
                    continue
                try:
                    if note_id in store.load()["pending"]:
                        raise LibraryError("Pending outcome requires normal reconciliation")
                    current = library.get(note_id)
                    article = current["properties"].get("article")
                    if collection == "works" and article is not None:
                        relation = store.load()["baselines"].get(article)
                        if not relation or relation.get("collection") != "posts" or relation.get("state") != "published":
                            raise LibraryError("Related Post lacks observed publication")
                    prior = next((value for value in library.publications() if value["id"] == note_id), None)
                    if prior and (prior["state"] != "published" or prior["sha256"] != current["sha256"]):
                        raise LibraryError("Existing selection differs from imported current draft")
                    # Verify a virtual publication before selecting anything.
                    # Unsupported protection, stale base, identity and content
                    # mismatches must never leave an unintended local selection.
                    virtual = {"id": note_id, "kind": current["properties"]["kind"], "state": "published",
                               "snapshot": {"title": current["properties"]["title"],
                                            "properties": current["properties"],
                                            "markdown": _split(current["markdown"])[1]}}
                    resolved = _resolve_relationships(virtual, store.load()["baselines"])
                    export = export_publication(resolved)
                    for pending in store.load()["pending"].values():
                        if (pending.get("identity") == export["identity"]
                                or str(pending.get("remote_id")) == str(row["remote_id"])):
                            raise LibraryError("Another pending outcome claims this identity")
                    for other in library.publications():
                        if other["id"] != note_id and other["kind"] == current["properties"]["kind"]:
                            if export_publication(other)["identity"] == export["identity"]:
                                raise LibraryError("Another selection claims this identity")
                    matching = [value for value in observed_records[collection]
                                if _identity_matches(value, export["identity"])]
                    if len(matching) != 1 or str(matching[0]["id"]) != str(row["remote_id"]):
                        raise LibraryError("Remote published identity is missing or ambiguous")
                    latest = client.get(collection, row["remote_id"], draft=True)
                    base = client.get(collection, row["remote_id"], draft=False)
                    if _remote_hash(latest, collection) != _remote_hash(base, collection):
                        raise LibraryError("Fresh latest/base published fields differ")
                    for value in (latest, base):
                        if str(value.get("id")) != str(row["remote_id"]) or value.get("_status") != "published":
                            raise LibraryError("Fresh latest/base identity or published state differs")
                        projection = _project(value, collection)
                        if any(key not in projection or projection[key] != field for key, field in export["fields"].items()):
                            raise LibraryError("Current import does not exactly match fresh published fields")
                        observed_baseline(resolved, value)
                        if collection == "posts":
                            export_publication({"id": note_id, "kind": "post", "state": "published",
                                                "snapshot": {"title": value.get("title"), "properties": value,
                                                             "markdown": value.get("markdown", "")}})
                    if not prior:
                        provisional[note_id] = library.publish(note_id, expected_hash=current["sha256"])
                    selected.append(note_id)
                    by_id[note_id] = row
                except (ValueError, LibraryError):
                    row.update(status="blocked", reason="local-selection-or-published-relation-requires-review")
            if selected:
                try:
                    for observed in adopt_existing(library, client, selected, apply=True)["results"]:
                        row = by_id[observed["id"]]
                        row.update(status="adopted" if observed["status"] == "adopted" else "blocked")
                        if observed["status"] != "adopted": row["reason"] = observed.get("reason", "adoption-rejected")
                finally:
                    # No remote write was attempted. If Admin changed after
                    # preflight, discard our provisional selection while keeping
                    # all successfully observed baselines and user selections.
                    saved = store.load()["baselines"]
                    for note_id, value in provisional.items():
                        if note_id not in saved:
                            _discard_provisional_selection(library, note_id, value)
        ids, by_id = [], {}
        for row in private:
            if row["id"] in store.load()["baselines"]:
                row["status"] = "existing-baseline"
            else:
                ids.append(row["id"])
                by_id[row["id"]] = row
        if ids:
            for observed in map_imported(library, client, ids, apply=True)["results"]:
                row = by_id[observed["id"]]
                row.update(status="mapped-imported-draft" if observed["status"] == "mapped-imported-draft" else "blocked")
                if observed["status"] != "mapped-imported-draft": row["reason"] = observed.get("reason", "mapping-rejected")
    return {"mode": "reconcile-local" if apply else "reconcile-preview", "remote_writes": False,
            "observations_verified": apply,
            "public_selection_local_only": apply, "counts": dict(Counter(row["status"] for row in results)),
            "blocked_reasons": dict(Counter(row["reason"] for row in results if row["status"] == "blocked")),
            "bootstrap_counts": imported["counts"], "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True)
    parser.add_argument("--snapshot", required=True, help="Reviewed complete latest/base snapshot JSON")
    parser.add_argument("--url", required=True)
    parser.add_argument("--api-key-env", default="HOMEPAGE_API_KEY")
    parser.add_argument("--allow-loopback-http", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        library = Library(args.vault)
        client = PayloadClient(args.url, os.environ.get(args.api_key_env),
                               allow_loopback_http=args.allow_loopback_http)
        snapshot = json.loads(Path(args.snapshot).read_bytes())
        print(json.dumps(reconcile(library, client, snapshot, apply=args.apply), ensure_ascii=False))
        return 0
    except (LibraryError, ValueError, OSError):
        print(json.dumps({"error": "Reconciliation rejected; preserve snapshot and review source, local state, and credentials"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
