from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import unittest

from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.two_stage import TwoStageConfig, fit_two_stage_selector


def _score(feature_date: date, ticker_index: int) -> FactorScore:
    safe = ticker_index < 5
    label = (ticker_index + 1) / 100.0 if safe else -0.10
    composite = float(ticker_index) if safe else float(100 + ticker_index)
    return FactorScore(
        sample_id=f"{feature_date.isoformat()}:S{ticker_index:02d}:5",
        ticker=f"S{ticker_index:02d}",
        feature_date=feature_date,
        label_available_date=feature_date + timedelta(days=5),
        horizon_days=5,
        factor_values={"low_range": 1.0 if safe else -1.0},
        missing_factors=(),
        composite_score=composite,
        cross_sectional_rank=ticker_index / 9,
        label_value=label,
    )


class TwoStageSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(12)]
        self.rows = [_score(day, index) for day in self.dates for index in range(10)]
        self.config = TwoStageConfig(
            horizon_days=5,
            risk_factor_name="low_range",
            target_top_n=2,
            candidate_fractions=(0.2, 0.5, 1.0),
            validation_dates=10,
            minimum_validation_dates=10,
        )

    def test_internal_validation_selects_risk_filtered_candidate_pool(self) -> None:
        model = fit_two_stage_selector(
            self.rows,
            config=self.config,
            prediction_date=date(2026, 1, 20),
        )
        self.assertEqual(0.5, model.selected_candidate_fraction)
        metrics = {item.candidate_fraction: item for item in model.audit.fraction_metrics}
        self.assertGreater(
            metrics[0.5].selection_objective,
            metrics[1.0].selection_objective,
        )

    def test_prediction_filters_unsafe_high_composite_rows(self) -> None:
        model = fit_two_stage_selector(
            self.rows,
            config=self.config,
            prediction_date=date(2026, 1, 20),
        )
        inference_date = date(2026, 1, 20)
        inference = [
            replace(
                _score(inference_date, index),
                label_available_date=None,
                label_value=None,
            )
            for index in range(10)
        ]
        predictions = model.predict(inference)
        selected = sorted(predictions, key=lambda item: -item.raw_score)[:2]
        self.assertEqual({"S03", "S04"}, {item.ticker for item in selected})

    def test_unavailable_label_and_missing_risk_factor_fail_closed(self) -> None:
        attacked = [
            replace(self.rows[0], label_available_date=date(2026, 1, 20)),
            *self.rows[1:],
        ]
        with self.assertRaisesRegex(ValueError, "available before"):
            fit_two_stage_selector(
                attacked,
                config=self.config,
                prediction_date=date(2026, 1, 20),
            )
        with self.assertRaisesRegex(ValueError, "risk factor"):
            fit_two_stage_selector(
                self.rows,
                config=replace(self.config, risk_factor_name="missing"),
                prediction_date=date(2026, 1, 20),
            )


if __name__ == "__main__":
    unittest.main()
