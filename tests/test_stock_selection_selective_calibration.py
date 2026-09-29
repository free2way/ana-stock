from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import unittest

from app.services.stock_selection.factor_baseline import BaselinePrediction
from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.selective_calibration import (
    SelectiveCalibrationConfig,
    fit_selective_calibrator,
    run_selective_walk_forward,
)
from app.services.stock_selection.selective_policy import (
    SelectiveEvaluationConfig,
    SelectivePolicyConfig,
    select_candidates,
)


def _history() -> tuple[list[BaselinePrediction], list[FactorScore]]:
    predictions: list[BaselinePrediction] = []
    labels: list[FactorScore] = []
    start = date(2026, 1, 1)
    for index in range(40):
        feature_date = start + timedelta(days=index // 10)
        rank = (index % 10) / 9
        sample_id = f"{feature_date.isoformat()}:S{index:02d}:5"
        prediction = BaselinePrediction(
            sample_id=sample_id,
            ticker=f"S{index:02d}",
            feature_date=feature_date,
            horizon_days=5,
            raw_score=rank,
            cross_sectional_rank=rank,
            model_version="ridge-oos-v1",
        )
        predictions.append(prediction)
        value = 0.02 if rank >= 8 / 9 else -0.01
        labels.append(
            FactorScore(
                sample_id=sample_id,
                ticker=prediction.ticker,
                feature_date=feature_date,
                label_available_date=feature_date + timedelta(days=5),
                horizon_days=5,
                factor_values={},
                missing_factors=(),
                composite_score=rank,
                cross_sectional_rank=rank,
                label_value=value,
                label_components={"risk_adjusted_return": value},
            )
        )
    return predictions, labels


class SelectiveCalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.predictions, self.labels = _history()
        self.config = SelectiveCalibrationConfig(
            bin_count=5,
            minimum_observations=40,
            prior_strength=0.0,
        )

    def test_calibration_turns_existing_oos_scores_into_selective_inputs(self) -> None:
        calibrator = fit_selective_calibrator(
            self.predictions,
            self.labels,
            prediction_date=date(2026, 2, 1),
            config=self.config,
        )
        inference_date = date(2026, 2, 2)
        inference = [
            replace(
                self.predictions[0],
                sample_id="new-low",
                ticker="LOW",
                feature_date=inference_date,
                cross_sectional_rank=0.0,
            ),
            replace(
                self.predictions[0],
                sample_id="new-high",
                ticker="HIGH",
                feature_date=inference_date,
                raw_score=1.0,
                cross_sectional_rank=1.0,
            ),
        ]
        candidates = calibrator.calibrate(inference)
        by_ticker = {item.ticker: item for item in candidates}
        self.assertEqual(0.0, by_ticker["LOW"].positive_probability)
        self.assertEqual(1.0, by_ticker["HIGH"].positive_probability)
        self.assertLess(by_ticker["LOW"].expected_risk_adjusted_return, 0)
        self.assertGreater(by_ticker["HIGH"].expected_risk_adjusted_return, 0)
        decision = select_candidates(
            candidates,
            config=SelectivePolicyConfig(
                max_selected_per_date=1,
                minimum_positive_probability=0.60,
                minimum_expected_risk_adjusted_return=0.003,
                minimum_cross_sectional_rank=0.80,
                maximum_uncertainty=0.20,
            ),
        )[0]
        self.assertEqual(("HIGH",), tuple(item.ticker for item in decision.selected))

    def test_calibration_rejects_labels_not_yet_observable(self) -> None:
        attacked = [
            replace(self.labels[0], label_available_date=date(2026, 2, 1)),
            *self.labels[1:],
        ]
        with self.assertRaisesRegex(ValueError, "available before"):
            fit_selective_calibrator(
                self.predictions,
                attacked,
                prediction_date=date(2026, 2, 1),
                config=self.config,
            )

    def test_equal_scores_are_never_split_across_calibration_bins(self) -> None:
        tied = [replace(item, raw_score=1.0, cross_sectional_rank=0.5) for item in self.predictions]
        calibrator = fit_selective_calibrator(
            tied,
            self.labels,
            prediction_date=date(2026, 2, 1),
            config=self.config,
        )
        self.assertEqual(1, len(calibrator.bins))
        self.assertEqual(40, calibrator.bins[0].observation_count)

    def test_walk_forward_uses_only_prior_matured_oos_dates(self) -> None:
        predictions: list[BaselinePrediction] = []
        labels: list[FactorScore] = []
        start = date(2026, 3, 1)
        for day_index in range(10):
            feature_date = start + timedelta(days=day_index)
            for ticker_index in range(6):
                rank = ticker_index / 5
                sample_id = f"{feature_date.isoformat()}:W{ticker_index}:2"
                prediction = BaselinePrediction(
                    sample_id=sample_id,
                    ticker=f"W{ticker_index}",
                    feature_date=feature_date,
                    horizon_days=2,
                    raw_score=rank,
                    cross_sectional_rank=rank,
                    model_version="walk-forward-v1",
                )
                predictions.append(prediction)
                value = 0.02 if rank >= 0.8 else -0.01
                labels.append(
                    FactorScore(
                        sample_id=sample_id,
                        ticker=prediction.ticker,
                        feature_date=feature_date,
                        label_available_date=feature_date + timedelta(days=2),
                        horizon_days=2,
                        factor_values={},
                        missing_factors=(),
                        composite_score=rank,
                        cross_sectional_rank=rank,
                        label_value=value,
                    )
                )
        result = run_selective_walk_forward(
            predictions,
            labels,
            evaluation_config=SelectiveEvaluationConfig(
                market="CN",
                model_key="walk-forward-shadow",
                round_trip_cost_bps=20.0,
            ),
            calibration_config=SelectiveCalibrationConfig(
                bin_count=3,
                minimum_observations=12,
                prior_strength=0.0,
            ),
            policy_config=SelectivePolicyConfig(
                max_selected_per_date=2,
                minimum_positive_probability=0.60,
                minimum_expected_risk_adjusted_return=0.003,
                minimum_cross_sectional_rank=0.80,
                maximum_uncertainty=0.20,
            ),
            calibration_lookback_dates=3,
        )
        self.assertEqual(
            "skipped:insufficient_matured_oos_calibration",
            result.audits[0].status,
        )
        first_success = next(item for item in result.audits if item.status == "success")
        self.assertLess(first_success.calibration_end_date, first_success.prediction_date)
        self.assertEqual(2, first_success.selected_count)
        self.assertIsNotNone(result.evaluation)
        assert result.evaluation is not None
        self.assertEqual(len(result.decisions), result.evaluation.oos_date_count)
        self.assertEqual(1.0, result.evaluation.coverage_rate)


if __name__ == "__main__":
    unittest.main()
