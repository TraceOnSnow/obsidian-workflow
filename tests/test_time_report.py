"""Daily time report: read-only aggregates in, one heading of the daily note out."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.core import LibraryError
from markdown_library.time_report import replace_section, time_report

TEMPLATE = '## 日程与待办\n\n![[TaskNotes/Views/当日任务.base]]\n\n## 时间\n\n## 记录\n'
NOW = datetime(2026, 11, 17, 4, 0, tzinfo=timezone.utc)  # 2026-11-17 12:00 at +08:00


def day(date, device='PC', seconds=7500, apps=(('Code.exe', 6000), ('chrome.exe', 1200)), tz='+08:00'):
    hours = [0] * 24
    hours[9], hours[22] = seconds - 600, 600
    return {'date': date, 'device': device, 'source': 'activitywatch', 'tz': tz, 'active_seconds': seconds,
            'hours': hours, 'apps': [{'app': app, 'seconds': value} for app, value in apps]}


class TimeReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.library = Library.initialize(self.vault)
        (self.vault / 'templates').mkdir()
        (self.vault / 'templates/每日笔记.md').write_text(TEMPLATE, encoding='utf-8')
        (self.vault / '10_个人记录/日记').mkdir(parents=True)

    def data(self, *records, month='2026-11', extra=''):
        folder = self.vault / 'data/activitywatch'
        folder.mkdir(parents=True, exist_ok=True)
        text = ''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records) + extra
        (folder / f'{month}.jsonl').write_text(text, encoding='utf-8')

    def note(self, date, text=TEMPLATE):
        path = self.vault / f'10_个人记录/日记/{date}.md'
        path.write_text(text, encoding='utf-8')
        return path

    def run_report(self, **options):
        return time_report(self.library, now=NOW, **options)

    def test_preview_summarizes_without_writing(self):
        self.data(day('2026-11-16'))
        path = self.note('2026-11-16')
        row = self.run_report()['days'][0]
        self.assertEqual((row['action'], row['active_seconds']), ('would-update', 7500))
        self.assertIn('- PC：活跃 2 小时 5 分，9:00–23:00 之间', row['markdown'])
        self.assertIn('| Code.exe | 1 小时 40 分 |', row['markdown'])
        self.assertIn('| 其他 | 5 分 |', row['markdown'])
        self.assertEqual(path.read_text(encoding='utf-8'), TEMPLATE)

    def test_apply_registers_plain_note_and_keeps_other_sections(self):
        self.data(day('2026-11-16'))
        path = self.note('2026-11-16', TEMPLATE + '\n今天写了导出脚本。\n\n```md\n## 时间\n```\n')
        row = self.run_report(apply=True)['days'][0]
        self.assertEqual((row['action'], row['note']), ('updated', 'registered'))
        text = path.read_text(encoding='utf-8')
        self.assertIn('## 时间\n\n- PC：活跃 2 小时 5 分', text)
        self.assertTrue(text.endswith('## 记录\n\n今天写了导出脚本。\n\n```md\n## 时间\n```\n'))
        self.assertIn('![[TaskNotes/Views/当日任务.base]]', text)
        again = self.run_report(apply=True)['days'][0]
        self.assertEqual((again['action'], again['note']), ('unchanged', 'existing'))
        self.assertEqual(path.read_text(encoding='utf-8'), text)

    def test_recomputed_day_replaces_previous_figures(self):
        self.data(day('2026-11-16'))
        path = self.note('2026-11-16')
        self.run_report(apply=True)
        self.data(day('2026-11-16', seconds=9000, apps=(('Code.exe', 9000),)))
        self.assertEqual(self.run_report(apply=True)['days'][0]['action'], 'updated')
        text = path.read_text(encoding='utf-8')
        self.assertIn('活跃 2 小时 30 分', text)
        self.assertNotIn('chrome.exe', text)
        self.assertEqual(text.count('## 时间'), 1)

    def test_unfinished_day_is_left_alone_unless_included(self):
        self.data(day('2026-11-17'))
        path = self.note('2026-11-17')
        self.assertEqual(self.run_report(apply=True)['days'][0]['action'], 'day-not-finished')
        self.assertEqual(path.read_text(encoding='utf-8'), TEMPLATE)
        self.assertEqual(self.run_report(apply=True, include_running=True)['days'][0]['action'], 'updated')

    def test_missing_note_is_created_only_on_request(self):
        self.data(day('2026-11-16'))
        self.assertEqual(self.run_report(apply=True)['days'][0]['action'], 'no-note')
        self.assertFalse((self.vault / '10_个人记录/日记/2026-11-16.md').exists())
        row = self.run_report(apply=True, create_missing=True)['days'][0]
        self.assertEqual((row['action'], row['note']), ('updated', 'created'))
        text = (self.vault / '10_个人记录/日记/2026-11-16.md').read_text(encoding='utf-8')
        self.assertIn('## 日程与待办', text)
        self.assertIn('## 时间\n\n- PC：活跃', text)

    def test_note_without_heading_is_not_changed(self):
        self.data(day('2026-11-16'))
        path = self.note('2026-11-16', '只有一句话。\n')
        self.assertEqual(self.run_report(apply=True)['days'][0]['action'], 'no-heading')
        self.assertIn('只有一句话。', path.read_text(encoding='utf-8'))

    def test_devices_categories_and_display_names(self):
        self.data(day('2026-11-16'), day('2026-11-16', device='Phone', seconds=1800, apps=(('com.tencent.mm', 1800),)),
                  extra='not json\n{"date": "2026-13-40"}\n')
        (self.vault / 'config').mkdir(exist_ok=True)
        (self.vault / 'config/time-tracking.json').write_text(json.dumps({
            'version': 1, 'app_names': {'Code.exe': 'VS Code', 'com.tencent.mm': '微信'},
            'categories': [{'name': '编程', 'apps': ['Code.exe']}, {'name': '聊天', 'apps': ['com.tencent.mm']}]},
            ensure_ascii=False), encoding='utf-8')
        result = self.run_report()
        row = result['days'][0]
        self.assertEqual(result['skipped_lines'], 2)
        self.assertEqual(row['active_seconds'], 9300)
        self.assertEqual([item['name'] for item in row['categories']], ['编程', '聊天', '未分类'])
        self.assertIn('- 合计：2 小时 35 分', row['markdown'])
        self.assertIn('| VS Code | 1 小时 40 分 |', row['markdown'])
        self.assertIn('| 微信 | 30 分 |', row['markdown'])

    def test_explicit_dates_and_limits(self):
        self.data(day('2026-11-14'), day('2026-11-15'), day('2026-11-16'))
        self.assertEqual([row['date'] for row in self.run_report(days=2)['days']], ['2026-11-15', '2026-11-16'])
        self.assertEqual(self.run_report(dates=['2026-11-01'])['days'], [{'date': '2026-11-01', 'action': 'no-data'}])
        with self.assertRaises(LibraryError):
            self.run_report(dates=['yesterday'])

    def test_replace_section_at_end_of_note(self):
        self.assertEqual(replace_section('## 时间\n旧内容\n', '时间', '新内容'), '## 时间\n\n新内容\n')
        self.assertIsNone(replace_section('### 时间\n', '时间', '新内容'))


if __name__ == '__main__':
    unittest.main()
