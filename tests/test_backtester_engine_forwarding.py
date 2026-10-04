from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from app.services.backtester import BacktestRunner
from app.services.backtesting.engine import EventDrivenDailyEngine
from app.services.backtesting.schemas import DailyBar, EngineConfig, SignalCandidate


def _bar(ticker: str, trade_date: str, price: float = 10.0, volume: float = 1_000_000.0) -> DailyBar:
    return DailyBar(ticker, trade_date, price, price, price, price, volume)


class EngineParamForwardingTests(TestCase):
    def test_backtester_forwards_risk_limits_to_event_engine(self) -> None:
        captured: dict = {}

        def fake_run(self, **kwargs):
            captured.update(kwargs)
            return 7

        with patch("app.services.backtesting.runner.EventDrivenBacktestRunner.run", fake_run):
            result = BacktestRunner().run(
                top_n=3,
                holding_days=5,
                max_sector_weight=0.2,
                max_gross_exposure=0.8,
                max_participation_rate=0.05,
                engine_version="event_driven_daily_v2",
            )

        self.assertEqual(7, result)
        self.assertAlmostEqual(0.2, captured["max_sector_weight"])
        self.assertAlmostEqual(0.8, captured["max_gross_exposure"])
        self.assertAlmostEqual(0.05, captured["max_participation_rate"])

    def test_engine_enforces_sector_cap_when_set(self) -> None:
        engine = EventDrivenDailyEngine(
            EngineConfig(
                market="US",
                top_n=2,
                holding_days=2,
                initial_cash=100_000.0,
                commission_bps=0.0,
                slippage_bps=0.0,
                max_position_weight=0.5,
                max_sector_weight=0.2,
            )
        )
        result = engine.run(
            bars=[
                _bar("AAA", "2026-01-05"),
                _bar("BBB", "2026-01-05"),
                _bar("AAA", "2026-01-06"),
                _bar("BBB", "2026-01-06"),
                _bar("AAA", "2026-01-07"),
                _bar("BBB", "2026-01-07"),
            ],
            signals=[
                SignalCandidate("2026-01-05", "AAA", 1.0, rank_value=1.0, sector="tech"),
                SignalCandidate("2026-01-05", "BBB", 0.9, rank_value=2.0, sector="tech"),
            ],
            calendar_sessions=["2026-01-05", "2026-01-06", "2026-01-07"],
        )
        # The second same-sector candidate has no remaining industry room.
        self.assertEqual(["AAA"], [fill["ticker"] for fill in result.fills if fill["side"] == "buy"])
        self.assertGreaterEqual(result.gate_stats.get("insufficient_cash_or_lot_size", 0), 1)
