"""Five-session path metric for the executable label profile.

The executable (``executable_net_return_v1``) label profile shipped the 20-day
path metrics but no 5-day key, so the insight page's expected 5-day return was
always ``-``. This mirrors the mature 20-day approach additively: the label's
own target value and the 1/3-day semantics are untouched.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.trainer import SignalTrainer


def _session_rows(symbol: str, sessions: int, *, start: date = date(2026, 1, 5)) -> list[dict]:
    rows: list[dict] = []
    for index in range(sessions):
        rows.append(
            {
                "symbol": symbol,
                "date": (start + timedelta(days=index)).isoformat(),
                "open": 10.0,
                "high": 10.5,
                "low": 9.5,
                "close": 10.0,
                "volume": 1_000_000.0,
            }
        )
    return rows


class FiveDayPathMetricsTests(TestCase):
    def test_helper_returns_none_for_short_window(self) -> None:
        trainer = SignalTrainer()
        rows = _session_rows("SHORT", 4)
        self.assertEqual(
            {"next_5d_close_return": None},
            trainer._future_path_metrics_5d(future_rows=rows, anchor_close=10.0),
        )

    def test_helper_returns_percent_close_return(self) -> None:
        trainer = SignalTrainer()
        rows = _session_rows("FIVE", 5)
        rows[-1]["close"] = 11.0
        self.assertEqual(
            {"next_5d_close_return": 10.0},
            trainer._future_path_metrics_5d(future_rows=rows, anchor_close=10.0),
        )

    def test_executable_profile_carries_five_day_return_without_changing_target(self) -> None:
        rows = _session_rows("SIX", 6)
        closes = (10.0, 10.5, 11.0, 10.5, 10.0, 11.0)
        for row, close in zip(rows, closes, strict=True):
            row["close"] = close
            row["high"] = close + 0.5
            row["low"] = 9.5
        trainer = SignalTrainer()
        target, profile, version = trainer._build_executable_net_return_target(
            symbol_rows=rows, index=0, horizon_days=5, market="CN", limit_band_pct=10.0
        )
        self.assertEqual("confirmed_next_open_fixed_exit_fill_cost_v2", version)
        self.assertIsNotNone(target)
        # Additive: the label's own net return is unchanged by the new key.
        self.assertAlmostEqual(target, profile["net_return"], places=12)
        # Anchor 10.0 -> 5th future close 11.0 == +10%.
        self.assertEqual(10.0, profile["next_5d_close_return"])
        # A 6-row window cannot fake a 20-day estimate.
        self.assertIsNone(profile["next_20d_close_return"])

    def test_detail_row_publishes_expected_return_5d_from_executable_samples(self) -> None:
        rows = _session_rows("SIX", 6)
        closes = (10.0, 10.5, 11.0, 10.5, 10.0, 11.0)
        for row, close in zip(rows, closes, strict=True):
            row["close"] = close
            row["high"] = close + 0.5
            row["low"] = 9.5
        trainer = SignalTrainer()
        _target, profile, _version = trainer._build_executable_net_return_target(
            symbol_rows=rows, index=0, horizon_days=5, market="CN", limit_band_pct=10.0
        )
        summary = trainer._summarize_target_profile([{"target_profile": profile}])
        # Coverage: the executable profile now exposes the 5-day key.
        self.assertIn("next_5d_close_return_avg", summary)
        self.assertEqual(1, summary["sample_count"])
        detail = trainer._build_detail_row(
            symbol_id=1,
            trade_date="2026-01-05",
            score=0.004,
            rank_value=1,
            universe_size=100,
            horizon_days=5,
            run_name="fixture",
            calibrated_metrics=summary,
        )
        self.assertEqual(10.0, detail["expected_return_5d"])
