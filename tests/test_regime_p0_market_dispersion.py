"""P0-6 regression: index-masking-weak-breadth must flag on CN, not only US.

CN structural rallies (heavyweights walked up to hold the index while the
median stock falls) previously fell through to `watchful`, so the model kept
attacking the exact regime where next-open entries have the worst follow-through.
"""
import unittest

from app.services.market_risk import _classify_market


def _day(**overrides) -> dict:
    row = {
        "avg_ret_pct": 0.2,
        "median_ret_pct": 0.1,
        "up_pct": 52.0,
        "down3_pct": 12.0,
        "down5_pct": 6.0,
        "near_20d_low_pct": 30.0,
        "avg_range_pct": 2.5,
    }
    row.update(overrides)
    return row


class MarketDispersionRegimeTests(unittest.TestCase):
    def test_cn_high_dispersion_day_flags_index_masking(self) -> None:
        result = _classify_market("CN", [_day(avg_ret_pct=1.8, median_ret_pct=-0.4)])

        self.assertEqual("high_dispersion", result["risk_regime"])
        self.assertEqual("REVIEW", result["buy_gate"])
        self.assertEqual(0.35, result["max_position_scale"])
        self.assertIn("index-masking-weak-breadth", result["flags"])

    def test_cn_breadth_masked_structural_rally_flags_regime(self) -> None:
        # Positive average over a non-positive median with <45% of stocks up:
        # the +/-10/20% limit bands compress the avg-median gap, so breadth
        # alone must flag the regime on CN.
        result = _classify_market("CN", [_day(avg_ret_pct=0.6, median_ret_pct=-0.1, up_pct=38.0)])

        self.assertEqual("high_dispersion", result["risk_regime"])
        self.assertEqual("REVIEW", result["buy_gate"])
        self.assertEqual(0.35, result["max_position_scale"])

    def test_cn_recent_high_dispersion_window_flags_regime(self) -> None:
        calm_today = _day(avg_ret_pct=0.5, median_ret_pct=0.2, up_pct=55.0)
        masked_yesterday = _day(avg_ret_pct=2.6, median_ret_pct=-0.6)
        result = _classify_market("CN", [calm_today, masked_yesterday])

        self.assertEqual("high_dispersion", result["risk_regime"])

    def test_cn_broad_rally_stays_risk_on(self) -> None:
        result = _classify_market("CN", [_day(avg_ret_pct=1.2, median_ret_pct=0.5, up_pct=62.0)])

        self.assertEqual("risk_on", result["risk_regime"])
        self.assertEqual("ALLOW", result["buy_gate"])
        self.assertEqual(1.0, result["max_position_scale"])

    def test_cn_mild_day_is_not_escalated(self) -> None:
        result = _classify_market("CN", [_day()])

        self.assertEqual("watchful", result["risk_regime"])
        self.assertNotIn("index-masking-weak-breadth", result["flags"])

    def test_us_dispersion_channel_is_preserved(self) -> None:
        result = _classify_market("US", [_day(avg_ret_pct=2.4, median_ret_pct=-0.5)])

        self.assertEqual("high_dispersion", result["risk_regime"])
        self.assertIn("index-masking-weak-breadth", result["flags"])


if __name__ == "__main__":
    unittest.main()
