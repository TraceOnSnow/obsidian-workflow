#!/usr/bin/env python3
"""Read-only HTTPS/OAuth acceptance; never prints or saves credentials or note text."""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import secrets
import sys
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

PROJECT = Path(__file__).resolve().parents[1]


class HiddenInputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('type') == 'hidden':
            self.fields[attrs.get('name')] = attrs.get('value', '')


async def verify(config_path, *, native_callback=False, browser_module=None, browser_executable=None):
    if browser_module and not native_callback:
        raise ValueError('Real browser verification requires --native-callback')
    from markdown_library.mcp_http import load_http_config
    from markdown_library.mcp_oauth import PersonalOAuthProvider
    config = load_http_config(config_path)
    resource, issuer = config['resource_url'], config['issuer_url']
    password_file = Path(config_path).parent / 'owner-password'
    if password_file.is_symlink() or password_file.stat().st_mode & 0o077:
        raise ValueError('credential file must be private')
    password = password_file.read_text().strip()
    provider = PersonalOAuthProvider(config['db_path'], issuer, resource,
                                      config['owner_password_hash'], config.get('allowed_redirect_uris', []))
    report = {'https_url': resource, 'callback_mode': 'native_loopback' if native_callback else 'chatgpt_web',
              'chatgpt_connection_verified': False}
    client_id = None
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
        try:
            response = await http.post(resource, json={'jsonrpc': '2.0', 'id': 1,
                'method': 'initialize', 'params': {'protocolVersion': '2025-03-26',
                'capabilities': {}, 'clientInfo': {'name': 'server-acceptance', 'version': '1'}}})
            assert response.status_code == 401
            assert 'oauth-protected-resource' in response.headers.get('www-authenticate', '')
            report['anonymous_denied'] = True
            host = urlsplit(resource)
            origin = f'{host.scheme}://{host.netloc}'
            resource_metadata = (await http.get(origin + '/.well-known/oauth-protected-resource' + host.path)).json()
            assert resource_metadata['resource'] == resource
            assert resource_metadata['authorization_servers'] == [issuer]
            metadata = (await http.get(origin + '/.well-known/oauth-authorization-server' + urlsplit(issuer).path)).json()
            assert metadata['issuer'] == issuer and 'S256' in metadata['code_challenge_methods_supported']
            report['oauth_discovery'] = True
            callback = (('http://127.0.0.1:43123/callback/server-acceptance-' if native_callback else
                         'https://chatgpt.com/connector/oauth/server-acceptance-') + secrets.token_hex(8))
            response = await http.post(metadata['registration_endpoint'], json={
                'client_name': 'Temporary server HTTPS acceptance', 'redirect_uris': [callback],
                'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
                'token_endpoint_auth_method': 'none' if native_callback else 'client_secret_post',
                'scope': 'library:read library:write'})
            assert response.status_code == 201
            client = response.json()
            client_id = client['client_id']
            client_fields = {'client_id': client_id}
            if client.get('client_secret'):
                client_fields['client_secret'] = client['client_secret']
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            state = secrets.token_urlsafe(24)
            response = await http.get(metadata['authorization_endpoint'], params={
                'client_id': client_id, 'redirect_uri': callback, 'response_type': 'code',
                'code_challenge': challenge, 'code_challenge_method': 'S256',
                'scope': 'library:read library:write', 'resource': resource, 'state': state})
            assert response.status_code in (302, 303, 307)
            login_url = response.headers['location']
            assert login_url.startswith(issuer + '/login?')
            if browser_module:
                process = await asyncio.create_subprocess_exec('node',
                    str(PROJECT / 'scripts/library-http-browser.cjs'),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                payload = json.dumps({'module_path': str(browser_module),
                    'executable': str(browser_executable), 'login_url': login_url,
                    'callback': callback, 'password': password}).encode()
                try:
                    output, _ = await asyncio.wait_for(process.communicate(payload), timeout=60)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                assert process.returncode == 0
                browser_result = json.loads(output)
                location = browser_result['location']
                assert browser_result['canonical_post_origin'] and browser_result['referer_absent']
                report['real_browser_consent'] = True
                report['canonical_form_origin'] = True
                report['cross_origin_callback_referer_absent'] = True
                report['consistent_referrer_policy'] = browser_result['consistent_referrer_policy']
            else:
                page = await http.get(login_url)
                assert page.status_code == 200
                form = HiddenInputs()
                form.feed(page.text)
                response = await http.post(issuer + '/login', data={**form.fields, 'password': password,
                                          'decision': 'allow'}, headers={'Origin': origin})
                assert response.status_code == 303
                location = response.headers['location']
            assert location.startswith(callback + '?')
            result = parse_qs(urlsplit(location).query)
            assert result['state'] == [state]
            response = await http.post(metadata['token_endpoint'], data={**client_fields,
                'grant_type': 'authorization_code', 'code': result['code'][0],
                'redirect_uri': callback, 'code_verifier': verifier, 'resource': resource})
            assert response.status_code == 200
            token = response.json()
            report['owner_consent_pkce'] = True
            response = await http.post(metadata['token_endpoint'], data={**client_fields,
                'grant_type': 'refresh_token', 'refresh_token': token['refresh_token'], 'resource': resource})
            assert response.status_code == 200
            refreshed = response.json()
            response = await http.get(resource, headers={'Authorization': 'Bearer ' + token['access_token']})
            assert response.status_code == 401
            report['refresh_rotates_and_invalidates_old_access'] = True
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + refreshed['access_token']},
                                         timeout=30) as authorized:
                async with streamable_http_client(resource, http_client=authorized) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        report['tools'] = sorted(tool.name for tool in tools.tools)
                        for tool in tools.tools:
                            assert tool.model_dump().get('securitySchemes')
                        workflow = await session.call_tool('get_workflow', {})
                        assert not workflow.isError and not workflow.structuredContent['website_publication_available']
                        results = await session.call_tool('search_notes', {'limit': 1, 'kind': 'note'})
                        assert not results.isError and results.structuredContent['items']
                        item = results.structuredContent['items'][0]
                        note = await session.call_tool('get_note', {'note_id': item['id']})
                        assert not note.isError
                        data = note.structuredContent
                        disk = (Path(config['vault']) / data['path']).read_bytes()
                        assert disk == data['markdown'].encode('utf-8')
                        assert hashlib.sha256(disk).hexdigest() == data['sha256']
                        report['mcp_initialize_tools_workflow_readback'] = True
                        report['note_sha_matches_disk'] = True
            response = await http.post(metadata['revocation_endpoint'], data={**client_fields,
                'token': refreshed['refresh_token'], 'token_type_hint': 'refresh_token'})
            assert response.status_code == 200
            response = await http.get(resource, headers={'Authorization': 'Bearer ' + refreshed['access_token']})
            assert response.status_code == 401
            report['revocation_denies_access'] = True
        finally:
            if client_id:
                provider.delete_client(client_id)
    report['temporary_client_removed'] = True
    return report


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=PROJECT / 'runtime/library-http/config.json')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--native-callback', action='store_true',
                        help='Verify the native-loopback OAuth flow without following the callback URL')
    parser.add_argument('--browser-module', type=Path,
                        help='Optional absolute path to an existing Playwright module; runs a real browser form')
    parser.add_argument('--browser-executable', type=Path,
                        help='Existing Chromium executable, required with --browser-module')
    args = parser.parse_args()
    try:
        if bool(args.browser_module) != bool(args.browser_executable):
            raise ValueError('Both browser paths are required')
        if args.browser_module and not (args.browser_module.is_absolute() and args.browser_executable.is_absolute()):
            raise ValueError('Browser paths must be absolute')
        report = asyncio.run(verify(args.config, native_callback=args.native_callback,
            browser_module=args.browser_module, browser_executable=args.browser_executable))
    except Exception:
        print(json.dumps({'verified': False, 'error': 'HTTPS/OAuth acceptance failed; no credentials or note text were printed.'}))
        return 1
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        args.report.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
