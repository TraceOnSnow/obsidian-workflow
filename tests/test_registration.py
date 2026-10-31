import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library, LibraryError
from markdown_library.cli import dispatch, parser
from markdown_library.core import _split


def digest(data):
    return hashlib.sha256(data).hexdigest()


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / "vault"
        self.library = Library.initialize(self.vault)

    def write(self, content, path="Blogs/旧笔记.md"):
        destination = self.vault / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return path

    def snapshot(self):
        return {str(path.relative_to(self.vault)): path.read_bytes()
                for path in self.vault.rglob("*") if path.is_file()}

    def test_preview_is_readonly_deterministic_and_apply_requires_hash(self):
        source = "# 原标题\n\n正文。\n".encode()
        path = self.write(source)
        (self.vault / ".markdown-library/lock").unlink()
        before = self.snapshot()
        plan = self.library.register(path)
        self.assertEqual(plan, self.library.track_existing(path))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(plan["original_sha256"], digest(source))
        self.assertFalse(plan["applied"])
        self.assertFalse(plan["selected_for_publication"])
        with self.assertRaisesRegex(LibraryError, "requires expected_hash"):
            self.library.register(path, apply=True)
        self.assertEqual(self.snapshot(), before)

    def test_preserves_body_and_yaml_text_bom_crlf_dates_comments(self):
        for index, source in enumerate([
            b"# Heading\n\n`code`  \n",
            b"\xef\xbb\xbf# Heading\r\n\r\nBody  \r\n",
            b"---\n# comment\ntitle: 'Original' # inline\ndate: 2020-01-02\ntags: [one, two]\ncustom: yes\n---\n# Body\n\n",
            b"\xef\xbb\xbf---\r\n# comment\r\ntitle: 'Original' # inline\r\ndate: 2020-01-02\r\n...\r\n# Body\r\n\r\n",
        ]):
            with self.subTest(index=index):
                path = self.write(source, f"note{index}.md")
                before_text = source.decode()
                plan = self.library.register(path)
                result = self.library.register(path, apply=True, expected_hash=digest(source))
                data = (self.vault / path).read_bytes()
                self.assertEqual(result["id"], plan["id"])
                self.assertEqual(data, result["markdown"].encode())
                self.assertEqual(data.startswith(b"\xef\xbb\xbf"), source.startswith(b"\xef\xbb\xbf"))
                self.assertEqual(_split(result["markdown"])[1], _split(before_text)[1].removeprefix("\ufeff"))
                if "# comment" in before_text:
                    self.assertIn("# comment", result["markdown"])
                    self.assertIn("title: 'Original' # inline", result["markdown"])
                    self.assertIn("date: 2020-01-02", result["markdown"])
                    self.assertEqual(result["properties"]["title"], "Original")
                if b"\r\n" in source:
                    self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
                self.assertEqual(self.library.history_markdown(result["id"], digest(source)).encode(), source)
                self.assertEqual(self.library.publications(), [])

    def test_idempotent_no_second_file_or_history_and_keeps_user_edit(self):
        source = b"Original body\n"
        path = self.write(source)
        registered = self.library.register(path, apply=True, expected_hash=digest(source))
        before = self.snapshot()
        result = self.library.register(path, apply=True, expected_hash=registered["sha256"])
        self.assertEqual(result["outcome"], "existing")
        self.assertFalse(result["applied"])
        self.assertEqual(self.snapshot(), before)
        repeated_plan = self.library.register(path)
        self.assertEqual(repeated_plan["original_sha256"], registered["sha256"])
        self.assertEqual(repeated_plan["registration_original_sha256"], digest(source))
        self.assertEqual(self.library.register(path, apply=True,
                         expected_hash=repeated_plan["original_sha256"])["outcome"], "existing")
        edited = (self.vault / path).read_bytes() + b"Native new text\n"
        (self.vault / path).write_bytes(edited)
        again = self.library.register(path, apply=True, expected_hash=digest(edited))
        self.assertEqual(again["id"], registered["id"])
        self.assertEqual(again["markdown"].encode(), edited)
        self.assertEqual(len(list(self.vault.glob("**/旧笔记.md"))), 1)

    def test_original_history_restore_re_register_and_identity_guard(self):
        original = b"---\n# preserve\naliases: [Original]\n---\nPrivate\n"
        path = self.write(original)
        first = self.library.register(path, apply=True, expected_hash=digest(original))
        history = self.library.history(first["id"])
        self.assertEqual([row["action"] for row in history], ["registration-original", "registered"])
        with self.assertRaises(LibraryError):
            self.library.restore(first["id"], digest(original), expected_hash=first["sha256"])
        with self.assertRaisesRegex(LibraryError, "Revision conflict"):
            self.library.restore_original(first["id"], expected_hash="stale")
        self.library.restore_original(first["id"], expected_hash=first["sha256"])
        self.assertEqual((self.vault / path).read_bytes(), original)
        self.assertEqual(self.library.list_notes(), [])
        self.assertEqual(self.library.history_markdown(first["id"], first["sha256"]), first["markdown"])
        again = self.library.register(path, apply=True, expected_hash=digest(original))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(self.library.get(first["id"])["markdown"], first["markdown"])

    def test_conflicts_and_untrusted_reserved_properties_do_not_write(self):
        for index, source in enumerate([
            b"---\nid: plugin-id\n---\nBody",
            b"---\nid: forged\nkind: note\nschema_version: 1\ntitle: Forged\n---\nBody",
            b"---\nid: null\n---\nBody",
            b"---\nkind: journal\n---\nBody",
            b"---\nschema_version: 2\n---\nBody",
            b"---\ntitle: First\ntitle: Second\n---\nBody",
            b"---\ntitle: 42\n---\nBody",
        ]):
            with self.subTest(index=index):
                path = self.write(source)
                before = self.snapshot()
                for apply in (False, True):
                    with self.assertRaises(LibraryError):
                        self.library.register(path, apply=apply, expected_hash=digest(source))
                self.assertEqual(self.snapshot(), before)
        path = self.write(b"Original")
        with self.assertRaisesRegex(LibraryError, "Revision conflict"):
            self.library.register(path, apply=True, expected_hash="stale")
        self.assertEqual((self.vault / path).read_bytes(), b"Original")
        self.write(b"---\ntitle: Existing\n---\nOriginal", path)
        with self.assertRaisesRegex(LibraryError, "overwrite an existing title"):
            self.library.register(path, title="Overwrite")

    def test_unsafe_paths_symlink_duplicates_and_pending_preview(self):
        path = self.write(b"Body")
        for unsafe in ("../escape.md", str(self.vault / path), ".obsidian/config.md", "plain.txt"):
            with self.assertRaises(LibraryError):
                self.library.register(unsafe)
        outside = self.vault.parent / "outside.md"
        outside.write_bytes(b"Outside")
        (self.vault / "link.md").symlink_to(outside)
        with self.assertRaisesRegex(LibraryError, "Symlinks"):
            self.library.register("link.md")
        (self.vault / "link.md").unlink()
        first = self.library.register(path, apply=True, expected_hash=digest(b"Body"))
        self.write(first["markdown"].encode(), "copy.md")
        with self.assertRaisesRegex(LibraryError, "Duplicate stable id"):
            self.library.register(path)
        (self.vault / "copy.md").unlink()
        pending = self.vault / ".markdown-library/transaction.json"
        pending.write_text("{}")
        before = self.snapshot()
        with self.assertRaisesRegex(LibraryError, "Pending library transaction"):
            self.library.register(path)
        self.assertEqual(self.snapshot(), before)

    def test_cli_alias_and_original_restore(self):
        path = self.write(b"Body")
        preview = dispatch(parser().parse_args(["--vault", str(self.vault), "track-existing", path]))
        applied = dispatch(parser().parse_args(["--vault", str(self.vault), "register", path,
                           "--apply", "--expected-hash", preview["original_sha256"]]))
        restored = dispatch(parser().parse_args(["--vault", str(self.vault), "restore-original", applied["id"],
                            "--expected-hash", applied["sha256"]]))
        self.assertTrue(restored["unregistered"])
        self.assertEqual((self.vault / path).read_bytes(), b"Body")

    def test_corrupt_original_object_cannot_be_adopted_and_published_identity_stays(self):
        source = b"Source\n"
        path = self.write(source)
        objects = self.vault / ".markdown-library/objects"
        objects.mkdir()
        (objects / f"{digest(source)}.md").write_bytes(b"Corrupt")
        with self.assertRaisesRegex(LibraryError, "Corrupted history object"):
            self.library.register(path, apply=True, expected_hash=digest(source))
        self.assertEqual((self.vault / path).read_bytes(), source)
        (objects / f"{digest(source)}.md").unlink()
        result = self.library.register(path, kind="post", apply=True, expected_hash=digest(source))
        self.library.publish(result["id"], expected_hash=result["sha256"])
        with self.assertRaisesRegex(LibraryError, "publication history"):
            self.library.restore_original(result["id"], expected_hash=result["sha256"])
        self.assertEqual((self.vault / path).read_bytes(), result["markdown"].encode())
