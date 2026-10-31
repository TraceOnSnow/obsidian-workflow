from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library, LibraryError
from markdown_library.bootstrap import BootstrapError, bootstrap, source_id


def fixture():
    return {
        "target": "https://source.example",
        "rating_scale": 100,
        "posts": [{"id": 9, "title": "文章草稿", "link": "notes/original", "locale": "zh", "markdown": "DRAFT BODY\n",
                   "publishedAt": "2020-01-02T00:00:00.000Z", "_status": "draft", "frontmatter": {"math": True}, "protectedContent": False}],
        "works": [{"id": 17, "title": "作品", "slug": "original-work", "type": "book", "rating": 0, "status": None,
                   "visibility": "private", "_status": "draft", "note": "短感受", "article": 9,
                   "experiences": [{"id": "experience-1", "startedOn": None, "endedOn": "2020-03", "note": "旧备注"}],
                   "sources": [{"id": "source-1", "provider": "catalogue", "externalId": "abc", "url": None}],
                   "aliases": ["别名"], "cover": None}],
        "published": {
            "posts": [{"id": 9, "title": "文章公开版", "link": "notes/original", "locale": "zh", "markdown": "PUBLISHED BODY\n",
                       "publishedAt": "2020-01-02T00:00:00.000Z", "_status": "published", "frontmatter": None, "protectedContent": False}],
            "works": [{"id": 17, "title": "作品", "slug": "original-work", "type": "book", "rating": None, "status": None,
                       "visibility": "public", "_status": "published", "note": "公开感受", "article": 9,
                       "experiences": [{"id": "experience-1", "startedOn": None, "endedOn": "2020", "note": "旧备注"}]}],
        },
    }


class FakeClient:
    target = "https://source.example"

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def list_collection(self, name, *, draft):
        self.calls.append((name, draft))
        return deepcopy(self.snapshot[name] if draft else self.snapshot["published"][name])

    def create(self, *args, **kwargs):
        raise AssertionError("Bootstrap must never create remote content")

    update = create
    delete = create


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Library.initialize(Path(self.temporary.name) / "vault")

    def files(self):
        return {str(file.relative_to(self.library.vault)): (file.read_bytes(), file.stat().st_mtime_ns)
                for file in self.library.vault.rglob("*") if file.is_file()}

    def test_preview_is_readonly_and_does_not_emit_private_bodies(self):
        before = self.files()
        result = bootstrap(self.library, snapshot=fixture())
        self.assertEqual(result["mode"], "read-only-preview")
        self.assertEqual(result["counts"], {"posts": 1, "works": 1})
        self.assertEqual(self.files(), before)
        self.assertNotIn("DRAFT BODY", json.dumps(result))
        self.assertNotIn("PUBLISHED BODY", json.dumps(result))
        self.assertFalse(result["remote_writes"])

    def test_imports_current_drafts_retains_base_same_ids_without_publication(self):
        result = bootstrap(self.library, snapshot=fixture(), apply=True)
        post = self.library.get(result["results"][0]["id"])
        work = self.library.get(result["results"][1]["id"])
        self.assertIn("DRAFT BODY", post["markdown"])
        self.assertNotIn("PUBLISHED BODY", post["markdown"])
        self.assertEqual(work["properties"]["article"], post["id"])
        self.assertEqual(work["properties"]["rating"], 0)
        self.assertIsNone(work["properties"]["status"])
        self.assertEqual(work["properties"]["experiences"][0], fixture()["works"][0]["experiences"][0])
        self.assertEqual(work["properties"]["sources"][0]["id"], "source-1")
        self.assertEqual(self.library.publications(), [])
        self.assertFalse((self.library.vault / ".markdown-library/sync-state.json").exists())
        revision = result["results"][0]["base_revision_sha256"]
        retained = self.library.history_markdown(post["id"], revision)
        self.assertIn("PUBLISHED BODY", retained)
        self.assertIn(post["id"], retained)
        self.assertEqual(len(self.library.list_notes()), 2)

    def test_reruns_preserve_native_edits_and_renames_and_report_source_changes(self):
        first = bootstrap(self.library, snapshot=fixture(), apply=True)
        post = first["results"][0]
        file = self.library.vault / post["path"]
        renamed = self.library.vault / "改过名字.md"
        file.rename(renamed)
        edited = renamed.read_bytes() + "用户私下修改\n".encode()
        renamed.write_bytes(edited)
        changed = fixture()
        changed["posts"][0]["markdown"] = "REMOTE CHANGED BODY"
        second = bootstrap(self.library, snapshot=changed, apply=True)
        self.assertEqual(second["results"][0]["id"], post["id"])
        self.assertEqual(second["results"][0]["path"], "改过名字.md")
        self.assertTrue(second["results"][0]["source_changed"])
        self.assertEqual(renamed.read_bytes(), edited)
        self.assertEqual(len(self.library.list_notes()), 2)
        self.assertEqual(self.library.publications(), [])

    def test_source_identity_includes_target_collection_and_remote_id(self):
        first = bootstrap(self.library, snapshot=fixture(), apply=True)
        other = fixture()
        other["target"] = "https://different.example"
        second = bootstrap(self.library, snapshot=other, apply=True)
        self.assertNotEqual(first["results"][0]["id"], second["results"][0]["id"])
        self.assertNotEqual(source_id(other["target"], "posts", 9), source_id(other["target"], "works", 9))
        self.assertEqual(len(self.library.list_notes()), 4)

    def test_protected_post_password_theme_plaintext_preserved_locally_and_gated(self):
        snapshot = fixture()
        snapshot["posts"][0].update({"frontmatter": {"password": "PRIVATE PASSWORD", "unknownTheme": ["keep"]},
                                     "protectedContent": True, "markdown": "PRIVATE PLAINTEXT"})
        result = bootstrap(self.library, snapshot=snapshot, apply=True)
        post = self.library.get(result["results"][0]["id"])
        self.assertEqual(post["properties"]["frontmatter"], snapshot["posts"][0]["frontmatter"])
        self.assertIn("PRIVATE PLAINTEXT", post["markdown"])
        self.assertNotIn("PRIVATE PASSWORD", json.dumps(result))
        with self.assertRaisesRegex(LibraryError, "Protected"):
            self.library.publish(post["id"], expected_hash=post["sha256"])
        self.assertEqual(self.library.publications(), [])

    def test_unknown_base_and_legacy_works_status_are_not_invented(self):
        snapshot = fixture()
        del snapshot["published"]
        del snapshot["works"][0]["_status"]
        result = bootstrap(self.library, snapshot=snapshot, apply=True)
        work = self.library.get(result["results"][1]["id"])
        self.assertFalse(result["results"][1]["base_read"])
        self.assertIsNone(result["results"][1]["source_status"])
        self.assertNotIn("payload_observed_status", work["properties"])
        self.assertNotIn("base_revision_sha256", result["results"][1])

    def test_readonly_client_gets_both_complete_variants_never_mutates_remote(self):
        client = FakeClient(fixture())
        result = bootstrap(self.library, client=client, apply=True)
        self.assertEqual(set(client.calls), {("posts", True), ("works", True), ("posts", False), ("works", False)})
        self.assertEqual(len(result["results"]), 2)

    def test_incomplete_snapshot_duplicate_ids_or_target_credentials_fail_before_writes(self):
        variants = []
        incomplete = fixture(); incomplete["posts"] = {"docs": incomplete["posts"], "hasNextPage": True}; variants.append(incomplete)
        duplicate = fixture(); duplicate["works"] *= 2; variants.append(duplicate)
        credentials = fixture(); credentials["target"] = "https://user:secret@source.example"; variants.append(credentials)
        missing_collection = fixture(); del missing_collection["posts"]; variants.append(missing_collection)
        for snapshot in variants:
            before = self.files()
            with self.assertRaises(BootstrapError):
                bootstrap(self.library, snapshot=snapshot, apply=True)
            # Apply can create a lock; it must never create a note or receipt on invalid input.
            self.assertFalse((self.library.vault / ".markdown-library/bootstrap-state.json").exists())
            self.assertEqual(list(self.library.vault.glob("posts/*.md")), [])
            self.assertEqual(list(self.library.vault.glob("works/*.md")), [])

    def test_receipt_source_conflict_and_deleted_source_never_duplicate(self):
        result = bootstrap(self.library, snapshot=fixture(), apply=True)
        receipt_path = self.library.vault / ".markdown-library/bootstrap-state.json"
        receipts = json.loads(receipt_path.read_text())
        key = result["results"][0]["source_id"]
        original = deepcopy(receipts)
        receipts["imports"][key]["id"] = result["results"][1]["id"]
        receipt_path.write_text(json.dumps(receipts))
        with self.assertRaisesRegex(BootstrapError, "Conflicting"):
            bootstrap(self.library, snapshot=fixture(), apply=True)
        receipt_path.write_text(json.dumps(original))
        post = self.library.get(result["results"][0]["id"])
        self.library.delete(post["id"], expected_hash=post["sha256"])
        with self.assertRaisesRegex(BootstrapError, "missing/deleted"):
            bootstrap(self.library, snapshot=fixture(), apply=True)
        self.assertEqual(len(self.library.list_notes()), 1)

    def test_receipt_crash_recovers_source_identity_without_overwriting_user_edit(self):
        from markdown_library.bootstrap import _Receipts
        with patch.object(_Receipts, "save", side_effect=OSError("simulated receipt crash")):
            with self.assertRaises(OSError):
                bootstrap(self.library, snapshot=fixture(), apply=True)
        post = self.library.list_notes()[0]
        file = self.library.vault / post["path"]
        edited = file.read_bytes() + b"user edit"
        file.write_bytes(edited)
        resumed = bootstrap(self.library, snapshot=fixture(), apply=True)
        self.assertEqual(resumed["results"][0]["id"], post["id"])
        self.assertEqual(file.read_bytes(), edited)
        self.assertEqual(len(self.library.list_notes()), 2)

    def test_retain_revision_guards_identity_current_hash_and_publication(self):
        current = self.library.create("private current", title="Post", kind="post")
        revision = current["markdown"].replace("private current", "imported historic source")
        with self.assertRaisesRegex(LibraryError, "Revision conflict"):
            self.library.retain_revision(current["id"], revision, expected_hash="stale")
        retained = self.library.retain_revision(current["id"], revision, expected_hash=current["sha256"], source={"type": "fixture"})
        self.assertEqual(self.library.get(current["id"])["sha256"], current["sha256"])
        self.assertEqual(self.library.history_markdown(current["id"], retained["sha256"]), revision)
        self.assertEqual(self.library.publications(), [])

    def test_cli_snapshot_preview_and_apply_are_local_only(self):
        snapshot_path = Path(self.temporary.name) / "source.json"
        snapshot_path.write_text(json.dumps(fixture()))
        command = [sys.executable, "-m", "markdown_library", "--vault", str(self.library.vault), "bootstrap", "--snapshot", str(snapshot_path)]
        preview = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(preview.stdout)["mode"], "read-only-preview")
        applied = subprocess.run(command + ["--apply"], capture_output=True, text=True, check=True)
        self.assertFalse(json.loads(applied.stdout)["remote_writes"])
        self.assertEqual(len(self.library.list_notes()), 2)

    def test_existing_same_target_sync_mapping_reuses_local_note(self):
        current = self.library.create("LOCAL EDITED BODY", title="Local title", kind="post", properties={"link": "notes/original", "locale": "zh"})
        sync_path = self.library.vault / ".markdown-library/sync-state.json"
        sync_path.write_text(json.dumps({"schema_version": 1, "target": fixture()["target"], "baselines": {
            current["id"]: {"collection": "posts", "remote_id": 9, "state": "published"},
        }, "pending": {}, "reverse_imports": {}}))
        original_sync = sync_path.read_bytes()
        result = bootstrap(self.library, snapshot=fixture(), apply=True)
        self.assertEqual(result["results"][0]["id"], current["id"])
        self.assertEqual(self.library.get(current["id"])["markdown"], current["markdown"])
        self.assertEqual(sync_path.read_bytes(), original_sync)
        self.assertEqual(len(self.library.list_notes()), 2)

    def test_retained_base_never_changes_existing_selected_publication(self):
        current = self.library.create("PRIVATE WORK BODY", title="Work", kind="work", properties={"slug": "work", "type": "book", "rating": 9})
        selected = self.library.publish(current["id"], expected_hash=current["sha256"])
        revision = current["markdown"].replace("rating: 9", "rating: 0")
        self.library.retain_revision(current["id"], revision, expected_hash=current["sha256"], source={"type": "remote-base"})
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.library.get(current["id"])["sha256"], current["sha256"])

    def test_identical_apply_reuses_verified_index_and_does_not_rewrite_receipts(self):
        from markdown_library.bootstrap import _Receipts
        initial = bootstrap(self.library, snapshot=fixture(), apply=True)
        before = self.files()
        with patch.object(self.library, "get", wraps=self.library.get) as get, \
             patch.object(self.library, "retain_revision", wraps=self.library.retain_revision) as retain, \
             patch.object(_Receipts, "save", side_effect=AssertionError("unchanged receipts must not be rewritten")):
            repeated = bootstrap(self.library, snapshot=fixture(), apply=True)
        self.assertEqual(get.call_count, 0)
        self.assertEqual(retain.call_count, 0)
        self.assertEqual(self.files(), before)
        self.assertTrue(all(row["reused_verified_checkpoint"] for row in repeated["results"]))
        self.assertEqual([row["id"] for row in initial["results"]], [row["id"] for row in repeated["results"]])

    def test_fast_path_native_edit_uses_ordinary_api_and_preserves_current_body(self):
        initial = bootstrap(self.library, snapshot=fixture(), apply=True)
        post = initial["results"][0]
        file = self.library.vault / post["path"]
        edited = file.read_bytes() + b"native edit"
        file.write_bytes(edited)
        with patch.object(self.library, "get", wraps=self.library.get) as get, \
             patch.object(self.library, "retain_revision", wraps=self.library.retain_revision) as retain:
            repeated = bootstrap(self.library, snapshot=fixture(), apply=True)
        self.assertNotIn("reused_verified_checkpoint", repeated["results"][0])
        self.assertTrue(get.called)
        self.assertTrue(retain.called)
        self.assertEqual(file.read_bytes(), edited)
        self.assertIn(self.library.get(post["id"])["sha256"], {row["sha256"] for row in self.library.history(post["id"])})

    def test_fast_path_source_or_relationship_changes_use_normal_retention(self):
        bootstrap(self.library, snapshot=fixture(), apply=True)
        changed = fixture()
        changed["posts"][0]["markdown"] = "NEW REMOTE DRAFT"
        changed["works"][0]["article"] = None
        changed["published"]["works"][0]["article"] = None
        with patch.object(self.library, "get", wraps=self.library.get) as get, \
             patch.object(self.library, "retain_revision", wraps=self.library.retain_revision) as retain:
            result = bootstrap(self.library, snapshot=changed, apply=True)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(retain.call_count, 2)
        self.assertTrue(all(row["source_changed"] for row in result["results"]))
        self.assertTrue(all("reused_verified_checkpoint" not in row for row in result["results"]))

    def test_fast_path_missing_object_is_repaired_and_corrupt_object_fails_visibly(self):
        initial = bootstrap(self.library, snapshot=fixture(), apply=True)
        post = initial["results"][0]
        checkpoint = self.library.vault / f".markdown-library/objects/{post['base_revision_sha256']}.md"
        original = checkpoint.read_bytes()
        checkpoint.unlink()
        with patch.object(self.library, "retain_revision", wraps=self.library.retain_revision) as retain:
            repaired = bootstrap(self.library, snapshot=fixture(), apply=True)
        self.assertTrue(retain.called)
        self.assertNotIn("reused_verified_checkpoint", repaired["results"][0])
        self.assertEqual(checkpoint.read_bytes(), original)
        checkpoint.write_bytes(b"CORRUPT CHECKPOINT")
        with self.assertRaisesRegex(LibraryError, "Corrupted history object"):
            bootstrap(self.library, snapshot=fixture(), apply=True)
        self.assertEqual(checkpoint.read_bytes(), b"CORRUPT CHECKPOINT")

    def test_fast_path_unregistered_provenance_uses_normal_retention(self):
        initial = bootstrap(self.library, snapshot=fixture(), apply=True)
        post = initial["results"][0]
        state_path = self.library.vault / ".markdown-library/state.json"
        state = json.loads(state_path.read_text())
        retained = next(row for row in state["notes"][post["id"]]["history"] if row.get("action") == "retained")
        retained["source"]["type"] = "unrelated-checkpoint"
        state_path.write_text(json.dumps(state))
        with patch.object(self.library, "retain_revision", wraps=self.library.retain_revision) as retain:
            result = bootstrap(self.library, snapshot=fixture(), apply=True)
        self.assertTrue(retain.called)
        self.assertNotIn("reused_verified_checkpoint", result["results"][0])


if __name__ == "__main__":
    unittest.main()
