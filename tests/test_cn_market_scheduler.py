from __future__ import annotations

import unittest

from app.services.cn_market_scheduler import _post_refresh_ready


class CNMarketSchedulerTests(unittest.TestCase):
    def test_partial_refresh_with_current_lake_starts_post_close_pipeline(self) -> None:
        self.assertTrue(
            _post_refresh_ready(
                {"status": "partial"},
                target_trade_date="2026-07-23",
                latest_lake_trade_date="2026-07-23",
            )
        )

    def test_stale_lake_never_starts_post_close_pipeline(self) -> None:
        self.assertFalse(
            _post_refresh_ready(
                {"status": "success"},
                target_trade_date="2026-07-23",
                latest_lake_trade_date="2026-07-22",
            )
        )

    def test_failed_refresh_never_starts_post_close_pipeline(self) -> None:
        self.assertFalse(
            _post_refresh_ready(
                {"status": "failed"},
                target_trade_date="2026-07-23",
                latest_lake_trade_date="2026-07-23",
            )
        )


if __name__ == "__main__":
    unittest.main()
