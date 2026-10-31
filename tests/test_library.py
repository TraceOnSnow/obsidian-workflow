import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library, LibraryError
from markdown_library.core import _split


def import_worker(vault):
    return Library(vault).import_note("first body", source_id="same-source", title="共同来源")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = Path(self.temporary.name) / "vault"
        self.library = Library.initialize(self.vault)

    def test_explicit_initialization_and_constructor_readonly(self):
        missing = Path(self.temporary.name) / "missing"
        library = Library(missing)
        self.assertFalse(missing.exists())
        with self.assertRaisesRegex(LibraryError, "not initialized"):
            library.validate()
        with self.assertRaises(LibraryError):
            library.list_notes()
        self.assertFalse(missing.exists())
        for unsafe in ("relative/vault", "/", str(missing / ".." / "escape")):
            with self.assertRaises(LibraryError):
                Library(unsafe)

    def test_default_locations_use_inbox_and_publication_drafts(self):
        for kind, folder in [("note", "00_收件箱"), ("post", "发表稿"), ("work", "works")]:
            note = self.library.create("Body", title="New " + kind, kind=kind)
            self.assertTrue(note["path"].startswith(folder + "/"))
        imported = self.library.import_note("Imported", source_id="new-import", title="Import")
        self.assertTrue(imported["path"].startswith("00_收件箱/"))
        self.assertFalse((self.vault / "notes").exists())
        self.assertFalse((self.vault / "posts").exists())

    def test_repeat_import_preserves_edit_metadata_and_private_source(self):
        first = self.library.import_note("---\ntags: [思考]\ncustom: yes\n---\n# 初稿\n", source_id="one", title="中文标题", source={"upstream": "/private/source"})
        file = self.vault / first["path"]
        native = file.read_text(encoding="utf-8") + "用户新口述\n"
        file.write_text(native, encoding="utf-8")
        repeated = self.library.import_note("stale content", source_id="one", title="stale title")
        self.assertEqual(repeated["outcome"], "existing")
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(repeated["markdown"], native)
        self.assertEqual(repeated["sha256"], hashlib.sha256(native.encode()).hexdigest())
        self.assertEqual(repeated["properties"]["tags"], ["思考"])
        self.assertNotIn("/private/source", native)
        self.assertIn("/private/source", (self.vault / ".markdown-library/state.json").read_text())
        self.assertEqual(len(self.library.history(first["id"])), 2)

    def test_native_rename_search_and_duplicate_id(self):
        created = self.library.create("needle", title="A")
        original = self.vault / created["path"]
        renamed = self.vault / "Renamed.md"
        original.rename(renamed)
        found = self.library.get(created["id"])
        self.assertEqual(found["path"], "Renamed.md")
        self.assertEqual(len(self.library.list_notes("NEEDLE")), 1)
        (self.vault / "duplicate.md").write_text(found["markdown"], encoding="utf-8")
        with self.assertRaisesRegex(LibraryError, "Duplicate stable id"):
            self.library.list_notes()

    def test_repeated_scans_reuse_parsing_but_read_native_content_and_check_identity(self):
        created = self.library.create("body", title="A")
        file = self.vault / created["path"]
        original_stat = file.stat()
        with patch("markdown_library.core._split", wraps=_split) as parser:
            self.library.get(created["id"])
            repeated = self.library.get(created["id"])
            self.assertEqual(parser.call_count, 1)
            repeated["properties"]["title"] = "caller mutation"
            self.assertEqual(self.library.get(created["id"])["properties"]["title"], "A")
            file.write_bytes(file.read_bytes().replace(b"title: A", b"title: B"))
            os.utime(file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            edited = self.library.get(created["id"])
            self.assertEqual(edited["properties"]["title"], "B")
            self.assertEqual(parser.call_count, 2)
            renamed = self.vault / "renamed.md"
            file.rename(renamed)
            self.assertEqual(self.library.get(created["id"])["path"], "renamed.md")
            (self.vault / "duplicate.md").write_bytes(renamed.read_bytes())
            with self.assertRaisesRegex(LibraryError, "Duplicate stable id"):
                self.library.list_notes()

    def test_replace_restore_conflicts_and_native_backup(self):
        created = self.library.create("original", title="A")
        file = self.vault / created["path"]
        file.write_text(created["markdown"] + "native edit", encoding="utf-8")
        with self.assertRaisesRegex(LibraryError, "Revision conflict"):
            self.library.replace(created["id"], created["markdown"], expected_hash=created["sha256"])
        current = self.library.get(created["id"])
        restored = self.library.restore(created["id"], created["sha256"], expected_hash=current["sha256"])
        self.assertEqual(restored["markdown"], created["markdown"])
        self.assertEqual(self.library.history_markdown(created["id"], current["sha256"]), current["markdown"])
        with self.assertRaisesRegex(LibraryError, "preserve stable id"):
            self.library.replace(created["id"], created["markdown"].replace(created["id"], "wrong-id"), expected_hash=restored["sha256"])

    def test_unsafe_paths_and_symlinks(self):
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        link = Path(self.temporary.name) / "link"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(LibraryError):
            Library(link)
        for relative in ("../escaped.md", str(outside / "absolute.md"), ".hidden/note.md"):
            with self.assertRaises(LibraryError):
                self.library.create("x", title="A", path=relative)
        (self.vault / "linked.md").symlink_to(outside / "file.md")
        with self.assertRaisesRegex(LibraryError, "Symlink"):
            self.library.list_notes()
        self.assertEqual(list(outside.iterdir()), [])

    def test_hidden_state_symlink_rejected(self):
        state = self.vault / ".markdown-library/state.json"
        state.unlink()
        state.symlink_to(Path(self.temporary.name) / "outside.json")
        with self.assertRaisesRegex(LibraryError, "Symlinks"):
            self.library.list_notes()

    def test_publish_snapshot_and_work_body_privacy(self):
        work = self.library.create("PRIVATE PROSE", title="作品", kind="work", properties={
            "rating": None, "note": "公开短感受", "private": "PRIVATE FIELD", "status": "planned",
            "experiences": [{"id": "row-one", "startedOn": "2020", "endedOn": "2021-03", "note": None}],
        })
        self.assertIsNone(self.library.publication(work["id"]))
        self.library.publish(work["id"], expected_hash=work["sha256"])
        snapshot = self.library.publication(work["id"])
        self.assertNotIn("PRIVATE", json.dumps(snapshot))
        self.assertNotIn("markdown", snapshot)
        self.assertIsNone(snapshot["properties"]["rating"])
        self.assertEqual(snapshot["properties"]["experiences"][0]["endedOn"], "2021-03")
        newer = self.library.replace(work["id"], work["markdown"].replace("公开短感受", "新版草稿"), expected_hash=work["sha256"])
        self.assertEqual(self.library.publication(work["id"]), snapshot)
        snapshot["properties"]["note"] = "MUTATED RETURN VALUE"
        self.assertEqual(self.library.publication(work["id"])["properties"]["note"], "公开短感受")
        self.library.publish(work["id"], expected_hash=newer["sha256"])
        self.assertEqual(self.library.publication(work["id"])["properties"]["note"], "新版草稿")
        self.library.unpublish(work["id"])
        self.assertIsNone(self.library.publication(work["id"]))
        self.assertEqual(self.library.publications()[0]["state"], "withdrawn")

    def test_post_publication_and_note_restriction(self):
        post = self.library.create("# Public post\n", title="Post", kind="post", properties={"slug": "a", "secret": "hidden"})
        record = self.library.publish(post["id"], expected_hash=post["sha256"])
        self.assertEqual(record["snapshot"]["markdown"], "# Public post\n")
        self.assertNotIn("secret", record["snapshot"]["properties"])
        note = self.library.create("private", title="note")
        with self.assertRaises(LibraryError):
            self.library.publish(note["id"], expected_hash=note["sha256"])

    def test_delete_recover_stays_unpublished_and_repeat_import_no_resurrection(self):
        post = self.library.import_note("body", source_id="post", title="Post", kind="post")
        self.library.publish(post["id"], expected_hash=post["sha256"])
        self.library.delete(post["id"], expected_hash=post["sha256"])
        self.assertFalse((self.vault / post["path"]).exists())
        self.assertEqual(self.library.publications()[0]["state"], "deleted")
        with self.assertRaises(LibraryError):
            self.library.import_note("stale", source_id="post", title="Post")
        with self.assertRaises(LibraryError):
            self.library.recover(post["id"], expected_hash="stale")
        recovered = self.library.recover(post["id"], expected_hash=post["sha256"])
        self.assertEqual(recovered["markdown"], post["markdown"])
        self.assertIsNone(self.library.publication(post["id"]))
        self.library.delete(post["id"], expected_hash=recovered["sha256"])
        (self.vault / recovered["path"]).write_text("occupied", encoding="utf-8")
        with self.assertRaisesRegex(LibraryError, "occupied"):
            self.library.recover(post["id"], expected_hash=post["sha256"])

    def test_concurrent_duplicate_imports_processes(self):
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(import_worker, [str(self.vault)] * 8))
        self.assertEqual(len({record["id"] for record in results}), 1)
        self.assertEqual(sum(record["outcome"] == "created" for record in results), 1)
        self.assertEqual(len(self.library.list_notes()), 1)

    def test_interrupted_import_recovers_without_duplicate(self):
        with patch.object(self.library, "_save", side_effect=OSError("simulated crash after file write")):
            with self.assertRaises(LibraryError):
                self.library.import_note("body", source_id="crash", title="Crash")
        self.assertTrue((self.vault / ".markdown-library/transaction.json").exists())
        result = self.library.import_note("stale", source_id="crash", title="Crash")
        self.assertEqual(result["outcome"], "existing")
        self.assertEqual(len(self.library.list_notes()), 1)

    def test_interrupted_transaction_does_not_replace_external_edit(self):
        with patch.object(self.library, "_save", side_effect=OSError("simulated crash")):
            with self.assertRaises(LibraryError):
                self.library.import_note("body", source_id="crash", title="Crash")
        working = next(self.vault.glob("00_收件箱/*.md"))
        working.write_text(working.read_text(encoding="utf-8") + "external", encoding="utf-8")
        with self.assertRaisesRegex(LibraryError, "transaction conflicts"):
            self.library.list_notes()
        self.assertTrue(working.read_text().endswith("external"))

    def test_manual_delete_never_propagates_publication_delete(self):
        post = self.library.create("public", title="Post", kind="post")
        self.library.publish(post["id"], expected_hash=post["sha256"])
        (self.vault / post["path"]).unlink()
        self.assertEqual(self.library.list_notes(), [])
        self.assertEqual(self.library.publications()[0]["state"], "published")

    def test_cli_json_and_explicit_publish(self):
        command = [sys.executable, "-m", "markdown_library", "--vault", str(self.vault)]
        listing = subprocess.run(command + ["list"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(listing.stdout), [])
        post = self.library.create("post", title="Post", kind="post")
        failed = subprocess.run(command + ["publish", post["id"], "--expected-hash", "stale"], capture_output=True, text=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("error", json.loads(failed.stderr))
        self.assertIsNone(self.library.publication(post["id"]))

    def test_raw_windows_crlf_bom_and_unrelated_id(self):
        created = self.library.create("body\n", title="Windows")
        file = self.vault / created["path"]
        raw = ("\ufeff" + created["markdown"].replace("\n", "\r\n")).encode("utf-8")
        file.write_bytes(raw)
        current = self.library.get(created["id"])
        self.assertEqual(current["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(current["markdown"].encode("utf-8"), raw)
        self.assertEqual(self.library.history_markdown(created["id"], current["sha256"]).encode("utf-8"), raw)
        (self.vault / "unrelated.md").write_text("---\nid: 123\nplugin: daily-note\n---\nUser file", encoding="utf-8")
        self.assertEqual(len(self.library.list_notes()), 1)

    def test_core_publication_to_offline_sync_retains_actual_schema(self):
        from markdown_library.sync import export_publication, plan_sync
        post = self.library.create("Published body", title="Post", kind="post", properties={
            "link": "notes/stable", "locale": "en", "categories": [["Writing", "Notes"]],
            "frontmatter": {"math": True, "tocNumbering": False}, "private-property": "SECRET",
        })
        self.library.publish(post["id"], expected_hash=post["sha256"])
        plan = plan_sync(self.library.publications(), {}, {"works": [], "posts": []})
        self.assertEqual(plan["actions"][0]["action"], "create")
        fields = plan["actions"][0]["payload"]
        self.assertEqual(fields["link"], "notes/stable")
        self.assertEqual(fields["locale"], "en")
        self.assertEqual(fields["categories"], [["Writing", "Notes"]])
        self.assertEqual(fields["frontmatter"], {"math": True, "tocNumbering": False})
        self.assertNotIn("SECRET", json.dumps(fields))
        work = self.library.create("PRIVATE BODY", title="Work", kind="work", properties={
            "slug": "stable-work", "type": "book", "note": "Public reaction", "rating": None,
            "experiences": [{"id": "stable-row", "startedOn": None, "endedOn": "2022-03", "private": "SECRET"}],
            "cover": {"id": 11, "private": "SECRET"}, "sources": None,
        })
        published = self.library.publish(work["id"], expected_hash=work["sha256"])
        exported = export_publication(published)
        self.assertNotIn("PRIVATE BODY", json.dumps(exported))
        self.assertNotIn("SECRET", json.dumps(published))
        self.assertEqual(exported["fields"]["experiences"], [{"id": "stable-row", "startedOn": None, "endedOn": "2022-03"}])
        self.assertIsNone(exported["fields"]["rating"])

    def test_protected_or_unknown_theme_posts_fail_publication(self):
        variants = [({"password": "private"}, "body"), ({"frontmatter": {"password": "private"}}, "body"),
                    ({"protectedContent": True}, "body"), ({}, ":::encrypted\nsecret\n:::"),
                    ({"frontmatter": {"unsupported-option": True}}, "body")]
        for properties, body in variants:
            post = self.library.create(body, title="Protected", kind="post", properties=properties)
            with self.assertRaises(LibraryError):
                self.library.publish(post["id"], expected_hash=post["sha256"])
            self.assertIsNone(self.library.publication(post["id"]))


if __name__ == "__main__":
    unittest.main()
