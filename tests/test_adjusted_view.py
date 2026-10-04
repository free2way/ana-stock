from __future__ import annotations

from datetime import date
from unittest import TestCase

from app.services.adjusted_view import (
    AdjustmentError,
    adjustment_version,
    build_adjusted_series,
    raw_series_digest,
)
from app.services.corporate_actions import CorporateActionRecord


def _bar(day: str, close: float, *, open_price: float | None = None) -> dict:
    price = open_price if open_price is not None else close
    return {
        "date": day,
        "symbol": "AAA",
        "open": price,
        "high": max(price, close),
        "low": min(price, close),
        "close": close,
        "volume": 1000.0,
    }


def _split(day: str, factor: float) -> CorporateActionRecord:
    return CorporateActionRecord(
        market="US", symbol="AAA", action_type="split",
        effective_date=date.fromisoformat(day), factor=factor,
    )


class AdjustedViewTests(TestCase):
    def test_qfq_split_makes_the_series_continuous(self) -> None:
        bars = [_bar("2026-01-02", 100.0), _bar("2026-01-05", 102.0), _bar("2026-01-06", 51.0), _bar("2026-01-07", 52.0)]
        adjusted, stats = build_adjusted_series(bars, [_split("2026-01-06", 2.0)], method="qfq")
        self.assertEqual(1, stats.events_applied)
        closes = {item.trade_date.isoformat(): round(item.close, 6) for item in adjusted}
        self.assertEqual(50.0, closes["2026-01-02"])
        self.assertEqual(51.0, closes["2026-01-05"])
        self.assertEqual(51.0, closes["2026-01-06"])
        self.assertEqual(52.0, closes["2026-01-07"])
        # The latest session must never be rescaled in a qfq view.
        self.assertEqual(1.0, adjusted[-1].multiplier)

    def test_hfq_split_scales_post_split_prices_up(self) -> None:
        bars = [_bar("2026-01-05", 100.0), _bar("2026-01-06", 51.0)]
        adjusted, _ = build_adjusted_series(bars, [_split("2026-01-06", 2.0)], method="hfq")
        closes = [round(item.close, 6) for item in adjusted]
        self.assertEqual([100.0, 102.0], closes)

    def test_cash_dividend_uses_previous_close(self) -> None:
        bars = [_bar("2026-06-19", 10.0), _bar("2026-06-22", 9.5)]
        action = CorporateActionRecord(
            market="CN", symbol="AAA", action_type="cash_dividend",
            effective_date=date(2026, 6, 22), cash_amount=0.5,
        )
        adjusted, stats = build_adjusted_series(bars, [action], method="qfq")
        self.assertEqual(1, stats.events_applied)
        self.assertEqual(9.5, round(adjusted[0].close, 6))
        self.assertEqual(9.5, round(adjusted[1].close, 6))

    def test_event_without_trading_date_is_skipped_and_counted(self) -> None:
        bars = [_bar("2026-01-05", 100.0), _bar("2026-01-06", 100.0)]
        adjusted, stats = build_adjusted_series(bars, [_split("2026-01-07", 2.0)], method="qfq")
        self.assertEqual(1, stats.events_skipped_no_trade_date)
        self.assertEqual(0, stats.events_applied)
        self.assertEqual([100.0, 100.0], [item.close for item in adjusted])

    def test_unusable_dividend_without_previous_close_is_skipped(self) -> None:
        bars = [_bar("2026-06-22", 9.5), _bar("2026-06-23", 9.6)]
        action = CorporateActionRecord(
            market="CN", symbol="AAA", action_type="cash_dividend",
            effective_date=date(2026, 6, 22), cash_amount=0.5,
        )
        _, stats = build_adjusted_series(bars, [action], method="qfq")
        self.assertEqual(1, stats.events_skipped_unusable)

    def test_version_pins_method_raw_digest_and_actions(self) -> None:
        bars = [_bar("2026-01-05", 100.0), _bar("2026-01-06", 51.0)]
        action = _split("2026-01-06", 2.0)
        digest = raw_series_digest(bars)
        first = adjustment_version(method="qfq", raw_digest=digest, actions=[action])
        second = adjustment_version(method="qfq", raw_digest=digest, actions=[action])
        self.assertEqual(first, second)
        changed = adjustment_version(method="qfq", raw_digest=digest, actions=[_split("2026-01-06", 3.0)])
        self.assertNotEqual(first, changed)
        self.assertNotEqual(
            first,
            adjustment_version(method="hfq", raw_digest=digest, actions=[action]),
        )

    def test_invalid_method_and_unsorted_bars_are_rejected(self) -> None:
        bars = [_bar("2026-01-05", 100.0)]
        with self.assertRaisesRegex(AdjustmentError, "method"):
            build_adjusted_series(bars, [], method="mystery")
        with self.assertRaisesRegex(AdjustmentError, "ascending"):
            build_adjusted_series([bars[0], _bar("2026-01-04", 99.0)], [], method="qfq")
