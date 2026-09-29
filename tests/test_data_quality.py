from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.data_quality import market_data_gate


class MarketDataGateTests(unittest.TestCase):
    @staticmethod
    def _overview(*, fresh: int, stale: int, missing: int = 0) -> dict:
        return {
            "CN": {
                "total_count": fresh + stale + missing,
                "fresh_count": fresh,
                "exception_count": 0,
                "no_trade_count": 0,
                "inactive_count": 0,
                "manual_approved_count": 0,
                "stale_count": stale,
                "missing_count": missing,
                "expected_as_of_date": "2026-08-21",
                "authoritative_as_of_date": "2026-08-21",
            }
        }

    def test_fewer_than_ten_abnormal_symbols_are_graded_and_non_blocking(self) -> None:
        with patch(
            "app.services.data_quality.PriceSyncStateRepository.get_market_freshness_overview",
            return_value=self._overview(fresh=5196, stale=6),
        ):
            result = market_data_gate(object(), market="CN")

        self.assertEqual("graded", result["status"])
        self.assertTrue(result["tolerated"])
        self.assertEqual(10, result["abnormal_limit"])

    def test_ten_abnormal_symbols_remain_blocked(self) -> None:
        with patch(
            "app.services.data_quality.PriceSyncStateRepository.get_market_freshness_overview",
            return_value=self._overview(fresh=5192, stale=6, missing=4),
        ):
            result = market_data_gate(object(), market="CN")

        self.assertEqual("blocked", result["status"])
        self.assertFalse(result["tolerated"])

    def test_entirely_abnormal_small_market_remains_blocked(self) -> None:
        with patch(
            "app.services.data_quality.PriceSyncStateRepository.get_market_freshness_overview",
            return_value=self._overview(fresh=0, stale=6),
        ):
            result = market_data_gate(object(), market="CN")

        self.assertEqual("blocked", result["status"])
        self.assertFalse(result["tolerated"])

    def test_reconciled_anomaly_count_ignores_state_lag_present_in_lake(self) -> None:
        overview = self._overview(fresh=100, stale=12)
        overview["CN"].update(
            {
                "blocking_anomaly_count": 2,
                "accounted_symbol_count": 110,
                "anomaly_classification": {
                    "classification_version": "market-symbol-anomaly-v1",
                    "counts": {"lake_present_state_lag": 10, "stale_unclassified": 2},
                },
            }
        )
        with patch(
            "app.services.data_quality.PriceSyncStateRepository.get_market_freshness_overview",
            return_value=overview,
        ):
            result = market_data_gate(object(), market="CN")

        self.assertEqual("graded", result["status"])
        self.assertEqual(12, result["raw_abnormal_count"])
        self.assertEqual(2, result["blocking_anomaly_count"])


if __name__ == "__main__":
    unittest.main()
