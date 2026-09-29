from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from app.api.main import (
    _app_setting_storage_readiness_summary,
    _prediction_cold_storage_summary,
    _market_health_ticker_filters,
    _storage_capacity_readiness_summary,
    _symbol_readiness_summary,
)


class HealthReadinessToleranceTests(unittest.TestCase):
    def test_us_health_uses_latest_lake_partition_as_operational_cohort(self) -> None:
        with patch(
            "app.api.main.get_latest_lake_trade_date",
            return_value="2026-09-04",
        ), patch(
            "app.api.main.list_lake_symbols_for_trade_date",
            return_value={"AAPL", "MSFT"},
        ):
            filters, scopes = _market_health_ticker_filters(
                cn_supported_tickers={"600000.SS", "000001.SZ"},
            )

        self.assertEqual({"CN": {"600000.SS", "000001.SZ"}, "US": {"AAPL", "MSFT"}}, filters)
        self.assertEqual("supported_price_refresh_universe", scopes["CN"])
        self.assertEqual("latest_lake_partition", scopes["US"])

    def test_cn_supported_scope_survives_missing_us_partition(self) -> None:
        with patch("app.api.main.get_latest_lake_trade_date", return_value=None):
            filters, scopes = _market_health_ticker_filters(
                cn_supported_tickers={"600000.SS"},
            )

        self.assertEqual({"CN": {"600000.SS"}}, filters)
        self.assertEqual("supported_price_refresh_universe", scopes["CN"])
        self.assertNotIn("US", scopes)

    def test_app_setting_storage_is_degraded_when_inline_limit_is_exceeded(self) -> None:
        result = _app_setting_storage_readiness_summary(
            {
                "threshold_bytes": 32768,
                "setting_count": 19,
                "oversized_inline_count": 1,
                "maximum_inline_bytes": 90000,
            }
        )

        self.assertEqual("degraded", result["status"])
        self.assertEqual(1, result["oversized_inline_count"])

    def test_app_setting_storage_is_ready_when_all_values_fit(self) -> None:
        result = _app_setting_storage_readiness_summary(
            {
                "threshold_bytes": 32768,
                "setting_count": 19,
                "oversized_inline_count": 0,
                "maximum_inline_bytes": 11869,
            }
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual(0, result["oversized_inline_count"])

    def test_fewer_than_fifteen_abnormal_symbols_are_tolerated(self) -> None:
        result = _symbol_readiness_summary(
            {
                "symbol_state_status": "partial",
                "fresh_count": 5180,
                "stale_count": 6,
                "missing_count": 0,
            }
        )

        self.assertEqual("fresh_with_tolerance", result["effective_status"])
        self.assertEqual("ok", result["readiness_status"])
        self.assertEqual(6, result["abnormal_count"])
        self.assertTrue(result["tolerated"])

    def test_exactly_fifteen_abnormal_symbols_are_tolerated(self) -> None:
        result = _symbol_readiness_summary(
            {
                "symbol_state_status": "partial",
                "fresh_count": 100,
                "stale_count": 12,
                "missing_count": 3,
            }
        )

        self.assertEqual("fresh_with_tolerance", result["effective_status"])
        self.assertEqual("ok", result["readiness_status"])
        self.assertTrue(result["tolerated"])

    def test_sixteen_abnormal_symbols_remain_degraded(self) -> None:
        result = _symbol_readiness_summary(
            {"symbol_state_status": "partial", "fresh_count": 100,
             "stale_count": 12, "missing_count": 4}
        )

        self.assertEqual(16, result["abnormal_count"])
        self.assertEqual("degraded", result["readiness_status"])
        self.assertFalse(result["tolerated"])

    def test_reconciled_blocking_count_controls_health_tolerance(self) -> None:
        result = _symbol_readiness_summary(
            {
                "symbol_state_status": "partial",
                "fresh_count": 100,
                "stale_count": 20,
                "missing_count": 5,
                "blocking_anomaly_count": 3,
            }
        )

        self.assertEqual(25, result["raw_abnormal_count"])
        self.assertEqual(3, result["abnormal_count"])
        self.assertEqual("ok", result["readiness_status"])

    def test_reconciled_accounted_count_allows_lake_backed_tolerance(self) -> None:
        result = _symbol_readiness_summary(
            {
                "symbol_state_status": "stale",
                "fresh_count": 0,
                "stale_count": 100,
                "missing_count": 0,
                "blocking_anomaly_count": 9,
                "accounted_symbol_count": 91,
            }
        )

        self.assertEqual(9, result["abnormal_count"])
        self.assertEqual(91, result["accounted_count"])
        self.assertTrue(result["tolerated"])
        self.assertEqual("ok", result["readiness_status"])

    def test_empty_or_entirely_stale_market_is_not_tolerated(self) -> None:
        empty = _symbol_readiness_summary(
            {
                "symbol_state_status": "missing",
                "fresh_count": 0,
                "stale_count": 0,
                "missing_count": 0,
            }
        )
        entirely_stale = _symbol_readiness_summary(
            {
                "symbol_state_status": "stale",
                "fresh_count": 0,
                "stale_count": 6,
                "missing_count": 0,
            }
        )

        self.assertEqual("degraded", empty["readiness_status"])
        self.assertEqual("degraded", entirely_stale["readiness_status"])
        self.assertFalse(empty["tolerated"])
        self.assertFalse(entirely_stale["tolerated"])

    def test_cold_storage_rollback_is_reported_as_degraded(self) -> None:
        with patch("app.api.main.settings.prediction_cold_reads_enabled", False):
            result = _prediction_cold_storage_summary()

        self.assertEqual("degraded", result["status"])
        self.assertEqual("postgresql_only_rollback", result["mode"])
        self.assertFalse(result["read_enabled"])

    def test_current_capacity_sample_is_ready_while_growth_collects(self) -> None:
        result = _storage_capacity_readiness_summary(
            {
                "status": "collecting",
                "sample_count": 1,
                "required_intervals": 5,
                "average_daily_growth_bytes": None,
                "limit_bytes": 20 * 1024 * 1024,
                "latest_sample": {"sample_date": "2026-08-22"},
            },
            enabled=True,
            today=date(2026, 8, 22),
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual("collecting", result["growth_status"])

    def test_stale_capacity_sample_is_reported(self) -> None:
        result = _storage_capacity_readiness_summary(
            {
                "status": "collecting",
                "sample_count": 1,
                "required_intervals": 5,
                "latest_sample": {"sample_date": "2026-08-20"},
            },
            enabled=True,
            today=date(2026, 8, 22),
        )

        self.assertEqual("stale", result["status"])
        self.assertEqual(2, result["sample_age_days"])


if __name__ == "__main__":
    unittest.main()
