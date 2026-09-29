from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch
import subprocess
import sys

from app.services.ai_daily_report import _candidate_from_full_market_row
from app.services.screener import ScreenerService
from app.services.model_score_contract import first_present, ordering_strength, rank_ratio
from app.services.stock_selection.training_weights import date_balanced_training_weights
from app.services.tradability_filter import evaluate_candidate_tradability


class ScoreContractTests(TestCase):
    def test_score_contract_import_does_not_load_research_or_repository(self):
        subprocess.run([sys.executable, "-c", (
            "import sys; import app.services.model_score_contract; "
            "assert 'app.services.stock_selection' not in sys.modules; "
            "assert 'app.services.repository' not in sys.modules"
        )], check=True, capture_output=True, text=True)

    def test_legacy_percent_units_never_guessed_from_magnitude(self):
        for percent in (0, 0.5, 1, 50, 100):
            with self.subTest(percent=percent):
                self.assertEqual(percent / 100, rank_ratio({"model_percentile": percent}))
                self.assertEqual(percent / 100, rank_ratio({"rank_percentile": percent / 100}))

    def test_zero_has_priority_over_fallback(self):
        self.assertEqual(0, first_present({"score": 0, "model_score": 99}, "score", "model_score"))
        self.assertEqual(0, rank_ratio({"rank_percentile": 0, "model_percentile": 99}))

    def test_invalid_scores_are_blocked(self):
        for row in ({"rank_percentile": 1.01}, {"model_percentile": -1},
                    {"model_percentile": 101}, {"raw_score": float("nan")},
                    {"model_percentile": float("inf")}, {"score": True}):
            with self.subTest(row=row):
                result = evaluate_candidate_tradability({"latest_close": 10, **row})
                self.assertEqual("BLOCKED", result.tradability_status)
                self.assertEqual("invalid_score_contract", result.block_reason)

    def test_raw_or_legacy_confidence_does_not_imply_probability(self):
        for row in ({"score": 0.99}, {"model_confidence": 99}, {"bullish_prob": 0.99}):
            with self.subTest(row=row):
                self.assertEqual((None, None), ordering_strength(row))
                result = evaluate_candidate_tradability({"latest_close": 10, "signal_strength": 95, **row})
                self.assertEqual("REVIEW", result.tradability_status)

    def test_calibration_requires_explicit_identity_and_units(self):
        row = {"estimate_schema_version": "calibrated_estimates_v1", "probability_unit": "ratio",
               "estimate_protocol_id": "test-protocol", "calibration_version": "test-calibration",
               "calibrated_probability": 0.0}
        self.assertEqual((0.0, "calibrated_probability"), ordering_strength(row))
        for field in ("probability_unit", "estimate_protocol_id", "calibration_version"):
            self.assertEqual((None, None), ordering_strength({**row, field: None}))

    def test_sell_is_blocked_even_without_score(self):
        self.assertEqual("BLOCKED", evaluate_candidate_tradability({"signal_label": "SELL"}).tradability_status)

    def test_negative_rank_margin_is_not_a_sell_or_model_approval(self):
        result = evaluate_candidate_tradability({
            "raw_score": -2, "score_semantics": "rank_margin", "rank_percentile": 0.99,
            "latest_close": 10, "signal_strength": 90, "model_activation_status": "unverified",
        })
        self.assertEqual("DEFER", result.tradability_status)
        self.assertIsNone(result.block_reason)
        self.assertIn("model-observation-only", result.risk_flags)

    def test_zero_strength_is_not_replaced_by_trend(self):
        result = evaluate_candidate_tradability({
            "model_percentile": 99, "model_signal_strength": 0, "trend_score": 95, "latest_close": 10,
        })
        self.assertIn("weak-signal-strength", result.risk_flags)

    def test_screener_preserves_zero_and_clears_stale_derived_results(self):
        # All external reads are mocked: this test never connects to PostgreSQL.
        with patch("app.services.screener.SessionLocal"), patch(
            "app.services.screener.load_market_context_snapshot", return_value={}
        ):
            row = {"market": "CN", "score": 0, "model_score": 1, "model_percentile": 95,
                   "model_signal_strength": 90, "latest_close": 10,
                   "model_activation_status": "champion", "block_reason": "old_block",
                   "risk_flags": ["low-conviction"], "model_execution_tags": ["low-conviction"]}
            result = ScreenerService.__new__(ScreenerService)._apply_trade_readiness([row])[0]
            self.assertEqual(0, result["tradability_diagnostics"]["raw_model_score"])
            self.assertIsNone(result["block_reason"])
            self.assertEqual([], result["risk_flags"])

    def test_report_preserves_units_zero_and_activation_status(self):
        row = {"ticker": "TEST", "market": "US", "model_score": 0,
               "model_percentile": 1, "model_confidence": 0, "latest_close": 10,
               "target_weight": 0, "model_target_weight": 0.1,
               "model_activation_status": "observation_insufficient_oos"}
        result = _candidate_from_full_market_row(row, template="test", market="US")
        self.assertEqual(0.01, result["rank_percentile"])
        self.assertEqual(1, result["percentile"])
        self.assertEqual(0, result["score"])
        self.assertEqual(0, result["confidence"])
        self.assertEqual(0, result["target_weight"])
        self.assertEqual(row["model_activation_status"], result["model_activation_status"])
        self.assertNotEqual("READY", result["tradability_status"])

    def test_report_canonical_rank_rechecks_cached_readiness(self):
        result = _candidate_from_full_market_row({
            "ticker": "TEST", "market": "US", "rank_percentile": 0.01,
            "trade_readiness_score": 99, "tradability_status": "READY", "latest_close": 10,
        }, template="test", market="US")
        self.assertEqual("REVIEW", result["tradability_status"])

    def test_legacy_confidence_no_longer_boosts_report_rank(self):
        row = {"ticker": "TEST", "market": "US", "model_percentile": 90, "latest_close": 10}
        low = _candidate_from_full_market_row({**row, "model_confidence": 0}, template="test", market="US")
        high = _candidate_from_full_market_row({**row, "model_confidence": 99}, template="test", market="US")
        self.assertEqual(low["full_market_rank_score"], high["full_market_rank_score"])


class DateBalancedWeightTests(TestCase):
    def test_same_day_equal_weights_and_date_mass_independent_of_ticker_count(self):
        rows = [{"trade_date": "2026-01-05", "ticker": ticker} for ticker in ("A", "B", "C")]
        rows.append({"trade_date": "2026-01-06", "ticker": "A"})
        weights, audit = date_balanced_training_weights(rows)
        self.assertEqual(weights[0], weights[1])
        self.assertEqual(weights[1], weights[2])
        self.assertAlmostEqual(1.35 / 0.65, weights[3] / sum(weights[:3]))
        self.assertAlmostEqual(4, sum(weights))
        self.assertEqual(2, audit["date_count"])
        self.assertLessEqual(audit["effective_sample_count"], 4)

    def test_input_order_does_not_change_weights(self):
        rows = [{"trade_date": day, "ticker": ticker} for day in ("2026-01-05", "2026-01-06") for ticker in ("A", "B")]
        weights, audit = date_balanced_training_weights(rows)
        reversed_weights, reversed_audit = date_balanced_training_weights(list(reversed(rows)))
        self.assertEqual(weights, list(reversed(reversed_weights)))
        self.assertEqual(audit["dates"], reversed_audit["dates"])

    def test_single_date_and_invalid_inputs(self):
        weights, audit = date_balanced_training_weights([{"trade_date": "2026-01-05"}] * 3)
        self.assertEqual([1, 1, 1], weights)
        self.assertEqual(1, audit["date_count"])
        with self.assertRaises(ValueError):
            date_balanced_training_weights([])
        with self.assertRaises(ValueError):
            date_balanced_training_weights([{"trade_date": "invalid"}])
