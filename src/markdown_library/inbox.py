"""Small deterministic inbox trigger. No model, HTTP, publication or Git actions.

First apply baselines existing identities. Later applies observe only managed
notes in the two inbox roots; unchanged observations must be 60 seconds apart.
Unclear routes and links that need rewriting remain in a private review queue.
"""
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import unquote

from .core import Library, LibraryError, _hash, _read, _registration_frontmatter, _split
from .mcp_actions import _RESERVED_NAMES, _outside_code, _rewrite, _WIKI


INBOXES = ("notes", "00_收件箱")
CATEGORY_ROOTS = ("10_个人记录", "20_项目与构想", "30_知识与参考")
ARCHIVE = "90_归档/历史资料"
VERSION = "inbox-deterministic-v1"
KEY = "inbox_organizer"
# Only specific title terms. No body-derived personal facts or general AI index.
RULES = (
    ("30_知识与参考/算法与编程/模板", r"做题模板|算法模板|(?:python|c\+\+|c17|c\+\+17).{0,12}(?:io|白板|模板)|笔试.?io模板"),
    ("10_个人记录/求职与实习/笔试复盘", r"笔试复盘|(?:\boa\b|笔试).{0,12}(?:作答|复盘|错题)"),
    ("10_个人记录/求职与实习/面试复盘", r"面试复盘|(?:一面|二面|三面|hr面).{0,10}复盘"),
    ("10_个人记录/求职与实习/面试准备", r"面试准备|面试复习|求职面试题"),
    ("10_个人记录/求职与实习/投递与选择", r"秋招追踪|暑期实习追踪|投递追踪|投递时间线"),
    ("10_个人记录/求职与实习/实习经历", r"实习经历|实习工作记录"),
    ("10_个人记录/求职与实习/简历与材料", r"简历修改|简历措辞|简历项目描述"),
    ("10_个人记录/日记", r"^\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?.*日记$|^日记[：: ]"),
    ("20_项目与构想/个人 AI 工作流", r"个人\s*ai\s*工作流|ai\s*软件开发工作流"),
    ("20_项目与构想/Steam Guess", r"steam\s*guess|steam猜游戏"),
    ("20_项目与构想/课程项目/Atlas", r"\batlas\b"),
    ("20_项目与构想/课程项目", r"hearthgen|comp\s*539"),
    ("30_知识与参考/数学与优化", r"核函数|特征映射|线性代数|凸优化|概率论"),
    ("30_知识与参考/算法与编程/Codeforces", r"codeforces|\bcf\s*round\b"),
    ("30_知识与参考/算法与编程/LeetCode", r"leetcode"),
)


def _visible_files(library, roots=None):
    bases = [library._path(root) for root in roots] if roots else [library.vault]
    for base in bases:
        if not base.is_dir():
            continue
        library._check_components(base)
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in dirs:
                library._check_components(Path(directory) / name)
            for name in sorted(files):
                if name.startswith(".") or not name.lower().endswith(".md"):
                    continue
                yield (Path(directory) / name).relative_to(library.vault).as_posix()


def categories(library):
    result = set()
    for root in CATEGORY_ROOTS:
        base = library._path(root)
        if not base.is_dir():
            continue
        for directory, dirs, _ in os.walk(base, followlinks=False):
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            library._check_components(Path(directory))
            result.add(Path(directory).relative_to(library.vault).as_posix())
    if library._path(ARCHIVE).is_dir():
        result.add(ARCHIVE)
    return result


def route(title, allowed):
    if re.match(r"^(?:归档[：: ]|已过期临时|已作废临时)", title):
        return (ARCHIVE, "explicit_archive_title") if ARCHIVE in allowed else (None, "archive_directory_missing")
    matched = {destination for destination, pattern in RULES if re.search(pattern, title, re.I)}
    # A more specific existing child wins over its parent (Atlas vs course work).
    matched = {path for path in matched if not any(other.startswith(path + "/") for other in matched)}
    if len(matched) != 1:
        return None, "category_uncertain" if not matched else "category_conflict"
    destination = next(iter(matched))
    return (destination, "title_keyword") if destination in allowed else (None, "category_directory_missing")


def _complete(body):
    fence = None
    for line in body.splitlines():
        found = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if not found:
            continue
        if fence is None:
            fence = found[1]
        elif found[1][0] == fence[0] and len(found[1]) >= len(fence) and not found[2].strip():
            fence = None
    return bool(body.strip()) and fence is None


def _read_inbox(library, state):
    candidates, skipped, duplicates = [], [], set()
    for relative in _visible_files(library, INBOXES):
        if Path(relative).stem.casefold() in _RESERVED_NAMES:
            continue
        path = library._path(relative)
        try:
            before = path.stat()
            if before.st_size > 2_000_000:
                skipped.append({"path": relative, "status": "needs_review", "reason": "file_too_large"})
                continue
            markdown = _read(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                skipped.append({"path": relative, "status": "waiting", "reason": "file_changing"})
                continue
            properties = _registration_frontmatter(markdown)[0]
            _, body = _split(markdown)
            identity = properties.get("id")
            if properties.get("kind") != "note":
                continue
            record = state["notes"].get(identity) if isinstance(identity, str) else None
            if record is None or record.get("deleted") or record.get("purged") or record.get("unregistered"):
                skipped.append({"path": relative, "status": "needs_review", "reason": "not_managed_active_note"})
                continue
            library._validate_properties(properties)
            if not _complete(body):
                skipped.append({"id": identity, "path": relative, "status": "waiting", "reason": "incomplete_body"})
                continue
            if any(item["id"] == identity for item in candidates):
                duplicates.add(identity)
            candidates.append({"id": identity, "path": relative, "markdown": markdown,
                               "properties": properties, "sha256": _hash(markdown),
                               "mtime_ns": after.st_mtime_ns, "size": after.st_size})
        except (UnicodeError, ValueError, OSError, LibraryError):
            skipped.append({"path": relative, "status": "waiting", "reason": "incomplete_or_unreadable_note"})
    for candidate in candidates:
        if candidate["id"] in duplicates:
            skipped.append({"id": candidate["id"], "path": candidate["path"], "status": "needs_review", "reason": "duplicate_stable_identity"})
    return [candidate for candidate in candidates if candidate["id"] not in duplicates], skipped


def _links_allow_byte_preserving_move(library, candidate, destination, files):
    """Reuse the real link resolver; only unique bare stems may stay unchanged.

    No other note is rewritten by this trigger. Qualified/relative links that
    actually need edits are queued for the existing explicit organizer helper.
    """
    old = candidate["path"]
    stem = Path(old).stem
    if sum(Path(path).stem == stem for path in files) != 1:
        return False
    for owner in files:
        markdown = candidate["markdown"] if owner == old else _read(library._path(owner))
        if owner != old and _split(markdown)[0].get("id") == candidate["id"]:
            return False
        def safe_short_link(match):
            raw, bar, alias = match[1].partition("|")
            target, mark, anchor = raw.partition("#")
            decoded = unquote(target)
            if decoded not in (stem, stem + ".md"):
                return match[0]
            replacement = destination if decoded.endswith(".md") else destination[:-3]
            return "[[" + replacement + (mark + anchor if mark else "") + (bar + alias if bar else "") + "]]"
        safe_equivalent = _outside_code(markdown, lambda text: _WIKI.sub(safe_short_link, text))
        resolved, _ = _rewrite(markdown, owner, destination if owner == old else owner, {old: destination}, set(files))
        if resolved != safe_equivalent:
            return False
    return True


def organize(vault, *, apply=False, max_new=4, now=None, route_overrides=None):
    if type(max_new) is not int or not 1 <= max_new <= 100:
        raise LibraryError("max_new must be between 1 and 100")
    now = time.time() if now is None else now
    library = Library(vault).validate()
    with library._lock() if apply else nullcontext():
        if library._path(".markdown-library/transaction.json").exists():
            return {"applied": False, "counts": {"waiting": 1}, "items": [], "reason": "pending_library_transaction", "model_calls": 0}
        state = library._load() if apply else library._read_state()
        machine = deepcopy(state.get(KEY))
        initial = machine is None
        if initial:
            machine = {"version": VERSION, "initialized_at": now, "baseline_ids": sorted(state["notes"]),
                       "observations": {}, "queue": {}, "receipts": {}}
        if machine.get("version") != VERSION:
            raise LibraryError("Unsupported inbox observation state")
        candidates, outcomes = _read_inbox(library, state)
        allowed = categories(library)
        chosen = []
        organized_ids = {receipt["id"] for receipt in machine["receipts"].values()}
        for candidate in candidates:
            identity, digest, path = candidate["id"], candidate["sha256"], candidate["path"]
            row = {"id": identity, "path": path}
            if identity in machine["baseline_ids"] or identity in organized_ids:
                outcomes.append({**row, "status": "existing", "reason": "existing_note_requires_user_batch"})
                continue
            record_path = state["notes"][identity].get("path", "")
            if record_path.split("/", 1)[0] not in INBOXES:
                outcomes.append({**row, "status": "existing", "reason": "existing_theme_note_in_inbox"})
                continue
            fingerprint = {key: candidate[key] for key in ("path", "sha256", "mtime_ns", "size")}
            previous = machine["observations"].get(identity)
            unchanged = previous and previous["fingerprint"] == fingerprint and previous["last_seen"] <= now
            observation = {"fingerprint": fingerprint, "stable_since": previous["stable_since"] if unchanged else now,
                           "scans": previous["scans"] + int(previous["last_seen"] < now) if unchanged else 1, "last_seen": now}
            machine["observations"][identity] = observation
            queued = machine["queue"].get(identity)
            if queued and queued["sha256"] != digest:
                machine["queue"].pop(identity, None)
                queued = None
            if (observation["scans"] < 2 or now - observation["stable_since"] < 60
                    or now - candidate["mtime_ns"] / 1e9 < 60):
                outcomes.append({**row, "status": "waiting", "reason": "awaiting_two_stable_scans_and_60_seconds"})
                continue
            override = (route_overrides or {}).get((identity, digest))
            if override and override.get("path", path) != path:
                outcomes.append({**row, "status": "waiting", "reason": "semantic_source_path_changed"})
                continue
            if override and queued and queued["reason"] == "category_uncertain":
                category = override.get("category")
                if category not in allowed:
                    reason = override.get("reason", "semantic_category_not_allowed") if category is None else "semantic_category_not_allowed"
                    machine["queue"][identity] = {"path": path, "sha256": digest, "reason": reason, "observed_at": now}
                    outcomes.append({**row, "status": "needs_review", "reason": reason})
                    continue
                machine["queue"].pop(identity, None)
                reason = "semantic_category"
            elif queued:
                outcomes.append({**row, "status": "needs_review", "reason": queued["reason"]})
                continue
            else:
                category, reason = route(candidate["properties"]["title"], allowed)
            if category is None:
                machine["queue"][identity] = {"path": path, "sha256": digest, "reason": reason, "observed_at": now}
                outcomes.append({**row, "status": "needs_review", "reason": reason})
                continue
            destination = category + "/" + Path(path).name
            if library._path(destination).exists():
                reason = "destination_occupied"
                machine["queue"][identity] = {"path": path, "sha256": digest, "reason": reason, "observed_at": now}
                outcomes.append({**row, "status": "needs_review", "reason": reason})
                continue
            if len(chosen) >= max_new:
                outcomes.append({**row, "status": "waiting", "reason": "batch_limit"})
                continue
            chosen.append((candidate, destination, row, reason))
        files = list(_visible_files(library)) if chosen else []
        writes, removes = [], []
        destinations = set()
        for candidate, destination, row, reason in chosen:
            try:
                links_safe = destination not in destinations and _links_allow_byte_preserving_move(library, candidate, destination, files)
            except (LibraryError, OSError, UnicodeError):
                links_safe = False
            if not links_safe:
                machine["queue"][candidate["id"]] = {"path": candidate["path"], "sha256": candidate["sha256"],
                                                       "reason": "link_repair_or_name_review_required", "observed_at": now}
                outcomes.append({**row, "status": "needs_review", "reason": "link_repair_or_name_review_required"})
                continue
            destinations.add(destination)
            outcomes.append({**row, "destination": destination, "status": "moved" if apply else "ready", "reason": reason})
            if apply:
                library._expect({"sha256": _hash(_read(library._path(candidate["path"])))}, candidate["sha256"])
                note = {key: candidate[key] for key in ("id", "path", "properties", "markdown", "sha256")}
                library._remember(state, note, "inbox-before-move")
                note["path"] = destination
                library._remember(state, note, "inbox-organized")
                receipt_id = _hash(json.dumps([candidate["id"], candidate["sha256"], destination], ensure_ascii=False))
                machine["receipts"][receipt_id] = {"id": candidate["id"], "sha256": candidate["sha256"],
                                                    "previous_path": candidate["path"], "path": destination, "moved_at": now}
                machine["observations"].pop(candidate["id"], None)
                writes.append((destination, candidate["markdown"], None))
                removes.append((candidate["path"], candidate["sha256"]))
        if apply:
            for destination, _, _ in writes:
                if not library._path(destination).parent.is_dir():
                    raise LibraryError("Existing category directory changed before move")
            state[KEY] = machine
            library._commit(state, writes, removes)
            for destination, markdown, _ in writes:
                library._expect({"sha256": _hash(_read(library._path(destination)))}, _hash(markdown))
        return {"applied": apply, "initialized": not initial or apply, "baseline_initialized_now": initial and apply,
                "counts": dict(Counter(row["status"] for row in outcomes)), "items": outcomes,
                "model_calls": 0, "archive_filter_required": any(row.get("destination", "").startswith(ARCHIVE + "/") for row in outcomes)}
