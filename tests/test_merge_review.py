from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.core import LibraryError, _render, _split
from markdown_library.review import review_sync, resolve_sync
from markdown_library.sync import export_publication, observed_baseline
from markdown_library.transport import _Store, TransportError, sync_once
from test_transport import FakePayload


class MergeReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Library.initialize(Path(temporary.name) / 'vault')
        self.client = FakePayload()

    def setup_note(self, kind='work', **properties):
        defaults = {'slug': 'work', 'type': 'book', 'note': 'base note', 'rating': 3} if kind == 'work' else {'link': 'post', 'locale': 'en'}
        note = self.library.create('PRIVATE WORK BODY' if kind == 'work' else 'Base public body',
                                   title='Base title', kind=kind, properties={**defaults, **properties})
        publication = self.library.publish(note['id'], expected_hash=note['sha256'])
        export = export_publication(publication)
        row = self.client.seed(export['collection'], export['fields'])
        store = self.store()
        state = store.load()
        state['baselines'][note['id']] = observed_baseline(publication, row)
        store.save(state)
        return note, publication

    def store(self):
        return _Store(self.library, self.client.target)

    def edit(self, document, *, body=None, **properties):
        props, old_body = _split(document['markdown'])
        props.update(properties)
        return self.library.replace(document['id'], _render(props, old_body if body is None else body), expected_hash=document['sha256'])

    def remote(self, collection='works', **fields):
        self.client.latest[collection]['70'].update({**fields, 'updatedAt': self.client.stamp()})

    def accept(self):
        return sync_once(self.library, self.client, apply=True, accept_remote=True, works_drafts_and_versions=True)

    def item(self, note, include_values=False):
        return review_sync(self.library, self.client, [note['id']], include_values=include_values)['items'][0]

    def resolve(self, note, choices, **overrides):
        item = self.item(note)
        args = {'expected_hash': item['current']['sha256'], 'expected_remote_version': item['payload_latest']['version'],
                'expected_review_token': item['review_token'], 'apply': True, **overrides}
        return resolve_sync(self.library, self.client, note['id'], choices, **args)

    def files(self):
        return {str(p.relative_to(self.library.vault)): p.read_bytes() for p in self.library.vault.rglob('*') if p.is_file() and p.name != 'sync.lock'}

    def test_disjoint_work_fields_and_private_body_merge_without_publication(self):
        note, publication = self.setup_note()
        baseline = deepcopy(self.store().load()['baselines'])
        self.edit(note, body='NEW PRIVATE BODY WITH [[links]]', rating=5, private_property='keep')
        self.remote(note='remote note')
        result = self.accept()
        self.assertEqual(result['results'][0]['status'], 'remote-draft-imported')
        current = self.library.get(note['id'])
        self.assertEqual(current['properties']['rating'], 5)
        self.assertEqual(current['properties']['note'], 'remote note')
        self.assertEqual(current['properties']['private_property'], 'keep')
        self.assertEqual(_split(current['markdown'])[1], 'NEW PRIVATE BODY WITH [[links]]')
        self.assertEqual(self.library.publications(), [publication])
        self.assertEqual(self.store().load()['baselines'], baseline)
        self.assertEqual(self.client.mutations, [])
        self.assertNotIn('PRIVATE BODY', str(result))
        self.assertNotIn('PRIVATE BODY', str(self.store().load()['reverse_imports']))

    def test_post_local_body_and_remote_title_merge(self):
        note, selected = self.setup_note('post')
        self.edit(note, body='User edited public draft')
        self.remote('posts', title='Admin edited title')
        self.assertEqual(self.accept()['results'][0]['status'], 'remote-draft-imported')
        current = self.library.get(note['id'])
        self.assertEqual(current['properties']['title'], 'Admin edited title')
        self.assertEqual(_split(current['markdown'])[1], 'User edited public draft')
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.client.mutations, [])

    def test_same_field_conflict_has_presence_and_base_local_remote(self):
        note, _ = self.setup_note()
        edited = self.edit(note, note='local note')
        self.remote(note='remote note', description='safe independent field')
        self.assertEqual(self.accept()['results'][0]['status'], 'conflict')
        item = self.item(note, True)
        conflict = item['merge']['conflicts'][0]
        self.assertEqual(conflict['field'], 'note')
        self.assertEqual([conflict[side]['value'] for side in ('base', 'local', 'remote')], ['base note', 'local note', 'remote note'])
        self.assertEqual(self.library.get(note['id'])['sha256'], edited['sha256'])
        self.assertEqual(self.resolve(note, {'note': 'local'})['status'], 'remote-draft-imported')
        current = self.library.get(note['id'])
        self.assertEqual(current['properties']['note'], 'local note')
        self.assertEqual(current['properties']['description'], 'safe independent field')
        self.assertEqual(self.accept()['results'][0]['status'], 'unchanged')
        self.assertEqual(self.client.mutations, [])

    def test_review_and_resolution_preview_are_strictly_read_only_and_redacted(self):
        note, _ = self.setup_note('post')
        self.edit(note, body='PRIVATE LOCAL REVIEW')
        self.remote('posts', markdown='PRIVATE REMOTE REVIEW')
        before = self.files()
        item = self.item(note)
        self.assertNotIn('PRIVATE LOCAL REVIEW', str(item))
        self.assertNotIn('PRIVATE REMOTE REVIEW', str(item))
        self.assertEqual(item['payload_formal']['state'], 'published')
        result = self.resolve(note, {'markdown': 'remote'}, apply=False)
        self.assertEqual(result['status'], 'resolution-available')
        self.assertEqual(self.files(), before)
        self.assertEqual(self.client.mutations, [])

    def test_remote_local_and_explicit_value_resolution_remain_unpublished(self):
        for choice, expected in [('remote', 'remote note'), ('base', 'base note'), ({'present': True, 'value': 'user combined'}, 'user combined')]:
            with self.subTest(choice=choice):
                self.setUp()
                note, selected = self.setup_note()
                self.edit(note, note='local note')
                self.remote(note='remote note')
                self.resolve(note, {'note': choice})
                self.assertEqual(self.library.get(note['id'])['properties']['note'], expected)
                self.assertEqual(self.library.publications(), [selected])
                self.assertEqual(self.client.mutations, [])

    def test_missing_vs_null_is_not_equal_and_explicit_absence_is_supported(self):
        note, _ = self.setup_note(description='base')
        self.edit(note, description=None)
        self.client.latest['works']['70'].pop('description')
        self.remote(note='remote note')
        conflict = next(c for c in self.item(note, True)['merge']['conflicts'] if c['field'] == 'description')
        self.assertEqual(conflict['local'], {'present': True, 'value': None})
        self.assertEqual(conflict['remote'], {'present': False})
        self.resolve(note, {'description': {'present': False}})
        self.assertNotIn('description', self.library.get(note['id'])['properties'])

    def test_stale_current_remote_formal_selection_or_baseline_cannot_resolve(self):
        for mode in ('current', 'remote', 'formal', 'selection', 'baseline', 'same-version-content'):
            with self.subTest(mode=mode):
                self.setUp()
                note, _ = self.setup_note()
                edited = self.edit(note, note='local')
                self.remote(note='remote')
                item = self.item(note)
                if mode == 'current': self.edit(edited, rating=9)
                if mode == 'remote': self.remote(note='newer')
                if mode == 'same-version-content': self.client.latest['works']['70']['note'] = 'version not bumped'
                if mode == 'formal': self.client.base['works']['70']['title'] = 'formal changed'
                if mode == 'selection': self.library.publish(note['id'], expected_hash=edited['sha256'])
                if mode == 'baseline':
                    state = self.store().load()
                    state['baselines'][note['id']]['remote_version'] = 'changed'
                    self.store().save(state)
                before = self.files()
                with self.assertRaises(TransportError):
                    resolve_sync(self.library, self.client, note['id'], {'note': 'remote'}, expected_hash=item['current']['sha256'],
                                 expected_remote_version=item['payload_latest']['version'], expected_review_token=item['review_token'], apply=True)
                self.assertEqual(self.files(), before)
                self.assertEqual(self.client.mutations, [])

    def test_resolve_requires_exact_fields_and_pending_is_never_erased(self):
        note, _ = self.setup_note()
        self.edit(note, note='local')
        self.remote(note='remote')
        for choices in ({}, {'note': 'remote', 'rating': 'local'}):
            with self.assertRaises(TransportError): self.resolve(note, choices)
        state = self.store().load()
        state['pending'][note['id']] = {'phase': 'uncertain'}
        self.store().save(state)
        with self.assertRaises(TransportError): self.resolve(note, {'note': 'remote'})
        self.assertEqual(self.store().load()['pending'], state['pending'])

    def test_arrays_relationships_and_protection_require_review(self):
        for kind, changes in [('work', {'tags': ['new']}), ('work', {'article': 999}),
                              ('work', {'experiences': [{'id': 'new', 'note': 'row'}]}),
                              ('post', {'frontmatter': {'password': 'SECRET'}}),
                              ('post', {'protectedContent': True}), ('post', {'markdown': ':::encrypted\nSECRET\n:::'})]:
            with self.subTest(changes=changes):
                self.setUp()
                note, _ = self.setup_note(kind)
                self.remote('works' if kind == 'work' else 'posts', **changes)
                outcome = self.accept()
                self.assertEqual(outcome['results'][0]['status'], 'conflict')
                self.assertNotIn('SECRET', str(outcome))
                item = self.item(note)
                self.assertEqual(item['merge']['conflicts'][0]['reason'], 'structured-or-protected-field-review')
                self.assertNotIn('SECRET', str(item))
                self.assertEqual(self.library.get(note['id'])['sha256'], note['sha256'])

    def test_formatting_only_change_does_not_block_semantic_merge(self):
        note, selected = self.setup_note()
        path = self.library.vault / note['path']
        path.write_text(note['markdown'].replace('title: Base title', "title: 'Base title'"))
        self.remote(note='Admin scalar')
        self.assertEqual(self.accept()['results'][0]['status'], 'remote-draft-imported')
        self.assertEqual(self.library.get(note['id'])['properties']['note'], 'Admin scalar')
        self.assertEqual(self.library.publications(), [selected])

    def test_exact_legacy_hashes_recover_but_unknown_remote_defaults_do_not(self):
        for recoverable in (True, False):
            with self.subTest(recoverable=recoverable):
                self.setUp()
                note, _ = self.setup_note()
                state = self.store().load()
                base = state['baselines'][note['id']]
                base.pop('local_fields'); base.pop('remote_fields')
                if not recoverable: base['remote_hash'] = 'unrecoverable-server-defaults'
                self.store().save(state)
                self.remote(note='remote')
                item = self.item(note, True)
                self.assertEqual(item['merge']['base_available'], recoverable)
                if recoverable:
                    self.assertEqual(self.accept()['results'][0]['status'], 'remote-draft-imported')
                else:
                    self.assertEqual(item['merge']['conflicts'][0]['reason'], 'base-unavailable')
                    with self.assertRaises(TransportError):
                        self.resolve(note, {c['field']: 'base' for c in item['merge']['conflicts']})

    def test_stale_history_article_does_not_abort_exact_ancestor_search(self):
        for recoverable in (True, False):
            with self.subTest(recoverable=recoverable):
                self.setUp()
                note, selected = self.setup_note()
                properties, body = _split(note['markdown'])
                properties['article'] = '[[posts/Article-before-archival]]'
                retained = self.library.retain_revision(
                    note['id'], _render(properties, body), expected_hash=note['sha256'])
                state = self.store().load()
                baseline = state['baselines'][note['id']]
                baseline.pop('local_fields')
                baseline.pop('remote_fields')
                if not recoverable:
                    baseline['remote_hash'] = 'unrecoverable-server-defaults'
                self.store().save(state)

                item = self.item(note)
                self.assertEqual(item['merge']['base_available'], recoverable)
                self.assertEqual(self.library.publications(), [selected])
                self.assertIn(retained['sha256'],
                              {row['sha256'] for row in self.library.history(note['id'])})
                self.assertEqual(self.client.mutations, [])

    def test_missing_current_article_still_blocks_review(self):
        note, _ = self.setup_note()
        current = self.edit(note, article='[[posts/No-such-current-article]]')
        with self.assertRaises(LibraryError):
            self.item(current)

    def test_next_explicit_publish_uses_accepted_remote_and_clears_receipt(self):
        note, _ = self.setup_note('post')
        self.edit(note, body='User draft')
        self.remote('posts', title='Admin title', _status='draft')
        self.assertEqual(self.accept()['results'][0]['status'], 'remote-draft-imported')
        current = self.library.get(note['id'])
        self.library.publish(note['id'], expected_hash=current['sha256'])
        self.assertEqual(self.accept()['results'][0]['status'], 'observed')
        self.assertNotIn(note['id'], self.store().load()['reverse_imports'])
        self.assertEqual(self.accept()['results'][0]['status'], 'unchanged')
        self.assertEqual(self.client.base['posts']['70']['markdown'], 'User draft')
        self.assertEqual(self.client.base['posts']['70']['title'], 'Admin title')

    def test_later_disjoint_edits_compare_to_accepted_observation(self):
        note, _ = self.setup_note()
        self.remote(note='Admin first')
        self.accept()
        current = self.library.get(note['id'])
        self.edit(current, rating=8, body='User private continuation')
        self.remote(description='Admin second')
        self.assertEqual(self.accept()['results'][0]['status'], 'remote-draft-imported')
        current = self.library.get(note['id'])
        self.assertEqual(current['properties']['rating'], 8)
        self.assertEqual(current['properties']['note'], 'Admin first')
        self.assertEqual(current['properties']['description'], 'Admin second')
        self.assertEqual(_split(current['markdown'])[1], 'User private continuation')
        self.assertEqual(self.client.mutations, [])

    def test_bulk_review_reads_four_collections_and_reports_semantic_current_changes(self):
        note, _ = self.setup_note()
        self.edit(note, rating=9, body='Private new words')
        calls = []
        list_collection = self.client.list_collection
        def listing(collection, *, draft=True):
            calls.append((collection, draft))
            return list_collection(collection, draft=draft)
        self.client.list_collection = listing
        self.client.get = lambda *args, **kwargs: self.fail('Bulk review must use full collection snapshots')
        before = self.files()
        result = review_sync(self.library, self.client)
        item = result['items'][0]
        self.assertEqual(set(calls), {('works', True), ('works', False), ('posts', True), ('posts', False)})
        self.assertEqual(item['comparisons']['current_vs_selection'], ['rating'])
        self.assertEqual(item['merge']['local_changed_fields'], ['rating'])
        self.assertEqual(item['merge']['remote_changed_fields'], [])
        self.assertTrue(item['comparisons']['current_file_differs_selection'])
        self.assertEqual(self.files(), before)

    def test_deleted_remote_does_not_abort_whole_bulk_review(self):
        note, _ = self.setup_note()
        self.remote(deletedAt='deleted-time')
        result = review_sync(self.library, self.client)
        self.assertEqual(result['items'][0]['status'], 'deleted-review')
        self.assertEqual(self.client.mutations, [])

    def test_wikilink_current_article_is_semantically_same_as_selected_stable_id(self):
        post = self.library.create('Post body', title='Linked post', kind='post', properties={'link': 'linked', 'locale': 'en'})
        publication = self.library.publish(post['id'], expected_hash=post['sha256'])
        export = export_publication(publication)
        remote = self.client.seed('posts', export['fields'])
        state = self.store().load()
        state['baselines'][post['id']] = observed_baseline(publication, remote)
        self.store().save(state)
        work = self.library.create('Private [[other]] body', title='Work', kind='work',
                                   properties={'slug': 'linked-work', 'type': 'book', 'article': post['id']})
        selected = self.library.publish(work['id'], expected_hash=work['sha256'])
        from markdown_library.transport import _resolve_relationships
        resolved = _resolve_relationships(selected, self.store().load()['baselines'])
        work_export = export_publication(resolved)
        row = self.client.seed('works', work_export['fields'])
        state = self.store().load()
        state['baselines'][work['id']] = observed_baseline(resolved, row)
        self.store().save(state)
        from markdown_library.relations import display_article
        self.edit(work, article=display_article(self.library, post['id']))
        item = self.item(work)
        self.assertEqual(item['comparisons']['current_vs_selection'], [])
        self.client.latest['works'][str(row['id'])].update(note='Admin note', updatedAt=self.client.stamp())
        result = self.accept()
        self.assertIn('remote-draft-imported', [r['status'] for r in result['results']])
        current = self.library.get(work['id'])
        self.assertTrue(current['properties']['article'].startswith('[['))
        self.assertEqual(_split(current['markdown'])[1], 'Private [[other]] body')
        self.assertEqual(self.client.mutations, [])

    def test_unselected_current_is_reported_as_local_draft_even_when_publication_is_unchanged(self):
        note, selected = self.setup_note()
        current = self.edit(note, rating=8)
        result = sync_once(self.library, self.client)
        self.assertEqual(result['results'][0]['status'], 'local-draft')
        self.assertEqual(self.library.get(note['id'])['sha256'], current['sha256'])
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.client.mutations, [])

    def test_resolved_local_choice_is_not_overwritten_by_later_remote_same_field_edit(self):
        note, selected = self.setup_note()
        self.edit(note, note='User retained choice')
        self.remote(note='Admin first')
        self.resolve(note, {'note': 'local'})
        self.remote(note='Admin second')
        self.assertEqual(self.accept()['results'][0]['status'], 'conflict')
        item = self.item(note, True)
        conflict = next(row for row in item['merge']['conflicts'] if row['field'] == 'note')
        self.assertEqual(conflict['base']['value'], 'Admin first')
        self.assertEqual(conflict['local']['value'], 'User retained choice')
        self.assertEqual(conflict['remote']['value'], 'Admin second')
        self.assertEqual(self.library.get(note['id'])['properties']['note'], 'User retained choice')
        self.assertEqual(self.library.publications(), [selected])
        self.assertEqual(self.client.mutations, [])

    def test_selected_property_removal_is_reviewed_instead_of_false_convergence(self):
        note, _ = self.setup_note()
        baseline = deepcopy(self.store().load()['baselines'])
        properties, body = _split(note['markdown'])
        properties.pop('rating')
        current = self.library.replace(note['id'], _render(properties, body), expected_hash=note['sha256'])
        self.library.publish(note['id'], expected_hash=current['sha256'])
        result = self.accept()
        self.assertEqual(result['results'][0]['status'], 'conflict')
        self.assertEqual(result['plan']['actions'][0]['reason'], 'property-removal-requires-review')
        self.assertEqual(result['plan']['actions'][0]['fields_requiring_review'], ['rating'])
        self.assertEqual(self.client.latest['works']['70']['rating'], 3)
        self.assertEqual(self.store().load()['baselines'], baseline)
        self.assertEqual(self.client.mutations, [])

    def test_explicit_null_property_clear_is_sent_and_independently_verified(self):
        note, _ = self.setup_note()
        current = self.edit(note, rating=None)
        self.library.publish(note['id'], expected_hash=current['sha256'])
        result = self.accept()
        self.assertEqual(result['results'][0]['status'], 'observed')
        self.assertIsNone(self.client.latest['works']['70']['rating'])
        self.assertIsNone(self.client.base['works']['70']['rating'])
        self.assertIsNone(self.store().load()['baselines'][note['id']]['local_fields']['rating'])

    def test_legacy_selected_property_removal_also_blocks_with_another_changed_field(self):
        note, _ = self.setup_note()
        state = self.store().load()
        state['baselines'][note['id']].pop('local_fields')
        state['baselines'][note['id']].pop('remote_fields')
        self.store().save(state)
        properties, body = _split(note['markdown'])
        properties.pop('rating')
        properties['title'] = 'New selected title'
        current = self.library.replace(note['id'], _render(properties, body), expected_hash=note['sha256'])
        self.library.publish(note['id'], expected_hash=current['sha256'])
        before = deepcopy(self.store().load())
        result = self.accept()
        self.assertEqual(result['results'][0]['status'], 'conflict')
        self.assertEqual(result['plan']['actions'][0]['reason'], 'property-removal-requires-review')
        self.assertEqual(self.store().load(), before)
        self.assertEqual(self.client.mutations, [])

    def test_formal_changed_during_resolution_is_rechecked_before_acceptance(self):
        for changed_version in (True, False):
            with self.subTest(changed_version=changed_version):
                self.setUp()
                note, _ = self.setup_note()
                self.edit(note, note='Local choice')
                self.remote(note='Remote choice')
                item = self.item(note)
                original_get = self.client.get
                formal_reads = []
                def racing_get(collection, remote_id, *, draft=True):
                    row = original_get(collection, remote_id, draft=draft)
                    if not draft:
                        formal_reads.append(row)
                        if len(formal_reads) == 1:
                            base = self.client.base[collection][str(remote_id)]
                            base['title'] = 'Formal changed during review'
                            if changed_version: base['updatedAt'] = self.client.stamp()
                    return row
                self.client.get = racing_get
                before = self.files()
                with self.assertRaises(TransportError):
                    resolve_sync(self.library, self.client, note['id'], {'note': 'remote'},
                                 expected_hash=item['current']['sha256'], expected_remote_version=item['payload_latest']['version'],
                                 expected_review_token=item['review_token'], apply=True)
                self.assertEqual(len(formal_reads), 2)
                self.assertEqual(self.files(), before)
                self.assertEqual(self.client.mutations, [])

    def test_accepted_selection_default_sync_receipt_does_not_retain_remote_draft_prose(self):
        note, _ = self.setup_note('post')
        self.remote('posts', markdown='PRIVATE_REMOTE_DRAFT_MARKER', _status='draft')
        first = self.accept()
        self.assertEqual(first['results'][0]['status'], 'remote-draft-imported')
        self.assertNotIn('PRIVATE_REMOTE_DRAFT_MARKER', str(first))
        for local_edit in (False, True):
            if local_edit:
                current = self.library.get(note['id'])
                self.edit(current, body='PRIVATE_REMOTE_DRAFT_MARKER plus PRIVATE_LOCAL_DRAFT_MARKER')
            result = self.accept()
            self.assertEqual(result['results'][0]['status'], 'local-draft' if local_edit else 'unchanged')
            action = result['plan']['actions'][0]
            self.assertNotIn('library_draft', action)
            self.assertNotIn('website_only_fields_requiring_review', action)
            self.assertNotIn('PRIVATE_REMOTE_DRAFT_MARKER', str(result))
            self.assertNotIn('PRIVATE_LOCAL_DRAFT_MARKER', str(result))
        self.assertEqual(self.client.mutations, [])

    def test_invalid_optional_lifecycle_state_is_rejected_without_mutation(self):
        self.setup_note()
        original = self.store().load()
        for key in ('lifecycle', 'lifecycle_pending'):
            state = deepcopy(original)
            state[key] = []
            self.store().save(state)
            before = self.files()
            with self.assertRaises(TransportError): self.store().load()
            self.assertEqual(self.files(), before)
