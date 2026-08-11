from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.services.market_freshness import (
    is_snapshot_as_of_current,
    latest_completed_market_date,
    summarize_market_freshness,
)


class MarketFreshnessTests(TestCase):
    def test_latest_completed_date_skips_juneteenth_weekend(self) -> None:
        now = datetime(2026, 6, 20, 12, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual("2026-06-18", latest_completed_market_date("CN", now=now))
        self.assertEqual("2026-06-18", latest_completed_market_date("US", now=now))

    def test_summary_marks_old_prices_stale_even_when_state_write_is_recent(self) -> None:
        result = summarize_market_freshness(
            [
                {"market": "CN", "last_synced_date": "2026-06-11", "updated_at": "2026-06-17"},
                {"market": "CN", "last_synced_date": "2026-06-18", "updated_at": "2026-06-20"},
            ],
            market="CN",
            expected_as_of_date="2026-06-18",
        )

        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["fresh_count"])
        self.assertEqual(1, result["stale_count"])

    def test_summary_excludes_explicit_no_trade_symbols_from_stale_count(self) -> None:
        result = summarize_market_freshness(
            [
                {
                    "market": "CN",
                    "last_synced_date": "2026-06-17",
                    "status": "no_trade",
                },
                {
                    "market": "CN",
                    "last_synced_date": "2026-06-18",
                    "status": "success",
                },
            ],
            market="CN",
            expected_as_of_date="2026-06-18",
        )

        self.assertEqual("fresh", result["status"])
        self.assertEqual(1, result["fresh_count"])
        self.assertEqual(0, result["stale_count"])
        self.assertEqual(1, result["no_trade_count"])
        self.assertEqual(1, result["eligible_count"])

    def test_summary_keeps_manual_approval_distinct_from_provider_confirmed_no_trade(self) -> None:
        result = summarize_market_freshness(
            [
                {"market": "CN", "last_synced_date": "2026-06-17", "status": "manual_approved"},
                {"market": "CN", "last_synced_date": "2026-06-18", "status": "success"},
            ],
            market="CN",
            expected_as_of_date="2026-06-18",
        )

        self.assertEqual("fresh", result["status"])
        self.assertEqual(1, result["manual_approved_count"])
        self.assertEqual(0, result["no_trade_count"])

    def test_snapshot_freshness_is_market_date_based(self) -> None:
        with patch("app.services.market_freshness.latest_completed_market_date", return_value="2026-07-22"):
            self.assertTrue(is_snapshot_as_of_current("2026-07-22T20:00:00+08:00", "CN"))
            self.assertFalse(is_snapshot_as_of_current("2026-07-21T20:00:00+08:00", "CN"))
