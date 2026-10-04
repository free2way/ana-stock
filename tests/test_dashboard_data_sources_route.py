"""Data-sources HTTP entry with fixed summary input."""

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

from app.api.routes.dashboard import concepts
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


def fixture_summary(*, populated=True, hostile=False):
    suffix = "<script>alert(1)</script>" if hostile else ""
    providers = [{"provider": f"tushare{suffix}", "count": 7}, {"provider": "yfinance", "count": 3}]
    states = [{"ticker": f"600000.SS{suffix}", "name": f"浦发银行{suffix}",
               "provider": f"tushare{suffix}", "message": f"ok{suffix}",
               "last_synced_date": "2026-09-30", "status": f"success{suffix}"}]
    return {
        "generated_at": "2026-09-30T10:00:00+08:00",
        "data_sources": {
            "primary_provider": f"tushare{suffix}" if populated else None,
            "current_provider_breakdown": providers if populated else [],
            "historical_price_strategy": [f"primary{suffix}", "fallback"],
            "symbol_profile_strategy": ["profiles"],
            "concept_strategy": ["concepts"],
            "supplemental_source_strategy": ["supplemental"],
            "concept_data": {"freshness": f"fresh{suffix}", "latest_as_of_date": "2026-09-30",
                             "concept_count": 12, "symbol_count": 99},
        },
        "sync_states": states if populated else [],
    }


def render_data_sources(*, lang="en", populated=True, hostile=False, authenticated=True, lookback=5):
    app = FastAPI()
    app.include_router(concepts.router)
    db = MagicMock()
    app.dependency_overrides[get_db_session] = lambda: db
    summary = fixture_summary(populated=populated, hostile=hostile)
    original = deepcopy(summary)
    with patch.object(concepts, "is_authenticated", return_value=authenticated), patch.object(
        concepts, "_load_home_summary", return_value=summary
    ) as loader, TestClient(app) as client:
        response = client.get("/dashboard/data-sources", params={"lang": lang, "lookback_runs": lookback},
                              follow_redirects=False)
    if summary != original:
        raise AssertionError("Rendering mutated the summary")
    return response, loader


class DashboardDataSourcesRouteTests(unittest.TestCase):
    def test_language_populated_empty_and_lookback_matrix(self):
        for lang in ("en", "zh"):
            for populated in (False, True):
                for lookback, expected in ((3, 3), (999, 5)):
                    with self.subTest(lang=lang, populated=populated, lookback=lookback):
                        response, loader = render_data_sources(lang=lang, populated=populated, lookback=lookback)
                        self.assertEqual(200, response.status_code)
                        self.assertEqual(expected, loader.call_args.kwargs["lookback_runs"])
                        self.assertIn("这个应用的数据来自哪里" if lang == "zh" else "Where This App Gets Data",
                                      response.text)
                        self.assertIn("暂无同步记录" if lang == "zh" and not populated else
                                      "No sync history yet" if not populated else "600000.SS", response.text)

    def test_named_template_matches_pre_refactor_safe_contract(self):
        fixture_path = Path(__file__).parent / "fixtures" / "dashboard_data_sources_render_contract.json"
        expected = json.loads(fixture_path.read_text())
        actual = {}
        for lang in ("en", "zh"):
            for populated in (False, True):
                for lookback in (3, 999):
                    response, _ = render_data_sources(lang=lang, populated=populated, lookback=lookback)
                    actual[f"{lang}-{populated}-{lookback}"] = PageContract(response.text).digest()
        self.assertEqual(expected, actual)

    def test_external_values_are_escaped(self):
        response, _ = render_data_sources(hostile=True)
        self.assertEqual(200, response.status_code)
        self.assertNotIn("<script>alert(1)</script>", response.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)

    def test_auth_blocks_summary_load(self):
        response, loader = render_data_sources(authenticated=False)
        self.assertIn(response.status_code, (302, 303, 307))
        self.assertIn("/login", response.headers["location"])
        loader.assert_not_called()

    def test_route_is_orchestration_only_and_template_is_named(self):
        source = inspect.getsource(concepts.dashboard_data_sources)
        self.assertNotIn("<article", source)
        self.assertNotIn("fragments=", source)
        template = (Path(__file__).parents[1] / "app/api/templates/dashboard/data_sources.html").read_text()
        self.assertNotIn("fragments[", template)
        self.assertNotIn("|safe", template)
