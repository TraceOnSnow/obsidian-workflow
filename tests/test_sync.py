"""Offline fixtures for selected-version publication and reconciliation."""

from copy import deepcopy
import json
import tempfile
import unittest

from markdown_library.core import Library
from markdown_library.sync import export_publication, observed_baseline, plan_sync


def post(body="Selected public body", **properties):
    return {"id": "post-stable-id", "kind": "post", "state": "published",
            "sha256": "hash-of-private-working-file", "published_at": "2026-10-21T00:00:00Z",
            "snapshot": {"id": "post-stable-id", "kind": "post", "title": "Selected title",
                         "markdown": body, "properties": {"link": "note/stable", "locale": "zh", **properties}}}


def work(**properties):
    return {"id": "work-stable-id", "kind": "work", "state": "published",
            "sha256": "private-file-revision", "published_at": "2026-10-21T00:00:00Z",
            "snapshot": {"id": "work-stable-id", "kind": "work", "title": "Selected work",
                         "properties": {"slug": "stable-work", "type": "book", **properties}}}


def remote_for(publication, remote_id=12, **changes):
    return {"id": remote_id, "updatedAt": "2026-10-21T01:00:00Z",
            **export_publication(publication)["fields"], **changes}


def fixtures(publication):
    remote = remote_for(publication)
    baseline = {publication["id"]: observed_baseline(publication, remote)}
    records = {export_publication(publication)["collection"]: {"docs": [remote], "hasNextPage": False}}
    return baseline, records


def action(publication, baseline=None, records=None, **kwargs):
    return plan_sync([publication], baseline or {}, records or {}, **kwargs)["actions"][0]


class SyncPlannerTests(unittest.TestCase):
    def test_real_library_publication_boundary_with_synthetic_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library.initialize(directory)
            note = library.create("Published paragraph", title="Fixture", kind="post",
                                  properties={"link": "note/fixture", "frontmatter": {"math": True}})
            library.publish(note["id"], expected_hash=note["sha256"])
            selected = library.publications()[0]
            remote = remote_for(selected)
            baseline = {note["id"]: observed_baseline(selected, remote)}
            library.replace(note["id"], note["markdown"].replace("Published paragraph", "PRIVATE later draft"),
                            expected_hash=note["sha256"])
            planned = plan_sync(library.publications(), baseline, {"posts": [remote]})
            self.assertEqual(planned["actions"][0]["action"], "unchanged")
            self.assertNotIn("PRIVATE", json.dumps(planned))
            library.unpublish(note["id"])
            self.assertEqual(plan_sync(library.publications(), baseline, {"posts": [remote]})
                             ["actions"][0]["action"], "unpublish")

    def test_only_selected_publication_is_outgoing_and_plan_never_applies(self):
        publication = post()
        publication["draft"] = {"markdown": "PRIVATE UNPUBLISHED EDIT"}
        publication["snapshot"]["properties"]["privatePrompt"] = "SECRET"
        planned = plan_sync([publication], {}, {})
        self.assertFalse(planned["applied"])
        self.assertEqual(planned["mode"], "offline-plan-only")
        self.assertEqual(planned["actions"][0]["action"], "create")
        self.assertEqual(planned["actions"][0]["payload"]["_status"], "published")
        self.assertNotIn("PRIVATE", json.dumps(planned))
        self.assertNotIn("SECRET", json.dumps(planned))
        self.assertEqual(plan_sync([], {}, {})["actions"], [])

    def test_draft_after_publish_does_not_change_selected_version(self):
        publication = post()
        baseline, records = fixtures(publication)
        publication["sha256"] = "newer-private-draft-hash"
        publication["draft"] = {"markdown": "new unpublished version"}
        self.assertEqual(action(publication, baseline, records)["action"], "unchanged")

    def test_work_body_and_unknown_properties_never_leave_boundary(self):
        publication = work(note="Explicit short public reaction", description="Public facts")
        publication["snapshot"]["markdown"] = "PRIVATE WORK PROSE"
        publication["snapshot"]["properties"].update({"markdown": "PRIVATE BODY", "frontmatter": {"secret": "PRIVATE"}})
        planned = action(publication)
        self.assertEqual(planned["payload"]["visibility"], "public")
        self.assertEqual(planned["payload"]["note"], "Explicit short public reaction")
        self.assertNotIn("PRIVATE", json.dumps(planned))
        self.assertTrue(planned["blocked_by"])
        self.assertFalse(planned["ready_for_executor"])
        self.assertTrue(action(publication, capabilities={"works_drafts_and_versions": True})["ready_for_executor"])

    def test_selected_safe_theme_options_are_preserved_and_protection_requires_review(self):
        publication = post(frontmatter={"subtitle": "Public subtitle", "math": True})
        self.assertEqual(action(publication)["payload"]["frontmatter"],
                         {"subtitle": "Public subtitle", "math": True})
        for properties in ({"frontmatter": {"password": "private-password"}},
                           {"password": "private-password"}, {"protectedContent": True},
                           {"frontmatter": {"unreviewedOption": "value"}}):
            with self.subTest(properties=properties), self.assertRaises(ValueError):
                export_publication(post(**properties))
        with self.assertRaises(ValueError):
            export_publication(post("  ::: encrypted\nprivate ciphertext\n:::"))

    def test_null_work_fields_and_partial_dates_survive(self):
        publication = work(experiences=None, sources=None, rating=None, article=None)
        fields = action(publication)["payload"]
        self.assertIsNone(fields["experiences"])
        self.assertIsNone(fields["sources"])
        self.assertIsNone(fields["rating"])
        publication["snapshot"]["properties"]["experiences"] = [
            {"startedOn": None, "endedOn": "2025-03"}, {"endedOn": "2024"}]
        self.assertEqual(action(publication)["payload"]["experiences"],
                         [{"startedOn": None, "endedOn": "2025-03"}, {"endedOn": "2024"}])

    def test_payload_date_instants_normalize_but_experience_text_precision_is_unchanged(self):
        publication = post(publishedAt="2020-01-02T00:00:00Z")
        remote = remote_for(publication, publishedAt="2020-01-02T08:00:00+08:00")
        baseline = {publication["id"]: observed_baseline(publication, remote)}
        self.assertEqual(action(publication, baseline, {"posts": [remote]})["action"], "unchanged")
        self.assertEqual(export_publication(publication)["fields"]["publishedAt"], "2020-01-02T00:00:00.000Z")
        publication = work(releaseDate="2020-01-02", experiences=[{"endedOn": "2020-01"}],
                           sources=[{"provider": "web", "externalId": "source", "fetchedAt": "2020-01-02T08:00:00+08:00"}])
        remote = remote_for(publication, releaseDate="2020-01-02T00:00:00.000Z", sources=[
            {"id": "generated-row", "provider": "web", "externalId": "source", "fetchedAt": "2020-01-02T00:00:00Z"}])
        baseline = {publication["id"]: observed_baseline(publication, remote)}
        self.assertEqual(action(publication, baseline, {"works": [remote]})["action"], "unchanged")
        self.assertEqual(export_publication(publication)["fields"]["experiences"], [{"endedOn": "2020-01"}])

    def test_new_selected_publication_updates_changed_fields_with_expected_version(self):
        publication = post(tags=["one"])
        baseline, records = fixtures(publication)
        publication["snapshot"]["markdown"] = "Explicitly selected revised body"
        planned = action(publication, baseline, records)
        self.assertEqual(planned["action"], "update")
        self.assertEqual(planned["payload"], {"markdown": "Explicitly selected revised body"})
        self.assertEqual(planned["remote_id"], 12)
        self.assertEqual(planned["expected_remote_version"], "2026-10-21T01:00:00Z")

    def test_remote_only_edit_becomes_library_draft_never_publication(self):
        publication = post()
        baseline, records = fixtures(publication)
        records["posts"]["docs"][0].update(markdown="Admin edit", updatedAt="2026-10-21T02:00:00Z")
        planned = action(publication, baseline, records)
        self.assertEqual(planned["action"], "reverse-change")
        self.assertEqual(planned["payload"], {})
        self.assertEqual(planned["library_draft"]["state"], "draft")
        self.assertEqual(planned["library_draft"]["markdown"], "Admin edit")
        self.assertFalse(planned["publishes"])
        self.assertEqual(publication["snapshot"]["markdown"], "Selected public body")

    def test_both_sides_changed_conflicts(self):
        publication = post()
        baseline, records = fixtures(publication)
        publication["snapshot"]["markdown"] = "new selected body"
        records["posts"]["docs"][0]["markdown"] = "Admin edit"
        self.assertEqual(action(publication, baseline, records)["action"], "conflict")

    def test_admin_only_theme_or_protection_changes_are_observed(self):
        publication = post()
        baseline, records = fixtures(publication)
        records["posts"]["docs"][0].update(frontmatter={"password": "admin-protection"}, protectedContent=True)
        planned = action(publication, baseline, records)
        self.assertEqual(planned["action"], "reverse-change")
        self.assertEqual(planned["website_only_fields_requiring_review"]["protectedContent"], True)
        self.assertEqual(planned["payload"], {})
        publication["snapshot"]["markdown"] = "new selected body"
        self.assertEqual(action(publication, baseline, records)["action"], "conflict")

    def test_create_timeout_reconciles_identity_without_duplicate_post(self):
        publication = post()
        first = action(publication)
        self.assertEqual(first["action"], "create")
        records = {"posts": {"docs": [remote_for(publication)]}}
        after_timeout = action(publication, records=records)
        self.assertEqual(after_timeout["action"], "adopt-review")
        self.assertEqual(after_timeout["payload"], {})
        self.assertEqual(after_timeout["candidate_ids"], [12])
        self.assertEqual(action(publication)["operation_key"], first["operation_key"])
        # Matching content still requires reviewed adoption; it is never overwritten.
        records["posts"]["docs"][0]["markdown"] = "Different existing content"
        self.assertEqual(action(publication, records=records)["action"], "adopt-review")

    def test_explicit_withdrawal_unpublishes_and_retains_history(self):
        publication = post()
        baseline, records = fixtures(publication)
        publication["state"] = "withdrawn"
        planned = action(publication, baseline, records)
        self.assertEqual(planned["action"], "unpublish")
        self.assertEqual(planned["payload"], {"_status": "draft"})
        records["posts"]["docs"][0]["_status"] = "draft"
        observed = observed_baseline(publication, records["posts"]["docs"][0])
        self.assertEqual(action(publication, {publication["id"]: observed}, records)["action"], "unchanged")
        publication["state"] = "published"
        self.assertEqual(action(publication, {publication["id"]: observed}, records)["payload"], {"_status": "published"})

    def test_remote_change_before_withdrawal_requires_review(self):
        publication = post()
        baseline, records = fixtures(publication)
        publication["state"] = "withdrawn"
        records["posts"]["docs"][0]["markdown"] = "Admin edit"
        self.assertEqual(action(publication, baseline, records)["action"], "conflict")

    def test_confirmed_delete_unpublishes_first_and_hard_delete_alone_requires_review(self):
        publication = work()
        baseline, records = fixtures(publication)
        publication["state"] = "deleted"
        planned = action(publication, baseline, records)
        self.assertEqual(planned["action"], "unpublish")
        self.assertEqual(planned["payload"], {"visibility": "private", "_status": "draft"})
        self.assertTrue(planned["retain_history"])
        self.assertTrue(planned["permanent_delete_requires_review"])
        self.assertTrue(planned["ready_for_executor"])
        records["works"]["docs"][0]["visibility"] = "private"
        records["works"]["docs"][0]["_status"] = "draft"
        observed = action(publication, baseline, records)
        self.assertEqual(observed["payload"], {})
        self.assertEqual(observed["action"], "unchanged")
        self.assertTrue(observed["retain_history"])
        self.assertTrue(observed["permanent_delete_requires_review"])

    def test_rename_and_offline_absence_never_propagate_deletion(self):
        publication = post()
        baseline, records = fixtures(publication)
        publication["path"] = "Renamed/Title changed.md"
        self.assertEqual(action(publication, baseline, records)["action"], "unchanged")
        missing = plan_sync([], baseline, records)
        self.assertEqual(missing["actions"], [])
        self.assertEqual(missing["missing_local_ids"], [publication["id"]])
        self.assertEqual(action(publication, baseline, {})["action"], "conflict")

    def test_planning_does_not_advance_baseline_or_mutate_inputs(self):
        publication = post()
        baseline, records = fixtures(publication)
        publication["snapshot"]["markdown"] = "selected edit"
        before = deepcopy((publication, baseline, records))
        planned = action(publication, baseline, records)
        self.assertEqual((publication, baseline, records), before)
        self.assertFalse(planned["applied"])
        with self.assertRaises(ValueError):
            observed_baseline(publication, records["posts"]["docs"][0])
        with self.assertRaises(ValueError):
            observed_baseline(publication, planned["payload"])

    def test_array_ids_are_preserved_or_require_review(self):
        publication = work(experiences=[{"endedOn": "2025"}])
        remote = remote_for(publication, experiences=[{"id": "row-1", "endedOn": "2025", "status": "completed"}])
        baseline = {publication["id"]: observed_baseline(publication, remote)}
        records = {"works": [remote]}
        publication["snapshot"]["title"] = "new title"
        planned = action(publication, baseline, records, capabilities={"works_drafts_and_versions": True})
        self.assertEqual(planned["payload"], {"title": "new title"})
        publication["snapshot"]["properties"]["experiences"][0]["endedOn"] = "2026"
        self.assertEqual(action(publication, baseline, records)["reason"], "array-row-identity-review")

    def test_populated_relationships_and_nested_unknown_fields_are_not_forwarded(self):
        publication = work(article={"id": 9, "markdown": "PRIVATE RELATED ARTICLE"},
                           sources=[{"provider": "web", "externalId": "x", "secret": "PRIVATE"}])
        exported = export_publication(publication)
        self.assertEqual(exported["fields"]["article"], 9)
        self.assertNotIn("PRIVATE", json.dumps(exported))

    def test_incomplete_remote_pages_and_duplicate_identities_fail_closed(self):
        with self.assertRaises(ValueError):
            plan_sync([post()], {}, {"posts": {"docs": [], "hasNextPage": True}})
        other = post()
        other["id"] = other["snapshot"]["id"] = "another-id"
        with self.assertRaises(ValueError):
            plan_sync([post(), other], {}, {})

    def test_identity_rename_cannot_overwrite_another_record(self):
        publication = post()
        baseline, records = fixtures(publication)
        publication["snapshot"]["properties"]["link"] = "another/link"
        records["posts"]["docs"].append(remote_for(publication, remote_id=99))
        self.assertEqual(action(publication, baseline, records)["reason"], "website-identity-owned-by-another-record")

    def test_remote_version_is_required_for_eventual_mutation(self):
        publication = post()
        baseline, records = fixtures(publication)
        records["posts"]["docs"][0].pop("updatedAt")
        publication["snapshot"]["markdown"] = "selected change"
        planned = action(publication, baseline, records)
        self.assertIn("remote_updatedAt_required", planned["blocked_by"])
        self.assertFalse(planned["ready_for_executor"])


if __name__ == "__main__":
    unittest.main()
