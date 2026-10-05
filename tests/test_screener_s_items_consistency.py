"""Tests for the track-A screening-consistency items (S-1, S-2, S-3, S-6, S-7, S-8).

These guard the shared result pipeline so page, CSV export and bulk actions
(watchlist / sync / focus pool) stay on one parameter set and one truncation
contract. Environment-dependent acceptance (Postgres integration, frontend
screenshots, live TradingView) is intentionally out of scope here.
"""
from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.api.presentation import screener_main
from app.api.routes import screener as screener_route
from app.services import screener as screener_service
from app.services.model_signal_summary import model_direction_rank, model_rank_strength
from app.services.screener import ScreenerService
from app.services.screener_snapshots import (
    FULL_MARKET_ALL_PRECOMPUTE_TEMPLATES,
    FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
    FULL_MARKET_US_PRECOMPUTE_TEMPLATES,
    WATCHLIST_PRECOMPUTE_TEMPLATES,
    _precompute_empty_reason,
    build_base_precompute_params,
    build_precompute_screener_params,
)
from app.services.stock_selection import screener_query


class _FakeSession:
    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _passthrough_service() -> MagicMock:
    service = MagicMock()
    service._apply_snapshot_persistence_filter.side_effect = lambda values, **_kwargs: values
    service._apply_model_signal_filter.side_effect = lambda values, **_kwargs: values
    service._apply_execution_tag_filter.side_effect = lambda values, **_kwargs: values
    service._sort_results.side_effect = lambda values, **_kwargs: values
    return service


def _base_params(**overrides: object) -> dict:
    params = {
        "model_template": "technical_momentum",
        "universe": "full_market",
        "market": "CN",
        "lang": "zh",
    }
    params.update(overrides)
    return params


class S1PageBatchConsistencyTests(unittest.TestCase):
    def test_profile_is_applied_inside_the_shared_pipeline(self) -> None:
        rows = [
            {
                "ticker": "PASS",
                "tradability_status": "READY",
                "model_hit_count": 2,
                "trade_readiness_score": 80.0,
                "risk_flags": [],
            },
            {
                "ticker": "NOT_READY",
                "tradability_status": "REVIEW",
                "model_hit_count": 3,
                "trade_readiness_score": 95.0,
                "risk_flags": [],
            },
        ]
        service = _passthrough_service()
        loader = lambda _params: [dict(row) for row in rows]  # noqa: E731

        profiled, profiled_meta = screener_query.build_final_results(
            service,
            _base_params(strategy_profile="quality_confluence_v1"),
            snapshot_loader=loader,
        )
        unprofiled, unprofiled_meta = screener_query.build_final_results(
            service,
            _base_params(),
            snapshot_loader=loader,
        )

        self.assertTrue(profiled_meta["snapshot_ready"])
        self.assertEqual(["PASS"], [row["ticker"] for row in profiled])
        self.assertEqual(["PASS", "NOT_READY"], [row["ticker"] for row in unprofiled])
        self.assertEqual(2, unprofiled_meta["total_count"])

    def test_profile_filter_runs_before_limit_so_it_is_not_crowded_out(self) -> None:
        # The only profile-approved row sits after the 500-row cut in raw order.
        rows = [
            {"ticker": f"RAW{i}", "tradability_status": "REVIEW", "model_hit_count": 3, "trade_readiness_score": 90.0}
            for i in range(520)
        ]
        rows.append(
            {
                "ticker": "APPROVED",
                "tradability_status": "READY",
                "model_hit_count": 2,
                "trade_readiness_score": 80.0,
                "risk_flags": [],
            }
        )
        service = _passthrough_service()
        loader = lambda _params: [dict(row) for row in rows]  # noqa: E731

        results, meta = screener_query.build_final_results(
            service,
            _base_params(strategy_profile="quality_confluence_v1"),
            snapshot_loader=loader,
        )

        self.assertEqual(["APPROVED"], [row["ticker"] for row in results])
        self.assertEqual(1, meta["total_count"])

    def test_page_and_bulk_endpoints_accept_strategy_profile(self) -> None:
        for function in (
            screener_route.screener_page,
            screener_route.export_screener_csv,
            screener_route.add_screener_result_to_watchlist,
            screener_route.add_all_screener_results_to_watchlist,
            screener_route.sync_screener_symbol,
            screener_route.sync_top_screener_results,
            screener_route.add_all_screener_results_to_focus,
        ):
            self.assertIn(
                "strategy_profile",
                inspect.signature(function).parameters,
                function.__name__,
            )

    def test_batch_endpoints_share_the_route_pipeline_wrapper(self) -> None:
        # Every bulk action funnels through the single wrapper that injects the
        # same snapshot loader as the page and export.
        tree = ast.parse(inspect.getsource(screener_route))
        callers = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "_build_final_results"
                for call in ast.walk(node)
            )
        }
        for name in (
            "screener_page",
            "export_screener_csv",
            "add_screener_result_to_watchlist",
            "_add_screen_results_to_watchlist",  # shared by add-all-to-watchlist
            "sync_top_screener_results",
            "add_all_screener_results_to_focus",
        ):
            self.assertIn(name, callers, name)


class S2SortBeforeTruncationTests(unittest.TestCase):
    def test_watchlist_state_sort_applies_to_the_full_set_before_slicing(self) -> None:
        rows = [{"ticker": f"RAW{i}"} for i in range(70)]
        rows.append({"ticker": "SYNCED"})  # highest watchlist state, last in raw order
        service = _passthrough_service()
        loader = lambda _params: [dict(row) for row in rows]  # noqa: E731

        results, meta = screener_query.build_final_results(
            service,
            _base_params(sort_by="watchlist_state", sort_order="desc"),
            snapshot_loader=loader,
            watchlist_state_map={"SYNCED": {"sync_enabled": 1, "sync_status": "success"}},
        )

        self.assertEqual(len(rows), meta["total_count"])
        self.assertEqual("SYNCED", results[0]["ticker"])
        # The page slice is results[:60]; the globally top-ranked row must be in it.
        self.assertIn("SYNCED", [row["ticker"] for row in results[:60]])


class S3ModelDirectionTests(unittest.TestCase):
    def test_short_side_scores_rank_below_long_side(self) -> None:
        self.assertEqual(0, model_direction_rank(-0.5))
        self.assertEqual(1, model_direction_rank(0.1))
        self.assertEqual(0, model_rank_strength(-0.5))
        self.assertGreater(model_rank_strength(0.1), model_rank_strength(-0.5))

    def test_model_strength_sort_does_not_top_sell(self) -> None:
        service = ScreenerService()
        rows = [
            {"ticker": "SELL", "model_score": -0.5, "model_signal_strength": 100},
            {"ticker": "BUY", "model_score": 0.1, "model_signal_strength": 28},
        ]

        ranked = service._sort_results(rows, sort_by="model_signal_strength", sort_order="desc")

        self.assertEqual(["BUY", "SELL"], [row["ticker"] for row in ranked])

    def _run_model_ranking(self, **kwargs):
        def decision(candidate: dict) -> dict:
            score = candidate["score"]
            return {
                "ticker": candidate["ticker"],
                "score": score,
                "signal_strength": min(100, max(8, int(abs(score) * 280))),
                "signal_label": "Sell" if score <= -0.05 else "Buy",
                "percentile": 0.9,
                "name": candidate["ticker"],
                "market": "US",
                "entry_style": "Avoid" if score < 0 else "Breakout",
            }

        prediction_repo = MagicMock()
        prediction_repo.return_value.list_predictions_for_run.return_value = [
            {"ticker": "SELL", "score": -0.5},
            {"ticker": "BUY", "score": 0.1},
        ]
        prediction_repo.return_value._build_signal_decision.side_effect = decision
        run_repo = MagicMock()
        run_repo.return_value.list_successful_runs.return_value = [SimpleNamespace(id=7)]

        with (
            patch.object(screener_service, "SessionLocal", lambda: _FakeSession()),
            patch.object(screener_service, "ModelRunRepository", run_repo),
            patch.object(screener_service, "PredictionRepository", prediction_repo),
            patch.object(screener_service, "load_latest_closes", lambda _tickers: {}),
        ):
            service = ScreenerService()
            with patch.object(service, "_load_universe", return_value=["SELL", "BUY"]):
                return service._screen_model_ranking(
                    universe="full_market",
                    market="US",
                    action_filter="ALL",
                    **kwargs,
                )

    def test_default_model_ranking_drops_sell_and_decouples_min_trend_score(self) -> None:
        results = self._run_model_ranking(
            min_trend_score=99,
            model_signal_filter="ALL",
            min_model_signal_strength=0.0,
        )

        # SELL is direction-gated out; min_trend_score no longer gates the model score.
        self.assertEqual(["BUY"], [row["ticker"] for row in results])


class S6WidestPrecomputeTests(unittest.TestCase):
    def test_precompute_params_use_the_widest_thresholds(self) -> None:
        params = build_base_precompute_params(
            model_template="cn_growth_value",
            universe="full_market",
            market="CN",
        )

        self.assertEqual(0, params["min_trend_score"])
        self.assertEqual(0, params["min_listing_days"])
        self.assertEqual(0.0, params["exclude_bottom_market_cap_pct"])
        self.assertGreater(params["pe_max"], 1.0e9)
        self.assertLess(params["min_roe_avg_3y"], -1.0e9)
        self.assertLess(params["min_net_profit_yoy"], -1.0e9)
        self.assertGreater(params["max_debt_to_assets"], 1.0e9)

    def test_relaxing_a_threshold_only_adds_rows(self) -> None:
        rows = [
            {"ticker": "CHEAP", "trend_score": 80, "pe_ttm": 10},
            {"ticker": "RICH", "trend_score": 80, "pe_ttm": 50},
        ]
        service = _passthrough_service()

        default_params = screener_query.normalize_screen_params(_base_params(pe_max=30.0))
        relaxed_params = screener_query.normalize_screen_params(_base_params(pe_max=60.0))

        default_rows = screener_query.filter_precomputed_rows(service, [dict(r) for r in rows], default_params)
        relaxed_rows = screener_query.filter_precomputed_rows(service, [dict(r) for r in rows], relaxed_params)

        default_tickers = {row["ticker"] for row in default_rows}
        relaxed_tickers = {row["ticker"] for row in relaxed_rows}
        self.assertEqual({"CHEAP"}, default_tickers)
        self.assertTrue(default_tickers.issubset(relaxed_tickers))
        self.assertIn("RICH", relaxed_tickers)


class S7TvMultiTimeframeTests(unittest.TestCase):
    def test_template_is_in_every_precompute_list(self) -> None:
        for templates in (
            WATCHLIST_PRECOMPUTE_TEMPLATES,
            FULL_MARKET_CN_PRECOMPUTE_TEMPLATES,
            FULL_MARKET_US_PRECOMPUTE_TEMPLATES,
            FULL_MARKET_ALL_PRECOMPUTE_TEMPLATES,
        ):
            self.assertIn("tv_multi_timeframe_bullish", templates)

    def test_precompute_params_include_the_tv_template(self) -> None:
        params = build_precompute_screener_params(markets=["CN", "US"], include_watchlist=False)
        templates = {item["model_template"] for item in params}
        self.assertIn("tv_multi_timeframe_bullish", templates)

    def test_run_screen_returns_rows_when_a_snapshot_exists(self) -> None:
        rows = [
            {
                "ticker": "TV1",
                "trend_score": 70,
                "volume_ratio": 1.5,
                "tradability_status": "READY",
                "model_hit_count": 0,
                "trade_readiness_score": 0.0,
            }
        ]
        service = _passthrough_service()
        loader = lambda _params: [dict(row) for row in rows]  # noqa: E731

        produced = screener_query.run_screen(
            service,
            _base_params(model_template="tv_multi_timeframe_bullish"),
            snapshot_loader=loader,
        )

        self.assertEqual(["TV1"], [row["ticker"] for row in produced])

    def test_empty_precompute_result_carries_an_explicit_reason(self) -> None:
        reason = _precompute_empty_reason({"model_template": "tv_multi_timeframe_bullish"})
        self.assertTrue(reason)
        self.assertIsNone(_precompute_empty_reason({"model_template": "technical_momentum"}))


class S8TruncationTransparencyTests(unittest.TestCase):
    def test_pipeline_reports_true_total_and_truncation(self) -> None:
        rows = [{"ticker": f"T{i:04d}", "trend_score": 70} for i in range(600)]
        service = _passthrough_service()
        loader = lambda _params: [dict(row) for row in rows]  # noqa: E731

        results, meta = screener_query.build_final_results(
            service,
            _base_params(),
            snapshot_loader=loader,
        )

        self.assertEqual(600, meta["total_count"])
        self.assertEqual(500, meta["returned_count"])
        self.assertTrue(meta["truncated"])
        self.assertEqual(500, len(results))

    def test_page_states_the_truncation_instead_of_claiming_a_full_export(self) -> None:
        repository = MagicMock()
        repository.get_or_create_default.return_value = SimpleNamespace(id=7)
        repository.list_ticker_map.return_value = {}
        request = MagicMock()
        request.query_params = {"run": "1"}
        rows = [{"ticker": "T1", "trend_score": 70, "market": "CN"}]
        final_meta = {
            "snapshot_ready": True,
            "total_count": 600,
            "returned_count": 500,
            "limit": 500,
            "truncated": True,
            "multi_screen_meta": {"available_templates": [], "missing_templates": []},
        }

        with (
            patch.object(screener_route, "is_authenticated", return_value=True),
            patch.object(screener_route, "resolve_request_lang", return_value="zh"),
            patch.object(screener_route, "_load_saved_presets", return_value=[]),
            patch.object(screener_route, "WatchlistRepository", return_value=repository),
            patch.object(
                screener_route,
                "_build_final_results",
                return_value=([dict(row) for row in rows], dict(final_meta)),
            ),
            patch.object(screener_route, "annotate_rows_with_kronos", return_value=None),
            patch.object(screener_route, "summarize_screener_rows", return_value={
                "tagged_names": 0,
                "risk_examples": [],
                "risk_top_tags": [],
                "status_counts": {},
            }),
            patch.object(screener_route, "load_or_build_recommendation_regression", return_value={}),
            patch.object(
                screener_route,
                "summarize_recommendation_regression",
                return_value={"headline": "", "metrics": [], "rules": [], "warnings": []},
            ),
            patch.object(screener_route, "_load_screener_snapshot_record", return_value=None),
            patch.object(screener_route, "load_model_selection_guidance_snapshot", return_value={}),
            patch.object(screener_route, "summarize_model_selection_guidance", return_value={}),
            patch.object(screener_main, "render_workspace_nav_html", return_value="<a>nav</a>"),
        ):
            page = screener_route.screener_page(
                request,
                message=None,
                lang="zh",
                model_template="technical_momentum",
                multi_model_templates=[],
                min_multi_model_hits=2,
                confluence_action_filter="ALL",
                strategy_profile="",
                universe="full_market",
                market="CN",
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
                min_hit_probability=None,
                probability_calibration=None,
                model_reliability_weights=None,
                show_evaluation=0,
                show_details=0,
                db=MagicMock(),
            )

        self.assertIn("600", page)
        self.assertIn("导出 CSV 只包含", page)
        self.assertNotIn("导出 CSV 包含全部", page)


if __name__ == "__main__":
    unittest.main()
