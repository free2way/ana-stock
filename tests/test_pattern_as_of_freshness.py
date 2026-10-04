"""S-5: technical-pattern labels carry an as_of freshness check and annotation."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.api.presentation.screener_components import _pattern_as_of_chip
from app.services.screener import ScreenerService


def _cached(as_of: str) -> dict:
    return {
        "ticker": "600000.SH",
        "as_of_date": as_of,
        "matched_patterns": ["bullish_ma_stack"],
        "bullish_ma_stack": True,
        "volume_breakout": False,
    }


class PatternAsOfFreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ScreenerService()

    def test_fresh_cached_snapshot_is_used_and_marked_current(self) -> None:
        with patch(
            "app.services.screener.latest_completed_market_date",
            return_value="2026-10-02",
        ):
            snapshot, freshness = self.service._resolve_cached_pattern_snapshot(
                "600000.SH", _cached("2026-10-02"), "CN"
            )

        self.assertEqual("current", freshness["pattern_as_of_source"])
        self.assertFalse(freshness["pattern_as_of_stale"])
        self.assertEqual("2026-10-02", freshness["pattern_as_of_date"])
        self.assertEqual(["bullish_ma_stack"], list(snapshot.matched_patterns))

    def test_stale_cached_snapshot_recomputes_live(self) -> None:
        live = SimpleNamespace(
            ticker="600000.SH",
            as_of_date="2026-10-02",
            matched_patterns=["volume_breakout"],
            bullish_ma_stack=False,
            volume_breakout=True,
        )
        with (
            patch("app.services.screener.latest_completed_market_date", return_value="2026-10-02"),
            patch.object(self.service.technical_patterns, "evaluate_ticker", return_value=live) as evaluated,
        ):
            snapshot, freshness = self.service._resolve_cached_pattern_snapshot(
                "600000.SH", _cached("2026-09-01"), "CN"
            )

        evaluated.assert_called_once_with("600000.SH")
        self.assertEqual("live_recompute", freshness["pattern_as_of_source"])
        self.assertFalse(freshness["pattern_as_of_stale"])
        self.assertEqual(["volume_breakout"], list(snapshot.matched_patterns))

    def test_stale_cached_snapshot_is_annotated_when_live_recompute_unavailable(self) -> None:
        with (
            patch("app.services.screener.latest_completed_market_date", return_value="2026-10-02"),
            patch.object(self.service.technical_patterns, "evaluate_ticker", return_value=None),
        ):
            snapshot, freshness = self.service._resolve_cached_pattern_snapshot(
                "600000.SH", _cached("2026-09-01"), "CN"
            )

        # The stale snapshot is kept for display, but must not masquerade as today.
        self.assertEqual("cached_stale", freshness["pattern_as_of_source"])
        self.assertTrue(freshness["pattern_as_of_stale"])
        self.assertEqual("2026-09-01", freshness["pattern_as_of_date"])
        self.assertEqual("2026-10-02", freshness["pattern_expected_as_of_date"])
        self.assertEqual(["bullish_ma_stack"], list(snapshot.matched_patterns))

    def test_page_chip_flags_stale_pattern_labels(self) -> None:
        html = _pattern_as_of_chip(
            {
                "pattern_as_of_stale": True,
                "pattern_as_of_date": "2026-09-01",
                "pattern_expected_as_of_date": "2026-10-02",
            },
            "zh",
        )
        self.assertIn("形态数据滞后", html)
        self.assertIn("2026-09-01", html)
        self.assertEqual(
            "",
            _pattern_as_of_chip({"pattern_as_of_stale": False}, "zh"),
        )
        self.assertEqual("", _pattern_as_of_chip({}, "zh"))


if __name__ == "__main__":
    unittest.main()
