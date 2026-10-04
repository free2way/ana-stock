from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api.presentation.screener_pages import (
    render_quality_profile_status,
    render_screener_regression_discipline,
    render_screener_main_page,
    render_selection_quality_page,
    render_today_focus_page,
)
from app.api.presentation import screener_main
from app.api.routes import screener


class ScreenerTemplateRenderingTests(unittest.TestCase):
    def test_main_presenter_has_no_reverse_dependency_on_routes(self) -> None:
        source = Path(screener_main.__file__).read_text()

        self.assertNotIn("app.api.routes", source)

    def test_main_screener_document_is_owned_by_template(self) -> None:
        tree = ast.parse(Path(screener.__file__).read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "screener_page"
        )
        route_call = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "render_main_screener_view"
        )
        self.assertGreater(len(route_call.keywords), 20)
        presenter_tree = ast.parse(inspect.getsource(screener_main.render_main_screener_view))
        render_call = next(
            node
            for node in ast.walk(presenter_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "render_screener_main_page"
        )
        fragment_keyword = next(item for item in render_call.keywords if item.arg == "fragments")
        self.assertIsInstance(fragment_keyword.value, ast.List)
        fragment_count = len(fragment_keyword.value.elts)
        self.assertGreater(fragment_count, 100)
        self.assertFalse(
            any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "<html" in node.value.lower()
                for node in ast.walk(function)
            )
        )
        template = (Path(screener.__file__).parents[1] / "templates" / "screeners" / "main.html").read_text()
        referenced = {int(value) for value in re.findall(r"fragments\[(\d+)\]", template)}
        self.assertEqual(set(range(fragment_count)), referenced)

        fragments = [""] * fragment_count
        fragments[0] = '<script>alert("lang")</script>'
        rendered = render_screener_main_page(fragments=fragments)
        self.assertNotIn('<script>alert("lang")</script>', rendered)

    def test_main_screener_fragments_escape_service_guidance(self) -> None:
        discipline = render_screener_regression_discipline(
            lang="en",
            recommendation_regression={
                "sample_count": 3,
                "policy": {"min_actionable_quality_score": '<script>alert("x")</script>'},
            },
            regression_guidance={
                "headline": "<img src=x onerror=alert(1)>",
                "warnings": ["<b>unsafe warning</b>"],
                "metrics": [{"label": "<em>metric</em>", "value": 3}],
            },
            status_counts={"ready": 1},
            visible_count=2,
        )
        quality = render_quality_profile_status(
            lang="en",
            regime="<script>risk</script>",
            breadth="<img src=x>",
            candidate_count=0,
        )

        self.assertNotIn("<img src=x onerror", discipline)
        self.assertIn("&lt;b&gt;unsafe warning&lt;/b&gt;", discipline)
        self.assertIn("&lt;em&gt;metric&lt;/em&gt;", discipline)
        self.assertNotIn("<script>risk</script>", quality)
        self.assertIn("no new entries", quality)

    def test_selection_quality_template_escapes_untrusted_snapshot_data(self) -> None:
        page = render_selection_quality_page(
            lang="en",
            nav_html="<a href='/dashboard'>Dashboard</a>",
            payload={
                "sample_count": 1,
                "summary": {
                    "all": {"hit_rate_1d_pct": 50},
                    "by_source": [
                        {
                            "source_name": "<script>source</script>",
                            "source_type": "model",
                            "metrics": {"count": 1, "available_1d": 1},
                        }
                    ],
                },
                "guidance": {
                    "headline_en": "<img src=x onerror=alert(1)>",
                    "rules_en": ["<b>rule</b>"],
                },
                "recent_records": [
                    {
                        "ticker": "BAD/<script>",
                        "name": "<svg onload=alert(1)>",
                        "risk_flags": ["<em>risk</em>"],
                    }
                ],
            },
        )

        self.assertNotIn("<script>source</script>", page)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<svg onload", page)
        self.assertIn("&lt;b&gt;rule&lt;/b&gt;", page)
        self.assertIn("&lt;em&gt;risk&lt;/em&gt;", page)
        self.assertIn("Selection Quality", page)

    def test_selection_quality_route_is_a_thin_template_adapter(self) -> None:
        tree = ast.parse(Path(screener.__file__).read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "selection_quality_page"
        )
        calls = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertFalse(any(isinstance(node, ast.JoinedStr) for node in ast.walk(function)))
        self.assertIn("load_or_build_selection_quality", calls)
        self.assertIn("render_selection_quality_page", calls)

    def test_selection_quality_route_loads_service_payload_and_renders(self) -> None:
        payload = {
            "sample_count": 12,
            "summary": {"all": {"hit_rate_1d_pct": 58.5}, "by_source": []},
            "guidance": {"headline_zh": "真实成交口径", "rules_zh": ["仅看成熟样本"]},
            "recent_records": [],
        }
        with (
            patch.object(screener, "is_authenticated", return_value=True),
            patch.object(screener, "resolve_request_lang", return_value="zh"),
            patch.object(screener, "load_or_build_selection_quality", return_value=payload),
            patch.object(screener, "render_workspace_nav_html", return_value="<a>nav</a>"),
        ):
            page = screener.selection_quality_page(MagicMock(), lang="zh", db=MagicMock())

        self.assertIn("命中率闭环", page)
        self.assertIn("真实成交口径", page)
        self.assertIn("58.50%", page)

    def test_today_focus_template_escapes_row_data_and_allows_trusted_fragments(self) -> None:
        page = render_today_focus_page(
            lang="en",
            nav_html="<a href='/dashboard'>Dashboard</a>",
            rows=[
                {
                    "ticker": "BAD/<script>",
                    "name": "<img src=x onerror=alert(1)>",
                    "market": "CN",
                    "patterns": "<b>breakout</b>",
                    "model_signal_label": "BUY",
                    "model_signal_strength": 88,
                    "watchlist_html": "<span class='trusted-watchlist'>Ready</span>",
                    "sync_badge_html": "<span class='trusted-sync'>Synced</span>",
                }
            ],
        )

        self.assertNotIn("<script>", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;b&gt;breakout&lt;/b&gt;", page)
        self.assertIn("trusted-watchlist", page)
        self.assertIn("trusted-sync", page)
        self.assertIn("Today Focus Pool", page)

    def test_today_focus_route_contains_no_page_sized_f_string(self) -> None:
        tree = ast.parse(Path(screener.__file__).read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "today_focus_pool_page"
        )
        self.assertFalse(any(isinstance(node, ast.JoinedStr) for node in ast.walk(function)))
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "render_today_focus_page"
                for node in ast.walk(function)
            )
        )

    def test_today_focus_route_loads_data_and_renders_the_template(self) -> None:
        repository = MagicMock()
        repository.get_or_create_default.return_value = SimpleNamespace(id=7)
        repository.list_ticker_map.return_value = {}
        items = [
            {
                "ticker": "600000.SS",
                "name": "浦发银行",
                "market": "CN",
                "matched_patterns": ["breakout"],
                "model_signal_label": "BUY",
                "model_signal_strength": 80,
            }
        ]

        with (
            patch.object(screener, "is_authenticated", return_value=True),
            patch.object(screener, "WatchlistRepository", return_value=repository),
            patch.object(screener, "_load_today_focus_items", return_value=items),
            patch.object(screener, "render_workspace_nav_html", return_value="<a>nav</a>"),
        ):
            page = screener.today_focus_pool_page(MagicMock(), lang="zh", db=MagicMock())

        self.assertIn("今日重点盯盘池", page)
        self.assertIn("600000.SS", page)
        self.assertIn("浦发银行", page)
        self.assertIn("breakout", page)

    def test_main_screener_real_route_entry_renders_without_executing_screen(self) -> None:
        repository = MagicMock()
        repository.get_or_create_default.return_value = SimpleNamespace(id=7)
        repository.list_ticker_map.return_value = {}
        request = MagicMock()
        request.query_params = {}

        with (
            patch.object(screener, "is_authenticated", return_value=True),
            patch.object(screener, "resolve_request_lang", return_value="en"),
            patch.object(screener, "_load_saved_presets", return_value=[]),
            patch.object(screener, "WatchlistRepository", return_value=repository),
            patch.object(screener, "load_or_build_recommendation_regression", return_value={}),
            patch.object(
                screener,
                "summarize_recommendation_regression",
                return_value={"headline": "", "metrics": [], "rules": [], "warnings": []},
            ),
            patch.object(screener, "load_model_selection_guidance_snapshot", return_value={}),
            patch.object(screener, "summarize_model_selection_guidance", return_value={}),
            patch.object(screener_main, "render_workspace_nav_html", return_value="<a>nav</a>"),
        ):
            page = screener.screener_page(
                request,
                message=None,
                lang="en",
                model_template="technical_momentum",
                multi_model_templates=[],
                min_multi_model_hits=2,
                confluence_action_filter="ALL",
                strategy_profile="",
                universe="full_market",
                market="ALL",
                min_trend_score=60,
                action_filter="ALL",
                min_volume_ratio=0.0,
                min_listing_days=365,
                pe_min=0.0,
                pe_max=30.0,
                min_roe_avg_3y=12.0,
                min_net_profit_yoy=20.0,
                min_revenue_yoy=0.0,
                max_debt_to_assets=100.0,
                min_dividend_yield=0.0,
                exclude_bottom_market_cap_pct=10.0,
                recent_snapshot_runs=0,
                min_snapshot_hits=0,
                model_signal_filter="ALL",
                min_model_signal_strength=0.0,
                execution_tag_filter="ALL",
                exclude_execution_tag_filter="ALL",
                sort_by="default",
                sort_order="desc",
                show_evaluation=0,
                show_details=0,
                db=MagicMock(),
            )

        self.assertIn("Strategy Workbench", page)
        self.assertIn("Technical Momentum", page)
        self.assertIn("Run screener", page)


if __name__ == "__main__":
    unittest.main()
