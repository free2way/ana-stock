"""Execution-feasibility hard gates for the precomputed screening chain.

Covers: (1) opt-in tradability/readiness hard gates, (2) CN signal-day limit-up
demotion/tagging, (3) query-time bottom-market-cap exclusion.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.services.market_lake import screen_lake_momentum
from app.services.screener import ScreenerService
from app.services.screener_snapshots import _compact_snapshot_rows
from app.services.stock_selection import screener_query


def _passthrough_service() -> MagicMock:
    service = MagicMock()
    service._apply_snapshot_persistence_filter.side_effect = lambda values, **_kwargs: values
    service._apply_model_signal_filter.side_effect = lambda values, **_kwargs: values
    service._apply_execution_tag_filter.side_effect = lambda values, **_kwargs: values
    # Identity sort keeps input order observable so demotion is measurable.
    service._sort_results.side_effect = lambda values, **_kwargs: values
    return service


def _params(**overrides: object) -> dict:
    params = {
        "model_template": "technical_momentum",
        "universe": "full_market",
        "market": "CN",
        "lang": "zh",
    }
    params.update(overrides)
    return screener_query.normalize_screen_params(params)


def _rank(rows: list[dict], params: dict) -> list[dict]:
    normalized = {
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
    }
    normalized.update(params)
    return screener_query._rank_precomputed_rows(_passthrough_service(), [dict(row) for row in rows], normalized)


class TradabilityHardGateTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        return [
            {"ticker": "READY", "market": "CN", "tradability_status": "READY", "trade_readiness_score": 80.0},
            {"ticker": "REVIEW", "market": "CN", "tradability_status": "REVIEW", "trade_readiness_score": 60.0},
            {"ticker": "BLOCKED", "market": "CN", "tradability_status": "BLOCKED", "trade_readiness_score": 95.0},
            {"ticker": "UNKNOWN", "market": "CN"},
        ]

    def test_default_params_keep_the_legacy_no_gate_behaviour(self) -> None:
        ranked = _rank(self._rows(), {"model_template": "technical_momentum"})
        self.assertEqual(
            {"READY", "REVIEW", "BLOCKED", "UNKNOWN"},
            {row["ticker"] for row in ranked},
        )

    def test_status_whitelist_filters_rows(self) -> None:
        ranked = _rank(self._rows(), {"tradability_status": "READY,REVIEW"})
        self.assertEqual({"READY", "REVIEW"}, {row["ticker"] for row in ranked})

    def test_min_readiness_is_an_independent_hard_gate(self) -> None:
        ranked = _rank(self._rows(), {"min_trade_readiness": 70.0})
        # BLOCKED (95) passes the numeric gate but UNKNOWN (no score) does not;
        # the status whitelist is a separate gate that is off here.
        self.assertEqual({"READY", "BLOCKED"}, {row["ticker"] for row in ranked})

    def test_relaxing_the_gate_only_adds_rows(self) -> None:
        strict = _rank(
            self._rows(),
            {"tradability_status": "READY", "min_trade_readiness": 50.0},
        )
        relaxed = _rank(
            self._rows(),
            {"tradability_status": "READY,REVIEW", "min_trade_readiness": 0.0},
        )
        strict_tickers = {row["ticker"] for row in strict}
        relaxed_tickers = {row["ticker"] for row in relaxed}
        self.assertEqual({"READY"}, strict_tickers)
        self.assertTrue(strict_tickers.issubset(relaxed_tickers))
        self.assertIn("REVIEW", relaxed_tickers)


class CnLimitUpGovernanceTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        return [
            {"ticker": "CN_LIMITUP", "market": "CN", "limit_up_today": True, "trade_readiness_score": 90.0},
            {"ticker": "CN_NORMAL", "market": "CN", "limit_up_today": False, "trade_readiness_score": 40.0},
            {"ticker": "US_LIMITUP", "market": "US", "limit_up_today": True, "trade_readiness_score": 95.0},
        ]

    def test_cn_limit_up_is_demoted_and_tagged(self) -> None:
        ranked = _rank(self._rows(), {"model_template": "technical_momentum"})
        order = [row["ticker"] for row in ranked]
        self.assertEqual(["CN_NORMAL", "US_LIMITUP", "CN_LIMITUP"], order)
        limit_row = next(row for row in ranked if row["ticker"] == "CN_LIMITUP")
        self.assertIn("limit-up-today", limit_row["model_execution_tags"])
        self.assertTrue(limit_row["limit_up_demoted"])
        # A US limit-up row is out of scope and must not be disturbed.
        us_row = next(row for row in ranked if row["ticker"] == "US_LIMITUP")
        self.assertNotIn("limit-up-today", us_row.get("model_execution_tags") or [])

    def test_limit_up_observation_template_keeps_its_own_order(self) -> None:
        ranked = _rank(self._rows(), {"model_template": "cn_limit_up_watch"})
        self.assertEqual(
            ["CN_LIMITUP", "CN_NORMAL", "US_LIMITUP"],
            [row["ticker"] for row in ranked],
        )
        limit_row = next(row for row in ranked if row["ticker"] == "CN_LIMITUP")
        self.assertNotIn("limit-up-today", limit_row.get("model_execution_tags") or [])


class MarketCapExclusionTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        caps = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        rows = [
            {"ticker": f"MK{index:02d}", "market": "US", "market_cap": cap}
            for index, cap in enumerate(caps, start=1)
        ]
        rows.append({"ticker": "NOCAP", "market": "US"})
        return rows

    def test_threshold_matches_the_fundamental_path(self) -> None:
        rows = [{"market_cap": cap} for cap in (10.0, 20.0, 30.0, 40.0, 50.0)]
        expected = ScreenerService()._compute_market_cap_threshold(rows, 10.0)
        self.assertEqual(expected, screener_query._market_cap_threshold(rows, 10.0))
        self.assertEqual(10.0, expected)

    def test_bottom_decile_is_removed_but_rows_without_a_cap_survive(self) -> None:
        ranked = _rank(self._rows(), {"exclude_bottom_market_cap_pct": 10.0})
        tickers = {row["ticker"] for row in ranked}
        self.assertNotIn("MK01", tickers)  # cap 10 <= threshold 20
        self.assertNotIn("MK02", tickers)  # cap 20 <= threshold 20
        self.assertIn("MK03", tickers)
        self.assertIn("NOCAP", tickers)  # unknown cap is never dropped

    def test_relaxing_the_cap_percentile_only_adds_rows(self) -> None:
        strict = _rank(self._rows(), {"exclude_bottom_market_cap_pct": 20.0})
        relaxed = _rank(self._rows(), {"exclude_bottom_market_cap_pct": 0.0})
        strict_tickers = {row["ticker"] for row in strict}
        relaxed_tickers = {row["ticker"] for row in relaxed}
        self.assertTrue(strict_tickers.issubset(relaxed_tickers))
        self.assertGreater(len(relaxed_tickers), len(strict_tickers))
        self.assertIn("MK01", relaxed_tickers)

    def test_cap_and_limit_up_fields_survive_snapshot_compaction(self) -> None:
        row = {
            "ticker": "600000.SS",
            "market_cap": 5_000_000_000.0,
            "limit_up_today": True,
            "limit_band_pct": 10.0,
            "volume": 1_000_000.0,
            "dollar_volume": 50_000_000.0,
            "unrelated": "dropped",
        }
        compacted = _compact_snapshot_rows([row], limit=1)
        self.assertNotIn("unrelated", compacted[0])
        self.assertEqual(5_000_000_000.0, compacted[0]["market_cap"])
        self.assertTrue(compacted[0]["limit_up_today"])
        self.assertEqual(10.0, compacted[0]["limit_band_pct"])
        # 逐票可成交门槛构造所需的量/额字段必须透传（treated 定义与 regime 解耦）。
        self.assertEqual(1_000_000.0, compacted[0]["volume"])
        self.assertEqual(50_000_000.0, compacted[0]["dollar_volume"])


class LakeCnLimitUpTests(unittest.TestCase):
    def _columns(self) -> list[str]:
        return [
            "date",
            "symbol",
            "close",
            "volume",
            "dollar_volume",
            "momentum_5",
            "momentum_20",
            "prev_close",
            "volume_ratio",
            "ma20",
            "ma60",
            "pullback_depth_pct",
            "distance_to_breakout_pct",
        ]

    def test_cn_rows_carry_limit_up_today_and_band(self) -> None:
        rows = [
            ("2026-10-02", "600000.SS", 11.0, 1_000_000, 11_000_000.0, 0.02, 0.10, 10.0, 1.2, 10.5, 10.0, 1.0, -1.0),
            ("2026-10-02", "000001.SZ", 10.5, 1_000_000, 10_500_000.0, 0.01, 0.05, 10.0, 1.1, 10.2, 10.0, 1.0, -1.0),
        ]
        with (
            patch("app.services.market_lake._recent_parquet_files", return_value=["dummy.parquet"]),
            patch("app.services.market_lake._duckdb_fetchall", return_value=(rows, self._columns())),
            patch(
                "app.services.market_lake._resolve_cn_limit_bands",
                return_value={"600000.SS": 10.0, "000001.SZ": 10.0},
            ),
        ):
            result = screen_lake_momentum(market="CN", trade_date="2026-10-02", limit=5)

        by_symbol = {row["symbol"]: row for row in result}
        # +10% vs the 10cm band -> limit-up; +5% is not.
        self.assertTrue(by_symbol["600000.SS"]["limit_up_today"])
        self.assertFalse(by_symbol["000001.SZ"]["limit_up_today"])
        self.assertEqual(10.0, by_symbol["600000.SS"]["limit_band_pct"])

    def test_us_rows_are_not_annotated(self) -> None:
        columns = self._columns()
        rows = [
            ("2026-10-02", "AAA", 11.0, 1_000_000, 11_000_000.0, 0.02, 0.10, 10.0, 1.2, 10.5, 10.0, 1.0, -1.0),
        ]
        with (
            patch("app.services.market_lake._recent_parquet_files", return_value=["dummy.parquet"]),
            patch("app.services.market_lake._duckdb_fetchall", return_value=(rows, columns)),
        ):
            result = screen_lake_momentum(market="US", trade_date="2026-10-02", limit=5)

        self.assertNotIn("limit_up_today", result[0])


if __name__ == "__main__":
    unittest.main()
