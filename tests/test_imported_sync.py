from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.bootstrap import bootstrap
from markdown_library.transport import _Store, map_imported, sync_once
from test_imported_mapping import MappingPayload


class ImportedDraftSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Library.initialize(Path(temporary.name) / "vault")
        self.client = MappingPayload()

    def imported(self, collection="works", **extra):
        fields = ({"title": "Work", "slug": "work", "type": "book", "visibility": "private",
                   "note": "Old impression", "description": "Old description", "markdown": "PRIVATE WORK BODY"}
                  if collection == "works" else
                  {"title": "Post", "link": "post", "locale": "zh", "markdown": "Old draft"})
        self.client.seed(collection, 501, **{**fields, **extra})
        result = bootstrap(self.library, client=self.client, apply=True)["results"]
        mapped = map_imported(self.library, self.client, [row["id"] for row in result], apply=True)
        self.assertTrue(all(row["status"] == "mapped-imported-draft" for row in mapped["results"]))
        return result[0]

    def change(self, collection="works", **fields):
        # Admin draft editing changes latest while the underlying base remains.
        self.client.latest[collection]["501"].update({"updatedAt": "admin-v2", **fields})

    def state(self):
        return _Store(self.library, self.client.target).load()

    def files(self):
        return {str(path.relative_to(self.library.vault)): path.read_bytes()
                for path in self.library.vault.rglob("*") if path.is_file()}

    def test_readonly_plan_and_apply_without_accept_do_not_change_draft(self):
        note = self.imported()
        self.change(note="New impression")
        before = self.files()
        planned = sync_once(self.library, self.client)
        self.assertEqual(planned["plan"]["actions"][0]["action"], "reverse-change")
        self.assertEqual(planned["results"][0]["status"], "remote-draft-available")
        self.assertEqual(self.files(), before)
        sync_once(self.library, self.client, apply=True)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.publications(), [])

    def test_accept_remote_work_keeps_private_body_and_clears_removed_field(self):
        note = self.imported()
        original_baseline = deepcopy(self.state()["baselines"][note["id"]])
        self.change(note="New impression", title="Renamed")
        self.client.latest["works"]["501"].pop("description")
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result["results"][0]["status"], "remote-draft-imported")
        current = self.library.get(note["id"])
        self.assertEqual(current["properties"]["note"], "New impression")
        self.assertEqual(current["properties"]["title"], "Renamed")
        self.assertNotIn("description", current["properties"])
        self.assertIn("PRIVATE WORK BODY", current["markdown"])
        baseline = self.state()["baselines"][note["id"]]
        self.assertEqual(baseline["imported_sha256"], original_baseline["imported_sha256"])
        self.assertEqual(baseline, original_baseline)
        accepted = self.state()["reverse_imports"][note["id"]]
        self.assertEqual(accepted["local_hash"], current["sha256"])
        self.assertEqual(accepted["remote_fields"]["note"], "New impression")
        self.assertEqual(baseline["state"], "imported-draft")
        again = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(again["results"][0]["status"], "unchanged")
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.publications(), [])

    def test_accept_post_then_explicit_publish_still_updates_same_record(self):
        note = self.imported("posts")
        self.change("posts", markdown="Admin new body")
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result["results"][0]["status"], "remote-draft-imported")
        current = self.library.get(note["id"])
        self.assertIn("Admin new body", current["markdown"])
        self.assertEqual(self.client.mutations, [])
        self.library.publish(note["id"], expected_hash=current["sha256"])
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        self.assertEqual([row[1] for row in self.client.mutations], ["501", "501"])

    def test_private_work_body_and_remote_public_field_merge_preserve_baseline(self):
        note = self.imported()
        file = self.library.vault / note["path"]
        file.write_bytes(file.read_bytes() + b"Native local edit")
        before = file.read_bytes()
        baseline = deepcopy(self.state())
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result["results"][0]["status"], "local-draft")
        self.change(note="Admin changed")
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result["results"][0]["status"], "remote-draft-imported")
        current = self.library.get(note["id"])
        from markdown_library.core import _split
        self.assertEqual(_split(current["markdown"])[1], _split(before.decode())[1])
        self.assertEqual(current["properties"]["note"], "Admin changed")
        self.assertEqual(self.state()["baselines"], baseline["baselines"])
        self.assertEqual(self.library.publications(), [])
        self.assertEqual(self.client.mutations, [])

    def test_scoped_publish_does_not_receive_unrelated_imported_draft(self):
        other = self.imported()
        self.change(note="Unrelated admin edit")
        before = self.library.get(other["id"])
        from test_transport import FakePayload
        client = FakePayload()
        client.target = self.client.target
        client.latest, client.base = deepcopy(self.client.latest), deepcopy(self.client.base)
        self.client = client
        note = self.library.create("New post", title="Scoped post", kind="post", properties={"link": "scoped"})
        self.library.publish(note["id"], expected_hash=note["sha256"])
        result = sync_once(self.library, self.client, apply=True, accept_remote=True, only_ids=[note["id"]])
        self.assertEqual([row["id"] for row in result["results"]], [note["id"]])
        self.assertEqual(self.library.get(other["id"])["sha256"], before["sha256"])
        self.assertNotIn(other["id"], self.state()["reverse_imports"])

    def test_new_protection_or_unknown_theme_is_rejected(self):
        note = self.imported("posts")
        baseline = deepcopy(self.state())
        original = self.library.get(note["id"])["markdown"]
        for fields in ({"protectedContent": True}, {"frontmatter": {"password": "SECRET"}},
                       {"markdown": ":::encrypted\nSECRET\n:::"}, {"frontmatter": {"unknown": True}}):
            self.client.latest["posts"]["501"] = deepcopy(self.client.base["posts"]["501"])
            self.change("posts", **fields)
            result = sync_once(self.library, self.client, apply=True, accept_remote=True)
            self.assertEqual(result["results"][0]["status"], "conflict")
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertEqual(self.library.get(note["id"])["markdown"], original)
            self.assertEqual(self.state(), baseline)

    def test_selected_snapshot_is_never_changed_by_imported_draft_flow(self):
        note = self.imported()
        self.library.publish(note["id"], expected_hash=note["sha256"])
        selected = self.library.publications()
        original = self.library.get(note["id"])["markdown"]
        self.change(note="Admin draft")
        result = sync_once(self.library, self.client, apply=True, accept_remote=True, works_drafts_and_versions=True)
        self.assertEqual(result["results"][0]["status"], "remote-draft-imported")
        self.assertEqual(self.library.publications(), selected)
        current = self.library.get(note["id"])
        self.assertEqual(current["properties"]["note"], "Admin draft")
        from markdown_library.core import _split
        self.assertEqual(_split(current["markdown"])[1], _split(original)[1])
        self.assertEqual(self.client.mutations, [])

    def test_unknown_relationship_and_local_race_do_not_replace(self):
        note = self.imported()
        self.change(article=999)
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.change(article=None, note="Admin draft")
        get = self.client.get
        def racing_get(collection, remote_id, *, draft=True):
            row = get(collection, remote_id, draft=draft)
            if not draft:
                file = self.library.vault / note["path"]
                file.write_bytes(file.read_bytes() + b"Native race")
            return row
        self.client.get = racing_get
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertTrue((self.library.vault / note["path"]).read_bytes().endswith(b"Native race"))
        self.assertEqual(self.client.mutations, [])

    def test_admin_publication_is_received_without_local_selection_or_automatic_outgoing_write(self):
        for collection in ("works", "posts"):
            with self.subTest(collection=collection):
                self.setUp()
                note = self.imported(collection)
                fields = {"_status": "published"}
                if collection == "works": fields["visibility"] = "public"
                self.change(collection, **fields)
                self.client.base[collection]["501"] = deepcopy(self.client.latest[collection]["501"])
                received = sync_once(self.library, self.client, apply=True, accept_remote=True)
                self.assertEqual(received["results"][0]["status"], "remote-draft-imported")
                observed = self.state()["baselines"][note["id"]]
                self.assertEqual(observed["state"], "imported-draft")
                accepted = self.state()["reverse_imports"][note["id"]]
                self.assertEqual(accepted["remote_fields"]["_status"], "published")
                if collection == "works": self.assertEqual(accepted["remote_fields"]["visibility"], "public")
                self.assertEqual(observed["observed_status"], "draft")
                self.assertEqual(self.library.publications(), [])
                self.assertEqual(self.client.mutations, [])
                current = self.library.get(note["id"])
                edited = self.library.replace(note["id"], current["markdown"] + "Local private continuation",
                                              expected_hash=current["sha256"])
                polled = sync_once(self.library, self.client, apply=True, accept_remote=True)
                self.assertEqual(polled["results"][0]["status"], "local-draft")
                self.assertEqual(self.client.mutations, [])
                self.library.publish(note["id"], expected_hash=edited["sha256"])
                result = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
                if collection == "works":
                    # Work private prose is absent from the public export; an
                    # explicit choice can converge against Admin publication.
                    self.assertEqual(result["results"][0]["status"], "reconciled")
                    self.assertEqual(self.client.mutations, [])
                else:
                    self.assertEqual(result["results"][0]["status"], "observed")
                    self.assertEqual([row[1] for row in self.client.mutations], ["501", "501"])
                self.assertEqual(self.state()["baselines"][note["id"]]["remote_id"], 501)
                self.assertEqual(self.state()["baselines"][note["id"]]["state"], "published")
