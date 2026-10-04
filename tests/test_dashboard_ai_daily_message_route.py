"""AI daily-report push-text HTTP entry with fixed service inputs."""

from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import inspect
import json
from pathlib import Path
import re
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.dashboard import reports
from app.core.db import get_db_session


class PageContract(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags, self.words = [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, sorted(attrs)))

    def handle_data(self, data):
        self.words.append(data)

    def digest(self):
        value = (self.tags, re.sub(r"\s+", " ", " ".join(self.words)).strip())
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def render_message_page(*, lang="en", has_social=False, authenticated=True, message="Line 1\nLine 2"):
    app = FastAPI()
    app.include_router(reports.router)
    db = MagicMock()
    app.dependency_overrides[get_db_session] = lambda: db
    report = {
        "headline": "fixture",
        "social_signal_summary": {"accounts": ["saved"], "actionable": []} if has_social else None,
    }
    original = deepcopy(report)
    live_social = {"accounts": ["live"], "actionable": [{"ticker": "FIX"}]}
    with patch.object(reports, "is_authenticated", return_value=authenticated), patch.object(
        reports, "_load_cached_ai_daily_report", return_value=report
    ) as loader, patch.object(
        reports, "social_signal_summary", return_value=live_social
    ) as social, patch.object(
        reports, "render_ai_daily_report_message", return_value=message
    ) as renderer, TestClient(app) as client:
        response = client.get("/dashboard/ai-daily-report/message", params={"lang": lang}, follow_redirects=False)
    if report != original:
        raise AssertionError("Rendering mutated the cached report")
    return response, loader, social, renderer


class DashboardAiDailyMessageRouteTests(unittest.TestCase):
    def test_language_and_social_summary_fallback(self):
        for lang in ("en", "zh"):
            with self.subTest(lang=lang):
                response, _, social, renderer = render_message_page(lang=lang)
                self.assertEqual(200, response.status_code)
                self.assertIn("推送文本" if lang == "zh" else "Push Text", response.text)
                social.assert_called_once()
                payload = renderer.call_args.args[0]
                self.assertEqual(["live"], payload["social_signal_summary"]["accounts"])

    def test_cached_social_summary_is_preserved(self):
        response, _, social, renderer = render_message_page(has_social=True)
        self.assertEqual(200, response.status_code)
        social.assert_not_called()
        self.assertEqual(["saved"], renderer.call_args.args[0]["social_signal_summary"]["accounts"])

    def test_message_is_escaped_inside_textarea(self):
        response, *_ = render_message_page(message="<script>alert(1)</script> & ready")
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; ready", response.text)

    def test_named_template_matches_pre_refactor_contract(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "dashboard_ai_daily_message_render_contract.json").read_text()
        )
        actual = {}
        for lang in ("en", "zh"):
            for has_social in (False, True):
                response, *_ = render_message_page(lang=lang, has_social=has_social)
                actual[f"{lang}-{has_social}"] = PageContract(response.text).digest()
        self.assertEqual(fixture, actual)

    def test_auth_blocks_data_access(self):
        response, loader, social, renderer = render_message_page(authenticated=False)
        self.assertIn(response.status_code, (302, 303, 307))
        self.assertIn("/login", response.headers["location"])
        loader.assert_not_called()
        social.assert_not_called()
        renderer.assert_not_called()

    def test_route_is_orchestration_only_and_template_is_named(self):
        source = inspect.getsource(reports.dashboard_ai_daily_report_message)
        self.assertNotIn("fragments=", source)
        template = (Path(__file__).parents[1] / "app/api/templates/dashboard/ai_daily_report_message.html")
        self.assertTrue(template.exists())
        self.assertNotIn("fragments[", template.read_text())
        self.assertNotIn("|safe", template.read_text())
