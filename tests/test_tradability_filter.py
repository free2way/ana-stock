"""Gap-chase hard entry rule: large signal-day gaps must not be chase-ready.

The executable-label regime enters at the next open, so a signal-day gap is a
direct haircut on the modeled edge.  These tests pin the rule's trigger
threshold, style exemption, and None-safe behavior.
"""
import unittest

from app.services.tradability_filter import (
    TradabilityRuleConfig,
    evaluate_candidate_tradability,
)


def _candidate(**overrides):
    base = {
        "ticker": "600000.SS",
        "market": "CN",
        "score": 0.30,
        "signal_label": "BUY",
        "signal_strength": 85.0,
        "latest_close": 10.0,
        "entry_style": "breakout",
    }
    base.update(overrides)
    return base


class GapChaseRuleTest(unittest.TestCase):
    def test_large_signal_day_gap_downgrades_ready_chase(self):
        decision = evaluate_candidate_tradability(_candidate(signal_open_gap_pct=5.2))
        self.assertIn("gap-chase-risk", decision.risk_flags)
        self.assertNotEqual("READY", decision.tradability_status)

    def test_moderate_gap_stays_unflagged(self):
        decision = evaluate_candidate_tradability(_candidate(signal_open_gap_pct=1.8))
        self.assertNotIn("gap-chase-risk", decision.risk_flags)

    def test_pullback_style_is_exempt(self):
        decision = evaluate_candidate_tradability(
            _candidate(signal_open_gap_pct=5.2, entry_style="pullback")
        )
        self.assertNotIn("gap-chase-risk", decision.risk_flags)

    def test_missing_gap_is_a_noop(self):
        decision = evaluate_candidate_tradability(_candidate())
        self.assertNotIn("gap-chase-risk", decision.risk_flags)

    def test_config_threshold_override(self):
        decision = evaluate_candidate_tradability(
            _candidate(signal_open_gap_pct=5.2),
            config=TradabilityRuleConfig(max_signal_open_gap_pct=6.0),
        )
        self.assertNotIn("gap-chase-risk", decision.risk_flags)

    def test_gap_unit_is_percent_not_fraction(self):
        decision = evaluate_candidate_tradability(_candidate(signal_open_gap_pct=0.052))
        self.assertNotIn("gap-chase-risk", decision.risk_flags)


if __name__ == "__main__":
    unittest.main()