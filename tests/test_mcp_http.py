import asyncio
import base64
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from markdown_library import Library
from markdown_library.mcp_http import build_http_app, load_http_config, MAX_REQUEST_BYTES
from markdown_library.mcp_oauth import hash_owner_password


class HTTPMCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.library = Library.initialize(self.vault)
        self.password = 'temporary owner test password'
        self.config = {
            'vault': str(self.vault), 'db_path': str(self.root / 'private' / 'oauth.sqlite3'),
            'owner_password_hash': hash_owner_password(self.password),
            'unix_socket': str(self.root / 'mcp.sock'),
        }
        self.app = build_http_app(self.vault, self.config)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url='https://traceonsnow.com')
        self.addAsyncCleanup(self.client.aclose)

    async def grant(self, scope='library:read library:write'):
        callback = 'https://chatgpt.com/connector_platform_oauth_redirect'
        registered = await self.client.post('/library-auth/register', json={
            'client_name': 'Temporary HTTP integration test', 'redirect_uris': [callback],
            'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
            'token_endpoint_auth_method': 'none', 'scope': scope,
        })
        self.assertEqual(registered.status_code, 201, registered.text)
        client_id = registered.json()['client_id']
        verifier = 'a' * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        authorized = await self.client.get('/library-auth/authorize', params={
            'response_type': 'code', 'client_id': client_id, 'redirect_uri': callback,
            'scope': scope, 'state': 'integration-test-state', 'code_challenge': challenge,
            'code_challenge_method': 'S256', 'resource': 'https://traceonsnow.com/library-mcp',
        })
        self.assertIn(authorized.status_code, [302, 303], authorized.text)
        login = await self.client.get(authorized.headers['location'])
        self.assertEqual(login.status_code, 200, login.text)
        # no-referrer would turn a real browser form POST's Origin into null.
        self.assertEqual(set(login.headers.get_list('referrer-policy')), {'same-origin'})
        csp = login.headers['content-security-policy']
        self.assertIn("form-action 'self' " + callback + ';', csp)
        self.assertNotIn('form-action *', csp)
        request = re.search(r'name="request" value="([^"]+)"', login.text).group(1)
        csrf = re.search(r'name="csrf" value="([^"]+)"', login.text).group(1)
        form = {
            'request': request, 'csrf': csrf, 'password': self.password, 'decision': 'allow',
        }
        # An opaque or cross-site origin must remain denied, even with valid
        # cookie/CSRF/password. Only the page policy is corrected, not the guard.
        for origin in ('null', 'https://attacker.example'):
            denied = await self.client.post('/library-auth/login', data=form,
                headers={'Origin': origin})
            self.assertEqual(denied.status_code, 403)
        approved = await self.client.post('/library-auth/login', data=form,
            headers={'Origin': 'https://traceonsnow.com'})
        self.assertEqual(approved.status_code, 303, approved.text)
        self.assertEqual(approved.headers['content-security-policy'], csp)
        query = parse_qs(urlsplit(approved.headers['location']).query)
        self.assertEqual(query['state'], ['integration-test-state'])
        exchanged = await self.client.post('/library-auth/token', data={
            'grant_type': 'authorization_code', 'code': query['code'][0],
            'client_id': client_id, 'redirect_uri': callback, 'code_verifier': verifier,
            'resource': 'https://traceonsnow.com/library-mcp',
        })
        self.assertEqual(exchanged.status_code, 200, exchanged.text)
        self.assertEqual(exchanged.json()['scope'], scope)
        return exchanged.json()

    async def test_discovery_anonymous_auth_challenge_and_health(self):
        discovery = await self.client.get('/.well-known/oauth-authorization-server/library-auth')
        self.assertEqual(discovery.status_code, 200)
        metadata = discovery.json()
        self.assertEqual(metadata['issuer'], 'https://traceonsnow.com/library-auth')
        self.assertEqual(metadata['authorization_endpoint'], 'https://traceonsnow.com/library-auth/authorize')
        self.assertEqual(metadata['token_endpoint'], 'https://traceonsnow.com/library-auth/token')
        self.assertEqual(metadata['registration_endpoint'], 'https://traceonsnow.com/library-auth/register')
        mounted = await self.client.get('/library-auth/.well-known/oauth-authorization-server')
        self.assertEqual(mounted.json(), metadata)
        resource = await self.client.get('/.well-known/oauth-protected-resource/library-mcp')
        self.assertEqual(resource.json()['resource'], 'https://traceonsnow.com/library-mcp')
        self.assertEqual(resource.json()['authorization_servers'], ['https://traceonsnow.com/library-auth'])
        self.assertEqual(resource.json()['scopes_supported'], ['library:read', 'library:write'])
        for headers in ({}, {'Authorization': 'Bearer invalid'}):
            denied = await self.client.post('/library-mcp', headers=headers, json={})
            self.assertEqual(denied.status_code, 401)
            self.assertIn('/.well-known/oauth-protected-resource/library-mcp', denied.headers['www-authenticate'])
        health = await self.client.get('/library-auth/healthz')
        self.assertEqual(health.json(), {'status': 'ok'})
        self.assertEqual(health.headers['cache-control'], 'no-store')

    async def test_request_host_origin_and_size_guards(self):
        for route in ('/library-mcp', '/library-auth/register', '/library-auth/healthz'):
            response = await self.client.get(route, headers={'Host': 'attacker.example'})
            self.assertEqual(response.status_code, 421)
            response = await self.client.get(route, headers={'Origin': 'https://attacker.example'})
            self.assertEqual(response.status_code, 403)
        self.assertEqual((await self.client.get('/library-auth/healthz',
            headers={'Origin': 'https://traceonsnow.com'})).status_code, 200)
        too_large = await self.client.post('/library-auth/token', content=b'a' * (MAX_REQUEST_BYTES + 1))
        self.assertEqual(too_large.status_code, 413)

        async def chunked():
            yield b'a' * MAX_REQUEST_BYTES
            yield b'b'
        too_large = await self.client.post('/library-auth/token', content=chunked())
        self.assertEqual(too_large.status_code, 413)

    async def test_owner_oauth_scope_enforcement_and_canonical_resource(self):
        token = await self.grant(scope='library:read')
        denied = await self.client.post('/library-mcp', headers={
            'Authorization': 'Bearer ' + token['access_token']}, json={})
        self.assertEqual(denied.status_code, 403)
        self.assertIn('insufficient_scope', denied.headers['www-authenticate'])
        self.assertIsNone(await self.app.state.oauth_provider.verify_token('invalid'))
        self.assertFalse(list(self.vault.rglob('oauth.sqlite3')))

    async def test_sdk_refuses_a_verifiers_wrong_resource_token(self):
        from mcp.server.auth.provider import AccessToken

        async def incorrect_verifier(token):
            return AccessToken(token=token, client_id='test', scopes=['library:read', 'library:write'],
                               resource='https://attacker.example/another-mcp')

        self.app.state.oauth_provider.verify_token = incorrect_verifier
        denied = await self.client.post('/library-mcp',
            headers={'Authorization': 'Bearer test-token'}, json={})
        self.assertEqual(denied.status_code, 401)

    async def test_real_http_sdk_initialize_tools_notes_and_stale_hash(self):
        # OAuth browser steps use HTTPS ASGI URLs; actual MCP exchanges below run
        # over a real local HTTP Unix socket, exactly as behind the HTTPS proxy.
        token = await self.grant()
        note = self.library.create('完整原稿背景。', title='临时协议笔记')
        server = uvicorn.Server(uvicorn.Config(self.app, uds=self.config['unix_socket'],
            access_log=False, log_config=None, log_level='warning', proxy_headers=False))
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(500):
                if server.started or task.done():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started, 'Temporary HTTP server did not start')
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.config['unix_socket']),
                    headers={'Authorization': 'Bearer ' + token['access_token']}) as client:
                async with streamable_http_client('http://traceonsnow.com/library-mcp',
                        http_client=client, terminate_on_close=False) as (reader, writer, session_id):
                    async with ClientSession(reader, writer) as session:
                        initialized = await session.initialize()
                        self.assertIn('get_workflow', initialized.instructions)
                        self.assertIsNone(session_id())
                        tools = (await session.list_tools()).tools
                        self.assertEqual(len(tools), 9)
                        for tool in tools:
                            data = tool.model_dump(by_alias=True, exclude_none=True)
                            expected = [{'type': 'oauth2', 'scopes': ['library:read', 'library:write']}]
                            self.assertEqual(data['securitySchemes'], expected)
                            self.assertEqual(data['_meta']['securitySchemes'], expected)
                        workflow = await session.call_tool('get_workflow', {})
                        self.assertFalse(workflow.structuredContent['website_publication_available'])
                        fetched = await session.call_tool('get_note', {'note_id': note['id']})
                        original = fetched.structuredContent
                        self.assertEqual(original['sha256'], note['sha256'])
                        self.assertEqual(hashlib.sha256(original['markdown'].encode()).hexdigest(), original['sha256'])
                        changed = original['markdown'].replace('完整原稿背景。', '保留背景并追加细节。')
                        saved = await session.call_tool('replace_note', {'note_id': note['id'],
                            'markdown': changed, 'expected_hash': original['sha256']})
                        self.assertFalse(saved.isError)
                        stale = await session.call_tool('replace_note', {'note_id': note['id'],
                            'markdown': original['markdown'], 'expected_hash': original['sha256']})
                        self.assertTrue(stale.isError)
                        self.assertEqual(self.library.get(note['id'])['markdown'], changed)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)

    async def test_read_only_server_uses_read_scope_and_no_write_tools(self):
        self.config['read_only'] = True
        readonly = build_http_app(self.vault, self.config)
        scopes = readonly.state.mcp_server.settings.auth.required_scopes
        self.assertEqual(scopes, ['library:read'])
        tools = await readonly.state.mcp_server.list_tools()
        self.assertEqual({tool.name for tool in tools}, {'get_workflow', 'search_notes', 'get_note', 'note_history'})
        for tool in tools:
            self.assertEqual(tool.model_dump(by_alias=True)['securitySchemes'],
                [{'type': 'oauth2', 'scopes': ['library:read']}])

    def test_private_fixed_config_and_invalid_urls(self):
        directory = self.root / 'config'
        directory.mkdir(mode=0o700)
        filename = directory / 'config.json'
        filename.write_text(json.dumps(self.config))
        filename.chmod(0o600)
        self.assertEqual(load_http_config(filename), self.config)
        filename.chmod(0o644)
        with self.assertRaises(ValueError):
            load_http_config(filename)
        filename.chmod(0o600)
        directory.chmod(0o755)
        with self.assertRaises(ValueError):
            load_http_config(filename)
        directory.chmod(0o700)
        inside = self.vault / 'secret.json'
        inside.write_text(json.dumps(self.config))
        inside.chmod(0o600)
        self.vault.chmod(0o700)
        with self.assertRaises(ValueError):
            load_http_config(inside)
        for bad in ('http://traceonsnow.com/library-auth', 'https://traceonsnow.com/',
                    'https://traceonsnow.com/library-auth?secret=value'):
            with self.assertRaises(ValueError):
                build_http_app(self.vault, dict(self.config, issuer_url=bad))
        with self.assertRaises(ValueError):
            build_http_app(self.vault, dict(self.config, db_path=str(self.vault / 'secret.sqlite3')))
        missing = self.root / 'missing'
        with self.assertRaises(Exception):
            build_http_app(missing, self.config)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
