import unittest
import test_purge as fixtures
from markdown_library.review import review_sync
from markdown_library.overview import publication_overview
from markdown_library.bootstrap import bootstrap, BootstrapError

class PurgeReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.PurgeTests('test_not_soft_deleted_refuses_without_writes')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.soft_delete()
        self.fixture.remote_remove()
        self.fixture.purge()

    def test_explicit_purge_is_not_a_missing_file_warning(self):
        f=self.fixture
        before=f.files()
        review=review_sync(f.library,f.client)
        self.assertEqual(review['items'][0]['status'],'purged')
        overview=publication_overview(f.library,review)
        self.assertEqual(overview['counts']['purged'],1)
        self.assertEqual(overview['counts'].get('review',0),0)
        self.assertEqual(f.files(),before)

    def test_reappearing_remote_requires_review(self):
        f=self.fixture
        f.client.seed('works',501,title='Reappeared',slug='private-work',type='book',visibility='private')
        before=f.files()
        review=review_sync(f.library,f.client)
        self.assertEqual(review['items'][0]['status'],'purged-review')
        self.assertEqual(publication_overview(f.library,review)['counts']['review'],1)
        self.assertEqual(f.files(),before)

    def test_mismatched_tombstone_is_not_hidden_as_completed(self):
        f=self.fixture
        state=f.library._read_state()
        state['notes'][f.note['id']]['remote_proof']['remote_id']='999'
        f.library._save(state)
        before=f.files()
        review=review_sync(f.library,f.client)
        self.assertEqual(review['items'][0]['status'],'purged-review')
        self.assertEqual(publication_overview(f.library,review)['counts']['review'],1)
        self.assertEqual(f.files(),before)

    def test_new_bootstrap_can_continue_without_recreating_purged_identity(self):
        f=self.fixture
        f.client.seed('works',502,title='New Work',slug='new-work',type='book',visibility='private')
        result=bootstrap(f.library,client=f.client,apply=True)
        self.assertEqual(len(result['results']),1)
        self.assertNotEqual(result['results'][0]['id'],f.note['id'])
        self.assertTrue(f.library._read_state()['notes'][f.note['id']]['purged'])

    def test_bootstrap_refuses_reappearing_permanently_deleted_identity(self):
        f=self.fixture
        f.client.seed('works',501,title='Reappeared',slug='private-work',type='book',visibility='private')
        before=f.files()
        with self.assertRaisesRegex(BootstrapError,'Permanently deleted remote identity'):
            bootstrap(f.library,client=f.client,apply=True)
        # Acquiring the established bootstrap lock is the only allowed new file.
        after=f.files()
        after.pop('.markdown-library/bootstrap.lock',None)
        before.pop('.markdown-library/bootstrap.lock',None)
        self.assertEqual(after,before)

if __name__=='__main__':unittest.main()
