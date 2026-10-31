import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library, LibraryError
from markdown_library.core import _hash, _split
from markdown_library.mcp_actions import LibraryActions


class LibraryActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.vault = Path(temporary.name) / "vault"
        self.library = Library.initialize(self.vault)
        self.actions = LibraryActions(self.library)

    def note(self, path="notes/old.md", body="original\n", kind="note", properties=None):
        return self.library.create(body, title=Path(path).stem, kind=kind, path=path, properties=properties)

    def write(self, path, markdown):
        file = self.vault / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(markdown, encoding="utf-8")

    def read(self, path):
        return (self.vault / path).read_text(encoding="utf-8")

    def state(self):
        return json.loads(self.read(".markdown-library/state.json"))

    def hashes(self, *notes):
        return {note["id"]: note["sha256"] for note in notes}

    def merged(self, target, body="merged body\n"):
        original_body = _split(target["markdown"])[1]
        return target["markdown"][:-len(original_body)] + body

    def test_move_repairs_navigation_managed_links_aliases_anchors_and_outgoing_links(self):
        old = self.note(body="[asset](../assets/image.png)\n[[./next|next]]\n")
        self.note("notes/next.md")
        linked = self.note("notes/references.md", "[[notes/old#Heading|别名]] [正文](old.md#part)\n")
        self.write("首页.md", "[[old|旧稿]]\n[读](notes/old.md#one)\n[ref]: notes/old.md \"说明\"\n")
        self.write("assets/image.png", "image")
        with patch.object(self.library, "_commit", wraps=self.library._commit) as commit:
            result = self.actions.move(old["id"], "20_项目与构想/sub/new.md", old["sha256"])
        self.assertEqual(commit.call_count, 1)
        self.assertFalse((self.vault / old["path"]).exists())
        self.assertEqual(self.library.get(old["id"])["path"], result["path"])
        self.assertIn("[[20_项目与构想/sub/new#Heading|别名]]", self.read(linked["path"]))
        self.assertIn("../../assets/image.png", self.read(result["path"]))
        self.assertIn("[[notes/next|next]]", self.read(result["path"]))
        self.assertIn("%E9%A1%B9", self.read("首页.md"))
        self.assertIn("[ref]: 20_", self.read("首页.md"))
        self.assertEqual(result["links_updated"], 7)
        self.assertFalse(result["publication_selected"])

    def test_move_repairs_table_wikilinks_without_losing_escaped_alias_delimiters(self):
        note = self.library.create("# Question\n", title="Old", path="notes/old.md")
        self.write("notes/table.md", "| Note | Anchored |\n| --- | --- |\n"
                   r"| [[notes/old\|Label]] | [[notes/old#Question\|Section]] |" + "\n")
        result = self.actions.move(note["id"], "90_归档/new.md", note["sha256"])
        self.assertIn(r"[[90_归档/new\|Label]]", self.read("notes/table.md"))
        self.assertIn(r"[[90_归档/new#Question\|Section]]", self.read("notes/table.md"))
        self.assertEqual(result["links_updated"], 2)

    def test_move_preserves_fenced_and_inline_code_and_external_links(self):
        note = self.note()
        literal = "```md\n[[notes/old]] [x](notes/old.md)\n```\n~~~\n[[old]]\n~~~\n`[[old]]` [web](https://example.org/notes/old.md)\n"
        self.write("首页.md", literal + "[[notes/old]]\n")
        result = self.actions.move(note["id"], "90_归档/new.md", note["sha256"])
        self.assertEqual(self.read("首页.md"), literal + "[[90_归档/new]]\n")
        self.assertEqual(result["links_updated"], 1)

    def test_move_rejects_stale_hash_without_mutation(self):
        note = self.note()
        self.write(note["path"], note["markdown"] + "native edit")
        before = self.state()
        with self.assertRaisesRegex(LibraryError, "Revision conflict"):
            self.actions.move(note["id"], "notes/new.md", note["sha256"])
        self.assertEqual(self.state(), before)
        self.assertTrue(self.read(note["path"]).endswith("native edit"))

    def test_move_rejects_unsafe_configuration_navigation_and_occupied_destinations(self):
        note = self.note()
        self.write("notes/taken.md", "user file")
        self.write("notes/directory.md/keep.txt", "keep")
        for destination in ("../escape.md", "/tmp/escape.md", "works/new.md", "notes/.private.md",
                            "notes/../escape.md", "notes/索引.md", "notes/config/new.md",
                            "notes/new.txt", "notes/taken.md", "notes/directory.md", "notes/a#b.md",
                            "notes\\evil.md"):
            with self.subTest(destination=destination), self.assertRaises(LibraryError):
                self.actions.move(note["id"], destination, note["sha256"])
        self.assertEqual(self.read("notes/taken.md"), "user file")

    def test_move_rejects_symlink_parent_and_non_note(self):
        note = self.note()
        (self.vault / "notes/link").symlink_to(self.vault / "notes", target_is_directory=True)
        with self.assertRaisesRegex(LibraryError, "Symlink"):
            self.actions.move(note["id"], "notes/link/new.md", note["sha256"])
        (self.vault / "notes/link").unlink()
        post = self.note("posts/post.md", kind="post")
        with self.assertRaisesRegex(LibraryError, "Only notes"):
            self.actions.move(post["id"], "notes/post.md", post["sha256"])

    def test_ambiguous_short_wikilink_rejected_but_explicit_path_allowed(self):
        note = self.note()
        self.note("30_知识与参考/old.md")
        self.write("首页.md", "[[old|alias]]\n")
        with self.assertRaisesRegex(LibraryError, "Ambiguous wikilink"):
            self.actions.move(note["id"], "notes/new.md", note["sha256"])
        self.assertEqual(self.read("首页.md"), "[[old|alias]]\n")
        self.write("首页.md", "[[notes/old|alias]]\n")
        self.actions.move(note["id"], "notes/new.md", note["sha256"])
        self.assertEqual(self.read("首页.md"), "[[notes/new|alias]]\n")

    def test_markdown_angles_percent_encoding_escaped_parentheses_queries_and_reference_links(self):
        note = self.note("notes/old (A).md")
        self.write("首页.md", '[read](<notes/old (A).md#标题> "caption")\n'
                   '[encoded](notes/old%20%28A%29.md?x=1#part)\n'
                   '[escaped](notes/old%20\\(A\\).md)\n'
                   '[ref]: <notes/old (A).md#end> "caption"\n')
        self.actions.move(note["id"], "notes/new.md", note["sha256"])
        self.assertEqual(self.read("首页.md"), '[read](<notes/new.md#标题> "caption")\n'
                         '[encoded](notes/new.md?x=1#part)\n[escaped](notes/new.md)\n'
                         '[ref]: <notes/new.md#end> "caption"\n')

    def test_unreliable_link_syntax_is_rejected_instead_of_leaving_a_dangling_link(self):
        note = self.note("notes/old(a(b)).md")
        self.write("首页.md", "[read](notes/old(a(b)).md)\n")
        with self.assertRaisesRegex(LibraryError, "Unsupported local Markdown link"):
            self.actions.move(note["id"], "notes/new.md", note["sha256"])
        self.assertTrue((self.vault / note["path"]).exists())
        self.assertFalse((self.vault / ".markdown-library/transaction.json").exists())

    def test_move_rejects_ambiguous_outgoing_short_link_that_could_change_resolution(self):
        note = self.note(body="[[neighbor]]\n")
        self.note("notes/neighbor.md")
        self.note("90_归档/neighbor.md")
        with self.assertRaisesRegex(LibraryError, "Ambiguous wikilink"):
            self.actions.move(note["id"], "90_归档/new.md", note["sha256"])
        self.assertTrue((self.vault / note["path"]).exists())

    def test_move_rejects_file_parent_before_creating_a_transaction(self):
        note = self.note()
        self.write("notes/parent", "keep")
        with self.assertRaisesRegex(LibraryError, "parent is occupied"):
            self.actions.move(note["id"], "notes/parent/new.md", note["sha256"])
        self.assertFalse((self.vault / ".markdown-library/transaction.json").exists())

    def test_navigation_edit_between_scan_and_commit_is_rejected(self):
        note = self.note()
        self.write("首页.md", "[[notes/old]]\n")
        original = self.library._commit

        def racing_commit(*args, **kwargs):
            self.write("首页.md", "user edit\n")
            return original(*args, **kwargs)

        with patch.object(self.library, "_commit", side_effect=racing_commit):
            with self.assertRaisesRegex(LibraryError, "external edit"):
                self.actions.move(note["id"], "notes/new.md", note["sha256"])
        self.assertEqual(self.read("首页.md"), "user edit\n")
        self.assertTrue((self.vault / note["path"]).exists())
        self.assertFalse((self.vault / "notes/new.md").exists())

    def test_move_journal_recovers_after_interruption(self):
        note = self.note()
        original = self.library._atomic

        def interrupt(relative, markdown):
            if relative == "notes/new.md":
                raise OSError("simulated interruption")
            return original(relative, markdown)

        with patch.object(self.library, "_atomic", side_effect=interrupt):
            with self.assertRaisesRegex(LibraryError, "simulated interruption"):
                self.actions.move(note["id"], "notes/new.md", note["sha256"])
        self.assertTrue((self.vault / ".markdown-library/transaction.json").exists())
        recovered = Library(self.vault).get(note["id"])
        self.assertEqual(recovered["path"], "notes/new.md")
        self.assertFalse((self.vault / note["path"]).exists())

    def test_merge_retains_identity_properties_sources_history_trash_and_links(self):
        target = self.note("posts/main.md", kind="post", properties={"tags": ["original"], "custom": {"keep": True}})
        source = self.library.import_note("source body\n", source_id="chat:one", title="source", source={"original": "private"})
        self.write("首页.md", f"[[{source['path'][:-3]}#Part|别名]]\n[来源]({source['path']}#part)\n")
        before_sources = copy.deepcopy(self.state()["sources"])
        result = self.actions.merge(target["id"], [source["id"]], self.merged(target), self.hashes(target, source))
        current = self.library.get(target["id"])
        self.assertEqual(current["properties"], target["properties"])
        self.assertEqual(_split(current["markdown"])[1], "merged body\n")
        state = self.state()
        record = state["notes"][source["id"]]
        self.assertTrue(record["deleted"])
        self.assertEqual(record["merged_into"], target["id"])
        self.assertEqual(self.read(record["trash"]), source["markdown"])
        mapping = state["sources"]["chat:one"]
        self.assertEqual(mapping["source"], before_sources["chat:one"]["source"])
        self.assertEqual(mapping["id"], target["id"])
        self.assertEqual(mapping["original_library_id"], source["id"])
        self.assertEqual(mapping["merge_redirects"][-1]["from_id"], source["id"])
        self.assertEqual(mapping["merge_redirects"][-1]["to_id"], target["id"])
        self.assertEqual(state["notes"][target["id"]]["merges"][-1]["sources"][0]["sha256"], source["sha256"])
        self.assertEqual(self.library.history_markdown(source["id"], source["sha256"]), source["markdown"])
        self.assertEqual(self.read("首页.md"), "[[posts/main#Part|别名]]\n[来源](posts/main.md#part)\n")
        restored = self.library.recover(source["id"], expected_hash=source["sha256"])
        self.assertEqual(restored["markdown"], source["markdown"])
        self.assertEqual(result["source_ids"], [source["id"]])
        self.assertFalse(result["publication_selected"])

    def test_merge_redirects_repeat_import_to_surviving_draft_without_overwriting(self):
        target = self.note("notes/main.md")
        provenance = {"upstream": "chat", "nested": {"ids": ["one", "two"]}}
        source = self.library.import_note("source text", source_id="chat:repeat", title="source", source=provenance)
        self.actions.merge(target["id"], [source["id"]], self.merged(target), self.hashes(target, source))
        surviving = self.library.get(target["id"])
        repeated = self.library.import_note("stale regenerated text", source_id="chat:repeat", title="wrong title", source={"replacement": "must not replace provenance"})
        self.assertEqual(repeated["outcome"], "existing")
        self.assertEqual(repeated["id"], target["id"])
        self.assertEqual(repeated["markdown"], surviving["markdown"])
        self.assertEqual(repeated["sha256"], surviving["sha256"])
        mapping = self.state()["sources"]["chat:repeat"]
        self.assertEqual(mapping["source"], provenance)
        self.assertEqual(mapping["original_library_id"], source["id"])
        self.assertTrue(self.state()["notes"][source["id"]]["deleted"])

    def test_chained_merges_preserve_all_import_provenance_and_redirect_audit(self):
        first = self.library.import_note("first", source_id="chat:first", title="first", source={"origin": "first", "extra": [1, 2]})
        intermediate = self.library.import_note("intermediate", source_id="chat:intermediate", title="intermediate", source={"origin": "intermediate"})
        final = self.note("notes/final.md")
        original_sources = copy.deepcopy(self.state()["sources"])
        self.actions.merge(intermediate["id"], [first["id"]], self.merged(intermediate, "first merge\n"), self.hashes(first, intermediate))
        intermediate = self.library.get(intermediate["id"])
        first_audit = copy.deepcopy(self.state()["sources"]["chat:first"]["merge_redirects"])
        self.actions.merge(final["id"], [intermediate["id"]], self.merged(final, "final merge\n"), self.hashes(final, intermediate))
        mappings = self.state()["sources"]
        for key, original in original_sources.items():
            self.assertEqual(mappings[key]["source"], original["source"])
            self.assertEqual(mappings[key]["id"], final["id"])
            self.assertEqual(mappings[key]["original_library_id"], original["id"])
            repeated = self.library.import_note("do not overwrite", source_id=key, title="stale")
            self.assertEqual(repeated["id"], final["id"])
            self.assertEqual(_split(repeated["markdown"])[1], "final merge\n")
        redirects = mappings["chat:first"]["merge_redirects"]
        self.assertEqual(redirects[:1], first_audit)
        self.assertEqual([(row["from_id"], row["to_id"]) for row in redirects],
                         [(first["id"], intermediate["id"]), (intermediate["id"], final["id"])])
        for note in (first, intermediate):
            self.assertEqual(self.library.history_markdown(note["id"], note["sha256"]), note["markdown"])

    def test_merge_does_not_change_publication_snapshot(self):
        target = self.note("posts/main.md", kind="post")
        source = self.note()
        self.library.publish(target["id"], expected_hash=target["sha256"])
        publications = self.library.publications()
        self.actions.merge(target["id"], [source["id"]], self.merged(target), self.hashes(target, source))
        self.assertEqual(self.library.publications(), publications)
        self.assertEqual(self.library.publication(target["id"])["markdown"], "original\n")

    def test_merge_validates_every_hash_and_all_target_properties(self):
        target, source = self.note("notes/target.md"), self.note()
        for hashes in ({}, {target["id"]: target["sha256"]}, {**self.hashes(target, source), source["id"]: "bad"}):
            with self.assertRaisesRegex(LibraryError, "Revision conflict"):
                self.actions.merge(target["id"], [source["id"]], self.merged(target), hashes)
        for markdown in ("body without metadata", target["markdown"].replace("title: target", "title: changed"),
                         target["markdown"].replace("kind: note", "kind: post")):
            with self.assertRaisesRegex(LibraryError, "preserve all target properties"):
                self.actions.merge(target["id"], [source["id"]], markdown, self.hashes(target, source))
        self.assertEqual(self.library.get(source["id"])["markdown"], source["markdown"])

    def test_merge_rejects_mapped_source_and_keeps_sync_file_byte_identical(self):
        target, source = self.note("notes/target.md"), self.note()
        filename = ".markdown-library/sync-state.json"
        sync = json.dumps({"schema_version": 1, "target": "https://example.org", "baselines": {source["id"]: {"collection": "posts", "remote_id": 1}}, "pending": {}})
        self.write(filename, sync)
        with self.assertRaisesRegex(LibraryError, "website-mapped source"):
            self.actions.merge(target["id"], [source["id"]], self.merged(target), self.hashes(target, source))
        self.assertEqual(self.read(filename), sync)

    def test_merge_rejects_bootstrap_mapping_and_corrupt_sync_state(self):
        target, source = self.note("notes/target.md"), self.note()
        self.write(".markdown-library/bootstrap-state.json", json.dumps({"imports": {"remote": {"id": source["id"]}}}))
        with self.assertRaisesRegex(LibraryError, "website-mapped source"):
            self.actions.merge(target["id"], [source["id"]], self.merged(target), self.hashes(target, source))
        (self.vault / ".markdown-library/bootstrap-state.json").unlink()
        self.write(".markdown-library/sync-state.json", "{malformed")
        with self.assertRaisesRegex(LibraryError, "Cannot verify"):
            self.actions.merge(target["id"], [source["id"]], self.merged(target), self.hashes(target, source))

    def test_merge_unchanged_target_is_still_guarded_against_a_concurrent_edit(self):
        target, source = self.note("notes/target.md"), self.note()
        original = self.library._commit

        def racing_commit(*args, **kwargs):
            self.write(target["path"], target["markdown"] + "user edit")
            return original(*args, **kwargs)

        with patch.object(self.library, "_commit", side_effect=racing_commit):
            with self.assertRaisesRegex(LibraryError, "external edit"):
                self.actions.merge(target["id"], [source["id"]], target["markdown"], self.hashes(target, source))
        self.assertTrue((self.vault / source["path"]).exists())
        self.assertTrue(self.read(target["path"]).endswith("user edit"))

    def test_merge_rejects_invalid_source_identity_kind_duplicates_and_missing(self):
        target, source = self.note("notes/target.md"), self.note()
        post = self.note("posts/source.md", kind="post")
        for ids in ([], [source["id"], source["id"]], [target["id"]], ["missing"], [post["id"]]):
            with self.subTest(ids=ids), self.assertRaises(LibraryError):
                self.actions.merge(target["id"], ids, self.merged(target), self.hashes(target, source, post))

    def test_merge_multiple_sources_is_one_recoverable_transaction(self):
        target, source, second = self.note("notes/target.md"), self.note(), self.note("notes/second.md")
        self.write("首页.md", "[[notes/old]] [[notes/second]]\n")
        original = self.library._atomic
        stopped = False

        def interrupt(relative, markdown):
            nonlocal stopped
            if relative.startswith(".markdown-library/trash/") and not stopped:
                stopped = True
                raise OSError("simulated interruption")
            return original(relative, markdown)

        with patch.object(self.library, "_atomic", side_effect=interrupt):
            with self.assertRaisesRegex(LibraryError, "simulated interruption"):
                self.actions.merge(target["id"], [source["id"], second["id"]], self.merged(target), self.hashes(target, source, second))
        recovered = Library(self.vault)
        self.assertEqual(_split(recovered.get(target["id"])["markdown"])[1], "merged body\n")
        self.assertEqual(self.read("首页.md"), "[[notes/target]] [[notes/target]]\n")
        for note in (source, second):
            self.assertFalse((self.vault / note["path"]).exists())
            self.assertEqual(recovered.recover(note["id"], expected_hash=note["sha256"])["markdown"], note["markdown"])


if __name__ == "__main__":
    unittest.main()
