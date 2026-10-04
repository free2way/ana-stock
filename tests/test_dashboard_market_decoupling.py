from __future__ import annotations

import ast
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from app.api.presentation import dashboard_heatmap, dashboard_market
from app.api.routes.dashboard import market
from app.services.market_heatmap import filter_market_heatmap_rows, summarize_heatmap_execution_risks
from app.services.market_pulse import filter_market_pulse_signals, summarize_market_pulse_signals


class DashboardMarketDecouplingTests(unittest.TestCase):
    def test_heatmap_filtering_is_pure_and_sorts_after_filters(self) -> None:
        rows = [
            {
                "label": "Technology",
                "market": "CN",
                "hits": 3,
                "avg_score": 80,
                "buy_signal_count": 2,
                "max_signal_strength": 90,
                "execution_tags": ["gap-risk"],
                "ticker_details": [{"signal_label": "BUY"}],
            },
            {
                "label": "Banks",
                "market": "CN",
                "hits": 5,
                "avg_score": 60,
                "buy_signal_count": 0,
                "max_signal_strength": 70,
                "ticker_details": [{"signal_label": "WATCH"}],
            },
            {
                "label": "U.S. Tech",
                "market": "US",
                "hits": 10,
                "buy_signal_count": 4,
                "max_signal_strength": 95,
            },
        ]
        original = [dict(row) for row in rows]

        filtered = filter_market_heatmap_rows(
            rows,
            market_filter="CN",
            signal_filter="BUY",
            min_signal_strength=80,
            sort_by="hits",
        )
        risk = summarize_heatmap_execution_risks(filtered)

        self.assertEqual(original, rows)
        self.assertEqual(["Technology"], [row["label"] for row in filtered])
        self.assertEqual(1, risk["tagged_names"])
        self.assertEqual([("gap-risk", 1)], risk["risk_top_tags"])

    def test_market_signal_policy_is_pure_and_explains_market_tone(self) -> None:
        source = [
            {
                "ticker": "AAA",
                "market": "CN",
                "signal_label": "BUY",
                "signal_strength": 82,
                "risk_flags": ["low-conviction"],
            },
            {
                "ticker": "BBB",
                "market": "CN",
                "signal_label": "SELL",
                "signal_strength": 75,
                "risk_flags": ["gap-risk"],
            },
            {
                "ticker": "CCC",
                "market": "US",
                "signal_label": "BUY",
                "signal_strength": 90,
            },
        ]
        original = [dict(row) for row in source]

        filtered = filter_market_pulse_signals(
            source,
            buy_hit_counts={"AAA": 2, "BBB": 1},
            market_filter="CN",
            min_signal_strength=70,
            min_buy_signal_count=1,
        )
        summary = summarize_market_pulse_signals(filtered, lang="en")

        self.assertEqual(source, original)
        self.assertEqual(["AAA", "BBB"], [row["ticker"] for row in filtered])
        self.assertEqual([], filtered[0]["market_risk_tags"])
        self.assertEqual(["gap-risk"], filtered[1]["market_risk_tags"])
        self.assertEqual(1, summary["tagged_names"])
        self.assertEqual({"BUY": 1, "WATCH": 0, "SELL": 1, "HOLD": 0}, summary["signal_bucket_counts"])
        self.assertEqual("Risk-on", summary["market_tone"])

    def _view(self) -> dict:
        return {
            "lang": "en",
            "lookback_runs": 5,
            "heatmap_sort": "hits",
            "market_filter": "CN",
            "kpi_focus": "all",
            "market_mode": "monitor",
            "signal_filter": "ALL",
            "min_signal_strength": 0,
            "min_buy_signal_count": 0,
            "execution_tag_filter": "ALL",
            "exclude_execution_tag_filter": "ALL",
            "nav_html": "<a href='/dashboard'>Dashboard</a>",
            "mode_pills": "<a>Monitor</a>",
            "market_pills": "<a>CN</a>",
            "active_mode_meta": {
                "eyebrow": "Market Pulse",
                "headline": "Track leadership",
                "help": "Observe the session",
            },
            "market_scope_title": "A-Shares",
            "market_tone": "Watchful",
            "mode_steps_html": "<div>Heat check</div>",
            "filtered_signal_count": 2,
            "signal_bucket_counts": {"BUY": 1},
            "tagged_names": 1,
            "board_count": 3,
            "heatmap_updated_at": "2026-10-02T18:00:00+08:00",
            "risk_top_tags_html": "<span>gap-risk · 1</span>",
            "risk_examples_html": "<script>alert(1)</script>",
            "kpi_detail_section": "",
            "heatmap_preview_html": "<a>Semiconductors</a>",
            "continuous_preview_html": "<a>Leader</a>",
            "concept_preview_html": "<a>AI theme</a>",
            "market_kpi_links": {
                "focused": "#focused",
                "buy": "#buy",
                "risk": "#risk",
                "boards": "#boards",
            },
            "active_focus_classes": {
                "focused": "",
                "buy": "",
                "risk": "",
                "boards": "",
            },
            "market_scope_cards_html": "<a>CN scope</a>",
            "market_scope_help": "Unified methodology",
            "top_signal_rows": "<article>Sample</article>",
            "lookback_pills": "<a>5</a>",
            "signal_pills": "<a>All</a>",
        }

    def test_market_route_delegates_document_rendering_to_presenter(self) -> None:
        tree = ast.parse(Path(market.__file__).read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "dashboard_market_page"
        )

        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "render_dashboard_market_page"
                for node in ast.walk(function)
            )
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "<!DOCTYPE html>" in node.value
                for node in ast.walk(function)
            )
        )

    def test_market_presenter_has_no_reverse_dependency_on_routes(self) -> None:
        self.assertNotIn("app.api.routes", Path(dashboard_market.__file__).read_text())

    def test_market_template_renders_and_escapes_scalar_state(self) -> None:
        page = dashboard_market.render_dashboard_market_page(self._view())

        self.assertIn("Market Pulse", page)
        self.assertIn("Track leadership", page)
        self.assertIn("Semiconductors", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_market_real_route_entry_renders_from_empty_snapshots(self) -> None:
        repository = MagicMock()
        repository.list_latest_signal_decisions.return_value = []
        repository.count_recent_signal_hits.return_value = {}

        with (
            patch.object(market, "is_authenticated", return_value=True),
            patch.object(market, "PredictionRepository", return_value=repository),
            patch.object(market, "load_latest_workspace_snapshot", return_value=None),
            patch.object(market, "_load_concept_tracker_rows", return_value=[]),
            patch.object(market, "_recent_market_heat_history", return_value={"CN": [], "US": []}),
            patch.object(market, "render_workspace_nav_html", return_value="<a>nav</a>"),
        ):
            page = market.dashboard_market_page(
                MagicMock(),
                lang="en",
                lookback_runs=5,
                heatmap_sort="hits",
                market_filter="CN",
                kpi_focus="ALL",
                mode="monitor",
                signal_filter="ALL",
                min_signal_strength=0,
                min_buy_signal_count=0,
                execution_tag_filter="ALL",
                exclude_execution_tag_filter="ALL",
                db=MagicMock(),
            )

        self.assertIn("Market Pulse", page)
        self.assertIn("No candidates match the current focus", page)

    def test_heatmap_route_delegates_document_rendering_to_presenter(self) -> None:
        tree = ast.parse(Path(market.__file__).read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "dashboard_market_heatmap_page"
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "render_dashboard_heatmap_page"
                for node in ast.walk(function)
            )
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "<!DOCTYPE html>" in node.value
                for node in ast.walk(function)
            )
        )

    def test_heatmap_template_renders_and_escapes_scalar_state(self) -> None:
        view = {
            "lang": "en",
            "lookback_runs": 5,
            "heatmap_sort": "hits",
            "heatmap_metric": "model",
            "market_filter": "CN",
            "signal_filter": "ALL",
            "min_signal_strength": 0,
            "min_buy_signal_count": 0,
            "execution_tag_filter": "ALL",
            "exclude_execution_tag_filter": "ALL",
            "nav_html": "<a>nav</a>",
            "market_pills": "<a>CN</a>",
            "heatmap_method_notice": "",
            "loading_hint": "",
            "lookback_pills": "<a>5</a>",
            "heatmap_metric_pills": "<a>Model</a>",
            "signal_pills": "<a>All</a>",
            "tagged_names": 1,
            "risk_top_tags_html": "<span>gap-risk</span>",
            "risk_examples_html": "<script>alert(1)</script>",
            "heatmap_sort_pills": "<a>Hits</a>",
            "heatmap_tiles": "<a class='heat-tile'>Technology</a>",
            "heatmap_metric_label": "Model strength",
            "focus_detail_html": "",
            "heatmap_scope_cards_html": "<a>CN scope</a>",
            "heatmap_scope_help": "Unified methodology",
            "flow_summary_html": "<div>Flow ready</div>",
            "resonance_score": 52.5,
            "tracked_signal_count": 12,
        }

        page = dashboard_heatmap.render_dashboard_heatmap_page(view)

        self.assertIn("Sector Heatmap", page)
        self.assertIn("Technology", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_heatmap_real_route_entry_renders_from_empty_snapshot(self) -> None:
        request = MagicMock()
        request.query_params = {}
        with (
            patch.object(market, "is_authenticated", return_value=True),
            patch.object(market, "load_latest_workspace_snapshot", return_value=None),
            patch.object(market, "_recent_market_heat_history", return_value={"CN": [], "US": []}),
            patch.object(market, "render_workspace_nav_html", return_value="<a>nav</a>"),
        ):
            page = market.dashboard_market_heatmap_page(
                request,
                lang="en",
                lookback_runs=5,
                heatmap_sort="hits",
                heatmap_metric="model",
                heatmap_focus="",
                market_filter="CN",
                signal_filter="ALL",
                min_signal_strength=0,
                min_buy_signal_count=0,
                execution_tag_filter="ALL",
                exclude_execution_tag_filter="ALL",
                db=MagicMock(),
            )

        self.assertIn("Sector Heatmap", page)
        self.assertIn("No heatmap data yet", page)


if __name__ == "__main__":
    unittest.main()
