from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from markdown_library.core import Library, LibraryError, _split
from markdown_library.merge import local_fields
from markdown_library.sync import export_publication
from markdown_library.transport import _Store, sync_once
from markdown_library.work_profile import compact_works, profile, seed_steam_end, website_properties
import test_imported_sync


class WorkProfileTests(unittest.TestCase):
    setUp = test_imported_sync.ImportedDraftSyncTests.setUp
    imported = test_imported_sync.ImportedDraftSyncTests.imported
    state = test_imported_sync.ImportedDraftSyncTests.state
    files = test_imported_sync.ImportedDraftSyncTests.files
    change = test_imported_sync.ImportedDraftSyncTests.change
    def compact(self, **kwargs):
        preview = compact_works(self.library, **kwargs)
        return compact_works(self.library, apply=True, expected_token=preview['expected_token'], **kwargs)

    def test_preview_readonly_compact_idempotent_preserves_public_projection_and_body(self):
        entry = self.imported(coverUrl='https://example.com/image', releaseDate='2020-01-01')
        before = self.library.get(entry['id'])
        projection = local_fields(before, 'works', library=self.library)
        state = deepcopy(self.state())
        files = self.files()
        preview = compact_works(self.library)
        self.assertEqual(self.files(), files)
        compact_works(self.library, apply=True, expected_token=preview['expected_token'])
        current = self.library.get(entry['id'])
        self.assertNotIn('slug', current['properties'])
        self.assertNotIn('description', current['properties'])
        self.assertEqual(_split(before['markdown'])[1], _split(current['markdown'])[1])
        self.assertEqual(local_fields(current, 'works', library=self.library), projection)
        self.assertEqual(self.state(), state)
        self.assertEqual(self.library.publications(), [])
        self.assertEqual(compact_works(self.library)['changed'], 0)

    def test_reverse_remote_personal_and_metadata_edits_stay_compact_then_publish_keeps_metadata(self):
        entry = self.imported(coverUrl='https://example.com/original')
        self.compact()
        self.change(rating=8, description='Admin updated description', coverUrl='https://example.com/new')
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result['results'][0]['status'], 'remote-draft-imported')
        current = self.library.get(entry['id'])
        self.assertEqual(current['properties']['rating'], 8)
        self.assertNotIn('coverUrl', current['properties'])
        self.assertNotIn('description', current['properties'])
        self.assertEqual(website_properties(self.library, current)['description'], 'Admin updated description')
        publication = self.library.publish(entry['id'], expected_hash=current['sha256'])
        export = export_publication(publication)
        self.assertEqual(export['fields']['coverUrl'], 'https://example.com/new')
        self.assertEqual(export['identity'], {'slug': 'work'})
        result = sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.assertEqual(result['results'][0]['status'], 'observed')
        self.assertEqual(self.client.base['works']['501']['description'], 'Admin updated description')

    def test_conflicting_personal_fields_keep_both_sides(self):
        entry = self.imported(rating=5)
        self.compact()
        current = self.library.get(entry['id'])
        from markdown_library.relations import patch_properties
        self.library.replace(entry['id'], patch_properties(current['markdown'], {'rating': 9}), expected_hash=current['sha256'])
        self.change(rating=8)
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result['results'][0]['status'], 'conflict')
        self.assertEqual(self.library.get(entry['id'])['properties']['rating'], 9)
        self.assertEqual(self.client.latest['works']['501']['rating'], 8)

    def test_expired_batch_refuses_whole_transaction(self):
        entry = self.imported()
        plan = compact_works(self.library)
        current = self.library.get(entry['id'])
        self.library.replace(entry['id'], current['markdown'] + '\nUser typing', expected_hash=current['sha256'])
        with self.assertRaises(LibraryError):
            compact_works(self.library, apply=True, expected_token=plan['expected_token'])
        self.assertIsNone(profile(self.library, entry['id']))

    def test_steam_seed_preserves_state_manual_dates_and_ambiguous_ranges(self):
        empty = {'lastPlayedOn': '2026-10-24', 'status': 'in-progress', 'experiences': []}
        self.assertEqual(seed_steam_end(empty)['status'], 'seeded')
        self.assertEqual(empty['experiences'], [{'endedOn': '2026-10-24'}])
        self.assertEqual(empty['status'], 'in-progress')
        self.assertEqual(seed_steam_end(empty)['status'], 'preserved-existing')
        multiple = {'lastPlayedOn': '2026-10-24', 'experiences': [{}, {}]}
        self.assertEqual(seed_steam_end(multiple)['status'], 'needs-review')
        starts_later = {'lastPlayedOn': '2026-10-24', 'experiences': [{'startedOn': '2026-10-25'}]}
        self.assertEqual(seed_steam_end(starts_later)['status'], 'needs-review')
        self.assertNotIn('endedOn', starts_later['experiences'][0])
        self.assertEqual(seed_steam_end({'lastPlayedOn': '2026-10-24', 'experiences': {}})['status'], 'needs-review')
        malformed = {'lastPlayedOn': '2026-10-24', 'experiences': [{'startedOn': '2026-02-31'}]}
        self.assertEqual(seed_steam_end(malformed)['reason'], 'invalid-start-date')

    def test_internal_work_body_merge_retains_history_import_redirect_and_publication(self):
        from markdown_library.mcp_actions import LibraryActions
        entry = self.imported()
        self.compact()
        target = self.library.get(entry['id'])
        source = self.library.import_note('Detailed impressions', source_id='my-source', title='Impressions')
        actions = LibraryActions(self.library)
        merged = target['markdown'] + '\nDetailed impressions\n'
        hashes = {target['id']: target['sha256'], source['id']: source['sha256']}
        with self.assertRaises(LibraryError):
            actions.merge(target['id'], [source['id']], merged, hashes)
        actions.merge(target['id'], [source['id']], merged, hashes, allow_work_target=True)
        self.assertEqual(self.library.import_note('Stale source', source_id='my-source', title='Impressions')['id'], target['id'])
        self.assertEqual(self.library.publications(), [])
        self.assertIsNotNone(profile(self.library, target['id']))
        self.assertTrue(self.library.history(source['id']))

    def test_new_minimal_work_uses_id_based_slug_without_visible_website_fields(self):
        work = self.library.create('Personal feeling', title='New work', kind='work', properties={'type': 'game', 'rating': 8})
        self.assertNotIn('slug', work['properties'])
        selected = self.library.publish(work['id'], expected_hash=work['sha256'])
        self.assertEqual(export_publication(selected)['identity'], {'slug': 'work-' + work['id']})

    def test_native_minimal_work_registration_requires_no_user_slug(self):
        from markdown_library.core import _hash
        path = self.library.vault / 'New book.md'
        text = '---\ntitle: New book\ntype: book\nrating: 9\n---\nMy feelings'
        path.write_text(text)
        registered = self.library.register('New book.md', kind='work', expected_hash=_hash(text), apply=True)
        selected = self.library.publish(registered['id'], expected_hash=registered['sha256'])
        self.assertEqual(export_publication(selected)['identity'], {'slug': 'work-' + registered['id']})
        self.assertNotIn('slug', registered['properties'])

    def test_numeric_article_and_relationship_count_survive_compaction(self):
        from markdown_library.merge import render_merged
        from markdown_library.relations import refresh_relations
        work = self.library.create('Feeling', title='With article', kind='work',
                                   properties={'slug': 'article-test', 'type': 'game', 'article': 999})
        self.compact()
        current = self.library.get(work['id'])
        fields = local_fields(current, 'works', library=self.library)
        fields['rating'] = 7
        result = render_merged(current, fields, 'works', {}, {'_status': 'draft'}, library=self.library)
        self.assertEqual(_split(result)[0]['rating'], 7)
        self.assertNotIn('article', _split(result)[0])
        self.assertEqual(refresh_relations(self.library)['relationships'], 1)

    def test_history_projection_does_not_invent_ancestor_from_live_metadata(self):
        entry = self.imported()
        original = self.library.get(entry['id'])
        self.compact()
        current = self.library.get(entry['id'])
        historical = local_fields({'id': entry['id'], 'properties': original['properties'],
                                  'markdown': original['markdown']}, 'works', library=self.library,
                                 include_retained=False)
        self.assertEqual(historical['description'], 'Old description')
        sparse_history = local_fields(current, 'works', library=self.library, include_retained=False)
        self.assertNotIn('description', sparse_history)
        # Lifecycle observation historically passes identity inside properties.
        embedded = local_fields({'properties': current['properties'], 'markdown': current['markdown']},
                                'works', library=self.library)
        self.assertEqual(embedded['slug'], 'work')

    def test_legacy_reverse_path_also_preserves_compact_frontmatter(self):
        from markdown_library.transport import _reverse_draft
        entry = self.imported()
        self.compact()
        current = self.library.get(entry['id'])
        publication = {'id': current['id'], 'kind': 'work', 'sha256': current['sha256']}
        action = {'library_draft': {'title': 'Work', 'properties': {
            'slug': 'work', 'type': 'book', 'description': 'Legacy received metadata', 'rating': 8}}}
        updated = _reverse_draft(self.library, publication, action)
        self.assertNotIn('description', updated['properties'])
        self.assertEqual(updated['properties']['rating'], 8)
        self.assertEqual(website_properties(self.library, updated)['description'], 'Legacy received metadata')

    def test_review_token_changes_for_metadata_only_change(self):
        from markdown_library.review import prepare_review
        entry = self.imported()
        self.compact()
        current = self.library.get(entry['id'])
        remote = self.client.latest['works']['501']
        before = prepare_review(self.library, self.state(), current, remote)
        retained = profile(self.library, entry['id'])
        retained['description'] = 'A newer metadata observation'
        unchanged = self.library.replace(entry['id'], current['markdown'], expected_hash=current['sha256'], website_metadata=retained)
        after = prepare_review(self.library, self.state(), unchanged, remote)
        self.assertEqual(current['sha256'], unchanged['sha256'])
        self.assertNotEqual(before['review_token'], after['review_token'])

    def test_restore_precompact_revision_keeps_personal_values_but_current_metadata_cache(self):
        entry = self.imported(rating=6)
        old = self.library.get(entry['id'])
        self.compact()
        current = self.library.get(entry['id'])
        metadata = profile(self.library, entry['id'])
        metadata['description'] = 'Latest website description'
        from markdown_library.relations import patch_properties
        edited = self.library.replace(entry['id'], patch_properties(current['markdown'], {'rating': 9}) + '\nNew thought',
            expected_hash=current['sha256'], website_metadata=metadata)
        restored = self.library.restore(entry['id'], old['sha256'], expected_hash=edited['sha256'])
        self.assertEqual(restored['properties']['rating'], 6)
        self.assertEqual(_split(restored['markdown'])[1], _split(old['markdown'])[1])
        self.assertNotIn('description', restored['properties'])
        self.assertEqual(website_properties(self.library, restored)['description'], 'Latest website description')
