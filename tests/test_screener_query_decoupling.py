from __future__ import annotations

import ast
import inspect
import unittest
from unittest.mock import MagicMock

from app.api.routes import screener as screener_route
from app.services.stock_selection import screener_query


class ScreenerQueryDecouplingTests(unittest.TestCase):
    def test_normalization_remains_available_through_compatibility_export(self) -> None:
        self.assertIs(screener_route._normalize_screen_params, screener_query.normalize_screen_params)
        normalized = screener_query.normalize_screen_params(
            {
                "model_template": "technical_momentum",
                "market": "CN",
                "limit": 2,
                "strategy_profile": "quality_confluence_v1",
            }
        )
        self.assertEqual(normalized["market"], "CN")
        self.assertEqual(normalized["limit"], 500)
        self.assertEqual(normalized["strategy_profile"], "quality_confluence_v1")

    def test_filtering_is_non_mutating_and_applies_financial_thresholds(self) -> None:
        rows = [
            {"ticker": "PASS", "trend_score": 80, "volume_ratio": 2, "pe_ttm": 15},
            {"ticker": "FAIL", "trend_score": 40, "volume_ratio": 2, "pe_ttm": 15},
        ]
        original = [dict(row) for row in rows]
        service = MagicMock()
        service._apply_snapshot_persistence_filter.side_effect = lambda values, **_kwargs: values
        service._apply_model_signal_filter.side_effect = lambda values, **_kwargs: values
        service._apply_execution_tag_filter.side_effect = lambda values, **_kwargs: values
        service._sort_results.side_effect = lambda values, **_kwargs: values
        params = screener_query.normalize_screen_params(
            {"model_template": "technical_momentum", "market": "CN", "min_trend_score": 60}
        )

        filtered = screener_query.filter_precomputed_rows(service, rows, params)

        self.assertEqual(rows, original)
        self.assertEqual(["PASS"], [row["ticker"] for row in filtered])

    def test_route_query_wrappers_only_bind_the_snapshot_adapter(self) -> None:
        for function_name in (
            "_load_screen_rows_from_snapshot",
            "_run_screen",
            "_screen_snapshot_ready",
            "_run_multi_screen",
            "_load_precomputed_screener_rows",
            "_filter_precomputed_rows",
        ):
            function = getattr(screener_route, function_name)
            tree = ast.parse(inspect.getsource(function))
            self.assertLessEqual(
                sum(isinstance(node, (ast.If, ast.For, ast.While)) for node in ast.walk(tree)),
                0,
                function_name,
            )


if __name__ == "__main__":
    unittest.main()
