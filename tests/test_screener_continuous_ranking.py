"""Continuous, deterministic ranking for the precomputed screener pool.

The production ``technical_momentum`` snapshot used to sort on the integer
``trend_score``, whose top block ties by design (108/5000 rows at 99). Top-N
was then decided by ticker order, so the experiment effect flipped sign with
the ranking field. These tests pin the replacement: a continuous
``model_score`` + same-date ``model_percentile`` persisted into the snapshot
rows, and a deterministic ``model_percentile -> dollar_volume -> ticker``
tie-break that is independent of the input row order.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.market_lake import (
    LAKE_MOMENTUM_SCORE_SOURCE,
    _cross_sectional_rank_ratios,
    apply_continuous_momentum_scores,
    screen_lake_momentum,
)
from app.services.screener import ScreenerService
from app.services.screener_snapshots import (
    SNAPSHOT_ROW_FIELDS,
    _compact_snapshot_rows,
    _govern_without_continuous_scores,
)
from app.services.stock_selection import screener_query


def _service() -> ScreenerService:
    # No DB-backed constructor: these tests only exercise pure ranking helpers.
    return ScreenerService.__new__(ScreenerService)


def _lake_columns() -> list[str]:
    return [
        "date",
        "symbol",
        "close",
        "volume",
        "dollar_volume",
        "momentum_5",
        "momentum_20",
        "volume_ratio",
        "ma20",
        "ma60",
        "pullback_depth_pct",
        "distance_to_breakout_pct",
    ]


class LakeMomentumContinuousScoreTests(unittest.TestCase):
    def test_rows_carry_continuous_score_and_same_date_percentile(self) -> None:
        rows = [
            ("2026-10-02", "AAA", 10.0, 1_000_000, 10_000_000.0, 0.03, 0.12, 1.2, 9.5, 9.0, 2.0, -1.0),
            ("2026-10-02", "BBB", 20.0, 1_000_000, 20_000_000.0, 0.01, 0.05, 1.0, 19.0, 18.0, 1.0, -2.0),
            ("2026-10-02", "CCC", 30.0, 1_000_000, 30_000_000.0, 0.05, 0.30, 2.0, 29.0, 28.0, 0.0, -3.0),
        ]
        with (
            patch("app.services.market_lake._recent_parquet_files", return_value=["dummy.parquet"]),
            patch("app.services.market_lake._duckdb_fetchall", return_value=(rows, _lake_columns())),
        ):
            result = screen_lake_momentum(market="US", trade_date="2026-10-02", limit=5)

        by_ticker = {row["ticker"]: row for row in result}
        for row in result:
            self.assertEqual(LAKE_MOMENTUM_SCORE_SOURCE, row["score_source"])
            self.assertIsInstance(row["model_score"], float)
            self.assertGreaterEqual(row["rank_percentile"], 0.0)
            self.assertLessEqual(row["rank_percentile"], 1.0)
            self.assertAlmostEqual(row["rank_percentile"] * 100.0, row["model_percentile"], places=4)
        # Weakest row -> 0.0, strongest -> 1.0; a real 0-1 cross-sectional rank.
        self.assertEqual(0.0, by_ticker["BBB"]["rank_percentile"])
        self.assertEqual(1.0, by_ticker["CCC"]["rank_percentile"])
        self.assertLess(by_ticker["BBB"]["model_score"], by_ticker["AAA"]["model_score"])
        self.assertLess(by_ticker["AAA"]["model_score"], by_ticker["CCC"]["model_score"])

    def test_percentile_is_grouped_by_trade_date(self) -> None:
        # A stale (halted) symbol keeps an earlier date; it must be ranked
        # inside its own same-date cross-section, not against the fresh pool.
        rows = [
            ("2026-10-02", "FRESH1", 10.0, 1_000_000, 10_000_000.0, 0.03, 0.12, 1.2, 9.5, 9.0, 2.0, -1.0),
            ("2026-10-02", "FRESH2", 20.0, 1_000_000, 20_000_000.0, 0.01, 0.05, 1.0, 19.0, 18.0, 1.0, -2.0),
            ("2026-10-01", "STALE", 30.0, 1_000_000, 30_000_000.0, 0.05, 0.30, 2.0, 29.0, 28.0, 0.0, -3.0),
        ]
        with (
            patch("app.services.market_lake._recent_parquet_files", return_value=["dummy.parquet"]),
            patch("app.services.market_lake._duckdb_fetchall", return_value=(rows, _lake_columns())),
        ):
            result = screen_lake_momentum(market="US", trade_date="2026-10-02", limit=5)

        by_ticker = {row["ticker"]: row for row in result}
        self.assertEqual(1.0, by_ticker["FRESH1"]["rank_percentile"])
        self.assertEqual(0.0, by_ticker["FRESH2"]["rank_percentile"])
        self.assertEqual(0.5, by_ticker["STALE"]["rank_percentile"])

    def test_single_row_cross_section_is_neutral(self) -> None:
        self.assertEqual([0.5], _cross_sectional_rank_ratios([3.0]))


class FallbackScoreTests(unittest.TestCase):
    def test_fallback_fills_rows_without_a_model_prediction(self) -> None:
        rows = [
            {"ticker": "A", "date": "2026-10-02", "momentum_20": 12.0, "momentum_5": 3.0, "volume_ratio": 1.5},
            {"ticker": "B", "date": "2026-10-02", "momentum_20": 2.0, "momentum_5": 1.0, "volume_ratio": 1.0},
        ]
        apply_continuous_momentum_scores(rows)
        self.assertLess(rows[1]["model_percentile"], rows[0]["model_percentile"])
        self.assertEqual(LAKE_MOMENTUM_SCORE_SOURCE, rows[0]["score_source"])
        self.assertEqual(0.0, rows[1]["rank_percentile"])
        self.assertEqual(1.0, rows[0]["rank_percentile"])

    def test_fallback_leaves_production_model_rows_untouched(self) -> None:
        rows = [{"ticker": "M", "model_score": 1.25, "model_percentile": 97.0, "score_source": "lightgbm_prediction_v1:percentile_0_100"}]
        apply_continuous_momentum_scores(rows)
        self.assertEqual(1.25, rows[0]["model_score"])
        self.assertEqual(97.0, rows[0]["model_percentile"])
        self.assertEqual("lightgbm_prediction_v1:percentile_0_100", rows[0]["score_source"])
        self.assertNotIn("rank_percentile", rows[0])

    def test_governance_does_not_see_the_fallback_score(self) -> None:
        rows = [
            {
                "ticker": "X",
                "date": "2026-10-02",
                "trend_score": 99,
                "model_score": 88.0,
                "model_percentile": 100.0,
                "rank_percentile": 1.0,
                "score_source": LAKE_MOMENTUM_SCORE_SOURCE,
            }
        ]
        seen: dict[str, dict] = {}

        class _FakeService:
            def apply_candidate_governance(self, values: list[dict]) -> list[dict]:
                seen["row"] = dict(values[0])
                values[0]["tradability_status"] = "REVIEW"
                return values

        with patch("app.services.screener_snapshots.ScreenerService", return_value=_FakeService()):
            governed = _govern_without_continuous_scores(rows)

        # The tradability decision was made without a model conviction...
        self.assertNotIn("model_percentile", seen["row"])
        self.assertNotIn("model_score", seen["row"])
        # ...but the ranking basis is re-attached for persistence.
        self.assertEqual(100.0, governed[0]["model_percentile"])
        self.assertEqual(88.0, governed[0]["model_score"])
        self.assertEqual(LAKE_MOMENTUM_SCORE_SOURCE, governed[0]["score_source"])
        self.assertEqual("REVIEW", governed[0]["tradability_status"])


class CompactSnapshotFieldTests(unittest.TestCase):
    def test_continuous_score_fields_survive_compaction(self) -> None:
        row = {
            "ticker": "600000.SS",
            "trend_score": 99,
            "model_score": 123.456,
            "model_percentile": 100.0,
            "rank_percentile": 1.0,
            "score_source": LAKE_MOMENTUM_SCORE_SOURCE,
            "unrelated": "dropped",
        }
        compacted = _compact_snapshot_rows([row], limit=1)[0]
        self.assertNotIn("unrelated", compacted)
        self.assertEqual(123.456, compacted["model_score"])
        self.assertEqual(100.0, compacted["model_percentile"])
        self.assertEqual(1.0, compacted["rank_percentile"])
        self.assertEqual(LAKE_MOMENTUM_SCORE_SOURCE, compacted["score_source"])

    def test_whitelist_contains_the_ranking_basis(self) -> None:
        for field in ("model_score", "model_percentile", "rank_percentile", "score_source"):
            self.assertIn(field, SNAPSHOT_ROW_FIELDS)


class DeterministicTieBreakTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        # Every row shares trend_score == 99 (the production tie block); only
        # the continuous score / liquidity / ticker differ.
        return [
            {"ticker": "Z", "market": "CN", "trend_score": 99, "model_percentile": 50.0, "dollar_volume": 100.0},
            {"ticker": "A", "market": "CN", "trend_score": 99, "model_percentile": 90.0, "dollar_volume": 10.0},
            {"ticker": "B", "market": "CN", "trend_score": 99, "model_percentile": 90.0, "dollar_volume": 500.0},
            {"ticker": "C", "market": "CN", "trend_score": 99, "model_percentile": 90.0, "dollar_volume": 500.0},
            {"ticker": "D", "market": "CN", "trend_score": 99, "model_percentile": 10.0, "dollar_volume": 999.0},
        ]

    def test_tie_block_order_is_score_then_liquidity_then_ticker(self) -> None:
        ranked = [row["ticker"] for row in _service()._sort_results(self._rows(), sort_by="trend_score", sort_order="desc")]
        # percentile 90 first (B/C before A by dollar volume, B/C by ticker),
        # then the 50, then the 10 even though it is the most liquid.
        self.assertEqual(["B", "C", "A", "Z", "D"], ranked)

    def test_order_is_independent_of_input_order(self) -> None:
        forward = _service()._sort_results(self._rows(), sort_by="trend_score", sort_order="desc")
        backward = _service()._sort_results(list(reversed(self._rows())), sort_by="trend_score", sort_order="desc")
        shuffled = _service()._sort_results(
            [self._rows()[index] for index in (2, 0, 4, 1, 3)], sort_by="trend_score", sort_order="desc"
        )
        self.assertEqual([row["ticker"] for row in forward], [row["ticker"] for row in backward])
        self.assertEqual([row["ticker"] for row in forward], [row["ticker"] for row in shuffled])

    def test_default_sort_also_breaks_ties_deterministically(self) -> None:
        ranked = [row["ticker"] for row in _service()._sort_results(self._rows(), sort_by="default", sort_order="desc")]
        backward = [
            row["ticker"]
            for row in _service()._sort_results(list(reversed(self._rows())), sort_by="default", sort_order="desc")
        ]
        self.assertEqual(["B", "C", "A", "Z", "D"], ranked)
        self.assertEqual(ranked, backward)

    def test_sort_by_model_percentile_uses_the_continuous_field(self) -> None:
        rows = [
            {"ticker": "LOW", "trend_score": 99, "model_percentile": 10.0},
            {"ticker": "HIGH", "trend_score": 40, "model_percentile": 99.9},
        ]
        ranked = [row["ticker"] for row in _service()._sort_results(rows, sort_by="model_percentile", sort_order="desc")]
        self.assertEqual(["HIGH", "LOW"], ranked)


class PrecomputedQueryTieBreakTests(unittest.TestCase):
    def _params(self, **overrides: object) -> dict:
        params = {
            "min_trend_score": 0,
            "min_listing_days": 0,
            "min_volume_ratio": 0.0,
            "pe_min": -1.0e12,
            "pe_max": 1.0e12,
            "min_roe_avg_3y": -1.0e12,
            "min_net_profit_yoy": -1.0e12,
            "min_revenue_yoy": -1.0e12,
            "max_debt_to_assets": 1.0e12,
            "min_dividend_yield": -1.0e12,
            "exclude_bottom_market_cap_pct": 0.0,
            "recent_snapshot_runs": 0,
            "min_snapshot_hits": 0,
            "model_signal_filter": "ALL",
            "min_model_signal_strength": 0.0,
            "execution_tag_filter": "ALL",
            "exclude_execution_tag_filter": "ALL",
            "sort_by": "default",
            "sort_order": "desc",
            "tradability_status": "ALL",
            "min_trade_readiness": 0.0,
            "limit": 5,
        }
        params.update(overrides)
        return params

    def _rank(self, rows: list[dict], params: dict) -> list[dict]:
        return screener_query._rank_precomputed_rows(_service(), [dict(row) for row in rows], params)

    def _tie_rows(self) -> list[dict]:
        return [
            {"ticker": "Z", "market": "CN", "trend_score": 99, "model_percentile": 50.0, "dollar_volume": 100.0},
            {"ticker": "A", "market": "CN", "trend_score": 99, "model_percentile": 90.0, "dollar_volume": 10.0},
            {"ticker": "B", "market": "CN", "trend_score": 99, "model_percentile": 90.0, "dollar_volume": 500.0},
            {"ticker": "C", "market": "CN", "trend_score": 99, "model_percentile": 90.0, "dollar_volume": 500.0},
            {"ticker": "D", "market": "CN", "trend_score": 99, "model_percentile": 10.0, "dollar_volume": 999.0},
        ]

    def test_top_n_cut_is_deterministic_within_a_tie_block(self) -> None:
        params = self._params()
        forward = [row["ticker"] for row in self._rank(self._tie_rows(), params)]
        backward = [row["ticker"] for row in self._rank(list(reversed(self._tie_rows())), params)]
        self.assertEqual(["B", "C", "A", "Z", "D"], forward)
        self.assertEqual(forward, backward)

    def test_query_can_rank_on_model_percentile(self) -> None:
        rows = [
            {"ticker": "LOW", "market": "CN", "trend_score": 99, "model_percentile": 10.0},
            {"ticker": "HIGH", "market": "CN", "trend_score": 40, "model_percentile": 99.9},
        ]
        ranked = [row["ticker"] for row in self._rank(rows, self._params(sort_by="model_percentile"))]
        self.assertEqual(["HIGH", "LOW"], ranked)

    def test_top_n_truncation_is_input_order_independent(self) -> None:
        params = self._params(limit=2)
        forward = [row["ticker"] for row in screener_query.filter_precomputed_rows(_service(), self._tie_rows(), params)]
        backward = [
            row["ticker"]
            for row in screener_query.filter_precomputed_rows(_service(), list(reversed(self._tie_rows())), params)
        ]
        self.assertEqual(["B", "C"], forward)
        self.assertEqual(forward, backward)


if __name__ == "__main__":
    unittest.main()
