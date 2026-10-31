"""Single-owner OAuth provider for the official MCP SDK HTTP transport.

Only client metadata (including SDK-compatible DCR client secrets) is recoverable
in the private SQLite file. Bearer tokens and browser/grant secrets are hashed.
This module never reads the content library or logs authentication payloads.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.routes import build_metadata, create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

OAUTH_SCOPES = ["library:read", "library:write"]
_COOKIE = "__Host-library-owner"
_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
_CONNECTOR_CALLBACK = re.compile(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,200}\Z")
_LOOPBACK_CALLBACK_PATH = re.compile(r"/callback(?:/[A-Za-z0-9_-]{1,200})?\Z")
_SECURITY_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache",
    "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


def _consent_headers(redirect_uri: str):
    # Chromium also applies form-action to a POST's redirect destination.
    # Name this already-validated callback, never every registered client or an
    # arbitrary origin. SDK exact-URI validation remains authoritative (CSP
    # does not enforce source paths after redirects). Queries are not CSP syntax.
    callback = urlsplit(redirect_uri)
    source = f'{callback.scheme}://{callback.netloc}' + quote(callback.path or '/', safe='/%-._~')
    return {**_SECURITY_HEADERS, 'Content-Security-Policy':
        _SECURITY_HEADERS['Content-Security-Policy'].replace("form-action 'self';", f"form-action 'self' {source};")}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_owner_password(password: str) -> str:
    """Create the private configuration value; never print the password or hash."""
    if not 12 <= len(password) <= 1024:
        raise ValueError("Owner password must contain 12 to 1024 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def _password_parts(encoded: str) -> tuple[bytes, bytes]:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[:4] != ["scrypt", "16384", "8", "1"]:
        raise ValueError("Unsupported owner password hash")
    try:
        salt, derived = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        raise ValueError("Invalid owner password hash") from None
    if len(salt) != 16 or len(derived) != 64:
        raise ValueError("Invalid owner password hash")
    return salt, derived


def verify_owner_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password) > 1024:
        return False
    try:
        salt, expected = _password_parts(encoded)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class PersonalOAuthProvider:
    """Persistent OAuthAuthorizationServerProvider / TokenVerifier implementation.

    Mount ``auth_routes() + login_routes()`` under the issuer's path. Expose the
    SDK metadata endpoint additionally at RFC 8414's host-level well-known path.
    The canonical resource is enforced even when a client omits RFC 8707 resource.
    """

    def __init__(self, db_path, issuer_url: str, resource_url: str,
                 owner_password_hash: str, allowed_redirect_uris=()):
        _password_parts(owner_password_hash)
        self.issuer_url = self._https_url(issuer_url)
        self.resource_url = self._https_url(resource_url)
        self.owner_password_hash = owner_password_hash
        self.allowed_redirect_uris = frozenset(str(uri) for uri in allowed_redirect_uris)
        for uri in self.allowed_redirect_uris:
            parsed = urlsplit(uri)
            if (parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.fragment
                    or parsed.username or parsed.password
                    or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})):
                raise ValueError("Extra OAuth callbacks must be exact HTTPS or loopback HTTP URLs")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.db_path.is_symlink():
            raise ValueError("OAuth database cannot be a symlink")
        fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL,
                    expires REAL NOT NULL, family TEXT,
                    PRIMARY KEY(kind, digest));
                CREATE INDEX IF NOT EXISTS object_family ON objects(family);
                CREATE TABLE IF NOT EXISTS limits (
                    key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
            """)

    @staticmethod
    def _https_url(value):
        parsed = urlsplit(str(value))
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("OAuth issuer and resource must be canonical HTTPS URLs")
        return str(value).rstrip("/")

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _allowed_redirect(self, uri: str) -> bool:
        if uri in self.allowed_redirect_uris or uri == _CALLBACK or _CONNECTOR_CALLBACK.fullmatch(uri):
            return True
        # Native desktop clients listen on a random local port for their OAuth
        # response. Permit numeric loopback only, with the documented callback
        # paths; registration still binds the entire exact URI and PKCE to it.
        try:
            parsed = urlsplit(uri)
            return bool(parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1'}
                        and not parsed.username and not parsed.password
                        and not parsed.query and not parsed.fragment
                        and parsed.port is not None and 1 <= parsed.port <= 65535
                        and _LOOPBACK_CALLBACK_PATH.fullmatch(parsed.path)
                        and '%' not in uri)
        except ValueError:
            return False

    def _limit(self, key: str, maximum: int, seconds: int) -> bool:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT start,count FROM limits WHERE key=?", (key,)).fetchone()
            if row is None or row["start"] + seconds <= now:
                db.execute("INSERT OR REPLACE INTO limits VALUES (?,?,1)", (key, now))
                return True
            if row["count"] >= maximum:
                return False
            db.execute("UPDATE limits SET count=count+1 WHERE key=?", (key,))
            return True

    def _load(self, kind, raw):
        if not isinstance(raw, str) or not 16 <= len(raw) <= 512:
            return None
        with self._connect() as db:
            row = db.execute("SELECT payload,expires FROM objects WHERE kind=? AND digest=?",
                             (kind, _digest(raw))).fetchone()
        return json.loads(row["payload"]) if row and row["expires"] > time.time() else None

    @staticmethod
    def _store(db, kind, raw, payload, expires, family=None):
        db.execute("INSERT INTO objects VALUES (?,?,?,?,?)",
                   (kind, _digest(raw), json.dumps(payload), expires, family))

    async def get_client(self, client_id):
        with self._connect() as db:
            row = db.execute("SELECT payload FROM clients WHERE id=?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["payload"]) if row else None

    async def register_client(self, client_info):
        redirects = client_info.redirect_uris or []
        if not redirects or len(redirects) > 10 or any(not self._allowed_redirect(str(u)) for u in redirects):
            raise RegistrationError("invalid_redirect_uri", "Callback URL is not permitted")
        if (client_info.token_endpoint_auth_method not in {"none", "client_secret_post", "client_secret_basic"}
                or set(client_info.grant_types) != {"authorization_code", "refresh_token"}
                or client_info.response_types != ["code"]
                or not set((client_info.scope or "").split()).issubset(OAUTH_SCOPES)):
            raise RegistrationError("invalid_client_metadata", "Unsupported client capabilities")
        payload = client_info.model_dump_json()
        if len(payload) > 16384 or len(client_info.client_name or "") > 200:
            raise RegistrationError("invalid_client_metadata", "Client metadata is too large")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM clients").fetchone()[0] >= 1000:
                raise RegistrationError("invalid_client_metadata", "Client registration capacity reached")
            try:
                db.execute("INSERT INTO clients VALUES (?,?)", (client_info.client_id, payload))
            except sqlite3.IntegrityError:
                raise RegistrationError("invalid_client_metadata", "Client already registered") from None

    async def authorize(self, client, params):
        if not self._allowed_redirect(str(params.redirect_uri)) or str(params.redirect_uri) not in [str(u) for u in client.redirect_uris or []]:
            raise AuthorizeError("invalid_request", "Callback URL is not permitted")
        if params.resource is not None and params.resource != self.resource_url:
            raise AuthorizeError("invalid_request", "Resource does not match this content library")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "A valid S256 PKCE challenge is required")
        scopes = params.scopes if params.scopes is not None else (client.scope or " ".join(OAUTH_SCOPES)).split()
        if not scopes or not set(scopes).issubset(OAUTH_SCOPES):
            raise AuthorizeError("invalid_scope", "Unsupported content library scope")
        if not self._limit("authorize-global", 1000, 3600):
            raise AuthorizeError("temporarily_unavailable", "Please retry later")
        pending = secrets.token_urlsafe(32)
        params = params.model_copy(update={"resource": self.resource_url, "scopes": scopes})
        with self._connect() as db:
            db.execute("DELETE FROM objects WHERE expires<?", (time.time(),))
            self._store(db, "pending", pending,
                        {"client_id": client.client_id, "params": params.model_dump(mode="json")}, time.time() + 600)
        return self.issuer_url + "/login?" + urlencode({"request": pending})

    async def load_authorization_code(self, client, authorization_code):
        data = self._load("code", authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **data)

    def _issue(self, db, client_id, scopes, family=None):
        now = int(time.time())
        family = family or secrets.token_hex(24)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        common = {"client_id": client_id, "scopes": scopes, "resource": self.resource_url, "subject": "owner"}
        self._store(db, "access", access, dict(common, expires_at=now + 3600), now + 3600, family)
        self._store(db, "refresh", refresh, dict(common, expires_at=now + 30 * 86400), now + 30 * 86400, family)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload,expires FROM objects WHERE kind='code' AND digest=?",
                             (_digest(authorization_code.code),)).fetchone()
            if not row or row["expires"] <= time.time():
                raise TokenError("invalid_grant", "Authorization code is no longer valid")
            data = json.loads(row["payload"])
            if data["client_id"] != client.client_id or data["resource"] != self.resource_url:
                raise TokenError("invalid_grant", "Authorization code is not valid for this client")
            db.execute("DELETE FROM objects WHERE kind='code' AND digest=?", (_digest(authorization_code.code),))
            return self._issue(db, client.client_id, data["scopes"])

    async def load_refresh_token(self, client, refresh_token):
        data = self._load("refresh", refresh_token)
        if not data or data["client_id"] != client.client_id or data["resource"] != self.resource_url:
            return None
        return RefreshToken(token=refresh_token, **data)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload,expires,family FROM objects WHERE kind='refresh' AND digest=?",
                             (_digest(refresh_token.token),)).fetchone()
            if not row or row["expires"] <= time.time():
                raise TokenError("invalid_grant", "Refresh token is no longer valid")
            data = json.loads(row["payload"])
            if data["client_id"] != client.client_id or data["resource"] != self.resource_url:
                raise TokenError("invalid_grant", "Refresh token is not valid for this client")
            if not set(scopes).issubset(data["scopes"]):
                raise TokenError("invalid_scope", "Scope cannot be expanded")
            db.execute("DELETE FROM objects WHERE family=?", (row["family"],))
            return self._issue(db, client.client_id, scopes, row["family"])

    async def load_access_token(self, token):
        data = self._load("access", token)
        if not data or data["resource"] != self.resource_url:
            return None
        return AccessToken(token=token, **data)

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def revoke_token(self, token):
        kind = "refresh" if isinstance(token, RefreshToken) else "access"
        with self._connect() as db:
            db.execute("DELETE FROM objects WHERE family IN (SELECT family FROM objects WHERE kind=? AND digest=?)",
                       (kind, _digest(token.token)))

    def revoke_all_tokens(self):
        """Local operator action: invalidate every grant, token and browser login."""
        with self._connect() as db:
            db.execute("DELETE FROM objects")

    def delete_client(self, client_id):
        """Local operator action: remove a client and all its authorization state."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM clients WHERE id=?", (client_id,))
            rows = db.execute("SELECT kind,digest,payload FROM objects").fetchall()
            for row in rows:
                if json.loads(row["payload"]).get("client_id") == client_id:
                    db.execute("DELETE FROM objects WHERE kind=? AND digest=?", (row["kind"], row["digest"]))

    def login_routes(self):
        return [Route("/login", self.login, methods=["GET", "POST"])]

    def auth_routes(self):
        registration = ClientRegistrationOptions(
            enabled=True, valid_scopes=OAUTH_SCOPES, default_scopes=OAUTH_SCOPES)
        revocation = RevocationOptions(enabled=True)
        routes = create_auth_routes(
            self, AnyHttpUrl(self.issuer_url),
            client_registration_options=registration, revocation_options=revocation)
        # SDK1.30 accepts public clients but omits `none` from discovery. Native
        # clients need this declaration to select their supported token method.
        metadata = build_metadata(AnyHttpUrl(self.issuer_url), None, registration, revocation)
        metadata.token_endpoint_auth_methods_supported = ['none', 'client_secret_post', 'client_secret_basic']
        metadata.revocation_endpoint_auth_methods_supported = ['none', 'client_secret_post', 'client_secret_basic']

        async def public_metadata(request):
            return JSONResponse(metadata.model_dump(mode='json', exclude_none=True),
                                headers={**_SECURITY_HEADERS, 'Access-Control-Allow-Origin': '*',
                                         'Access-Control-Allow-Methods': 'GET, OPTIONS'})

        for index, route in enumerate(routes):
            if route.path == '/.well-known/oauth-authorization-server':
                routes[index] = Route(route.path, public_metadata, methods=['GET', 'OPTIONS'])
            elif route.path in {"/token", "/register", "/revoke"}:
                routes[index] = Route(route.path, _GuardedEndpoint(self, route.app, route.path), methods=route.methods)
        return routes

    @staticmethod
    def _error(message, status=400):
        return HTMLResponse("<!doctype html><html lang='en'><meta charset='utf-8'><title>Content library authorization</title><p>"
                            + html.escape(message) + "</p></html>", status_code=status, headers=_SECURITY_HEADERS)

    async def login(self, request: Request):
        if request.method == "GET":
            pending = request.query_params.get("request", "")
            data = self._load("pending", pending)
            if not data:
                return self._error("Authorization request expired. Start again from your client.")
            client = await self.get_client(data["client_id"])
            if not client:
                return self._error("Unknown client")
            cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            data.update(cookie_hash=_digest(cookie), csrf_hash=_digest(csrf))
            with self._connect() as db:
                db.execute("UPDATE objects SET payload=? WHERE kind='pending' AND digest=? AND expires>?",
                           (json.dumps(data), _digest(pending), time.time()))
            esc = html.escape
            content = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>连接个人内容库</title>
<style>body{{margin:0;padding:24px;background:#f5f5f2;color:#262622;font:16px/1.7 system-ui,sans-serif}}
main{{max-width:480px;margin:8vh auto;padding:32px;background:white;border:1px solid #e4e4df;border-radius:16px}}
h1{{font-size:24px;margin-top:0}}input[type=password]{{display:block;box-sizing:border-box;width:100%;padding:12px;margin-top:8px;border:1px solid #aaa;border-radius:8px;font:inherit}}
button{{padding:10px 18px;border:0;border-radius:8px;background:#252b27;color:white;font:inherit;cursor:pointer}}
button[value=deny]{{background:#eee;color:#333;margin-left:8px}}details{{font-size:13px;color:#666;overflow-wrap:anywhere}}label{{display:block;margin-top:24px}}</style>
<main><h1>连接个人内容库</h1>
<p><strong>{esc(client.client_name or '当前客户端')}</strong> 请求访问你的私人内容库。</p>
<p>允许后可以读取私人笔记，以及保存和恢复当前稿。网站发布需要通过原来的发布入口另行操作。</p>
<details><summary>连接详情</summary><p>权限：{esc(', '.join(data['params']['scopes']))}</p>
<p>回调地址：{esc(str(data['params']['redirect_uri']))}</p></details>
<form method="post" action="{esc(self.issuer_url + '/login', quote=True)}">
<input type="hidden" name="request" value="{esc(pending, quote=True)}">
<input type="hidden" name="csrf" value="{esc(csrf, quote=True)}">
<label>内容库连接密码<input type="password" name="password" required maxlength="1024" autocomplete="current-password" autofocus></label>
<p><button name="decision" value="allow">允许连接</button>
<button name="decision" value="deny" formnovalidate>取消</button></p></form></main></html>"""
            response = HTMLResponse(content, headers=_consent_headers(data['params']['redirect_uri']))
            response.set_cookie(_COOKIE, cookie, max_age=600, secure=True, httponly=True, samesite="lax", path="/")
            return response

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 8192:
                return self._error("Invalid form", 413)
        if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
            return self._error("Invalid form")
        form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        if any(len(values) != 1 for values in form.values()):
            return self._error("Invalid form")
        value = lambda name: form.get(name, [""])[0]
        pending = value("request")
        data = self._load("pending", pending)
        cookie = request.cookies.get(_COOKIE, "")
        origin = request.headers.get("origin")
        expected_origin = urlsplit(self.issuer_url)
        if (not data or not cookie or not value("csrf")
                or not hmac.compare_digest(data.get("cookie_hash", ""), _digest(cookie))
                or not hmac.compare_digest(data.get("csrf_hash", ""), _digest(value("csrf")))
                or (origin and origin != f"{expected_origin.scheme}://{expected_origin.netloc}")):
            return self._error("Invalid or expired browser authorization", 403)
        decision = value("decision")
        if decision not in {"allow", "deny"}:
            return self._error("Choose authorize or deny")
        if decision == "allow":
            ip = request.client.host if request.client else "unknown"
            if not self._limit("login-ip:" + _digest(ip), 10, 900) or not self._limit("login-global", 40, 900):
                return self._error("Too many attempts. Please retry in 15 minutes.", 429)
            # This private single-owner endpoint is rate-limited before the fixed-cost
            # KDF. Keep verification in-process; no password enters a work queue.
            valid = verify_owner_password(value("password"), self.owner_password_hash)
            if not valid:
                return self._error("Password was not accepted. Reload authorization to retry.", 403)
        params = AuthorizationParams.model_validate(data["params"])
        code = secrets.token_urlsafe(32)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("DELETE FROM objects WHERE kind='pending' AND digest=? AND payload=? AND expires>?",
                                 (_digest(pending), json.dumps(data), time.time())).rowcount
            if changed != 1:
                return self._error("Authorization request expired. Start again from your client.", 403)
            if decision == "allow":
                expires = time.time() + 90
                grant = {"client_id": data["client_id"], "subject": "owner", "expires_at": expires,
                         **params.model_dump(mode="json", exclude={"state"})}
                self._store(db, "code", code, grant, expires)
        uri = construct_redirect_uri(str(params.redirect_uri), state=params.state,
                                     **({"code": code} if decision == "allow" else {"error": "access_denied"}))
        response = RedirectResponse(uri, status_code=303,
                                    headers=_consent_headers(str(params.redirect_uri)))
        response.delete_cookie(_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        return response


class _GuardedEndpoint:
    """Keep the SDK handlers; enforce bounded bodies and missing SDK audience check."""
    def __init__(self, provider, app, path):
        self.provider, self.app, self.path = provider, app, path

    async def __call__(self, scope, receive, send):
        if scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > 16384:
                return await JSONResponse({"error": "invalid_request"}, status_code=413, headers=_SECURITY_HEADERS)(scope, receive, send)
            if not message.get("more_body", False):
                break
        if self.path == "/register" and not self.provider._limit("registration-global", 100, 86400):
            return await JSONResponse({"error": "temporarily_unavailable"}, status_code=429, headers=_SECURITY_HEADERS)(scope, receive, send)
        if self.path == "/token":
            content_type = dict(scope.get("headers", [])).get(b"content-type", b"").decode("latin1").split(";")[0].strip().lower()
            if content_type != "application/x-www-form-urlencoded":
                return await JSONResponse({"error": "invalid_request", "error_description": "Use an URL-encoded token form"},
                                          status_code=400, headers=_SECURITY_HEADERS)(scope, receive, send)
            form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            resources = form.get("resource", [])
            if any(len(v) != 1 for v in form.values()) or (resources and resources != [self.provider.resource_url]):
                return await JSONResponse({"error": "invalid_request", "error_description": "Invalid resource or repeated parameter"},
                                          status_code=400, headers={**_SECURITY_HEADERS, "Access-Control-Allow-Origin": "*"})(scope, receive, send)
        if self.path == '/revoke':
            # SDK1.30 incorrectly requires the optional client_secret field for
            # public clients. Keep SDK validation/auth, supplying only its empty
            # default after confirming this is a registered public client.
            headers = dict(scope.get('headers', []))
            if headers.get(b'content-type', b'').split(b';')[0] == b'application/x-www-form-urlencoded':
                form = parse_qs(body.decode('utf-8', errors='replace'), keep_blank_values=True)
                client_ids = form.get('client_id', [])
                if len(client_ids) == 1 and 'client_secret' not in form:
                    client = await self.provider.get_client(client_ids[0])
                    if client and client.token_endpoint_auth_method == 'none':
                        form['client_secret'] = ['']
                        body = bytearray(urlencode(form, doseq=True).encode())
                        scope = dict(scope)
                        scope['headers'] = [(key, value) for key, value in scope.get('headers', [])
                                            if key.lower() != b'content-length'] + [
                                                (b'content-length', str(len(body)).encode())]
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)
