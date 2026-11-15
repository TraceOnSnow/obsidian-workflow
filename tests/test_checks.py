"""Editorial checks: private rules file in the Vault, generic read-only engine."""
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.checks import check_notes
from markdown_library.cli import main
from markdown_library.core import LibraryError


class EditorialCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.library = Library.initialize(self.vault)

    def rules(self, *rules):
        target = self.vault / 'config/editorial-checks.json'
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps({'version': 1, 'rules': list(rules)}, ensure_ascii=False))

    def found(self, **options):
        result = check_notes(self.library, **options)
        return [(row['rule'], row['line'], row['excerpt']) for row in result['findings']]

    def test_forbid_skips_fenced_code_and_reports_whole_note_line_numbers(self):
        note = self.library.create('第一行。\n\n这一点未核实。\n\n```cpp\n// 未核实\n```\n', title='面经')
        self.rules({'id': 'no-status', 'type': 'forbid', 'pattern': '未核实', 'message': '不写核实状态'})
        result = check_notes(self.library)
        self.assertFalse(result['clean'])
        self.assertEqual(result['counts'], {'no-status': 1})
        row = result['findings'][0]
        self.assertEqual((row['id'], row['path'], row['message']), (note['id'], note['path'], '不写核实状态'))
        self.assertEqual(note['markdown'].split('\n')[row['line'] - 1], '这一点未核实。')
        self.assertEqual(self.library.get(note['id'])['sha256'], note['sha256'])

    def test_code_span_rule_only_looks_inside_inline_code(self):
        self.library.create('复杂度 `O(n)`，条件 `k≤10`，函数 `solve(n)`，正文里 k≤10 不算。', title='笔试')
        self.rules({'id': 'math', 'type': 'forbid-in-code-span', 'pattern': r'^O\(.*\)$|[≤≥]'})
        self.assertEqual([row[2] for row in self.found()], ['`O(n)`', '`k≤10`'])

    def test_require_and_filename_rules(self):
        self.library.create('## 第一题：甲\n\n### 题目大意\n\n略。', title='2026-01-02 有题意')
        self.library.create('## 第一题：乙\n\n直接写思路。', title='没有日期也没有题意')
        self.rules({'id': 'statement', 'type': 'require', 'pattern': '^### 题目大意'},
                   {'id': 'dated', 'type': 'filename', 'pattern': r'^\d{4}-\d{2}-\d{2}'})
        found = self.found()
        self.assertEqual(sorted(row[:2] for row in found), [('dated', None), ('statement', None)])
        self.assertTrue(all('没有日期也没有题意' in check_notes(self.library)['findings'][i]['path'] for i in range(2)))

    def test_scope_by_kind_folder_path_and_ignored_lines(self):
        self.library.create('真名出现。\n\n[[原稿路径/真名|私人原稿]]', title='公开稿', kind='post', path='发表稿/公开稿.md')
        self.library.create('真名出现。', title='私人稿', path='私人/私人稿.md')
        self.library.create('真名出现。', title='收件', path='00_收件箱/收件.md')
        rule = {'id': 'anonymous', 'type': 'forbid', 'pattern': '真名'}
        self.rules(rule)
        self.assertEqual(len(self.found()), 4)
        self.rules(rule | {'kinds': ['post']})
        self.assertEqual(len(self.found()), 2)
        self.rules(rule | {'kinds': ['post'], 'ignore_line_pattern': r'\|私人原稿\]\]'})
        self.assertEqual([(row[0], row[2]) for row in self.found()], [('anonymous', '真名出现。')])
        self.rules(rule | {'folders': ['私人/', '00_收件箱/'], 'exclude_folders': ['00_收件箱/']})
        self.assertEqual(len(self.found()), 1)
        self.rules(rule | {'path_pattern': '收件'})
        self.assertEqual(len(self.found()), 1)
        self.rules(rule)
        self.assertEqual(len(self.found(folder='发表稿')), 2)
        self.assertEqual(len(self.found(kind='note')), 2)

    def test_selected_ids_limit_and_clean_result(self):
        first = self.library.create('坏 坏 坏', title='a')
        second = self.library.create('好', title='b')
        self.rules({'id': 'bad', 'type': 'forbid', 'pattern': '坏'})
        result = check_notes(self.library, ids=[first['id']], limit=2)
        self.assertEqual((result['checked'], result['total'], len(result['findings']), result['truncated']), (1, 3, 2, True))
        clean = check_notes(self.library, ids=[second['id']])
        self.assertEqual((clean['clean'], clean['total'], clean['findings']), (True, 0, []))
        with self.assertRaises(LibraryError):
            check_notes(self.library, ids=['missing'])

    def test_missing_or_invalid_rules_are_refused(self):
        self.library.create('正文', title='a')
        with self.assertRaisesRegex(LibraryError, 'No editorial rules file'):
            check_notes(self.library)
        for bad in ({'id': 'x', 'type': 'forbid', 'pattern': '('},
                    {'id': 'x', 'type': 'unknown', 'pattern': 'a'},
                    {'id': 'x', 'type': 'forbid', 'pattern': 'a', 'surprise': 1},
                    {'id': 'x', 'type': 'forbid', 'pattern': 'a', 'kinds': 'post'}):
            self.rules(bad)
            with self.assertRaises(LibraryError):
                check_notes(self.library)
        self.rules({'id': 'x', 'type': 'forbid', 'pattern': 'a'}, {'id': 'x', 'type': 'forbid', 'pattern': 'b'})
        with self.assertRaisesRegex(LibraryError, 'unique'):
            check_notes(self.library)
        with self.assertRaises(LibraryError):
            check_notes(self.library, folder='.markdown-library')

    def test_cli_exits_two_only_when_something_is_found(self):
        self.library.create('这一点未核实。', title='a')
        self.rules({'id': 'no-status', 'type': 'forbid', 'pattern': '未核实'})
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(['--vault', str(self.vault), 'check']), 2)
        self.assertEqual(json.loads(out.getvalue())['counts'], {'no-status': 1})
        self.rules({'id': 'no-status', 'type': 'forbid', 'pattern': '不会出现'})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--vault', str(self.vault), 'check']), 0)
        other = Path(self.tmp.name) / 'other.json'
        other.write_text(json.dumps({'version': 1, 'rules': [{'id': 'r', 'type': 'forbid', 'pattern': '核实'}]}))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--vault', str(self.vault), 'check', '--rules', str(other)]), 2)


if __name__ == '__main__':
    unittest.main()
