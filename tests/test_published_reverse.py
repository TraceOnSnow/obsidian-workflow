from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.sync import export_publication, observed_baseline
from markdown_library.transport import _Store, sync_once
from test_transport import FakePayload


class PublishedReversePollingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Library.initialize(Path(temporary.name) / "vault")
        self.client = FakePayload()

    def setup_note(self, kind="post", properties=None):
        props = ({"link": "stable", "locale": "zh"} if kind == "post" else {"slug": "stable", "type": "book"})
        note = self.library.create("Original body", title="Original", kind=kind,
                                   properties={**props, **(properties or {})})
        publication = self.library.publish(note["id"], expected_hash=note["sha256"])
        export = export_publication(publication)
        remote = self.client.seed(export["collection"], export["fields"])
        state = self.store().load()
        state["baselines"][note["id"]] = observed_baseline(publication, remote)
        self.store().save(state)
        return note, deepcopy(publication), deepcopy(state["baselines"])

    def store(self):
        return _Store(self.library, self.client.target)

    def change(self, collection="posts", **fields):
        self.client.latest[collection]["70"].update({**fields, "updatedAt": self.client.stamp()})

    def accept(self):
        return sync_once(self.library, self.client, apply=True, accept_remote=True, works_drafts_and_versions=True)

    def test_repeated_poll_retains_accepted_draft_without_rewrite_or_republish(self):
        note, selected, baseline = self.setup_note()
        self.change(markdown="Admin first draft", _status="draft")
        self.assertEqual(self.accept()["results"][0]["status"], "remote-draft-imported")
        current = self.library.get(note["id"])
        history = self.library.history(note["id"])
        receipt = deepcopy(self.store().load()["reverse_imports"])
        for apply in (False, True, True):
            result = sync_once(self.library, self.client, apply=apply, accept_remote=True)
            self.assertEqual(result["results"][0]["status"], "unchanged")
            self.assertEqual(self.library.get(note["id"])["sha256"], current["sha256"])
            self.assertEqual(self.library.history(note["id"]), history)
            self.assertEqual(self.store().load()["reverse_imports"], receipt)
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.store().load()["baselines"], baseline)
        self.assertEqual(self.client.mutations, [])

    def test_second_and_third_remote_only_edits_receive_using_accepted_current(self):
        note, selected, baseline = self.setup_note()
        for body in ("Admin first", "Admin second", "Admin third"):
            self.change(markdown=body, _status="draft")
            self.assertEqual(self.accept()["results"][0]["status"], "remote-draft-imported")
            self.assertIn(body, self.library.get(note["id"])["markdown"])
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.store().load()["baselines"], baseline)
        self.assertEqual(self.client.mutations, [])

    def test_local_edit_after_accept_is_retained_and_new_remote_then_conflicts(self):
        note, selected, baseline = self.setup_note()
        self.change(markdown="Admin draft")
        self.accept()
        current = self.library.get(note["id"])
        edited = self.library.replace(note["id"], current["markdown"] + "Local continuation",
                                      expected_hash=current["sha256"])
        for apply in (False, True):
            result = sync_once(self.library, self.client, apply=apply, accept_remote=True)
            self.assertEqual(result["results"][0]["status"], "local-draft")
        self.change(markdown="Admin further draft")
        result = self.accept()
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(self.library.get(note["id"])["sha256"], edited["sha256"])
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.store().load()["baselines"], baseline)
        self.assertEqual(self.client.mutations, [])

    def test_remote_revert_to_successful_baseline_is_still_a_new_draft_observation(self):
        note, selected, baseline = self.setup_note()
        self.change(markdown="Admin changed")
        self.accept()
        self.change(markdown="Original body")
        result = self.accept()
        self.assertEqual(result["results"][0]["status"], "remote-draft-imported")
        self.assertIn("Original body", self.library.get(note["id"])["markdown"])
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.store().load()["baselines"], baseline)
        self.assertEqual(self.client.mutations, [])

    def test_explicit_new_selection_after_accept_still_updates_existing_remote(self):
        note, _, _ = self.setup_note()
        self.change(markdown="Admin draft", _status="draft")
        self.accept()
        current = self.library.get(note["id"])
        edited = self.library.replace(note["id"], current["markdown"].replace("Admin draft", "User final"),
                                      expected_hash=current["sha256"])
        self.library.publish(note["id"], expected_hash=edited["sha256"])
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        self.assertEqual(self.client.base["posts"]["70"]["markdown"], "User final")
        self.assertTrue(all(row[0] == "update" for row in self.client.mutations))

    def test_remote_removed_managed_properties_clear_but_custom_and_work_body_remain(self):
        note, selected, baseline = self.setup_note("work", {"description": "Old description", "rating": 4,
                                                            "custom_private": "Keep"})
        self.change("works", note="Admin reaction")
        self.client.latest["works"]["70"].pop("description")
        self.client.latest["works"]["70"].pop("rating")
        self.assertEqual(self.accept()["results"][0]["status"], "remote-draft-imported")
        current = self.library.get(note["id"])
        self.assertNotIn("description", current["properties"])
        self.assertNotIn("rating", current["properties"])
        self.assertEqual(current["properties"]["custom_private"], "Keep")
        self.assertIn("Original body", current["markdown"])
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.store().load()["baselines"], baseline)

    def test_native_edit_between_remote_check_and_replace_preserves_cas(self):
        note, selected, baseline = self.setup_note()
        self.change(markdown="First")
        self.accept()
        accepted = deepcopy(self.store().load()["reverse_imports"])
        self.change(markdown="Second")
        get = self.client.get
        def racing_get(collection, remote_id, *, draft=True):
            row = get(collection, remote_id, draft=draft)
            path = self.library.vault / note["path"]
            path.write_bytes(path.read_bytes() + b"Native race")
            return row
        self.client.get = racing_get
        result = self.accept()
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertIn("Native race", self.library.get(note["id"])["markdown"])
        self.assertEqual(self.store().load()["reverse_imports"], accepted)
        self.assertEqual(self.store().load()["baselines"], baseline)
        self.assertEqual(self.library.publications(), [selected])
