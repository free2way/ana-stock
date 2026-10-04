"""Security regression tests.

Covers the global authentication middleware (every mutation endpoint must be
unreachable without a session), reflected-XSS escaping of the shared banner
messages, the login failure rate limiter, and the Secure cookie flag.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.services.auth import (
    LOGIN_FAILURE_MAX_ATTEMPTS,
    is_login_rate_limited,
    register_login_failure,
    reset_login_failures,
    reset_login_rate_limit_state,
)
from tests.postgres_safety import ApplicationPostgresTestCase


SOCIAL_MUTATION_ENDPOINTS = (
    "/social-signals/accounts/add",
    "/social-signals/accounts/remove",
    "/social-signals/posts/add",
    "/social-signals/poll/run",
    "/social-signals/mentions/remove",
    "/social-signals/watchlist/add",
    "/social-signals/watchlist/sync",
)

PROTECTED_PAGES = (
    "/dashboard",
    "/dashboard/ops",
    "/watchlist",
    "/portfolio",
    "/screeners",
    "/social-signals",
    "/jobs",
    "/jobs/recent",
    "/settings",
    "/insights",
    "/symbols",
    "/backtests",
    "/review-journal",
)

PUBLIC_GETS = (
    "/health",
    "/health/ready",
    "/login",
)

BANNER_XSS_PAYLOAD = "<script>alert(1)</script>"


class SecurityHardeningTests(ApplicationPostgresTestCase):
    def setUp(self) -> None:
        super().setUp()  # truncates every table in the test database
        self._environ_snapshot = dict(os.environ)
        temp_path = Path(tempfile.mkdtemp(prefix="ana-security-"))
        os.environ["PQW_STORAGE_DIR"] = str(temp_path / "storage")
        os.environ["PQW_DATA_DIR"] = str(temp_path / "data")
        os.environ["PQW_RAW_DATA_DIR"] = str(temp_path / "data" / "raw")
        os.environ["PQW_NORMALIZED_DATA_DIR"] = str(temp_path / "data" / "normalized")
        os.environ["PQW_QLIB_DATA_DIR"] = str(temp_path / "data" / "qlib")
        os.environ["PQW_ARTIFACTS_DIR"] = str(temp_path / "data" / "artifacts")
        os.environ["PQW_AUTH_USERNAME"] = "admin"
        os.environ["PQW_AUTH_PASSWORD"] = "admin1234"
        os.environ["PQW_AUTH_SECRET"] = "test-secret"
        os.environ["PQW_STORAGE_CAPACITY_MONITOR_ENABLED"] = "false"

        from app.core.config import reset_settings_cache

        reset_settings_cache()
        reset_login_rate_limit_state()

        from app.api.main import app

        self.app = app
        self.client = TestClient(app)
        self._login(self.client)

    def tearDown(self) -> None:
        self.client.close()
        reset_login_rate_limit_state()
        os.environ.clear()
        os.environ.update(self._environ_snapshot)

        from app.core.config import reset_settings_cache

        reset_settings_cache()
        super().tearDown()

    def _login(self, client: TestClient) -> None:
        response = client.post(
            "/login",
            data={"username": "admin", "password": "admin1234", "next": "/dashboard"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)

    # -- global auth middleware -------------------------------------------------

    def test_public_paths_stay_open(self) -> None:
        fresh = TestClient(self.app)
        try:
            for path in PUBLIC_GETS:
                response = fresh.get(path, follow_redirects=False)
                self.assertEqual(200, response.status_code, path)
                self.assertNotIn("/login", response.headers.get("location", ""), path)
        finally:
            fresh.close()

    def test_protected_pages_redirect_unauthenticated(self) -> None:
        fresh = TestClient(self.app)
        try:
            for path in PROTECTED_PAGES:
                response = fresh.get(path, follow_redirects=False)
                self.assertEqual(303, response.status_code, path)
                self.assertIn("/login", response.headers.get("location", ""), path)
        finally:
            fresh.close()

    def test_social_mutation_endpoints_require_authentication(self) -> None:
        fresh = TestClient(self.app)
        try:
            for path in SOCIAL_MUTATION_ENDPOINTS:
                response = fresh.post(path, data={}, follow_redirects=False)
                self.assertEqual(303, response.status_code, path)
                self.assertIn("/login", response.headers.get("location", ""), path)

            # The middleware must have blocked every request before any
            # handler ran, so no social account may have been persisted.
            from sqlalchemy import select

            from app.core.db import SessionLocal
            from app.models.tables import AppSetting
            from app.services.social_signals import SOCIAL_ACCOUNTS_KEY

            with SessionLocal() as db:
                stored = db.scalar(select(AppSetting.value).where(AppSetting.key == SOCIAL_ACCOUNTS_KEY))
            self.assertIsNone(stored)
        finally:
            fresh.close()

    def test_symbol_mutation_requires_authentication(self) -> None:
        fresh = TestClient(self.app)
        try:
            response = fresh.post(
                "/symbols",
                json={"ticker": "EVIL.SS", "name": "Evil", "market": "CN", "exchange": "SSE"},
                follow_redirects=False,
            )
            self.assertEqual(303, response.status_code)
            self.assertIn("/login", response.headers["location"])
        finally:
            fresh.close()

    # -- reflected XSS in banner messages ---------------------------------------

    def test_banner_messages_are_html_escaped(self) -> None:
        for path in ("/watchlist", "/portfolio", "/screeners"):
            response = self.client.get(
                path,
                params={"message": BANNER_XSS_PAYLOAD, "lang": "en"},
                follow_redirects=False,
            )
            self.assertEqual(200, response.status_code, path)
            self.assertNotIn("<script>alert(1)</script>", response.text, path)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text, path)

    def test_screener_banner_preserves_action_links(self) -> None:
        response = self.client.get(
            "/screeners",
            params={"message": "Added 600519.SS to watchlist", "lang": "en"},
            follow_redirects=False,
        )
        self.assertEqual(200, response.status_code)
        self.assertIn("Added 600519.SS to watchlist", response.text)
        self.assertIn("Open Dashboard", response.text)

    # -- login rate limiting ------------------------------------------------------

    def test_login_rate_limiter_flags_and_recovers(self) -> None:
        try:
            for _ in range(LOGIN_FAILURE_MAX_ATTEMPTS):
                register_login_failure("unit-client")
            self.assertTrue(is_login_rate_limited("unit-client"))
            self.assertFalse(is_login_rate_limited("other-client"))

            reset_login_failures("unit-client")
            self.assertFalse(is_login_rate_limited("unit-client"))
        finally:
            reset_login_rate_limit_state()

    def test_login_rate_limiter_window_expiry(self) -> None:
        from app.services.auth import LOGIN_FAILURE_WINDOW_SECONDS, _login_failures, _login_failure_lock

        try:
            register_login_failure("unit-client")
            stale = time.time() - LOGIN_FAILURE_WINDOW_SECONDS - 1.0
            with _login_failure_lock:
                _login_failures["unit-client"].clear()
                _login_failures["unit-client"].append(stale)
            self.assertFalse(is_login_rate_limited("unit-client"))
            # register must prune the stale record, not accumulate on top of it
            register_login_failure("unit-client")
            with _login_failure_lock:
                self.assertEqual(1, len(_login_failures["unit-client"]))
        finally:
            reset_login_rate_limit_state()

    def test_too_many_failed_logins_block_correct_password(self) -> None:
        try:
            fresh = TestClient(self.app)
            try:
                for _ in range(LOGIN_FAILURE_MAX_ATTEMPTS):
                    failed = fresh.post(
                        "/login",
                        data={"username": "admin", "password": "wrong-password", "next": "/dashboard"},
                        follow_redirects=False,
                    )
                    self.assertEqual(303, failed.status_code)
                    self.assertIn("Invalid+username+or+password", failed.headers["location"])

                blocked = fresh.post(
                    "/login",
                    data={"username": "admin", "password": "admin1234", "next": "/dashboard"},
                    follow_redirects=False,
                )
                self.assertEqual(303, blocked.status_code)
                self.assertIn("/login", blocked.headers["location"])
                self.assertIn("Too+many+failed+attempts", blocked.headers["location"])
            finally:
                fresh.close()

            # The blocked correct-password attempt never reaches credential
            # verification, so it cannot clear the counter. The block only
            # lifts when the sliding window expires; simulate expiry with the
            # ops reset hook (a process restart has the same effect).
            reset_login_rate_limit_state()
            fresh = TestClient(self.app)
            try:
                recovered = fresh.post(
                    "/login",
                    data={"username": "admin", "password": "admin1234", "next": "/dashboard"},
                    follow_redirects=False,
                )
                self.assertEqual(303, recovered.status_code)
                self.assertEqual("/dashboard", recovered.headers["location"])
            finally:
                fresh.close()
        finally:
            reset_login_rate_limit_state()

    # -- cookie hardening ---------------------------------------------------------

    def test_auth_cookie_omits_secure_on_http(self) -> None:
        fresh = TestClient(self.app)
        try:
            response = fresh.post(
                "/login",
                data={"username": "admin", "password": "admin1234", "next": "/dashboard"},
                follow_redirects=False,
            )
            set_cookie = response.headers["set-cookie"].lower()
            self.assertIn("httponly", set_cookie)
            self.assertNotIn("secure", set_cookie)
        finally:
            fresh.close()

    def test_auth_cookie_sets_secure_on_https(self) -> None:
        fresh = TestClient(self.app, base_url="https://testserver")
        try:
            response = fresh.post(
                "/login",
                data={"username": "admin", "password": "admin1234", "next": "/dashboard"},
                follow_redirects=False,
            )
            set_cookie = response.headers["set-cookie"].lower()
            self.assertIn("httponly", set_cookie)
            self.assertIn("secure", set_cookie)
        finally:
            fresh.close()

    def test_auth_cookie_secure_override_to_true(self) -> None:
        os.environ["PQW_AUTH_COOKIE_SECURE"] = "true"

        from app.core.config import reset_settings_cache

        reset_settings_cache()
        try:
            fresh = TestClient(self.app)
            try:
                response = fresh.post(
                    "/login",
                    data={"username": "admin", "password": "admin1234", "next": "/dashboard"},
                    follow_redirects=False,
                )
                self.assertIn("secure", response.headers["set-cookie"].lower())
            finally:
                fresh.close()
        finally:
            reset_settings_cache()


if __name__ == "__main__":
    unittest.main()
