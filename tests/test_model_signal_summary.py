from unittest import TestCase
from unittest.mock import patch

from starlette.requests import Request

from app.services.model_signal_summary import (
    enrich_model_output,
    estimate_display_percent,
    model_confidence,
)
from app.services.trainer import SignalTrainer
from app.services.workspace_snapshots import _watchlist_combined_analysis
from app.api.routes.symbols import _lightweight_symbol_summary
from app.api.routes.dashboard import _first_not_none
from app.api.routes.insights import TEXT, insight_model_output


class ModelEstimateSemanticsTests(TestCase):
    def test_regression_score_does_not_create_estimates(self):
        for score in (None, -0.5, 0, 0.2, 2):
            result = enrich_model_output({"score": score}, lang="en")
            for key in ("confidence", "bullish_prob", "bearish_prob", "expected_return_5d", "expected_return_20d", "expected_drawdown_20d"):
                self.assertIsNone(result.get(key), key)
            self.assertIsNone(model_confidence(score))
            self.assertEqual("unavailable", result["estimate_source"])

    def test_supplied_values_including_zero_are_preserved_not_certified(self):
        result = enrich_model_output({"score": 0.2, "confidence": 0, "expected_return_5d": -2.0}, lang="en")
        self.assertEqual(0, result["confidence"])
        self.assertEqual(-2, result["expected_return_5d"])
        self.assertEqual("supplied_unverified", result["estimate_source"])
        self.assertEqual("legacy_unverified", result["estimate_protocol_id"])
        self.assertFalse(result["estimate_certified"])
        self.assertEqual("legacy_percent", result["estimate_units"]["probability"])
        self.assertIsNone(result.get("expected_return_20d"))

    def test_explicit_calibrated_ratio_has_unambiguous_units(self):
        result = enrich_model_output(
            {
                "score": 0.2,
                "bullish_prob": 0.607,
                "expected_return_5d": 0.012,
                "estimate_schema_version": "calibrated_estimates_v1",
                "probability_unit": "ratio",
                "return_unit": "ratio",
                "estimate_protocol_id": "protocol-fixture-v1",
            },
            lang="en",
        )
        self.assertTrue(result["estimate_certified"])
        self.assertEqual("calibrated_protocol", result["estimate_source"])
        self.assertAlmostEqual(60.7, estimate_display_percent(result, "bullish_prob"))
        self.assertAlmostEqual(1.2, estimate_display_percent(result, "expected_return_5d"))

    def test_invalid_calibrated_probability_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "bullish_prob must be in"):
            enrich_model_output(
                {
                    "bullish_prob": 1.2,
                    "estimate_schema_version": "calibrated_estimates_v1",
                    "probability_unit": "ratio",
                    "return_unit": "ratio",
                    "estimate_protocol_id": "protocol-fixture-v1",
                },
                lang="en",
            )

    def test_legacy_cache_cannot_self_certify_without_schema(self):
        result = enrich_model_output(
            {
                "bullish_prob": 60.7,
                "estimate_source": "calibrated_protocol",
                "estimate_protocol_id": "old-cache",
            },
            lang="en",
        )
        self.assertFalse(result["estimate_certified"])
        self.assertEqual("supplied_unverified", result["estimate_source"])
        self.assertEqual("calibrated_protocol", result["legacy_estimate_source"])
        self.assertEqual("old-cache", result["estimate_protocol_id"])

    def test_display_contract_does_not_replace_zero_or_claim_score_probability(self):
        self.assertEqual(0, _first_not_none(0, 67, None))
        self.assertIn("never converted", TEXT["en"]["probability_help"])
        self.assertIn("不会把", TEXT["zh"]["probability_help"])
        self.assertNotIn("score-derived probability", TEXT["en"]["probability_help"])

    def test_short_horizon_extrema_are_not_twenty_day_estimates(self):
        result = SignalTrainer()._build_detail_row(
            symbol_id=1, trade_date="2026-09-11", score=0.2, rank_value=1,
            universe_size=100, horizon_days=5, run_name="test",
            calibrated_metrics={"next_5d_close_return_avg": 1.2,
                                "next_5d_max_return_avg": 8, "next_5d_max_drawdown_avg": -4},
        )
        self.assertEqual(1.2, result["expected_return_5d"])
        for key in ("confidence", "bullish_prob", "expected_return_20d", "expected_drawdown_20d"):
            self.assertIsNone(result[key])

    def test_unknown_confidence_stays_unknown_in_lightweight_views(self):
        for confidence in (None, 0, 67):
            output = {"score": 0.2, "confidence": confidence}
            self.assertEqual(confidence, _watchlist_combined_analysis(output)["confidence"])
            self.assertEqual(confidence, _lightweight_symbol_summary({"ticker": "TEST"}, output)["confidence"])

    @patch("app.api.routes.insights.is_authenticated", return_value=True)
    @patch("app.api.routes.insights._build_model_context")
    def test_model_output_api_keeps_ratio_and_unit_metadata(
        self, build_context, _authenticated
    ):
        model_output = enrich_model_output(
            {
                "ticker": "TEST",
                "score": 0.2,
                "bullish_prob": 0.607,
                "estimate_schema_version": "calibrated_estimates_v1",
                "probability_unit": "ratio",
                "return_unit": "ratio",
                "estimate_protocol_id": "protocol-fixture-v1",
            },
            lang="en",
        )
        build_context.return_value = {
            "model_output": model_output,
            "drivers": {"positive": [], "risks": []},
            "fundamental_summary": None,
            "feature_contributions": {},
            "trade_plan": None,
        }
        request = Request({"type": "http", "method": "GET", "path": "/"})

        payload = insight_model_output(request, "TEST", lang="en", db=object())

        self.assertEqual(0.607, payload["bullish_prob"])
        self.assertEqual("ratio", payload["estimate_units"]["probability"])
        self.assertTrue(payload["estimate_certified"])
