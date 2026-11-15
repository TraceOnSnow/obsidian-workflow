"""Bounded search: every term must match, best matches first, short excerpts only."""
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.note_queries import search_notes


class NoteSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = Library.initialize(Path(self.tmp.name) / 'vault')
        self.window = self.library.create('先讲了项目。\n\n最后一题用滑动窗口，窗口右移时更新答案。', title='某次面试')
        self.titled = self.library.create('只提到一次窗口。', title='滑动窗口模板')
        self.other = self.library.create('HAVING filters groups; WHERE filters rows.', title='SQL notes')

    def titles(self, query, **options):
        return [item['title'] for item in search_notes(self.library, query, **options)['items']]

    def test_all_terms_must_match_in_any_order_and_case(self):
        self.assertEqual(self.titles('where having'), ['SQL notes'])
        self.assertEqual(self.titles('滑动窗口 项目'), ['某次面试'])
        self.assertEqual(self.titles('滑动窗口 不存在的词'), [])

    def test_title_match_ranks_above_body_only_match(self):
        self.assertEqual(self.titles('滑动窗口'), ['滑动窗口模板', '某次面试'])

    def test_snippet_is_a_short_body_excerpt_around_the_match(self):
        long_body = '铺垫。' * 200 + '关键句在这里出现。' + '收尾。' * 200
        self.library.create(long_body, title='长文')
        item = search_notes(self.library, '关键句')['items'][0]
        self.assertIn('关键句在这里出现', item['snippet'])
        self.assertLess(len(item['snippet']), 200)
        self.assertTrue(item['snippet'].startswith('…') and item['snippet'].endswith('…'))
        self.assertNotIn('markdown', item)
        self.assertNotIn('\n', item['snippet'])

    def test_empty_query_keeps_path_order_without_snippets(self):
        result = search_notes(self.library, '', limit=2)
        self.assertEqual(result['total'], 3)
        self.assertEqual([item['path'] for item in result['items']],
                         sorted(item['path'] for item in result['items']))
        self.assertNotIn('snippet', result['items'][0])
        self.assertEqual(result['next_offset'], 2)

    def test_filters_and_pagination_still_apply_to_ranked_results(self):
        self.assertEqual(self.titles('窗口', kind='post'), [])
        first = search_notes(self.library, '窗口', limit=1)
        second = search_notes(self.library, '窗口', limit=1, offset=first['next_offset'])
        self.assertEqual(first['total'], 2)
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertIsNone(second['next_offset'])


if __name__ == '__main__':
    unittest.main()
