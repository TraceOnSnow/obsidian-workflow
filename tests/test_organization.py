import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from markdown_library import Library, LibraryError
from markdown_library.cli import dispatch, parser
from markdown_library.core import _hash, _render
from markdown_library.mcp_actions import LibraryActions
from markdown_library.navigation import refresh_navigation
from markdown_library.note_queries import note_sources, source_messages
from markdown_library.organize import organize


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.vault = self.root / 'vault'
        self.library = Library.initialize(self.vault)
        self.a = self.library.create('A', title='A', path='00_收件箱/A.md')
        self.b = self.library.create('B', title='B', path='00_收件箱/B.md')
        (self.vault / '00_收件箱/A.md').write_text(self.a['markdown'] + '\n[[00_收件箱/B|B]]\n')
        self.a = self.library.get(self.a['id'])

    def snapshot(self):
        return {str(p.relative_to(self.vault)): p.read_bytes() for p in self.vault.rglob('*') if p.is_file()}

    def request(self):
        return {'operation': 'move', 'moves': [
            {'note_id': n['id'], 'destination': '25_游戏设计/' + Path(n['path']).name, 'expected_hash': n['sha256']}
            for n in [self.a, self.b]]}

    def test_preview_is_read_only_and_batch_move_repairs_cross_links(self):
        before = self.snapshot()
        request = self.request()
        preview = organize(self.library, request)
        self.assertEqual(before, self.snapshot())
        result = organize(self.library, request, apply=True, expected_token=preview['token'])
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['readback_verified'])
        self.assertEqual(len(result['moves']), 2)
        self.assertIn('[[25_游戏设计/B|B]]', self.library.get(self.a['id'])['markdown'])
        self.assertEqual(self.library.get(self.b['id'])['markdown'], self.b['markdown'])
        self.assertFalse(result['publication_selected'])
        self.assertFalse((self.vault / '25_游戏设计/实时目录.base').exists())
        self.assertFalse(json.loads((self.vault / '.markdown-library/state.json').read_text())['publications'])

    def test_invalid_later_move_leaves_the_entire_batch_untouched(self):
        request = self.request()
        request['moves'][1]['destination'] = 'config/forbidden.md'
        before = self.snapshot()
        with self.assertRaises(LibraryError):
            organize(self.library, request, apply=True, expected_token='not-a-token')
        self.assertEqual(before, self.snapshot())

    def test_short_title_and_filename_preserve_body_and_other_properties(self):
        request = self.request()
        request['moves'][0].update(destination='25_游戏设计/Short.md', title='Short')
        request['moves'][1].update(destination=self.b['path'], title='Short B')
        preview = organize(self.library, request)
        result = organize(self.library, request, apply=True, expected_token=preview['token'])
        self.assertTrue(result['readback_verified'])
        self.assertTrue(all(row['body_unchanged'] for row in result['moves']))
        for original, title in [(self.a, 'Short'), (self.b, 'Short B')]:
            saved = self.library.get(original['id'])
            self.assertEqual(saved['properties'], original['properties'] | {'title': title})
        self.assertTrue((self.vault / self.b['path']).exists())

    def test_token_binds_new_backlinks_and_prevents_partial_moves(self):
        request = self.request()
        preview = organize(self.library, request)
        (self.vault / 'reference.md').write_text('[[00_收件箱/A]]\n')
        before = self.snapshot()
        with self.assertRaisesRegex(LibraryError, 'preview changed'):
            organize(self.library, request, apply=True, expected_token=preview['token'])
        self.assertEqual(before, self.snapshot())

    def test_batch_rejects_destination_parent_collisions_before_any_write(self):
        request = self.request()
        request['moves'][1]['destination'] = request['moves'][0]['destination'] + '/B.md'
        before = self.snapshot()
        with self.assertRaisesRegex(LibraryError, 'parent'):
            organize(self.library, request)
        self.assertEqual(before, self.snapshot())

    def test_stale_hash_and_occupied_or_duplicate_destinations(self):
        cases = []
        request = self.request(); request['moves'][0]['expected_hash'] = 'stale'; cases.append(request)
        request = self.request(); request['moves'][1]['destination'] = request['moves'][0]['destination']; cases.append(request)
        request = self.request(); request['moves'][0]['destination'] = self.b['path']; cases.append(request)
        for request in cases:
            with self.subTest(request=request), self.assertRaises(LibraryError):
                organize(self.library, request)
        self.assertTrue((self.vault / self.a['path']).exists())
        self.assertTrue((self.vault / self.b['path']).exists())

    def test_merge_preview_and_apply_keep_recoverable_source(self):
        request = {'operation': 'merge', 'target_id': self.a['id'], 'source_ids': [self.b['id']],
                   'markdown': self.a['markdown'] + '\nB details\n',
                   'expected_hashes': {n['id']: n['sha256'] for n in [self.a, self.b]}}
        before = self.snapshot()
        preview = organize(self.library, request)
        self.assertEqual(before, self.snapshot())
        result = organize(self.library, request, apply=True, expected_token=preview['token'])
        self.assertTrue(result['readback_verified'])
        self.assertTrue(result['sources_recoverable'])
        self.assertEqual(result['source_ids'], [self.b['id']])
        self.assertIn('B details', self.library.get(self.a['id'])['markdown'])
        self.assertTrue((self.vault / '.markdown-library/trash' / (self.b['id'] + '.md')).is_file())

    def test_custom_navigation_does_not_block_other_directories_or_change_base_layout(self):
        (self.vault / '首页.md').write_text('My home')
        topic = self.vault / '25_游戏设计'; topic.mkdir()
        (topic / '实时目录.base').write_text('My base layout')
        request = self.request(); preview = organize(self.library, request)
        result = organize(self.library, request, apply=True, expected_token=preview['token'])
        self.assertEqual(result['navigation']['state'], 'refreshed')
        self.assertEqual((topic / '实时目录.base').read_text(), 'My base layout')
        self.assertEqual((self.vault / '首页.md').read_text(), 'My home')
        self.assertFalse((topic / '索引.md').exists())
        refreshed = refresh_navigation(self.library)
        self.assertIn('首页.md', refreshed['preserved'])
        self.assertFalse((self.vault / 'Blogs').exists())

    def test_cli_schema_requires_no_vault_initialization(self):
        missing = self.root / 'missing'
        result = subprocess.run([sys.executable, '-m', 'markdown_library', '--vault', str(missing),
                                 'move', '--schema'], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('oneOf', json.loads(result.stdout))
        self.assertFalse(missing.exists())

    def test_cli_preview_and_apply_in_different_processes_with_virtual_works(self):
        from markdown_library.work_catalog import backing_path, build_catalog, catalog_entry, row_from_note
        state = self.library._read_state()
        state['work_catalog'] = {'schema_version': 1, 'path': 'works/作品总表.md', 'rows': {}}
        properties = {'id': 'virtual-work', 'kind': 'work', 'schema_version': 1,
                      'title': 'Virtual game', 'type': 'game', 'rating': 85, 'status': 'completed'}
        markdown = _render(properties, '')
        work = {'id': properties['id'], 'path': 'works/virtual.md', 'properties': properties,
                'markdown': markdown, 'sha256': _hash(markdown)}
        state['work_catalog']['rows'][work['id']] = catalog_entry(work, None)
        state['notes'][work['id']] = {'path': backing_path(state, work['id']), 'history': [],
                                     'kind': 'work', 'deleted': False}
        self.library._atomic(state['work_catalog']['path'], build_catalog([row_from_note(work, None)]))
        self.library._save(state)
        request_path = self.root / 'process-request.json'; request_path.write_text(json.dumps(self.request()))
        command = [sys.executable, '-m', 'markdown_library', '--vault', str(self.vault),
                   'organize', '--request', str(request_path)]
        previews = []
        for seed in ('1', '2'):
            completed = subprocess.run(command, env=os.environ | {'PYTHONHASHSEED': seed},
                                       capture_output=True, text=True, timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            previews.append(json.loads(completed.stdout))
        self.assertEqual(previews[0]['token'], previews[1]['token'])
        applied = subprocess.run(command + ['--apply', '--expected-token', previews[0]['token']],
                                 env=os.environ | {'PYTHONHASHSEED': '3'}, capture_output=True, text=True, timeout=15)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue(json.loads(applied.stdout)['readback_verified'])

    def test_cli_moves_and_navigation_require_review_token(self):
        request_path = self.root / 'request.json'
        request_path.write_text(json.dumps(self.request()))
        args = ['--vault', str(self.vault), 'move', '--request', str(request_path)]
        preview = dispatch(parser().parse_args(args))
        with self.assertRaisesRegex(LibraryError, 'token'):
            dispatch(parser().parse_args(args + ['--apply']))
        result = dispatch(parser().parse_args(args + ['--apply', '--expected-token', preview['token']]))
        self.assertTrue(result['readback_verified'])
        nav_args = ['--vault', str(self.vault), 'navigation', '--scope', '25_游戏设计']
        nav_preview = dispatch(parser().parse_args(nav_args))
        refreshed = dispatch(parser().parse_args(nav_args + ['--apply', '--expected-token', nav_preview['token']]))
        self.assertEqual(refreshed['state'], 'refreshed')

    def test_provenance_and_recorded_messages_are_scoped_bounded_and_read_only(self):
        job = 'a' * 64
        note = self.library.import_note('Original', source_id='chat:test', title='Source',
                                       source={'type': 'chatgpt-workbench', 'job_id': job, 'secret': 'never-expose'})
        workspace = self.root / 'workbench'
        job_path = workspace / 'jobs' / job; job_path.mkdir(parents=True)
        (job_path / 'input.json').write_text(json.dumps({'messages': [
            {'id': 'm001', 'role': 'user', 'text': 'x' * 2500},
            {'id': 'm002', 'role': 'assistant', 'text': 'suggestion'}]}))
        before = self.snapshot()
        sources = note_sources(self.library, note['id'])
        self.assertNotIn('never-expose', json.dumps(sources))
        first = source_messages(self.library, note['id'], workbench=workspace, role='user', max_chars=1000)
        self.assertTrue(first['messages'][0]['text_truncated'])
        self.assertEqual(first['next'], {'offset': 0, 'text_offset': 1000})
        last = source_messages(self.library, note['id'], workbench=workspace, role='user',
                               offset=0, text_offset=2000, limit=1, max_chars=1000)
        self.assertIsNone(last['next'])
        self.assertEqual(len(last['messages'][0]['text']), 500)
        with self.assertRaises(LibraryError):
            source_messages(self.library, self.a['id'], workbench=workspace, job_id=job)
        missing = source_messages(self.library, self.a['id'], workbench=workspace)
        self.assertFalse(missing['source_available'])
        self.assertFalse(missing['selection_required'])
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
