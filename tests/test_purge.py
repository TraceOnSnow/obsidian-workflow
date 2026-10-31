from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library, LibraryError
from markdown_library.bootstrap import bootstrap
from markdown_library.core import _purge_proof
from markdown_library.transport import _Store, map_imported, sync_once
from test_lifecycle import TrashPayload


class PurgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.library = Library.initialize(Path(temporary.name) / 'vault')
        self.client = TrashPayload()
        self.client.seed('works', 501, title='Private Work', slug='private-work', type='book',
                         visibility='private', note='Private deleted impression')
        result = bootstrap(self.library, client=self.client, apply=True)['results'][0]
        map_imported(self.library, self.client, [result['id']], apply=True)
        self.note = self.library.get(result['id'])
        self.source_key = result['source_id']
        self.store = _Store(self.library, self.client.target)

    def files(self):
        return {str(path.relative_to(self.library.vault)): path.read_bytes()
                for path in self.library.vault.rglob('*') if path.is_file()}

    def soft_delete(self):
        self.library.delete(self.note['id'], expected_hash=self.note['sha256'])
        result = sync_once(self.library, self.client, apply=True)
        self.assertEqual(result['results'][0]['status'], 'deleted-observed')

    def proof(self):
        baseline = self.store.load()['baselines'][self.note['id']]
        return {'target': self.client.target, 'collection': 'works', 'remote_id': '501',
                'previous_remote_version': baseline['remote_version'],
                'observed_at': '2026-10-25T00:00:00+00:00', 'latest_status': 404, 'formal_status': 404,
                'deletion_transport': 'payload-local-api', 'deletion_result': 'deleted',
                'deletion_receipt_sha256': hashlib.sha256(b'isolated SDK deletion receipt').hexdigest()}

    def remote_remove(self):
        for rows in (self.client.latest, self.client.base):
            rows['works'].pop('501')

    def purge(self, proof=None):
        proof = proof or self.proof()
        plan = self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof)
        result = self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof,
                                    expected_token=plan['purge_token'], apply=True)
        return plan, result

    def test_not_soft_deleted_refuses_without_writes(self):
        before = self.files()
        with self.assertRaisesRegex(LibraryError, 'already soft-deleted'):
            self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=self.proof())
        self.assertEqual(self.files(), before)

    def test_soft_delete_without_observed_sync_refuses(self):
        self.library.delete(self.note['id'], expected_hash=self.note['sha256'])
        before = self.files()
        with self.assertRaisesRegex(LibraryError, 'observed mapped soft-deletion'):
            self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=self.proof())
        self.assertEqual(self.files(), before)

    def test_missing_or_mismatched_remote_proof_refuses(self):
        self.soft_delete()
        original = self.proof()
        variants = [None, {**original, 'remote_id': '999'}, {**original, 'previous_remote_version': 'stale'},
                    {**original, 'formal_status': 200}, {**original, 'target': 'https://other.example'},
                    {**original, 'deletion_receipt_sha256': ''}]
        before = self.files()
        for proof in variants:
            with self.subTest(proof=proof), self.assertRaises(LibraryError):
                self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof)
        self.assertEqual(self.files(), before)

    def test_local_api_and_http_proof_are_not_conflated(self):
        self.soft_delete()
        local = self.proof()
        self.assertEqual(_purge_proof(local)['deletion_transport'], 'payload-local-api')
        with self.assertRaises(LibraryError):
            _purge_proof({**local, 'hard_delete_status': 200})
        http = {key: value for key, value in local.items() if key != 'deletion_result'}
        http.update(deletion_transport='http', hard_delete_status=204)
        self.assertEqual(_purge_proof(http)['hard_delete_status'], 204)

    def test_preview_is_read_only_and_apply_requires_token(self):
        self.soft_delete()
        proof = self.proof()
        before = self.files()
        plan = self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof)
        self.assertFalse(plan['applied'])
        self.assertIn(f'.markdown-library/trash/{self.note["id"]}.md', plan['remove_paths'])
        self.assertEqual(self.files(), before)
        with self.assertRaisesRegex(LibraryError, 'preview token'):
            self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof, apply=True)
        self.assertEqual(self.files(), before)

    def test_stale_token_and_wrong_hash_refuse(self):
        self.soft_delete()
        proof = self.proof()
        before = self.files()
        with self.assertRaisesRegex(LibraryError, 'preview is stale'):
            self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof,
                               expected_token='wrong', apply=True)
        with self.assertRaisesRegex(LibraryError, 'Revision conflict'):
            self.library.purge(self.note['id'], expected_hash='0' * 64, remote_proof=proof)
        self.assertEqual(self.files(), before)

    def test_purge_removes_content_but_preserves_identity_sources_and_real_observations(self):
        self.soft_delete()
        before_state = self.library._read_state()
        before_sync = self.store.load()
        self.remote_remove()
        plan, result = self.purge()
        self.assertTrue(result['applied'])
        after = self.library._read_state()
        record = after['notes'][self.note['id']]
        self.assertTrue(record['purged'])
        self.assertTrue(record['deleted'])
        self.assertEqual(record['history'], [])
        self.assertNotIn('trash', record)
        self.assertNotIn('path', record)
        self.assertEqual(after['sources'], before_state['sources'])
        self.assertNotIn(self.note['id'], after['publications'])
        for relative in plan['remove_paths']:
            self.assertFalse((self.library.vault / relative).exists(), relative)
        self.assertFalse((self.library.vault / self.note['path']).exists())
        baseline = self.store.load()['baselines'][self.note['id']]
        for key in ('identity', 'remote_id', 'remote_version', 'remote_hash', 'state'):
            self.assertEqual(baseline[key], before_sync['baselines'][self.note['id']][key])
        self.assertNotIn('local_fields', baseline)
        self.assertNotIn('remote_fields', baseline)
        self.assertEqual(self.store.load()['lifecycle'], before_sync['lifecycle'])
        self.assertNotIn('Private deleted impression', json.dumps(record))
        self.assertNotIn('Private deleted impression', json.dumps(baseline))

    def test_object_shared_by_other_note_is_preserved(self):
        other = self.library.create('Kept user content', title='Keep this')
        self.soft_delete()
        state = self.library._read_state()
        state['notes'][other['id']]['history'].append({'sha256': self.note['sha256'], 'action': 'shared-fixture'})
        self.library._save(state)
        other_before = deepcopy(state['notes'][other['id']])
        self.remote_remove()
        plan, _ = self.purge()
        self.assertIn(self.note['sha256'], plan['shared_objects_preserved'])
        shared = self.library.private / 'objects' / (self.note['sha256'] + '.md')
        self.assertTrue(shared.exists())
        self.assertEqual(self.library._read_state()['notes'][other['id']], other_before)
        self.assertEqual((self.library.vault / other['path']).read_text(), other['markdown'])

    def test_repeated_purge_is_noop_with_same_proof(self):
        self.soft_delete()
        proof = self.proof()
        self.remote_remove()
        plan, _ = self.purge(proof)
        before = self.files()
        preview = self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof)
        self.assertEqual(preview['action'], 'already-purged')
        result = self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof,
                                    expected_token=plan['purge_token'], apply=True)
        self.assertFalse(result['applied'])
        self.assertEqual(self.files(), before)

    def test_source_reimport_and_recovery_history_paths_cannot_rebuild(self):
        self.soft_delete()
        self.remote_remove()
        self.purge()
        with self.assertRaisesRegex(LibraryError, 'permanently deleted'):
            self.library.import_note('do not recreate', source_id=self.source_key, title='Resurrect', kind='work')
        self.assertEqual(self.library.list_notes(), [])
        for action in [lambda: self.library.get(self.note['id']),
                       lambda: self.library.recover(self.note['id'], expected_hash=self.note['sha256']),
                       lambda: self.library.history(self.note['id']),
                       lambda: self.library.restore(self.note['id'], self.note['sha256'], expected_hash=self.note['sha256'])]:
            with self.assertRaises(LibraryError):
                action()
        self.assertFalse((self.library.vault / self.note['path']).exists())

    def test_timer_accepts_absent_purged_mapping_without_creating_or_writing(self):
        self.soft_delete()
        self.remote_remove()
        self.purge()
        mutations = deepcopy(self.client.mutations)
        before = self.files()
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result['results'][0]['status'], 'purged')
        self.assertEqual(result['plan']['missing_local_ids'], [])
        self.assertEqual(result['plan']['unmapped_remote_records'], [])
        self.assertEqual(self.client.mutations, mutations)
        self.assertEqual(self.files(), before)

    def test_reappeared_remote_is_conflict_without_restoring_local(self):
        self.soft_delete()
        remote = deepcopy(self.client.latest['works']['501'])
        self.remote_remove()
        self.purge()
        self.client.latest['works']['501'] = remote
        self.client.base['works']['501'] = deepcopy(remote)
        mutations = deepcopy(self.client.mutations)
        result = sync_once(self.library, self.client, apply=True, accept_remote=True)
        self.assertEqual(result['results'][0]['status'], 'conflict')
        self.assertIn('reappeared', result['results'][0]['reason'])
        self.assertFalse((self.library.vault / self.note['path']).exists())
        self.assertEqual(self.client.mutations, mutations)

    def test_external_edit_to_history_object_is_preserved(self):
        self.soft_delete()
        path = self.library.private / 'objects' / (self.note['sha256'] + '.md')
        path.write_text('later user edit')
        before = self.files()
        with self.assertRaisesRegex(LibraryError, 'object was edited'):
            self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=self.proof())
        self.assertEqual(self.files(), before)

    def test_selected_snapshot_is_removed_only_for_purged_identity(self):
        self.library.publish(self.note['id'], expected_hash=self.note['sha256'])
        self.soft_delete()
        self.remote_remove()
        self.purge()
        self.assertNotIn(self.note['id'], self.library._read_state()['publications'])

    def test_new_shared_reference_makes_preview_stale(self):
        other = self.library.create('Keep', title='Other')
        self.soft_delete()
        proof = self.proof()
        plan = self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof)
        state = self.library._read_state()
        state['notes'][other['id']]['history'].append({'sha256': self.note['sha256'], 'action': 'new-reference'})
        self.library._save(state)
        before = self.files()
        with self.assertRaisesRegex(LibraryError, 'preview is stale'):
            self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof,
                               expected_token=plan['purge_token'], apply=True)
        self.assertEqual(self.files(), before)

    def test_pending_outcome_blocks_purge(self):
        self.soft_delete()
        state = self.store.load()
        state['lifecycle_pending'] = {self.note['id']: {'uncertain': True}}
        self.store.save(state)
        before = self.files()
        with self.assertRaisesRegex(LibraryError, 'observed mapped soft-deletion'):
            self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=self.proof())
        self.assertEqual(self.files(), before)

    def test_interrupted_commit_replays_to_purged_tombstone(self):
        self.soft_delete()
        proof = self.proof()
        self.remote_remove()
        plan = self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof)
        atomic = self.library._atomic
        failed = False
        def interrupted(relative, text):
            nonlocal failed
            if relative == '.markdown-library/state.json' and not failed:
                failed = True
                raise OSError('isolated simulated state commit failure')
            return atomic(relative, text)
        with patch.object(self.library, '_atomic', side_effect=interrupted):
            with self.assertRaisesRegex(LibraryError, 'simulated state commit failure'):
                self.library.purge(self.note['id'], expected_hash=self.note['sha256'], remote_proof=proof,
                                   expected_token=plan['purge_token'], apply=True)
        self.assertTrue((self.library.private / 'transaction.json').exists())
        with self.library._lock():
            self.assertTrue(self.library._load()['notes'][self.note['id']]['purged'])
        self.assertFalse((self.library.private / 'transaction.json').exists())
        self.assertEqual(sync_once(self.library, self.client, apply=True)['results'][0]['status'], 'purged')


if __name__ == '__main__':
    unittest.main()
