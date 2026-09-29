from datetime import date, timedelta
from unittest import TestCase

from app.services.model_evaluation import summarize_executable_labels, summarize_evaluation_samples
from app.services.stock_selection.executable_outcomes import ExecutionEligibility, confirmed_outcome
from app.services.stock_selection.labels import PriceBar
from app.services.trainer import SignalTrainer


class ConfirmedOutcomeTests(TestCase):
    def setUp(self):
        self.dates = [date(2026, 9, 7) + timedelta(days=i) for i in range(5)]
        # Abstract trading-session calendar: do not infer exchange calendars from weekdays.
        self.dates.append(date(2026, 9, 14))
        self.bars = [PriceBar(day, op, max(op, close, 110), min(op, close, 95), close, 1000)
                     for day, op, close in zip(self.dates, (99, 100, 106, 107, 103, 98), (99, 105, 108, 107, 101, 96))]
        self.kwargs = dict(signal_date=self.dates[0], trading_dates=self.dates, horizon_days=5,
                           market="CN", cost_bps=40, eligibility=ExecutionEligibility(True, True))

    def test_same_golden_loss_in_trainer_and_evaluator_with_one_cost_charge(self):
        target = SignalTrainer.executable_training_target(self.bars, **self.kwargs)
        label = confirmed_outcome(self.bars, **self.kwargs)
        result = summarize_executable_labels([label], horizon_days=5, cost_bps=40)
        self.assertAlmostEqual(-0.044, target["target"])
        self.assertAlmostEqual(-4.4, result["avg_return"])
        self.assertAlmostEqual(target["target"] * 100, result["avg_return"])
        self.assertEqual(0, result["hit_rate"])
        self.assertEqual(self.dates[-1].isoformat(), target["label_available_date"])
        self.assertEqual(
            "cvar95_tail_gated_v2 (mean of worst >=5 samples; worst sample kept as record)",
            result["drawdown_semantics"],
        )
        self.assertEqual("iid_normal_diagnostic_not_promotion_evidence", result["confidence_method"])

    def test_unfilled_or_deferred_decisions_stay_in_denominator_but_not_hit_rate(self):
        for eligibility, reason in ((ExecutionEligibility(False, True), "entry_not_executable"),
                                    (ExecutionEligibility(None, True), "entry_unknown"),
                                    (ExecutionEligibility(True, False), "exit_deferred"),
                                    (ExecutionEligibility(True, None), "exit_unknown")):
            with self.subTest(reason=reason):
                args = {**self.kwargs, "eligibility": eligibility}
                target = SignalTrainer.executable_training_target(self.bars, **args)
                label = confirmed_outcome(self.bars, **args)
                result = summarize_executable_labels([label], horizon_days=5, cost_bps=40)
                self.assertIsNone(target["target"])
                self.assertEqual(1, result["selected_sample_count"])
                self.assertEqual(1, result["unmeasured_sample_count"])
                self.assertIsNone(result["hit_rate"])
                self.assertEqual({reason: 1}, result["excluded_reasons"])

    def test_unknown_price_path_never_skips_a_session(self):
        with self.assertRaisesRegex(ValueError, "missing_price_path"):
            confirmed_outcome(self.bars[:2] + self.bars[3:], **self.kwargs)

    def test_immature_target_is_not_filled_with_zero(self):
        result = SignalTrainer.executable_training_target(self.bars[:-1], **{
            **self.kwargs, "trading_dates": self.dates[:-1],
        })
        self.assertIsNone(result["target"])
        self.assertEqual("label_not_mature", result["exclusion_reason"])

    def test_cn_same_day_exit_rejected_without_changing_us(self):
        with self.assertRaisesRegex(ValueError, "market execution"):
            confirmed_outcome(self.bars, **{**self.kwargs, "horizon_days": 1})
        label = confirmed_outcome(self.bars, **{**self.kwargs, "horizon_days": 1, "market": "US"})
        self.assertEqual(label.entry_date, label.exit_date)

    def test_industry_target_requires_explicit_same_window_baseline(self):
        with self.assertRaisesRegex(ValueError, "industry return required"):
            SignalTrainer.executable_training_target(self.bars, **self.kwargs, target_mode="industry_excess_return")
        result = SignalTrainer.executable_training_target(self.bars, **self.kwargs,
            target_mode="industry_excess_return", industry_return=0.02)
        self.assertAlmostEqual(-0.064, result["target"])

    def test_mixed_cost_or_horizon_cannot_enter_evaluation(self):
        label = confirmed_outcome(self.bars, **self.kwargs)
        for horizon, cost in ((3, 40), (5, 20)):
            with self.assertRaisesRegex(ValueError, "mixed executable"):
                summarize_executable_labels([label], horizon_days=horizon, cost_bps=cost)

    def test_nonfinite_prices_and_costs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            PriceBar(self.dates[0], 100, float("nan"), 99, 100)
        for cost in (float("nan"), float("inf"), -20):
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                confirmed_outcome(self.bars, **{**self.kwargs, "cost_bps": cost})

    def test_excluded_or_nonfinite_legacy_samples_cannot_improve_results(self):
        result = summarize_evaluation_samples([
            {"gross_return_pct": 100, "excluded_reason": "no_fill"},
            {"gross_return_pct": float("inf")}, {"gross_return_pct": -4},
        ], horizon_days=5, round_trip_cost_bps=40)
        self.assertEqual(3, result["selected_sample_count"])
        self.assertEqual(1, result["sample_count"])
        self.assertAlmostEqual(-4.4, result["avg_return"])
