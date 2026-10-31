from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library
from markdown_library.bootstrap import bootstrap
from markdown_library.cli import main
from markdown_library.sync import export_publication, observed_baseline
from markdown_library.transport import _Store, adopt_existing, sync_once


class ReadOnlyPayload:
    target = "https://adoption.example"

    def __init__(self):
        self.latest = {"posts": [], "works": []}
        self.base = {"posts": [], "works": []}
        self.gets = []

    def list_collection(self, collection, *, draft=True):
        return deepcopy((self.latest if draft else self.base)[collection])

    def get(self, collection, remote_id, *, draft=True):
        self.gets.append((collection, str(remote_id), draft))
        return deepcopy(next(row for row in (self.latest if draft else self.base)[collection] if str(row["id"]) == str(remote_id)))

    def seed(self, collection, fields, remote_id=101):
        row = {**deepcopy(fields), "id": remote_id, "updatedAt": "2020-01-02T00:00:00.000Z", "_status": "published"}
        self.latest[collection].append(deepcopy(row))
        self.base[collection].append(deepcopy(row))
        return row

    def create(self, *args, **kwargs):
        raise AssertionError("Adoption never calls a remote mutation")

    update = create
    delete = create


class AdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Library.initialize(Path(self.temporary.name) / "vault")
        self.client = ReadOnlyPayload()

    def post(self, *, link="notes/original", selected=True):
        note = self.library.create("Published body", title="Post", kind="post", properties={"link": link, "locale": "en"})
        publication = self.library.publish(note["id"], expected_hash=note["sha256"]) if selected else None
        return note, publication

    def seed(self, publication, remote_id=101):
        exported = export_publication(publication)
        return self.client.seed(exported["collection"], exported["fields"], remote_id)

    def state(self):
        return _Store(self.library, self.client.target).load()

    def save_state(self, *, baselines=None, pending=None):
        store = _Store(self.library, self.client.target)
        state = store.load()
        state["baselines"].update(baselines or {})
        state["pending"].update(pending or {})
        store.save(state)

    def test_preview_and_explicit_apply_only_save_requested_baseline(self):
        note, publication = self.post()
        row = self.seed(publication)
        other, other_publication = self.post(link="notes/another")
        self.seed(other_publication, remote_id=102)
        original_notes = {item["id"]: item["markdown"] for item in self.library.list_notes()}
        original_publications = self.library.publications()
        default = sync_once(self.library, self.client)
        self.assertTrue(all(row["status"] == "adopt-review" for row in default["results"]))
        preview = adopt_existing(self.library, self.client, [note["id"]])
        self.assertEqual(preview["results"][0]["status"], "adoptable")
        self.assertEqual(self.state()["baselines"], {})
        applied = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(applied["results"][0]["status"], "adopted")
        self.assertEqual(set(self.state()["baselines"]), {note["id"]})
        self.assertNotIn(other["id"], self.state()["baselines"])
        self.assertEqual(self.library.publications(), original_publications)
        self.assertEqual({item["id"]: item["markdown"] for item in self.library.list_notes()}, original_notes)
        self.assertIn(("posts", "101", True), self.client.gets)
        self.assertIn(("posts", "101", False), self.client.gets)

    def test_differing_latest_and_base_fields_or_states_rejected(self):
        for mode in ("content", "state"):
            with self.subTest(mode=mode):
                note, publication = self.post(link="notes/" + mode)
                self.seed(publication, remote_id=101 if mode == "content" else 102)
                self.client.latest["posts"][-1].update({"description": "Latest only"} if mode == "content" else {"_status": "draft"})
                result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
                self.assertEqual(result["results"][0]["status"], "rejected")
                self.assertNotIn(note["id"], self.state()["baselines"])

    def test_selected_field_or_nested_theme_mismatch_rejected(self):
        note, publication = self.post()
        self.seed(publication)
        for rows in (self.client.latest, self.client.base):
            rows["posts"][0]["markdown"] = "A different published body"
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(self.state()["baselines"], {})
        nested = self.library.create("Other body", title="Nested", kind="post", properties={"link": "nested", "locale": "en", "frontmatter": {}})
        selected = self.library.publish(nested["id"], expected_hash=nested["sha256"])
        fields = export_publication(selected)["fields"]; fields["frontmatter"] = {"math": True}
        self.client.seed("posts", fields, 102)
        result = adopt_existing(self.library, self.client, [nested["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")

    def test_date_normalization_accepts_equivalent_payload_datetimes(self):
        note = self.library.create("Body", title="Date", kind="post", properties={"link": "date", "locale": "en", "publishedAt": "2020-01-02T08:00:00+08:00"})
        selected = self.library.publish(note["id"], expected_hash=note["sha256"])
        fields = export_publication(selected)["fields"]; fields["publishedAt"] = "2020-01-02T00:00:00.000Z"
        self.client.seed("posts", fields)
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "adopted")

    def test_duplicate_remote_or_local_identity_rejected(self):
        note, publication = self.post()
        self.seed(publication)
        self.seed(publication, remote_id=102)
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.client.latest["posts"].pop()
        self.client.base["posts"].pop()
        self.post()  # Another selected local document claims the same route.
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(self.state()["baselines"], {})

    def test_pending_and_existing_baseline_are_never_overwritten(self):
        note, publication = self.post()
        row = self.seed(publication)
        self.save_state(pending={note["id"]: {"phase": "reconciliation-required", "identity": {"link": "notes/original", "locale": "en"}}})
        path = self.library.vault / ".markdown-library/sync-state.json"
        before = path.read_bytes()
        rejected = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(rejected["results"][0]["status"], "rejected")
        self.assertEqual(path.read_bytes(), before)
        state = self.state(); state["pending"] = {}; state["baselines"][note["id"]] = observed_baseline(publication, row)
        _Store(self.library, self.client.target).save(state)
        before = path.read_bytes()
        rejected = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(rejected["results"][0]["status"], "rejected")
        self.assertEqual(path.read_bytes(), before)

    def test_remote_claim_by_another_pending_or_baseline_rejected(self):
        note, publication = self.post()
        row = self.seed(publication)
        self.save_state(pending={"another-id": {"identity": {"link": "notes/original", "locale": "en"}, "remote_id": 101}})
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        state = self.state(); state["pending"] = {}; state["baselines"]["another-id"] = observed_baseline(publication, row)
        _Store(self.library, self.client.target).save(state)
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")

    def test_linked_post_must_be_observed_before_work_adoption(self):
        post, publication = self.post()
        self.seed(publication)
        work = self.library.create("PRIVATE WORK BODY", title="Work", kind="work", properties={"slug": "work", "type": "book", "article": post["id"]})
        selected_work = self.library.publish(work["id"], expected_hash=work["sha256"])
        fields = export_publication(selected_work)["fields"]; fields["article"] = 101
        self.client.seed("works", fields, remote_id=201)
        only_work = adopt_existing(self.library, self.client, [work["id"]], apply=True)
        self.assertEqual(only_work["results"][0]["status"], "rejected")
        preview = adopt_existing(self.library, self.client, [work["id"], post["id"]])
        self.assertTrue(all(item["status"] == "adoptable" for item in preview["results"]))
        self.assertEqual(self.state()["baselines"], {})
        applied = adopt_existing(self.library, self.client, [work["id"], post["id"]], apply=True)
        self.assertTrue(all(item["status"] == "adopted" for item in applied["results"]))
        self.assertEqual(set(self.state()["baselines"]), {post["id"], work["id"]})
        self.assertIn("PRIVATE WORK BODY", self.library.get(work["id"])["markdown"])

    def test_protected_remote_content_rejected_even_when_flag_stale(self):
        note, publication = self.post()
        self.seed(publication)
        for mode in ("flag", "password", "encrypted"):
            for rows in (self.client.latest, self.client.base):
                row = rows["posts"][0]
                row.pop("frontmatter", None); row["protectedContent"] = False; row["markdown"] = "Published body"
                if mode == "flag": row["protectedContent"] = True
                if mode == "password": row["frontmatter"] = {"password": "DO NOT PRINT"}
                if mode == "encrypted": row["markdown"] = ":::encrypted\nPRIVATE\n:::"
            result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
            self.assertEqual(result["results"][0]["status"], "rejected")
            self.assertNotIn("DO NOT PRINT", json.dumps(result))
        self.assertEqual(self.state()["baselines"], {})

    def test_no_selected_snapshot_or_withdrawn_snapshot_cannot_be_adopted(self):
        note, _ = self.post(selected=False)
        unselected = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(unselected["results"][0]["status"], "rejected")
        publication = self.library.publish(note["id"], expected_hash=note["sha256"])
        self.seed(publication)
        self.library.unpublish(note["id"])
        withdrawn = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(withdrawn["results"][0]["status"], "rejected")
        self.assertEqual(self.state()["baselines"], {})

    def imported_post(self):
        remote = self.client.seed("posts", {"title": "Imported", "link": "imported", "locale": "en", "markdown": "Imported body"}, remote_id=101)
        snapshot = {"rating_scale": 100, "target": self.client.target, "posts": [remote], "works": [], "published": {"posts": [remote], "works": []}}
        imported = bootstrap(self.library, snapshot=snapshot, apply=True)["results"][0]
        current = self.library.get(imported["id"])
        self.library.publish(current["id"], expected_hash=current["sha256"])
        return current

    def test_bootstrap_target_and_known_remote_id_must_match(self):
        note = self.imported_post()
        self.client.target = "https://wrong-target.example"
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.client.target = "https://adoption.example"
        for rows in (self.client.latest, self.client.base): rows["posts"][0]["id"] = 999
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertEqual(self.state()["baselines"], {})

    def test_bootstrap_then_explicit_selection_and_adoption_needs_no_manual_state_edit(self):
        note = self.imported_post()
        result = adopt_existing(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "adopted")
        sync = sync_once(self.library, self.client)
        self.assertEqual(sync["results"][0]["status"], "unchanged")

    def test_another_imported_document_claim_blocks_adoption_of_a_clone(self):
        original = self.imported_post()
        self.library.unpublish(original["id"])
        clone = self.library.create("Imported body", title="Imported", kind="post", properties={"link": "imported", "locale": "en"})
        self.library.publish(clone["id"], expected_hash=clone["sha256"])
        result = adopt_existing(self.library, self.client, [clone["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertIn("already claimed", result["results"][0]["reason"])
        self.assertEqual(self.state()["baselines"], {})

    def test_cli_uses_explicit_ids_and_env_credentials_without_remote_writes(self):
        note, publication = self.post()
        self.seed(publication)
        stdout = io.StringIO()
        with patch("markdown_library.transport.PayloadClient", return_value=self.client), patch.dict(os.environ, {"ADOPT_FIXTURE_KEY": "synthetic-key"}), contextlib.redirect_stdout(stdout):
            result = main(["--vault", str(self.library.vault), "adopt", note["id"], "--url", self.client.target,
                           "--api-key-env", "ADOPT_FIXTURE_KEY", "--apply"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue())["results"][0]["status"], "adopted")
        self.assertNotIn("synthetic-key", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
