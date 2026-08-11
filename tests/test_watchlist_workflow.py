from __future__ import annotations

import unittest

from app.api.routes.watchlist import _render_watchlist_market_context, _watchlist_queue_bucket


class WatchlistWorkflowTests(unittest.TestCase):
    def test_risk_is_prioritized_over_archiving(self) -> None:
        item = {
            "sync_enabled": False,
            "execution_tags": ["earnings-soon"],
            "combined_analysis": {"decision": "BUY"},
        }

        self.assertEqual("risk", _watchlist_queue_bucket(item))

    def test_action_buckets_follow_next_human_action(self) -> None:
        self.assertEqual(
            "primary",
            _watchlist_queue_bucket({"sync_enabled": True, "execution_tags": [], "combined_analysis": {"decision": "STRONG BUY"}}),
        )
        self.assertEqual(
            "observe",
            _watchlist_queue_bucket({"sync_enabled": True, "execution_tags": [], "combined_analysis": {"decision": "HOLD"}}),
        )
        self.assertEqual(
            "archive",
            _watchlist_queue_bucket({"sync_enabled": False, "execution_tags": [], "combined_analysis": {"decision": "HOLD"}}),
        )
        self.assertEqual(
            "risk",
            _watchlist_queue_bucket({"sync_enabled": True, "execution_tags": [], "combined_analysis": {"decision": "SELL"}}),
        )

    def test_market_context_keeps_a_share_and_us_freshness_separate(self) -> None:
        rendered = _render_watchlist_market_context(
            lang="zh",
            items=[
                {"ticker": "600519.SH", "market": "CN", "sync_enabled": True, "sync_status": "success", "last_synced_date": "2026-07-22"},
                {"ticker": "AAPL", "market": "US", "sync_enabled": True, "sync_status": "success", "last_synced_date": "2026-07-22"},
            ],
        )

        self.assertIn("A 股", rendered)
        self.assertIn("美股", rendered)
        self.assertIn("watchlist-market-CN", rendered)
        self.assertIn("watchlist-market-US", rendered)


if __name__ == "__main__":
    unittest.main()
