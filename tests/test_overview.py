from pathlib import Path
from contextlib import contextmanager
from copy import deepcopy
import json
from unittest.mock import patch
import tempfile
import unittest

from markdown_library.core import Library, LibraryError
from markdown_library.overview import publication_overview, REPORT_PATH
from markdown_library.relations import patch_properties
from markdown_library.publication_navigation import strip_publication_navigation
from markdown_library.sync import export_publication, observed_baseline
from markdown_library.work_profile import set_profile


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Library.initialize(Path(self.temp.name) / "vault")
        self.work = self.library.create("Private body", title="Work", kind="work", properties={"slug": "work", "type": "book", "rating": 8})
        self.post = self.library.create("Draft article", title="Post", kind="post", properties={"link": "post"})
        self.note = self.library.create("Personal", title="Note")

    def review(self):
        return {"mode": "read-only-review", "items": [
            {"id": self.work["id"], "payload_formal": {"state": "published", "visibility": "public", "fields_hash": "old"},
             "payload_latest": {"state": "draft", "visibility": "public", "fields_hash": "new"}},
            {"id": self.post["id"], "payload_formal": {"state": "draft", "fields_hash": "draft"},
             "payload_latest": {"state": "draft", "fields_hash": "draft"}}
        ]}

    def test_actual_visibility_comes_from_formal_not_latest_or_local_selection(self):
        result = publication_overview(self.library, self.review(), observed_at="test", apply=True)
        self.assertEqual(result["counts"]["public"], 1)
        self.assertEqual(result["counts"]["private"], 1)
        self.assertEqual(result["counts"]["local"], 1)
        content = (self.library.vault / REPORT_PATH).read_text()
        self.assertIn("后台最新稿与正式版本不同", content)
        self.assertNotIn("Private body", content)
        self.assertNotIn("Draft article", content)
        self.assertEqual(self.library.publications(), [])

    def test_public_field_change_is_distinguished_from_private_body_or_format(self):
        self.library.publish(self.work["id"], expected_hash=self.work["sha256"])
        current = self.library.replace(self.work["id"], self.work["markdown"] + "\nPrivate edit", expected_hash=self.work["sha256"])
        publication_overview(self.library, self.review(), observed_at="first", apply=True)
        self.assertIn("仅私人正文／本地属性／格式变化", (self.library.vault / REPORT_PATH).read_text())
        self.library.replace(current["id"], patch_properties(current["markdown"], {"rating": 9}), expected_hash=current["sha256"])
        publication_overview(self.library, self.review(), observed_at="second", apply=True)
        self.assertIn("当前稿有未选入发布版的修改", (self.library.vault / REPORT_PATH).read_text())

    def test_preview_and_repeat_are_readonly_and_user_document_is_retained(self):
        before = {path.relative_to(self.library.vault): path.read_bytes() for path in self.library.vault.rglob("*") if path.is_file()}
        publication_overview(self.library, self.review(), observed_at="same")
        after = {path.relative_to(self.library.vault): path.read_bytes() for path in self.library.vault.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        first = publication_overview(self.library, self.review(), observed_at="same", apply=True)
        second = publication_overview(self.library, self.review(), observed_at="same", apply=True)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        (self.library.vault / REPORT_PATH).write_text("My own page")
        with self.assertRaises(LibraryError):
            publication_overview(self.library, self.review(), apply=True)

    def test_missing_unmapped_note_is_visible_without_inferred_deletion(self):
        (self.library.vault / self.note["path"]).unlink()
        result = publication_overview(self.library, self.review(), observed_at="test", apply=True)
        self.assertEqual(result["counts"]["review"], 1)
        self.assertNotIn("local", result["counts"])
        self.assertIn("本地当前文件缺失／已回收", (self.library.vault / REPORT_PATH).read_text())
        self.assertFalse(self.library._read_state()["notes"][self.note["id"]]["deleted"])

    def test_recoverable_merged_source_is_not_a_missing_note_alarm(self):
        from markdown_library.mcp_actions import LibraryActions
        source = self.library.create('Additional details', title='Another note')
        LibraryActions(self.library).merge(self.note['id'], [source['id']],
            self.note['markdown'] + '\nAdditional details',
            {self.note['id']: self.note['sha256'], source['id']: source['sha256']})
        result = publication_overview(self.library, self.review())
        self.assertEqual(result['counts']['merged'], 1)
        self.assertNotIn('review', result['counts'])

    def test_explicit_private_trash_is_accounted_for_without_missing_alarm(self):
        self.library.delete(self.note['id'], expected_hash=self.note['sha256'])
        result = publication_overview(self.library, self.review())
        self.assertEqual(result['counts']['private_trash'], 1)
        self.assertNotIn('review', result['counts'])

    def test_polling_without_state_changes_does_not_rewrite_sync_file(self):
        publication_overview(self.library, self.review(), observed_at="first", apply=True)
        target = self.library.vault / REPORT_PATH
        before = target.read_bytes(), target.stat().st_mtime_ns
        again = publication_overview(self.library, self.review(), observed_at="later", apply=True)
        self.assertFalse(again["changed"])
        self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)
        self.assertEqual(again["observed_at"], "first")
        self.assertEqual(again["checked_at"], "later")


    def add_source(self, source, *, key="publication-source", post=None):
        state = self.library._read_state()
        state["sources"][key] = {"id": (post or self.post)["id"], "source": source}
        self.library._save(state)

    def published_review(self):
        current = self.library.get(self.post["id"])
        self.library.publish(self.post["id"], expected_hash=current["sha256"])
        selected = self.library._read_state()["publications"][self.post["id"]]
        fields = export_publication(selected)["fields"]
        remote = {**fields, "id": 7, "updatedAt": "2026-10-28T12:00:00Z"}
        baseline = observed_baseline(selected, remote)
        sync_state = {"schema_version": 1, "target": "https://example.test", "baselines": {self.post["id"]: baseline},
                      "pending": {}, "reverse_imports": {}}
        self.library._atomic(".markdown-library/sync-state.json", json.dumps(sync_state))
        review = self.review()
        review["items"][1] = {"id": self.post["id"], "payload_formal": {
            "state": "published", "fields_hash": baseline["remote_hash"]}, "payload_latest": {
            "state": "draft", "fields_hash": "backend-new-draft"}}
        return review

    def test_post_table_and_block_use_formal_observation_and_confirmed_url(self):
        review = self.published_review()
        publication_overview(self.library, review, observed_at="test", apply=True)
        page = (self.library.vault / REPORT_PATH).read_text()
        self.assertLess(page.index("## 文章发表"), page.index("## 网站已公开"))
        self.assertIn("网站正式版已发布", page)
        self.assertIn("https://example.test/post/post", page)
        self.assertIn("静态页面更新可能稍后完成", page)
        self.assertIn(f"Post未发布改动：无。 ^publication-{self.post['id']}", page)
        # Backend drafts do not change the independently observed formal status.
        self.assertIn("后台最新稿与正式版本不同", page)
        self.assertIn("来源未记录", page)
        self.assertEqual((self.library.vault / self.note["path"]).read_text(), self.note["markdown"])

    def test_explicit_source_footer_preserves_body_properties_selection_and_baseline(self):
        review = self.published_review()
        self.add_source({"type": "publication-copy", "note_id": self.note["id"], "source_body_sha256": "copied-body"})
        selected = deepcopy(self.library._read_state()["publications"])
        sync_bytes = (self.library.private / "sync-state.json").read_bytes()
        publication_overview(self.library, review, observed_at="first", apply=True)
        for before in (self.post, self.note):
            actual = (self.library.vault / before["path"]).read_text()
            self.assertEqual(strip_publication_navigation(actual), before["markdown"])
            self.assertEqual(actual.count("publication-nav:start"), 1)
            self.assertIn(f"![[发布与同步状态#^publication-{self.post['id']}]]", actual)
        source_text = (self.library.vault / self.note["path"]).read_text()
        self.assertIn("已有独立发表稿", source_text)
        self.assertNotIn("私人全文已公开", source_text)
        self.assertIn("私人原稿；已有独立发表稿", (self.library.vault / REPORT_PATH).read_text())
        self.assertIn("私人原稿：主来源：", (self.library.vault / self.post["path"]).read_text())
        self.assertEqual(self.library._read_state()["publications"], selected)
        self.assertEqual((self.library.private / "sync-state.json").read_bytes(), sync_bytes)
        before_files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.library.vault.rglob("*") if p.is_file()}
        again = publication_overview(self.library, review, observed_at="later", apply=True)
        self.assertFalse(again["changed"])
        self.assertEqual(again["navigation_updated"], 0)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.library.vault.rglob("*") if p.is_file()}, before_files)

    def test_legacy_source_id_and_all_explicit_source_fields_are_supported(self):
        for field in ("private_source_id", "note_id", "work_id", "source_note_id"):
            with self.subTest(field=field):
                state = self.library._read_state()
                state["sources"] = {}
                self.library._save(state)
                self.add_source({"type": "independent-publication-copy-v1", field: self.note["id"]})
                publication_overview(self.library, self.review(), observed_at=field, apply=True)
                page = (self.library.vault / REPORT_PATH).read_text()
                article_section = page.split("## 文章发表", 1)[1].split("发布由明确选版", 1)[0]
                self.assertIn(self.note["path"].removesuffix(".md"), article_section)
                self.assertIn("主来源：", article_section)

    def test_unknown_sources_and_same_titles_do_not_create_inferred_relationships(self):
        note = self.library.get(self.note["id"])
        self.library.replace(note["id"], patch_properties(note["markdown"], {"title": "Post"}), expected_hash=note["sha256"])
        self.add_source({"type": "publication-copy", "note_id": "missing-private-identity"})
        publication_overview(self.library, self.review(), apply=True)
        page = (self.library.vault / REPORT_PATH).read_text()
        article_section = page.split("## 文章发表", 1)[1].split("发布由明确选版", 1)[0]
        self.assertIn("来源身份待核对", article_section)
        self.assertNotIn(self.note["path"].removesuffix(".md"), article_section)
        self.assertNotIn("publication-nav:start", (self.library.vault / self.note["path"]).read_text())
        self.assertIn("publication-nav:start", (self.library.vault / self.post["path"]).read_text())

    def test_multiple_sources_remain_visible_and_only_unambiguous_primary_is_preferred(self):
        other = self.library.create("Other private body", title="Other source")
        self.add_source({"type": "historical-copy", "note_id": other["id"]}, key="secondary")
        self.add_source({"type": "publication-copy", "note_id": self.note["id"]}, key="primary")
        publication_overview(self.library, self.review(), apply=True)
        actual = (self.library.vault / self.post["path"]).read_text()
        self.assertLess(actual.index(self.note["path"].removesuffix(".md")), actual.index(other["path"].removesuffix(".md")))
        self.assertIn("主来源：", actual)
        self.add_source({"type": "publication-copy", "note_id": other["id"]}, key="other-primary")
        publication_overview(self.library, self.review(), apply=True)
        actual = (self.library.vault / self.post["path"]).read_text()
        self.assertNotIn("主来源：", actual)
        for note in (self.note, other):
            self.assertIn(note["path"].removesuffix(".md"), actual)
            self.assertIn("已有独立发表稿", (self.library.vault / note["path"]).read_text())

    def test_retained_work_article_proves_private_work_source(self):
        state = self.library._read_state()
        set_profile(state, self.work["id"], {"article": self.post["id"], "slug": "work"})
        self.library._save(state)
        publication_overview(self.library, self.review(), apply=True)
        work = (self.library.vault / self.work["path"]).read_text()
        post = (self.library.vault / self.post["path"]).read_text()
        self.assertEqual(strip_publication_navigation(work), self.work["markdown"])
        self.assertIn("已有独立发表稿", work)
        self.assertIn(self.work["path"].removesuffix(".md"), post)
        self.assertNotIn("Private body", (self.library.vault / REPORT_PATH).read_text())

    def test_post_changes_are_compared_with_observed_formal_fields_not_private_source(self):
        review = self.published_review()
        self.add_source({"type": "publication-copy", "note_id": self.note["id"]})
        publication_overview(self.library, review, apply=True)
        source = self.library.get(self.note["id"])
        self.library.replace(source["id"], source["markdown"].replace("Personal", "Private revised body"), expected_hash=source["sha256"])
        publication_overview(self.library, review, apply=True)
        page = (self.library.vault / REPORT_PATH).read_text()
        self.assertIn(f"Post未发布改动：无。 ^publication-{self.post['id']}", page)
        post = self.library.get(self.post["id"])
        self.library.replace(post["id"], post["markdown"].replace("Draft article", "Revised article"), expected_hash=post["sha256"])
        publication_overview(self.library, review, apply=True)
        page = (self.library.vault / REPORT_PATH).read_text()
        self.assertIn(f"Post未发布改动：有。 ^publication-{self.post['id']}", page)
        self.assertIn("网站正式版已发布", page)

    def test_draft_unmapped_trash_and_missing_formal_are_distinct(self):
        publication_overview(self.library, self.review(), apply=True)
        self.assertIn("网站正式版未发布", (self.library.vault / REPORT_PATH).read_text())
        unmapped = {"mode": "read-only-review", "items": []}
        publication_overview(self.library, unmapped, apply=True)
        self.assertIn("仅库内；网站状态未建立映射", (self.library.vault / REPORT_PATH).read_text())
        missing = {"mode": "read-only-review", "items": [{"id": self.post["id"], "status": "remote-missing-review"}]}
        publication_overview(self.library, missing, apply=True)
        self.assertIn("网站正式状态待核对", (self.library.vault / REPORT_PATH).read_text())
        review = self.review()
        review["items"][1]["payload_formal"]["deleted_at"] = "2026-10-28"
        publication_overview(self.library, review, apply=True)
        self.assertIn("网站已回收", (self.library.vault / REPORT_PATH).read_text())
        post = self.library.get(self.post["id"])
        self.library.delete(post["id"], expected_hash=post["sha256"])
        publication_overview(self.library, unmapped, apply=True)
        page = (self.library.vault / REPORT_PATH).read_text()
        self.assertIn("发表稿已回收；网站状态待核对", page)
        self.assertIn(f"^publication-{post['id']}", page)

    def test_unverified_or_locally_changed_link_never_supplies_public_url(self):
        review = self.published_review()
        post = self.library.get(self.post["id"])
        self.library.replace(post["id"], patch_properties(post["markdown"], {"link": "unpublished-route"}), expected_hash=post["sha256"])
        publication_overview(self.library, review, apply=True)
        page = (self.library.vault / REPORT_PATH).read_text()
        self.assertIn("https://example.test/post/post", page)
        self.assertNotIn("https://example.test/post/unpublished-route", page)
        review["items"][1]["payload_formal"]["fields_hash"] = "changed-without-observed-identity"
        publication_overview(self.library, review, apply=True)
        page = (self.library.vault / REPORT_PATH).read_text()
        self.assertNotIn("https://example.test/post/", page)
        self.assertIn("链接待确认", page)

    def test_external_body_edit_aborts_report_and_other_footers_before_writing(self):
        self.add_source({"type": "publication-copy", "note_id": self.note["id"]})
        original_lock = self.library._lock
        source_path = self.library.vault / self.note["path"]
        @contextmanager
        def concurrent_edit():
            with original_lock():
                source_path.write_text(self.note["markdown"] + "\nConcurrent user text")
                yield
        state_before = self.library._read_state()
        with patch.object(self.library, "_lock", concurrent_edit):
            with self.assertRaisesRegex(LibraryError, "concurrently"):
                publication_overview(self.library, self.review(), apply=True)
        self.assertFalse((self.library.vault / REPORT_PATH).exists())
        self.assertEqual((self.library.vault / self.post["path"]).read_text(), self.post["markdown"])
        self.assertEqual(self.library._read_state(), state_before)
        self.assertIn("Concurrent user text", source_path.read_text())

    def test_concurrent_source_state_edit_aborts_without_overwriting_relationships(self):
        original_lock = self.library._lock
        @contextmanager
        def concurrent_edit():
            with original_lock():
                state = self.library._read_state()
                state["sources"]["new-source"] = {"id": self.post["id"], "source": {"note_id": self.note["id"]}}
                self.library._save(state)
                yield
        with patch.object(self.library, "_lock", concurrent_edit):
            with self.assertRaisesRegex(LibraryError, "concurrently"):
                publication_overview(self.library, self.review(), apply=True)
        self.assertFalse((self.library.vault / REPORT_PATH).exists())
        self.assertIn("new-source", self.library._read_state()["sources"])
        self.assertNotIn("publication-nav:start", (self.library.vault / self.post["path"]).read_text())

    def test_handwritten_layout_is_retained_exactly_around_managed_footer(self):
        source = self.library.get(self.note["id"])
        handmade = source["markdown"] + "\n## My layout\n\n- Keep this handwritten link [[Other]]\n\n```text\nlayout\n```"
        self.library.replace(source["id"], handmade, expected_hash=source["sha256"])
        self.add_source({"type": "publication-copy", "note_id": source["id"]})
        publication_overview(self.library, self.review(), apply=True)
        actual = (self.library.vault / source["path"]).read_text()
        self.assertEqual(strip_publication_navigation(actual), handmade)


    def test_readonly_source_and_post_keep_bytes_and_metadata_while_relationship_remains_visible(self):
        for note, protection in ((self.note, {"style_anchor": True}), (self.post, {"ai_edit": False})):
            current = self.library.get(note["id"])
            self.library.replace(current["id"], patch_properties(current["markdown"], protection), expected_hash=current["sha256"])
        self.add_source({"type": "publication-copy", "note_id": self.note["id"]})
        paths = [self.library.vault / note["path"] for note in (self.note, self.post)]
        before_files = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        state_before = self.library._read_state()
        result = publication_overview(self.library, self.review(), apply=True)
        self.assertEqual(result["navigation_updated"], 0)
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before_files)
        self.assertEqual(self.library._read_state(), state_before)
        page = (self.library.vault / REPORT_PATH).read_text()
        self.assertIn(self.note["path"].removesuffix(".md"), page)
        self.assertIn(f"^publication-{self.post['id']}", page)


if __name__ == "__main__":
    unittest.main()
