from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.backtesting import (
    DailyBar,
    EngineConfig,
    EventDrivenDailyEngine,
    MarketCorporateAction,
    SignalCandidate,
)


def _bar(ticker: str, trade_date: str, *, open_price: float, close: float, volume: float = 100_000.0) -> DailyBar:
    return DailyBar(
        ticker=ticker,
        trade_date=trade_date,
        open=open_price,
        high=max(open_price, close),
        low=min(open_price, close),
        close=close,
        volume=volume,
    )


class EventDrivenBacktestTests(unittest.TestCase):
    def test_public_runner_routes_explicit_v2_without_touching_legacy_engine(self) -> None:
        from app.services.backtester import BacktestRunner

        with patch(
            "app.services.backtesting.runner.EventDrivenBacktestRunner.run",
            return_value=7,
        ) as mocked_run:
            count = BacktestRunner().run(
                top_n=3,
                model_run_id=42,
                holding_days=5,
                commission_bps=8.0,
                slippage_bps=12.0,
                max_position_weight=0.2,
                min_signal_score=0.05,
                min_adv=50_000_000.0,
                max_gap_pct=0.08,
                engine_version="event_driven_daily_v2",
            )

        self.assertEqual(7, count)
        self.assertEqual(3, mocked_run.call_args.kwargs["top_n"])
        self.assertEqual(42, mocked_run.call_args.kwargs["model_run_id"])

    def test_public_runner_rejects_unknown_engine_version(self) -> None:
        from app.services.backtester import BacktestRunner

        with self.assertRaisesRegex(ValueError, "engine_version"):
            BacktestRunner().run(engine_version="unknown_engine")

    def test_legacy_config_is_explicitly_versioned(self) -> None:
        from app.services.backtester import BacktestRunner

        config = BacktestRunner()._run_config(
            top_n=1,
            model_run_id=42,
            holding_days=3,
            commission_bps=8.0,
            slippage_bps=12.0,
            max_position_weight=0.2,
            min_signal_score=0.05,
            benchmark_symbol="UNIVERSE_EQUAL_WEIGHT",
            max_sector_weight=0.35,
            min_adv=50_000_000.0,
            max_gap_pct=0.08,
            rebalance_threshold=0.02,
        )

        self.assertEqual("legacy_forward_return_v1", config["engine_version"])
        self.assertTrue(config["legacy"])
        self.assertEqual("forward_label", config["holding_period_basis"])

    def test_signal_uses_next_session_open_and_close_exit(self) -> None:
        engine = EventDrivenDailyEngine(
            EngineConfig(
                market="CN",
                top_n=1,
                holding_days=1,
                initial_cash=10_000.0,
                commission_bps=0.0,
                slippage_bps=0.0,
                max_position_weight=1.0,
            )
        )
        result = engine.run(
            bars=[
                _bar("000001.SZ", "2026-01-05", open_price=9.8, close=10.0),
                _bar("000001.SZ", "2026-01-06", open_price=10.5, close=12.0),
            ],
            signals=[SignalCandidate("2026-01-05", "000001.SZ", score=1.0, rank_value=1.0)],
        )

        self.assertEqual(2, len(result.fills))
        self.assertEqual("2026-01-06", result.fills[0]["fill_date"])
        self.assertEqual("buy", result.fills[0]["side"])
        self.assertEqual(10.5, result.fills[0]["fill_price"])
        self.assertEqual(12.0, result.fills[1]["fill_price"])
        self.assertEqual(11_350.0, result.end_nav)
        self.assertEqual(1.135, result.metrics[-1]["nav"])
        self.assertEqual(0, result.open_position_count)

    def test_costs_are_charged_only_on_actual_fills_and_accounting_balances(self) -> None:
        engine = EventDrivenDailyEngine(
            EngineConfig(
                market="US",
                top_n=1,
                holding_days=1,
                initial_cash=10_000.0,
                commission_bps=10.0,
                slippage_bps=10.0,
                max_position_weight=1.0,
            )
        )
        result = engine.run(
            bars=[
                _bar("AAA", "2026-01-05", open_price=10.0, close=10.0),
                _bar("AAA", "2026-01-06", open_price=10.0, close=10.0),
            ],
            signals=[SignalCandidate("2026-01-05", "AAA", score=1.0)],
        )

        self.assertAlmostEqual(9_960.08, result.end_nav, places=6)
        self.assertAlmostEqual(19.96, result.cumulative_fees, places=6)
        self.assertAlmostEqual(19.96, result.cumulative_slippage, places=6)
        final_state = result.portfolio_states[-1]
        self.assertAlmostEqual(final_state["nav"], final_state["cash"] + final_state["position_market_value"])

    def test_cn_limit_up_and_zero_volume_entries_are_rejected_without_cost(self) -> None:
        engine = EventDrivenDailyEngine(
            EngineConfig(
                market="CN",
                top_n=2,
                holding_days=1,
                initial_cash=100_000.0,
                commission_bps=8.0,
                slippage_bps=12.0,
                max_position_weight=0.5,
            )
        )
        result = engine.run(
            bars=[
                _bar("000001.SZ", "2026-01-05", open_price=10.0, close=10.0),
                _bar("000002.SZ", "2026-01-05", open_price=10.0, close=10.0),
                _bar("000001.SZ", "2026-01-06", open_price=11.0, close=11.0),
                _bar("000002.SZ", "2026-01-06", open_price=10.0, close=10.0, volume=0.0),
            ],
            signals=[
                SignalCandidate("2026-01-05", "000001.SZ", score=1.0, rank_value=1.0),
                SignalCandidate("2026-01-05", "000002.SZ", score=0.9, rank_value=2.0),
            ],
        )

        self.assertEqual(0, len(result.fills))
        self.assertEqual(2, len(result.rejects))
        self.assertEqual(1, result.gate_stats["cn_limit_up_buy_blocked"])
        self.assertEqual(1, result.gate_stats["suspended_or_no_volume"])
        self.assertEqual(100_000.0, result.end_nav)
        self.assertEqual(0.0, result.cumulative_fees)

    def test_holding_period_counts_market_sessions_and_supports_overlapping_sleeves(self) -> None:
        bars = []
        for ticker in ("AAA", "BBB"):
            for day in range(5, 9):
                bars.append(_bar(ticker, f"2026-01-{day:02d}", open_price=10.0, close=10.0))
        engine = EventDrivenDailyEngine(
            EngineConfig(
                market="US",
                top_n=1,
                holding_days=2,
                initial_cash=10_000.0,
                commission_bps=0.0,
                slippage_bps=0.0,
                max_position_weight=0.5,
            )
        )
        result = engine.run(
            bars=bars,
            signals=[
                SignalCandidate("2026-01-05", "AAA", score=1.0),
                SignalCandidate("2026-01-06", "BBB", score=1.0),
            ],
        )

        fills = [(item["ticker"], item["side"], item["fill_date"]) for item in result.fills]
        self.assertIn(("AAA", "buy", "2026-01-06"), fills)
        self.assertIn(("AAA", "sell", "2026-01-07"), fills)
        self.assertIn(("BBB", "buy", "2026-01-07"), fills)
        self.assertIn(("BBB", "sell", "2026-01-08"), fills)

    def test_cn_st_main_board_uses_five_percent_limit_but_chinext_keeps_twenty_percent(self) -> None:
        engine = EventDrivenDailyEngine(
            EngineConfig(
                market="CN",
                top_n=2,
                holding_days=1,
                initial_cash=100_000.0,
                commission_bps=0.0,
                slippage_bps=0.0,
                max_position_weight=0.5,
            )
        )
        result = engine.run(
            bars=[
                DailyBar("600001.SS", "2026-01-05", 10.0, 10.0, 10.0, 10.0, 100_000.0, is_st=True),
                DailyBar("300001.SZ", "2026-01-05", 10.0, 10.0, 10.0, 10.0, 100_000.0, is_st=True),
                DailyBar("600001.SS", "2026-01-06", 10.5, 10.5, 10.5, 10.5, 100_000.0, is_st=True),
                DailyBar("300001.SZ", "2026-01-06", 10.5, 10.5, 10.5, 10.5, 100_000.0, is_st=True),
            ],
            signals=[
                SignalCandidate("2026-01-05", "600001.SS", score=1.0, rank_value=1.0),
                SignalCandidate("2026-01-05", "300001.SZ", score=0.9, rank_value=2.0),
            ],
        )

        self.assertEqual(1, result.gate_stats["cn_limit_up_buy_blocked"])
        self.assertEqual(["300001.SZ", "300001.SZ"], [row["ticker"] for row in result.fills])

    def test_frozen_top_one_rejects_missing_bar_without_replacing_with_rank_two(self) -> None:
        engine = EventDrivenDailyEngine(EngineConfig(
            market="US", top_n=1, holding_days=1, initial_cash=10_000,
            max_position_weight=1, commission_bps=0, slippage_bps=0,
        ))
        result = engine.run(
            bars=[_bar("AAA", "2026-01-05", open_price=10, close=10),
                  _bar("BBB", "2026-01-05", open_price=10, close=10),
                  _bar("BBB", "2026-01-06", open_price=10, close=10)],
            signals=[SignalCandidate("2026-01-05", "AAA", 1.0, ordinal=1),
                     SignalCandidate("2026-01-05", "BBB", 0.9, ordinal=2)],
            calendar_sessions=["2026-01-05", "2026-01-06"],
        )
        self.assertEqual([], list(result.fills))
        self.assertEqual(["AAA"], [row["ticker"] for row in result.rejects])
        self.assertEqual(1, result.gate_stats["missing_market_bar"])

    def test_missing_quote_on_exit_defers_lot_and_does_not_shorten_horizon(self) -> None:
        engine = EventDrivenDailyEngine(EngineConfig(
            market="US", top_n=1, holding_days=2, initial_cash=10_000,
            max_position_weight=1, commission_bps=0, slippage_bps=0,
        ))
        bars = [_bar("AAA", "2026-01-05", open_price=10, close=10),
                _bar("AAA", "2026-01-06", open_price=10, close=10, volume=100_000)]
        result = engine.run(
            bars=bars, signals=[SignalCandidate("2026-01-05", "AAA", 1.0)],
            calendar_sessions=["2026-01-05", "2026-01-06", "2026-01-07"],
        )
        self.assertEqual(1, result.open_position_count)
        self.assertEqual("EXIT_DEFERRED", result.outcomes[0]["status"])
        self.assertEqual("2026-01-07", result.outcomes[0]["scheduled_exit_date"])
        self.assertEqual("2026-01-08", result.outcomes[0]["next_exit_attempt_date"])
        self.assertEqual(1, result.portfolio_states[-1]["stale_mark_lots"])
        resumed = engine.run(
            bars=bars + [_bar("AAA", "2026-01-08", open_price=10, close=11)],
            signals=[SignalCandidate("2026-01-05", "AAA", 1.0)],
            calendar_sessions=["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
        )
        self.assertEqual("2026-01-08", resumed.outcomes[0]["exit_date"])
        self.assertEqual("matured", resumed.outcomes[0]["status"])

    def test_split_and_cash_dividend_conserve_account_value(self) -> None:
        engine = EventDrivenDailyEngine(EngineConfig(
            market="US", top_n=1, holding_days=3, initial_cash=10_000,
            max_position_weight=1, commission_bps=0, slippage_bps=0,
        ))
        result = engine.run(
            bars=[_bar("AAA", "2026-01-05", open_price=100, close=100),
                  _bar("AAA", "2026-01-06", open_price=100, close=100),
                  _bar("AAA", "2026-01-07", open_price=50, close=50),
                  _bar("AAA", "2026-01-08", open_price=49, close=49)],
            signals=[SignalCandidate("2026-01-05", "AAA", 1.0)],
            corporate_actions=[MarketCorporateAction("AAA", "2026-01-07", "split", factor=2),
                               MarketCorporateAction("AAA", "2026-01-08", "cash_dividend", cash_amount=1)],
        )
        self.assertEqual(0, result.open_position_count)
        self.assertEqual(10_000, result.end_nav)
        self.assertEqual(0, result.outcomes[0]["net_pnl"])
        self.assertEqual(2, len(result.corporate_action_events))

    def test_explicit_calendar_controls_holding_sessions_even_with_no_bars_on_omitted_date(self) -> None:
        engine = EventDrivenDailyEngine(EngineConfig(
            market="US", top_n=1, holding_days=2, initial_cash=10_000,
            max_position_weight=1, commission_bps=0, slippage_bps=0,
        ))
        result = engine.run(
            bars=[_bar("AAA", "2026-01-05", open_price=10, close=10),
                  _bar("AAA", "2026-01-06", open_price=10, close=10),
                  _bar("AAA", "2026-01-08", open_price=10, close=11)],
            signals=[SignalCandidate("2026-01-05", "AAA", 1)],
            calendar_sessions=["2026-01-05", "2026-01-06", "2026-01-08"],
        )
        self.assertEqual("2026-01-08", result.outcomes[0]["exit_date"])

    def test_adjusted_prices_cannot_be_combined_with_raw_corporate_actions(self) -> None:
        engine = EventDrivenDailyEngine(EngineConfig(market="US"))
        with self.assertRaisesRegex(ValueError, "double count"):
            engine.run(
                bars=[DailyBar("AAA", "2026-01-05", 10, 10, 10, 10, 100, price_basis="adjusted"),
                      DailyBar("AAA", "2026-01-06", 10, 10, 10, 10, 100, price_basis="adjusted")],
                signals=[], corporate_actions=[MarketCorporateAction("AAA", "2026-01-06", "split", factor=2)],
            )

    def test_prior_adv_capacity_yields_partial_fill_without_spending_extra_cash(self) -> None:
        engine = EventDrivenDailyEngine(EngineConfig(
            market="US", top_n=1, holding_days=1, initial_cash=100_000,
            max_position_weight=1, max_participation_rate=0.01,
            commission_bps=0, slippage_bps=0,
        ))
        result = engine.run(
            bars=[_bar("AAA", "2026-01-05", open_price=10, close=10, volume=100_000),
                  _bar("AAA", "2026-01-06", open_price=10, close=10, volume=100_000)],
            signals=[SignalCandidate("2026-01-05", "AAA", 1)],
        )
        self.assertEqual("partial", result.fills[0]["fill_status"])
        self.assertEqual(1000, result.fills[0]["quantity"])
        self.assertEqual(9000, result.fills[0]["remaining_quantity"])
        self.assertEqual(100_000, result.end_nav)


if __name__ == "__main__":
    unittest.main()
