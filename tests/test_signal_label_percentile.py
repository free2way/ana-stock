"""Signal text label aligns with the percentile-normalized badge.

The badge (``build_model_state``) was re-based onto cross-sectional quintiles
when scores compressed to the net-return scale, but the Buy/Watch/Hold/Sell
text label still used the old absolute thresholds and pinned nearly every row
to Hold. These tests lock the two onto the same percentile contract while
preserving the legacy behaviour for percentile-less callers.
"""

from __future__ import annotations

from unittest import TestCase

from app.api.routes.dashboard._common import _dashboard_home_signal, _signal_pill
from app.services.model_signal_summary import (
    build_model_state,
    build_signal_label,
    enrich_model_output,
)


class SignalLabelPercentileTests(TestCase):
    def test_quintile_boundaries_match_badge_contract(self) -> None:
        # score is deliberately tiny (the compressed production scale): only the
        # percentile may drive the label.
        score = 0.005
        cases = {
            100.0: "买点",
            80.0: "买点",
            79.9: "观察",
            60.0: "观察",
            59.9: "持有",
            40.0: "持有",
            39.9: "持有",
            20.0: "卖点",
            0.0: "卖点",
        }
        for percentile, expected in cases.items():
            with self.subTest(percentile=percentile):
                self.assertEqual(
                    expected,
                    build_signal_label(score, lang="zh", percentile=percentile),
                )
                self.assertEqual(
                    {"买点": "Buy", "观察": "Watch", "持有": "Hold", "卖点": "Sell"}[expected],
                    build_signal_label(score, lang="en", percentile=percentile),
                )

    def test_badge_and_label_never_contradict(self) -> None:
        # strong/positive => buy/watch, weak => sell, neutral/cautious => hold.
        expected_key = {
            "买点": {"strong"},
            "观察": {"positive"},
            "持有": {"neutral", "cautious"},
            "卖点": {"weak"},
        }
        for percentile in (0.0, 10.0, 20.0, 30.0, 40.0, 55.0, 60.0, 70.0, 80.0, 95.0, 100.0):
            with self.subTest(percentile=percentile):
                label = build_signal_label(0.004, lang="zh", percentile=percentile)
                key = build_model_state(0.004, lang="zh", percentile=percentile)["key"]
                self.assertIn(key, expected_key[label])

    def test_without_percentile_keeps_legacy_thresholds(self) -> None:
        self.assertEqual("买点", build_signal_label(0.18, lang="zh"))
        self.assertEqual("观察", build_signal_label(0.05, lang="zh"))
        self.assertEqual("持有", build_signal_label(0.049, lang="zh"))
        self.assertEqual("卖点", build_signal_label(-0.05, lang="zh"))
        self.assertEqual("持有", build_signal_label(-0.049, lang="zh"))
        # A compressed score with no percentile stays Hold (pre-change behaviour).
        self.assertEqual("持有", build_signal_label(0.005, lang="zh"))

    def test_unusable_percentile_falls_back_to_score_thresholds(self) -> None:
        for percentile in (None, "bad", float("nan"), -1.0, 101.0, True):
            with self.subTest(percentile=percentile):
                self.assertEqual(
                    "买点",
                    build_signal_label(0.2, lang="zh", percentile=percentile),
                )

    def test_missing_score_stays_none(self) -> None:
        self.assertIsNone(build_signal_label(None, lang="zh", percentile=99.0))

    def test_enrich_model_output_uses_percentile_for_signal_label(self) -> None:
        enriched = enrich_model_output(
            {"score": 0.004, "percentile": 95.0}, lang="zh"
        )
        self.assertEqual("买点", enriched["signal_label"])
        self.assertIn(enriched["state"]["key"], {"strong"})

    def test_dashboard_helpers_use_percentile(self) -> None:
        self.assertEqual(("买点", "sig-buy"), _dashboard_home_signal(0.004, "zh", 95.0))
        self.assertEqual(("卖点", "sig-sell"), _dashboard_home_signal(0.004, "zh", 5.0))
        pill = _signal_pill(0.004, lang="zh", compact=True, percentile=95.0)
        self.assertIn("买点", pill)
        # Without a percentile the pill keeps the legacy Hold text.
        self.assertIn("持有", _signal_pill(0.004, lang="zh", compact=True))
