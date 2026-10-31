#!/usr/bin/env python3
"""Read-only stdio acceptance against an explicitly chosen initialized Vault."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def verify(vault):
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'markdown_library.mcp_server', '--vault', str(vault), '--read-only'])
    async with asyncio.timeout(40):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                names = sorted(t.name for t in tools)
                assert names == ['get_note', 'get_workflow', 'note_history', 'search_notes']
                assert all(t.annotations.readOnlyHint for t in tools)
                workflow = await session.call_tool('get_workflow', {})
                assert not workflow.isError and workflow.structuredContent['read_only']
                hits = await session.call_tool('search_notes', {'kind': 'note', 'limit': 1})
                assert not hits.isError
                result = {'stdio_initialize': True, 'tools': names, 'workflow_loaded': True,
                          'notes_available': hits.structuredContent['total'],
                          'note_readback': False, 'chatgpt_connection_verified': False}
                for item in hits.structuredContent['items']:
                    reply = await session.call_tool('get_note', {'note_id': item['id']})
                    assert not reply.isError
                    note = reply.structuredContent
                    data = (vault / note['path']).read_bytes()
                    assert hashlib.sha256(data).hexdigest() == note['sha256']
                    assert data.decode('utf-8') == note['markdown']
                    result['note_readback'] = True
                return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify(args.vault))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.report.write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
