from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.stock_selection.factor_baseline import (
    equal_weight_predictions,
    fit_ridge_factor_baseline,
)
from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorDirection,
    FactorObservation,
    FactorSpec,
)
from app.services.stock_selection.schemas import LabeledSample


def _sample(
    ticker: str,
    feature_date: date,
    *,
    momentum: float | None,
    risk: float,
    label: float,
    available_offset: int = 3,
) -> LabeledSample:
    features = {"risk": risk}
    if momentum is not None:
        features["momentum"] = momentum
    return LabeledSample(
        sample_id=f"{ticker}:{feature_date.isoformat()}",
        market="US",
        ticker=ticker,
        feature_date=feature_date,
        label_start_date=feature_date + timedelta(days=1),
        label_end_date=feature_date + timedelta(days=available_offset),
        label_available_date=feature_date + timedelta(days=available_offset),
        horizon_days=3,
        label_value=label,
        features=features,
        dataset_version="fixture-v1",
    )


class FactorBaselineTests(TestCase):
    def setUp(self) -> None:
        self.start = date(2026, 7, 1)
        self.pipeline = CrossSectionalFactorPipeline(
            [
                FactorSpec("momentum", FactorDirection.HIGHER_BETTER, weight=1.0),
                FactorSpec("risk", FactorDirection.LOWER_BETTER, weight=1.0),
            ]
        )

    def test_transform_is_same_date_only_and_respects_direction(self) -> None:
        day_one = [
            _sample("A", self.start, momentum=1.0, risk=3.0, label=0.1),
            _sample("B", self.start, momentum=2.0, risk=2.0, label=0.2),
            _sample("C", self.start, momentum=3.0, risk=1.0, label=0.3),
        ]
        baseline = self.pipeline.transform(day_one)
        with_future_extreme = self.pipeline.transform(
            day_one
            + [
                _sample(
                    "FUTURE",
                    self.start + timedelta(days=1),
                    momentum=1_000_000.0,
                    risk=0.0,
                    label=1.0,
                )
            ]
        )
        baseline_by_id = {item.sample_id: item for item in baseline}
        changed_by_id = {item.sample_id: item for item in with_future_extreme}

        for sample in day_one:
            self.assertEqual(
                baseline_by_id[sample.sample_id].factor_values,
                changed_by_id[sample.sample_id].factor_values,
            )
            self.assertEqual(
                baseline_by_id[sample.sample_id].cross_sectional_rank,
                changed_by_id[sample.sample_id].cross_sectional_rank,
            )
        self.assertGreater(
            baseline_by_id[day_one[2].sample_id].composite_score,
            baseline_by_id[day_one[0].sample_id].composite_score,
        )

    def test_missing_factor_is_neutral_and_audited(self) -> None:
        scores = self.pipeline.transform(
            [
                _sample("A", self.start, momentum=None, risk=1.0, label=0.1),
                _sample("B", self.start, momentum=2.0, risk=2.0, label=0.2),
            ]
        )
        first = next(item for item in scores if item.ticker == "A")
        self.assertEqual(0.0, first.factor_values["momentum"])
        self.assertIn("momentum", first.missing_factors)

    def test_equal_weight_and_ridge_baselines_are_reproducible(self) -> None:
        samples = []
        for day_offset in range(2):
            feature_date = self.start + timedelta(days=day_offset)
            samples.extend(
                [
                    _sample("A", feature_date, momentum=1.0, risk=3.0, label=0.1),
                    _sample("B", feature_date, momentum=2.0, risk=2.0, label=0.2),
                    _sample("C", feature_date, momentum=3.0, risk=1.0, label=0.3),
                ]
            )
        scores = self.pipeline.transform(samples)
        equal_weight = equal_weight_predictions(scores)
        first = fit_ridge_factor_baseline(
            scores,
            factor_names=("momentum", "risk"),
            horizon_days=3,
            prediction_date=self.start + timedelta(days=6),
            alpha=1.0,
        )
        second = fit_ridge_factor_baseline(
            scores,
            factor_names=("momentum", "risk"),
            horizon_days=3,
            prediction_date=self.start + timedelta(days=6),
            alpha=1.0,
        )
        predictions = first.predict(scores)

        self.assertEqual(6, len(equal_weight))
        self.assertEqual(first.model_version, second.model_version)
        self.assertEqual(first.coefficients, second.coefficients)
        self.assertEqual(6, len(predictions))
        self.assertTrue(all(0.0 <= item.cross_sectional_rank <= 1.0 for item in predictions))

    def test_ridge_rejects_label_that_is_not_available(self) -> None:
        samples = [
            _sample("A", self.start, momentum=1.0, risk=2.0, label=0.1),
            _sample("B", self.start, momentum=2.0, risk=1.0, label=0.2),
        ]
        scores = self.pipeline.transform(samples)
        with self.assertRaisesRegex(ValueError, "labels unavailable"):
            fit_ridge_factor_baseline(
                scores,
                factor_names=("momentum", "risk"),
                horizon_days=3,
                prediction_date=self.start + timedelta(days=3),
            )

    def test_ridge_rejects_unlabeled_inference_rows(self) -> None:
        scores = self.pipeline.transform(
            [
                FactorObservation(
                    observation_id="live:A",
                    ticker="A",
                    feature_date=self.start,
                    horizon_days=3,
                    features={"momentum": 1.0, "risk": 2.0},
                ),
                FactorObservation(
                    observation_id="live:B",
                    ticker="B",
                    feature_date=self.start,
                    horizon_days=3,
                    features={"momentum": 2.0, "risk": 1.0},
                ),
            ]
        )
        with self.assertRaisesRegex(ValueError, "requires matured labels"):
            fit_ridge_factor_baseline(
                scores,
                factor_names=("momentum", "risk"),
                horizon_days=3,
                prediction_date=self.start + timedelta(days=4),
            )
