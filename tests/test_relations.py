from pathlib import Path
import tempfile
import unittest

from markdown_library.core import Library, LibraryError, _split
from markdown_library.relations import canonical_article, display_article, patch_properties, refresh_relations


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Library.initialize(Path(self.temp.name) / "vault")
        self.post = self.library.create("My article\n", title="Article", kind="post", path="posts/Article.md",
                                        properties={"link": "article", "locale": "zh"})
        self.work = self.library.create("Private body\n[[posts/Article]]\n", title="Work", kind="work", path="works/Work.md",
                                        properties={"slug": "work", "type": "book", "article": self.post["id"]})

    def test_id_and_wikilink_resolve_to_same_identity(self):
        for value in [self.post["id"], "[[posts/Article]]", "[[Article|My title]]", "[[posts/Article.md#Section]]"]:
            self.assertEqual(canonical_article(self.library, value), self.post["id"])
        self.assertEqual(display_article(self.library, self.post["id"]), "[[posts/Article|Article]]")

    def test_wrong_kind_missing_ambiguous_and_private_targets_are_rejected(self):
        self.library.create("Other", title="Duplicate", kind="post", path="other/Article.md", properties={"link": "other"})
        for value in ["[[Article]]", "[[missing]]", "[[../outside]]", "[[.markdown-library/objects/x]]", self.work["id"], True]:
            with self.subTest(value=value), self.assertRaises(LibraryError):
                canonical_article(self.library, value)

    def test_preview_is_readonly_then_apply_preserves_bodies_and_selection(self):
        self.library.publish(self.work["id"], expected_hash=self.work["sha256"])
        before_selection = self.library.publications()
        before = {path.relative_to(self.library.vault): path.read_bytes()
                  for path in self.library.vault.rglob("*") if path.is_file()}
        preview = refresh_relations(self.library)
        self.assertEqual(len(preview["changed"]), 2)
        after = {path.relative_to(self.library.vault): path.read_bytes()
                 for path in self.library.vault.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        result = refresh_relations(self.library, apply=True)
        self.assertTrue(result["applied"])
        work = self.library.get(self.work["id"])
        post = self.library.get(self.post["id"])
        self.assertEqual(_split(work["markdown"])[1], _split(self.work["markdown"])[1])
        self.assertEqual(_split(post["markdown"])[1], "My article\n")
        self.assertEqual(work["properties"]["article"], "[[posts/Article|Article]]")
        self.assertEqual(post["properties"]["related_works"], ["[[works/Work|Work]]"])
        self.assertEqual(before_selection, self.library.publications())
        self.assertEqual(refresh_relations(self.library, apply=True)["changed"], [])

    def test_changed_article_updates_reverse_navigation_without_rewriting_body(self):
        refresh_relations(self.library, apply=True)
        another = self.library.create("Another body", title="Another", kind="post", path="posts/Another.md", properties={"link": "another"})
        current = self.library.get(self.work["id"])
        self.library.replace(current["id"], patch_properties(current["markdown"], {"article": "[[posts/Another]]"}), expected_hash=current["sha256"])
        refresh_relations(self.library, apply=True)
        self.assertEqual(self.library.get(self.post["id"])["properties"]["related_works"], [])
        self.assertEqual(self.library.get(another["id"])["properties"]["related_works"], ["[[works/Work|Work]]"])

    def test_native_property_link_publishes_stable_identity_without_private_navigation(self):
        refresh_relations(self.library, apply=True)
        current = self.library.get(self.work["id"])
        selected = self.library.publish(current["id"], expected_hash=current["sha256"])
        self.assertEqual(selected["snapshot"]["properties"]["article"], self.post["id"])
        self.assertNotIn("markdown", selected["snapshot"])
        current_post = self.library.get(self.post["id"])
        selected_post = self.library.publish(current_post["id"], expected_hash=current_post["sha256"])
        self.assertNotIn("related_works", selected_post["snapshot"]["properties"])

    def test_trashed_linked_post_retains_identity_without_blocking_other_navigation(self):
        refresh_relations(self.library, apply=True)
        current = self.library.get(self.post["id"])
        self.library.delete(current["id"], expected_hash=current["sha256"])
        value = self.library.get(self.work["id"])["properties"]["article"]
        self.assertEqual(canonical_article(self.library, value), self.post["id"])
        self.assertEqual(canonical_article(self.library, self.post["id"]), self.post["id"])
        self.assertEqual(display_article(self.library, self.post["id"]), value)
        self.assertEqual(refresh_relations(self.library, apply=True)["issues"], [])
        self.library.recover(current["id"], expected_hash=current["sha256"])
        self.assertEqual(refresh_relations(self.library, apply=True)["issues"], [])
        self.assertEqual(self.library.get(current["id"])["properties"]["related_works"], ["[[works/Work|Work]]"])

    def test_frontmatter_patch_preserves_comments_dates_bom_crlf_and_body(self):
        source = '\ufeff---\r\n# Header\r\narticle: old # keep comment\r\nuntouched: "2020-01-01"\r\n---\r\nBody\r\n[[mine]]\r\n'
        result = patch_properties(source, {"article": "[[posts/Article]]", "related_works": []})
        self.assertIn('# Header\r\n', result)
        self.assertIn('# keep comment\r\n', result)
        self.assertIn('untouched: "2020-01-01"\r\n', result)
        self.assertTrue(result.startswith('\ufeff---\r\n'))
        self.assertEqual(_split(source)[1], _split(result)[1])
        self.assertEqual(_split(result)[0]["article"], "[[posts/Article]]")

    def test_broken_link_keeps_all_existing_reverse_navigation(self):
        refresh_relations(self.library, apply=True)
        current = self.library.get(self.work["id"])
        self.library.replace(current["id"], patch_properties(current["markdown"], {"article": "[[missing]]"}), expected_hash=current["sha256"])
        original = self.library.get(self.post["id"])["markdown"]
        result = refresh_relations(self.library, apply=True)
        self.assertFalse(result["applied"])
        self.assertTrue(result["issues"])
        self.assertEqual(self.library.get(self.post["id"])["markdown"], original)


if __name__ == "__main__":
    unittest.main()
