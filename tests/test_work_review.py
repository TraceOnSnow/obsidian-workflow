from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_library import Library, LibraryError
from markdown_library.core import _split
from markdown_library.relations import wikilink
from markdown_library.work_profile import profile, set_profile, WEBSITE_ONLY
from markdown_library.work_review import prepare_work_review, review_body, FOOTER_START
from markdown_library.publication_navigation import set_publication_navigation


class WorkReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = Library.initialize(Path(self.tmp.name) / 'vault')
        self.body = '# Full feelings\n\nPrivate details, examples and reservations.\n\n```text\nkeep code\n```\n'
        self.work = self.library.create(self.body, title='My game', kind='work', properties={'type': 'game', 'rating': 8})

    def snapshot(self):
        return {path.relative_to(self.library.vault).as_posix(): path.read_bytes()
                for path in self.library.vault.rglob('*') if path.is_file()}

    def preview(self):
        work = self.library.get(self.work['id'])
        return prepare_work_review(self.library, work['id'], work['sha256'])

    def apply(self, plan, **kwargs):
        return prepare_work_review(self.library, self.work['id'], plan['work']['sha256'],
                                   expected_token=plan['expected_token'], apply=True, **kwargs)

    def test_creation_links_backlinks_compact_body_and_no_publication(self):
        before = self.snapshot()
        plan = self.preview()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(plan, self.preview())
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'created')
        work = self.library.get(self.work['id'])
        post = self.library.get(result['post']['id'])
        self.assertTrue(post['path'].startswith('发表稿/'))
        self.assertEqual(_split(post['markdown'])[1], self.body)
        self.assertEqual(review_body(_split(work['markdown'])[1]), self.body)
        self.assertIn(wikilink(post), work['markdown'])
        self.assertEqual(post['properties']['related_works'], [wikilink(work)])
        self.assertEqual(post['properties']['link'], 'work-' + work['id'] + '-review')
        self.assertEqual(post['properties']['locale'], 'zh')
        self.assertEqual(post['properties']['title'], work['properties']['title'])
        self.assertEqual(profile(self.library, work['id'])['article'], post['id'])
        self.assertTrue(WEBSITE_ONLY.isdisjoint(work['properties']))
        self.assertFalse(result['publication_selected'])
        self.assertFalse(result['remote_writes'])
        self.assertEqual(self.library.publications(), [])

    def test_prepare_does_not_copy_private_publication_status_into_post(self):
        marked = set_publication_navigation(self.work['markdown'], '> ![[发布与同步状态#^publication-private]]')
        self.library.replace(self.work['id'], marked, expected_hash=self.work['sha256'])
        result = self.apply(self.preview())
        post = self.library.get(result['post']['id'])
        self.assertEqual(_split(post['markdown'])[1], self.body)
        self.assertNotIn('publication-private', post['markdown'])

    def test_repeat_has_no_rewrites_no_duplicate_footer_or_post(self):
        first = self.apply(self.preview())
        plan = self.preview()
        before = self.snapshot()
        repeated = self.apply(plan)
        self.assertFalse(repeated['applied'])
        self.assertEqual(repeated['changed'], [])
        self.assertEqual(repeated['outcome'], 'unchanged')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(first['post']['id'], repeated['post']['id'])
        self.assertEqual(len(self.library.list_notes()), 2)
        self.assertEqual(self.library.get(self.work['id'])['markdown'].count(FOOTER_START), 1)

    def test_work_change_or_profile_change_expires_preview_without_mutation(self):
        plan = self.preview()
        work = self.library.get(self.work['id'])
        self.library.replace(work['id'], work['markdown'] + '\nA new thought.', expected_hash=work['sha256'])
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            self.apply(plan)
        self.assertEqual(self.snapshot(), before)
        plan = self.preview()
        with self.library._lock():
            state = self.library._load()
            retained = profile(self.library, work['id'], state)
            retained['description'] = 'Changed hidden metadata'
            set_profile(state, work['id'], retained)
            self.library._commit(state)
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            self.apply(plan)
        self.assertEqual(self.snapshot(), before)

    def test_edited_post_guard_and_correct_guard_copy_full_body_keep_selections(self):
        first = self.apply(self.preview())
        post = self.library.get(first['post']['id'])
        self.library.publish(post['id'], expected_hash=post['sha256'])
        selections = deepcopy(self.library.publications())
        self.library.replace(post['id'], post['markdown'] + '\nSeparately edited Post draft.', expected_hash=post['sha256'])
        plan = self.preview()
        self.assertTrue(plan['requires_post_hash'])
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            self.apply(plan)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(LibraryError):
            self.apply(plan, expected_post_hash=post['sha256'])
        updated = self.library.get(post['id'])
        result = self.apply(plan, expected_post_hash=updated['sha256'])
        self.assertEqual(result['outcome'], 'update')
        self.assertEqual(_split(self.library.get(post['id'])['markdown'])[1], self.body)
        self.assertEqual(self.library.publications(), selections)
        self.assertTrue(any(entry['sha256'] == updated['sha256'] for entry in self.library.history(post['id'])))

    def test_post_edit_after_preview_expires_even_with_current_post_hash(self):
        first = self.apply(self.preview())
        plan = self.preview()
        post = self.library.get(first['post']['id'])
        current = self.library.replace(post['id'], post['markdown'] + '\nEdit', expected_hash=post['sha256'])
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            self.apply(plan, expected_post_hash=current['sha256'])
        self.assertEqual(self.snapshot(), before)

    def test_work_body_update_excludes_footer_and_preserves_text_added_after_footer(self):
        first = self.apply(self.preview())
        work = self.library.get(self.work['id'])
        extra = '\nMore detail after the navigation footer.\n'
        self.library.replace(work['id'], work['markdown'] + extra, expected_hash=work['sha256'])
        plan = self.preview()
        self.apply(plan, expected_post_hash=plan['post']['sha256'])
        post = self.library.get(first['post']['id'])
        work = self.library.get(work['id'])
        self.assertEqual(_split(post['markdown'])[1], self.body + extra)
        self.assertEqual(review_body(_split(work['markdown'])[1]), self.body + extra)
        self.assertNotIn(FOOTER_START, post['markdown'])

    def test_reuses_existing_local_or_proven_numeric_article_preserves_other_backlinks(self):
        post = self.library.create('Old body', title='Hand edited title', kind='post',
                                   properties={'link': 'original-link', 'related_works': ['[[works/Other|Other]]']})
        with self.library._lock():
            state = self.library._load()
            retained = profile(self.library, self.work['id'], state)
            retained['article'] = 27
            set_profile(state, self.work['id'], retained)
            self.library._commit(state)
        self.library._atomic('.markdown-library/sync-state.json', json.dumps({'baselines': {
            post['id']: {'collection': 'posts', 'remote_id': '27'}}}))
        plan = self.preview()
        self.assertEqual(plan['post']['id'], post['id'])
        result = self.apply(plan, expected_post_hash=post['sha256'])
        saved = self.library.get(post['id'])
        self.assertEqual(saved['properties']['title'], 'Hand edited title')
        self.assertEqual(saved['properties']['link'], 'original-link')
        self.assertIn('[[works/Other|Other]]', saved['properties']['related_works'])
        self.assertEqual(len(self.library.list_notes()), 2)
        self.assertEqual(result['post']['id'], post['id'])

    def test_unproven_article_occupied_path_and_empty_work_fail_safely(self):
        with self.library._lock():
            state = self.library._load()
            retained = profile(self.library, self.work['id'], state)
            retained['article'] = 99
            set_profile(state, self.work['id'], retained)
            self.library._commit(state)
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            self.preview()
        self.assertEqual(self.snapshot(), before)
        with self.library._lock():
            state = self.library._load()
            retained = profile(self.library, self.work['id'], state)
            retained.pop('article')
            set_profile(state, self.work['id'], retained)
            self.library._commit(state)
        target = self.library.vault / ('发表稿/work-' + self.work['id'] + '-review.md')
        target.parent.mkdir(exist_ok=True)
        target.write_text('An unrelated ordinary file')
        with self.assertRaises(LibraryError):
            self.preview()
        target.unlink()
        empty = self.library.create('', title='Empty', kind='work')
        with self.assertRaises(LibraryError):
            prepare_work_review(self.library, empty['id'], empty['sha256'])

    def test_crlf_body_and_malformed_managed_footer_preserve_user_text(self):
        work = self.library.get(self.work['id'])
        body = '\r\n# Feelings\r\n\r\nExact original paragraphs.\r\n'
        from markdown_library.core import _render
        self.library.replace(work['id'], _render(work['properties'], body), expected_hash=work['sha256'])
        result = self.apply(self.preview())
        self.assertEqual(_split(self.library.get(result['post']['id'])['markdown'])[1], body)
        current = self.library.get(work['id'])
        self.assertEqual(review_body(_split(current['markdown'])[1]), body)
        self.library.replace(work['id'], current['markdown'] + '\n' + FOOTER_START,
                             expected_hash=current['sha256'])
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            self.preview()
        self.assertEqual(self.snapshot(), before)

    def test_journal_recovers_post_work_link_and_profile_as_one_operation(self):
        plan = self.preview()
        atomic = self.library._atomic
        def interrupt(relative, text):
            if relative == self.work['path']:
                raise OSError('Simulated interruption after Post save')
            return atomic(relative, text)
        with patch.object(self.library, '_atomic', side_effect=interrupt):
            with self.assertRaises(LibraryError):
                self.apply(plan)
        self.assertTrue(self.library._path('.markdown-library/transaction.json').exists())
        work = self.library.get(self.work['id'])
        post = self.library.get(profile(self.library, work['id'])['article'])
        self.assertIn(wikilink(post), work['markdown'])
        self.assertEqual(_split(post['markdown'])[1], self.body)
        self.assertEqual(self.library.publications(), [])
        self.assertFalse(self.library._path('.markdown-library/transaction.json').exists())


if __name__ == '__main__':
    unittest.main()
