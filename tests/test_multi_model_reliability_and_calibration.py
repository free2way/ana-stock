"""Reliability-weighted confluence + calibrated probability abstention.

Covers the two changes requested for the screener fusion layer:

1. ``aggregate_multi_model_rows`` weights each model's vote by its recent
   ex-ante reliability (OOS hit rate / IC), keeping ``model_hit_count`` as the
   raw agreement count behind ``min_multi_model_hits``.
2. Every candidate carries a calibrated ``expected_hit_probability`` plus a
   ``calibration_status``; without calibration data the probability is ``None``
   and an optional ``min_hit_probability`` gate abstains on uncalibrated rows.
"""
from __future__ import annotations

import unittest

from app.services.stock_selection.multi_model_confluence import aggregate_multi_model_rows
from app.services.stock_selection.selective_policy import coverage_precision_curve


# Real MODEL_TEMPLATES keys: the confluence normalizer rejects unknown names.
HIGH = "lightgbm_top_picks"
MID = "technical_momentum"
LOW = "next_tesla_swing"


def _row(ticker: str, *, score: float = 50.0, action: str = "pullback", **extra) -> dict:
    row = {
        "ticker": ticker,
        "market": "CN",
        "snapshot_score": score,
        "trend_score": score,
        "action_label": action,
        "model_execution_tags": [],
        "tradability_status": "READY",
        "trade_readiness_score": 80.0,
    }
    row.update(extra)
    return row


def _params(**overrides) -> dict:
    params = {
        "multi_model_templates": [HIGH, LOW],
        "min_multi_model_hits": 2,
        "confluence_action_filter": "ALL",
        "strategy_profile": "",
        "market": "CN",
        "universe": "full_market",
        "sort_by": "confluence_rank",
        "sort_order": "desc",
        "limit": 500,
        "lang": "zh",
    }
    params.update(overrides)
    return params


CALIBRATION_SPEC = {
    "method": "isotonic",
    "source": "unit_test_calibration_v1",
    "samples": [[0.1, 0], [0.5, 0], [0.9, 1], [0.95, 1]],
}


class ReliabilityWeightTests(unittest.TestCase):
    def test_missing_reliability_falls_back_to_equal_weights(self) -> None:
        rows = {HIGH: [_row("AAA")], LOW: [_row("AAA")]}

        out, meta = aggregate_multi_model_rows(rows, template_keys=[HIGH, LOW], params=_params())

        self.assertEqual("equal_weight_fallback", meta["weight_source"])
        self.assertEqual(2, out[0]["model_hit_count"])
        self.assertAlmostEqual(2.0, out[0]["weighted_score"])
        self.assertAlmostEqual(1.0, out[0]["model_weight_lightgbm_top_picks"])
        self.assertAlmostEqual(1.0, out[0]["model_weight_next_tesla_swing"])
        self.assertEqual("equal_weight_fallback", out[0]["weight_source"])

    def test_row_metadata_reliability_sets_weights(self) -> None:
        rows = {
            HIGH: [_row("AAA", model_oos_hit_rate=0.8)],
            LOW: [_row("AAA", model_oos_hit_rate=0.2)],
        }

        out, meta = aggregate_multi_model_rows(rows, template_keys=[HIGH, LOW], params=_params())

        self.assertEqual("row_metadata:oos_hit_rate", meta["weight_source"])
        self.assertAlmostEqual(0.8, out[0]["model_weight_lightgbm_top_picks"])
        self.assertAlmostEqual(0.2, out[0]["model_weight_next_tesla_swing"])
        self.assertAlmostEqual(1.0, out[0]["weighted_score"])
        self.assertAlmostEqual(1.0, out[0]["weighted_score_normalized"])

    def test_high_reliability_vote_outweighs_low_reliability(self) -> None:
        rows = {HIGH: [_row("AAA")], LOW: [_row("AAA")]}
        low = aggregate_multi_model_rows(
            rows,
            template_keys=[HIGH, LOW],
            params=_params(model_reliability_weights={HIGH: 0.2, LOW: 0.2}),
        )[0][0]
        high = aggregate_multi_model_rows(
            rows,
            template_keys=[HIGH, LOW],
            params=_params(model_reliability_weights={HIGH: 0.9, LOW: 0.2}),
        )[0][0]

        self.assertEqual(low["model_hit_count"], high["model_hit_count"])
        self.assertGreater(high["weighted_score"], low["weighted_score"])

    def test_reliability_breaks_ties_between_equal_hit_counts(self) -> None:
        rows = {HIGH: [_row("AAA")], MID: [_row("AAA"), _row("BBB")], LOW: [_row("BBB")]}
        out, _meta = aggregate_multi_model_rows(
            rows,
            template_keys=[HIGH, MID, LOW],
            params=_params(
                multi_model_templates=[HIGH, MID, LOW],
                model_reliability_weights={HIGH: 0.9, MID: 0.5, LOW: 0.1},
            ),
        )
        by_ticker = {row["ticker"]: row for row in out}

        self.assertEqual(2, by_ticker["AAA"]["model_hit_count"])
        self.assertEqual(2, by_ticker["BBB"]["model_hit_count"])
        self.assertGreater(by_ticker["AAA"]["weighted_score"], by_ticker["BBB"]["weighted_score"])
        self.assertEqual(["AAA", "BBB"], [row["ticker"] for row in out])

    def test_min_hit_count_is_still_enforced(self) -> None:
        rows = {HIGH: [_row("ONLY")], LOW: [_row("OTHER")]}

        out, _meta = aggregate_multi_model_rows(rows, template_keys=[HIGH, LOW], params=_params())

        self.assertEqual([], out)


class CalibrationTests(unittest.TestCase):
    def _calibrated_rows(self) -> dict:
        return {
            HIGH: [_row("ALPHA", model_score=0.05), _row("OMEGA", model_score=0.95)],
            LOW: [_row("ALPHA", model_score=0.05), _row("OMEGA", model_score=0.95)],
        }

    def test_probability_is_none_without_calibration_data(self) -> None:
        out, meta = aggregate_multi_model_rows(
            self._calibrated_rows(),
            template_keys=[HIGH, LOW],
            params=_params(),
        )

        self.assertEqual("unavailable:no_calibration_spec", meta["calibration_status"])
        self.assertEqual(2, len(out))
        for row in out:
            self.assertIsNone(row["expected_hit_probability"])
            self.assertTrue(row["calibration_status"].startswith("unavailable"))

    def test_insufficient_calibration_data_is_reported(self) -> None:
        out, meta = aggregate_multi_model_rows(
            self._calibrated_rows(),
            template_keys=[HIGH, LOW],
            params=_params(probability_calibration={"method": "isotonic", "samples": [[0.5, 1]]}),
        )

        self.assertEqual("unavailable:insufficient_calibration_data", meta["calibration_status"])
        self.assertTrue(any(row["expected_hit_probability"] is None for row in out))

    def test_calibrated_probability_is_monotone_in_score(self) -> None:
        out, meta = aggregate_multi_model_rows(
            self._calibrated_rows(),
            template_keys=[HIGH, LOW],
            params=_params(probability_calibration=CALIBRATION_SPEC),
        )
        by_ticker = {row["ticker"]: row for row in out}

        self.assertEqual("calibrated", meta["calibration_status"])
        self.assertEqual("unit_test_calibration_v1", meta["calibration_source"])
        self.assertIsNotNone(by_ticker["OMEGA"]["expected_hit_probability"])
        self.assertIsNotNone(by_ticker["ALPHA"]["expected_hit_probability"])
        self.assertGreater(
            by_ticker["OMEGA"]["expected_hit_probability"],
            by_ticker["ALPHA"]["expected_hit_probability"],
        )
        self.assertEqual("calibrated", by_ticker["OMEGA"]["calibration_status"])

    def test_abstention_gate_returns_a_subset(self) -> None:
        rows = self._calibrated_rows()
        ungated, _meta = aggregate_multi_model_rows(
            rows,
            template_keys=[HIGH, LOW],
            params=_params(probability_calibration=CALIBRATION_SPEC),
        )
        gated, meta = aggregate_multi_model_rows(
            rows,
            template_keys=[HIGH, LOW],
            params=_params(probability_calibration=CALIBRATION_SPEC, min_hit_probability=0.5),
        )

        ungated_tickers = {row["ticker"] for row in ungated}
        gated_tickers = {row["ticker"] for row in gated}
        self.assertEqual({"ALPHA", "OMEGA"}, ungated_tickers)
        self.assertTrue(gated_tickers.issubset(ungated_tickers))
        self.assertEqual({"OMEGA"}, gated_tickers)
        self.assertTrue(meta["probability_gate"]["enabled"])
        self.assertAlmostEqual(0.5, meta["probability_gate"]["coverage"])
        self.assertEqual(1, meta["probability_gate"]["selected_count"])

    def test_uncalibrated_rows_abstain_when_gate_enabled(self) -> None:
        gated, meta = aggregate_multi_model_rows(
            self._calibrated_rows(),
            template_keys=[HIGH, LOW],
            params=_params(min_hit_probability=0.5),
        )

        self.assertEqual([], gated)
        self.assertEqual(2, meta["probability_gate"]["excluded_uncalibrated"])

    def test_invalid_abstention_threshold_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_hit_probability"):
            aggregate_multi_model_rows(
                self._calibrated_rows(),
                template_keys=[HIGH, LOW],
                params=_params(probability_calibration=CALIBRATION_SPEC, min_hit_probability=1.5),
            )

    def test_selective_calibration_research_bins_are_reusable(self) -> None:
        from dataclasses import asdict
        from datetime import date, timedelta

        from app.services.stock_selection.factor_baseline import BaselinePrediction
        from app.services.stock_selection.factor_pipeline import FactorScore
        from app.services.stock_selection.selective_calibration import (
            SelectiveCalibrationConfig,
            fit_selective_calibrator,
        )

        predictions: list[BaselinePrediction] = []
        labels: list[FactorScore] = []
        start = date(2026, 1, 1)
        for index in range(40):
            feature_date = start + timedelta(days=index // 10)
            rank = (index % 10) / 9
            sample_id = f"{feature_date.isoformat()}:S{index:02d}:5"
            predictions.append(
                BaselinePrediction(
                    sample_id=sample_id,
                    ticker=f"S{index:02d}",
                    feature_date=feature_date,
                    horizon_days=5,
                    raw_score=rank,
                    cross_sectional_rank=rank,
                    model_version="v1",
                )
            )
            labels.append(
                FactorScore(
                    sample_id=sample_id,
                    ticker=f"S{index:02d}",
                    feature_date=feature_date,
                    label_available_date=feature_date + timedelta(days=5),
                    horizon_days=5,
                    factor_values={},
                    missing_factors=(),
                    composite_score=rank,
                    cross_sectional_rank=rank,
                    label_value=0.02 if rank >= 8 / 9 else -0.01,
                )
            )
        calibrator = fit_selective_calibrator(
            predictions,
            labels,
            prediction_date=date(2026, 2, 1),
            config=SelectiveCalibrationConfig(bin_count=5, minimum_observations=40, prior_strength=0.0),
        )

        out, meta = aggregate_multi_model_rows(
            self._calibrated_rows(),
            template_keys=[HIGH, LOW],
            params=_params(
                probability_calibration={
                    "method": "bins",
                    "bins": [asdict(bin_row) for bin_row in calibrator.bins],
                    "source": calibrator.calibration_version,
                }
            ),
        )

        by_ticker = {row["ticker"]: row for row in out}
        self.assertEqual("calibrated", meta["calibration_status"])
        self.assertEqual(calibrator.calibration_version, meta["calibration_source"])
        self.assertGreater(
            by_ticker["OMEGA"]["expected_hit_probability"],
            by_ticker["ALPHA"]["expected_hit_probability"],
        )


class CoveragePrecisionTests(unittest.TestCase):
    def test_raising_the_threshold_trades_coverage_for_precision(self) -> None:
        rows = [
            {"expected_hit_probability": probability, "label_value": outcome}
            for probability, outcome in ((0.1, 0), (0.3, 0), (0.5, 0), (0.7, 1), (0.9, 1))
        ]

        points = coverage_precision_curve(
            rows,
            thresholds=[0.0, 0.4, 0.6, 0.8],
            outcome_key="label_value",
        )

        coverages = [point.coverage for point in points]
        self.assertEqual(sorted(coverages, reverse=True), coverages)
        expected_precisions = [0.4, 2 / 3, 1.0, 1.0]
        for point, expected in zip(points, expected_precisions):
            self.assertIsNotNone(point.precision)
            assert point.precision is not None
            self.assertAlmostEqual(expected, point.precision)
        self.assertEqual(1.0, points[0].coverage)
        self.assertAlmostEqual(0.2, points[-1].coverage)

    def test_invalid_threshold_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "thresholds"):
            coverage_precision_curve([], thresholds=[1.5])


if __name__ == "__main__":
    unittest.main()
