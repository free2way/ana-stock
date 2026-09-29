from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorDirection,
    FactorObservation,
    FactorSpec,
)
from app.services.stock_selection.ranker import RankerConfig, fit_cross_sectional_ranker
from app.services.stock_selection.schemas import LabeledSample


class CrossSectionalRankerTests(TestCase):
    def setUp(self) -> None:
        self.start = date(2026, 5, 1)
        self.prediction_date = date(2026, 7, 1)
        self.pipeline = CrossSectionalFactorPipeline(
            [
                FactorSpec("momentum", FactorDirection.HIGHER_BETTER),
                FactorSpec("risk", FactorDirection.LOWER_BETTER),
            ]
        )
        self.config = RankerConfig(
            feature_names=("momentum", "risk"),
            horizon_days=3,
            min_group_size=5,
            n_estimators=35,
            learning_rate=0.1,
            num_leaves=7,
            min_child_samples=2,
            subsample=1.0,
            colsample_bytree=1.0,
        )

    def _training_samples(self) -> list[LabeledSample]:
        samples: list[LabeledSample] = []
        for day_index in range(6):
            feature_date = self.start + timedelta(days=day_index * 7)
            for ticker_index in range(10):
                ticker = f"S{ticker_index:02d}"
                momentum = float(ticker_index) + day_index * 0.05
                samples.append(
                    LabeledSample(
                        sample_id=f"{feature_date.isoformat()}:{ticker}",
                        market="US",
                        ticker=ticker,
                        feature_date=feature_date,
                        label_start_date=feature_date + timedelta(days=1),
                        label_end_date=feature_date + timedelta(days=3),
                        label_available_date=feature_date + timedelta(days=3),
                        horizon_days=3,
                        label_value=momentum - ticker_index * 0.01,
                        features={
                            "momentum": momentum,
                            "risk": float(10 - ticker_index),
                        },
                        dataset_version="ranker-fixture-v1",
                    )
                )
        return samples

    def test_lambdarank_groups_by_feature_date_and_is_reproducible(self) -> None:
        scores = self.pipeline.transform(self._training_samples())
        first = fit_cross_sectional_ranker(
            scores,
            config=self.config,
            prediction_date=self.prediction_date,
        )
        second = fit_cross_sectional_ranker(
            scores,
            config=self.config,
            prediction_date=self.prediction_date,
        )

        self.assertEqual((10, 10, 10, 10, 10, 10), first.audit.training_group_sizes)
        self.assertEqual(60, first.audit.training_sample_count)
        self.assertEqual(6, len(first.audit.training_group_dates))
        self.assertEqual(60, sum(first.audit.relevance_grade_counts.values()))
        self.assertEqual(set(range(5)), set(first.audit.relevance_grade_counts))
        self.assertEqual(first.model_version, second.model_version)
        self.assertEqual(
            set(self.config.feature_names),
            set(first.audit.feature_importance_gain),
        )

    def test_unlabeled_post_close_cross_section_can_be_ranked(self) -> None:
        training_scores = self.pipeline.transform(self._training_samples())
        model = fit_cross_sectional_ranker(
            training_scores,
            config=self.config,
            prediction_date=self.prediction_date,
        )
        inference_date = self.prediction_date
        observations = [
            FactorObservation(
                observation_id=f"live:{index}",
                ticker=f"L{index:02d}",
                feature_date=inference_date,
                horizon_days=3,
                features={"momentum": float(index), "risk": float(10 - index)},
            )
            for index in range(10)
        ]

        inference_scores = self.pipeline.transform(observations)
        predictions = model.predict(inference_scores)

        self.assertEqual(10, len(predictions))
        self.assertTrue(all(item.label_value is None for item in inference_scores))
        self.assertTrue(all(item.label_available_date is None for item in inference_scores))
        self.assertLess(min(item.cross_sectional_rank for item in predictions), 0.2)
        self.assertGreater(max(item.cross_sectional_rank for item in predictions), 0.8)
        by_ticker = {item.ticker: item for item in predictions}
        self.assertGreater(by_ticker["L09"].raw_score, by_ticker["L00"].raw_score)

        wrong_horizon = self.pipeline.transform(
            [
                FactorObservation(
                    observation_id="wrong-horizon",
                    ticker="W",
                    feature_date=inference_date,
                    horizon_days=5,
                    features={"momentum": 1.0, "risk": 1.0},
                )
            ]
        )
        with self.assertRaisesRegex(ValueError, "match model horizon"):
            model.predict(wrong_horizon)

    def test_training_rejects_unavailable_or_missing_labels(self) -> None:
        scores = self.pipeline.transform(self._training_samples())
        with self.assertRaisesRegex(ValueError, "labels unavailable"):
            fit_cross_sectional_ranker(
                scores,
                config=self.config,
                prediction_date=self.start + timedelta(days=3),
            )

        unlabeled = self.pipeline.transform(
            [
                FactorObservation(
                    observation_id=f"unlabeled:{index}",
                    ticker=f"U{index}",
                    feature_date=self.start,
                    horizon_days=3,
                    features={"momentum": float(index), "risk": 1.0},
                )
                for index in range(5)
            ]
        )
        with self.assertRaisesRegex(ValueError, "requires matured labels"):
            fit_cross_sectional_ranker(
                unlabeled,
                config=self.config,
                prediction_date=self.prediction_date,
            )

    def test_small_and_constant_groups_are_skipped_with_reason(self) -> None:
        samples = self._training_samples()
        small_date = self.start + timedelta(days=45)
        for index in range(2):
            samples.append(
                LabeledSample(
                    sample_id=f"small:{index}",
                    market="US",
                    ticker=f"X{index}",
                    feature_date=small_date,
                    label_start_date=small_date + timedelta(days=1),
                    label_end_date=small_date + timedelta(days=3),
                    label_available_date=small_date + timedelta(days=3),
                    horizon_days=3,
                    label_value=float(index),
                    features={"momentum": float(index), "risk": 1.0},
                    dataset_version="ranker-fixture-v1",
                )
            )
        constant_date = self.start + timedelta(days=49)
        for index in range(5):
            samples.append(
                LabeledSample(
                    sample_id=f"constant:{index}",
                    market="US",
                    ticker=f"C{index}",
                    feature_date=constant_date,
                    label_start_date=constant_date + timedelta(days=1),
                    label_end_date=constant_date + timedelta(days=3),
                    label_available_date=constant_date + timedelta(days=3),
                    horizon_days=3,
                    label_value=0.0,
                    features={"momentum": float(index), "risk": 1.0},
                    dataset_version="ranker-fixture-v1",
                )
            )

        model = fit_cross_sectional_ranker(
            self.pipeline.transform(samples),
            config=self.config,
            prediction_date=self.prediction_date,
        )

        self.assertIn((small_date, "below_min_group_size"), model.audit.skipped_groups)
        self.assertIn((constant_date, "constant_label"), model.audit.skipped_groups)
        self.assertEqual(60, model.audit.training_sample_count)
