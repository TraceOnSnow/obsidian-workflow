import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from markdown_library import Library
from markdown_library.mcp_server import build_server


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.library = Library.initialize(self.vault)

    def params(self, *extra):
        return StdioServerParameters(command=sys.executable,
            args=['-m', 'markdown_library.mcp_server', '--vault', str(self.vault), *extra],
            env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})

    async def call(self, session, name, arguments=None, error=False):
        result = await session.call_tool(name, arguments or {})
        self.assertEqual(result.isError, error, result.content)
        if error:
            return result
        self.assertIsInstance(result.structuredContent, dict)
        return result.structuredContent

    async def test_real_stdio_read_create_edit_conflict_history_restore_and_idempotency(self):
        async with stdio_client(self.params()) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                init = await session.initialize()
                self.assertIn('get_workflow', init.instructions)
                tools = (await session.list_tools()).tools
                self.assertEqual(len(tools), 9)
                self.assertFalse(any('publish' in t.name or 'shell' in t.name for t in tools))
                self.assertLess(len(json.dumps([t.model_dump(exclude_none=True) for t in tools])), 19000)
                annotations = {t.name: t.annotations for t in tools}
                self.assertTrue(annotations['get_note'].readOnlyHint)
                self.assertFalse(annotations['replace_note'].readOnlyHint)
                workflow = await self.call(session, 'get_workflow')
                self.assertFalse(workflow['website_publication_available'])
                created = await self.call(session, 'create_note', {
                    'title': '带中文的真实协议测试', 'markdown': '# 标题\n\n第一版背景。\n', 'source_id': 'test:001'})
                note_id = created['id']
                note = await self.call(session, 'get_note', {'note_id': note_id})
                updated = note['markdown'].replace('第一版背景。', '保留背景，补充第二版细节。')
                saved = await self.call(session, 'replace_note', {
                    'note_id': note_id, 'markdown': updated, 'expected_hash': note['sha256']})
                self.assertEqual(self.library.get(note_id)['markdown'], updated)
                await self.call(session, 'replace_note', {
                    'note_id': note_id, 'markdown': note['markdown'], 'expected_hash': note['sha256']}, error=True)
                retry = await self.call(session, 'create_note', {
                    'title': 'different', 'markdown': 'must not replace', 'source_id': 'test:001'})
                self.assertEqual(retry['id'], note_id)
                self.assertEqual(retry['sha256'], saved['sha256'])
                hits = await self.call(session, 'search_notes', {'query': '第二版', 'kind': 'note'})
                self.assertEqual(hits['total'], 1)
                self.assertNotIn('markdown', hits['items'][0])
                versions = await self.call(session, 'note_history', {'note_id': note_id})
                self.assertGreaterEqual(len(versions['history']), 2)
                await self.call(session, 'restore_note', {
                    'note_id': note_id, 'revision': note['sha256'], 'expected_hash': saved['sha256']})
                self.assertEqual(self.library.get(note_id)['markdown'], note['markdown'])
                moved = await self.call(session, 'move_note', {'note_id': note_id,
                    'destination': '20_项目与构想/协议验收/主稿.md', 'expected_hash': note['sha256']})
                self.assertEqual(moved['navigation']['state'], 'refreshed')
                navigation = (self.vault / '20_项目与构想/协议验收/索引.md').read_text()
                self.assertIn('![[20_项目与构想/协议验收/实时目录.base]]', navigation)
                self.assertNotIn('主稿', navigation)
                source = await self.call(session, 'create_note', {'title': '合并来源',
                    'markdown': '来源独有细节。', 'source_id': 'test:002'})
                merged = await self.call(session, 'merge_notes', {'target_id': note_id,
                    'source_ids': [source['id']], 'markdown': note['markdown'] + '\n来源独有细节。\n',
                    'expected_hashes': {note_id: moved['sha256'], source['id']: source['sha256']}})
                self.assertIn('来源独有细节', self.library.get(note_id)['markdown'])
                self.assertEqual(merged['sources'][0]['id'], source['id'])
                state = json.loads((self.vault / '.markdown-library/state.json').read_text())
                self.assertFalse(state['publications'])

    async def test_saved_move_survives_user_navigation_conflict(self):
        note = self.library.create('body', title='keep')
        (self.vault / '首页.md').write_text('用户自己的首页，不能覆盖。')
        async with stdio_client(self.params()) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await self.call(session, 'move_note', {'note_id': note['id'],
                    'destination': '10_个人记录/keep.md', 'expected_hash': note['sha256']})
                self.assertEqual(result['navigation']['state'], 'needs_refresh')
                self.assertEqual(self.library.get(note['id'])['path'], '10_个人记录/keep.md')
                self.assertEqual((self.vault / '首页.md').read_text(), '用户自己的首页，不能覆盖。')

    async def test_hidden_paths_forged_identity_and_read_only_transport(self):
        note = self.library.create('private', title='原稿')
        async with stdio_client(self.params()) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                await self.call(session, 'search_notes', {'folder': '.markdown-library'}, error=True)
                await self.call(session, 'get_note', {'note_id': '../../.env'}, error=True)
                await self.call(session, 'create_note', {
                    'title': 'bad', 'markdown': '---\nkind: post\n---\nbody', 'source_id': 'bad'}, error=True)
                await self.call(session, 'replace_note', {'note_id': note['id'],
                    'markdown': note['markdown'].replace('kind: note', 'kind: post'),
                    'expected_hash': note['sha256']}, error=True)
                self.assertEqual(len(self.library.list_notes()), 1)
        async with stdio_client(self.params('--read-only')) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                self.assertEqual({t.name for t in (await session.list_tools()).tools},
                    {'get_workflow', 'search_notes', 'get_note', 'note_history'})
                self.assertTrue((await self.call(session, 'get_workflow'))['read_only'])

    async def test_pagination_does_not_return_full_corpus(self):
        self.library.create('full private body', title='a')
        self.library.create('full private body', title='b')
        async with stdio_client(self.params()) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                first = await self.call(session, 'search_notes', {'limit': 1})
                second = await self.call(session, 'search_notes', {'limit': 1, 'offset': first['next_offset']})
                self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
                self.assertNotIn('full private body', json.dumps(first))

    def test_missing_vault_never_initialized(self):
        missing = Path(self.tmp.name) / 'missing'
        with self.assertRaises(Exception):
            build_server(missing)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
