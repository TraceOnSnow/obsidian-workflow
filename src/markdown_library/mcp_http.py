"""Authenticated, sessionless HTTPS MCP application for one fixed private Vault.

The production process only binds a Unix socket. HTTPS and the public hostname
are supplied by the existing reverse proxy; forwarded headers are never trusted.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .mcp_oauth import PersonalOAuthProvider
from .mcp_diagnostics import record_connection, registration_summary
from .mcp_server import build_server

DEFAULT_ISSUER = 'https://traceonsnow.com/library-auth'
DEFAULT_RESOURCE = 'https://traceonsnow.com/library-mcp'
MAX_REQUEST_BYTES = 1024 * 1024
SCOPES = ['library:read', 'library:write']


def _https_url(value: str, label: str):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.path or parsed.path == '/'
            or parsed.path.endswith('/') or '%' in parsed.path or '..' in parsed.path
            or parsed.port not in (None, 443)):
        raise ValueError(f'{label} must be a canonical HTTPS URL with a fixed non-root path')
    return parsed


class RequestBoundary:
    """Apply the same bounded body, host and browser-origin policy to every route."""

    def __init__(self, app, *, host: str, origin: str,
                 issuer_path='/library-auth', resource_path='/library-mcp'):
        self.app = app
        self.hosts = {host.lower(), host.lower() + ':443'}
        self.origin = origin
        self.issuer_path, self.resource_path = issuer_path, resource_path

    def stage(self, path):
        if path == self.resource_path:
            return 'mcp'
        if path == '/.well-known/oauth-protected-resource' + self.resource_path:
            return 'resource_metadata'
        if path in {'/.well-known/oauth-authorization-server' + self.issuer_path,
                    self.issuer_path + '/.well-known/oauth-authorization-server'}:
            return 'authorization_metadata'
        for endpoint in ['register', 'authorize', 'login', 'token', 'revoke', 'healthz']:
            if path == self.issuer_path + '/' + endpoint:
                return 'health' if endpoint == 'healthz' else endpoint
        return 'other'

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        stage, method = self.stage(scope['path']), scope['method']

        async def reject(message, status, reason):
            record_connection(stage, method, status, reason)
            return await JSONResponse({'error': message}, status_code=status)(scope, receive, send)

        headers = scope.get('headers', [])
        hosts = [v.decode('latin-1').lower() for k, v in headers if k.lower() == b'host']
        origins = [v.decode('latin-1') for k, v in headers if k.lower() == b'origin']
        if len(hosts) != 1 or hosts[0] not in self.hosts:
            return await reject('Invalid host', 421, 'request_boundary_host')
        if origins and (len(origins) != 1 or origins[0] != self.origin):
            return await reject('Browser origin not permitted', 403, 'request_boundary_origin')
        lengths = [v for k, v in headers if k.lower() == b'content-length']
        try:
            if len(lengths) > 1 or (lengths and (int(lengths[0]) < 0 or int(lengths[0]) > MAX_REQUEST_BYTES)):
                raise ValueError()
        except ValueError:
            return await reject('Request body too large or invalid', 413, 'request_boundary_body')
        messages, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            messages.append(message)
            size += len(message.get('body', b''))
            if size > MAX_REQUEST_BYTES:
                return await reject('Request body too large', 413, 'request_boundary_body')
            if not message.get('more_body', False):
                break
        position = 0
        registration = registration_summary(b''.join(m.get('body', b'') for m in messages)) if stage == 'register' else None
        response_status, error_body = 500, bytearray()

        async def bounded_receive():
            nonlocal position
            if position < len(messages):
                message = messages[position]
                position += 1
                return message
            return await receive()

        async def private_send(message):
            nonlocal response_status
            if message['type'] == 'http.response.start':
                response_status = message['status']
                message = dict(message)
                message['headers'] = list(message.get('headers', [])) + [
                    (b'cache-control', b'no-store'),
                    (b'x-content-type-options', b'nosniff'),
                    # no-referrer makes browser form POSTs send Origin: null.
                    # Keep same-origin validation usable without leaking OAuth
                    # request URLs to cross-origin callback destinations.
                    (b'referrer-policy', b'same-origin'),
                ]
            elif message['type'] == 'http.response.body' and response_status >= 400:
                error_body.extend(message.get('body', b'')[:max(0, 4096 - len(error_body))])
            await send(message)

        try:
            await self.app(scope, bounded_receive, private_send)
        finally:
            reason = None
            if error_body:
                try:
                    reason = json.loads(error_body).get('error')
                except (ValueError, AttributeError):
                    reason = 'unhandled'
            record_connection(stage, method, response_status, reason, registration)


def build_http_app(vault: str | Path, config: Mapping[str, Any]) -> Starlette:
    """Build an authenticated HTTP app; constructing it never initializes a Vault."""
    issuer_url = str(config.get('issuer_url', DEFAULT_ISSUER))
    resource_url = str(config.get('resource_url', DEFAULT_RESOURCE))
    issuer = _https_url(issuer_url, 'issuer_url')
    resource = _https_url(resource_url, 'resource_url')
    if (issuer.hostname != resource.hostname or issuer.path == resource.path
            or resource.path.startswith(issuer.path + '/')):
        raise ValueError('Issuer and resource must use nonoverlapping paths on the same HTTPS host')
    vault_path = Path(vault).resolve()
    db_path = Path(config['db_path']).expanduser()
    if not db_path.is_absolute() or db_path.resolve().is_relative_to(vault_path):
        raise ValueError('OAuth state must use an absolute path outside the Vault')
    read_only = config.get('read_only', False)
    if not isinstance(read_only, bool):
        raise ValueError('read_only must be a boolean')
    scopes = ['library:read'] if read_only else list(SCOPES)
    provider = PersonalOAuthProvider(db_path, issuer_url, resource_url,
        config['owner_password_hash'], allowed_redirect_uris=config.get('allowed_redirect_uris', ()))
    server = build_server(vault_path, read_only=read_only, fastmcp_options={
        'token_verifier': provider,
        'auth': AuthSettings(issuer_url=issuer_url, resource_server_url=resource_url,
                             required_scopes=scopes, validate_token_resource=True),
        'transport_security': TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[resource.hostname, resource.hostname + ':443'],
            allowed_origins=['https://' + resource.hostname]),
        'streamable_http_path': resource.path,
        'stateless_http': True,
        'json_response': True,
        'max_request_body_size': MAX_REQUEST_BYTES,
    })
    mcp_app = server.streamable_http_app()
    auth_routes = provider.auth_routes()
    metadata = next(route for route in auth_routes
                    if route.path == '/.well-known/oauth-authorization-server')

    async def healthz(request):
        return JSONResponse({'status': 'ok'})

    routes = [
        Route('/.well-known/oauth-authorization-server' + issuer.path,
              endpoint=metadata.endpoint, methods=['GET', 'OPTIONS']),
        Mount(issuer.path, routes=[*auth_routes, *provider.login_routes(),
                                  Route('/healthz', healthz, methods=['GET'])]),
        Mount('/', app=mcp_app),
    ]

    @asynccontextmanager
    async def lifespan(app):
        async with server.session_manager.run():
            yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(RequestBoundary, host=resource.hostname, origin='https://' + resource.hostname,
                       issuer_path=issuer.path, resource_path=resource.path)
    # These references are local management/test interfaces, never HTTP responses.
    app.state.mcp_server = server
    app.state.oauth_provider = provider
    return app


def load_http_config(filename: str | Path) -> dict[str, Any]:
    """Load private owner configuration, requiring owner-only file and directory."""
    config_path = Path(filename).expanduser()
    file_info, dir_info = config_path.lstat(), config_path.parent.stat()
    if (not stat.S_ISREG(file_info.st_mode) or file_info.st_uid != os.getuid()
            or stat.S_IMODE(file_info.st_mode) != 0o600 or dir_info.st_uid != os.getuid()
            or stat.S_IMODE(dir_info.st_mode) != 0o700):
        raise ValueError('Config must be an owned regular 0600 file in an owned 0700 directory')
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('Config must contain a JSON object')
    vault = Path(config['vault']).expanduser()
    if not vault.is_absolute() or config_path.resolve().is_relative_to(vault.resolve()):
        raise ValueError('Config must live outside the fixed absolute Vault')
    socket_path = Path(config['unix_socket']).expanduser()
    if not socket_path.is_absolute() or socket_path.resolve().is_relative_to(vault.resolve()):
        raise ValueError('Unix socket must use an absolute path outside the Vault')
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Private fixed JSON config outside the Vault')
    args = parser.parse_args()
    try:
        config = load_http_config(args.config)
        app = build_http_app(config['vault'], config)
    except (OSError, ValueError, KeyError, TypeError):
        parser.error('Invalid private MCP configuration; inspect paths and permissions locally')
    import uvicorn
    uvicorn.run(app, uds=config['unix_socket'], access_log=False,
                proxy_headers=False, log_level='warning')


if __name__ == '__main__':
    main()
