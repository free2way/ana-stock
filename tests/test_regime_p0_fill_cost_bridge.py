from dataclasses import replace
from datetime import date
from unittest import TestCase

from app.services.backtesting import DailyBar, EngineConfig, EventDrivenDailyEngine, SignalCandidate
from app.services.execution_costs import FillCostModel
from app.services.model_evaluation import summarize_executable_labels
from app.services.stock_selection.executable_outcomes import (
    ExecutionEligibility, confirmed_outcome, FILL_COST_OUTCOME_VERSION,
)
from app.services.stock_selection.labels import PriceBar
from app.services.trainer import SignalTrainer


class FillCostBridgeTests(TestCase):
    def setUp(self):
        self.dates = [date.fromisoformat(day) for day in (
            "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15")]
        self.bars = [PriceBar(day, op, max(op, close) + 1, min(op, close) - 1, close, 1_000_000)
                     for day, op, close in zip(self.dates, (99, 100, 106, 107, 103, 98), (99, 105, 108, 107, 101, 96))]
        self.cost = FillCostModel(8, 12)
        self.kwargs = dict(signal_date=self.dates[0], trading_dates=self.dates, horizon_days=5,
                           market="CN", cost_model=self.cost, eligibility=ExecutionEligibility(True, True))

    def engine(self, market="CN", *, zero_volume=False, blocked_exit=False):
        ticker = "000001.SZ" if market == "CN" else "AAA"
        return EventDrivenDailyEngine(EngineConfig(
            market=market, holding_days=5, top_n=1, initial_cash=100_000,
            commission_bps=8, slippage_bps=12, max_position_weight=1,
        )).run(bars=[DailyBar(ticker, bar.trade_date.isoformat(), bar.open, bar.high, bar.low,
                             bar.close, 0 if (zero_volume and i == 1) or (blocked_exit and i == 5) else bar.volume)
                     for i, bar in enumerate(self.bars)],
               signals=[SignalCandidate(self.dates[0].isoformat(), ticker, 1)],
               calendar_sessions=[day.isoformat() for day in self.dates])

    def test_trainer_evaluator_and_cn_us_account_have_same_net_return(self):
        for market in ("CN", "US"):
            with self.subTest(market=market):
                args = {**self.kwargs, "market": market}
                target = SignalTrainer.executable_training_target(self.bars, **args)
                label = confirmed_outcome(self.bars, **args)
                summary = summarize_executable_labels([label], horizon_days=5, cost_model=self.cost)
                result = self.engine(market)
                self.assertEqual(1, len(result.outcomes))
                actual = result.outcomes[0]
                self.assertEqual("matured", actual["status"])
                self.assertAlmostEqual(actual["net_return"], target["target"], places=12)
                self.assertAlmostEqual(actual["net_return"] * 100, summary["avg_return"], places=10)
                self.assertAlmostEqual(-4, summary["gross_avg_return"])
                self.assertEqual(0, summary["hit_rate"])
                self.assertEqual(FILL_COST_OUTCOME_VERSION, target["label_version"])
                self.assertEqual(self.cost.model_hash, target["cost_model_hash"])
                self.assertEqual(self.cost.model_hash, result.cost_model_metadata["hash"])
                self.assertEqual("entry_notional_plus_entry_fee", summary["return_denominator"])
                self.assertAlmostEqual(result.initial_cash + actual["net_pnl"], result.end_nav, places=8)

    def test_per_fill_notional_fee_slippage_and_cash_reconcile(self):
        result = self.engine()
        buy, sell = result.fills
        expected = self.cost.round_trip(100, 96, quantity=buy["quantity"])
        for actual, side in ((buy, "entry"), (sell, "exit")):
            for field in ("fill_price", "notional", "fee", "slippage"):
                self.assertAlmostEqual(expected[side][field], actual[field], places=10)
        self.assertAlmostEqual(expected["net_pnl"], result.outcomes[0]["net_pnl"], places=9)
        self.assertAlmostEqual(buy["fee"] + sell["fee"], result.cumulative_fees)
        self.assertAlmostEqual(buy["slippage"] + sell["slippage"], result.cumulative_slippage)

    def test_flat_legacy_result_stays_distinct_from_per_fill_net(self):
        flat_args = {**self.kwargs, "cost_model": None, "cost_bps": 40}
        flat = confirmed_outcome(self.bars, **flat_args)
        per_fill = confirmed_outcome(self.bars, **self.kwargs)
        self.assertAlmostEqual(-0.044, flat.net_return)
        expected = 96 * (1 - .0012) * (1 - .0008) / (100 * (1 + .0012) * (1 + .0008)) - 1
        self.assertAlmostEqual(expected, per_fill.net_return)
        self.assertNotAlmostEqual(flat.net_return, per_fill.net_return, places=6)
        self.assertEqual(40, per_fill.round_trip_cost_bps)  # Nominal only; never deducted again.

    def test_same_nominal_bps_does_not_mean_same_contract(self):
        other = FillCostModel(20, 0)
        self.assertEqual(self.cost.nominal_round_trip_bps, other.nominal_round_trip_bps)
        self.assertNotEqual(self.cost.model_hash, other.model_hash)
        label = confirmed_outcome(self.bars, **self.kwargs)
        with self.assertRaisesRegex(ValueError, "mixed executable"):
            summarize_executable_labels([label], horizon_days=5, cost_model=other)

    def test_mixed_versions_or_cost_inputs_are_rejected(self):
        label = confirmed_outcome(self.bars, **self.kwargs)
        with self.assertRaises(ValueError):
            confirmed_outcome(self.bars, **self.kwargs, cost_bps=40)
        with self.assertRaises(ValueError):
            summarize_executable_labels([label], horizon_days=5, cost_bps=40)
        with self.assertRaises(ValueError):
            summarize_executable_labels([label], horizon_days=5, cost_model=self.cost, cost_bps=40)
        with self.assertRaises(ValueError):
            summarize_executable_labels([], horizon_days=5)

    def test_no_fill_charges_nothing_and_stays_in_selected_denominator(self):
        result = self.engine(zero_volume=True)
        self.assertEqual(0, len(result.fills))
        self.assertEqual(0, result.cumulative_fees)
        self.assertEqual(result.initial_cash, result.end_nav)
        label = confirmed_outcome(self.bars, **{**self.kwargs, "eligibility": ExecutionEligibility(False, False, "no_volume")})
        summary = summarize_executable_labels([label], horizon_days=5, cost_model=self.cost)
        self.assertIsNone(label.net_return)
        self.assertEqual(1, summary["selected_sample_count"])
        self.assertEqual(1, summary["unmeasured_sample_count"])
        self.assertIsNone(summary["hit_rate"])
        self.assertEqual({"no_volume": 1}, summary["excluded_reasons"])

    def test_deferred_exit_keeps_unmeasured_label_and_cost_identity(self):
        account = self.engine(blocked_exit=True)
        self.assertEqual(1, len(account.fills))
        self.assertEqual(1, account.open_position_count)
        self.assertEqual("EXIT_DEFERRED", account.outcomes[0]["status"])
        self.assertIsNone(account.outcomes[0]["net_return"])
        self.assertAlmostEqual(account.fills[0]["fee"], account.cumulative_fees)
        state = account.portfolio_states[-1]
        self.assertAlmostEqual(state["cash"] + state["position_market_value"], state["nav"])
        label = confirmed_outcome(self.bars, **{**self.kwargs, "eligibility": ExecutionEligibility(True, False)})
        self.assertIsNone(label.net_return)
        self.assertEqual("exit_deferred", label.exclusion_reason)
        self.assertEqual(self.cost.model_hash, label.cost_model_hash)

    def test_corrupt_net_label_cannot_silently_disappear(self):
        label = confirmed_outcome(self.bars, **self.kwargs)
        with self.assertRaisesRegex(ValueError, "finite gross and net"):
            summarize_executable_labels([replace(label, net_return=float("nan"))], horizon_days=5, cost_model=self.cost)

    def test_higher_cost_reduces_net_but_not_raw_gross(self):
        values = [FillCostModel(c, s).round_trip(100, 96) for c, s in ((0, 0), (8, 12), (16, 24))]
        self.assertGreater(values[0]["net_return"], values[1]["net_return"])
        self.assertGreater(values[1]["net_return"], values[2]["net_return"])
        self.assertEqual(1, len({value["gross_return"] for value in values}))

    def test_invalid_costs_prices_and_quantities_are_rejected(self):
        for value in (float("nan"), float("inf"), -1, 10_000, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FillCostModel(value, 0)
            with self.subTest(value=value), self.assertRaises(ValueError):
                EngineConfig(market="CN", slippage_bps=value)
        for price, quantity, side in ((0, 1, "buy"), (100, 0, "buy"), (100, 1, "unknown"), (100, float("nan"), "sell")):
            with self.assertRaises(ValueError):
                self.cost.fill(price, quantity, side=side)

    def test_cost_identity_is_stable_for_integer_and_float_config(self):
        self.assertEqual(FillCostModel(8, 12).model_hash, FillCostModel(8.0, 12.0).model_hash)

    def test_proportional_cost_returns_are_quantity_invariant(self):
        small = self.cost.round_trip(100, 96, quantity=1)
        large = self.cost.round_trip(100, 96, quantity=300)
        self.assertAlmostEqual(small["net_return"], large["net_return"], places=12)
        self.assertAlmostEqual(small["net_pnl"] * 300, large["net_pnl"], places=8)

    def test_industry_excess_is_based_on_cash_net_not_nominal_flat_cost(self):
        target = SignalTrainer.executable_training_target(self.bars, **self.kwargs,
            target_mode="industry_excess_return", industry_return=0.02)
        self.assertAlmostEqual(self.cost.round_trip(100, 96)["net_return"] - .02, target["target"])
