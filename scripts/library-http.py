#!/usr/bin/env python3
"""Local operator controls for the private HTTPS Markdown MCP service."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / 'runtime/library-http'
VAULT = Path('/home/trace/projects/personal-records-vault')
SOCKET = Path('/home/trace/projects/homepage-deployment/web/conf.d/library-mcp/mcp.sock')
UNIT = 'markdown-library-http.service'


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def private_write(target, data):
    if target.is_symlink():
        raise ValueError('refusing symlink')
    fd, name = tempfile.mkstemp(prefix='.new-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)


def load_config(root):
    target = root / 'config.json'
    if (root.is_symlink() or target.is_symlink() or
            root.stat().st_mode & 0o077 or target.stat().st_mode & 0o077):
        raise ValueError('runtime directory/config must be private')
    return json.loads(target.read_text())


def configure(root=RUNTIME, vault=VAULT, unix_socket=SOCKET):
    from markdown_library.core import Library
    Library(vault).validate()
    if root.is_symlink():
        raise ValueError('refusing symlink')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    if any(root.iterdir()):
        raise ValueError('runtime exists; preserve it instead of overwriting credentials')
    from markdown_library.mcp_oauth import hash_owner_password
    password = secrets.token_urlsafe(24)
    config = {
        'vault': str(vault), 'db_path': str(root / 'oauth.sqlite3'),
        'owner_password_hash': hash_owner_password(password),
        'issuer_url': 'https://traceonsnow.com/library-auth',
        'resource_url': 'https://traceonsnow.com/library-mcp',
        'allowed_redirect_uris': [], 'read_only': False,
        'unix_socket': str(unix_socket),
    }
    private_write(root / 'owner-password', password + '\n')
    private_write(root / 'config.json', json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    emit({'configured': True, 'url': config['resource_url'], 'credential_saved_privately': True,
          'next': 'credentials in your interactive terminal; install/start the documented service'})


def credentials(root=RUNTIME):
    # Never return this secret through a normal agent/tool pipe or redirected log.
    if not sys.stdout.isatty():
        raise ValueError('show credentials only in your own interactive terminal')
    config = load_config(root)
    target = root / 'owner-password'
    if target.is_symlink() or target.stat().st_mode & 0o077:
        raise ValueError('credential file must be private')
    print('MCP URL:', config['resource_url'])
    print('Authentication: OAuth (automatic discovery / dynamic client registration)')
    print('Owner login password:', target.read_text().strip())
    print('Enter this password only on the HTTPS personal-library authorization page.')


def provider(root):
    from markdown_library.mcp_oauth import PersonalOAuthProvider
    config = load_config(root)
    return PersonalOAuthProvider(config['db_path'], config['issuer_url'], config['resource_url'],
                                 config['owner_password_hash'], config['allowed_redirect_uris'])


def revoke_all(root=RUNTIME):
    result = provider(root).revoke_all_tokens()
    if hasattr(result, '__await__'):
        asyncio.run(result)
    emit({'revoked': True, 'scope': 'all library HTTP OAuth grants',
          'next': 'Reconnect clients through owner login'})


def rotate_password(root=RUNTIME):
    from markdown_library.mcp_oauth import hash_owner_password
    config = load_config(root)
    revoke_all(root)
    password = secrets.token_urlsafe(24)
    config['owner_password_hash'] = hash_owner_password(password)
    private_write(root / 'owner-password', password + '\n')
    private_write(root / 'config.json', json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    emit({'password_rotated': True, 'secret_printed': False,
          'next': 'restart service, then run credentials in your interactive terminal'})


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, target):
        super().__init__('traceonsnow.com', timeout=3)
        self.target = target

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.target))


def status(root=RUNTIME):
    config = load_config(root)
    connection = UnixHTTP(config['unix_socket'])
    healthy = False
    anonymous_denied = False
    try:
        connection.request('GET', '/library-auth/healthz')
        response = connection.getresponse()
        healthy = response.status == 200
        response.read()
        connection.request('GET', '/library-mcp')
        response = connection.getresponse()
        anonymous_denied = response.status == 401
        response.read()
    except (OSError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    emit({'configured': True, 'url': config['resource_url'], 'healthy': healthy,
          'anonymous_denied': anonymous_denied, 'chatgpt_verified': False})


def service(action):
    return subprocess.run(['systemctl', '--user', action, UNIT], check=False).returncode


def diagnostics():
    """Only export the fixed-schema connection events, never raw service logs."""
    from markdown_library.mcp_diagnostics import PREFIX, STAGES, METHODS, REASONS, _safe_registration
    result = subprocess.run(['journalctl', '--user', '-u', UNIT, '--since', '-30 minutes',
                             '--output=json', '--no-pager', '-n', '200'],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        emit({'diagnostics_available': False})
        return
    events = []
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
            message = entry.get('MESSAGE', '')
            if not isinstance(message, str) or PREFIX + ' ' not in message:
                continue
            raw = json.loads(message.split(PREFIX + ' ', 1)[1])
            event = {'stage': raw['stage'] if raw.get('stage') in STAGES else 'other',
                     'method': raw['method'] if raw.get('method') in METHODS else 'OTHER',
                     'status': raw['status'] if type(raw.get('status')) is int and 100 <= raw['status'] <= 599 else 0}
            if raw.get('reason') in REASONS:
                event['reason'] = raw['reason']
            if 'registration' in raw:
                event['registration'] = _safe_registration(raw['registration'])
            stamp = int(entry['__REALTIME_TIMESTAMP']) / 1000000
            event['time_utc'] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
            events.append(event)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            continue
    emit({'diagnostics_available': True, 'events': events[-20:], 'credentials_included': False})


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'credentials', 'status', 'revoke-all',
                                          'rotate-password', 'start', 'stop', 'restart', 'diagnostics'])
    args = parser.parse_args()
    try:
        if args.action == 'configure':
            configure()
        elif args.action == 'credentials':
            credentials()
        elif args.action == 'status':
            status()
        elif args.action == 'revoke-all':
            revoke_all()
        elif args.action == 'rotate-password':
            rotate_password()
        elif args.action == 'diagnostics':
            diagnostics()
        else:
            return service(args.action)
    except (OSError, ValueError, KeyError):
        emit({'error': 'Local HTTP MCP operation failed; check private configuration and service. No secret was printed.'})
        return 1
    return 0


if __name__ == '__main__':
    # Use the existing project dependency environment, never install globally.
    expected = PROJECT / '.venv/bin/python'
    if Path(sys.prefix).resolve() != (PROJECT / '.venv').resolve() and expected.exists():
        os.execv(str(expected), [str(expected), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit(main())
