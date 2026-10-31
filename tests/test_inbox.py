"""Disposable Vault tests for the bounded deterministic inbox trigger."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library, LibraryError
from markdown_library.core import _hash
from markdown_library.inbox import organize, KEY, ARCHIVE


class InboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / "vault"
        self.library = Library.initialize(self.vault)
        for category in ("notes", "00_收件箱", "30_知识与参考/算法与编程/模板",
                         "30_知识与参考/数学与优化", "20_项目与构想/课程项目/Atlas", ARCHIVE):
            (self.vault / category).mkdir(parents=True, exist_ok=True)
        self.clock = 2_000_000_000.0

    def note(self, name="new.md", title="我的 C++17 做题模板", body="# 私人正文\n\n- [x] 用户勾选\n\n完整记录。\n", kind="note"):
        note = self.library.create(body, title=title, path="notes/" + name, kind=kind, properties={"custom": "keep"})
        return note

    def baseline(self):
        return organize(self.vault, apply=True, now=self.clock)

    def observe(self, seconds=1, **kwargs):
        return organize(self.vault, apply=True, now=self.clock + seconds, **kwargs)

    def snapshot(self):
        return {p.relative_to(self.vault).as_posix(): p.read_bytes() for p in self.vault.rglob("*") if p.is_file()}

    def test_status_read_only_and_baseline_never_reorganizes_existing_notes(self):
        old = self.note()
        before = self.snapshot()
        result = organize(self.vault, now=self.clock)
        self.assertEqual(before, self.snapshot())
        self.assertFalse(result["initialized"])
        self.assertEqual(result["items"][0]["reason"], "existing_note_requires_user_batch")
        self.baseline()
        for time in (1, 65, 500):
            result = self.observe(time)
            self.assertEqual(result["counts"], {"existing": 1})
        self.assertTrue((self.vault / old["path"]).exists())

    def test_two_scans_and_60_seconds_then_one_guarded_commit_and_byte_preserving_move(self):
        self.baseline()
        note = self.note()
        before = (self.vault / note["path"]).read_bytes()
        with self.library._lock():
            state = self.library._load()
            state["sources"]["source:test"] = {"id": note["id"], "source": {"message": "retained"}}
            state["publications"]["old-published"] = {"sentinel": "keep"}
            self.library._save(state)
        self.assertEqual(self.observe(1)["counts"], {"waiting": 1})
        self.assertEqual(self.observe(40)["counts"], {"waiting": 1})
        original_commit = Library._commit
        with patch.object(Library, "_commit", autospec=True, side_effect=original_commit) as commit, \
                patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No model/network")):
            result = self.observe(62)
            self.assertEqual(commit.call_count, 1)
        row = result["items"][0]
        self.assertEqual(row["status"], "moved")
        self.assertEqual((self.vault / row["destination"]).read_bytes(), before)
        self.assertFalse((self.vault / note["path"]).exists())
        state = self.library._read_state()
        self.assertEqual(state["notes"][note["id"]]["path"], row["destination"])
        self.assertEqual(state["sources"]["source:test"]["source"], {"message": "retained"})
        self.assertEqual(state["publications"], {"old-published": {"sentinel": "keep"}})
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(self.observe(200)["items"], [])

    def test_edit_resets_settling_and_partial_sync_is_waiting(self):
        self.baseline()
        note = self.note()
        self.observe(1)
        self.library.replace(note["id"], note["markdown"] + "用户新改稿。\n", expected_hash=note["sha256"])
        self.assertEqual(self.observe(62)["counts"], {"waiting": 1})
        self.assertEqual(self.observe(100)["counts"], {"waiting": 1})
        self.assertEqual(self.observe(123)["counts"], {"moved": 1})
        partial = self.note("partial.md", body="# 未同步完\n\n```cpp\npartial code")
        result = self.observe(200)
        self.assertEqual(result["items"][0]["reason"], "incomplete_body")
        self.assertTrue((self.vault / partial["path"]).exists())

    def test_ambiguous_titles_queue_and_do_not_starve_later_clear_notes(self):
        self.baseline()
        unknown = self.note("unknown.md", title="一个新想法")
        conflict = self.note("conflict.md", title="核函数与我的做题模板")
        self.note("clear.md")
        self.observe(1)
        result = self.observe(62)
        by_id = {row["id"]: row for row in result["items"]}
        self.assertEqual(by_id[unknown["id"]]["reason"], "category_uncertain")
        self.assertEqual(by_id[conflict["id"]]["reason"], "category_conflict")
        self.assertEqual(result["counts"], {"needs_review": 2, "moved": 1})
        state = self.library._read_state()
        self.assertEqual(len(state[KEY]["queue"]), 2)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("一个新想法", serialized)
        self.assertNotIn("完整记录", serialized)

    def test_link_helper_allows_unique_short_links_but_queues_real_repairs(self):
        self.baseline()
        short = self.note("short.md")
        full = self.note("full.md")
        ref = self.vault / "10_个人记录"
        ref.mkdir(exist_ok=True)
        file = ref / "references.md"
        file.write_text("[[short#正文|别名]]\n[[notes/full|明确路径]]\n`[[notes/short]]`\n")
        before = file.read_bytes()
        self.observe(1)
        result = self.observe(62)
        by_id = {row["id"]: row for row in result["items"]}
        self.assertEqual(by_id[short["id"]]["status"], "moved")
        self.assertEqual(by_id[full["id"]]["reason"], "link_repair_or_name_review_required")
        self.assertEqual(file.read_bytes(), before)
        self.assertTrue((self.vault / full["path"]).exists())

    def test_only_inbox_notes_known_dirs_and_max_four_no_other_types(self):
        self.baseline()
        for index in range(6):
            self.note(str(index) + ".md")
        self.note("work.md", kind="work")
        self.library.create("旧主题正文", title="核函数", path="30_知识与参考/数学与优化/旧主题.md")
        self.observe(1)
        result = self.observe(62)
        self.assertEqual(result["counts"], {"waiting": 2, "moved": 4})
        self.assertEqual(self.observe(123)["counts"], {"moved": 2})
        self.assertTrue((self.vault / "notes/work.md").exists())
        self.assertTrue((self.vault / "30_知识与参考/数学与优化/旧主题.md").exists())

    def test_archive_is_move_with_base_filter_report_no_delete_or_publication(self):
        self.baseline()
        note = self.note(title="归档：过期的临时清单")
        self.observe(1)
        result = self.observe(62)
        self.assertTrue(result["archive_filter_required"])
        self.assertEqual(result["items"][0]["destination"], ARCHIVE + "/new.md")
        record = self.library._read_state()["notes"][note["id"]]
        self.assertFalse(record["deleted"])
        self.assertEqual((self.vault / record["path"]).read_text(), note["markdown"])

    def test_editor_race_is_guarded_and_no_source_edit_is_overwritten(self):
        self.baseline()
        note = self.note()
        self.observe(1)
        original_commit = Library._commit
        edited = note["markdown"] + "临提交前的本机改动。\n"
        def race(library, state, writes=None, removes=None):
            if writes:
                (self.vault / note["path"]).write_text(edited)
            return original_commit(library, state, writes, removes)
        with patch.object(Library, "_commit", autospec=True, side_effect=race), self.assertRaises(LibraryError):
            self.observe(62)
        self.assertEqual((self.vault / note["path"]).read_text(), edited)
        self.assertFalse((self.vault / "30_知识与参考/算法与编程/模板/new.md").exists())

    def test_duplicate_identity_outside_inbox_never_moves_and_missing_category_not_created(self):
        self.baseline()
        note = self.note()
        copied = self.vault / "30_知识与参考/数学与优化/other-name.md"
        copied.write_text(note["markdown"])
        self.observe(1)
        result = self.observe(62)
        self.assertEqual(result["items"][0]["reason"], "link_repair_or_name_review_required")
        self.assertTrue((self.vault / note["path"]).exists())
        copied.unlink()
        self.note("category.md", title="面试复盘")
        self.observe(100)
        result = self.observe(162)
        row = next(item for item in result["items"] if item["path"].endswith("category.md"))
        self.assertEqual(row["reason"], "category_directory_missing")
        self.assertFalse((self.vault / "10_个人记录/求职与实习/面试复盘").exists())

    def test_returned_organized_note_is_existing_and_navigation_ignored(self):
        self.baseline()
        note = self.note()
        self.observe(1)
        destination = self.observe(62)["items"][0]["destination"]
        (self.vault / destination).rename(self.vault / note["path"])
        with self.library._lock():
            state = self.library._load()
            state["notes"][note["id"]]["path"] = note["path"]
            self.library._save(state)
        self.note("索引.md")
        result = self.observe(200)
        self.assertEqual(result["counts"], {"existing": 1})
        self.assertEqual(len(result["items"]), 1)


if __name__ == "__main__":
    unittest.main()
