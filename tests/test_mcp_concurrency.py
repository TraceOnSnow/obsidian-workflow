"""MCP tools run off the event loop, keep their schemas, and still serialize writes."""
import asyncio
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.base import Tool

from markdown_library import Library
from markdown_library.mcp_server import build_server

SLOW = 0.6


class MCPConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.library = Library.initialize(self.vault)

    async def test_slow_tool_does_not_block_other_calls_or_the_loop(self):
        server = build_server(self.vault)
        loop_thread = threading.get_ident()
        worker_threads = []

        def slow_search(library, query='', **_):
            worker_threads.append(threading.get_ident())
            time.sleep(SLOW)
            return {'query': query, 'notes': []}

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        self.addCleanup(beat.cancel)
        with mock.patch('markdown_library.note_queries.search_notes', slow_search):
            start = time.monotonic()
            first = asyncio.create_task(server.call_tool('search_notes', {'query': 'a'}))
            second = asyncio.create_task(server.call_tool('search_notes', {'query': 'b'}))
            await asyncio.sleep(0.05)
            workflow = await server.call_tool('get_workflow', {})
            fast_done = time.monotonic() - start
            results = await asyncio.gather(first, second)
            elapsed = time.monotonic() - start

        self.assertLess(fast_done, SLOW / 2, 'a quick tool waited for the slow one')
        self.assertLess(elapsed, SLOW * 1.6, 'two slow calls ran one after another')
        self.assertGreaterEqual(elapsed, SLOW * 0.9)
        self.assertGreater(ticks, int(SLOW / 0.02) // 2, 'event loop was blocked')
        self.assertNotIn(loop_thread, worker_threads)
        self.assertEqual({r[1]['query'] for r in results}, {'a', 'b'})
        self.assertFalse(workflow[1]['website_publication_available'])

    async def test_error_mapping_survives_the_worker_thread(self):
        server = build_server(self.vault)
        with self.assertRaisesRegex(ToolError, 'Note is missing or deleted'):
            await server.call_tool('get_note', {'note_id': 'absent'})

        def failing(*_args, **_kwargs):
            raise OSError('/secret/server/path')

        with mock.patch('markdown_library.note_queries.search_notes', failing):
            with self.assertRaises(ToolError) as caught:
                await server.call_tool('search_notes', {'query': 'x'})
        self.assertIn('Library operation failed', str(caught.exception))
        self.assertNotIn('/secret/server/path', str(caught.exception))

    async def test_tool_schemas_match_the_plain_sync_functions(self):
        for read_only, count in ((False, 12), (True, 5)):
            server = build_server(self.vault, read_only=read_only)
            tools = server._tool_manager.list_tools()
            self.assertEqual(len(tools), count)
            for tool in tools:
                original = tool.fn.__wrapped__
                self.assertTrue(tool.is_async, tool.name)
                self.assertFalse(asyncio.iscoroutinefunction(original), tool.name)
                # What the previous sync decorator registered: the same function,
                # called directly on the loop.
                before = Tool.from_function(original, title=tool.title,
                                            annotations=tool.annotations, structured_output=True)
                self.assertFalse(before.is_async)
                self.assertEqual(tool.name, before.name)
                self.assertEqual(tool.description, before.description)
                self.assertEqual(tool.parameters, before.parameters, tool.name)
                self.assertEqual(tool.output_schema, before.output_schema, tool.name)
                self.assertEqual(tool.context_kwarg, before.context_kwarg)
            listed = {t.name: t for t in await server.list_tools()}
            self.assertEqual(set(listed), {t.name for t in tools})
            for tool in tools:
                self.assertEqual(listed[tool.name].inputSchema, tool.parameters)

    async def test_concurrent_writes_still_serialize_on_the_vault_lock(self):
        server = build_server(self.vault)
        _, created = await server.call_tool('create_note', {
            'title': '并发', 'markdown': '# 并发\n\n初稿。\n', 'source_id': 'test:concurrency'})
        current = self.library.get(created['id'])

        async def replace(index):
            markdown = current['markdown'].replace('初稿。', f'第{index}稿。')
            try:
                _, saved = await server.call_tool('replace_note', {
                    'note_id': current['id'], 'markdown': markdown,
                    'expected_hash': current['sha256']})
                return markdown, saved
            except ToolError as exc:
                self.assertIn('Revision conflict', str(exc))
                return None

        outcomes = await asyncio.gather(*(replace(i) for i in range(8)))
        winners = [o for o in outcomes if o is not None]
        self.assertEqual(len(winners), 1, 'stale hashes must not all succeed')
        final = self.library.get(current['id'])
        self.assertEqual(final['markdown'], winners[0][0])
        self.assertEqual(final['sha256'], winners[0][1]['sha256'])

        retries = await asyncio.gather(*(server.call_tool('create_note', {
            'title': '重复', 'markdown': '同一来源\n', 'source_id': 'test:same-source'}) for _ in range(6)))
        self.assertEqual(len({saved['id'] for _, saved in retries}), 1)
        self.assertEqual(len([n for n in self.library.list_notes() if n['markdown'].endswith('同一来源\n')]), 1)


if __name__ == '__main__':
    unittest.main()
