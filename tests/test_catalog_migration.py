from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from markdown_library.core import Library, LibraryError, _render
from markdown_library.catalog_migration import migrate_work_catalog, ARCHIVE, BASE_ARCHIVE
from markdown_library.work_catalog import parse_catalog, build_catalog


class CatalogMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lib = Library.initialize(Path(self.temp.name).resolve())

    def work(self, title, body='', **fields):
        return self.lib.create(body, title=title, kind='work', properties={'type': 'game', **fields})

    def migrate(self):
        preview = migrate_work_catalog(self.lib)
        return migrate_work_catalog(self.lib, apply=True, expected_token=preview['token'])

    def test_preview_is_readonly_and_migration_preserves_all_records_and_selections(self):
        zero = self.work('Zero', rating=0, experiences=[])
        unknown = self.work('Unknown', note=None)
        review = self.work('Review', '# Review\n\nMy own experience.', rating=85,
                           experiences=[{'endedOn': '2024', 'note': 'first'}, {'endedOn': '2025-03'}])
        draft = self.work('Draft', '# Draft\n\n暂拟观点待确认。\n\nAn unconfirmed draft.')
        post = self.lib.create('A public article.', title='Article', kind='post', properties={'link': 'article', 'locale': 'zh',
                'related_works': ['[[' + review['path'].removesuffix('.md') + ']]']})
        self.lib.publish(review['id'], expected_hash=review['sha256'])
        self.lib.publish(post['id'], expected_hash=post['sha256'])
        original_state = self.lib._read_state()
        before = {str(p.relative_to(self.lib.vault)): p.read_bytes() for p in self.lib.vault.rglob('*') if p.is_file()}
        preview = migrate_work_catalog(self.lib)
        self.assertEqual(before, {str(p.relative_to(self.lib.vault)): p.read_bytes() for p in self.lib.vault.rglob('*') if p.is_file()})
        result = migrate_work_catalog(self.lib, apply=True, expected_token=preview['token'])
        self.assertEqual((result['works'], result['review_files'], result['archived_draft_sections']), (4, 1, 1))
        self.assertEqual(original_state['publications'], self.lib._read_state()['publications'])
        self.assertTrue((self.lib.vault / ARCHIVE).is_file())
        for old in [zero, unknown, review, draft]:
            current = self.lib.get(old['id'])
            self.assertEqual(current['properties'], old['properties'])
            self.assertFalse((self.lib.vault / old['path']).exists())
            self.assertFalse(self.lib._read_state()['notes'][old['id']].get('deleted'))
        current = self.lib.get(review['id'])
        raw = (self.lib.vault / current['path']).read_text()
        self.assertIn('My own experience.', raw)
        self.assertNotIn('rating:', raw)
        self.assertIn('My own experience.', current['markdown'])
        self.assertNotIn('暂拟观点', self.lib.get(draft['id'])['markdown'])
        self.assertIn(current['path'].removesuffix('.md'), self.lib.get(post['id'])['markdown'])
        self.assertTrue(migrate_work_catalog(self.lib)['already_migrated'])

    def test_stale_migration_preview_preserves_new_user_edit(self):
        work = self.work('Game', rating=85)
        plan = migrate_work_catalog(self.lib)
        changed = self.lib.replace(work['id'], work['markdown'].replace('rating: 85\n', 'rating: 86\n'), expected_hash=work['sha256'])
        with self.assertRaisesRegex(LibraryError, 'expired'):
            migrate_work_catalog(self.lib, apply=True, expected_token=plan['token'])
        self.assertEqual(self.lib.get(work['id'])['sha256'], changed['sha256'])
        self.assertFalse((self.lib.vault / 'works/作品总表.md').exists())

    def test_native_views_widths_and_unrated_zero_semantics_are_retained(self):
        self.work('Game', rating=0)
        base = '''views:
- name: 我的视图
  filters: rating.isEmpty()
  order: [formula.work, rating]
  columnSize: {formula.work: 377, note.rating: 98}
'''
        self.lib._atomic('works/作品表格.base', base)
        self.migrate()
        catalog = parse_catalog((self.lib.vault / 'works/作品总表.md').read_text()).document
        self.assertEqual(catalog['views'][0]['name'], '我的视图')
        self.assertEqual(catalog['views'][0]['filters']['conditions'][0]['operator'], 'isEmpty')
        self.assertEqual(catalog['records'][0]['values']['rating'], 0)
        self.assertEqual(next(x for x in catalog['fields'] if x['id'] == 'title')['width'], 377)
        self.assertEqual((self.lib.vault / BASE_ARCHIVE).read_text(), base)

    def test_table_only_record_can_gain_an_independent_review_without_changing_id(self):
        work = self.work('Game', rating=85)
        self.migrate()
        current = self.lib.get(work['id'])
        self.lib.replace(work['id'], _render(current['properties'], '# Game\n\nA new personal impression.'), expected_hash=current['sha256'])
        after = self.lib.get(work['id'])
        self.assertEqual(after['properties']['id'], work['id'])
        self.assertEqual(after['properties']['rating'], 85)
        self.assertIn('works/感想/', after['path'])
        self.assertTrue((self.lib.vault / after['path']).is_file())

    def test_moved_review_repairs_other_work_links_and_relative_assets(self):
        other = self.work('Other')
        self.lib._atomic('assets/chart.svg', '<svg/>')
        note = self.work('Review', '# Review\n\nSee [[' + other['path'].removesuffix('.md') + ']].\n\n![chart](../assets/chart.svg)')
        self.migrate()
        after = self.lib.get(note['id'])
        self.assertIn('[[works/作品总表]]', after['markdown'])
        self.assertIn('../../../assets/chart.svg', after['markdown'])
        self.assertEqual((self.lib.vault / 'assets/chart.svg').read_text(), '<svg/>')

    def test_existing_timestamp_experiences_are_preserved_without_truncation(self):
        dates = [{'startedOn': '2026-09-01T12:00:00.000Z', 'endedOn': '2026-10-09T12:00:00.000Z'}]
        work = self.work('Game', experiences=dates)
        self.migrate()
        self.assertEqual(self.lib.get(work['id'])['properties']['experiences'], dates)

    def test_preview_token_covers_transformed_prose(self):
        self.work('Game', '# Game\n\nMy experience.')
        original = migrate_work_catalog(self.lib)
        with patch('markdown_library.catalog_migration._review', return_value=('# Game\n\nChanged experience.\n', '', '')):
            changed = migrate_work_catalog(self.lib)
            self.assertNotEqual(original['token'], changed['token'])
            with self.assertRaisesRegex(LibraryError, 'expired'):
                migrate_work_catalog(self.lib, apply=True, expected_token=original['token'])
        self.assertFalse((self.lib.vault / 'works/作品总表.md').exists())

    def test_table_only_work_keeps_visible_independent_post_entry(self):
        from markdown_library.publication_navigation import set_publication_navigation
        work = self.work('Game', set_publication_navigation('', '发表稿：[[posts/published|公开稿]]'))
        self.migrate()
        doc = parse_catalog((self.lib.vault / 'works/作品总表.md').read_text()).document
        self.assertIn('article', {field['id'] for field in doc['fields']})
        self.assertEqual(doc['records'][0]['values']['article'], '[[posts/published|公开稿]]')
        self.assertIsNone(doc['records'][0]['values']['review'])
        self.assertEqual(self.lib.get(work['id'])['properties'], work['properties'])

    def test_invalid_planned_fields_fail_before_removing_original(self):
        work = self.work('Game', rating=85)
        from markdown_library.catalog_migration import _plan
        summary, plan = _plan(self.lib)
        plan[3]['rows'][work['id']]['properties']['rating'] = 81
        # Clear the staged row while retaining a numeric original; the overlay
        # would silently alter a Work unless checked before commit.
        for index, (path, text, expected) in enumerate(plan[4]):
            if path == 'works/作品总表.md':
                document = parse_catalog(text).document
                document['records'][0]['values']['rating'] = None
                plan[4][index] = (path, build_catalog(document['records'], document), expected)
        with patch('markdown_library.catalog_migration._plan', return_value=(summary, plan)):
            with self.assertRaisesRegex(LibraryError, 'Planned Work fields differ'):
                migrate_work_catalog(self.lib, apply=True, expected_token=summary['token'])
        self.assertTrue((self.lib.vault / work['path']).is_file())
        self.assertFalse((self.lib.vault / 'works/作品总表.md').exists())


if __name__ == '__main__':
    unittest.main()
