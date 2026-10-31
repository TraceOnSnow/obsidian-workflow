from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library, LibraryError
from markdown_library.catalog_sync import daily_origin, sync_catalog
from markdown_library.sync import export_publication
from markdown_library.transport import _Store, UncertainWrite, sync_once
from markdown_library.work_catalog import (DEFAULT_PATH, build_catalog, catalog_entry,
                                          parse_catalog, review_markdown, row_from_note)
from test_transport import FakePayload


class CatalogSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Library.initialize(str(Path(temporary.name).resolve()))
        self.client = FakePayload()
        self.work = self.library.create('Private full review must never be exported.\n', title='Original title', kind='work',
                                        properties={'slug': 'original-title', 'type': 'game', 'rating': 85,
                                                    'status': 'completed', 'note': 'old short note', 'cover': 9,
                                                    'description': 'website description',
                                                    'experiences': [{'id': 'experience-id', 'endedOn': '2026-01-01'}]})
        self.work_id = self.work['id']
        self.library.publish(self.work_id, expected_hash=self.work['sha256'])
        sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        self.remote_id = str(_Store(self.library, self.client.target).load()['baselines'][self.work_id]['remote_id'])
        self.enable_catalog()
        self.client.mutations.clear()

    def enable_catalog(self):
        with self.library._lock():
            state = self.library._load()
            notes = self.library._scan(state)
            works = [note for note in notes.values() if note['properties']['kind'] == 'work']
            state['work_catalog'] = {'schema_version': 1, 'path': DEFAULT_PATH,
                                     'rows': {note['id']: catalog_entry(note, note['path']) for note in works},
                                     'daily_origins': {note['id']: daily_origin(note) for note in works}}
            self.library._atomic(DEFAULT_PATH, build_catalog([row_from_note(note, note['path']) for note in works]))
            for note in works:
                self.library._atomic(note['path'], review_markdown(note))
            self.library._save(state)

    def edit_row(self, **fields):
        destination = self.library._path(DEFAULT_PATH)
        catalog = parse_catalog(destination.read_text())
        next(row for row in catalog.document['records'] if row['id'] == self.work_id)['values'].update(fields)
        destination.write_text(build_catalog(catalog.document['records'], catalog))

    def remote_edit(self, *, draft=False, **fields):
        latest = self.client.latest['works'][self.remote_id]
        latest.update(deepcopy(fields))
        latest['updatedAt'] = self.client.stamp()
        if draft:
            latest['_status'] = 'draft'
        else:
            self.client.base['works'][self.remote_id] = deepcopy(latest)

    def sync(self, **kwargs):
        return sync_catalog(self.library, self.client, apply=True, accept_remote=True,
                            works_drafts_and_versions=True, **kwargs)

    def test_local_public_rating_edit_publishes_card_preserving_metadata_and_body(self):
        before = deepcopy(self.client.base['works'][self.remote_id])
        self.edit_row(rating=0)
        receipt = self.sync()
        self.assertIn('observed', [row['status'] for row in receipt['results']])
        current = self.client.base['works'][self.remote_id]
        self.assertEqual(current['rating'], 0)
        for field in ('title', 'type', 'slug', 'cover', 'description', 'visibility', 'experiences'):
            self.assertEqual(current[field], before[field])
        self.assertTrue(all(mutation[1] == 'works' for mutation in self.client.mutations))
        self.assertNotIn('Private full review', str(self.client.mutations))
        self.assertEqual(self.library.get(self.work_id)['properties']['rating'], 0)
        self.assertEqual(self.library._read_state()['work_catalog']['daily_origins'][self.work_id],
                         daily_origin(self.library.get(self.work_id)))

    def test_status_note_and_date_keep_original_experience_id(self):
        self.edit_row(status='in-progress', note='new note', endedOn='2026-10-29')
        self.sync()
        current = self.client.base['works'][self.remote_id]
        self.assertEqual(current['status'], 'in-progress')
        self.assertEqual(current['note'], 'new note')
        self.assertEqual(current['experiences'], [{'id': 'experience-id', 'endedOn': '2026-10-29'}])

    def test_remote_only_rating_returns_to_table_without_recreating_standalone_fields(self):
        self.remote_edit(rating=92)
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.get(self.work_id)['properties']['rating'], 92)
        review = self.library._path(self.work['path']).read_text()
        self.assertNotIn('rating:', review)
        self.assertIn('Private full review', review)
        self.assertIn('remote-draft-imported', [row['status'] for row in receipt['catalog_selection']['results']])
        self.sync()
        self.assertEqual(self.client.mutations, [])

    def test_same_daily_field_changed_both_ends_is_conflict(self):
        self.edit_row(rating=99)
        self.remote_edit(rating=93)
        selected = self.library.publication(self.work_id)
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.get(self.work_id)['properties']['rating'], 99)
        self.assertEqual(self.client.base['works'][self.remote_id]['rating'], 93)
        self.assertEqual(self.library.publication(self.work_id), selected)
        self.assertIn('conflict', [row['status'] for row in receipt['results']])

    def test_preexisting_conflict_without_new_daily_edit_is_reported_once(self):
        self.edit_row(rating=89)
        with self.library._lock():
            state = self.library._load()
            note = self.library._scan(state)[self.work_id]
            state['work_catalog']['daily_origins'][self.work_id] = daily_origin(note)
            self.library._save(state)
        self.remote_edit(rating=90)
        selected = self.library.publication(self.work_id)
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.publication(self.work_id), selected)
        conflicts = [row for row in receipt['results'] if row['id'] == self.work_id and row['status'] == 'conflict']
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(receipt['catalog_selection']['results'], [])

    def test_new_daily_conflict_keeps_preflight_evidence_without_duplicate_result(self):
        self.edit_row(rating=89)
        self.remote_edit(rating=90)
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        conflicts = [row for row in receipt['results'] if row['id'] == self.work_id and row['status'] == 'conflict']
        self.assertEqual(len(conflicts), 1)
        preflight = receipt['catalog_selection']['results']
        self.assertEqual(len(preflight), 1)
        self.assertEqual(preflight[0]['fields_requiring_review'], ['rating'])

    def test_distinct_daily_fields_merge_and_preserve_newer_remote_note(self):
        self.edit_row(rating=99)
        self.remote_edit(note='new admin short note')
        receipt = self.sync()
        current = self.client.base['works'][self.remote_id]
        self.assertEqual(current['rating'], 99)
        self.assertEqual(current['note'], 'new admin short note')
        self.assertEqual(self.library.get(self.work_id)['properties']['note'], 'new admin short note')
        self.assertIn('observed', [row['status'] for row in receipt['results']])

    def test_remote_description_and_local_rating_merge_preserving_website_metadata(self):
        self.edit_row(rating=97)
        self.remote_edit(description='new website description')
        self.sync()
        self.assertEqual(self.client.base['works'][self.remote_id]['description'], 'new website description')
        self.assertEqual(self.client.base['works'][self.remote_id]['rating'], 97)

    def test_local_title_is_not_authorized_by_daily_rating_edit(self):
        self.edit_row(title='Private renamed title', rating=94)
        self.sync()
        self.assertEqual(self.client.base['works'][self.remote_id]['title'], 'Original title')
        self.assertEqual(self.library.get(self.work_id)['properties']['title'], 'Private renamed title')
        self.assertEqual(self.client.base['works'][self.remote_id]['rating'], 94)

    def test_private_remote_is_never_republished(self):
        self.remote_edit(visibility='private')
        self.edit_row(rating=99)
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.client.base['works'][self.remote_id]['visibility'], 'private')
        self.assertIn('blocked', [row['status'] for row in receipt['results']])

    def test_existing_website_draft_is_not_promoted(self):
        self.remote_edit(draft=True, note='unpublished admin short note')
        self.edit_row(rating=98)
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.client.latest['works'][self.remote_id]['_status'], 'draft')
        self.assertEqual(self.client.base['works'][self.remote_id]['rating'], 85)
        self.assertIn('blocked', [row['status'] for row in receipt['results']])

    def test_new_catalog_work_is_not_first_published(self):
        note = self.library.create('', title='New private work', kind='work',
                                   properties={'slug': 'private-work', 'type': 'game', 'rating': 50})
        self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertIsNone(self.library.publication(note['id']))
        self.assertNotIn(note['id'], self.library._read_state()['work_catalog']['daily_origins'])

    def test_migration_origin_suppresses_old_unselected_difference(self):
        self.edit_row(rating=96)
        with self.library._lock():
            state = self.library._load()
            note = self.library._scan(state)[self.work_id]
            state['work_catalog']['daily_origins'][self.work_id] = daily_origin(note)
            self.library._save(state)
        selected = self.library.publication(self.work_id)
        self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.publication(self.work_id), selected)
        self.edit_row(note='newly edited short note')
        self.sync()
        self.assertEqual(self.client.base['works'][self.remote_id]['rating'], 85)
        self.assertEqual(self.client.base['works'][self.remote_id]['note'], 'newly edited short note')
        self.assertEqual(self.library.get(self.work_id)['properties']['rating'], 96)

    def test_missing_row_does_not_authorize_website_deletion(self):
        destination = self.library._path(DEFAULT_PATH)
        catalog = parse_catalog(destination.read_text())
        destination.write_text(build_catalog([], catalog))
        with self.assertRaises(LibraryError):
            self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.client.base['works'][self.remote_id]['visibility'], 'public')

    def test_unknown_pending_does_not_replace_selection_or_retry_write(self):
        store = _Store(self.library, self.client.target)
        state = store.load()
        publication = self.library.publication(self.work_id)
        exported = export_publication(self.library._read_state()['publications'][self.work_id])
        state['pending'][self.work_id] = {'publication_hash': 'unknown-hash', 'identity': exported['identity'],
                                          'state': 'published', 'phase': 'reconciliation-required'}
        store.save(state)
        self.edit_row(rating=90)
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.publication(self.work_id), publication)
        self.assertIn(self.work_id, receipt['pending_ids'])
        self.assertIn('reconciliation-required', [row['status'] for row in receipt['results']])

    def test_uncertain_draft_write_is_not_repeated_or_automatically_published(self):
        self.edit_row(rating=91)
        update = self.client.update
        failed = False

        def timeout_after_update(*args, **kwargs):
            nonlocal failed
            result = update(*args, **kwargs)
            if not failed:
                failed = True
                raise UncertainWrite('simulated timeout after draft save')
            return result

        self.client.update = timeout_after_update
        first = self.sync()
        self.assertIn(self.work_id, first['pending_ids'])
        mutations = deepcopy(self.client.mutations)
        second = self.sync()
        self.assertEqual(self.client.mutations, mutations)
        self.assertIn(self.work_id, second['pending_ids'])
        self.assertEqual(self.client.base['works'][self.remote_id]['rating'], 85)

    def test_multiple_remote_updates_return_into_one_catalog_without_stale_storage_guards(self):
        second = self.library.create('Second private review', title='Second work', kind='work',
                                     properties={'slug': 'second-work', 'type': 'game', 'rating': 70})
        self.library.publish(second['id'], expected_hash=second['sha256'])
        sync_once(self.library, self.client, apply=True, works_drafts_and_versions=True)
        with self.library._lock():
            state = self.library._load()
            notes = self.library._scan(state)
            state['work_catalog']['daily_origins'][second['id']] = daily_origin(notes[second['id']])
            self.library._save(state)
        second_remote = str(_Store(self.library, self.client.target).load()['baselines'][second['id']]['remote_id'])
        self.remote_edit(rating=92)
        self.client.latest['works'][second_remote].update(rating=93, updatedAt=self.client.stamp())
        self.client.base['works'][second_remote] = deepcopy(self.client.latest['works'][second_remote])
        self.client.mutations.clear()
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.get(self.work_id)['properties']['rating'], 92)
        self.assertEqual(self.library.get(second['id'])['properties']['rating'], 93)
        self.assertNotIn('conflict', [row['status'] for row in receipt['results']])

    def test_same_edit_at_both_ends_converges_without_a_website_write(self):
        self.edit_row(rating=92)
        self.remote_edit(rating=92)
        receipt = self.sync()
        self.assertEqual(self.client.mutations, [])
        self.assertIn('reconciled', [row['status'] for row in receipt['results']])
        self.assertEqual(self.library._read_state()['work_catalog']['daily_origins'][self.work_id],
                         daily_origin(self.library.get(self.work_id)))

    def test_preview_preserves_selection_origins_table_and_network(self):
        self.edit_row(rating=92)
        selected = self.library.publication(self.work_id)
        private = self.library._path('.markdown-library/state.json').read_bytes()
        table = self.library._path(DEFAULT_PATH).read_bytes()
        receipt = sync_catalog(self.library, self.client, works_drafts_and_versions=True)
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.library.publication(self.work_id), selected)
        self.assertEqual(self.library._path('.markdown-library/state.json').read_bytes(), private)
        self.assertEqual(self.library._path(DEFAULT_PATH).read_bytes(), table)
        self.assertIn('catalog-selection-available', [row['status'] for row in receipt['catalog_selection']['results']])

    def test_associated_post_is_not_selected_or_published(self):
        post = self.library.create('Private associated article', title='Private article', kind='post',
                                   properties={'link': 'private-article'})
        self.edit_row(rating=94)
        receipt = self.sync()
        self.assertIsNone(self.library.publication(post['id']))
        self.assertFalse(receipt['catalog_selection']['associated_posts_selected'])
        self.assertTrue(all(mutation[1] == 'works' for mutation in self.client.mutations))
        self.assertEqual(self.client.base['posts'], {})


if __name__ == '__main__':
    unittest.main()
