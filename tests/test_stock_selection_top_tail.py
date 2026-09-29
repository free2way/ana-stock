from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import unittest

from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.top_tail import TopTailConfig, fit_top_tail_classifier


def _score(feature_date: date, ticker_index: int) -> FactorScore:
    label = (ticker_index - 5) / 10.0
    return FactorScore(
        sample_id=f"{feature_date.isoformat()}:S{ticker_index:02d}:3",
        ticker=f"S{ticker_index:02d}",
        feature_date=feature_date,
        label_available_date=feature_date + timedelta(days=3),
        horizon_days=3,
        factor_values={"signal": label, "risk": float(10 - ticker_index)},
        missing_factors=(),
        composite_score=label,
        cross_sectional_rank=ticker_index / 9,
        label_value=label,
    )


class TopTailClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(6)]
        self.rows = [_score(day, index) for day in self.dates for index in range(10)]
        self.config = TopTailConfig(
            feature_names=("signal", "risk"),
            horizon_days=3,
            target_top_n=2,
            min_group_size=10,
            n_estimators=25,
            learning_rate=0.1,
            num_leaves=5,
            min_child_samples=2,
            subsample=1.0,
            colsample_bytree=1.0,
        )

    def test_fit_targets_positive_daily_head_and_is_deterministic(self) -> None:
        prediction_date = date(2026, 1, 12)
        first = fit_top_tail_classifier(
            self.rows,
            config=self.config,
            prediction_date=prediction_date,
        )
        second = fit_top_tail_classifier(
            self.rows,
            config=self.config,
            prediction_date=prediction_date,
        )
        self.assertEqual(12, first.audit.positive_label_count)
        self.assertEqual(48, first.audit.negative_label_count)
        self.assertEqual(60, first.audit.training_sample_count)
        self.assertEqual(first.model_version, second.model_version)

    def test_predict_returns_ranked_probabilities(self) -> None:
        model = fit_top_tail_classifier(
            self.rows,
            config=self.config,
            prediction_date=date(2026, 1, 12),
        )
        inference_date = date(2026, 1, 12)
        inference = [
            replace(
                _score(inference_date, index),
                label_available_date=None,
                label_value=None,
            )
            for index in range(10)
        ]
        predictions = model.predict(inference)
        self.assertEqual(10, len(predictions))
        self.assertTrue(all(0.0 <= item.raw_score <= 1.0 for item in predictions))
        self.assertGreater(
            next(item.raw_score for item in predictions if item.ticker == "S09"),
            next(item.raw_score for item in predictions if item.ticker == "S00"),
        )

    def test_unavailable_labels_fail_closed(self) -> None:
        attacked = [
            replace(self.rows[0], label_available_date=date(2026, 1, 12)),
            *self.rows[1:],
        ]
        with self.assertRaisesRegex(ValueError, "unavailable"):
            fit_top_tail_classifier(
                attacked,
                config=self.config,
                prediction_date=date(2026, 1, 12),
            )


if __name__ == "__main__":
    unittest.main()
