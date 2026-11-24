from pathlib import Path
import tempfile
import unittest

from markdown_library import Library, LibraryError
from markdown_library.job_tracker import END, START, parse, parse_line, regenerate, tracker_summary

EVENTS = '''# 追踪

- 09-03 甲公司｜方向A｜笔试 — 挂
- 09-24 甲公司｜方向B｜笔试 — 过；之后进入面试
- 10-14 [[x/10-14 甲 一面|甲公司｜方向C｜一面]] — 挂
- 09-28~ 乙公司｜岗1｜笔试 — 没消息
- 10-11 乙公司｜岗2｜笔试 — 没消息
- 10-20（截止／窗口） 乙公司｜测评 — 已邀请
- 10-29 丙公司｜岗｜简历 — 结束；暂不匹配
- 10-30 丁公司｜岗｜二面 — 过；免笔试
- 10-30 丁公司｜岗｜主管面 — 等结果

我自己写的结尾。
'''


class JobTrackerTests(unittest.TestCase):
    def test_parse_line_fields(self):
        event = parse_line('- 09-28~（可选场次） 乙公司｜岗1｜笔试 — 没消息；备注')
        self.assertEqual((event['date'], event['approximate'], event['company'], event['role'],
                          event['stage'], event['result'], event['note']),
                         ('09-28', True, '乙公司', '岗1', '笔试', '没消息', '备注'))
        self.assertIsNone(parse_line('普通段落'))
        with self.assertRaisesRegex(LibraryError, '环节'):
            parse_line('- 09-03 甲公司｜技术笔试 — 挂')
        with self.assertRaisesRegex(LibraryError, '结果'):
            parse_line('- 09-03 甲公司｜笔试 — 已参加，笔试挂。')

    def test_summary_counts_and_progressive_view(self):
        updated, events, problems = regenerate(EVENTS, as_of='11-19')
        self.assertEqual(problems, [])
        self.assertEqual(len(events), 9)
        summary = updated[updated.index(START):updated.index(END)]
        self.assertIn('**简历阶段结束：1**\n- 丙公司', summary)
        self.assertIn('**笔试：4**\n- 过 1：甲公司\n- 挂 1：甲公司\n- 没消息 2：乙公司 ×2', summary)
        self.assertIn('- 还在流程里：丁公司', summary)
        self.assertIn('- 一面挂：甲公司', summary)
        self.assertNotIn('笔试挂：甲公司', summary)
        self.assertIn('- 笔试挂：乙公司 ×2', summary)
        self.assertTrue(updated.rstrip().endswith(END))
        self.assertIn('我自己写的结尾。', updated)

    def test_contact_lines_with_results_do_not_place_a_company(self):
        markdown = EVENTS.replace('我自己写的结尾。', '- 11-01 戊公司｜沟通 — 没消息；招聘方联系\n'
                                  '- 11-02 丁公司｜方向｜沟通 — 等结果\n\n我自己写的结尾。')
        updated, events, problems = regenerate(markdown, as_of='11-19')
        self.assertEqual(problems, [])
        summary = updated[updated.index(START):updated.index(END)]
        self.assertNotIn('戊公司', summary)
        self.assertIn('- 还在流程里：丁公司', summary)

    def test_regenerate_replaces_only_marked_section(self):
        first, _, _ = regenerate(EVENTS, as_of='11-19')
        with_tail = first + '\n作者的帖子\n'
        second, _, _ = regenerate(with_tail.replace('- 10-30 丁公司｜岗｜主管面 — 等结果', '- 10-30 丁公司｜岗｜主管面 — 挂'),
                                  as_of='11-20')
        self.assertEqual(second.count(START), 1)
        self.assertIn('（截至11-20）', second)
        self.assertIn('- 主管面挂：丁公司', second)
        self.assertTrue(second.endswith('\n作者的帖子\n'))

    def test_problems_block_writing(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        library = Library.initialize(Path(temporary.name) / 'vault')
        note = library.create(EVENTS + '- 11-01 戊公司｜面试 — 已参加\n', title='追踪', path='10_个人记录/追踪.md')
        result = tracker_summary(library, note['id'], as_of='11-19', apply=True)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(len(result['problems']), 1)
        self.assertEqual(library.get(note['id'])['sha256'], note['sha256'])
        fixed = library.replace(note['id'], library.get(note['id'])['markdown'].replace('｜面试 — 已参加', '｜一面 — 没消息'),
                                expected_hash=note['sha256'])
        result = tracker_summary(library, note['id'], as_of='11-19', apply=True)
        self.assertEqual(result['status'], 'applied')
        self.assertIn(START, library.get(note['id'])['markdown'])
        self.assertNotEqual(result['sha256'], fixed['sha256'])
        again = tracker_summary(library, note['id'], as_of='11-19', apply=True)
        self.assertFalse(again['changed'])


if __name__ == '__main__':
    unittest.main()
