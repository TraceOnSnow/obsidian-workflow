"""Bounded semantic routing with a disposable real Workspace ledger and mocked HTTP."""
from contextlib import contextmanager
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("inbox_semantic", Path(__file__).parents[1] / "scripts/inbox-semantic-organize.py")
semantic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(semantic)
from markdown_library import Library
from markdown_library.inbox import organize, KEY
from chatgpt_pipeline import bulk, notes


class SemanticInboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        root = patch.object(bulk, "__file__", str(self.base / "src/chatgpt_pipeline/bulk.py"))
        root.start()
        self.addCleanup(root.stop)
        self.vault = self.base / "vault"
        self.library = Library.initialize(self.vault)
        self.category = "20_项目与构想/游戏设计"
        (self.vault / self.category).mkdir(parents=True)
        self.workspace = self.base / "runtime/workbench"
        self.workspace.mkdir(parents=True)
        (self.workspace / "jobs").mkdir()
        bulk.save(self.workspace / "config.json", notes.config())
        with sqlite3.connect(self.workspace / "state.sqlite3") as db:
            db.execute("CREATE TABLE calls(job_id TEXT,stage TEXT,request_hash TEXT,reservation TEXT,started_at TEXT,cost TEXT,response_id TEXT,PRIMARY KEY(job_id,stage))")
            db.execute("CREATE TABLE jobs(id TEXT)")
            db.execute("CREATE TABLE items(id TEXT)")
        credential = patch.object(notes, "credential", return_value="test-only-key")
        credential.start()
        self.addCleanup(credential.stop)
        self.clock = 2_000_000_000.0
        organize(self.vault, apply=True, now=self.clock)

    def note(self, name="new.md"):
        return self.library.create("# 新构想\n\n我想把每回合的选择做成朋友之间的猜词游戏，尚未实现。\n",
                                   title="一个新想法", path="notes/" + name)

    def observe(self):
        organize(self.vault, apply=True, now=self.clock + 1)

    def reply(self, category=None):
        raw = json.dumps({"id": "test-generation", "model": bulk.MODEL, "usage": {"cost": 0.001},
                          "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                              "category": self.category if category is None else category})}}]}).encode()
        stream = io.BytesIO(raw)
        stream.status = 200
        return stream

    def run_route(self, **kwargs):
        return semantic.run(self.vault, self.workspace, "not-a-real-key-file", apply=True,
                            semantic=True, now=self.clock + 62, **kwargs)

    def calls(self):
        with sqlite3.connect(self.workspace / "state.sqlite3") as db:
            return db.execute("SELECT job_id,stage,cost FROM calls").fetchall()

    def test_actual_shared_transport_bills_route_and_never_registers_ui_jobs_or_items(self):
        note = self.note()
        self.observe()
        original_lock = Library._lock
        in_lock = []
        @contextmanager
        def track(library, *args, **kwargs):
            with original_lock(library, *args, **kwargs):
                in_lock.append(True)
                try:
                    yield
                finally:
                    in_lock.pop()
        def send(*args, **kwargs):
            self.assertFalse(in_lock, "Network request held the Library lock")
            return self.reply()
        with patch.object(Library, "_lock", track), patch("urllib.request.OpenerDirector.open", side_effect=send) as request:
            result = self.run_route()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["counts"], {"moved": 1})
        self.assertEqual(result["model_calls"], 1)
        current = self.library.get(note["id"])
        self.assertEqual(current["markdown"], note["markdown"])
        self.assertEqual(current["path"], self.category + "/new.md")
        calls = self.calls()
        self.assertEqual(calls[0][1:], ("route", "0.001"))
        with sqlite3.connect(self.workspace / "state.sqlite3") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertTrue((self.workspace / "jobs" / calls[0][0] / "external-task.json").exists())

    def test_edit_during_request_keeps_new_current_note_in_inbox(self):
        note = self.note()
        self.observe()
        edited = note["markdown"] + "\n正在改另一个主题。\n"
        def send(*args, **kwargs):
            self.library.replace(note["id"], edited, expected_hash=note["sha256"])
            return self.reply()
        with patch("urllib.request.OpenerDirector.open", side_effect=send):
            result = self.run_route()
        self.assertEqual(result["counts"], {"waiting": 1})
        self.assertEqual((self.vault / note["path"]).read_text(), edited)
        self.assertFalse((self.vault / self.category / "new.md").exists())

    def test_budget_blocked_uses_existing_two_dollar_cap_without_request(self):
        note = self.note()
        self.observe()
        with sqlite3.connect(self.workspace / "state.sqlite3") as db:
            db.execute("INSERT INTO calls(job_id,stage,cost,reservation) VALUES('old','draft','2','0')")
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Budget blocked")):
            result = self.run_route()
        self.assertEqual(result["items"][0]["reason"], "semantic_budget_blocked")
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue((self.vault / note["path"]).exists())

    def test_unknown_attempt_not_retried_even_if_category_set_changes(self):
        note = self.note()
        self.observe()
        with patch("urllib.request.OpenerDirector.open", side_effect=TimeoutError("unknown outcome")) as request:
            result = self.run_route()
            (self.vault / "20_项目与构想/其他构想").mkdir()
            again = self.run_route()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(self.calls()), 1)
        self.assertIsNone(self.calls()[0][2])
        self.assertEqual(result["items"][0]["reason"], "semantic_unknown_attempt")
        self.assertEqual(again["model_calls"], 0)
        self.assertTrue((self.vault / note["path"]).exists())

    def test_old_notes_and_read_only_modes_make_no_call(self):
        # Baseline an old identity in a second isolated Vault.
        second = self.base / "old-vault"
        old_library = Library.initialize(second)
        old_library.create("# 旧内容\n\n已有正文", title="旧构想", path="notes/old.md")
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No call")):
            semantic.run(second, self.workspace, apply=True, semantic=True, now=self.clock)
            result = semantic.run(second, self.workspace, apply=True, semantic=True, now=self.clock + 300)
            note = self.note()
            self.observe()
            before = (self.vault / ".markdown-library/state.json").read_bytes()
            semantic.run(self.vault, self.workspace, apply=False, semantic=True, now=self.clock + 62)
            self.assertEqual(before, (self.vault / ".markdown-library/state.json").read_bytes())
        self.assertEqual(result["counts"], {"existing": 1})
        self.assertFalse(self.calls())

    def test_out_of_allowlist_category_is_rejected_without_move_or_retry(self):
        note = self.note()
        self.observe()
        with patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **k: self.reply("../../outside")) as request:
            result = self.run_route()
            self.run_route()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["items"][0]["reason"], "semantic_category_not_allowed")
        self.assertTrue((self.vault / note["path"]).exists())

    def test_max_four_combines_deterministic_and_semantic_moves(self):
        category = self.vault / "30_知识与参考/算法与编程/模板"
        category.mkdir(parents=True)
        for index in range(2):
            self.library.create("# 模板\n\n已有代码解释", title="我的做题模板", path=f"notes/clear-{index}.md")
        for index in range(4):
            self.note(f"unknown-{index}.md")
        self.observe()
        with patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **k: self.reply()) as request:
            result = self.run_route()
        self.assertEqual(request.call_count, 2)
        self.assertEqual(result["counts"].get("moved"), 4)
        self.assertEqual(len(self.calls()), 2)

    def test_needs_review_and_links_requiring_repair_stay_queued(self):
        note = self.note()
        self.observe()
        with patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **k: self.reply("needs_review")) as request:
            result = self.run_route()
            self.run_route()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["items"][0]["reason"], "semantic_needs_review")
        self.assertTrue((self.vault / note["path"]).exists())
        another = self.note("linked.md")
        (self.vault / "references.md").write_text("[[notes/linked|来源]]\n")
        organize(self.vault, apply=True, now=self.clock + 70)
        with patch("urllib.request.OpenerDirector.open", side_effect=lambda *a, **k: self.reply()):
            result = semantic.run(self.vault, self.workspace, "fake-key", apply=True, semantic=True, now=self.clock + 131)
        row = next(row for row in result["items"] if row["id"] == another["id"])
        self.assertEqual(row["reason"], "link_repair_or_name_review_required")
        self.assertTrue((self.vault / another["path"]).exists())


if __name__ == "__main__":
    unittest.main()
