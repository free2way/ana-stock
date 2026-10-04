from __future__ import annotations

import ast
import inspect
from pathlib import Path
import unittest

from app.api.presentation import dashboard_performance
from app.api.presentation import dashboard_performance_components
from app.api.presentation.dashboard_performance_evaluations import (
    build_evaluation_fragments,
)
from app.api.presentation.dashboard_performance_guidance import (
    build_guidance_fragments,
)
from app.api.routes.dashboard import performance
from app.services.model_performance_overview import (
    aggregate_model_run_performance,
    summarize_rows_by_dimension,
)
from app.services.model_performance_brief import build_model_performance_brief
from app.services.model_selection_usage import select_discouraged_recommendations


class DashboardPerformanceDecouplingTests(unittest.TestCase):
    def test_recent_run_aggregation_is_weighted_and_pure(self) -> None:
        runs = [
            {"id": 1, "name": "LightGBM", "market": "CN"},
            {"id": 2, "name": "LightGBM", "market": "CN"},
        ]
        summaries = {
            1: {
                "trade_dates": 3,
                "pick_count": 10,
                "latest_trade_date": "2026-09-29",
                "windows": {5: {"count": 10, "avg_return": 2.0, "hit_rate": 60.0}},
            },
            2: {
                "trade_dates": 2,
                "pick_count": 30,
                "latest_trade_date": "2026-10-01",
                "windows": {5: {"count": 30, "avg_return": -1.0, "hit_rate": 40.0}},
            },
        }
        original = [dict(item) for item in runs]

        result = aggregate_model_run_performance(
            runs,
            summary_loader=lambda item: summaries[item["id"]],
        )
        aggregate = result[("LightGBM", "CN")]
        window = aggregate["window_sums"][5]

        self.assertEqual(original, runs)
        self.assertEqual(2, aggregate["runs"])
        self.assertEqual(5, aggregate["trade_dates_covered"])
        self.assertEqual(40, aggregate["sample_count"])
        self.assertEqual("2026-10-01", aggregate["latest_trade_date"])
        self.assertEqual(40, window["count"])
        self.assertAlmostEqual(-10.0, window["weighted_return"])
        self.assertAlmostEqual(1800.0, window["hit_weight"])

    def test_model_performance_route_delegates_run_aggregation(self) -> None:
        tree = ast.parse(Path(performance.__file__).read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "dashboard_model_performance"
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "aggregate_model_run_performance"
                for node in ast.walk(function)
            )
        )

    def test_regime_and_sector_grouping_is_pure_and_ranked(self) -> None:
        rows = [
            {
                "regime_label": "risk_on",
                "sector_group": "Tech",
                "return_3d": 1.0,
                "return_5d": 2.0,
                "return_10d": -1.0,
            },
            {
                "regime_label": "risk_on",
                "sector_group": "Tech",
                "return_3d": -1.0,
                "return_5d": 4.0,
                "return_10d": 3.0,
            },
            {"return_5d": -2.0},
        ]
        original = [dict(item) for item in rows]

        regimes = summarize_rows_by_dimension(
            rows, dimension="regime", missing_label="Unlabeled"
        )
        sectors = summarize_rows_by_dimension(
            rows, dimension="sector", missing_label="Unclassified", limit=1
        )

        self.assertEqual(original, rows)
        self.assertEqual("risk_on", regimes[0]["label"])
        self.assertEqual(2, regimes[0]["sample_count"])
        self.assertEqual(3.0, regimes[0]["windows"][5]["avg_return"])
        self.assertEqual(100.0, regimes[0]["windows"][5]["hit_rate"])
        self.assertEqual(["Tech"], [item["label"] for item in sectors])

    def test_performance_components_escape_external_values(self) -> None:
        rows = dashboard_performance_components.render_structured_evaluation_rows(
            [
                {
                    "model_name": "<script>bad</script>",
                    "model_type": "ranker",
                    "market": "CN",
                    "metrics": [{"horizon_days": 5, "hit_rate": 55.0}],
                }
            ],
            lang="en",
        )
        detail = dashboard_performance_components.render_detail_rows(
            {
                "rows": [
                    {
                        "ticker": "<bad>",
                        "name": "A&B",
                        "sector_group": "<Tech>",
                    }
                ]
            },
            lang="en",
        )

        self.assertNotIn("<script>bad</script>", rows)
        self.assertIn("&lt;script&gt;bad&lt;/script&gt;", rows)
        self.assertIn("&lt;bad&gt;", detail)
        self.assertIn("A&amp;B", detail)

    def test_large_table_and_card_composition_is_outside_route(self) -> None:
        source = inspect.getsource(performance.dashboard_model_performance)
        for renderer_name in (
            "render_structured_evaluation_rows",
            "render_run_summary_cards",
            "render_training_diagnostic",
            "render_aggregate_model_rows",
            "render_grouped_return_rows",
            "render_watchlist_fragments",
            "render_detail_rows",
        ):
            self.assertIn(f"{renderer_name}(", source)
        self.assertNotIn("aggregate_regime_groups", source)
        self.assertNotIn("watchlist_rows_html = \"\".join", source)
        self.assertNotIn("structured_evaluation_rows_html = \"\".join", source)
        self.assertNotIn("app.api.routes", inspect.getsource(dashboard_performance_components))

    def test_template_evaluation_fragments_are_pure_and_localised(self) -> None:
        next_tesla = {
            "snapshot_total": 2,
            "clean_snapshot_total": 1,
            "windows": {
                "buy_the_dip": {5: {"count": 2, "avg_return": 2.5, "hit_rate": 60.0}},
                "wait_for_breakout": {5: {"count": 2, "avg_return": 1.0, "hit_rate": 45.0}},
            },
        }
        technical = {"windows": {}, "snapshot_total": 3, "labeled_snapshot_total": 1}
        lightgbm = {
            "windows": {},
            "snapshot_total": 4,
            "labeled_snapshot_total": 2,
            "sector_counts": {"pullback": {"<Tech>": 2}},
            "sector_windows": {
                "pullback": {"<Tech>": {5: {"count": 1, "avg_return": 1.0, "hit_rate": 100.0}}}
            },
        }
        original = dict(next_tesla)

        fragments = build_evaluation_fragments(
            next_tesla=next_tesla,
            next_tesla_maturity_state={"tone": "mid"},
            technical=technical,
            technical_maturity_state={},
            lightgbm=lightgbm,
            lightgbm_maturity_state={"tone": "good"},
            lightgbm_prediction={},
            lang="zh",
        )

        self.assertEqual(original, next_tesla)
        self.assertIn("最近回看 2 个快照", fragments["next_tesla_note"])
        self.assertIn("回踩买点", fragments["next_tesla_takeaway"])
        self.assertIn("&lt;Tech&gt;", fragments["lightgbm_sector_pullback_html"])
        self.assertNotIn("<Tech>", fragments["lightgbm_sector_pullback_html"])
        self.assertIn("background:#dcfce7", fragments["lightgbm_maturity_style"])
        self.assertNotIn(
            "app.api.routes",
            inspect.getsource(build_evaluation_fragments),
        )

    def test_route_delegates_template_evaluation_composition(self) -> None:
        source = inspect.getsource(performance.dashboard_model_performance)
        self.assertIn("build_evaluation_fragments(", source)
        self.assertNotIn("def _lightgbm_metric_row", source)
        self.assertNotIn("def _technical_metric_row", source)
        self.assertNotIn("def _next_tesla_market_split_html", source)

    def test_discouraged_guidance_requires_enough_weak_samples(self) -> None:
        recommendations = [
            {"template": "too-small", "sample_count": 4, "stats_1d": {"hit_rate": 0}},
            {"template": "negative", "sample_count": 5, "stats_1d": {"avg_return": -0.1}},
            {"template": "low-hit", "sample_count": 8, "stats_1d": {"hit_rate": 44.9}},
            {"template": "healthy", "sample_count": 20, "stats_1d": {"avg_return": 1.0, "hit_rate": 60}},
        ]

        result = select_discouraged_recommendations(recommendations)

        self.assertEqual(["negative", "low-hit"], [item["template"] for item in result])

    def test_guidance_rendering_escapes_snapshot_and_candidate_fields(self) -> None:
        fragments = build_guidance_fragments(
            guidance={
                "winner_total": 1,
                "recommendations": [
                    {
                        "template_label": "<Model>",
                        "sample_count": 6,
                        "stats_1d": {"avg_return": -1.0, "hit_rate": 40.0},
                    }
                ],
                "winner_attribution": [
                    {"ticker": "<bad>", "name": "A&B", "market": "CN", "hits": []}
                ],
            },
            guidance_summary={
                "snapshot_meta": {"source": "snapshot", "snapshot_date": "<today>"}
            },
            validation_summary={"rows": []},
            market="CN",
            lang="en",
        )

        self.assertIn("&lt;Model&gt;", fragments["cards_html"])
        self.assertIn("&lt;today&gt;", fragments["cards_html"])
        self.assertIn("&lt;bad&gt;", fragments["winner_rows_html"])
        self.assertIn("A&amp;B", fragments["winner_rows_html"])

    def test_route_delegates_guidance_composition(self) -> None:
        source = inspect.getsource(performance.dashboard_model_performance)
        self.assertIn("build_guidance_fragments(", source)
        self.assertNotIn("def _guidance_bucket_label", source)
        self.assertNotIn("discouraged_items", source)
        self.assertNotIn("guidance_rows_html +=", source)

    def test_model_performance_brief_selects_decisive_template(self) -> None:
        cards = build_model_performance_brief(
            next_tesla={"clean_snapshot_total": 5},
            next_tesla_state={"level": "Observation"},
            technical={"labeled_snapshot_total": 8},
            technical_state={"level": "Early Read"},
            lightgbm={"labeled_snapshot_total": 2},
            lightgbm_state={"level": "Observation"},
            market="CN",
            lang="en",
        )

        self.assertEqual("Technical Momentum", cards[0]["title"])
        self.assertEqual("Current scope: CN", cards[1]["title"])
        self.assertEqual("Good for an early read", cards[2]["title"])

    def test_overview_renderer_escapes_service_copy(self) -> None:
        rendered = dashboard_performance_components.render_overview_cards(
            [{"eyebrow": "<E>", "title": "<T>", "copy": "A&B"}]
        )

        self.assertIn("&lt;E&gt;", rendered)
        self.assertIn("&lt;T&gt;", rendered)
        self.assertIn("A&amp;B", rendered)

    def test_route_delegates_overview_policy(self) -> None:
        source = inspect.getsource(performance.dashboard_model_performance)
        self.assertIn("build_model_performance_brief(", source)
        self.assertIn("render_overview_cards(", source)
        self.assertNotIn("def _maturity_rank", source)
        self.assertNotIn("overview_focus_title", source)

    def test_model_performance_document_is_owned_by_template(self) -> None:
        tree = ast.parse(Path(performance.__file__).read_text())
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "dashboard_model_performance"
        )
        render_call = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "render_model_performance_page"
        )
        view_keyword = next(item for item in render_call.keywords if item.arg == "view")
        self.assertIsInstance(view_keyword.value, ast.Dict)
        self.assertFalse(
            any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "<!DOCTYPE html>" in node.value
                for node in ast.walk(function)
            )
        )

        template_path = Path(performance.__file__).parents[2] / "templates" / "dashboard" / "model_performance.html"
        self.assertNotIn("fragments[", template_path.read_text())
        self.assertIn("guidance_ui", template_path.read_text())
        self.assertNotIn("app.api.routes", inspect.getsource(dashboard_performance))


if __name__ == "__main__":
    unittest.main()
