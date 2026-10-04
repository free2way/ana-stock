from __future__ import annotations

from unittest import TestCase

from app.services.price_basis import preferred_close, preferred_price
from app.services.selection_quality import _next_session_metrics_from_history


class PreferredCloseTests(TestCase):
    def test_adjusted_close_wins_over_legacy_alias_and_raw(self) -> None:
        row = {"adjusted_close": 5.0, "adj_close": 9.0, "close": 10.0}
        self.assertEqual(5.0, preferred_close(row))

    def test_legacy_adj_close_alias_wins_over_raw_close(self) -> None:
        self.assertEqual(9.0, preferred_close({"adj_close": 9.0, "close": 10.0}))

    def test_raw_close_is_the_last_resort(self) -> None:
        self.assertEqual(10.0, preferred_close({"close": 10.0}))

    def test_missing_or_non_numeric_rows_return_none(self) -> None:
        self.assertIsNone(preferred_close({}))
        self.assertIsNone(preferred_close({"adjusted_close": None, "adj_close": None, "close": None}))
        self.assertIsNone(preferred_close({"close": "not-a-number"}))
        self.assertIsNone(preferred_close({"close": float("nan")}))
        self.assertIsNone(preferred_close(None))  # type: ignore[arg-type]

    def test_unusable_candidate_falls_through_to_next_priority(self) -> None:
        # A blank/garbled adjusted value must not blank out a valid raw close.
        self.assertEqual(10.0, preferred_close({"adjusted_close": "", "adj_close": "x", "close": 10.0}))

    def test_numeric_strings_are_accepted(self) -> None:
        self.assertEqual(6.5, preferred_close({"adjusted_close": "6.5", "close": 7.0}))

    def test_preferred_price_generalizes_to_other_fields(self) -> None:
        row = {"adjusted_open": 1.5, "open": 2.0, "adjusted_close": 3.0, "close": 4.0}
        self.assertEqual(1.5, preferred_price(row, "open"))
        self.assertEqual(2.0, preferred_price({"open": 2.0}, "open"))
        # The legacy adj_close alias is close-specific, never an open fallback.
        self.assertIsNone(preferred_price({"adj_close": 9.0}, "open"))
        self.assertEqual(3.0, preferred_price(row, "close"))
        self.assertEqual(2.0, preferred_price({"high": 2.0}, "high"))

    def test_preferred_price_rejects_blank_field(self) -> None:
        self.assertIsNone(preferred_price({"close": 1.0}, ""))


class SelectionQualityAdjustedCloseTests(TestCase):
    def test_close_return_uses_adjusted_close_while_gap_stays_raw(self) -> None:
        history = [
            {
                "date": "2026-07-01",
                "open": 9.8,
                "high": 10.2,
                "low": 9.5,
                "close": 10.0,
                "adjusted_close": 5.0,
            },
            {
                "date": "2026-07-02",
                "open": 10.5,
                "high": 11.5,
                "low": 10.0,
                "close": 11.0,
                "adjusted_close": 6.0,
            },
        ]
        metrics = _next_session_metrics_from_history(history=history, report_date="2026-07-01")
        assert metrics is not None
        # Adjusted basis: 6.0 / 5.0 - 1 = +20%, not the raw 10.0 -> 11.0 (+10%).
        self.assertEqual(20.0, metrics["close_1d_pct"])
        self.assertTrue(metrics["close_hit"])
        # The overnight gap keeps the original raw open / raw close basis.
        self.assertEqual(5.0, metrics["gap_open_pct"])
