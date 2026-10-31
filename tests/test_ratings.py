from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_library.core import Library, LibraryError, _hash, _split
from markdown_library.bootstrap import BootstrapError, bootstrap
from markdown_library.merge import merge_fields, recover_base
from markdown_library.ratings import (canonical_revision, convert_markdown, convert_rating,
                                      migrate_ratings)
from markdown_library.relations import patch_properties
from markdown_library.sync import (_remote_hash, content_hash, export_publication,
                                   observed_baseline, observed_imported_baseline)
from markdown_library.transport import _Store


class RatingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library = Library.initialize(Path(self.temporary.name) / 'vault')

    def files(self):
        return {str(path.relative_to(self.library.vault)): path.read_bytes()
                for path in self.library.vault.rglob('*') if path.is_file()}

    def legacy(self):
        state = self.library._read_state()
        state.pop('rating_scale')
        for record in state['notes'].values():
            for row in record.get('history', []): row.pop('rating_scale', None)
        self.library._save(state)
        filename = self.library.private / 'sync-state.json'
        if filename.exists():
            value = json.loads(filename.read_text())
            value.pop('rating_scale', None)
            filename.write_text(json.dumps(value))

    def work(self, rating=8.5):
        return self.library.create('PRIVATE BODY\n', title='Game', kind='work',
                                   properties={'slug': 'game', 'type': 'game', 'rating': rating})

    def migrate(self):
        plan = migrate_ratings(self.library)
        return migrate_ratings(self.library, apply=True, expected_token=plan['expected_token'])

    def test_canonical_inputs_keep_low_scores_and_fractional_precision(self):
        for value in (0, 8.5, 85, 100, 85.25, None):
            note = self.work(value)
            self.assertEqual(note['properties']['rating'], value)
            self.assertEqual(self.library.publish(note['id'], expected_hash=note['sha256'])['snapshot']['properties']['rating'], value)
        for value in (-1, 100.1, True, '85', float('inf'), float('nan'), 10**1000):
            with self.subTest(value=str(value)), self.assertRaises(LibraryError):
                self.work(value)
        self.assertEqual(convert_rating(8.03, source_scale=10), 80.3)
        self.assertEqual(convert_rating(8.525, source_scale=10), 85.25)

    def test_scalar_conversion_preserves_all_other_bytes(self):
        raw = '\ufeff---\r\nkind: work\r\nrating: 8.5 # my score\r\ncustom: "verbatim"\r\n---\r\nBODY\r\n'
        self.assertEqual(convert_markdown(raw, source_scale=10), raw.replace('8.5', '85'))
        self.assertEqual(convert_markdown(raw, source_scale=100), raw)

    def test_preview_readonly_migration_idempotent_and_history_bytes_retained(self):
        note = self.work()
        selected = self.library.publish(note['id'], expected_hash=note['sha256'])
        edited = self.library.replace(note['id'], patch_properties(note['markdown'], {'rating': 9.125}), expected_hash=note['sha256'])
        self.legacy()
        before = self.files()
        plan = migrate_ratings(self.library)
        self.assertEqual(self.files(), before)
        self.assertEqual(plan['changed_current'], 1)
        result = migrate_ratings(self.library, apply=True, expected_token=plan['expected_token'])
        current = self.library.get(note['id'])
        self.assertEqual(current['properties']['rating'], 91.25)
        publication = self.library.publications()[0]
        self.assertEqual(publication['snapshot']['properties']['rating'], 85)
        self.assertNotEqual(publication['sha256'], current['sha256'])
        self.assertEqual(publication['published_at'], selected['published_at'])
        for path, raw in before.items():
            if '/objects/' in path: self.assertEqual(self.files()[path], raw)
        restored = self.library.restore(note['id'], note['sha256'], expected_hash=current['sha256'])
        self.assertEqual(restored['properties']['rating'], 85)
        self.assertEqual(self.library.history_markdown(note['id'], note['sha256']), note['markdown'])
        self.assertEqual(self.library.publications()[0], publication)
        self.assertTrue(migrate_ratings(self.library, apply=True)['already_migrated'])

    def test_expired_token_and_legacy_runtime_guard(self):
        note = self.work()
        self.legacy()
        with self.assertRaisesRegex(LibraryError, 'explicit migration'):
            self.library.get(note['id'])
        plan = migrate_ratings(self.library)
        filename = self.library.vault / note['path']
        filename.write_text(note['markdown'] + 'external edit')
        with self.assertRaisesRegex(LibraryError, 'preview expired'):
            migrate_ratings(self.library, apply=True, expected_token=plan['expected_token'])
        self.assertNotIn('rating_scale', self.library._read_state())

    def test_baseline_and_accepted_conflicts_transform_independently(self):
        note = self.work(8)
        publication = self.library.publish(note['id'], expected_hash=note['sha256'])
        remote = {'id': 1, 'updatedAt': 'version', **export_publication(publication)['fields']}
        baseline = observed_baseline(publication, remote)
        current = self.library.replace(note['id'], patch_properties(note['markdown'], {'rating': 9}), expected_hash=note['sha256'])
        accepted_fields = {**remote, 'rating': 7}
        from markdown_library.merge import local_fields, remote_fields
        accepted = {'local_fields': local_fields(current, 'works'), 'remote_fields': remote_fields(accepted_fields, 'works'),
                    'local_hash': current['sha256'], 'selection_hash': content_hash(publication), 'remote_version': 'version'}
        accepted['remote_hash'] = _remote_hash(accepted_fields, 'works')
        accepted['local_fields_hash'] = content_hash(accepted['local_fields'])
        store = _Store(self.library, 'https://example.test')
        sync = store.load()
        sync['baselines'][note['id']] = baseline
        sync['reverse_imports'][note['id']] = accepted
        store.save(sync)
        self.legacy()
        self.migrate()
        sync = store.load()
        baseline = sync['baselines'][note['id']]
        accepted = sync['reverse_imports'][note['id']]
        self.assertEqual(baseline['local_fields']['rating'], 80)
        self.assertEqual(baseline['remote_fields']['rating'], 80)
        self.assertEqual(accepted['local_fields']['rating'], 90)
        self.assertEqual(accepted['remote_fields']['rating'], 70)
        self.assertEqual(accepted['selection_hash'], content_hash(self.library.publications()[0]))
        self.assertEqual(accepted['local_hash'], self.library.get(note['id'])['sha256'])
        merge = merge_fields(baseline['local_fields'], baseline['remote_fields'], accepted['local_fields'], accepted['remote_fields'])
        self.assertEqual(merge['conflicts'][0]['field'], 'rating')

    def test_hash_only_baseline_recovers_exact_history_and_purged_stays_untouched(self):
        note = self.work(8.5)
        selected = self.library.publish(note['id'], expected_hash=note['sha256'])
        remote = {'id': 1, 'updatedAt': 'same-version', **export_publication(selected)['fields']}
        baseline = observed_baseline(selected, remote)
        baseline.pop('local_fields'); baseline.pop('remote_fields')
        store = _Store(self.library, 'https://example.test')
        sync = store.load(); sync['baselines'][note['id']] = baseline
        purged = {'collection': 'works', 'state': 'deleted', 'local_hash': 'opaque-old', 'remote_hash': 'opaque-old'}
        sync['baselines']['purged'] = deepcopy(purged); store.save(sync)
        state = self.library._read_state(); state['notes']['purged'] = {'purged': True, 'history': []}; self.library._save(state)
        self.legacy(); self.migrate()
        new_sync = store.load(); base = new_sync['baselines'][note['id']]
        self.assertEqual(new_sync['baselines']['purged'], purged)
        self.assertEqual(base['remote_version'], 'same-version')
        self.assertEqual(base['local_fields']['rating'], 85)
        self.assertEqual(base['remote_fields']['rating'], 85)
        base.pop('local_fields'); base.pop('remote_fields')
        left, right, reason = recover_base(self.library, note['id'], base, None, new_sync['baselines'])
        self.assertEqual(reason, 'exact-history-hashes')
        self.assertEqual(left['rating'], 85)

    def test_pending_blocks_and_unknown_hash_blocks_without_writes(self):
        note = self.work()
        selected = self.library.publish(note['id'], expected_hash=note['sha256'])
        remote = {'id': 1, 'updatedAt': 'version', **export_publication(selected)['fields']}
        base = observed_baseline(selected, remote)
        store = _Store(self.library, 'https://example.test'); sync = store.load()
        sync['baselines'][note['id']] = base; sync['pending']['x'] = {'status': 'uncertain'}; store.save(sync)
        self.legacy(); before = self.files()
        with self.assertRaisesRegex(LibraryError, 'pending'): migrate_ratings(self.library)
        self.assertEqual(self.files(), before)
        sync['pending'] = {}; base['local_hash'] = 'unknown'; base.pop('local_fields'); base.pop('remote_fields'); store.save(sync)
        before = self.files()
        with self.assertRaisesRegex(LibraryError, 'Cannot prove rating baseline'): migrate_ratings(self.library)
        self.assertEqual(self.files(), before)

    def test_soft_deleted_history_recovery_converts_score(self):
        note = self.work(8.5)
        self.library.delete(note['id'], expected_hash=note['sha256'])
        self.legacy(); self.migrate()
        restored = self.library.recover(note['id'], expected_hash=note['sha256'])
        self.assertEqual(restored['properties']['rating'], 85)

    def test_interrupted_migration_journal_replays_once(self):
        note = self.work()
        self.legacy(); plan = migrate_ratings(self.library)
        with patch.object(self.library, '_replay', side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                migrate_ratings(self.library, apply=True, expected_token=plan['expected_token'])
        current = self.library.get(note['id'])
        self.assertEqual(current['properties']['rating'], 85)
        self.assertTrue(migrate_ratings(self.library)['already_migrated'])

    def test_same_sha_with_different_nonzero_scales_requires_review(self):
        note = self.work(9)
        self.legacy(); self.migrate()
        current = self.library.get(note['id'])
        # A real 9/100 edit can exactly reuse the immutable old 9/10 bytes.
        reused = self.library.replace(note['id'], note['markdown'], expected_hash=current['sha256'])
        self.assertEqual(reused['sha256'], note['sha256'])
        with self.assertRaisesRegex(LibraryError, 'Ambiguous historical rating scale'):
            self.library.restore(note['id'], note['sha256'], expected_hash=reused['sha256'])
        self.assertEqual(self.library.get(note['id'])['properties']['rating'], 9)

    def test_offline_snapshot_requires_explicit_scale_both_variants(self):
        snapshot = {'target': 'https://example.test', 'posts': [], 'works': [
            {'id': 1, 'title': 'Game', 'slug': 'game', 'type': 'game', 'rating': 8.5, '_status': 'draft', 'visibility': 'private'}],
            'published': {'works': [{'id': 1, 'title': 'Game', 'slug': 'game', 'type': 'game', 'rating': 7.75, '_status': 'published', 'visibility': 'public'}]}}
        with self.assertRaisesRegex(BootstrapError, 'rating_scale'): bootstrap(self.library, snapshot=snapshot)
        snapshot['rating_scale'] = 10
        result = bootstrap(self.library, snapshot=snapshot, apply=True)
        work = self.library.get(result['results'][0]['id'])
        self.assertEqual(work['properties']['rating'], 85)
        self.assertEqual(snapshot['works'][0]['rating'], 8.5)
        revisions = self.library.history(work['id'])
        scores = [_split(self.library.history_markdown(work['id'], row['sha256']))[0].get('rating') for row in revisions]
        self.assertIn(77.5, scores)
        self.assertEqual(self.library.publications(), [])
