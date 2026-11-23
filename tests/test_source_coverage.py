"""Recording that an existing note already covers a chat source: mapping only, guarded, idempotent."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from markdown_library import Library
from markdown_library.cli import main
from markdown_library.core import LibraryError
from markdown_library.note_queries import note_sources
from markdown_library.source_coverage import COVERED_TYPE, cover_source


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def covered(conversation='conv-synthetic-1', messages=('msg-a',), **extra):
    return {'type': COVERED_TYPE, 'account_key': 'account-synthetic', 'conversation_id': conversation,
            'evidence': [{'source_message_id': m, 'role': 'user', 'text_sha256': sha('synthetic text ' + m)}
                         for m in messages]} | extra


class SourceCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = Library.initialize(Path(self.tmp.name) / 'vault')
        self.note = self.library.create('合成正文，只用于测试。', title='合成笔记')
        self.other = self.library.create('另一篇合成正文。', title='另一篇合成笔记')

    def state(self):
        return json.loads((self.library.vault / '.markdown-library/state.json').read_text())

    def apply(self, note, source):
        preview = cover_source(self.library, note['id'], source, expected_hash=note['sha256'])
        return preview, cover_source(self.library, note['id'], source, expected_hash=note['sha256'],
                                     apply=True, expected_token=preview['token'])

    def test_preview_writes_nothing_then_apply_records_only_the_mapping(self):
        before = self.state()
        markdown = (self.library.vault / self.note['path']).read_text()
        preview = cover_source(self.library, self.note['id'], covered(decision='现稿已覆盖'),
                               expected_hash=self.note['sha256'])
        self.assertEqual((preview['status'], preview['outcome'], preview['applied']), ('preview', 'new', False))
        self.assertTrue(preview['source_ref'].startswith(COVERED_TYPE + ':'))
        self.assertEqual(self.state(), before)
        result = cover_source(self.library, self.note['id'], covered(decision='现稿已覆盖'),
                              expected_hash=self.note['sha256'], apply=True, expected_token=preview['token'])
        self.assertEqual((result['status'], result['readback_verified']), ('completed', True))
        after = self.state()
        entry = after['sources'][preview['source_ref']]
        self.assertEqual(entry['id'], self.note['id'])
        self.assertEqual(entry['source']['conversation_id'], 'conv-synthetic-1')
        self.assertEqual(entry['source']['decision'], '现稿已覆盖')
        self.assertEqual(entry['source']['evidence'][0]['source_message_id'], 'msg-a')
        self.assertEqual(after['notes'], before['notes'])
        self.assertEqual(after['publications'], before['publications'])
        self.assertEqual((self.library.vault / self.note['path']).read_text(), markdown)
        self.assertEqual(self.library.get(self.note['id'])['sha256'], self.note['sha256'])
        provenance = note_sources(self.library, self.note['id'])['sources']
        self.assertEqual([row['source_ref'] for row in provenance], [preview['source_ref']])
        self.assertEqual(provenance[0]['source']['type'], COVERED_TYPE)

    def test_same_source_again_is_unchanged_and_evidence_order_does_not_matter(self):
        _, first = self.apply(self.note, covered(messages=('msg-a', 'msg-b')))
        before = self.state()
        preview, again = self.apply(self.note, covered(messages=('msg-b', 'msg-a')))
        self.assertEqual(preview['outcome'], 'existing')
        self.assertEqual(preview['source_ref'], first['source_ref'])
        self.assertEqual(again['status'], 'unchanged')
        self.assertEqual(self.state(), before)

    def test_workbench_reference_fields_are_kept_without_changing_identity(self):
        plain = covered()
        detailed = covered(input_source={'account_key': 'account-synthetic', 'conversation_id': 'conv-synthetic-1',
                                         'object_path': 'objects/synthetic.json'})
        detailed['evidence'][0] |= {'id': 'm001', 'primary': True}
        first = cover_source(self.library, self.note['id'], plain, expected_hash=self.note['sha256'])
        preview, _ = self.apply(self.note, detailed)
        self.assertEqual(preview['source_ref'], first['source_ref'])
        stored = self.state()['sources'][preview['source_ref']]['source']
        self.assertEqual((stored['evidence'][0]['id'], stored['evidence'][0]['primary']), ('m001', True))
        self.assertEqual(stored['input_source']['object_path'], 'objects/synthetic.json')
        mismatch = covered(input_source={'conversation_id': 'conv-other'})
        with self.assertRaisesRegex(LibraryError, 'different account or conversation'):
            cover_source(self.library, self.note['id'], mismatch, expected_hash=self.note['sha256'])

    def test_same_source_for_another_note_is_refused(self):
        self.apply(self.note, covered())
        with self.assertRaisesRegex(LibraryError, 'already registered to another note'):
            cover_source(self.library, self.other['id'], covered(), expected_hash=self.other['sha256'])

    def test_related_registrations_are_reported(self):
        self.apply(self.note, covered(messages=('msg-a',)))
        preview = cover_source(self.library, self.other['id'], covered(messages=('msg-a', 'msg-c')),
                               expected_hash=self.other['sha256'])
        related = preview['related_sources']
        self.assertEqual(len(related), 1)
        self.assertEqual((related[0]['note_id'], related[0]['same_conversation'], related[0]['matched_message_ids']),
                         (self.note['id'], True, ['msg-a']))

    def test_stale_hash_or_token_is_refused(self):
        preview = cover_source(self.library, self.note['id'], covered(), expected_hash=self.note['sha256'])
        with self.assertRaisesRegex(LibraryError, 'Revision conflict'):
            cover_source(self.library, self.note['id'], covered(), expected_hash='0' * 64)
        with self.assertRaisesRegex(LibraryError, 'requires the token'):
            cover_source(self.library, self.note['id'], covered(), expected_hash=self.note['sha256'], apply=True)
        with self.assertRaisesRegex(LibraryError, 'preview again'):
            cover_source(self.library, self.note['id'], covered(decision='不同'), expected_hash=self.note['sha256'],
                         apply=True, expected_token=preview['token'])
        edited = self.library.replace(self.note['id'], self.library.get(self.note['id'])['markdown'] + '\n补一句。\n',
                                      expected_hash=self.note['sha256'])
        with self.assertRaisesRegex(LibraryError, 'Revision conflict'):
            cover_source(self.library, self.note['id'], covered(), expected_hash=self.note['sha256'],
                         apply=True, expected_token=preview['token'])
        self.assertEqual(self.state()['sources'], {})
        self.assertNotEqual(edited['sha256'], self.note['sha256'])

    def test_invalid_sources_are_refused(self):
        cases = [
            {**covered(), 'type': 'other-type'},
            {**covered(), 'unexpected': 1},
            {**covered(), 'evidence': []},
            {**covered(), 'evidence': [{'source_message_id': 'm', 'role': 'user', 'text_sha256': 'short'}]},
            {**covered(), 'evidence': [{'source_message_id': 'm', 'role': 'assistant', 'text_sha256': sha('x')}]},
            {**covered(messages=('m', 'm'))},
            {**covered(), 'conversation_id': ''},
        ]
        for source in cases:
            with self.subTest(source=source), self.assertRaises(LibraryError):
                cover_source(self.library, self.note['id'], source, expected_hash=self.note['sha256'])

    def test_post_is_refused(self):
        post = self.library.create('合成发表稿。', title='合成发表稿', kind='post')
        with self.assertRaisesRegex(LibraryError, 'not on a Post'):
            cover_source(self.library, post['id'], covered(), expected_hash=post['sha256'])

    def test_import_source_is_not_overwritten_and_stays_distinct(self):
        imported = self.library.import_note('合成导入正文。', source_id='chatgpt-workbench:synthetic-item',
                                            title='合成导入', source={'conversation_id': 'conv-synthetic-1'})
        preview, _ = self.apply(imported, covered())
        self.assertEqual(preview['related_sources'][0]['source_ref'], 'chatgpt-workbench:synthetic-item')
        sources = self.state()['sources']
        self.assertEqual(sources['chatgpt-workbench:synthetic-item']['source'], {'conversation_id': 'conv-synthetic-1'})
        self.assertEqual(sources[preview['source_ref']]['id'], imported['id'])

    def test_catalog_backed_work_keeps_catalog_and_hash(self):
        from markdown_library.core import _hash, _render
        from markdown_library.work_catalog import backing_path, build_catalog, catalog_entry, row_from_note
        state = self.library._read_state()
        state['work_catalog'] = {'schema_version': 1, 'path': 'works/作品总表.md', 'rows': {}}
        metadata = {'id': 'work-synthetic', 'kind': 'work', 'schema_version': 1, 'title': '合成作品', 'type': 'game'}
        markdown = _render(metadata, '')
        work = {'id': 'work-synthetic', 'path': 'works/work-synthetic.md', 'properties': metadata,
                'markdown': markdown, 'sha256': _hash(markdown)}
        state['work_catalog']['rows'][work['id']] = catalog_entry(work, None)
        state['notes'][work['id']] = {'path': backing_path(state, work['id']), 'history': [],
                                      'kind': 'work', 'deleted': False}
        self.library._atomic(state['work_catalog']['path'], build_catalog([row_from_note(work, None)]))
        self.library._save(state)
        current = self.library.get(work['id'])
        catalog = (self.library.vault / 'works/作品总表.md').read_bytes()
        preview, result = self.apply(current, covered())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((self.library.vault / 'works/作品总表.md').read_bytes(), catalog)
        self.assertEqual(self.library.get(work['id'])['sha256'], current['sha256'])
        self.assertEqual(self.state()['sources'][preview['source_ref']]['id'], work['id'])

    def run_cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(['--vault', str(self.library.vault), *args])
        return code, json.loads(out.getvalue() or err.getvalue())

    def test_cli_preview_then_apply(self):
        source = Path(self.tmp.name) / 'source.json'
        source.write_text(json.dumps(covered()))
        code, schema = self.run_cli('cover-source', '--schema')
        self.assertEqual((code, schema['type']), (0, COVERED_TYPE))
        code, preview = self.run_cli('cover-source', self.note['id'], '--source', str(source),
                                     '--expected-hash', self.note['sha256'])
        self.assertEqual((code, preview['status']), (0, 'preview'))
        self.assertEqual(self.state()['sources'], {})
        code, result = self.run_cli('cover-source', self.note['id'], '--source', str(source),
                                    '--expected-hash', self.note['sha256'], '--apply',
                                    '--expected-token', preview['token'])
        self.assertEqual((code, result['status']), (0, 'completed'))
        code, error = self.run_cli('cover-source', self.note['id'], '--source', str(source))
        self.assertEqual(code, 2)
        self.assertIn('expected-hash', error['error'])


if __name__ == '__main__':
    unittest.main()
