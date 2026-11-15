from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from markdown_library import Library, LibraryError
from markdown_library.bootstrap import _index
from markdown_library.catalog_sync import daily_origin
from markdown_library.cli import dispatch, parser
from markdown_library.core import _render, _split
from markdown_library.lifecycle import _local
from markdown_library.mcp_actions import LibraryActions
from markdown_library.overview import publication_overview
from markdown_library.organize import organize
from markdown_library.relations import refresh_relations, wikilink
from markdown_library.transport import sync_once
from markdown_library.work_catalog import (DEFAULT_PATH, build_catalog, catalog_entry,
                                          parse_catalog, review_markdown, row_from_note)
from markdown_library.work_profile import set_profile
from markdown_library.work_review import prepare_work_review, review_body
from test_transport import FakePayload


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Library.initialize(Path(temporary.name) / 'vault')
        self.virtual = self.library.create('', title='Catalog-only work', kind='work',
                                            properties={'slug': 'catalog-only', 'type': 'game', 'rating': 72,
                                                        'note': 'short impression', 'status': 'completed'})
        self.body = '# Private review\n\nDetailed personal review.\n\n```text\nkeep code bytes\n```\n'
        self.reviewed = self.library.create(self.body, title='Reviewed work', kind='work',
                                             properties={'slug': 'reviewed-work', 'type': 'book', 'rating': 88,
                                                         'note': 'daily short note',
                                                         'experiences': [{'id': 'original-exp', 'endedOn': '2026-01-01'}]})
        with self.library._lock():
            state = self.library._load()
            notes = self.library._scan(state)
            paths = {self.virtual['id']: None, self.reviewed['id']: self.reviewed['path']}
            state['work_catalog'] = {'schema_version': 1, 'path': DEFAULT_PATH,
                                     'rows': {key: catalog_entry(note, paths[key]) for key, note in notes.items()},
                                     'daily_origins': {key: daily_origin(note) for key, note in notes.items()}}
            self.library._atomic(DEFAULT_PATH, build_catalog([row_from_note(note, paths[key]) for key, note in notes.items()]))
            self.library._path(self.virtual['path']).unlink()
            self.library._atomic(self.reviewed['path'], review_markdown(self.reviewed))
            self.library._save(state)
        # Establish the ordinary observed logical index once, as migration does.
        self.library.list_notes()

    def snapshot(self):
        return {path.relative_to(self.library.vault).as_posix(): path.read_bytes()
                for path in self.library.vault.rglob('*') if path.is_file()}

    def test_confirm_native_catalog_removals_individually_preserves_other_edits(self):
        notes = [self.library.get(item['id']) for item in (self.virtual, self.reviewed)]
        catalog_path = self.library._path(DEFAULT_PATH)
        catalog = parse_catalog(catalog_path.read_text())
        catalog_path.write_text(build_catalog([], catalog))
        self.library._path(self.reviewed['path']).unlink()
        before = self.snapshot()
        first = notes[0]
        self.library.confirm_missing_delete(first['id'], expected_hash=first['sha256'])
        self.assertEqual(before, self.snapshot())
        self.library.confirm_missing_delete(first['id'], expected_hash=first['sha256'], apply=True)
        self.assertNotIn(notes[1]['id'], self.library.deletion_records())
        with self.assertRaisesRegex(LibraryError, 'Known Work catalog row is missing'):
            self.library.list_notes()
        second = notes[1]
        self.library.confirm_missing_delete(second['id'], expected_hash=second['sha256'], apply=True)
        self.assertEqual(catalog_path.read_bytes(), before[DEFAULT_PATH])
        self.assertEqual(self.library.list_notes(), [])
        for note in notes:
            retained = self.library._path('.markdown-library/trash/' + note['id'] + '.md')
            self.assertEqual(retained.read_text(), note['markdown'])

    def test_confirm_native_catalog_removal_refuses_existing_row(self):
        note = self.library.get(self.virtual['id'])
        with self.assertRaisesRegex(LibraryError, 'Identity still exists'):
            self.library.confirm_missing_delete(note['id'], expected_hash=note['sha256'], apply=True)

    def test_steam_batch_preserves_unrelated_native_blank_dates_after_readback(self):
        from markdown_library.work_profile import compact_works
        self.enable_completed_default()
        state = self.library._read_state()
        state['work_catalog']['default_end_today'] = True
        self.library._save(state)
        destination = self.library._path(DEFAULT_PATH)
        catalog = parse_catalog(destination.read_text())
        row = deepcopy(catalog.document['records'][0])
        row['id'] = 'unrelated-native-row'
        row['values'].update(title='Untouched game', status='', review=None, endedOn='')
        catalog.document['records'].append(row)
        destination.write_text(build_catalog(catalog.document['records'], catalog))
        before = deepcopy(row)
        request = dict(seed_steam=True, steam_dates={self.virtual['id']: '2026-11-14'},
                       note_ids=[self.virtual['id']])
        preview = compact_works(self.library, **request)
        compact_works(self.library, apply=True, expected_token=preview['expected_token'],
                      apply_catalog_defaults=False, **request)
        self.library.get(self.virtual['id'])
        self.assertEqual(self.table_record(row['id']), before)

    def test_organize_renames_work_review_and_repairs_links_without_changing_fields(self):
        work = self.library.get(self.reviewed['id'])
        backlink = self.library.create('[[%s|Review]]' % work['path'][:-3], title='Backlink')
        destination = 'works/感想/小说/2020/Readable work.md'
        request = {'operation': 'move', 'moves': [{'note_id': work['id'],
            'destination': destination, 'expected_hash': work['sha256']}]}
        before = self.snapshot()
        preview = organize(self.library, request)
        self.assertEqual(before, self.snapshot())
        result = organize(self.library, request, apply=True, expected_token=preview['token'])
        self.assertTrue(result['readback_verified'])
        current = self.library.get(work['id'])
        self.assertEqual(current['path'], destination)
        self.assertEqual(current['properties'], work['properties'])
        self.assertEqual(current['markdown'], work['markdown'])
        self.assertFalse(self.library._path(work['path']).exists())
        self.assertIn(destination[:-3], self.library.get(backlink['id'])['markdown'])
        row = parse_catalog(self.library._path(DEFAULT_PATH).read_text()).document['records']
        self.assertIn(destination[:-3], next(r for r in row if r['id'] == work['id'])['values']['review'])
        self.assertEqual(self.library.publications(), [])

    def test_organize_rejects_work_title_changes_moves_outside_review_root_and_empty_reviews(self):
        work = self.library.get(self.reviewed['id'])
        virtual = self.library.get(self.virtual['id'])
        for note, destination, extra in [
            (work, str(Path(work['path']).with_name('Changed.md')), {'title': 'Changed'}),
            (work, '00_收件箱/Changed.md', {}),
            (work, 'works/Elsewhere.md', {}),
            (work, 'works/感想/../Changed.md', {}),
            (virtual, 'works/Changed.md', {}),
        ]:
            before = self.snapshot()
            with self.assertRaises(LibraryError):
                organize(self.library, {'operation': 'move', 'moves': [{'note_id': note['id'],
                    'destination': destination, 'expected_hash': note['sha256'], **extra}]})
            self.assertEqual(before, self.snapshot())

    def test_work_move_conflict_does_not_partially_reorganize_reviews(self):
        work = self.library.get(self.reviewed['id'])
        request = {'operation': 'move', 'moves': [{'note_id': work['id'],
            'destination': 'works/感想/小说/2020/Readable.md', 'expected_hash': work['sha256']}]}
        preview = organize(self.library, request)
        edited = self.library.replace(work['id'], work['markdown'] + '\nUser edit\n', expected_hash=work['sha256'])
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            organize(self.library, request, apply=True, expected_token=preview['token'])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.library.get(work['id'])['sha256'], edited['sha256'])

    def test_organize_merges_note_into_catalog_work_and_retains_source(self):
        work = self.library.get(self.virtual['id'])
        source = self.library.create('Personal experience', title='Source', path='00_收件箱/Source.md')
        request = {'operation': 'merge', 'target_id': work['id'], 'source_ids': [source['id']],
            'markdown': work['markdown'] + '\n# Review\n\nPersonal experience\n',
            'expected_hashes': {work['id']: work['sha256'], source['id']: source['sha256']}}
        preview = organize(self.library, request)
        result = organize(self.library, request, apply=True, expected_token=preview['token'])
        self.assertTrue(result['readback_verified'])
        self.assertEqual(self.library.get(work['id'])['properties'], work['properties'])
        self.assertIn('Personal experience', self.library.get(work['id'])['markdown'])
        self.assertFalse(self.library._path(source['path']).exists())
        record = self.library._read_state()['notes'][source['id']]
        self.assertEqual(record['merged_into'], work['id'])
        self.assertEqual(self.library._path(record['trash']).read_text(), source['markdown'])
        self.assertEqual(self.library.publications(), [])

    def test_new_catalog_reviews_use_readable_names_and_refuse_collisions(self):
        work = self.library.create('# Actual review\n\nMy experience.', title='My game: First', kind='work')
        self.assertEqual(work['path'], 'works/感想/My game： First.md')
        with self.assertRaises(LibraryError):
            self.library.create('# Another review\n\nAnother experience.', title='My game: First', kind='work')
        virtual = self.library.get(self.virtual['id'])
        self.library.replace(virtual['id'], virtual['markdown'] + '\n# New review\n\nMy experience.\n',
                             expected_hash=virtual['sha256'])
        self.assertEqual(self.library.get(virtual['id'])['path'], 'works/感想/Catalog-only work.md')

    def enable_completed_default(self):
        state = self.library._read_state()
        state['work_catalog']['default_status'] = 'completed'
        self.library._save(state)

    def test_new_work_default_preserves_explicit_status_and_existing_unknown(self):
        self.enable_completed_default()
        default = self.library.create('', title='New game', kind='work', properties={'type': 'game'})
        explicit = self.library.create('', title='Still playing', kind='work', properties={'type': 'game', 'status': 'in-progress'})
        self.assertEqual(self.library.get(default['id'])['properties']['status'], 'completed')
        self.assertEqual(explicit['properties']['status'], 'in-progress')
        self.assertIsNone(self.library.get(self.reviewed['id'])['properties'].get('status'))

    def test_native_row_default_is_saved_once_without_overriding_later_clear(self):
        self.enable_completed_default()
        path = self.library._path(DEFAULT_PATH)
        table = parse_catalog(path.read_text())
        new = deepcopy(table.document['records'][0])
        new['id'] = 'new-native-work'
        new['values'].update(title='New native game', status='', review=None)
        table.document['records'].append(new)
        path.write_text(build_catalog(table.document['records'], table))
        before = self.snapshot()
        _index(self.library)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.library.get(new['id'])['properties']['status'], 'completed')
        self.assertEqual(self.table_record(new['id'])['values']['status'], 'completed')
        self.edit_table(new['id'], status=None)
        self.assertIsNone(self.library.get(new['id'])['properties']['status'])

    def test_bulk_status_script_preserves_other_cells_views_and_publication_selections(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('bulk_work_status', Path(__file__).resolve().parents[1] / 'scripts/set-work-status.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.edit_table(self.virtual['id'], status='in-progress')
        before = self.snapshot()
        selected = deepcopy(self.library._read_state()['publications'])
        preview = module.update(self.library, 'completed', set_default=True)
        self.assertEqual(before, self.snapshot())
        result = module.update(self.library, 'completed', set_default=True, apply=True, expected_token=preview['token'])
        self.assertEqual(result['changed'], 1)
        self.assertEqual(self.library.get(self.virtual['id'])['properties']['status'], 'in-progress')
        self.assertEqual(selected, self.library._read_state()['publications'])
        self.assertEqual(self.library.get(self.reviewed['id'])['properties']['rating'], 88)
        self.assertEqual(self.library._read_state()['work_catalog']['default_status'], 'completed')

    def test_new_record_end_date_defaults_to_today_without_touching_existing_dates(self):
        self.enable_completed_default()
        state = self.library._read_state()
        state['work_catalog']['default_end_today'] = True
        self.library._save(state)
        with patch('markdown_library.work_catalog._today', return_value='2026-10-28'):
            new = self.library.create('', title='New game', kind='work', properties={'type': 'game'})
            self.assertEqual(new['properties']['experiences'], [{'endedOn': '2026-10-28'}])
            explicit = self.library.create('', title='Known date', kind='work', properties={'type': 'game', 'experiences': [{'endedOn': '2025-03'}]})
            self.assertEqual(explicit['properties']['experiences'], [{'endedOn': '2025-03'}])
            table = parse_catalog(self.library._path(DEFAULT_PATH).read_text())
            row = deepcopy(table.document['records'][0])
            row['id'] = 'new-row-with-date'
            row['values'].update(title='New table game', review=None, endedOn='')
            table.document['records'].append(row)
            self.library._path(DEFAULT_PATH).write_text(build_catalog(table.document['records'], table))
            self.assertEqual(self.library.get(row['id'])['properties']['experiences'][0]['endedOn'], '2026-10-28')
            self.edit_table(row['id'], endedOn=None)
            self.assertIsNone(self.library.get(row['id'])['properties']['experiences'][0]['endedOn'])
        self.assertFalse(self.library.get(self.virtual['id'])['properties'].get('experiences'))
        self.assertEqual(self.library.get(self.reviewed['id'])['properties']['experiences'], [{'id': 'original-exp', 'endedOn': '2026-01-01'}])

    def edit_table(self, note_id, **fields):
        destination = self.library._path(DEFAULT_PATH)
        catalog = parse_catalog(destination.read_text())
        row = next(row for row in catalog.document['records'] if row['id'] == note_id)
        row['values'].update(fields)
        destination.write_text(build_catalog(catalog.document['records'], catalog))

    def table_record(self, note_id):
        return next(row for row in parse_catalog(self.library._path(DEFAULT_PATH).read_text()).document['records']
                    if row['id'] == note_id)

    def test_get_virtual_and_reviewed_work_have_full_logical_markdown_with_separate_physical_fields(self):
        virtual = self.library.get(self.virtual['id'])
        reviewed = self.library.get(self.reviewed['id'])
        self.assertTrue(virtual['catalog_backed'])
        self.assertFalse(self.library._path(virtual['path']).exists())
        self.assertEqual(virtual['properties']['rating'], 72)
        self.assertEqual(reviewed['properties']['rating'], 88)
        self.assertEqual(_split(reviewed['markdown'])[1], self.body)
        properties, body = _split(self.library._path(reviewed['storage_path']).read_text())
        self.assertEqual(body, self.body)
        self.assertNotIn('rating', properties)
        self.assertNotIn('note', properties)
        self.assertNotIn('experiences', properties)
        self.assertEqual({note['id'] for note in self.library.list_notes()}, {virtual['id'], reviewed['id']})

    def test_replace_changes_virtual_daily_values_and_creates_only_needed_review_file(self):
        original = self.library.get(self.virtual['id'])
        properties = deepcopy(original['properties'])
        properties.update(rating=0, note='A new short impression')
        self.library.replace(original['id'], _render(properties, 'A new full private review.\n'),
                             expected_hash=original['sha256'])
        current = self.library.get(original['id'])
        self.assertEqual(current['properties']['rating'], 0)
        self.assertEqual(self.table_record(original['id'])['values']['rating'], 0)
        self.assertIn('A new full private review.', self.library._path(current['storage_path']).read_text())
        self.assertNotIn('rating:', self.library._path(current['storage_path']).read_text())
        self.assertEqual(self.library.get(self.reviewed['id'])['properties']['rating'], 88)
        self.assertFalse(self.library._path(self.virtual['path']).exists())

    def test_create_work_uses_one_stable_catalog_id_and_no_standalone_daily_file(self):
        created = self.library.create('', title='Created through tool', kind='work',
                                       properties={'type': 'movie', 'rating': 64})
        current = self.library.get(created['id'])
        self.assertEqual(self.table_record(created['id'])['id'], created['id'])
        self.assertEqual(current['properties']['rating'], 64)
        self.assertTrue(current['catalog_backed'])
        self.assertFalse(self.library._path(current['path']).exists())
        self.assertEqual(len(self.library.list_notes()), 3)

    def test_native_new_row_is_discovered_with_same_identity_and_can_be_updated(self):
        catalog = parse_catalog(self.library._path(DEFAULT_PATH).read_text())
        native = deepcopy(catalog.document['records'][0])
        native['id'] = 'native-ui-row-stable-id'
        native['values'].update(title='Added in native table', rating=59, review=None)
        catalog.document['records'].append(native)
        self.library._path(DEFAULT_PATH).write_text(build_catalog(catalog.document['records'], catalog))
        current = self.library.get(native['id'])
        self.assertEqual(current['id'], native['id'])
        self.assertEqual(current['properties']['rating'], 59)
        properties = {**current['properties'], 'rating': 61}
        self.library.replace(current['id'], _render(properties, ''), expected_hash=current['sha256'])
        self.assertEqual(self.table_record(native['id'])['values']['rating'], 61)
        self.assertIsNone(self.library.publication(native['id']))
        self.assertNotIn(native['id'], self.library._read_state()['work_catalog']['daily_origins'])

    def test_readonly_index_does_not_checkpoint_external_table_changes_or_objects(self):
        self.edit_table(self.virtual['id'], rating=77)
        before = self.snapshot()
        state, notes = _index(self.library)
        self.assertEqual(notes[self.virtual['id']]['properties']['rating'], 77)
        self.assertEqual(notes[self.reviewed['id']]['properties']['rating'], 88)
        self.assertEqual(self.snapshot(), before)
        self.assertNotEqual(state['notes'][self.virtual['id']]['sha256'], notes[self.virtual['id']]['sha256'])

    def test_move_unrelated_note_repairs_actual_review_body_preserving_all_daily_fields(self):
        reference = self.library.create('Referenced text', title='Referenced', path='00_收件箱/referenced.md')
        reviewed = self.library.get(self.reviewed['id'])
        body = _split(reviewed['markdown'])[1] + '\n[[00_收件箱/referenced|reference]]\n'
        self.library.replace(reviewed['id'], _render(reviewed['properties'], body), expected_hash=reviewed['sha256'])
        before = deepcopy(self.table_record(reviewed['id'])['values'])
        LibraryActions(self.library).move(reference['id'], '30_知识与参考/moved-reference.md', reference['sha256'])
        current = self.library.get(reviewed['id'])
        self.assertIn('[[30_知识与参考/moved-reference|reference]]', _split(current['markdown'])[1])
        self.assertEqual(self.table_record(reviewed['id'])['values'], before)
        physical_properties, physical_body = _split(self.library._path(current['storage_path']).read_text())
        self.assertNotIn('rating', physical_properties)
        self.assertIn('moved-reference', physical_body)
        self.assertEqual(current['properties']['experiences'][0]['id'], 'original-exp')

    def test_prepare_review_relations_and_overview_preserve_separate_drafts_and_catalog_fields(self):
        work = self.library.get(self.reviewed['id'])
        plan = prepare_work_review(self.library, work['id'], work['sha256'])
        prepared = prepare_work_review(self.library, work['id'], work['sha256'],
                                       expected_token=plan['expected_token'], apply=True)
        post = self.library.get(prepared['post']['id'])
        self.assertEqual(_split(post['markdown'])[1], self.body)
        relations = refresh_relations(self.library, apply=True)
        self.assertEqual(relations['issues'], [])
        post = self.library.get(post['id'])
        self.assertEqual(post['properties']['related_works'], [wikilink(self.library.get(work['id']))])
        overview = publication_overview(self.library, {'mode': 'read-only-review', 'items': []}, apply=True)
        self.assertEqual(overview['counts']['local'], 3)
        self.assertEqual(self.library.get(work['id'])['properties']['rating'], 88)
        self.assertEqual(review_body(_split(self.library.get(work['id'])['markdown'])[1]), self.body)
        self.assertEqual(self.library.publications(), [])
        self.assertNotIn('rating:', self.library._path(self.reviewed['path']).read_text())

    def test_overview_after_native_table_edit_is_guarded_against_actual_concurrency_only(self):
        self.edit_table(self.virtual['id'], rating=79)
        before_selection = self.library.publications()
        result = publication_overview(self.library, {'mode': 'read-only-review', 'items': []}, apply=True)
        self.assertTrue(result['applied'])
        self.assertEqual(self.library.get(self.virtual['id'])['properties']['rating'], 79)
        self.assertEqual(self.library.publications(), before_selection)

    def test_overview_virtual_row_links_point_to_existing_catalog(self):
        publication_overview(self.library, {'mode': 'read-only-review', 'items': []}, apply=True)
        content = self.library._path('发布与同步状态.md').read_text()
        self.assertIn('(' + quote(DEFAULT_PATH, safe='/') + ')', content)
        self.assertNotIn('%23work-', content)

    def test_virtual_article_relation_and_status_footer_stay_inside_catalog_without_new_review(self):
        post = self.library.create('Existing independent article', title='Existing article', kind='post',
                                   properties={'link': 'existing-article'})
        with self.library._lock():
            state = self.library._load()
            set_profile(state, self.virtual['id'], {'article': post['id']})
            self.library._save(state)
        self.assertEqual(refresh_relations(self.library, apply=True)['issues'], [])
        publication_overview(self.library, {'mode': 'read-only-review', 'items': []}, apply=True)
        virtual = self.library.get(self.virtual['id'])
        self.assertFalse(self.library._path(virtual['path']).exists())
        self.assertIsNone(self.table_record(virtual['id'])['values']['review'])
        self.assertEqual(virtual['properties']['rating'], 72)
        self.assertIn('markdown-library:publication-nav:start', virtual['markdown'])
        post = self.library.get(post['id'])
        self.assertEqual(post['properties']['related_works'], [wikilink(virtual)])
        content = self.library._path('发布与同步状态.md').read_text()
        self.assertNotIn('#work-', content)
        self.assertEqual(self.library.publications(), [])

    def test_prepare_empty_virtual_work_fails_without_state_or_file_changes(self):
        work = self.library.get(self.virtual['id'])
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            prepare_work_review(self.library, work['id'], work['sha256'])
        self.assertEqual(self.snapshot(), before)

    def test_lifecycle_local_delete_and_recover_both_storage_forms(self):
        for original in (self.virtual, self.reviewed):
            with self.subTest(work=original['id']):
                work = self.library.get(original['id'])
                record = self.library._read_state()['notes'][work['id']]
                local = _local(self.library, work['id'], record)
                self.assertEqual(local['sha256'], work['sha256'])
                self.assertFalse(local['deleted'])
                deleted = self.library.delete(work['id'], expected_hash=work['sha256'])
                with self.assertRaises(LibraryError):
                    self.library.get(work['id'])
                trashed = _local(self.library, work['id'], self.library._read_state()['notes'][work['id']])
                self.assertTrue(trashed['deleted'])
                self.library.recover(work['id'], expected_hash=deleted['sha256'])
                recovered = self.library.get(work['id'])
                self.assertEqual(recovered['markdown'], work['markdown'])
                self.assertEqual(self.table_record(work['id'])['values']['rating'], work['properties']['rating'])
                self.assertIsNone(self.library.publication(work['id']))

    def test_stale_hash_after_table_edit_does_not_overwrite_new_rating_or_body(self):
        old = self.library.get(self.reviewed['id'])
        self.edit_table(old['id'], rating=91)
        table_before = self.library._path(DEFAULT_PATH).read_bytes()
        review_before = self.library._path(old['storage_path']).read_bytes()
        with self.assertRaises(LibraryError):
            self.library.replace(old['id'], old['markdown'] + '\nOld stale body change', expected_hash=old['sha256'])
        self.assertEqual(self.library._path(DEFAULT_PATH).read_bytes(), table_before)
        self.assertEqual(self.library._path(old['storage_path']).read_bytes(), review_before)
        self.assertEqual(self.library.get(old['id'])['properties']['rating'], 91)

    def test_editor_change_after_scan_is_caught_by_physical_storage_guard(self):
        work = self.library.get(self.reviewed['id'])
        original_guard = self.library._assert_note_storage

        def concurrent_edit(note):
            self.edit_table(self.virtual['id'], rating=81)
            original_guard(note)

        with patch.object(self.library, '_assert_note_storage', side_effect=concurrent_edit):
            with self.assertRaises(LibraryError):
                self.library.replace(work['id'], work['markdown'] + '\nNew body', expected_hash=work['sha256'])
        self.assertEqual(self.library.get(self.virtual['id'])['properties']['rating'], 81)
        self.assertNotIn('New body', self.library.get(work['id'])['markdown'])

    def test_unrelated_row_edit_keeps_per_work_hash_and_survives_other_work_replace(self):
        old = self.library.get(self.reviewed['id'])
        self.edit_table(self.virtual['id'], rating=82)
        self.assertEqual(self.library.get(old['id'])['sha256'], old['sha256'])
        self.library.replace(old['id'], old['markdown'] + '\nNew review paragraph', expected_hash=old['sha256'])
        self.assertEqual(self.library.get(self.virtual['id'])['properties']['rating'], 82)
        self.assertIn('New review paragraph', self.library.get(old['id'])['markdown'])

    def test_merge_into_virtual_work_saves_full_target_before_recoverably_deleting_source(self):
        source = self.library.create('Substantive source review to merge.\n', title='Source to merge')
        target = self.library.get(self.virtual['id'])
        markdown = _render(target['properties'], 'Merged full review with substantive source content.\n')
        LibraryActions(self.library).merge(target['id'], [source['id']], markdown,
                                          {target['id']: target['sha256'], source['id']: source['sha256']},
                                          allow_work_target=True)
        current = self.library.get(target['id'])
        self.assertIn('Merged full review with substantive source content.', _split(current['markdown'])[1])
        self.assertEqual(current['properties']['rating'], 72)
        self.assertIsNotNone(self.table_record(target['id'])['values']['review'])
        self.assertIn('Merged full review', self.library._path(current['storage_path']).read_text())
        record = self.library._read_state()['notes'][source['id']]
        self.assertTrue(record['deleted'])
        self.assertIn('Substantive source review', self.library._path(record['trash']).read_text())
        self.assertEqual(self.library.publications(), [])

    def test_cli_sync_inactive_library_keeps_ordinary_unselected_edit_private(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Library.initialize(Path(directory) / 'legacy-vault')
            work = library.create('', title='Legacy', kind='work',
                                  properties={'slug': 'legacy-work', 'type': 'game', 'rating': 80})
            client = FakePayload()
            library.publish(work['id'], expected_hash=work['sha256'])
            sync_once(library, client, apply=True, works_drafts_and_versions=True)
            current = library.get(work['id'])
            library.replace(work['id'], _render({**current['properties'], 'rating': 90}, ''), expected_hash=current['sha256'])
            client.mutations.clear()
            arguments = parser().parse_args(['--vault', str(library.vault), 'sync', '--url', client.target,
                                             '--apply', '--accept-remote', '--works-drafts-and-versions'])
            with patch.dict('os.environ', {'HOMEPAGE_API_KEY': 'fixture-key'}), patch('markdown_library.transport.PayloadClient', return_value=client):
                result = dispatch(arguments)
            self.assertEqual(client.mutations, [])
            self.assertNotIn('catalog_selection', result)
            self.assertEqual(library.publication(work['id'])['properties']['rating'], 80)


if __name__ == '__main__':
    unittest.main()
