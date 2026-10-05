"""Regression contracts for shared rendering and screening semantics."""
import ast
import importlib
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.api import rendering
from app.api.routes import dashboard, portfolio, screener, watchlist
from app.services import screener_snapshots
from app.services import screener as screener_service
from app.services import execution_tag_filters
from app.services import symbol_details, technical_patterns, price_snapshot, ticker_format
from app.services import template_evaluation
from app.services import display_compaction


class SharedReturnCalculationTests(TestCase):
    def test_continuous_leader_sort_keys_preserve_ties_and_fallbacks(self):
        row = {"ticker": "A", "score": "0.5", "signal_strength": "20", "hits": "3",
               "score_history": [1, 4, 2]}
        expected = {"ticker": ("A",), "score": (0.5, "A"), "signal": (20.0, "A"),
                    "trend": (1.0, "A"), "hits": (3, 0.5, "A"), "unknown": (3, 0.5, "A")}
        for field, key in expected.items():
            with self.subTest(field=field):
                self.assertEqual(key, dashboard._continuous_leader_sort_key(row, field))
        for history in (None, [], [4]):
            self.assertEqual((0.0, "A"), dashboard._continuous_leader_sort_key(
                {"ticker": "A", "score_history": history}, "trend"))
        self.assertEqual((0, 0.0, "A"), dashboard._continuous_leader_sort_key({"ticker": "A"}, "hits"))

    def test_compatibility_sort_export_keeps_service_identity(self):
        from app.services.continuous_leaders import continuous_leader_sort_key

        self.assertIs(dashboard._continuous_leader_sort_key, continuous_leader_sort_key)
        # Page/export ordering is exercised through HTTP in
        # test_continuous_leaders_routes, without freezing route internals.

    def test_consumers_keep_shared_calculation_identity(self):
        self.assertIs(dashboard._aggregate_window_stats, template_evaluation.aggregate_window_stats)
        self.assertIs(portfolio._load_portfolio_daily_change_pct, price_snapshot.load_daily_change_pct)
        self.assertIs(watchlist._load_watchlist_daily_change_pct, price_snapshot.load_daily_change_pct)

    def test_window_statistics_preserve_zero_and_threshold_semantics(self):
        self.assertEqual({"count": 0, "avg_return": None, "hit_rate": None,
                          "strong_hit_rate": None, "miss_rate": None}, dashboard._aggregate_window_stats([]))
        self.assertEqual({"count": 5, "avg_return": 0.0, "hit_rate": 40.0,
                          "strong_hit_rate": 20.0, "miss_rate": 20.0},
                         dashboard._aggregate_window_stats([-3, -1, 0, 1, 3]))

    def test_daily_change_preserves_close_fallback_and_missing_data(self):
        cases = [([], None), ([{"close": 10}], None),
                 ([{"close": 10}, {"close": 11}], 10.0),
                 ([{"close": 0}, {"close": 11}], None),
                 ([{"close": "bad"}, {"close": 11}], None),
                 ([{"close": 0, "adj_close": 10}, {"close": 0, "adj_close": 11}], 10.0)]
        for rows, expected in cases:
            with self.subTest(rows=rows), patch.object(price_snapshot, "load_lake_price_history", return_value=rows) as lake:
                actual = price_snapshot.load_daily_change_pct(market=" cn ", ticker="600519.SH")
                if expected is None:
                    self.assertIsNone(actual)
                else:
                    self.assertAlmostEqual(expected, actual)
                lake.assert_called_once_with(market="CN", ticker="600519.SS", limit=2)

    def test_unsupported_market_and_empty_symbol_do_not_read_lake(self):
        with patch.object(price_snapshot, "load_lake_price_history") as lake:
            for market, ticker in [("HK", "0700.HK"), (None, "AAPL"), ("US", "")]:
                self.assertIsNone(price_snapshot.load_daily_change_pct(market=market, ticker=ticker))
            lake.assert_not_called()


class LakeCandidateTests(TestCase):
    def test_market_mapping_and_legacy_methods(self):
        readers = [symbol_details.SymbolDataService.__new__(symbol_details.SymbolDataService),
                   technical_patterns.TechnicalPatternService.__new__(technical_patterns.TechnicalPatternService)]
        for ticker, expected in [("", []), ("  ", []), (" aapl ", [("US", "AAPL")]),
                ("600519.sh", [("CN", "600519.SS")]), ("000001", [("CN", "000001.SZ")]),
                ("920001", [("CN", "920001.BJ")]), ("0700.HK", []),
                ("BRK.B", [("US", "BRK.B")])]:
            with self.subTest(ticker=ticker):
                self.assertEqual(expected, ticker_format.lake_ticker_candidates(ticker))
                self.assertEqual(expected, price_snapshot._lake_candidates(ticker))
                for reader in readers:
                    self.assertEqual(expected, reader._lake_candidates(ticker))

    def test_symbol_history_fallback_preserves_market_and_limit(self):
        reader = symbol_details.SymbolDataService.__new__(symbol_details.SymbolDataService)
        rows = [{"date": "2026-09-01", "close": 10}]
        with patch.object(symbol_details, "load_lake_price_history", return_value=rows) as lake:
            self.assertIs(rows, reader.get_history("600519.SH", limit=17))
            lake.assert_called_once_with(market="CN", ticker="600519.SS", limit=17)

    def test_technical_frame_fallback_preserves_defaults_and_sort(self):
        reader = technical_patterns.TechnicalPatternService.__new__(technical_patterns.TechnicalPatternService)
        with patch.object(technical_patterns, "load_lake_price_history", return_value=[
                {"date": "2026-09-02", "close": 12}, {"date": "2026-09-01", "close": 10}]) as lake:
            frame = reader._load_price_frame("aapl")
            lake.assert_called_once_with(market="US", ticker="AAPL", limit=240)
            self.assertEqual([10, 12], frame["close"].tolist())
            self.assertEqual([10, 12], frame["open"].tolist())
            self.assertEqual([0, 0], frame["volume"].tolist())

    def test_price_snapshot_fallback_preserves_alias_and_numeric_conversion(self):
        with patch.object(price_snapshot, "get_or_set", side_effect=lambda *args, **kw: kw["loader"]()), patch.object(
                price_snapshot, "load_lake_price_history", return_value=[{"close": "12.5"}]) as lake:
            self.assertEqual(12.5, price_snapshot.load_latest_close("600519.sh"))
            lake.assert_called_once_with(market="CN", ticker="600519.SS", limit=1)


class ExecutionTagFilterTests(TestCase):
    def test_all_consumers_keep_original_names_and_share_functions(self):
        for consumer in (screener_service, dashboard, watchlist):
            for name in ("matches_execution_tag_filter", "excludes_execution_tag_filter"):
                self.assertIs(getattr(consumer, "_" + name), getattr(execution_tag_filters, name))

    def test_empty_filter_disables_both_include_and_exclude(self):
        for tags in (None, [], ["blocked"], [None, 1]):
            for query in (None, "", " ", "ALL", " all, , ALL ", ",,"):
                for fn in (execution_tag_filters.matches_execution_tag_filter,
                           execution_tag_filters.excludes_execution_tag_filter):
                    with self.subTest(tags=tags, query=query, function=fn.__name__):
                        self.assertTrue(fn(tags, query))

    def test_nonempty_queries_match_any_exact_normalized_tag(self):
        for tags, query, matched in [
            (None, "blocked", False), ([], "blocked", False),
            ([" BLOCKED ", "other"], "unknown,blocked", True),
            (["blocked"], "block", False), (["block"], "blocked", False),
            (["blocked"], "all,unknown", False),
            (["blocked"], "all,BLOCKED,blocked", True),
            ([None, 0, " "], "none", True), ([None, 0], "0", True),
        ]:
            with self.subTest(tags=tags, query=query):
                self.assertEqual(matched, execution_tag_filters.matches_execution_tag_filter(tags, query))
                self.assertEqual(not matched, execution_tag_filters.excludes_execution_tag_filter(tags, query))


class SharedPresentationTests(TestCase):
    def test_dashboard_compaction_exports_keep_shared_service_identity(self):
        from app.api.routes.dashboard import _common, ops

        self.assertIs(_common._compact_label, display_compaction.compact_label)
        self.assertIs(_common._compact_run_name, display_compaction.compact_run_name)
        self.assertIs(_common._compact_job_type, display_compaction.compact_job_type)
        self.assertIs(ops._compact_json_summary, display_compaction.compact_json_summary)

    def test_existing_route_names_reference_shared_functions(self):
        for route in (screener, portfolio):
            self.assertIs(route._compact_text, rendering.compact_text)
        for route in (dashboard, screener):
            self.assertIs(route._mini_trend_bars, rendering.mini_trend_bars)

    def test_compact_text_preserves_edge_case_slicing(self):
        for value, limit, expected in [(None, 28, ""), ("  abc  ", 3, "abc"),
                ("中文名字", 3, "中文…"), ("abc", 1, "…"),
                ("abc", 0, "ab…"), ("abc", -1, "a…")]:
            with self.subTest(value=value, limit=limit):
                self.assertEqual(expected, rendering.compact_text(value, limit))

    def test_mini_trend_html_and_invalid_inputs(self):
        self.assertEqual("<div class='mini-trend empty'><span>暂无趋势</span></div>",
                         rendering.mini_trend_bars([], lang="zh"))
        self.assertEqual("<div class='mini-trend empty'><span>No trend</span></div>",
                         rendering.mini_trend_bars([], lang="en"))
        self.assertEqual("<div class='mini-trend'><span style='height:16%;'></span>"
                         "<span style='height:50%;'></span><span style='height:100%;'></span></div>",
                         rendering.mini_trend_bars([-1, 5, 10], lang="zh"))
        with self.assertRaises(ValueError):
            rendering.mini_trend_bars(["invalid"], lang="en")


class PublicCompatibilityTests(TestCase):
    def test_package_reexports_remain_the_original_objects(self):
        package = importlib.import_module("app.services.stock_selection")
        tree = ast.parse(Path(package.__file__).read_text())
        imports = {alias.asname or alias.name: (node.module, alias.name)
                   for node in tree.body if isinstance(node, ast.ImportFrom)
                   for alias in node.names}
        self.assertEqual(218, len(imports))
        self.assertEqual(set(imports), set(package.__all__))
        self.assertEqual(len(imports), len(package.__all__))
        for name, (module, original) in imports.items():
            with self.subTest(export=name):
                self.assertIs(getattr(package, name), getattr(importlib.import_module(module), original))

    def test_unreferenced_hk_model_is_still_registered(self):
        from app.models.base import Base
        from app.models.tables import HKPaperSimulation
        self.assertIs(HKPaperSimulation.__table__, Base.metadata.tables["hk_paper_simulations"])


class SharedScreeningTests(TestCase):
    def test_unused_private_paths_are_removed_not_public_endpoints(self):
        self.assertFalse(hasattr(screener, "_persist_screener_snapshot"))
        self.assertFalse(hasattr(dashboard, "_load_ops_summary"))
        self.assertFalse(hasattr(dashboard, "_price_sparkline_svg"))
        paths = {route.path for route in dashboard.router.routes}
        self.assertIn("/dashboard/ops", paths)
        self.assertIn("/dashboard/ai-daily-report", paths)

    def test_live_and_precomputed_consumers_share_implementations(self):
        for name in ("_normalize_multi_model_templates", "_action_semantic_buckets",
                     "_template_action_semantic_buckets"):
            self.assertIs(getattr(screener, name), getattr(screener_snapshots, name))

    def test_templates_preserve_order_filter_unknowns_and_deduplicate(self):
        first, second = list(screener.MODEL_TEMPLATES)[:2]
        for values in (f" {second},unknown,{first},{second} ", [second, None, "unknown", first, second]):
            self.assertEqual([second, first], screener._normalize_multi_model_templates(values))
        self.assertEqual([], screener._normalize_multi_model_templates(None))

    def test_action_semantics_and_template_overrides(self):
        cases = [(None, []), ("unknown", []), (" BUY ", ["bullish_entry"]),
                 ("buy the dip", ["buy_the_dip", "bullish_entry"]),
                 ("wait for breakout", ["breakout_confirmation"]),
                 ("Hold And Watch", ["watchlist"])]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, screener._action_semantic_buckets(value))
        self.assertEqual(["watchlist", "buy_the_dip", "bullish_entry"],
            screener._template_action_semantic_buckets("cn_hammer_reversal", "watch"))
        self.assertEqual(["breakout_confirmation", "bullish_entry"],
            screener._template_action_semantic_buckets("cn_volume_breakout", "breakout"))


class SharedRenderingTests(TestCase):
    def test_portfolio_and_watchlist_share_renderer(self):
        self.assertIs(portfolio._render_daily_change_chip, rendering.render_daily_change_chip)
        self.assertIs(watchlist._render_daily_change_chip, rendering.render_daily_change_chip)

    def test_daily_change_keeps_missing_sign_precision_and_colors(self):
        for value in (None, "invalid", []):
            self.assertEqual("<span class='muted'>-</span>", rendering.render_daily_change_chip(value))
        for value, text, color in ((1.234, "+1.23%", "#4ade80"),
                                    (-2.345, "-2.35%", "#f87171"), (0, "+0.00%", "#cbd5e1")):
            with self.subTest(value=value):
                result = rendering.render_daily_change_chip(value)
                self.assertIn(text, result)
                self.assertIn(color, result)

    def test_metric_row_keeps_horizon_order_and_missing_values(self):
        values = {"watch": {1: {"count": 2, "avg_return": 0, "hit_rate": 50},
                            3: {"count": 1, "avg_return": -2.5, "hit_rate": 0},
                            5: {"count": 3, "avg_return": 4, "hit_rate": 100}}}
        result = screener._evaluation_metric_row(values, "watch", "观察")
        self.assertEqual(7, result.count("<td>"))
        self.assertIn("<td>观察</td><td>2</td>", result)
        self.assertLess(result.index("-2.50%"), result.index("4.00%"))
        empty = screener._evaluation_metric_row({}, "watch", "WATCH")
        self.assertEqual(3, empty.count("<td>0</td>"))
