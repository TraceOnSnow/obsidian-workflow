from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from markdown_library.core import Library, LibraryError, _hash, _read, _render, _split
from markdown_library.work_catalog import (
    assert_storage, backing_path, build_catalog, catalog_entry, enabled,
    expand_operations, has_review_body, parse_catalog, review_markdown,
    row_from_note, scan_overlay,
)


class WorkCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Library.initialize(Path(self.temporary.name).resolve())
        self.state = self.library._read_state()
        self.state['work_catalog'] = {'schema_version': 1, 'path': 'works/作品总表.md', 'rows': {}}

    def note(self, note_id='work-a', body='', **properties):
        metadata = {'id': note_id, 'kind': 'work', 'schema_version': 1, 'title': note_id,
                    'type': 'game', **properties}
        markdown = _render(metadata, body)
        return {'id': note_id, 'path': 'works/' + note_id + '.md', 'properties': metadata,
                'markdown': markdown, 'sha256': _hash(markdown)}

    def seed(self, *notes):
        records = []
        for note in notes:
            review = note['path'] if has_review_body(note['markdown']) else None
            self.state['work_catalog']['rows'][note['id']] = catalog_entry(note, review)
            self.state['notes'][note['id']] = {'path': review or backing_path(self.state, note['id']),
                                              'history': [], 'kind': 'work', 'deleted': False}
            records.append(row_from_note(note, review))
            if review:
                self.library._atomic(review, review_markdown(note))
        self.library._atomic(self.state['work_catalog']['path'], build_catalog(records))

    def catalog(self):
        return parse_catalog(_read(self.library._path(self.state['work_catalog']['path'])))

    def native_edit(self, edit):
        catalog = self.catalog()
        edit(catalog.document)
        self.library._atomic(self.state['work_catalog']['path'], build_catalog(catalog.document['records'], catalog))

    def scan(self):
        return scan_overlay(self.library, self.state, {})

    def replay(self, writes, removes):
        operations = [{'path': path, 'markdown': markdown, 'expected': expected}
                      for path, markdown, expected in writes]
        operations.extend({'path': path, 'markdown': None, 'expected': expected} for path, expected in removes)
        transaction = {'state': self.state, 'operations': operations}
        self.library._atomic('.markdown-library/transaction.json', json.dumps(transaction, ensure_ascii=False))
        self.library._replay(transaction)

    def replace(self, current, **changes):
        properties, body = _split(current['markdown'])
        properties.update(changes)
        markdown = _render(properties, body)
        operations = expand_operations(self.library, self.state, [(current['path'], markdown, current['sha256'])])
        self.replay(*operations)

    def test_opt_in_only_and_invalid_config_fails_closed(self):
        self.assertFalse(enabled({'notes': {}}))
        writes = [('a.md', 'body', None)]
        self.assertEqual(expand_operations(self.library, {}, writes), (writes, []))
        with self.assertRaises(LibraryError):
            enabled({'work_catalog': {}})

    def test_parser_rejects_corruption_duplicate_keys_duplicate_ids_and_schema(self):
        self.seed(self.note())
        catalog = self.catalog()
        source = build_catalog(catalog.document['records'], catalog)
        bad = [source.replace('"schemaVersion": 1', '"schemaVersion": 2'),
               source.replace('"schemaVersion": 1', '"schemaVersion": 1, "schemaVersion": 1'),
               source.replace('"rating": null', '"rating": NaN'),
               source + source, source.replace('```\n', '', 1)]
        for markdown in bad:
            with self.subTest(markdown=markdown[:30]), self.assertRaises(LibraryError):
                parse_catalog(markdown)
        catalog.document['records'].append(deepcopy(catalog.document['records'][0]))
        with self.assertRaises(LibraryError):
            build_catalog(catalog.document['records'], catalog)

    def test_zero_null_low_values_and_multiline_short_review_are_lossless(self):
        self.seed(self.note(rating=0, note='first line\nsecond | line\n```code```'), self.note('work-b', rating=None))
        found = self.scan()
        self.assertEqual(found['work-a']['properties']['rating'], 0)
        self.assertIsNone(found['work-b']['properties']['rating'])
        self.assertEqual(found['work-a']['properties']['note'], 'first line\nsecond | line\n```code```')
        self.replace(found['work-a'], rating=5)
        self.assertEqual(self.scan()['work-a']['properties']['rating'], 5)
        for value in (-1, 101, True, '85', float('inf')):
            with self.subTest(value=value), self.assertRaises(LibraryError):
                row_from_note(self.note(rating=value))

    def test_extra_experiences_and_arbitrary_metadata_survive_flat_date_edit(self):
        experiences = [{'id': 'first', 'startedOn': '2025-03', 'endedOn': '2025-04-01',
                        'platform': 'PC', 'note': 'multiline\nexperience', 'extra': {'unknown': 1}},
                       {'id': 'revisit', 'startedOn': '2026', 'progress': 0, 'note': 'second'}]
        self.seed(self.note(experiences=experiences, custom={'nested': ['kept']}))
        self.native_edit(lambda doc: doc['records'][0]['values'].update(endedOn='2025-04-02'))
        current = self.scan()['work-a']
        expected = deepcopy(experiences)
        expected[0]['endedOn'] = '2025-04-02'
        self.assertEqual(current['properties']['experiences'], expected)
        self.replace(current, rating=0)
        self.assertEqual(self.scan()['work-a']['properties']['experiences'], expected)
        self.assertEqual(self.scan()['work-a']['properties']['custom'], {'nested': ['kept']})

    def test_absent_properties_and_explicit_null_experiences_do_not_get_invented(self):
        self.seed(self.note(), self.note('work-b', experiences=None))
        found = self.scan()
        self.assertNotIn('rating', found['work-a']['properties'])
        self.assertNotIn('experiences', found['work-a']['properties'])
        self.assertIsNone(found['work-b']['properties']['experiences'])

    def test_view_layout_unknown_fields_and_surrounding_markdown_survive_write(self):
        self.seed(self.note())
        catalog = self.catalog()
        catalog.prefix = 'Introduction\r\n\r\n'
        catalog.suffix = '\r\nTail\r\n'
        catalog.opening = '```super-table\r\n'
        catalog.document['fields'][0].update(width=287, align='left', future={'x': 1})
        catalog.document['fields'].append({'id': 'user', 'name': '自定义', 'type': 'text'})
        catalog.document['views'][0].update(visibleFieldIds=['title', 'rating'], unknown=True)
        catalog.document['future'] = {'preserve': [1, 2]}
        catalog.document['records'][0]['values']['user'] = 'my text'
        catalog.document['records'][0]['future'] = 'row extra'
        original = deepcopy(catalog.document)
        self.library._atomic(self.state['work_catalog']['path'], build_catalog(catalog.document['records'], catalog))
        self.replace(self.scan()['work-a'], rating=91)
        result = self.catalog()
        self.assertEqual(result.prefix, catalog.prefix)
        self.assertEqual(result.suffix, catalog.suffix)
        self.assertEqual(result.document['fields'], original['fields'])
        self.assertEqual(result.document['views'], original['views'])
        self.assertEqual(result.document['future'], original['future'])
        self.assertEqual(result.document['records'][0]['values']['user'], 'my text')
        self.assertEqual(result.document['records'][0]['future'], 'row extra')

    def test_hashes_are_per_work_and_row_conflict_fails_closed(self):
        self.seed(self.note(rating=0), self.note('work-b', rating=50))
        old = self.scan()
        self.native_edit(lambda doc: doc['records'][1]['values'].update(rating=80))
        new = self.scan()
        self.assertEqual(old['work-a']['sha256'], new['work-a']['sha256'])
        self.assertNotEqual(old['work-b']['sha256'], new['work-b']['sha256'])
        # Reusing an unaffected row revision preserves the other native row edit.
        self.replace(old['work-a'], rating=5)
        self.assertEqual(self.scan()['work-b']['properties']['rating'], 80)
        with self.assertRaises(LibraryError):
            self.replace(old['work-b'], rating=20)

    def test_grouped_row_edits_emit_one_catalog_compare_and_swap(self):
        self.seed(self.note(), self.note('work-b'))
        notes = self.scan()
        writes = []
        for note in notes.values():
            properties, body = _split(note['markdown'])
            properties['rating'] = 0
            writes.append((note['path'], _render(properties, body), note['sha256']))
        expanded, removes = expand_operations(self.library, self.state, writes)
        self.assertEqual(len(expanded), 1)
        self.assertEqual(removes, [])
        self.assertEqual(expanded[0][0], self.state['work_catalog']['path'])
        self.native_edit(lambda doc: doc['views'][0].update(name='Native view change'))
        with self.assertRaises(LibraryError):
            self.replay(expanded, removes)
        self.assertEqual(self.catalog().document['views'][0]['name'], 'Native view change')

    def test_assert_storage_catches_table_or_review_change(self):
        self.seed(self.note(body='A detailed personal review\n'))
        old = self.scan()['work-a']
        assert_storage(self.library, old)
        self.library._atomic(old['storage_path'], _read(self.library._path(old['storage_path'])) + 'Typing')
        with self.assertRaises(LibraryError):
            assert_storage(self.library, old)

    def test_body_update_writes_only_linked_minimal_review_and_table(self):
        self.seed(self.note(rating=0))
        note = self.scan()['work-a']
        updated = note['markdown'] + 'Detailed first person review.\n'
        writes, removes = expand_operations(self.library, self.state, [(note['path'], updated, note['sha256'])])
        self.assertEqual(len(writes), 2)
        self.replay(writes, removes)
        current = self.scan()['work-a']
        self.assertIn('Detailed first person review.', current['markdown'])
        self.assertEqual(current['properties']['rating'], 0)
        physical_props, _ = _split(_read(self.library._path(current['storage_path'])))
        self.assertNotIn('rating', physical_props)
        self.assertNotIn('type', physical_props)
        self.assertEqual(physical_props['id'], 'work-a')
        self.assertEqual(len(list(self.library.vault.glob('works/**/*.md'))), 2)

    def test_program_navigation_only_stays_in_table_without_review_files(self):
        body = ('# A work\n\n<!-- markdown-library:publication-nav:start -->\n'
                '[[发表稿/review.md|文章]]\n<!-- markdown-library:publication-nav:end -->\n')
        self.assertFalse(has_review_body(body))
        self.seed(self.note(body=body, note='A short impression'))
        note = self.scan()['work-a']
        self.assertEqual(_split(note['markdown'])[1], body)
        self.replace(note, rating=0)
        self.assertEqual(len(list(self.library.vault.glob('works/**/*.md'))), 1)

    def test_missing_row_or_review_never_becomes_deletion(self):
        self.seed(self.note(body='Personal review'))
        self.library._path('works/work-a.md').unlink()
        with self.assertRaisesRegex(LibraryError, 'missing'):
            self.scan()
        self.native_edit(lambda doc: doc.update(records=[]))
        with self.assertRaisesRegex(LibraryError, 'absence is not a deletion'):
            self.scan()
        self.assertFalse(self.state['notes']['work-a']['deleted'])

    def test_explicit_lifecycle_removes_row_and_review_but_keeps_unrelated_work(self):
        self.seed(self.note(body='Personal review'), self.note('work-b'))
        current = self.scan()['work-a']
        self.state['notes']['work-a']['deleted'] = True
        writes, removes = expand_operations(self.library, self.state, removes=[(current['path'], current['sha256'])])
        self.replay(writes, removes)
        self.assertEqual([row['id'] for row in self.catalog().document['records']], ['work-b'])
        self.assertFalse(self.library._path(current['path']).exists())

    def test_row_removal_without_explicit_lifecycle_is_refused(self):
        self.seed(self.note())
        current = self.scan()['work-a']
        with self.assertRaisesRegex(LibraryError, 'explicit lifecycle'):
            expand_operations(self.library, self.state, removes=[(current['path'], current['sha256'])])

    def test_native_review_rename_follows_identity_and_next_edit_rewrites_link(self):
        self.seed(self.note(body='Personal review'))
        old = self.library._path('works/work-a.md')
        new = self.library._path('works/renamed.md')
        old.rename(new)
        markdown = _read(new)
        physical = {'id': 'work-a', 'path': 'works/renamed.md', 'markdown': markdown,
                    'properties': _split(markdown)[0], 'sha256': _hash(markdown)}
        found = scan_overlay(self.library, self.state, {'work-a': physical})
        self.replace(found['work-a'], rating=0)
        self.assertEqual(self.catalog().document['records'][0]['values']['review'], '[[works/renamed.md]]')
        self.assertEqual(self.scan()['work-a']['path'], 'works/renamed.md')

    def test_new_native_rows_get_identity_and_no_hidden_markdown_copy(self):
        self.seed(self.note())
        catalog = self.catalog()
        catalog.document['records'].append(row_from_note(self.note('native-new', rating=0)))
        self.library._atomic(self.state['work_catalog']['path'], build_catalog(catalog.document['records'], catalog))
        found = self.scan()
        self.assertEqual(found['native-new']['properties']['rating'], 0)
        self.assertEqual(found['native-new']['path'], 'works/作品总表.md#work-native-new')
        self.assertEqual(len(list(self.library.vault.glob('works/**/*.md'))), 1)

    def test_new_logical_work_can_be_added_in_same_transaction(self):
        self.seed(self.note())
        created = self.note('new-work', rating=0)
        self.state['work_catalog']['rows'][created['id']] = catalog_entry(created)
        self.state['notes'][created['id']] = {'path': backing_path(self.state, created['id']), 'kind': 'work'}
        operations = expand_operations(self.library, self.state,
                                       [(backing_path(self.state, created['id']), created['markdown'], None)])
        self.replay(*operations)
        self.assertEqual(self.scan()['new-work']['properties']['rating'], 0)

    def test_dates_and_cell_types_fail_closed(self):
        for changes in ({'type': []}, {'status': {}}, {'note': 3},
                        {'experiences': [{'startedOn': '2026-02-30'}]}):
            with self.subTest(changes=changes), self.assertRaises(LibraryError):
                row_from_note(self.note(**changes))

    def test_core_create_replace_and_delete_use_catalog_storage(self):
        self.seed(self.note(rating=0))
        self.library._save(self.state)
        before = self.library.get('work-a')
        self.library.replace('work-a', before['markdown'].replace('rating: 0', 'rating: 5'),
                             expected_hash=before['sha256'])
        current = self.library.get('work-a')
        self.assertEqual(current['properties']['rating'], 5)
        created = self.library.create(title='A new work', kind='work', properties={'type': 'game', 'rating': 0})
        self.assertTrue(self.library.get(created['id'])['catalog_backed'])
        self.library.delete(current['id'], expected_hash=current['sha256'])
        self.assertNotIn('work-a', {note['id'] for note in self.library.list_notes()})
        self.assertEqual(len(list(self.library.vault.glob('works/**/*.md'))), 1)

    def test_unknown_physical_work_cannot_bypass_catalog(self):
        self.seed(self.note())
        orphan = self.note('orphan', body='Personal body')
        self.library._atomic(orphan['path'], review_markdown(orphan))
        with self.assertRaisesRegex(LibraryError, 'no catalog row'):
            scan_overlay(self.library, self.state, {'orphan': orphan})

    def test_core_lifecycle_recovers_rows_and_reviews_without_reselecting_publication(self):
        self.seed(self.note(), self.note('work-b', body='A full personal review', rating=0))
        self.library._save(self.state)
        for note_id in ('work-a', 'work-b'):
            note = self.library.get(note_id)
            self.library.delete(note_id, expected_hash=note['sha256'])
            self.library.recover(note_id, expected_hash=note['sha256'])
            recovered = self.library.get(note_id)
            self.assertEqual(recovered['sha256'], note['sha256'])
        self.assertEqual(self.library.publications(), [])

    def test_review_edit_after_operation_expansion_cannot_be_overwritten(self):
        self.seed(self.note(body='Existing personal review', rating=0))
        note = self.scan()['work-a']
        changed = note['markdown'].replace('rating: 0', 'rating: 5')
        writes, removes = expand_operations(self.library, self.state,
                                            [(note['path'], changed, note['sha256'])])
        physical = self.library._path(note['storage_path'])
        native = _read(physical) + '\nTyping concurrently\n'
        self.library._atomic(note['storage_path'], native)
        with self.assertRaises(LibraryError):
            self.replay(writes, removes)
        self.assertEqual(_read(physical), native)

    def test_native_empty_number_projects_null_and_preserves_raw_table_until_edited(self):
        self.seed(self.note(rating=85), self.note('work-b', rating=0))
        self.native_edit(lambda doc: doc['records'][0]['values'].update(rating=''))
        raw = _read(self.library._path(self.state['work_catalog']['path']))
        notes = self.scan()
        self.assertIsNone(notes['work-a']['properties']['rating'])
        self.assertEqual(notes['work-b']['properties']['rating'], 0)
        self.assertEqual(_read(self.library._path(self.state['work_catalog']['path'])), raw)
        self.replace(notes['work-b'], note='Unrelated Work edit')
        self.assertEqual(self.catalog().document['records'][0]['values']['rating'], '')
        self.replace(self.scan()['work-a'], note='This row now edited')
        self.assertIsNone(self.catalog().document['records'][0]['values']['rating'])

    def test_native_empty_number_scans_and_syncs_as_a_cleared_rating(self):
        from test_catalog_sync import CatalogSyncTests
        fixture = CatalogSyncTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.edit_row(rating='')
        current = fixture.library.get(fixture.work_id)
        self.assertIsNone(current['properties']['rating'])
        receipt = fixture.sync()
        self.assertIn('observed', [row['status'] for row in receipt['results']])
        self.assertIsNone(fixture.client.base['works'][fixture.remote_id]['rating'])
        self.assertIsNone(fixture.library.get(fixture.work_id)['properties']['rating'])
        self.assertEqual(fixture.sync()['pending_ids'], [])

    def test_native_blank_new_row_stays_pending_while_existing_work_is_editable(self):
        self.seed(self.note(rating=0))
        pending = row_from_note(self.note('native-pending'))
        pending['values'] = {key: '' for key in pending['values']}
        self.native_edit(lambda doc: doc['records'].append(deepcopy(pending)))
        self.library._save(self.state)
        from markdown_library.bootstrap import _index
        before = self.library._read_state()
        readonly_state, readonly_notes = _index(self.library)
        self.assertNotIn('native-pending', readonly_notes)
        self.assertNotIn('native-pending', readonly_state['work_catalog']['rows'])
        self.assertEqual(self.library._read_state(), before)
        current = self.library.get('work-a')
        self.assertEqual(current['properties']['rating'], 0)
        state = self.library._read_state()
        self.assertNotIn('native-pending', state['notes'])
        self.assertNotIn('native-pending', state['work_catalog']['rows'])
        self.assertNotIn('native-pending', {note['id'] for note in self.library.list_notes()})
        self.library.replace(current['id'], current['markdown'].replace('rating: 0', 'rating: 5'),
                             expected_hash=current['sha256'])
        catalog = parse_catalog(_read(self.library._path(state['work_catalog']['path'])))
        self.assertEqual(next(row for row in catalog.document['records']
                              if row['id'] == 'native-pending'), pending)
        self.assertEqual(self.library.publications(), [])

    def test_native_pending_row_finalizes_with_its_original_stable_id(self):
        self.seed(self.note())
        pending = row_from_note(self.note('native-pending'))
        pending['values']['title'] = ''
        self.native_edit(lambda doc: doc['records'].append(pending))
        self.library._save(self.state)
        self.library.get('work-a')
        self.native_edit(lambda doc: doc['records'][1]['values'].update(title='My new work'))
        from markdown_library.bootstrap import _index
        before = self.library._read_state()
        readonly_state, readonly_notes = _index(self.library)
        self.assertEqual(readonly_notes['native-pending']['properties']['title'], 'My new work')
        self.assertNotIn('native-pending', readonly_state['notes'])
        self.assertEqual(self.library._read_state(), before)
        current = self.library.get('native-pending')
        self.assertEqual(current['id'], 'native-pending')
        self.assertEqual(current['properties']['title'], 'My new work')
        self.assertIn('native-pending', self.library._read_state()['work_catalog']['rows'])
        self.assertEqual(self.library.publications(), [])

    def test_cleared_known_title_and_missing_new_row_cells_fail_closed(self):
        self.seed(self.note())
        self.library._save(self.state)
        self.native_edit(lambda doc: doc['records'][0]['values'].update(title=''))
        with self.assertRaises(LibraryError):
            self.library.get('work-a')
        with self.assertRaisesRegex(LibraryError, 'title must remain nonempty'):
            expand_operations(self.library, self.state, [('unrelated.md', 'A note', None)])
        catalog = self.catalog()
        partial = {'id': 'native-pending', 'values': {'title': ''}}
        with self.assertRaisesRegex(LibraryError, 'missing required cells'):
            build_catalog([*catalog.document['records'], partial], catalog)

    def test_pending_blank_row_does_not_publish_or_delete_during_sync(self):
        from test_catalog_sync import CatalogSyncTests
        fixture = CatalogSyncTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        destination = fixture.library._path(fixture.library._read_state()['work_catalog']['path'])
        catalog = parse_catalog(_read(destination))
        pending = row_from_note(self.note('native-pending'))
        pending['values']['title'] = ''
        destination.write_text(build_catalog([*catalog.document['records'], pending], catalog), encoding='utf-8')
        remote = deepcopy(fixture.client.base)
        receipt = fixture.sync()
        self.assertEqual(receipt['pending_ids'], [])
        self.assertEqual(fixture.client.mutations, [])
        self.assertEqual(fixture.client.base, remote)
        self.assertNotIn('native-pending', fixture.library._read_state()['notes'])


if __name__ == '__main__':
    unittest.main()
