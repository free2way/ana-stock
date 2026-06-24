from datetime import datetime
from unittest import TestCase
from zoneinfo import ZoneInfo

from app.services.market_freshness import latest_completed_market_date, summarize_market_freshness


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
