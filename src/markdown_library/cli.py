"""JSON command line interface with an explicit Vault and explicit network commands."""

import argparse
import json
import os
from pathlib import Path
import sys

from .core import Library, LibraryError


def _read(filename):
    return sys.stdin.buffer.read().decode("utf-8") if filename == "-" else Path(filename).read_bytes().decode("utf-8")


def parser():
    result = argparse.ArgumentParser(description="Local Markdown library. Saving and publishing are separate actions.")
    result.add_argument("--vault", required=True, help="Explicit absolute Vault path")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Explicitly initialize a local Vault")
    listing = commands.add_parser("list", help="List managed notes, discovering native edits and renames")
    listing.add_argument("--query", default="")
    for name in ("get", "unpublish", "publication"):
        commands.add_parser(name).add_argument("id")
    work_review = commands.add_parser('prepare-work-review', help='Prepare Work body as a linked Post current draft; never select or publish')
    work_review.add_argument('id')
    work_review.add_argument('--expected-hash', required=True)
    work_review.add_argument('--expected-post-hash')
    work_review.add_argument('--expected-token')
    work_review.add_argument('--apply', action='store_true')
    register = commands.add_parser("register", aliases=["track-existing"], help="Preview tracking one existing Markdown file in place; never selects publication")
    register.add_argument("path", help="Explicit visible relative .md path inside the Vault")
    register.add_argument("--title", help="Title only if missing; defaults to the filename")
    register.add_argument("--kind", choices=["note", "work", "post"], default="note")
    register.add_argument("--apply", action="store_true")
    register.add_argument("--expected-hash", help="Original current-file SHA256; required with --apply")
    original = commands.add_parser("restore-original", help="Restore exact pre-registration original in place; retains history and removes managed identity")
    original.add_argument("id")
    original.add_argument("--expected-hash", required=True)
    for name in ("create", "import"):
        command = commands.add_parser(name)
        command.add_argument("file", help="UTF-8 Markdown file, or - for stdin")
        command.add_argument("--title", required=True)
        command.add_argument("--kind", choices=["note", "work", "post"], default="note")
        if name == "create":
            command.add_argument("--path", help="Relative visible .md destination; refuses overwrite")
            command.add_argument("--properties", help="JSON properties file")
        else:
            command.add_argument("--source-id", required=True)
            command.add_argument("--source", help="Private provenance JSON file")
    replace = commands.add_parser("replace")
    replace.add_argument("id")
    replace.add_argument("file", help="Complete replacement Markdown, preserving frontmatter id")
    replace.add_argument("--expected-hash", required=True)
    history = commands.add_parser("history")
    history.add_argument("id")
    history.add_argument("--revision", help="Read a specific SHA256 revision")
    restore = commands.add_parser("restore")
    restore.add_argument("id")
    restore.add_argument("revision")
    restore.add_argument("--expected-hash", required=True)
    for name in ("publish", "delete", "recover"):
        command = commands.add_parser(name)
        command.add_argument("id")
        command.add_argument("--expected-hash", required=True)
        if name == "recover":
            command.add_argument("--path", help="Optional relative destination")
    missing_delete = commands.add_parser("confirm-missing-delete", help="Explicitly confirm a native deletion using the last retained object; missing scans alone never delete")
    missing_delete.add_argument("id")
    missing_delete.add_argument("--expected-hash", required=True)
    missing_delete.add_argument("--apply", action="store_true")
    commands.add_parser("publications", help="Selected public snapshots, including withdrawal/delete intents")
    sync = commands.add_parser("sync-plan", help="Offline plan from selected snapshots and explicit remote/baseline JSON files")
    sync.add_argument("--remote", required=True, help="Complete remote records JSON: {works: [...], posts: [...]}")
    sync.add_argument("--baseline", required=True, help="Baseline JSON keyed by library ID; use {} initially")
    sync.add_argument("--works-drafts-and-versions", action="store_true", help="Assert externally verified Works draft/version support; does not execute a write")
    online = commands.add_parser("sync", help="Explicit Payload one-shot read/plan; --apply enables selected unblocked operations")
    online.add_argument("--url", help="Explicit HTTPS origin; otherwise reads only HOMEPAGE_URL")
    online.add_argument("--api-key-env", default="HOMEPAGE_API_KEY", help="Name of environment variable holding API key; no automatic config file reads")
    online.add_argument("--allow-loopback-http", action="store_true")
    online.add_argument("--timeout", type=float, default=20)
    online.add_argument("--apply", action="store_true")
    online.add_argument("--only", action="append", metavar="ID", help="Limit all sync mutations to these library IDs; repeat for a batch")
    online.add_argument("--accept-remote", action="store_true", help="With --apply, receive one-sided remote changes into selected or unselected imported drafts; never selects publication")
    online.add_argument("--resume-pending", action="append", default=[], metavar="ID", help="Explicitly continue a reviewed discovered draft, never re-POST")
    online.add_argument("--works-drafts-and-versions", action="store_true", help="Assert externally verified backend/client capability")
    bootstrap = commands.add_parser("bootstrap", help="Preview/import existing website records as local working files; never writes to the website")
    bootstrap.add_argument("--snapshot", help="Complete JSON snapshot file containing target, works, posts, optional published variants")
    bootstrap.add_argument("--target", help="Explicit source origin for an offline snapshot")
    bootstrap.add_argument("--url", help="Explicit HTTPS origin for read-only API source; or HOMEPAGE_URL")
    bootstrap.add_argument("--api-key-env", default="HOMEPAGE_API_KEY")
    bootstrap.add_argument("--allow-loopback-http", action="store_true")
    bootstrap.add_argument("--timeout", type=float, default=20)
    bootstrap.add_argument("--apply", action="store_true", help="Import local working files/history only; never select publication or write remote")
    adopt = commands.add_parser("adopt", help="Verify selected library snapshots against existing published records; --apply stores local baselines only")
    adopt.add_argument("ids", nargs="+", help="Explicit library IDs to adopt; other records are untouched")
    adopt.add_argument("--url", help="Explicit HTTPS origin; otherwise HOMEPAGE_URL")
    adopt.add_argument("--api-key-env", default="HOMEPAGE_API_KEY")
    adopt.add_argument("--allow-loopback-http", action="store_true")
    adopt.add_argument("--timeout", type=float, default=20)
    adopt.add_argument("--apply", action="store_true", help="Save verified local baselines; never publishes or writes the website")
    mapping = commands.add_parser("map-imported", help="Observe original bootstrap private/draft records; --apply stores local imported-draft mappings only")
    mapping.add_argument("ids", nargs="+", help="Explicit imported library IDs")
    mapping.add_argument("--url", help="Explicit HTTPS origin; otherwise HOMEPAGE_URL")
    mapping.add_argument("--api-key-env", default="HOMEPAGE_API_KEY")
    mapping.add_argument("--allow-loopback-http", action="store_true")
    mapping.add_argument("--timeout", type=float, default=20)
    mapping.add_argument("--apply", action="store_true", help="Save verified draft mapping receipts; never select or publish content")
    relations = commands.add_parser("relations", help="Preview or refresh Work/Post backlinks without publication")
    relations.add_argument("--apply", action="store_true")
    for name in ("sync-review", "sync-resolve", "lifecycle-review", "lifecycle-resolve"):
        command = commands.add_parser(name, help="Review or explicitly resolve working-draft/lifecycle conflicts; never publish")
        if name in {"sync-resolve", "lifecycle-resolve"}:
            command.add_argument("id")
            command.add_argument("--expected-hash", required=True)
            command.add_argument("--expected-remote-version", required=True)
            command.add_argument("--expected-review-token", required=True)
            command.add_argument("--apply", action="store_true")
            if name == "sync-resolve":
                command.add_argument("--resolutions", required=True, help="JSON file mapping field names to local/remote/base or {present,value}")
            else:
                command.add_argument("--decision", choices=["delete", "restore"], required=True)
        else:
            command.add_argument("ids", nargs="*")
            if name == "sync-review":
                command.add_argument("--include-values", action="store_true", help="Explicitly include private draft field values")
        command.add_argument("--url")
        command.add_argument("--api-key-env", default="HOMEPAGE_API_KEY")
        command.add_argument("--allow-loopback-http", action="store_true")
        command.add_argument("--timeout", type=float, default=20)
    return result


def dispatch(arguments):
    if arguments.command == "init":
        library = Library.initialize(arguments.vault)
        return {"initialized": True, "vault": str(library.vault)}
    library = Library(arguments.vault)
    action = arguments.command
    if action == "relations":
        from .relations import refresh_relations
        return refresh_relations(library, apply=arguments.apply)
    if action in {"sync-review", "sync-resolve", "lifecycle-review", "lifecycle-resolve"}:
        from .transport import PayloadClient
        url = arguments.url or os.environ.get("HOMEPAGE_URL")
        key = os.environ.get(arguments.api_key_env)
        if not url or not key:
            raise LibraryError(f"{action} requires an explicit URL and named API-key environment variable")
        client = PayloadClient(url, key, arguments.timeout, allow_loopback_http=arguments.allow_loopback_http)
        if action == "sync-review":
            from .transport import review_sync
            return review_sync(library, client, arguments.ids or None, include_values=arguments.include_values)
        if action == "lifecycle-review":
            from .lifecycle import review_lifecycle
            return review_lifecycle(library, client, arguments.ids or None)
        guards = {"expected_hash": arguments.expected_hash,
                  "expected_remote_version": arguments.expected_remote_version,
                  "expected_review_token": arguments.expected_review_token, "apply": arguments.apply}
        if action == "sync-resolve":
            from .transport import resolve_sync
            return resolve_sync(library, client, arguments.id, json.loads(_read(arguments.resolutions)), **guards)
        from .lifecycle import resolve_lifecycle
        return resolve_lifecycle(library, client, arguments.id, arguments.decision, **guards)
    if action == "list":
        return library.list_notes(arguments.query)
    if action in {"get", "unpublish", "publication"}:
        return getattr(library, action)(arguments.id)
    if action == 'prepare-work-review':
        from .work_review import prepare_work_review
        return prepare_work_review(library, arguments.id, arguments.expected_hash,
            expected_post_hash=arguments.expected_post_hash, expected_token=arguments.expected_token,
            apply=arguments.apply)
    if action in {"register", "track-existing"}:
        return library.register(arguments.path, title=arguments.title, kind=arguments.kind,
                                apply=arguments.apply, expected_hash=arguments.expected_hash)
    if action == "restore-original":
        return library.restore_original(arguments.id, expected_hash=arguments.expected_hash)
    if action == "create":
        return library.create(_read(arguments.file), title=arguments.title, kind=arguments.kind,
                              properties=json.loads(_read(arguments.properties)) if arguments.properties else None,
                              path=arguments.path)
    if action == "import":
        return library.import_note(_read(arguments.file), source_id=arguments.source_id,
                                   title=arguments.title, kind=arguments.kind,
                                   source=json.loads(_read(arguments.source)) if arguments.source else None)
    if action == "replace":
        return library.replace(arguments.id, _read(arguments.file), expected_hash=arguments.expected_hash)
    if action == "history":
        if arguments.revision:
            return {"id": arguments.id, "sha256": arguments.revision,
                    "markdown": library.history_markdown(arguments.id, arguments.revision)}
        return library.history(arguments.id)
    if action == "restore":
        return library.restore(arguments.id, arguments.revision, expected_hash=arguments.expected_hash)
    if action in {"publish", "delete"}:
        return getattr(library, action)(arguments.id, expected_hash=arguments.expected_hash)
    if action == "recover":
        return library.recover(arguments.id, path=arguments.path, expected_hash=arguments.expected_hash)
    if action == "confirm-missing-delete":
        return library.confirm_missing_delete(arguments.id, expected_hash=arguments.expected_hash, apply=arguments.apply)
    if action == "publications":
        return library.publications()
    if action == "sync-plan":
        from .sync import plan_sync
        return plan_sync(library.publications(), json.loads(_read(arguments.baseline)),
                         json.loads(_read(arguments.remote)),
                         capabilities={"works_drafts_and_versions": arguments.works_drafts_and_versions})
    if action == "sync":
        from .transport import PayloadClient
        from .catalog_sync import sync_catalog as sync_once
        url = arguments.url or os.environ.get("HOMEPAGE_URL")
        key = os.environ.get(arguments.api_key_env)
        if not url or not key:
            raise LibraryError("sync requires --url or HOMEPAGE_URL and the named API-key environment variable")
        client = PayloadClient(url, key, arguments.timeout, allow_loopback_http=arguments.allow_loopback_http)
        return sync_once(library, client, apply=arguments.apply,
                         works_drafts_and_versions=arguments.works_drafts_and_versions,
                         accept_remote=arguments.accept_remote, resume_pending=arguments.resume_pending,
                         only_ids=arguments.only)
    if action == "bootstrap":
        from .bootstrap import bootstrap
        if arguments.snapshot:
            if arguments.url:
                raise LibraryError("bootstrap accepts a snapshot or configured API source, not both")
            return bootstrap(library, snapshot=json.loads(_read(arguments.snapshot)), target=arguments.target, apply=arguments.apply)
        from .transport import PayloadClient
        url = arguments.url or os.environ.get("HOMEPAGE_URL")
        key = os.environ.get(arguments.api_key_env)
        if not url or not key:
            raise LibraryError("bootstrap requires --snapshot or an explicit URL and named API-key environment variable")
        client = PayloadClient(url, key, arguments.timeout, allow_loopback_http=arguments.allow_loopback_http)
        return bootstrap(library, client=client, target=arguments.target, apply=arguments.apply)
    if action in {"adopt", "map-imported"}:
        from .transport import PayloadClient, adopt_existing, map_imported
        url = arguments.url or os.environ.get("HOMEPAGE_URL")
        key = os.environ.get(arguments.api_key_env)
        if not url or not key:
            raise LibraryError(f"{action} requires --url or HOMEPAGE_URL and the named API-key environment variable")
        client = PayloadClient(url, key, arguments.timeout, allow_loopback_http=arguments.allow_loopback_http)
        operation = adopt_existing if action == "adopt" else map_imported
        return operation(library, client, arguments.ids, apply=arguments.apply)
    raise LibraryError(f"Unknown command: {action}")


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        output = dispatch(arguments)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0
    except (LibraryError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
