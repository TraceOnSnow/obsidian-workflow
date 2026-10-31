from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library, LibraryError
from markdown_library.bootstrap import bootstrap
from markdown_library.transport import _Store, map_imported, sync_once


class MappingPayload:
    target = "https://mapping.example"

    def __init__(self):
        self.latest = {"works": {}, "posts": {}}
        self.base = deepcopy(self.latest)
        self.mutations, self.gets, self.clock = [], [], 0

    def seed(self, collection, remote_id, **fields):
        row = {"id": remote_id, "updatedAt": "v0", "_status": "draft", **fields}
        self.latest[collection][str(remote_id)] = deepcopy(row)
        self.base[collection][str(remote_id)] = deepcopy(row)

    def list_collection(self, collection, *, draft=True):
        return deepcopy(list((self.latest if draft else self.base)[collection].values()))

    def get(self, collection, remote_id, *, draft=True):
        self.gets.append((collection, str(remote_id), draft))
        return deepcopy((self.latest if draft else self.base)[collection][str(remote_id)])

    def create(self, *args, **kwargs):
        raise AssertionError("Imported mapping must never create remote records")

    def update(self, collection, remote_id, fields, *, expected_version, draft):
        rows = self.latest if draft else self.base
        assert rows[collection][str(remote_id)]["updatedAt"] == expected_version
        self.clock += 1
        row = {**self.latest[collection][str(remote_id)], **deepcopy(fields), "updatedAt": f"v{self.clock}"}
        self.mutations.append((collection, str(remote_id), draft, deepcopy(fields)))
        self.latest[collection][str(remote_id)] = deepcopy(row)
        if not draft:
            self.base[collection][str(remote_id)] = deepcopy(row)
        return row


class ImportedMappingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Library.initialize(Path(temporary.name) / "vault")
        self.client = MappingPayload()

    def imported_work(self, **fields):
        self.client.seed("works", 501, title="Private Work", slug="private-work", type="book",
                         visibility="private", note="Short impression", **fields)
        result = bootstrap(self.library, client=self.client, apply=True)
        return next(row for row in result["results"] if row["collection"] == "works")

    def state(self):
        return _Store(self.library, self.client.target).load()

    def files(self):
        return {str(path.relative_to(self.library.vault)): path.read_bytes()
                for path in self.library.vault.rglob("*") if path.is_file()}

    def test_private_import_map_then_explicit_publish_updates_existing_id(self):
        note = self.imported_work()
        before = self.files()
        preview = map_imported(self.library, self.client, [note["id"]])
        self.assertEqual(preview["results"][0]["status"], "mappable-imported-draft")
        self.assertEqual(self.files(), before)
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.publications(), [])
        mapped = map_imported(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(mapped["results"][0]["status"], "mapped-imported-draft")
        baseline = self.state()["baselines"][note["id"]]
        self.assertEqual(baseline["state"], "imported-draft")
        self.assertEqual(baseline["remote_id"], 501)
        self.assertEqual(self.library.publications(), [])
        self.assertEqual(self.client.latest["works"]["501"]["visibility"], "private")
        unselected = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual(unselected["plan"]["missing_local_ids"], [])
        self.assertEqual(self.client.mutations, [])
        self.library.publish(note["id"], expected_hash=note["sha256"])
        plan = sync_once(self.library, self.client, works_drafts_and_versions=True)
        self.assertEqual(plan["plan"]["actions"][0]["action"], "update")
        self.assertEqual(plan["plan"]["actions"][0]["remote_id"], 501)
        done = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual(done["results"][0]["status"], "observed")
        self.assertEqual([row[1] for row in self.client.mutations], ["501", "501"])
        self.assertEqual(self.state()["baselines"][note["id"]]["state"], "published")

    def test_local_and_remote_changes_or_split_base_reject(self):
        for mode in ("local", "remote", "base", "version", "status"):
            with self.subTest(mode=mode):
                self.setUp()
                note = self.imported_work()
                if mode == "local":
                    path = self.library.vault / note["path"]
                    path.write_bytes(path.read_bytes() + b"Native edit")
                elif mode == "remote":
                    for rows in (self.client.latest, self.client.base):
                        rows["works"]["501"]["note"] = "Admin edit"
                elif mode == "base":
                    self.client.base["works"]["501"]["description"] = "Different base"
                elif mode == "version":
                    self.client.latest["works"]["501"]["updatedAt"] = "v99"
                else:
                    for rows in (self.client.latest, self.client.base):
                        rows["works"]["501"]["_status"] = "published"
                result = map_imported(self.library, self.client, [note["id"]], apply=True)
                self.assertEqual(result["results"][0]["status"], "rejected")
                self.assertEqual(self.state()["baselines"], {})
                self.assertEqual(self.client.mutations, [])

    def test_source_receipt_mismatch_and_original_hash_tamper_reject(self):
        for mode in ("source-id", "target", "source-hash", "receipt-hash", "receipt-id"):
            with self.subTest(mode=mode):
                self.setUp()
                note = self.imported_work()
                private = self.library.vault / ".markdown-library"
                state_path = private / "state.json"
                state = json.loads(state_path.read_text())
                entry = next(iter(state["sources"].values()))
                if mode == "source-id": entry["source"]["remote_id"] = 999
                if mode == "target": entry["source"]["target"] = "https://another.example"
                if mode == "source-hash": entry["source"]["source_hash"] = "forged"
                state_path.write_text(json.dumps(state))
                receipt_path = private / "bootstrap-state.json"
                receipts = json.loads(receipt_path.read_text())
                receipt = next(iter(receipts["imports"].values()))
                if mode == "receipt-hash": receipt["imported_sha256"] = "forged"
                if mode == "receipt-id": receipt["id"] = "another-id"
                receipt_path.write_text(json.dumps(receipts))
                try:
                    result = map_imported(self.library, self.client, [note["id"]], apply=True)
                    self.assertEqual(result["results"][0]["status"], "rejected")
                except Exception as exc:
                    from markdown_library import LibraryError
                    self.assertIsInstance(exc, LibraryError)
                self.assertEqual(self.state()["baselines"], {})

    def test_pending_baseline_and_duplicate_claim_never_overwritten(self):
        for mode in ("pending", "baseline", "other-pending", "other-baseline", "duplicate-remote"):
            with self.subTest(mode=mode):
                self.setUp()
                note = self.imported_work()
                store = _Store(self.library, self.client.target)
                state = store.load()
                claim = {"collection": "works", "remote_id": 501, "identity": {"slug": "private-work"}}
                if mode == "pending": state["pending"][note["id"]] = claim
                if mode == "baseline": state["baselines"][note["id"]] = claim
                if mode == "other-pending": state["pending"]["other"] = claim
                if mode == "other-baseline": state["baselines"]["other"] = claim
                store.save(state)
                if mode == "duplicate-remote":
                    self.client.seed("works", 502, title="Duplicate", slug="private-work", type="book", visibility="private")
                before = self.files()
                if mode == "other-baseline":
                    with self.assertRaises(LibraryError):
                        map_imported(self.library, self.client, [note["id"]], apply=True)
                else:
                    result = map_imported(self.library, self.client, [note["id"]], apply=True)
                    self.assertEqual(result["results"][0]["status"], "rejected")
                for name, data in before.items():
                    self.assertEqual((self.library.vault / name).read_bytes(), data)

    def test_private_post_relation_can_map_but_actual_work_publish_stays_blocked(self):
        self.client.seed("posts", 77, title="Draft Post", link="draft", locale="zh", markdown="Private body")
        work = self.imported_work(article=77)
        posts = [note for note in self.library.list_notes() if note["properties"]["kind"] == "post"]
        result = map_imported(self.library, self.client, [work["id"], posts[0]["id"]], apply=True)
        self.assertTrue(all(row["status"] == "mapped-imported-draft" for row in result["results"]))
        self.assertEqual(self.library.get(work["id"])["properties"]["article"], posts[0]["id"])
        self.library.publish(work["id"], expected_hash=work["sha256"])
        result = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual(result["results"][0]["status"], "blocked")
        self.assertEqual(self.client.mutations, [])

    def test_protected_posts_keep_receipts_without_active_mapping(self):
        for protection in ({"protectedContent": True}, {"frontmatter": {"password": "PRIVATE"}},
                           {"markdown": ":::encrypted\nPRIVATE\n:::"}, {"frontmatter": {"unknown": True}}):
            with self.subTest(protection=protection):
                self.setUp()
                fields = {"title": "Protected", "link": "protected", "locale": "zh", "markdown": "Private", **protection}
                self.client.seed("posts", 77, **fields)
                note = bootstrap(self.library, client=self.client, apply=True)["results"][0]
                before = self.files()
                result = map_imported(self.library, self.client, [note["id"]], apply=True)
                self.assertEqual(result["results"][0]["status"], "rejected")
                self.assertNotIn("PRIVATE", json.dumps(result))
                # Lock files may already exist from bootstrap; no receipt/content changes.
                self.assertEqual(self.state()["baselines"], {})
                for name, data in before.items(): self.assertEqual((self.library.vault / name).read_bytes(), data)

    def test_mapping_requires_actual_bootstrap_source_and_rejects_public_record(self):
        note = self.library.create("Private", title="Not imported", kind="work", properties={"slug": "private", "type": "book"})
        result = map_imported(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        imported = self.imported_work(_status="published")
        # A genuinely private published Work can map without changing visibility.
        self.assertEqual(map_imported(self.library, self.client, [imported["id"]])["results"][0]["status"], "mappable-imported-draft")
        self.setUp()
        self.client.seed("posts", 77, title="Public", link="public", locale="zh", markdown="Public", _status="published")
        note = bootstrap(self.library, client=self.client, apply=True)["results"][0]
        self.assertEqual(map_imported(self.library, self.client, [note["id"]])["results"][0]["status"], "rejected")

    def test_plain_draft_post_maps_and_reuses_remote_id_on_selected_publish(self):
        self.client.seed("posts", 77, title="Draft", link="draft", locale="zh", markdown="Private body")
        note = bootstrap(self.library, client=self.client, apply=True)["results"][0]
        mapped = map_imported(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(mapped["results"][0]["status"], "mapped-imported-draft")
        self.assertEqual(self.client.mutations, [])
        self.library.publish(note["id"], expected_hash=note["sha256"])
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        self.assertEqual([row[1] for row in self.client.mutations], ["77", "77"])

    def test_native_edit_during_remote_observation_rejects_mapping(self):
        note = self.imported_work()
        get = self.client.get
        def editing_get(collection, remote_id, *, draft=True):
            row = get(collection, remote_id, draft=draft)
            if not draft:
                path = self.library.vault / note["path"]
                path.write_bytes(path.read_bytes() + b"New native edit")
            return row
        self.client.get = editing_get
        result = map_imported(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertIn("changed while observing", result["results"][0]["reason"])
        self.assertEqual(self.state()["baselines"], {})

    def test_unresolved_remote_only_post_relationship_keeps_bootstrap_receipt(self):
        note = self.imported_work(article=999)
        result = map_imported(self.library, self.client, [note["id"]], apply=True)
        self.assertEqual(result["results"][0]["status"], "rejected")
        self.assertIn("proven bootstrap Post", result["results"][0]["reason"])
        self.assertEqual(self.state()["baselines"], {})
        self.assertEqual(self.library.get(note["id"])["properties"]["article"], 999)
