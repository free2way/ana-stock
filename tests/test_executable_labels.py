from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.stock_selection.labels import PriceBar, build_executable_label


def _bar(day: date, *, open_price: float, close: float, low: float, high: float, volume: float = 1_000.0) -> PriceBar:
    return PriceBar(
        trade_date=day,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


class ExecutableLabelTests(TestCase):
    def test_uses_next_open_exit_close_cost_and_path_drawdown(self) -> None:
        start = date(2026, 8, 10)
        bars = [
            _bar(start, open_price=99.0, close=100.0, low=98.0, high=101.0),
            _bar(start + timedelta(days=1), open_price=102.0, close=103.0, low=99.0, high=104.0),
            _bar(start + timedelta(days=2), open_price=103.0, close=106.0, low=101.0, high=107.0),
            _bar(start + timedelta(days=3), open_price=106.0, close=108.0, low=104.0, high=109.0),
        ]
        label = build_executable_label(
            bars,
            signal_index=0,
            horizon_days=3,
            round_trip_cost_bps=20.0,
            market_return=0.01,
            industry_return=0.015,
            drawdown_penalty=0.25,
        )

        gross = (108.0 / 102.0) - 1.0
        net = gross - 0.002
        drawdown = (99.0 / 102.0) - 1.0
        self.assertEqual(start + timedelta(days=1), label.entry_date)
        self.assertEqual(start + timedelta(days=3), label.exit_date)
        self.assertEqual(label.exit_date, label.label_available_date)
        self.assertAlmostEqual(gross, label.gross_return)
        self.assertAlmostEqual(net, label.net_return)
        self.assertAlmostEqual(net - 0.01, label.market_excess_return)
        self.assertAlmostEqual(net - 0.015, label.industry_excess_return)
        self.assertAlmostEqual(drawdown, label.path_drawdown)
        self.assertAlmostEqual((net - 0.015) - 0.25 * abs(drawdown), label.risk_adjusted_return)
        self.assertTrue(label.is_profitable)

    def test_non_executable_entry_stays_explicit(self) -> None:
        start = date(2026, 8, 10)
        bars = [
            _bar(start, open_price=10.0, close=10.0, low=9.9, high=10.1),
            _bar(start + timedelta(days=1), open_price=11.0, close=11.0, low=11.0, high=11.0, volume=0.0),
        ]
        label = build_executable_label(
            bars,
            signal_index=0,
            horizon_days=1,
            entry_is_executable=False,
            exclusion_reason="limit_up_no_fill",
        )
        self.assertFalse(label.tradable)
        self.assertEqual("limit_up_no_fill", label.exclusion_reason)
        self.assertIsNone(label.risk_adjusted_return)
        self.assertIsNone(label.net_return)
        self.assertIsNone(label.is_profitable)

    def test_golden_loss_uses_exit_not_intraperiod_high(self) -> None:
        start = date(2026, 8, 10)
        bars = [
            _bar(start, open_price=99.0, close=99.0, low=98.0, high=100.0),
            _bar(start + timedelta(days=1), open_price=100.0, close=105.0, low=99.0, high=110.0),
            _bar(start + timedelta(days=2), open_price=106.0, close=108.0, low=104.0, high=110.0),
            _bar(start + timedelta(days=3), open_price=107.0, close=107.0, low=103.0, high=109.0),
            _bar(start + timedelta(days=4), open_price=103.0, close=101.0, low=99.0, high=104.0),
            _bar(start + timedelta(days=5), open_price=98.0, close=96.0, low=95.0, high=100.0),
        ]

        label = build_executable_label(
            bars,
            signal_index=0,
            horizon_days=5,
            round_trip_cost_bps=40.0,
            drawdown_penalty=0.0,
        )

        self.assertAlmostEqual(-0.044, label.net_return or 0.0, places=12)
        self.assertFalse(label.is_profitable)

    def test_reverse_split_like_path_is_excluded(self) -> None:
        start = date(2026, 8, 10)
        bars = [
            _bar(start, open_price=0.05, close=0.05, low=0.04, high=0.06),
            _bar(start + timedelta(days=1), open_price=0.05, close=0.05, low=0.04, high=0.06),
            _bar(start + timedelta(days=2), open_price=4.0, close=4.0, low=3.8, high=4.2),
        ]
        label = build_executable_label(bars, signal_index=0, horizon_days=2)
        self.assertFalse(label.tradable)
        self.assertEqual("suspected_corporate_action_discontinuity", label.exclusion_reason)

    def test_requires_full_future_horizon(self) -> None:
        start = date(2026, 8, 10)
        bars = [
            _bar(start, open_price=10.0, close=10.0, low=9.0, high=11.0),
            _bar(start + timedelta(days=1), open_price=10.0, close=10.0, low=9.0, high=11.0),
        ]
        with self.assertRaisesRegex(ValueError, "insufficient future bars"):
            build_executable_label(bars, signal_index=0, horizon_days=3)
