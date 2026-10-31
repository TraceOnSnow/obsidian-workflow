import asyncio
import base64
import hashlib
import html
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.routing import Mount

from markdown_library.mcp_oauth import (
    OAUTH_SCOPES, PersonalOAuthProvider, hash_owner_password, verify_owner_password,
)
from mcp.server.auth.provider import TokenError


ISSUER = "https://traceonsnow.com/library-auth"
RESOURCE = "https://traceonsnow.com/library-mcp"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
PASSWORD = "temporary test owner password"
VERIFIER = "A" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_owner_password(PASSWORD)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "oauth.sqlite"
        self.provider = self.new_provider()
        self.app = Starlette(routes=[Mount("/library-auth", routes=self.provider.auth_routes() + self.provider.login_routes())])
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://traceonsnow.com", follow_redirects=False)
        self.addAsyncCleanup(self.http.aclose)

    def new_provider(self, **kwargs):
        return PersonalOAuthProvider(self.db_path, ISSUER, RESOURCE, self.password_hash, **kwargs)

    async def register(self, **kwargs):
        metadata = {"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none", "client_name": "Test client"}
        metadata.update(kwargs)
        return await self.http.post("/library-auth/register", json=metadata)

    async def start(self, client, **kwargs):
        params = dict(client_id=client["client_id"], response_type="code", redirect_uri=CALLBACK,
                      code_challenge=CHALLENGE, code_challenge_method="S256", resource=RESOURCE,
                      scope=" ".join(OAUTH_SCOPES), state="client-state")
        params.update(kwargs)
        return await self.http.get("/library-auth/authorize", params=params)

    async def login_form(self, client, **kwargs):
        redirect = await self.start(client, **kwargs)
        self.assertEqual(redirect.status_code, 302, redirect.text)
        response = await self.http.get(redirect.headers["location"])
        self.assertEqual(response.status_code, 200)
        fields = {name: html.unescape(value) for name, value in re.findall(r'name="(request|csrf)" value="([^"]+)"', response.text)}
        fields.update(password=PASSWORD, decision="allow")
        return fields, response

    async def grant(self, client, **kwargs):
        form, _ = await self.login_form(client, **kwargs)
        response = await self.http.post("/library-auth/login", data=form)
        self.assertEqual(response.status_code, 303, response.text)
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertEqual(query["state"], ["client-state"])
        return query["code"][0]

    async def exchange(self, client, code, **kwargs):
        fields = dict(grant_type="authorization_code", client_id=client["client_id"], code=code,
                      redirect_uri=CALLBACK, code_verifier=VERIFIER, resource=RESOURCE)
        if client.get("client_secret"):
            fields["client_secret"] = client["client_secret"]
        fields.update(kwargs)
        return await self.http.post("/library-auth/token", data=fields)

    async def test_persistent_code_token_rotation_revocation_and_hashed_storage(self):
        client = (await self.register()).json()
        code = await self.grant(client)
        restarted = self.new_provider()
        self.assertEqual((await restarted.get_client(client["client_id"])).client_name, "Test client")
        self.assertIsNotNone(await restarted.load_authorization_code(await restarted.get_client(client["client_id"]), code))
        response = await self.exchange(client, code)
        self.assertEqual(response.status_code, 200, response.text)
        tokens = response.json()
        access = await restarted.verify_token(tokens["access_token"])
        self.assertEqual(access.resource, RESOURCE)
        self.assertEqual(access.subject, "owner")
        self.assertEqual((await self.exchange(client, code)).json()["error"], "invalid_grant")
        rotated = await self.http.post("/library-auth/token", data={
            "grant_type": "refresh_token", "client_id": client["client_id"],
            "refresh_token": tokens["refresh_token"], "resource": RESOURCE})
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertIsNone(await restarted.verify_token(tokens["access_token"]))
        self.assertIsNone(await restarted.load_refresh_token(await restarted.get_client(client["client_id"]), tokens["refresh_token"]))
        current = rotated.json()
        raw = self.db_path.read_bytes()
        for secret in (code, tokens["access_token"], tokens["refresh_token"], current["access_token"], current["refresh_token"]):
            self.assertNotIn(secret.encode(), raw)
        self.assertEqual(self.db_path.stat().st_mode & 0o777, 0o600)
        revoked = await self.http.post("/library-auth/revoke", data={
            "client_id": client["client_id"], "client_secret": "", "token": current["access_token"]})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertIsNone(await restarted.verify_token(current["access_token"]))
        self.assertIsNone(await restarted.load_refresh_token(await restarted.get_client(client["client_id"]), current["refresh_token"]))

    async def test_redirect_allowlist_and_client_metadata_rejections(self):
        for callback in ["https://evil.example/callback", CALLBACK + "?next=https://evil.example",
                         CALLBACK + "#fragment", "https://chatgpt.com.evil.example/connector/oauth/id",
                         "https://chatgpt.com/connector/oauth/id/extra", "http://chatgpt.com/connector/oauth/id",
                         "https://user@chatgpt.com/connector/oauth/id"]:
            response = await self.register(redirect_uris=[callback])
            self.assertEqual(response.status_code, 400, callback)
            self.assertEqual(response.json()["error"], "invalid_redirect_uri")
        response = await self.register(redirect_uris=["https://chatgpt.com/connector/oauth/abc-DEF_123"])
        self.assertEqual(response.status_code, 201)
        self.assertEqual((await self.register(token_endpoint_auth_method="private_key_jwt")).status_code, 400)
        self.assertEqual((await self.register(scope="admin")).status_code, 400)

    async def test_no_open_redirect_and_bad_audience_pkce_or_redirect_cannot_redeem(self):
        client = (await self.register()).json()
        response = await self.start(client, redirect_uri="https://evil.example/")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("location", response.headers)
        response = await self.start(client, resource="https://evil.example/mcp")
        self.assertEqual(parse_qs(urlsplit(response.headers["location"]).query)["error"], ["invalid_request"])
        code = await self.grant(client)
        for changes in [dict(code_verifier="wrong"), dict(resource="https://evil.example/mcp"), dict(redirect_uri=CALLBACK + "?changed=1")]:
            response = await self.exchange(client, code, **changes)
            self.assertEqual(response.status_code, 400, response.text)
        response = await self.exchange(client, code)
        self.assertEqual(response.status_code, 200, response.text)

    async def test_native_loopback_dcr_reaches_owner_consent_and_exchanges_with_pkce(self):
        metadata = (await self.http.get('/library-auth/.well-known/oauth-authorization-server')).json()
        self.assertIn('none', metadata['token_endpoint_auth_methods_supported'])
        for callback in ['http://127.0.0.1:43123/callback',
                         'http://127.0.0.1:43124/callback/abcdefghijkl',
                         'http://[::1]:43125/callback/desktop_ABC-123']:
            with self.subTest(callback=callback):
                registration = await self.register(redirect_uris=[callback])
                self.assertEqual(registration.status_code, 201, registration.text)
                client = registration.json()
                code = await self.grant(client, redirect_uri=callback)
                bad = await self.exchange(client, code, redirect_uri=callback.replace('4312', '5312'))
                self.assertEqual(bad.status_code, 400)
                response = await self.exchange(client, code, redirect_uri=callback)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNotNone(await self.provider.verify_token(response.json()['access_token']))

    async def test_native_callback_policy_refuses_nonlocal_or_changed_routes(self):
        for callback in ['http://localhost:43123/callback', 'http://127.0.0.2:43123/callback',
                         'http://127.0.0.1.evil.example:43123/callback',
                         'http://127.0.0.1:0/callback', 'http://127.0.0.1/callback',
                         'http://127.0.0.1:43123/callback?next=secret',
                         'http://127.0.0.1:43123/callback#secret',
                         'http://user@127.0.0.1:43123/callback',
                         'http://127.0.0.1:43123/callback/id/extra',
                         'http://127.0.0.1:43123/callback/%61',
                         'https://127.0.0.1:43123/callback',
                         'http://127.0.0.1:43123/oauth/callback']:
            with self.subTest(callback=callback):
                response = await self.register(redirect_uris=[callback])
                self.assertEqual(response.status_code, 400)
        callback = 'http://127.0.0.1:43123/callback/desktop_id'
        client = (await self.register(redirect_uris=[callback])).json()
        for changed in ['http://127.0.0.1:53123/callback/desktop_id',
                        'http://127.0.0.1:43123/callback/other_id']:
            response = await self.start(client, redirect_uri=changed)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('location', response.headers)

    async def test_browser_csp_is_bound_to_pending_callback_for_allow_and_deny(self):
        callbacks = ['http://127.0.0.1:43123/callback/one',
                     'http://127.0.0.1:43124/callback/two']
        for index, callback in enumerate(callbacks):
            client = (await self.register(redirect_uris=[callback])).json()
            form, page = await self.login_form(client, redirect_uri=callback)
            csp = page.headers['content-security-policy']
            self.assertIn("form-action 'self' " + callback + ';', csp)
            self.assertNotIn(callbacks[1 - index], csp)
            self.assertIn("default-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            result = await self.http.post('/library-auth/login', data={**form,
                'decision': 'allow' if index == 0 else 'deny',
                'redirect_uri': 'https://attacker.example/callback'},
                headers={'Origin': 'https://traceonsnow.com'})
            self.assertEqual(result.status_code, 303, result.text)
            self.assertTrue(result.headers['location'].startswith(callback + '?'))
            self.assertEqual(result.headers['content-security-policy'], csp)

    async def test_public_client_revocation_does_not_need_fake_client_secret(self):
        client = (await self.register()).json()
        code = await self.grant(client)
        tokens = (await self.exchange(client, code)).json()
        response = await self.http.post('/library-auth/revoke', data={
            'client_id': client['client_id'], 'token': tokens['refresh_token'],
            'token_type_hint': 'refresh_token'})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(await self.provider.verify_token(tokens['access_token']))

    async def test_multipart_resource_bypass_and_duplicate_token_parameters_rejected(self):
        client = (await self.register()).json()
        code = await self.grant(client)
        response = await self.http.post("/library-auth/token", files={
            "grant_type": (None, "authorization_code"), "client_id": (None, client["client_id"]),
            "code": (None, code), "redirect_uri": (None, CALLBACK), "code_verifier": (None, VERIFIER),
            "resource": (None, "https://evil.example/mcp")})
        self.assertEqual(response.status_code, 400)
        response = await self.http.post("/library-auth/token", content="resource=" + RESOURCE + "&resource=" + RESOURCE,
                                        headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((await self.exchange(client, code)).status_code, 200)

    async def test_csrf_cookie_origin_consent_password_and_replay(self):
        client = (await self.register(client_name='<img src=x onerror="alert(1)">')).json()
        form, response = await self.login_form(client)
        self.assertNotIn("<img", response.text)
        self.assertIn("&lt;img", response.text)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        for change in [dict(csrf="wrong"), dict(password="wrong"), dict(decision="unexpected")]:
            result = await self.http.post("/library-auth/login", data={**form, **change})
            self.assertIn(result.status_code, {400, 403})
        response = await self.http.post("/library-auth/login", data=form, headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://traceonsnow.com") as stranger:
            self.assertEqual((await stranger.post("/library-auth/login", data=form)).status_code, 403)
        response = await self.http.post("/library-auth/login", data={**form, "decision": "deny", "password": ""})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(parse_qs(urlsplit(response.headers["location"]).query)["error"], ["access_denied"])
        self.assertEqual((await self.http.post("/library-auth/login", data=form)).status_code, 403)

    async def test_failed_password_rate_limited_and_expired_browser_grant_rejected(self):
        client = (await self.register()).json()
        form, _ = await self.login_form(client)
        for _ in range(10):
            response = await self.http.post("/library-auth/login", data={**form, "password": "wrong"})
            self.assertEqual(response.status_code, 403)
        response = await self.http.post("/library-auth/login", data=form)
        self.assertEqual(response.status_code, 429)
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE objects SET expires=0 WHERE kind='pending'")
        self.assertEqual((await self.http.post("/library-auth/login", data=form)).status_code, 403)

    async def test_client_secret_authentication_and_refresh_scope_audience(self):
        client = (await self.register(token_endpoint_auth_method="client_secret_post")).json()
        code = await self.grant(client)
        response = await self.exchange(client, code, client_secret="wrong")
        self.assertEqual(response.status_code, 401)
        tokens = (await self.exchange(client, code)).json()
        refresh = dict(grant_type="refresh_token", client_id=client["client_id"], client_secret=client["client_secret"],
                       refresh_token=tokens["refresh_token"], resource=RESOURCE)
        self.assertEqual((await self.http.post("/library-auth/token", data={**refresh, "resource": "https://evil.example"})).status_code, 400)
        self.assertEqual((await self.http.post("/library-auth/token", data={**refresh, "scope": "admin"})).status_code, 400)
        narrowed = await self.http.post("/library-auth/token", data={**refresh, "scope": "library:read"})
        self.assertEqual(narrowed.status_code, 200, narrowed.text)
        self.assertEqual(narrowed.json()["scope"], "library:read")
        self.assertEqual((await self.http.post("/library-auth/token", data=refresh)).status_code, 400)

    async def test_code_and_refresh_exchange_cannot_replay_loaded_grants(self):
        client = (await self.register()).json()
        code = await self.grant(client)
        info = await self.provider.get_client(client["client_id"])
        grant1 = await self.provider.load_authorization_code(info, code)
        grant2 = await self.provider.load_authorization_code(info, code)
        results = await asyncio.gather(self.provider.exchange_authorization_code(info, grant1),
                                       self.provider.exchange_authorization_code(info, grant2), return_exceptions=True)
        self.assertEqual(sum(isinstance(value, TokenError) for value in results), 1)
        tokens = next(value for value in results if not isinstance(value, Exception))
        refresh1 = await self.provider.load_refresh_token(info, tokens.refresh_token)
        refresh2 = await self.provider.load_refresh_token(info, tokens.refresh_token)
        results = await asyncio.gather(self.provider.exchange_refresh_token(info, refresh1, OAUTH_SCOPES),
                                       self.provider.exchange_refresh_token(info, refresh2, OAUTH_SCOPES), return_exceptions=True)
        self.assertEqual(sum(isinstance(value, TokenError) for value in results), 1)

    async def test_operator_revocation_and_client_deletion(self):
        client = (await self.register()).json()
        tokens = (await self.exchange(client, await self.grant(client))).json()
        self.provider.revoke_all_tokens()
        self.assertIsNone(await self.provider.verify_token(tokens["access_token"]))
        tokens = (await self.exchange(client, await self.grant(client))).json()
        self.provider.delete_client(client["client_id"])
        self.assertIsNone(await self.provider.get_client(client["client_id"]))
        self.assertIsNone(await self.provider.verify_token(tokens["access_token"]))

    async def test_expired_codes_tokens_and_cross_client_grants_rejected(self):
        client = (await self.register()).json()
        other = (await self.register()).json()
        code = await self.grant(client)
        self.assertEqual((await self.exchange(other, code)).status_code, 400)
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE objects SET expires=0 WHERE kind='code'")
        self.assertEqual((await self.exchange(client, code)).status_code, 400)
        tokens = (await self.exchange(client, await self.grant(client))).json()
        other_info = await self.provider.get_client(other["client_id"])
        self.assertIsNone(await self.provider.load_refresh_token(other_info, tokens["refresh_token"]))
        with sqlite3.connect(self.db_path) as db:
            db.execute("UPDATE objects SET expires=0 WHERE kind IN ('access','refresh')")
        self.assertIsNone(await self.provider.verify_token(tokens["access_token"]))
        info = await self.provider.get_client(client["client_id"])
        self.assertIsNone(await self.provider.load_refresh_token(info, tokens["refresh_token"]))

    async def test_password_hash_and_extra_callbacks(self):
        self.assertTrue(verify_owner_password(PASSWORD, self.password_hash))
        self.assertFalse(verify_owner_password("wrong", self.password_hash))
        self.assertFalse(verify_owner_password(PASSWORD, "bad hash"))
        with self.assertRaises(ValueError):
            self.new_provider(allowed_redirect_uris=["http://evil.example/callback"])
        provider = self.new_provider(allowed_redirect_uris=["http://localhost:8765/callback"])
        self.assertTrue(provider._allowed_redirect("http://localhost:8765/callback"))
        self.assertFalse(provider._allowed_redirect("http://localhost:8765/callback?next=evil"))


if __name__ == "__main__":
    unittest.main()
