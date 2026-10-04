"""S-12: ledger buy fees, FX-aware aggregation, HK data-unavailable, executable hit口径."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.postgres_safety import ApplicationPostgresTestCase

from app.services.fx_rates import (
    market_data_supported,
    normalize_currency_aggregation,
    resolve_fx_rate,
)
from app.services.portfolio_book import (
    load_portfolio_positions,
    sell_portfolio_position,
    upsert_portfolio_position,
)
from app.services.recommendation_regression import _next_session_metrics


class FxAggregationUnitTests(unittest.TestCase):
    # An explicitly unconfigured operator table.  Passing ``table=None`` would
    # load the ambient DB setting, making this unit test order-/environment-
    # dependent once a real fx_rate_table is installed.
    NO_TABLE = {"available": False, "base": None, "as_of": None, "source": None, "rates": {}}

    def test_mixed_currency_converts_to_base_before_summing(self) -> None:
        rows = [
            {"market": "CN", "market_value": 10000.0},
            {"market": "US", "market_value": 1000.0},
            {"market": "HK", "market_value": 500.0},
        ]
        table = {"available": True, "base": "CNY", "as_of": "2026-10-02", "source": "manual", "rates": {"USD": 7.0, "HKD": 0.9}}
        summary = normalize_currency_aggregation(rows, value_key="market_value", table=table)

        self.assertEqual("ok", summary["fx_status"])
        self.assertEqual("CNY", summary["base_currency"])
        self.assertAlmostEqual(17450.0, summary["total_base"])
        self.assertAlmostEqual(7000.0, rows[1]["market_value_base"])
        self.assertAlmostEqual(0.9, rows[2]["fx_rate"])

    def test_mixed_currency_without_fx_table_never_sums_raw_amounts(self) -> None:
        rows = [
            {"market": "CN", "market_value": 10000.0},
            {"market": "US", "market_value": 1000.0},
        ]
        summary = normalize_currency_aggregation(rows, value_key="market_value", table=self.NO_TABLE)

        self.assertEqual("partial", summary["fx_status"])
        self.assertIn("US", summary["fx_unavailable_markets"])
        # 10000 + 1000 raw would be the bug; only the convertible part is summed.
        self.assertAlmostEqual(10000.0, summary["total_base"])
        self.assertTrue(rows[1]["fx_unavailable"])
        self.assertIsNone(rows[1]["market_value_base"])

    def test_single_currency_needs_no_fx_table(self) -> None:
        rows = [{"market": "US", "market_value": 2500.0}]
        summary = normalize_currency_aggregation(rows, value_key="market_value", table=None)
        self.assertEqual("ok", summary["fx_status"])
        self.assertEqual("USD", summary["base_currency"])
        self.assertAlmostEqual(2500.0, summary["total_base"])

    def test_invalid_table_is_unavailable_not_fabricated(self) -> None:
        self.assertIsNone(resolve_fx_rate("USD", "CNY", {"available": False, "base": None, "rates": {}}))
        self.assertIsNone(resolve_fx_rate("USD", "CNY", {"available": True, "base": "CNY", "rates": {"USD": 0.0}}))
        self.assertIsNone(resolve_fx_rate("USD", "CNY", {"available": True, "base": "CNY", "rates": {}}))

    def test_hk_is_not_a_price_supported_market(self) -> None:
        self.assertFalse(market_data_supported("HK"))
        self.assertTrue(market_data_supported("CN"))
        self.assertTrue(market_data_supported("US"))


class PortfolioLedgerTests(ApplicationPostgresTestCase):
    def test_buy_fee_is_booked_into_cost_and_realized_pnl(self) -> None:
        upsert_portfolio_position(
            {
                "ticker": "ASTS",
                "name": "AST SpaceMobile",
                "market": "US",
                "quantity": 100,
                "cost_basis": 18.5,
                "fee": 5.0,
                "note": "swing",
            }
        )
        position = load_portfolio_positions()[0]
        # Fee spread over 100 shares: 18.50 -> 18.55 effective cost.
        self.assertAlmostEqual(18.55, position["cost_basis"], places=6)
        self.assertAlmostEqual(5.0, position["buy_fee"], places=6)

        result = sell_portfolio_position(
            {
                "ticker": "ASTS",
                "quantity": 100,
                "price": 21.0,
                "fee": 3.0,
                "reason": "止盈/保护利润",
            }
        )
        trade = result["trade"]
        # 2100 - 1855 - 3 = 242; ignoring the buy fee would overstate PnL by 5.
        self.assertAlmostEqual(242.0, trade["realized_pnl"], places=6)

    def test_hk_position_is_flagged_data_unavailable(self) -> None:
        from app.core.db import SessionLocal
        from app.services.portfolio_intelligence import build_portfolio_intelligence

        upsert_portfolio_position(
            {
                "ticker": "0700.HK",
                "name": "Tencent",
                "market": "HK",
                "quantity": 100,
                "cost_basis": 300.0,
                "fee": 0.0,
            }
        )
        with SessionLocal() as db, patch(
            "app.services.portfolio_intelligence.load_latest_closes", return_value={}
        ):
            intelligence = build_portfolio_intelligence(db=db, lang="zh")

        hk_row = intelligence["all_items"][0]
        self.assertTrue(hk_row["data_unavailable"])
        self.assertEqual("unsupported_market", hk_row["data_unavailable_reason"])
        self.assertEqual("数据不可用", hk_row["risk_tag"])


class ExecutableRegressionTests(unittest.TestCase):
    def test_hit_uses_next_open_to_expiry_close_net_of_fees(self) -> None:
        history = [
            {"date": "2026-09-29", "open": 98.0, "high": 99.5, "low": 97.5, "close": 98.5},
            {"date": "2026-09-30", "open": 98.5, "high": 99.5, "low": 98.0, "close": 99.0},
            {"date": "2026-10-01", "open": 100.0, "high": 101.5, "low": 99.5, "close": 101.0},
            {"date": "2026-10-02", "open": 101.0, "high": 101.5, "low": 100.0, "close": 100.5},
            {"date": "2026-10-05", "open": 100.5, "high": 101.0, "low": 100.0, "close": 100.4},
            {"date": "2026-10-06", "open": 100.4, "high": 100.8, "low": 100.0, "close": 100.3},
            {"date": "2026-10-07", "open": 100.3, "high": 100.5, "low": 99.9, "close": 100.2},
        ]
        with patch(
            "app.services.recommendation_regression.load_lake_price_history",
            return_value=history,
        ):
            metrics = _next_session_metrics(ticker="AAPL", market="US", report_date="2026-09-30")

        self.assertIsNotNone(metrics)
        assert metrics is not None
        # Observation口径 (close-to-close) still shows a gain...
        self.assertTrue(metrics["close_hit"])
        self.assertGreater(metrics["close_1d_pct"], 0)
        # ...but the executable, fee-adjusted口径 is negative after costs.
        self.assertEqual("2026-10-01", metrics["executable_entry_date"])
        self.assertEqual("2026-10-07", metrics["executable_exit_date"])
        self.assertEqual(5, metrics["executable_holding_days"])
        self.assertGreater(float(metrics["round_trip_cost_bps"]), 0)
        self.assertLess(metrics["executable_return_pct"], 0)
        self.assertFalse(metrics["executable_hit"])
        self.assertLess(metrics["executable_return_pct"], metrics["executable_gross_return_pct"])


if __name__ == "__main__":
    unittest.main()
