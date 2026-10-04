"""Archived AI-report detail HTTP entry with isolated report inputs."""

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


def fixture_report(*, populated=True, hostile=False):
    suffix = "<script>alert(1)</script>" if hostile else ""
    rows = [] if not populated else [{
        "ticker": f"FIX-A{suffix}", "name": f"Fixture A{suffix}", "verdict": f"BUY{suffix}",
        "tradability_status": "actionable", "report_pool_reason": f"ranked{suffix}",
        "quant_rank": 1, "verification_score": 72, "trade_readiness_score": 80,
        "readiness_bucket": f"ready{suffix}", "entry_trigger": f"breakout{suffix}",
        "invalidation_condition": f"close below support{suffix}", "latest_price": 12.345,
        "close_vs_buy_zone_high_pct": 1.2, "block_reason": "", "market": "CN",
        "headline": f"Fixture headline{suffix}",
    }]
    return {
        "report_date": "2026-10-01",
        "headline": f"Archived fixture report{suffix}",
        "lightgbm_execution_bias": {"title": f"Neutral{suffix}", "summary": f"Wait for confirmation{suffix}"},
        "market_recommendations": rows,
    }


def fixture_outcomes(*, populated=True, hostile=False):
    if not populated:
        return []
    suffix = "<script>alert(1)</script>" if hostile else ""
    return [{
        "ticker": f"FIX-A{suffix}", "name": f"Fixture A{suffix}",
        "baseline_date": "2026-10-01", "baseline_close": 12.0,
        "latest_date": "2026-10-02", "latest_close": 12.6,
        "return_pct": 5.0, "status": "measured",
    }]


def render_history_detail(*, lang="en", populated=True, hostile=False, missing=False, authenticated=True):
    app = FastAPI()
    app.include_router(reports.router)
    db = MagicMock()
    app.dependency_overrides[get_db_session] = lambda: db
    report = fixture_report(populated=populated, hostile=hostile)
    outcomes = fixture_outcomes(populated=populated, hostile=hostile)
    snapshot = None if missing else {
        "id": 77,
        "snapshot_date": "2026-10-01",
        "created_at": "2026-10-01T20:30:00+08:00",
        "payload": {"source": "fixture"},
    }
    originals = deepcopy((report, outcomes, snapshot))
    with patch.object(reports, "is_authenticated", return_value=authenticated), patch.object(
        reports, "load_ai_daily_report_history_item", return_value=snapshot
    ) as loader, patch.object(
        reports, "_hydrate_ai_report_names", return_value=report
    ) as hydrator, patch.object(
        reports, "render_ai_daily_report_message", return_value=f"Message{('<b>unsafe</b>' if hostile else '')}"
    ), patch.object(
        reports, "_render_ai_report_guidance_bridge",
        return_value="<section class='card' id='model-guidance'>Guidance</section>",
    ), patch.object(
        reports, "_report_outcome_rows", return_value=outcomes
    ), patch.object(
        reports, "_report_outcome_summary", return_value=("No measured rows" if not populated else "1 measured row")
    ), patch.object(
        reports, "format_trade_status", return_value="Executable"
    ), patch.object(
        reports, "build_trade_explain_text", return_value=f"Explain{('<i>unsafe</i>' if hostile else '')}"
    ), TestClient(app) as client:
        response = client.get("/dashboard/ai-daily-report/history/77", params={"lang": lang},
                              follow_redirects=False)
    if (report, outcomes, snapshot) != originals:
        raise AssertionError("Rendering mutated report inputs")
    return response, loader, hydrator


class DashboardAiReportHistoryDetailRouteTests(unittest.TestCase):
    def test_language_populated_empty_and_links(self):
        for lang in ("en", "zh"):
            for populated in (False, True):
                with self.subTest(lang=lang, populated=populated):
                    response, _, _ = render_history_detail(lang=lang, populated=populated)
                    self.assertEqual(200, response.status_code)
                    self.assertIn("历史详情" if lang == "zh" else "History Detail", response.text)
                    self.assertIn("Archived fixture report", response.text)
                    self.assertIn(f"/dashboard/ai-daily-report/history?lang={lang}", response.text)
                    if populated:
                        self.assertIn("FIX-A", response.text)
                        self.assertIn("1 measured row", response.text)
                        self.assertIn("/insights/FIX-A", response.text)
                        self.assertIn("model_template=lightgbm_top_picks", response.text)
                    else:
                        self.assertIn("暂无可验证记录" if lang == "zh" else "No measurable records yet", response.text)
                        self.assertIn("当日没有可执行买入池记录" if lang == "zh" else
                                      "No executable buy-pool rows", response.text)

    def test_external_values_and_message_are_escaped(self):
        response, _, _ = render_history_detail(hostile=True)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertNotIn("<b>unsafe</b>", response.text)
        self.assertNotIn("<i>unsafe</i>", response.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)
        self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;", response.text)
        self.assertIn("&lt;i&gt;unsafe&lt;/i&gt;", response.text)
        self.assertIn("id='model-guidance'", response.text)

    def test_missing_and_auth_short_circuit(self):
        missing, _, hydrator = render_history_detail(missing=True)
        blocked, loader, blocked_hydrator = render_history_detail(authenticated=False)
        self.assertEqual(404, missing.status_code)
        hydrator.assert_not_called()
        self.assertIn(blocked.status_code, (302, 303, 307))
        loader.assert_not_called()
        blocked_hydrator.assert_not_called()

    def test_named_template_matches_pre_refactor_contract(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "dashboard_ai_report_history_detail_render_contract.json")
            .read_text()
        )
        actual = {}
        for lang in ("en", "zh"):
            for populated in (False, True):
                response, _, _ = render_history_detail(lang=lang, populated=populated)
                actual[f"{lang}-{populated}"] = PageContract(response.text).digest()
        self.assertEqual(fixture, actual)

    def test_route_is_orchestration_only_and_template_is_named(self):
        source = inspect.getsource(reports.dashboard_ai_daily_report_history_detail)
        self.assertNotIn("<tr>", source)
        self.assertNotIn("fragments=", source)
        template = Path(__file__).parents[1] / "app/api/templates/dashboard/ai_daily_report_history_detail.html"
        self.assertTrue(template.exists())
        self.assertNotIn("fragments[", template.read_text())
        self.assertNotIn("|safe", template.read_text())
