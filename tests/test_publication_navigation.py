"""Private navigation must never become public Post content."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.core import _split
from markdown_library.merge import local_fields, recover_base
from markdown_library.publication_navigation import (
    END, START, set_publication_navigation, strip_publication_navigation,
)
from markdown_library.sync import export_publication, observed_baseline, plan_sync


PRIVATE = "![[发布与同步状态#文章发表]]\n[Private status](https://private.invalid/status)\n/home/private/vault"


class PublicationNavigationTests(unittest.TestCase):
    def test_exact_roundtrip_and_idempotent_set_for_original_whitespace(self):
        for body in ("", "body", "body\n", "body\n\n", "body\r\n", "body\r\n\r\n"):
            with self.subTest(body=repr(body)):
                managed = set_publication_navigation(body, PRIVATE)
                self.assertEqual(strip_publication_navigation(managed), body)
                self.assertEqual(set_publication_navigation(managed, PRIVATE), managed)
                self.assertEqual(set_publication_navigation(managed, ""), body)
                self.assertEqual(set_publication_navigation(managed, None), body)

    def test_source_code_comments_and_suffix_are_preserved(self):
        prefix = "Text  \n<!-- ordinary comment -->\n```cpp\nint x = 1; // keep\n```\n"
        suffix = "\nAfter the navigation.  \n"
        managed = set_publication_navigation(prefix, PRIVATE) + suffix
        self.assertEqual(strip_publication_navigation(managed), prefix + suffix)
        replaced = set_publication_navigation(managed, "![[New local status]]")
        self.assertEqual(strip_publication_navigation(replaced), prefix + suffix)
        self.assertNotIn("private.invalid", replaced)
        self.assertIn("![[New local status]]", replaced)
        self.assertEqual(strip_publication_navigation(prefix), prefix)

    def test_invalid_markers_fail_closed_for_strip_and_set(self):
        for text in (
            START + "\nprivate", END + "\n", END + "\n" + START,
            START + "\n" + START + "\n" + END,
            START + "\n" + END + "\n" + END,
            START + "\n" + END + "\n" + START + "\n" + END,
            "inline " + START + "\nprivate\n" + END,
            " " + START + "\nprivate\n" + END,
            "```md\n" + START + "\nprivate\n" + END + "\n```",
            START + "\n```\nprivate\n" + END,
            "<!-- markdown-library:publication-nav:unknown -->",
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    strip_publication_navigation(text)
                with self.assertRaises(ValueError):
                    set_publication_navigation(text, PRIVATE)

    def test_cannot_inject_nested_marker_or_append_to_open_code_fence(self):
        for footer in (START, END, PRIVATE + "\n" + START, "```\nprivate"):
            with self.subTest(footer=footer), self.assertRaises(ValueError):
                set_publication_navigation("body", footer)
        for body in ("```cpp\nint x;", "~~~md\nbody"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                set_publication_navigation(body, PRIVATE)

    def test_temporary_post_export_and_current_projection_ignore_footer(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library.initialize(Path(directory) / "vault")
            note = library.create("Public body\n<!-- ordinary -->\n```txt\npublic code\n```\n",
                                  title="Post", kind="post", properties={"link": "post/test"})
            selected = library.publish(note["id"], expected_hash=note["sha256"])
            before = export_publication(selected)
            current_before = local_fields(note, "posts")
            managed = set_publication_navigation(note["markdown"], PRIVATE)
            edited = library.replace(note["id"], managed, expected_hash=note["sha256"])
            self.assertEqual(local_fields(edited, "posts"), current_before)
            self.assertIn(PRIVATE, edited["markdown"])
            self.assertEqual(strip_publication_navigation(edited["markdown"]), note["markdown"])
            selected_after = library.publish(note["id"], expected_hash=edited["sha256"])
            self.assertEqual(export_publication(selected_after), before)
            self.assertNotIn("private.invalid", str(export_publication(selected_after)))
            self.assertIn("<!-- ordinary -->", before["fields"]["markdown"])
            row = {"id": 7, "updatedAt": "2026-10-27T00:00:00Z", **before["fields"]}
            baseline = {note["id"]: observed_baseline(selected, row)}
            result = plan_sync([selected_after], baseline, {"posts": [row]})
            self.assertEqual(result["actions"][0]["action"], "unchanged")
            self.assertNotIn("private.invalid", str(result))

    def test_post_validation_still_checks_original_protected_content(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library.initialize(directory)
            note = library.create("Public body", title="Post", kind="post", properties={"link": "p"})
            selected = library.publish(note["id"], expected_hash=note["sha256"])
            protected = deepcopy(selected)
            protected["snapshot"]["markdown"] = set_publication_navigation("Public body", "::: encrypted\nsecret\n:::")
            with self.assertRaisesRegex(ValueError, "protected posts"):
                export_publication(protected)
            for properties in ({"protectedContent": True}, {"password": "secret"},
                               {"frontmatter": {"password": "secret"}}):
                protected = deepcopy(selected)
                protected["snapshot"]["properties"].update(properties)
                with self.subTest(properties=properties), self.assertRaises(ValueError):
                    export_publication(protected)

    def test_selected_snapshot_with_footer_still_proves_legacy_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library.initialize(directory)
            note = library.create("Public body", title="Post", kind="post",
                                  properties={"link": "p", "locale": "zh"})
            selected = library.publish(note["id"], expected_hash=note["sha256"])
            fields = export_publication(selected)["fields"]
            row = {"id": 7, "updatedAt": "2026-10-27T00:00:00Z", **fields}
            baseline = observed_baseline(selected, row)
            del baseline["local_fields"]
            del baseline["remote_fields"]
            state = library._read_state()
            state["publications"][note["id"]]["snapshot"]["markdown"] = set_publication_navigation(
                selected["snapshot"]["markdown"], PRIVATE)
            local, remote, proof = recover_base(library, note["id"], baseline, None,
                                               {note["id"]: baseline}, library_state=state)
            self.assertEqual(proof, "exact-history-hashes")
            self.assertEqual(local["markdown"], fields["markdown"])
            self.assertEqual(remote["markdown"], fields["markdown"])

    def test_bad_post_markers_fail_at_both_public_projections(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library.initialize(directory)
            note = library.create("Public body", title="Post", kind="post", properties={"link": "p"})
            selected = library.publish(note["id"], expected_hash=note["sha256"])
            selected["snapshot"]["markdown"] += "\n" + START + "\nprivate"
            with self.assertRaises(ValueError):
                export_publication(selected)
            note["markdown"] += "\n" + START + "\nprivate"
            with self.assertRaises(ValueError):
                local_fields(note, "posts")

    def test_work_projection_does_not_parse_or_change_private_body(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library.initialize(directory)
            note = library.create("Private work body\n" + START, title="Work", kind="work",
                                  properties={"slug": "work", "type": "book"})
            selected = library.publish(note["id"], expected_hash=note["sha256"])
            self.assertNotIn("markdown", export_publication(selected)["fields"])
            self.assertNotIn("markdown", local_fields(note, "works"))
            self.assertIn(START, _split(note["markdown"])[1])


if __name__ == "__main__":
    unittest.main()
