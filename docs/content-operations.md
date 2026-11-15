# Local content operations

The installed `markdown-library` CLI and MCP use the same guarded organization
implementation. These operations manage private drafts only: they never select
publication, call a model, or write to a website. Use the CLI directly when the
current Agent does not have an MCP connection; do not import internal Python
helpers or construct a temporary orchestration script.

## Find the current inputs

`search` returns bounded identities, paths and hashes, rather than every matching
body. Read a selected note with `get ID` before preparing semantic changes.
A query is split on whitespace and every term must appear (case-insensitive
substring, so Chinese needs no segmentation). Results are ordered by relevance,
with title matches first, and each carries a short one-line `snippet` around the
match. An empty query lists by path without snippets.

## Check notes against the owner's mechanical rules

`check` reads a rules file kept with the Vault (default
`config/editorial-checks.json`, or `--rules FILE`) and reports violations
without editing anything. It exits 2 when something is found, so it can gate
filing or publishing. Limit it with note IDs, `--folder` or `--kind`.

```sh
markdown-library --vault /absolute/vault check
markdown-library --vault /absolute/vault check NOTE_ID --kind post
```

The rules are private policy and are not part of this repository. Each rule has
an `id`, a `type` and a regular-expression `pattern`, plus optional `message`,
`kinds`, `folders`, `exclude_folders`, `path_pattern` and `ignore_line_pattern`:

| Type | Meaning |
| --- | --- |
| `forbid` | The pattern must not match any body line outside fenced code |
| `forbid-in-code-span` | The pattern must not match the text of an inline code span |
| `require` | The pattern must match at least one body line outside fenced code |
| `filename` | The file name without `.md` must match the pattern |

```sh
markdown-library --vault /absolute/vault search --query game --limit 10
markdown-library --vault /absolute/vault get NOTE_ID
markdown-library --vault /absolute/vault organize --schema
```

## Move a batch or merge an approved group

`organize`, `move`, and `merge` accept `--request FILE` or `--request -` for stdin.
The latter two enforce the corresponding operation type. A move request is:

```json
{
  "operation": "move",
  "moves": [
    {
      "note_id": "NOTE_ID",
      "destination": "25_游戏设计/Existing name.md",
      "expected_hash": "SHA_FROM_CURRENT_READ"
    }
  ]
}
```

A merge request contains `operation: "merge"`, `target_id`, `source_ids`, the
complete target `markdown`, and `expected_hashes` for the target and every source.
Preserve all target properties. The program validates this, protects mapped or
published sources, retains originals, and redirects imported source identities.

```sh
markdown-library --vault /absolute/vault organize --request request.json
markdown-library --vault /absolute/vault organize --request request.json --apply --expected-token PREVIEW_TOKEN
```

The Agent obtains the hashes and token; the user does not need to copy or manage
them. Preview makes no content/history/state changes. Apply rechecks the request,
current files and source/publication mappings under the library lock. A changed
note, new backlink, occupied path, duplicate destination or conflicting parent
invalidates the batch before any note is moved. Up to 100 moves share one library
transaction, so changes to cross-links are computed once against the whole map.

The receipt includes final IDs, paths, hashes, link repair counts and
`readback_verified`. A moved note may require link-only edits for relative assets
or cross-links; `content_unchanged` reports exact byte identity, not semantic
equivalence. Merge sources remain recoverable through the original lifecycle.

A Post may also be moved, but only to another filename inside `发表稿/`. This
renames the local file and repairs links; its title, website link and
publication state stay unchanged, and `title` is rejected for Posts (change a
public title with `publish-article`).

A Work with an existing review file can likewise be moved within the configured
review root (`works/感想/` by default), including type/year subfolders.
Its catalog identity, title, daily fields and publication state remain
unchanged; catalog-only Works and `title` changes are rejected. The same merge
request can use a Work target, preserving all target properties while absorbing
ordinary note sources and retaining their recoverable history.

A move item may also specify a short `title`. This changes only the frontmatter
title and preserves the full body, including any authored heading; the receipt
reports `body_unchanged`. To change just the display title, keep the same
destination. To shorten both the filename and display title, supply both together.

Directories refresh after the content transaction. A directory problem cannot
undo the saved content operation: inspect the separate `navigation` result, and
retry only the directory step, never the move/merge. Custom indexes and existing
Base layouts are retained and listed separately; they do not prevent unrelated
generated directories from refreshing. No retired empty Blogs entry is recreated.

```sh
markdown-library --vault /absolute/vault navigation --scope 25_游戏设计
markdown-library --vault /absolute/vault navigation --scope 25_游戏设计 --apply --expected-token PREVIEW_TOKEN
```

Existing single-note MCP `move_note` and `merge_notes` calls remain compatible.
For a batch or an explicit preview use `organize_notes(schema=true)`, then submit
the request, then apply with its token. Read-only MCP does not expose this tool.
`get_workflow` reads the maintained in-vault policy on each call when available,
so a conversation-driven rule update does not depend on editing a second embedded
policy or restarting the service. The built-in workflow remains a fallback for a
new deployment without that policy file.

## Find recorded source messages

```sh
markdown-library --vault /absolute/vault sources NOTE_ID
markdown-library --vault /absolute/vault source-messages NOTE_ID --workbench /absolute/workbench --job-id RECORDED_JOB_ID --role user --limit 5
```

`sources` (MCP: `note_sources`) returns only the selected current note's source
references and merge redirects. It does not expose arbitrary state or credentials.
If several source jobs are recorded, `source-messages` returns `available_jobs`
until one is selected. It accepts only a job recorded for that note, and only
`jobs/<job_id>/input.json` inside the explicit local workspace; paths and symlinks
cannot escape it. This local source-file reader is not exposed over remote MCP.

For timestamps, it can additionally read the source package named in that saved
input, after checking its exact byte hash and conversation identity. It returns
UTC `created_at` values only for the selected messages, plus conversation dates
and `source_timestamps.status`; missing or changed packages provide no dates.
This does not reverify the original upload commit or turn draft creation times
into message times.

The messages are the recorded inputs used for that historical draft. They are
not a live query of ChatGPT or a fresh verification of the original export. The
receipt names that artifact and includes its exact file hash. Missing material
is reported rather than inferred. Use `offset`/`limit`, optionally `role`, and
`max-chars` to bound the read. A long message returns an explicit continuation
with `offset` and `text_offset`; resume it with `--limit 1 --text-offset N`. No
truncated segment should be treated as a complete conversation.
