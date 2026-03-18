import os
import re
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TOKEN_SECRET", "test-token-secret")
os.environ.setdefault("OAUTH_CLIENT_ID", "test-client")
os.environ.setdefault("OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault(
    "ALLOWED_REDIRECT_HOSTS",
    "chat.openai.com,chatgpt.com,localhost,127.0.0.1",
)
os.environ.setdefault("ALLOWED_HOSTS", "testserver,localhost,127.0.0.1")
os.environ.setdefault("DEBUG", "false")

from fastapi.testclient import TestClient

import main
from app.client import CampusSquareClient
from app.config import settings
from app.oauth import (
    _auth_codes,
    _auth_requests,
    _enforce_state_limits,
    _render_login_form,
    _session_cache,
)
from app.scraper import CampusSquareScraper


class OAuthSecurityTests(unittest.TestCase):
    def setUp(self):
        _auth_requests.clear()
        _auth_codes.clear()
        _session_cache.clear()
        settings.oauth_client_id = "test-client"
        settings.oauth_client_secret = "test-client-secret"
        settings.allowed_redirect_hosts = [
            "chat.openai.com",
            "chatgpt.com",
            "localhost",
            "127.0.0.1",
        ]
        settings.allowed_hosts = ["testserver", "localhost", "127.0.0.1"]
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        _auth_requests.clear()
        _auth_codes.clear()
        _session_cache.clear()

    def test_authorize_rejects_unapproved_redirect_uri(self):
        response = self.client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "test-client",
                "redirect_uri": "https://attacker.example/callback",
                "scope": "openid",
                "state": "abc",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("許可されていない redirect_uri", response.text)

    def test_authorization_code_is_bound_to_redirect_uri(self):
        auth_response = self.client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "test-client",
                "redirect_uri": "http://localhost:8000/callback",
                "scope": "openid",
                "state": "abc",
            },
        )
        auth_request_id = re.search(
            r'name="auth_request_id" value="([^"]+)"',
            auth_response.text,
        ).group(1)

        with patch.object(CampusSquareClient, "login", new=AsyncMock(return_value=True)):
            callback_response = self.client.post(
                "/oauth/callback",
                data={
                    "auth_request_id": auth_request_id,
                    "username": "f00x000x",
                    "password": "secret",
                },
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302)
        code = re.search(r"[?&]code=([^&]+)", callback_response.headers["location"]).group(1)

        token_response = self.client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "test-client",
                "client_secret": "test-client-secret",
                "redirect_uri": "http://localhost:8000/other",
            },
        )
        self.assertEqual(token_response.status_code, 400)
        self.assertEqual(token_response.json()["detail"], "redirect_uri mismatch")

    def test_login_form_escapes_error_html(self):
        html = _render_login_form("request-id", '<script>alert("x")</script>')
        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertIn("&lt;script&gt;", html)


class ClientSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_frame_content_rejects_external_url(self):
        client = CampusSquareClient(username="f00x000x", password="secret")
        client._logged_in = True
        client._last_activity = time.time()
        client._client = AsyncMock()

        with self.assertRaises(ValueError):
            await client.get_frame_content("https://metadata.google.internal/computeMetadata/v1")

        client._client.get.assert_not_called()


class ScraperPrivacyTests(unittest.TestCase):
    def test_response_cache_is_disabled_by_default(self):
        original_ttl = settings.response_cache_ttl_seconds
        settings.response_cache_ttl_seconds = 0
        try:
            scraper = CampusSquareScraper(Mock())
            scraper._set_cache("grades", {"student_name": "Alice"})
            self.assertIsNone(scraper._get_cache("grades"))
            self.assertEqual(scraper._cache, {})
        finally:
            settings.response_cache_ttl_seconds = original_ttl


class CapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_cache_is_bounded(self):
        original_limit = settings.max_session_cache_entries
        settings.max_session_cache_entries = 1
        try:
            old_client = Mock()
            old_client.close = AsyncMock()
            new_client = Mock()
            new_client.close = AsyncMock()
            _session_cache.clear()
            _session_cache["old"] = {
                "client": old_client,
                "scraper": object(),
                "last_used": 1.0,
            }
            _session_cache["new"] = {
                "client": new_client,
                "scraper": object(),
                "last_used": 2.0,
            }

            await _enforce_state_limits()

            self.assertNotIn("old", _session_cache)
            self.assertIn("new", _session_cache)
            old_client.close.assert_awaited_once()
        finally:
            settings.max_session_cache_entries = original_limit
            _session_cache.clear()


if __name__ == "__main__":
    unittest.main()
