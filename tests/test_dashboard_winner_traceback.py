"""Real winner-traceback HTTP entry; only auth and snapshot I/O are replaced."""

from copy import deepcopy
import ast
from html.parser import HTMLParser
import inspect
import hashlib
import json
import re
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.dashboard import performance
from app.core.db import get_db_session
from app.services.model_winner_traceback import summarize_winner_traceback


class PageContract(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.words = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, sorted(attrs)))

    def handle_data(self, data):
        self.words.append(data)

    def digest(self):
        value = (self.tags, re.sub(r"\s+", " ", " ".join(self.words)).strip())
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def fixture_winners():
    return [
        {"ticker": "FIX-A", "name": "A <script>alert('x')</script>", "market": "CN",
         "signal_date": "2026-09-29", "winner_date": "2026-09-30",
         "return_1d": 8.0, "hit_count": 2,
         "hits": [{"template": "lightgbm", "template_label": "Model <A>", "action_bucket": "buy"},
                  {"template": "technical_momentum", "action_bucket": "watch"}]},
        {"ticker": "FIX-B", "market": "US", "return_1d": 4.0, "hit_count": 1,
         "hits": [{"template": "lightgbm", "action_bucket": "unknown<&"}]},
        {"ticker": "FIX-C", "market": "CN", "return_1d": 6.0, "hit_count": 0},
    ]


def render_winners(*, market="CN", lang="en", min_hits=0, winners=None, source="snapshot"):
    app = FastAPI()
    app.include_router(performance.router)
    app.dependency_overrides[get_db_session] = lambda: MagicMock()
    guidance = {"winner_attribution": fixture_winners() if winners is None else winners,
                "snapshot_meta": {"source": source}}
    original = deepcopy(guidance)
    with patch.object(performance, "is_authenticated", return_value=True), patch.object(
        performance, "load_model_selection_guidance_snapshot", return_value=guidance
    ) as loader, TestClient(app) as client:
        response = client.get("/dashboard/model-performance/winner-traceback", params={
            "market": market, "lang": lang, "min_hits": min_hits,
        })
    if guidance != original:
        raise AssertionError("Rendering mutated the snapshot")
    return response, loader


class WinnerTracebackRouteTests(unittest.TestCase):
    def test_render_contract_matches_pre_refactor_baseline(self):
        from tests.test_dashboard_performance_route import DashboardPerformanceRouteTests

        baseline = json.loads((Path(__file__).parent / "fixtures/dashboard_performance_render_contract.json").read_text())
        actual = {}
        for market in ("CN", "US", "ALL"):
            for lang in ("zh", "en"):
                for min_hits in (0, 1, 2, 3):
                    response, _ = render_winners(market=market, lang=lang, min_hits=min_hits)
                    actual[f"winner-{market}-{lang}-{min_hits}"] = PageContract(response.text).digest()
                for populated in (False, True):
                    response = DashboardPerformanceRouteTests()._render(market=market, lang=lang, populated=populated)
                    actual[f"performance-{market}-{lang}-{populated}"] = PageContract(response.text).digest()
        for lang in ("zh", "en"):
            actual[f"empty-{lang}"] = PageContract(render_winners(lang=lang, winners=[])[0].text).digest()
        self.assertEqual(baseline, actual)

    def test_summary_preserves_full_denominator_counts_order_and_inputs(self):
        winners = fixture_winners()
        winners[0]["hits"].append(dict(winners[0]["hits"][0]))
        original = deepcopy(winners)
        result = summarize_winner_traceback(winners, min_hits=2, bucket_labels={"buy": "Buy"})
        self.assertEqual(3, result["total"])
        self.assertEqual(2, result["captured"])
        self.assertEqual(1, result["missed"])
        self.assertAlmostEqual(200 / 3, result["capture_rate"])
        self.assertEqual(6.0, result["avg_return"])
        self.assertEqual(["FIX-A"], [row["ticker"] for row in result["rows"]])
        self.assertEqual(("Model <A>", 2), result["templates"][0])
        self.assertEqual(("Buy", 2), result["buckets"][0])
        self.assertEqual(original, winners)
        self.assertIsNone(summarize_winner_traceback([], min_hits=0, bucket_labels={})["avg_return"])
        missing = summarize_winner_traceback([{"hit_count": 1}, {"return_1d": 6}], min_hits=0, bucket_labels={})
        self.assertEqual(3.0, missing["avg_return"])
        self.assertEqual(1, missing["captured"])

    def test_route_and_service_boundaries(self):
        for route in (performance.dashboard_model_performance, performance.dashboard_model_winner_traceback):
            source = inspect.getsource(route)
            self.assertNotIn("<tr>", source)
            self.assertNotIn("<article", source)
            self.assertNotIn("fragments=[", source)
        tree = ast.parse(inspect.getsource(inspect.getmodule(summarize_winner_traceback)))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertEqual(["collections"], imports)
        source = inspect.getsource(performance.dashboard_model_winner_traceback)
        self.assertIn("summarize_winner_traceback(", source)
        self.assertIn("render_winner_traceback_page(", source)
        template = Path(__file__).parents[1] / "app/api/templates/dashboard/winner_traceback.html"
        self.assertNotIn("fragments[", template.read_text())
        self.assertNotIn("|safe", template.read_text())

    def test_market_language_and_hit_filter_matrix(self):
        for market in ("CN", "US", "ALL"):
            for lang in ("zh", "en"):
                for min_hits in (0, 1, 2, 3):
                    with self.subTest(market=market, lang=lang, min_hits=min_hits):
                        response, loader = render_winners(market=market, lang=lang, min_hits=min_hits)
                        self.assertEqual(200, response.status_code)
                        self.assertEqual(market, loader.call_args.kwargs["market"])
                        self.assertTrue(loader.call_args.kwargs["allow_fallback"])
                        self.assertIn("66.7%", response.text)
                        self.assertIn("6.00%", response.text)
                        self.assertEqual(min_hits <= 2, "FIX-A" in response.text)
                        self.assertEqual(min_hits <= 1, "FIX-B" in response.text)
                        self.assertEqual(min_hits == 0, "FIX-C" in response.text)
                        self.assertNotIn("<script>alert", response.text)
                        if min_hits <= 2:
                            self.assertIn("&lt;script&gt;", response.text)

    def test_empty_normalized_inputs_and_auth(self):
        for lang in ("zh", "en"):
            response, loader = render_winners(market="invalid", lang=lang, min_hits=-2, winners=[])
            self.assertEqual(200, response.status_code)
            self.assertEqual("CN", loader.call_args.kwargs["market"])
            self.assertIn("没有强票归因样本" if lang == "zh" else "No winner-attribution samples", response.text)
        app = FastAPI()
        app.include_router(performance.router)
        app.dependency_overrides[get_db_session] = lambda: MagicMock()
        with patch.object(performance, "is_authenticated", return_value=False), patch.object(
            performance, "load_model_selection_guidance_snapshot"
        ) as loader, TestClient(app) as client:
            response = client.get("/dashboard/model-performance/winner-traceback", follow_redirects=False)
        self.assertIn(response.status_code, (302, 303, 307))
        self.assertIn("/login", response.headers["location"])
        loader.assert_not_called()

    def test_detail_limit_and_links_preserve_existing_contract(self):
        rows = [{"ticker": f"LIMIT-{i}", "market": "US", "hit_count": 1,
                 "hits": [{"template": "lightgbm", "action_bucket": "buy"}]}
                for i in range(81)]
        response, _ = render_winners(winners=rows)
        self.assertIn("LIMIT-79", response.text)
        self.assertNotIn("LIMIT-80", response.text)
        links = [dict(attrs).get("href", "") for tag, attrs in PageContract(response.text).tags if tag == "a"]
        self.assertTrue(any("model_template=lightgbm&market=US&universe=full_market" in href for href in links))
        self.assertIn("100.0%", response.text)
