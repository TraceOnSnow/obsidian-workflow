from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from markdown_library import Library, LibraryError
from markdown_library.sync import export_publication, observed_baseline
from markdown_library.transport import PayloadClient, TransportError, UncertainWrite, _Store, sync_once


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.value).encode()


class Opener:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result)


class FakePayload:
    target = "https://isolated.example"

    def __init__(self):
        self.latest = {"works": {}, "posts": {}}
        self.base = {"works": {}, "posts": {}}
        self.mutations = []
        self.clock = 0
        self.create_timeout = False
        self.publish_mismatch = False
        self.mutate_draft_before_publish = False
        self.normalize_dates = False

    def stamp(self):
        self.clock += 1
        return f"2026-01-01T00:00:{self.clock:02d}.000Z"

    def list_collection(self, collection, *, draft=True, trash=False):
        return [row for row in deepcopy(list((self.latest if draft else self.base)[collection].values())) if trash or not row.get("deletedAt")]

    def get(self, collection, remote_id, *, draft=True, trash=False):
        return deepcopy((self.latest if draft else self.base)[collection][str(remote_id)])

    def create(self, collection, fields):
        self.mutations.append(("create", collection, deepcopy(fields)))
        remote_id = str(len(self.latest[collection]) + 1)
        row = {**deepcopy(fields), "id": remote_id, "updatedAt": self.stamp(), "deletedAt": None}
        if self.normalize_dates and row.get("publishedAt"):
            row["publishedAt"] = "2020-01-02T00:00:00.000Z"
        self.latest[collection][remote_id] = deepcopy(row)
        self.base[collection][remote_id] = deepcopy(row)
        if self.create_timeout:
            self.create_timeout = False
            raise UncertainWrite("simulated timeout")
        return {"doc": row}

    def update(self, collection, remote_id, fields, *, expected_version, draft, trash=False):
        self.mutations.append(("update", collection, deepcopy(fields), expected_version, draft))
        rows = self.latest if draft else self.base
        if rows[collection][str(remote_id)]["updatedAt"] != expected_version:
            raise UncertainWrite("conditional timestamp mismatch")
        row = {**deepcopy(self.latest[collection][str(remote_id)]), **deepcopy(fields), "updatedAt": self.stamp()}
        if not draft and self.publish_mismatch:
            row["title"] = "unexpected server title"
        self.latest[collection][str(remote_id)] = deepcopy(row)
        if not draft:
            self.base[collection][str(remote_id)] = deepcopy(row)
        return row

    def seed(self, collection, fields):
        row = {**deepcopy(fields), "id": "70", "updatedAt": self.stamp(), "_status": "published", "deletedAt": None}
        self.latest[collection]["70"] = deepcopy(row)
        self.base[collection]["70"] = deepcopy(row)
        return row


class PayloadClientTests(unittest.TestCase):
    def test_new_admin_records_require_mapping_without_automatic_import(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library.initialize(str(Path(directory).resolve()))
            client = FakePayload()
            client.seed("works", {"title": "Admin record", "slug": "admin-record", "type": "game", "visibility": "private"})
            result = sync_once(library, client, apply=True, accept_remote=True)
            self.assertEqual(result["plan"]["unmapped_remote_records"], [{"collection": "works", "remote_id": "70"}])
            self.assertEqual(library.list_notes(), [])
            self.assertEqual(client.mutations, [])

    def test_https_and_explicit_loopback_only(self):
        for url in ("http://example.com", "http://127.0.0.1", "https://user:secret@example.com", "https://example.com/api", "https://example.com?token=secret"):
            with self.assertRaises(TransportError):
                PayloadClient(url, "key")
        self.assertEqual(PayloadClient("http://127.0.0.1:3000", "key", allow_loopback_http=True).target, "http://127.0.0.1:3000")

    def test_all_pages_draft_depth_and_authorization(self):
        opener = Opener([
            {"docs": [{"id": 1}], "hasNextPage": True, "nextPage": 2, "totalDocs": 2},
            {"docs": [{"id": 2}], "hasNextPage": False, "totalDocs": 2},
        ])
        client = PayloadClient("https://isolated.example", "secret", opener=opener)
        self.assertEqual([row["id"] for row in client.list_collection("posts")], [1, 2])
        for request in opener.requests:
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query["draft"], ["true"])
            self.assertEqual(query["depth"], ["0"])
            self.assertEqual(request.get_header("Authorization"), "content-clients API-Key secret")
            self.assertEqual(request.get_header("User-agent"), "markdown-library/0.1")

    def test_pagination_incomplete_and_duplicate_ids_fail(self):
        for response in ({"docs": [], "hasNextPage": False, "totalDocs": 1},
                         {"docs": [{"id": 1}, {"id": 1}], "hasNextPage": False, "totalDocs": 2}):
            client = PayloadClient("https://isolated.example", "key", opener=Opener([response]))
            with self.assertRaises(TransportError):
                client.list_collection("works")

    def test_credentials_and_server_error_bodies_are_redacted(self):
        secret = "do-not-echo-key"
        for error in (TimeoutError(secret), HTTPError("https://isolated.example?key=" + secret, 403, secret, {}, BytesIO(secret.encode()))):
            client = PayloadClient("https://isolated.example", secret, opener=Opener([error]))
            with self.assertRaises(TransportError) as caught:
                client.list_collection("posts")
            self.assertNotIn(secret, str(caught.exception))
        client = PayloadClient("https://isolated.example", secret, opener=Opener([TimeoutError(secret)]))
        with self.assertRaises(UncertainWrite) as caught:
            client.create("posts", {"title": "Post"})
        self.assertNotIn(secret, str(caught.exception))

    def test_standard_bulk_patch_uses_requested_timestamp_and_refuses_zero_rows(self):
        opener = Opener([{"docs": [{"id": "1"}], "errors": []}, {"docs": [], "errors": []}])
        client = PayloadClient("https://isolated.example", "key", opener=opener)
        client.update("posts", "1", {"title": "A"}, expected_version="draft-version", draft=True)
        query = parse_qs(urlsplit(opener.requests[0].full_url).query)
        self.assertEqual(query["where[and][1][updatedAt][equals]"], ["draft-version"])
        self.assertEqual(query["draft"], ["true"])
        with self.assertRaises(UncertainWrite):
            client.update("posts", "1", {}, expected_version="base-version", draft=False)


class SyncExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Library.initialize(Path(self.temporary.name) / "vault")
        self.client = FakePayload()

    def post(self):
        note = self.library.create("Published source", title="Post", kind="post", properties={"link": "notes/stable", "locale": "en"})
        publication = self.library.publish(note["id"], expected_hash=note["sha256"])
        return note, publication

    def baseline(self, publication):
        export = export_publication(publication)
        row = self.client.seed(export["collection"], export["fields"])
        store = _Store(self.library, self.client.target)
        state = store.load()
        state["baselines"][publication["id"]] = observed_baseline(publication, row)
        store.save(state)
        return row

    def state(self):
        return _Store(self.library, self.client.target).load()

    def test_default_reads_and_plans_without_remote_or_baseline_writes(self):
        note, publication = self.post()
        result = sync_once(self.library, self.client)
        self.assertEqual(result["results"][0]["status"], "create")
        self.assertEqual(self.client.mutations, [])
        self.assertFalse((self.library.vault / ".markdown-library/sync-state.json").exists())

    def test_scoped_publish_excludes_other_publications_and_deletions(self):
        note, publication = self.post()
        other = self.library.create("Outside", title="Outside", kind="post", properties={"link": "outside"})
        self.library.publish(other["id"], expected_hash=other["sha256"])
        sync_once(self.library, self.client, apply=True)
        self.library.delete(other["id"], expected_hash=other["sha256"])
        updated = self.library.replace(note["id"], note["markdown"] + "\nNew version", expected_hash=note["sha256"])
        self.library.publish(note["id"], expected_hash=updated["sha256"])
        before_remote = deepcopy(self.client.latest)
        before_baseline = deepcopy(self.state()["baselines"][other["id"]])
        self.client.mutations.clear()
        result = sync_once(self.library, self.client, apply=True, only_ids=[note["id"]])
        self.assertEqual([row["id"] for row in result["results"]], [note["id"]])
        self.assertEqual(result["results"][0]["status"], "observed")
        remote_id = before_baseline["remote_id"]
        self.assertEqual(self.client.latest["posts"][str(remote_id)], before_remote["posts"][str(remote_id)])
        self.assertEqual(self.state()["baselines"][other["id"]], before_baseline)
        self.assertEqual(len(self.client.mutations), 2)

    def test_invalid_sync_scope_fails_before_remote_access(self):
        for scope in ([], "all", ["unknown"], [None]):
            with self.subTest(scope=scope), self.assertRaises(TransportError):
                sync_once(self.library, self.client, apply=True, only_ids=scope)
        self.assertEqual(self.client.mutations, [])

    def test_create_draft_then_publish_and_independent_baseline(self):
        note, publication = self.post()
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        self.assertEqual(len(self.client.mutations), 2)
        self.assertEqual(self.client.mutations[0][2]["_status"], "draft")
        self.assertEqual(self.client.mutations[1][2]["_status"], "published")
        self.assertFalse(self.client.mutations[1][-1])
        self.assertIn(note["id"], self.state()["baselines"])
        self.assertEqual(self.state()["pending"], {})

    def test_create_timeout_reconciles_identity_without_duplicate_post(self):
        note, publication = self.post()
        self.client.create_timeout = True
        first = sync_once(self.library, self.client, apply=True)
        self.assertEqual(first["pending_ids"], [note["id"]])
        second = sync_once(self.library, self.client, apply=True)
        self.assertEqual(second["results"][0]["candidate_ids"], ["1"])
        self.assertEqual(len(self.client.mutations), 1)
        self.assertEqual(self.state()["baselines"], {})
        third = sync_once(self.library, self.client, apply=True, resume_pending=[note["id"]])
        self.assertEqual(third["results"][0]["status"], "reconciled")
        self.assertEqual(len(self.client.mutations), 2)
        self.assertEqual(len(self.client.latest["posts"]), 1)

    def test_readback_mismatch_never_advances_baseline(self):
        note, publication = self.post()
        self.client.publish_mismatch = True
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "reconciliation-required")
        self.assertEqual(self.state()["baselines"], {})
        self.assertIn(note["id"], self.state()["pending"])

    def test_update_uses_latest_version_for_draft_base_version_for_publish(self):
        note, publication = self.post()
        self.baseline(publication)
        changed = self.library.replace(note["id"], note["markdown"].replace("Published source", "Selected next version"), expected_hash=note["sha256"])
        self.library.publish(note["id"], expected_hash=changed["sha256"])
        base_version = self.client.base["posts"]["70"]["updatedAt"]
        self.client.latest["posts"]["70"]["updatedAt"] = self.client.stamp()
        draft_version = self.client.latest["posts"]["70"]["updatedAt"]
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        self.assertEqual(self.client.mutations[0][3], draft_version)
        self.assertTrue(self.client.mutations[0][4])
        self.assertEqual(self.client.mutations[1][3], base_version)
        self.assertFalse(self.client.mutations[1][4])

    def test_remote_only_vs_current_local_edit_conflicts(self):
        note, publication = self.post()
        self.baseline(publication)
        self.client.latest["posts"]["70"].update({"markdown": "Remote new source", "updatedAt": self.client.stamp()})
        local = self.library.replace(note["id"], note["markdown"] + " private new draft", expected_hash=note["sha256"])
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(self.library.get(note["id"])["sha256"], local["sha256"])
        self.assertEqual(self.client.mutations, [])

    def test_remote_work_import_preserves_private_body_and_remains_unpublished(self):
        note = self.library.create("PRIVATE BODY", title="Work", kind="work", properties={"slug": "work", "type": "book", "note": "before"})
        publication = self.library.publish(note["id"], expected_hash=note["sha256"])
        self.baseline(publication)
        self.client.latest["works"]["70"].update({"note": "Remote reaction", "updatedAt": self.client.stamp()})
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result["results"][0]["status"], "remote-draft-imported")
        current = self.library.get(note["id"])
        self.assertIn("PRIVATE BODY", current["markdown"])
        self.assertEqual(current["properties"]["note"], "Remote reaction")
        self.assertEqual(self.library.publication(note["id"]), publication["snapshot"])
        self.assertEqual(self.client.mutations, [])

    def test_works_capability_blocks_and_creates_private_draft_when_explicit(self):
        note = self.library.create("PRIVATE BODY", title="Work", kind="work", properties={"slug": "work", "type": "book"})
        self.library.publish(note["id"], expected_hash=note["sha256"])
        blocked = sync_once(self.library, self.client, apply=True)
        self.assertTrue(blocked["results"][0]["blocked_by"])
        self.assertEqual(self.client.mutations, [])
        result = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        self.assertEqual(self.client.mutations[0][2]["visibility"], "private")
        self.assertEqual(self.client.mutations[1][2]["visibility"], "public")
        self.assertNotIn("PRIVATE BODY", json.dumps(self.client.mutations))

    def test_withdrawal_preserves_latest_fields_and_does_not_delete(self):
        note, publication = self.post()
        self.baseline(publication)
        self.library.unpublish(note["id"])
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        mutation = self.client.mutations[0]
        self.assertEqual(mutation[2]["markdown"], "Published source")
        self.assertEqual(mutation[2]["_status"], "draft")
        self.assertFalse(mutation[4])
        self.assertIn("70", self.client.base["posts"])

    def test_target_change_fails_without_reading_other_target(self):
        self.post()
        sync_once(self.library, self.client, apply=True)
        self.client.target = "https://different.example"
        with self.assertRaisesRegex(TransportError, "changed target"):
            sync_once(self.library, self.client)

    def test_accept_remote_then_explicit_publish_converges_without_reset(self):
        note, publication = self.post()
        self.baseline(publication)
        remote = self.client.latest["posts"]["70"]
        remote.update({"markdown": "Admin source", "updatedAt": self.client.stamp()})
        self.client.base["posts"]["70"] = deepcopy(remote)
        imported = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(imported["results"][0]["status"], "remote-draft-imported")
        current = self.library.get(note["id"])
        self.library.publish(note["id"], expected_hash=current["sha256"])
        reconciled = sync_once(self.library, self.client, apply=True)
        self.assertEqual(reconciled["results"][0]["status"], "reconciled")
        final = sync_once(self.library, self.client)
        self.assertEqual(final["results"][0]["status"], "unchanged")
        self.assertEqual(self.client.mutations, [])

    def test_empty_update_reconciles_matching_server_defaults(self):
        note, publication = self.post()
        remote = self.baseline(publication)
        self.client.latest["posts"]["70"]["description"] = "Server description"
        self.client.base["posts"]["70"]["description"] = "Server description"
        # The server-filled default was part of the last successful observation.
        store = _Store(self.library, self.client.target)
        state = store.load()
        state["baselines"][note["id"]] = observed_baseline(publication, self.client.latest["posts"]["70"])
        store.save(state)
        replaced = self.library.replace(note["id"], note["markdown"].replace("schema_version: 1", "schema_version: 1\ndescription: Server description"), expected_hash=note["sha256"])
        self.library.publish(note["id"], expected_hash=replaced["sha256"])
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "reconciled")
        self.assertEqual(self.client.mutations, [])

    def test_same_batch_posts_before_works_resolves_library_relationship(self):
        post, publication = self.post()
        work = self.library.create("PRIVATE BODY", title="Work", kind="work", properties={"slug": "work", "type": "book", "article": post["id"]})
        self.library.publish(work["id"], expected_hash=work["sha256"])
        result = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual([row["status"] for row in result["results"]], ["observed", "observed"])
        self.assertEqual(self.client.mutations[0][1], "posts")
        self.assertEqual(self.client.latest["works"]["1"]["article"], "1")
        self.assertNotIn(post["id"], json.dumps(self.client.mutations))
        self.assertEqual(self.library.get(work["id"])["properties"]["article"], post["id"])
        next_run = sync_once(self.library, self.client)
        self.assertTrue(all(row["status"] == "unchanged" for row in next_run["results"]))

    def test_missing_link_dependency_blocks_work_without_any_http_uuid(self):
        work = self.library.create("private", title="Work", kind="work", properties={"slug": "work", "type": "book", "article": "00000000-0000-0000-0000-000000000000"})
        with self.assertRaisesRegex(LibraryError, "Linked Post is missing"):
            self.library.publish(work["id"], expected_hash=work["sha256"])
        self.assertEqual(self.library.publications(), [])
        self.assertEqual(self.client.mutations, [])

    def test_withdraw_post_and_delete_linked_work_in_same_batch(self):
        post, _ = self.post()
        work = self.library.create("private", title="Work", kind="work", properties={
            "slug": "linked-withdrawal", "type": "book", "article": post["id"],
        })
        self.library.publish(work["id"], expected_hash=work["sha256"])
        initial = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertTrue(all(row["status"] == "observed" for row in initial["results"]))
        mapped = self.state()["baselines"]
        remote_post = mapped[post["id"]]["remote_id"]
        remote_work = mapped[work["id"]]["remote_id"]
        self.library.unpublish(post["id"])
        self.library.delete(work["id"], expected_hash=work["sha256"])
        result = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual({row["status"] for row in result["results"]}, {"observed", "deleted-observed"})
        self.assertEqual(self.client.base["posts"][str(remote_post)]["_status"], "draft")
        hidden_work = self.client.base["works"][str(remote_work)]
        self.assertTrue(hidden_work["deletedAt"])
        self.assertEqual(hidden_work["_status"], "published")
        self.assertEqual(hidden_work["visibility"], "public")
        self.assertEqual(hidden_work["article"], remote_post)
        self.assertTrue(all(row[0] != "delete" for row in self.client.mutations))
        self.library.recover(work["id"], expected_hash=work["sha256"])
        restored = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual(next(row for row in restored["results"] if row["id"] == work["id"])["status"], "restored-draft-observed")
        current = self.library.get(work["id"])
        self.library.publish(work["id"], expected_hash=current["sha256"])
        self.client.mutations.clear()
        blocked = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual(next(row for row in blocked["results"] if row["id"] == work["id"])["status"], "blocked")
        self.assertEqual(self.client.mutations, [])

    def test_payload_datetime_normalization_is_semantic_not_conflict(self):
        note = self.library.create("Body", title="Post", kind="post", properties={
            "link": "date-normalized", "locale": "en", "publishedAt": "2020-01-02T08:00:00+08:00",
        })
        self.library.publish(note["id"], expected_hash=note["sha256"])
        self.client.normalize_dates = True
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        self.assertEqual(self.state()["pending"], {})

    def test_accept_latest_remote_draft_then_local_publish_updates_older_base(self):
        note, publication = self.post()
        self.baseline(publication)
        previous = deepcopy(self.state()["baselines"])
        latest = self.client.latest["posts"]["70"]
        latest.update({"markdown": "Admin unpublished draft", "_status": "draft", "updatedAt": self.client.stamp()})
        imported = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(imported["results"][0]["status"], "remote-draft-imported")
        self.assertEqual(self.state()["baselines"], previous)
        self.assertEqual(self.client.base["posts"]["70"]["markdown"], "Published source")
        current = self.library.get(note["id"])
        self.library.publish(note["id"], expected_hash=current["sha256"])
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "observed")
        self.assertEqual(self.client.base["posts"]["70"]["markdown"], "Admin unpublished draft")
        self.assertEqual(self.client.base["posts"]["70"]["_status"], "published")

    def test_confirmed_tombstone_uses_native_trash_retaining_remote_and_local_history(self):
        note, publication = self.post()
        self.baseline(publication)
        self.library.delete(note["id"], expected_hash=note["sha256"])
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result["results"][0]["status"], "deleted-observed")
        self.assertTrue(self.client.base["posts"]["70"]["deletedAt"])
        self.assertEqual(self.client.base["posts"]["70"]["_status"], "published")
        self.assertEqual(self.client.base["posts"]["70"]["markdown"], "Published source")
        self.assertTrue((self.library.vault / f".markdown-library/trash/{note['id']}.md").exists())
        self.assertEqual(self.library.publications()[0]["state"], "deleted")
        self.assertTrue(all(row[0] != "delete" for row in self.client.mutations))


if __name__ == "__main__":
    unittest.main()
