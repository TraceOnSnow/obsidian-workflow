from pathlib import Path
import tempfile
import unittest

from markdown_library import Library, LibraryError
from markdown_library.cli import dispatch, parser
from markdown_library.note_edit import edit_note


class NoteEditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = Path(self.temporary.name) / 'vault'
        self.library = Library.initialize(self.vault)
        self.note = self.library.create('# 10-27 面经\n\n内容 A\n', title='10-27 面经', path='10_个人记录/求职/10-27 面经.md',
                                        properties={'date': '2026-10-27'})
        self.other = self.library.create('见 [[10_个人记录/求职/10-27 面经|10-27 面经]]\n', title='追踪', path='10_个人记录/求职/追踪.md')

    def test_preview_writes_nothing(self):
        before = (self.vault / self.note['path']).read_bytes()
        result = edit_note(self.library, self.note['id'], {'edits': [{'old': '内容 A', 'new': '内容 B'}]})
        self.assertEqual(result['status'], 'preview')
        self.assertTrue(result['changed'])
        self.assertIn('-内容 A', result['diff'])
        self.assertIn('+内容 B', result['diff'])
        self.assertEqual((self.vault / self.note['path']).read_bytes(), before)

    def test_apply_exact_edit(self):
        result = edit_note(self.library, self.note['id'], {'edits': [{'old': '内容 A', 'new': '内容 B'}]}, apply=True)
        current = self.library.get(self.note['id'])
        self.assertIn('内容 B', current['markdown'])
        self.assertEqual(result['sha256'], current['sha256'])

    def test_count_mismatch_and_stale_hash_refuse(self):
        with self.assertRaisesRegex(LibraryError, 'occurs 0 times'):
            edit_note(self.library, self.note['id'], {'edits': [{'old': '不存在', 'new': 'x'}]}, apply=True)
        with self.assertRaisesRegex(LibraryError, 'occurs 3 times, expected 1'):
            edit_note(self.library, self.note['id'], {'edits': [{'old': '10-27', 'new': '10-26'}]}, apply=True)
        with self.assertRaisesRegex(LibraryError, 'Revision conflict'):
            edit_note(self.library, self.note['id'], {'expected_hash': 'stale',
                      'edits': [{'old': '内容 A', 'new': 'x'}]}, apply=True)
        self.assertEqual(self.library.get(self.note['id'])['sha256'], self.note['sha256'])

    def test_stable_id_is_protected(self):
        with self.assertRaisesRegex(LibraryError, 'stable id'):
            edit_note(self.library, self.note['id'],
                      {'edits': [{'old': 'id: ' + self.note['id'], 'new': 'id: other'}]}, apply=True)

    def test_redate_with_rename_repairs_links(self):
        # title, H1 and the date property each carry the date once
        request = {'edits': [{'old': '10-27', 'new': '10-26', 'count': 3}],
                   'rename': {'destination': '10-26 面经'}}
        result = edit_note(self.library, self.note['id'], request, apply=True)
        current = self.library.get(self.note['id'])
        self.assertEqual(current['path'], '10_个人记录/求职/10-26 面经.md')
        self.assertEqual(result['path'], current['path'])
        self.assertEqual(current['properties']['title'], '10-26 面经')
        self.assertEqual(current['properties']['date'], '2026-10-26')
        self.assertIn('[[10_个人记录/求职/10-26 面经', self.library.get(self.other['id'])['markdown'])
        self.assertFalse((self.vault / '10_个人记录/求职/10-27 面经.md').exists())

    def test_cli(self):
        request = Path(self.temporary.name) / 'edit.json'
        request.write_text('{"edits": [{"old": "内容 A", "new": "内容 C"}]}')
        arguments = parser().parse_args(['--vault', str(self.vault), 'edit', self.note['id'],
                                         '--request', str(request), '--apply'])
        self.assertEqual(dispatch(arguments)['status'], 'applied')
        self.assertIn('内容 C', self.library.get(self.note['id'])['markdown'])
        schema = dispatch(parser().parse_args(['--vault', str(self.vault), 'edit', '--schema']))
        self.assertIn('edits', schema['properties'])


if __name__ == '__main__':
    unittest.main()
