"""Render a private, human-readable inventory from independent API observations."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from urllib.parse import quote, urlsplit

from .bootstrap import _index
from .core import LibraryError, _hash, _read, _split
from .relations import canonical_article
from .sync import export_publication
from .publication_navigation import strip_publication_navigation, set_publication_navigation


MARKER = "<!-- markdown-library:publication-overview:v1; generated navigation -->"
REPORT_PATH = "发布与同步状态.md"


def _label(value):
    return str(value).replace("\n", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def _current_public_hash(library, note, notes):
    properties, body = _split(note["markdown"])
    if properties['kind'] == 'work':
        from .work_profile import website_properties
        properties = website_properties(library, note)
    snapshot = {"id": note["id"], "kind": properties["kind"], "title": properties["title"],
                "properties": deepcopy(properties)}
    if properties["kind"] == "work" and properties.get("article") is not None:
        snapshot["properties"]["article"] = canonical_article(library, properties["article"], notes=notes)
    if properties["kind"] == "post":
        snapshot["markdown"] = strip_publication_navigation(body)
    return export_publication({"id": note["id"], "kind": properties["kind"], "state": "published",
                               "snapshot": snapshot})["content_hash"]


def _wiki(note):
    if note.get("catalog_backed"):
        from .relations import wikilink
        return wikilink(note)
    target = note["path"].removesuffix(".md")
    if any(char in target for char in "[]|\r\n"):
        raise LibraryError("Publication navigation requires an unambiguous note path")
    title = re.sub(r"[\[\]|\r\n]", " ", note["properties"]["title"])
    return f"[[{target}|{title}]]"


def _post_sources(library, state, notes, posts, sync_state):
    """Only explicit provenance and an existing Work.article prove a source."""
    result = {post_id: {"ids": set(), "primary": set(), "unknown": False} for post_id in posts}
    for row in state["sources"].values():
        post_id = row.get("id")
        if post_id not in result:
            continue
        source = row.get("source", {})
        for key in ("private_source_id", "note_id", "work_id", "source_note_id"):
            source_id = source.get(key)
            if not source_id:
                continue
            candidate = notes.get(source_id) if isinstance(source_id, str) else None
            if not candidate or candidate["properties"]["kind"] not in {"note", "work"}:
                result[post_id]["unknown"] = True
                continue
            result[post_id]["ids"].add(source_id)
            if key == "private_source_id" or source.get("type") in {
                    "publication-copy", "independent-publication-copy-v1"}:
                result[post_id]["primary"].add(source_id)
    from .work_profile import profile
    for note in notes.values():
        if note["properties"]["kind"] != "work":
            continue
        article = (profile(library, note["id"], state) or {}).get("article")
        if article is None:
            continue
        try:
            post_id = canonical_article(library, article, notes=notes)
        except LibraryError:
            continue
        if post_id not in result and isinstance(post_id, (str, int)) and str(post_id).isdigit():
            matches = [key for key, baseline in sync_state.get("baselines", {}).items()
                       if baseline.get("collection") == "posts" and str(baseline.get("remote_id")) == str(post_id)]
            post_id = matches[0] if len(matches) == 1 else None
        if post_id in result:
            result[post_id]["ids"].add(note["id"])
    return result


def _differs_selection(library, note, selected):
    if not selected or note["sha256"] == selected.get("sha256"):
        return False
    clean = strip_publication_navigation(note["markdown"])
    if _hash(clean) == selected.get("sha256"):
        return False
    digest = selected.get("sha256", "")
    if re.fullmatch(r"[0-9a-f]{64}", digest):
        retained = library._path(f".markdown-library/objects/{digest}.md")
        if retained.is_file():
            original = _read(retained)
            if _hash(original) == digest and clean == strip_publication_navigation(original):
                return False
    return True


def _post_status(item, record):
    formal = (item or {}).get("payload_formal")
    latest = (item or {}).get("payload_latest")
    if (formal or {}).get("deleted_at") or (latest or {}).get("deleted_at"):
        return "网站已回收", False
    if formal is not None and formal.get("state") == "published":
        return "网站正式版已发布", True
    if formal is not None and formal.get("state") == "draft":
        return "网站正式版未发布", False
    if record.get("deleted") and record.get("trash"):
        return "发表稿已回收；网站状态待核对", False
    if item is None or item.get("status") == "unmapped":
        return "仅库内；网站状态未建立映射", False
    return "网站正式状态待核对", False


def _post_edits(library, note, item, state, notes, baseline, public):
    if note is None:
        return "发表稿缺失／已回收"
    if not public:
        return "全文尚未发布" if (item or {}).get("payload_formal") is not None or item is None or item.get("status") == "unmapped" else "待核对"
    formal_hash = (item or {}).get("payload_formal", {}).get("fields_hash")
    try:
        current_hash = _current_public_hash(library, note, notes)
        if formal_hash and current_hash == formal_hash:
            return "无"
        if formal_hash and baseline.get("remote_hash") == formal_hash and baseline.get("local_hash"):
            return "无" if current_hash == baseline["local_hash"] else "有"
        selected = state["publications"].get(note["id"])
        if selected and export_publication(selected)["content_hash"] == formal_hash:
            return "无" if current_hash == formal_hash else "有"
    except (ValueError, LibraryError):
        pass
    return "待核对"


def _post_url(sync_state, item, baseline, public):
    """An observed identity, never an edited local link, supplies the URL."""
    identity = baseline.get("identity", {})
    origin = sync_state.get("target")
    if not public or not origin or not identity.get("link"):
        return None
    parsed = urlsplit(origin)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    formal = (item or {}).get("payload_formal", {})
    if not formal.get("fields_hash") or baseline.get("remote_hash") != formal["fields_hash"]:
        return None
    locale = identity.get("locale", "zh")
    prefix = "" if locale == "zh" else "/" + quote(str(locale), safe="")
    return origin.rstrip("/") + prefix + "/post/" + quote(str(identity["link"]), safe="/")


def _article_navigation(library, state, notes, items, sync_state):
    posts = {key: value for key, value in notes.items() if value["properties"]["kind"] == "post"}
    for key, record in state["notes"].items():
        if record.get("kind") == "post" and not record.get("purged") and key not in posts:
            selected = state["publications"].get(key, {}).get("snapshot", {})
            posts[key] = {"id": key, "path": record.get("path", key),
                          "properties": {"kind": "post", "title": selected.get("title") or record.get("path") or key}}
    sources = _post_sources(library, state, notes, posts, sync_state)
    rows, blocks, footers, reverse = [], [], {}, {}
    for post in sorted(posts.values(), key=lambda row: (row["properties"]["title"].casefold(), row["id"])):
        post_id = post["id"]
        item, record = items.get(post_id), state["notes"].get(post_id, {})
        baseline = sync_state.get("baselines", {}).get(post_id, {})
        status, public = _post_status(item, record)
        edits = _post_edits(library, notes.get(post_id), item, state, notes, baseline, public)
        url = _post_url(sync_state, item, baseline, public)
        web = f"[网站文章]({url})" if url else "链接待确认" if public else "—"
        relation = sources[post_id]
        primary = next(iter(relation["primary"])) if len(relation["primary"]) == 1 else None
        source_ids = sorted(relation["ids"], key=lambda key: (key != primary, notes[key]["path"]))
        links = [("主来源：" if key == primary else "") + _wiki(notes[key]) for key in source_ids]
        source_text = "；".join(links) or "来源未记录"
        if relation["unknown"]:
            source_text += "；另有来源身份待核对"
        post_link = _wiki(post) if post_id in notes else f"已回收／缺失：{_label(post['path'])}"
        rows.append(f"| {_label(post['properties']['title'])} | {status} | {web} | {post_link.replace('|', chr(92) + '|')} | {source_text.replace('|', chr(92) + '|')} | {edits} |")
        blocks.append(f"{_label(post['properties']['title'])}：{status}；{web}；发表稿：{post_link}；私人原稿：{source_text}；Post未发布改动：{edits}。 ^publication-{post_id}")
        footer = f"> [!info] 发表信息\n> ![[发布与同步状态#^publication-{post_id}]]"
        if links:
            footer += "\n> 私人原稿：" + "；".join(links)
        if post_id in notes:
            footers[post_id] = footer
        for source_id in source_ids:
            reverse.setdefault(source_id, []).append((post_id, post))
    for source_id, related in reverse.items():
        lines = ["> [!info] 发表信息", "> 已有独立发表稿；两稿分别维护，后续修改需另行发表。"]
        for post_id, post in related:
            lines += [f"> ![[发布与同步状态#^publication-{post_id}]]", "> 发表稿：" + _wiki(post)]
        footers[source_id] = "\n".join(lines)
    lines = ["## 文章发表", "", "状态来自网站正式版本的API回读；静态页面更新可能稍后完成。私人原稿与独立发表稿分别维护。", "",
             "| 文章 | 网站正式状态 | 文章链接 | 发表稿路径 | 明确的私人原稿 | Post未发布改动 |", "| --- | --- | --- | --- | --- | --- |", *(rows or ["| 暂无文章 | | | | | |"]), "",
             "## 逐篇发表状态", ""]
    for block in blocks:
        lines += [block, ""]
    return lines, footers


def publication_overview(library, review, *, observed_at=None, apply=False):
    """Keep actual website visibility separate from selection and current edits.

    This does not fetch, modify or select website content. The supplied review
    must come from the configured standard API, not an inferred local status.
    """
    if review.get("mode") != "read-only-review" or not isinstance(review.get("items"), list):
        raise LibraryError("A complete read-only sync review is required for the overview")
    persisted_state = library._read_state()
    state, notes = _index(library)
    items = {item["id"]: item for item in review["items"]}
    if len(items) != len(review["items"]):
        raise LibraryError("Duplicate review identities")
    sync_path = library._path(".markdown-library/sync-state.json")
    sync_expected = sync_path.read_text(encoding="utf-8") if sync_path.exists() else None
    sync_state = json.loads(sync_expected) if sync_expected is not None else {}
    if sync_path.exists():
        mapped = set(sync_state.get("baselines", {}))
        if (mapped & set(notes)) - set(items):
            raise LibraryError("Review is incomplete; mapped records cannot be labeled as local-only")
    article_lines, footers = _article_navigation(library, state, notes, items, sync_state)
    groups = {"public": [], "private": [], "local": [], "review": []}
    counts = Counter()
    for note in sorted(notes.values(), key=lambda row: (row["properties"]["kind"], row["properties"]["title"].casefold(), row["id"])):
        note_id, kind = note["id"], note["properties"]["kind"]
        item = items.get(note_id)
        selected = state["publications"].get(note_id)
        flags = []
        current_differs = _differs_selection(library, note, selected)
        if current_differs:
            try:
                same_public = _current_public_hash(library, note, notes) == export_publication(selected)["content_hash"]
                flags.append("仅私人正文／本地属性／格式变化" if same_public else "当前稿有未选入发布版的修改")
            except (ValueError, LibraryError):
                flags.append("当前稿的发布字段需要检查")
        elif not selected:
            if kind == "note":
                flags.append("私人原稿单独保存；发表状态见对应Post" if note_id in footers else "私人笔记，不直接发表")
            else:
                flags.append("未选发布版本")
            if item and item.get("comparisons", {}).get("current_file_differs_observation"):
                flags.append("当前稿有库内修改")
        if selected and selected.get("state") in {"withdrawn", "deleted"}:
            flags.append("已选择撤回／删除")
        if item is None or item.get("status") == "unmapped":
            group = "local"
            public_label = "私人原稿；已有独立发表稿" if kind == "note" and note_id in footers else "仅库内，未建立网站映射"
        elif item.get("status") in {"local-missing-review", "remote-missing-review", "remote-deleted-review", "deleted-review", "lifecycle-review"}:
            group = "review"
            public_label = "删除／缺失状态待核对"
        else:
            formal, latest = item.get("payload_formal"), item.get("payload_latest")
            if formal is None:
                group, public_label = "review", "缺少正式版本回读"
            else:
                public = (not formal.get("deleted_at") and formal.get("state") == "published"
                          and (kind == "post" or formal.get("visibility") == "public"))
                group, public_label = ("public", "网站已公开") if public else ("private", "网站未公开")
                if latest and latest.get("deleted_at"):
                    flags.append("后台已进入回收站")
                elif latest and latest.get("fields_hash") != formal.get("fields_hash"):
                    flags.append("后台最新稿与正式版本不同")
            if item.get("pending"):
                flags.append("写入结果待核对")
            conflicts = item.get("merge", {}).get("conflicts", [])
            if conflicts:
                flags.append("冲突：" + "、".join(conflict["field"] for conflict in conflicts))
                counts["conflicts"] += 1
        counts[group] += 1
        if group == "public":
            counts[f"public_{kind}"] += 1
        if current_differs:
            counts["current_differs_from_selection"] += 1
        link_path = (note.get("storage_path") or note.get("catalog_path")) if note.get("catalog_backed") else note["path"]
        link = f"[{_label(note['properties']['title'])}]({quote(link_path, safe='/')})"
        groups[group].append(f"| {link} | {kind} | {public_label} | {_label('；'.join(flags) or '所选版本未变化')} |")
    for note_id in sorted(set(items) | set(state["notes"])):
        if note_id in notes:
            continue
        record = state["notes"].get(note_id, {})
        if record.get("purged") and items.get(note_id, {}).get("status") == "purged":
            counts["purged"] += 1
            continue
        # A recoverably merged private source is accounted for by its surviving
        # identity, not a missing-note incident requiring user intervention.
        if record.get('deleted') and record.get('merged_into') and note_id not in items:
            survivor, seen = record['merged_into'], {note_id}
            while survivor not in notes and survivor not in seen:
                seen.add(survivor)
                survivor = state['notes'].get(survivor, {}).get('merged_into')
                if survivor is None:
                    break
            if survivor in notes and note_id not in state['publications']:
                counts['merged'] += 1
                continue
        if (record.get('deleted') and record.get('trash') and note_id not in items
                and note_id not in state['publications']):
            # An explicit private-note deletion with a verified retained draft
            # is an accounted-for Trash entry, not a missing-file incident.
            retained = library._path(record['trash'])
            if retained.is_file():
                text = _read(retained)
                properties, _ = _split(text)
                if _hash(text) == record.get('sha256') and properties.get('id') == note_id:
                    counts['private_trash'] += 1
                    continue
        selected = state["publications"].get(note_id, {})
        title = selected.get("snapshot", {}).get("title") or record.get("path") or note_id
        groups["review"].append(f"| {_label(title)} | {_label(record.get('kind', ''))} | 本地当前文件缺失／已回收 | 按稳定身份核对；不推断网站删除 |")
        counts["review"] += 1
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    lines = [MARKER, "# 发布与同步状态", "", f"状态生成时间：{observed_at}。内容或状态变化时刷新；尚未同步到服务器的本机修改不在本页内。", "",
             f"- 网站已公开：**{counts['public']}** 项（{counts['public_work']} 个作品、{counts['public_post']} 篇文章）。",
             f"- 已对应后台、未公开：**{counts['private']}** 项；仅在内容库：**{counts['local']}** 项。",
             f"- 缺失／删除待核对：**{counts['review']}** 项；字段冲突：**{counts['conflicts']}** 项。", "",
             *([f"- 已按明确确认永久清理：**{counts['purged']}** 项，保留防止重新导入的身份记录。", ""] if counts['purged'] else []),
             *article_lines,
             "发布由明确选版决定，不由文件夹、评分、体验进度或普通保存决定。`status` 是作品体验进度；`payload_observed_status` 是历史观察值，都不是发布按钮。", "",
             "- Work：由稳定ID关联网站，保留标题与类型、评分、短评和体验记录；slug等网站资料由程序维护。正文先准备关联Post草稿，再明确发布。",
             "- Post：需要 `kind: post`、标题和固定 `link`（语言通常为 `zh`）；明确发布时才发布选定正文。",
             "- 普通 `kind: note` 笔记不进入网站。属性齐全只表示可准备发布，仍需明确选版。",
             "- 无工具身份的新笔记、模板、导航和技能说明不在上述注册清单内，也不会自动进入网站；附件仍由各系统分别管理。",
             "- Work 的网站资料及关联可由程序保存在隐藏身份元数据中；准备长评草稿时建立正文双链。Post 的 `related_works` 是程序派生的反向入口；双链不授权发布。", "",
             "[首页](首页.md) · [网站内容](网站内容.md) · [作品表格](works/索引.md) · [文章目录](发表稿/索引.md)", ""]
    for key, title in [("public", "网站已公开"), ("private", "已对应后台但未公开"), ("local", "仅在内容库"), ("review", "需要核对")]:
        lines += [f"## {title}", "", "| 内容 | 类型 | 网站状态 | 当前稿与同步提示 |", "| --- | --- | --- | --- |"]
        lines += groups[key] or ["| 暂无 | | | |"]
        lines.append("")
    content = "\n".join(lines)
    destination = library._path(REPORT_PATH)
    expected = destination.read_text(encoding="utf-8") if destination.exists() else None
    if expected is not None and not expected.startswith(MARKER + "\n"):
        raise LibraryError("Existing user-authored overview retained; choose another report path")
    # Avoid a new Sync version and possible AI reindex of the whole inventory
    # every two minutes when only its polling timestamp has changed.
    stable = lambda value: re.sub(r"^状态生成时间：[^\n]*\n", "", value, flags=re.MULTILINE)
    changed = expected is None or stable(expected) != stable(content)
    page_observed_at = observed_at
    if not changed:
        match = re.search(r"^状态生成时间：([^。]+)。", expected, flags=re.MULTILINE)
        if match:
            page_observed_at = match.group(1)
    navigation_updates = {}
    for note_id, footer in footers.items():
        note = notes[note_id]
        if note["properties"].get("style_anchor") is True or note["properties"].get("ai_edit") is False:
            continue
        updated = set_publication_navigation(note["markdown"], footer)
        if updated != note["markdown"]:
            navigation_updates[note_id] = {**note, "markdown": updated, "sha256": _hash(updated)}
    if apply and (changed or navigation_updates):
        with library._lock():
            actual = destination.read_text(encoding="utf-8") if destination.exists() else None
            current_sync = sync_path.read_text(encoding="utf-8") if sync_path.exists() else None
            if actual != expected or library._read_state() != persisted_state or current_sync != sync_expected:
                raise LibraryError("Overview or library state changed concurrently; retry with a fresh snapshot")
            # Verify every relation participant before writing anything, including
            # sources whose own footer was already up to date.
            for note_id in footers:
                note = notes[note_id]
                try:
                    library._assert_note_storage(note)
                except LibraryError as exc:
                    raise LibraryError("Publication navigation changed concurrently; retry with a fresh snapshot") from exc
            updated_state, writes = deepcopy(state), []
            for note_id, updated in navigation_updates.items():
                library._remember(updated_state, notes[note_id], "publication-navigation-original")
                library._remember(updated_state, updated, "publication-navigation")
                writes.append((updated["path"], updated["markdown"], notes[note_id]["sha256"]))
            if changed:
                writes.append((REPORT_PATH, content, _hash(expected) if expected is not None else None))
            library._commit(updated_state, writes)
    return {"applied": bool(apply), "changed": bool(changed or navigation_updates), "report_changed": changed,
            "navigation_updated": len(navigation_updates), "path": REPORT_PATH,
            "counts": dict(counts), "observed_at": page_observed_at, "checked_at": observed_at}
