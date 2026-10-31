from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from markdown_library import Library, LibraryError
from markdown_library.bootstrap import bootstrap
from markdown_library.lifecycle import review_lifecycle, resolve_lifecycle, sync_lifecycle
from markdown_library.transport import _Store, UncertainWrite, map_imported
from test_imported_mapping import MappingPayload

class TrashPayload(MappingPayload):
    def __init__(self):
        super().__init__(); self.timeout_after_update = False
    def seed(self, collection, remote_id, **fields):
        return super().seed(collection, remote_id, deletedAt=None, **fields)
    def list_collection(self, collection, *, draft=True, trash=False):
        return [r for r in super().list_collection(collection, draft=draft) if trash or not r.get('deletedAt')]
    def get(self, collection, remote_id, *, draft=True, trash=False):
        row = super().get(collection, remote_id, draft=draft)
        if row.get('deletedAt') and not trash: raise LibraryError('Payload HTTP 404')
        return row
    def update(self, collection, remote_id, fields, *, expected_version, draft, trash=False):
        if fields.get('deletedAt'):
            assert self.latest[collection][str(remote_id)]['updatedAt'] == expected_version
            expected_version = self.base[collection][str(remote_id)]['updatedAt']
        row = super().update(collection, remote_id, fields, expected_version=expected_version, draft=draft)
        if self.timeout_after_update:
            self.timeout_after_update = False
            raise UncertainWrite('simulated timeout')
        return row

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.library = Library.initialize(Path(temp.name) / 'vault'); self.client = TrashPayload()
        self.client.seed('works', 501, title='Private Work', slug='private-work', type='book', visibility='private', note='Short impression')
        row = bootstrap(self.library, client=self.client, apply=True)['results'][0]
        map_imported(self.library, self.client, [row['id']], apply=True)
        self.note = self.library.get(row['id'])
    def run_sync(self, **options):
        store = _Store(self.library, self.client.target); state = store.load()
        records = {name: self.client.list_collection(name, trash=True) for name in ('works', 'posts')}
        return sync_lifecycle(self.library, self.client, store, state, records, **options)
    def files(self):
        return {str(p.relative_to(self.library.vault)):p.read_bytes() for p in self.library.vault.rglob('*') if p.is_file()}
    def trash_remote(self, **changes):
        for data in (self.client.latest,self.client.base): data['works']['501'].update({'deletedAt':'2026-01-01T00:00:00Z','updatedAt':'deleted-v1',**changes})
    def delete_local(self): return self.library.delete(self.note['id'], expected_hash=self.note['sha256'])
    def test_unselected_mapped_local_delete_roundtrip_idempotent(self):
        self.delete_local(); before=self.files()
        actions,_,ids=self.run_sync()
        self.assertEqual(actions[0]['action'],'delete'); self.assertEqual(self.files(),before); self.assertEqual(ids,{self.note['id']})
        self.run_sync(apply=True); self.assertTrue(self.client.latest['works']['501']['deletedAt'])
        count=len(self.client.mutations); self.run_sync(apply=True); self.assertEqual(len(self.client.mutations),count)
        self.assertTrue(self.library.delete(self.note['id'],expected_hash=self.note['sha256'])['already_deleted'])
        self.library.recover(self.note['id'],expected_hash=self.note['sha256']); self.run_sync(apply=True)
        restored=self.client.latest['works']['501']; self.assertIsNone(restored['deletedAt']); self.assertEqual(restored['_status'],'draft')
        self.assertEqual(restored['visibility'],'private'); self.assertEqual(self.library.get(self.note['id'])['sha256'],self.note['sha256'])
        self.assertEqual(self.library.publications(),[]); self.assertEqual(self.run_sync()[0],[])
    def test_website_delete_requires_accept_and_restore_keeps_id(self):
        self.trash_remote(); before=self.files(); self.run_sync(apply=True); self.assertEqual(self.files(),before)
        self.assertEqual(self.run_sync(apply=True,accept_remote=True)[1][0]['status'],'deleted-observed')
        self.assertFalse((self.library.vault/self.note['path']).exists()); self.assertEqual(self.client.mutations,[])
        for data in (self.client.latest,self.client.base): data['works']['501'].update({'deletedAt':None,'updatedAt':'restored-v2','_status':'draft'})
        self.run_sync(apply=True,accept_remote=True); self.assertEqual(self.library.get(self.note['id'])['sha256'],self.note['sha256'])
        self.assertEqual(self.client.mutations,[])
    def test_missing_remote_conflicts_without_recreate(self):
        self.delete_local(); self.client.latest['works'].clear(); self.client.base['works'].clear()
        result=self.run_sync(apply=True,accept_remote=True)[1][0]; self.assertEqual(result['status'],'conflict'); self.assertIn('missing',result['reason'])
        self.assertEqual(self.client.mutations,[])
    def test_missing_local_is_not_delete(self):
        (self.library.vault/self.note['path']).unlink(); self.assertEqual(self.run_sync(apply=True,accept_remote=True),([],[],set()))
        self.trash_remote(); self.assertEqual(self.run_sync(apply=True,accept_remote=True)[1][0]['status'],'conflict')
        self.assertFalse(self.library._read_state()['notes'][self.note['id']]['deleted'])
    def test_rename_remote_delete_recovery_preserves_path(self):
        (self.library.vault/self.note['path']).rename(self.library.vault/'renamed.md'); self.trash_remote(); self.run_sync(apply=True,accept_remote=True)
        self.library.recover(self.note['id'],expected_hash=self.note['sha256']); self.assertEqual(self.library.get(self.note['id'])['path'],'renamed.md')
    def test_delete_vs_remote_edit_review_preserves_both(self):
        self.delete_local(); self.client.latest['works']['501'].update({'note':'Concurrent remote edit','updatedAt':'edit-v2'})
        self.assertEqual(self.run_sync(apply=True)[1][0]['status'],'conflict'); self.assertEqual(self.client.mutations,[])
        item=review_lifecycle(self.library,self.client)['items'][0]
        guards={'expected_hash':item['expected_hash'],'expected_remote_version':item['expected_remote_version'],'expected_review_token':item['review_token']}
        before=self.files(); resolve_lifecycle(self.library,self.client,self.note['id'],'delete',**guards); self.assertEqual(self.files(),before)
        result=resolve_lifecycle(self.library,self.client,self.note['id'],'delete',apply=True,**guards)
        self.assertEqual(result['result']['status'],'deleted-observed')
        evidence=list((self.library.private/'lifecycle'/self.note['id']).glob('*.json'))
        self.assertTrue(any('Concurrent remote edit' in p.read_text() for p in evidence))
        self.assertEqual(self.library.history_markdown(self.note['id'],self.note['sha256']),self.note['markdown'])
    def test_local_edit_vs_remote_delete_keeps_current(self):
        current=self.library.replace(self.note['id'],self.note['markdown']+'\nLOCAL EDIT',expected_hash=self.note['sha256']); self.trash_remote()
        self.assertEqual(self.run_sync(apply=True,accept_remote=True)[1][0]['status'],'conflict')
        self.assertEqual(self.library.get(self.note['id'])['sha256'],current['sha256']); self.assertEqual(self.client.mutations,[])
    def test_stale_resolution_rejected(self):
        self.delete_local(); item=review_lifecycle(self.library,self.client)['items'][0]; self.client.latest['works']['501']['updatedAt']='later'
        with self.assertRaisesRegex(LibraryError,'stale'):
            resolve_lifecycle(self.library,self.client,self.note['id'],'delete',apply=True,expected_hash=item['expected_hash'],expected_remote_version=item['expected_remote_version'],expected_review_token=item['review_token'])
        self.assertEqual(self.client.mutations,[])
    def test_timeout_readback_does_not_repeat_mutation(self):
        self.delete_local(); self.client.timeout_after_update=True
        self.assertEqual(self.run_sync(apply=True)[1][0]['status'],'reconciliation-required'); self.assertEqual(len(self.client.mutations),1)
        self.assertEqual(self.run_sync(apply=True)[1][0]['status'],'reconciled'); self.assertEqual(len(self.client.mutations),1)
    def test_recover_retains_snapshot_without_republication(self):
        self.library.publish(self.note['id'],expected_hash=self.note['sha256']); snapshot=deepcopy(self.library.publications()[0]['snapshot'])
        self.delete_local(); self.library.recover(self.note['id'],expected_hash=self.note['sha256']); selected=self.library.publications()[0]
        self.assertEqual(selected['snapshot'],snapshot); self.assertEqual(selected['state'],'withdrawn'); self.assertIsNone(self.library.publication(self.note['id']))
    def test_reappeared_id_and_occupied_recovery_are_rejected(self):
        self.delete_local(); (self.library.vault/self.note['path']).write_text(self.note['markdown'])
        with self.assertRaisesRegex(LibraryError,'reappeared'): self.library.list_notes()
        with self.assertRaisesRegex(LibraryError,'occupied'): self.library.recover(self.note['id'],expected_hash=self.note['sha256'])

    def test_confirm_native_missing_preview_apply_and_recovery(self):
        (self.library.vault/self.note['path']).unlink()
        before=self.files()
        result=self.library.confirm_missing_delete(self.note['id'],expected_hash=self.note['sha256'])
        self.assertFalse(result['applied']); self.assertEqual(self.files(),before)
        self.library.confirm_missing_delete(self.note['id'],expected_hash=self.note['sha256'],apply=True)
        self.assertEqual(self.library.deletion_records()[self.note['id']]['lifecycle']['origin'],'confirmed-missing')
        self.assertEqual(self.run_sync(apply=True)[1][0]['status'],'deleted-observed')
        self.library.recover(self.note['id'],expected_hash=self.note['sha256'])
        self.assertEqual(self.library.get(self.note['id'])['sha256'],self.note['sha256'])

    def test_confirm_missing_unmapped_requires_exact_retained_object(self):
        note=self.library.create('Native draft',title='Unmapped')
        (self.library.vault/note['path']).unlink()
        with self.assertRaisesRegex(LibraryError,'Revision conflict'):
            self.library.confirm_missing_delete(note['id'],expected_hash='wrong',apply=True)
        result=self.library.confirm_missing_delete(note['id'],expected_hash=note['sha256'],apply=True)
        self.assertTrue(result['deleted']); self.assertNotIn(note['id'],_Store(self.library,self.client.target).load()['baselines'])
        self.library.recover(note['id'],expected_hash=note['sha256'])
        self.assertEqual(self.library.get(note['id'])['markdown'],note['markdown'])

    def test_confirm_missing_refuses_renamed_or_unretained(self):
        (self.library.vault/self.note['path']).rename(self.library.vault/'native-rename.md')
        with self.assertRaisesRegex(LibraryError,'Identity still exists'):
            self.library.confirm_missing_delete(self.note['id'],expected_hash=self.note['sha256'],apply=True)
        (self.library.vault/'native-rename.md').unlink()
        self.library._path('.markdown-library/objects/'+self.note['sha256']+'.md').unlink()
        with self.assertRaisesRegex(LibraryError,'Last retained object is missing'):
            self.library.confirm_missing_delete(self.note['id'],expected_hash=self.note['sha256'],apply=True)

    def test_uncertain_write_requires_formal_and_latest_readback(self):
        self.delete_local(); self.client.timeout_after_update=True
        self.run_sync(apply=True)
        self.client.base['works']['501']['deletedAt']=None
        result=self.run_sync(apply=True)[1][0]
        self.assertEqual(result['status'],'reconciliation-required')
        self.assertEqual(len(self.client.mutations),1)
        self.assertIn(self.note['id'],_Store(self.library,self.client.target).load()['lifecycle_pending'])

    def test_restoration_keeps_verified_field_ancestor_without_faking_publication_hash(self):
        from markdown_library.merge import recover_base
        self.delete_local(); self.run_sync(apply=True)
        self.library.recover(self.note['id'],expected_hash=self.note['sha256']); self.run_sync(apply=True)
        state=_Store(self.library,self.client.target).load(); baseline=state['baselines'][self.note['id']]
        local,remote,evidence=recover_base(self.library,self.note['id'],baseline,None,state['baselines'])
        self.assertEqual(evidence,'retained-observation')
        self.assertIsNotNone(local); self.assertEqual(remote['visibility'],'private')

    def test_native_bulk_trash_matches_latest_after_accepted_private_edit(self):
        from markdown_library.transport import sync_once
        self.client.latest['works']['501'].update({'note':'Latest private note','updatedAt':'latest-edit-v3'})
        observed=sync_once(self.library,self.client,apply=True,accept_remote=True)
        self.assertEqual(observed['results'][0]['status'],'remote-draft-imported')
        current=self.library.get(self.note['id'])
        self.library.delete(self.note['id'],expected_hash=current['sha256'])
        self.assertEqual(self.run_sync(apply=True)[1][0]['status'],'deleted-observed')
        self.assertEqual(self.client.base['works']['501']['note'],'Latest private note')
        self.assertTrue(self.client.base['works']['501']['deletedAt'])
