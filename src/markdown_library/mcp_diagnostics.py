"""Bounded connection diagnostics that retain no request or credential values.

The caller classifies the endpoint before calling this module. Paths, queries,
headers and token/login bodies are deliberately absent from the event API.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)
PREFIX = 'LIBRARY_MCP_CONNECTION'
MAX_REGISTRATION_BYTES = 16384
MAX_CALLBACK_COUNT = 100
STAGES = frozenset({
    'mcp', 'resource_metadata', 'authorization_metadata', 'register', 'authorize',
    'login', 'token', 'revoke', 'health', 'other',
})
METHODS = frozenset({'GET', 'POST', 'OPTIONS', 'HEAD', 'PUT', 'PATCH', 'DELETE', 'CONNECT', 'TRACE'})
AUTH_METHODS = frozenset({
    'omitted', 'none', 'client_secret_post', 'client_secret_basic', 'private_key_jwt',
    'tls_client_auth', 'self_signed_tls_client_auth', 'other', 'invalid',
})
CALLBACK_TYPES = frozenset({'web_chatgpt', 'loopback_v4', 'loopback_v6', 'loopback_hostname', 'other'})
CALLBACK_PATHS = frozenset({'callback', 'callback_id', 'oauth_callback', 'web_stable', 'web_scoped', 'other'})
REASONS = frozenset({
    # OAuth authorization, token, registration, revocation and resource errors.
    'invalid_request', 'invalid_client', 'invalid_grant', 'unauthorized_client',
    'unsupported_grant_type', 'invalid_scope', 'access_denied',
    'unsupported_response_type', 'server_error', 'temporarily_unavailable',
    'invalid_redirect_uri', 'invalid_client_metadata', 'invalid_software_statement',
    'unapproved_software_statement', 'invalid_token', 'insufficient_scope',
    'unsupported_token_type', 'invalid_target', 'authorization_pending',
    'slow_down', 'expired_token', 'request_not_supported',
    'request_uri_not_supported', 'registration_not_supported',
    'interaction_required', 'login_required', 'account_selection_required',
    'consent_required', 'invalid_request_uri', 'invalid_request_object',
    'request_boundary_host', 'request_boundary_origin', 'request_boundary_body',
    'unhandled',
})


def _enum(value, allowed, fallback):
    return value if isinstance(value, str) and value in allowed else fallback


def _callback_summary(uri):
    if not isinstance(uri, str) or any(ord(char) < 32 or ord(char) == 127 for char in uri):
        return 'other', 'other'
    try:
        parsed = urlsplit(uri)
        # Reading port also validates malformed/out-of-range port spellings.
        port = parsed.port
        host = parsed.hostname
        if parsed.scheme not in {'https', 'http'} or not host:
            return 'other', 'other'
        kind = 'other'
        if not parsed.username and not parsed.password:
            if parsed.scheme == 'https' and host == 'chatgpt.com' and port in (None, 443):
                kind = 'web_chatgpt'
            elif host == 'localhost':
                kind = 'loopback_hostname'
            else:
                try:
                    address = ipaddress.ip_address(host)
                    if address.is_loopback:
                        kind = 'loopback_v4' if address.version == 4 else 'loopback_v6'
                except ValueError:
                    pass
        path = parsed.path
        path_kind = 'other'
        if path == '/callback':
            path_kind = 'callback'
        elif re.fullmatch(r'/callback/[^/]+', path):
            path_kind = 'callback_id'
        elif path == '/oauth/callback':
            path_kind = 'oauth_callback'
        elif kind == 'web_chatgpt' and path == '/connector_platform_oauth_redirect':
            path_kind = 'web_stable'
        elif kind == 'web_chatgpt' and re.fullmatch(r'/connector/oauth/[^/]+', path):
            path_kind = 'web_scoped'
        return kind, path_kind
    except (ValueError, UnicodeError):
        return 'other', 'other'


def registration_summary(bodybytes: bytes) -> dict:
    """Reduce a bounded DCR JSON body to fixed enum values and a capped count."""
    invalid = {'auth_method': 'invalid', 'callback_types': [], 'callback_paths': [], 'callback_count': 0}
    if not isinstance(bodybytes, bytes) or len(bodybytes) > MAX_REGISTRATION_BYTES:
        return invalid
    try:
        data = json.loads(bodybytes)
    except (ValueError, UnicodeError, RecursionError):
        return invalid
    if not isinstance(data, dict):
        return invalid
    method = data.get('token_endpoint_auth_method', 'omitted')
    method = _enum(method, AUTH_METHODS, 'other' if isinstance(method, str) else 'invalid')
    callbacks = data.get('redirect_uris', [])
    if not isinstance(callbacks, list):
        callbacks = []
    kinds, paths = set(), set()
    for callback in callbacks[:MAX_CALLBACK_COUNT]:
        kind, path = _callback_summary(callback)
        kinds.add(kind)
        paths.add(path)
    return {'auth_method': method, 'callback_types': sorted(kinds),
            'callback_paths': sorted(paths), 'callback_count': min(len(callbacks), MAX_CALLBACK_COUNT)}


def _safe_registration(registration):
    """Revalidate summaries so the logging API cannot accidentally leak a raw dict."""
    if not isinstance(registration, dict):
        registration = {}
    count = registration.get('callback_count', 0)
    if type(count) is not int:
        count = 0
    result = {
        'auth_method': _enum(registration.get('auth_method'), AUTH_METHODS, 'invalid'),
        'callback_count': max(0, min(count, MAX_CALLBACK_COUNT)),
    }
    for name, allowed in (('callback_types', CALLBACK_TYPES), ('callback_paths', CALLBACK_PATHS)):
        values = registration.get(name, [])
        result[name] = sorted({value for value in values[:MAX_CALLBACK_COUNT]
                              if isinstance(value, str) and value in allowed}) if isinstance(values, list) else []
    return result


def record_connection(stage, method, status, reason=None, registration=None) -> dict:
    """Log one fixed-schema WARNING event and return the same safe event dict.

    No arbitrary string supplied to this function is written to the log. The
    registration parameter accepts only the enum/count summary described above.
    """
    event = {
        'stage': _enum(stage, STAGES, 'other'),
        'method': _enum(method.upper() if isinstance(method, str) and len(method) <= 16 else None,
                        METHODS, 'OTHER'),
        'status': int(status) if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else 0,
    }
    if reason is not None:
        event['reason'] = _enum(reason, REASONS, 'unhandled')
    if registration is not None:
        event['registration'] = _safe_registration(registration)
    LOGGER.warning('%s %s', PREFIX, json.dumps(event, sort_keys=True, separators=(',', ':'), ensure_ascii=True))
    return event
