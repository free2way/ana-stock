from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from unittest import TestCase

from app.services.stock_selection.evaluation import (
    CrossSectionalEvaluationConfig,
    evaluate_cross_sectional_predictions,
)
from app.services.stock_selection.factor_baseline import BaselinePrediction
from app.services.stock_selection.factor_pipeline import FactorScore


def _score(
    ticker: str,
    feature_date: date,
    *,
    label: float,
    horizon_days: int = 3,
    with_components: bool = False,
) -> FactorScore:
    return FactorScore(
        sample_id=f"{feature_date.isoformat()}:{ticker}:{horizon_days}",
        ticker=ticker,
        feature_date=feature_date,
        label_available_date=feature_date + timedelta(days=horizon_days),
        horizon_days=horizon_days,
        factor_values={"signal": label},
        missing_factors=(),
        composite_score=label,
        cross_sectional_rank=0.5,
        label_value=label,
        label_components=(
            {
                "gross_return": label + 0.02,
                "net_return": label + 0.01,
                "market_excess_return": label + 0.005,
                "industry_excess_return": label,
                "path_drawdown": min(label, 0.0),
                "risk_adjusted_return": label,
            }
            if with_components
            else {}
        ),
    )


def _prediction(score: FactorScore, *, raw_score: float | None = None) -> BaselinePrediction:
    value = score.label_value if raw_score is None else raw_score
    assert value is not None
    return BaselinePrediction(
        sample_id=score.sample_id,
        ticker=score.ticker,
        feature_date=score.feature_date,
        horizon_days=score.horizon_days,
        raw_score=float(value),
        cross_sectional_rank=0.5,
        model_version="fixture-model-v1",
    )


class StockSelectionEvaluationTests(TestCase):
    def test_perfect_ranking_reports_ic_quantiles_and_top_n(self) -> None:
        start = date(2026, 4, 1)
        labels = [
            _score(f"S{ticker_index:02d}", start + timedelta(days=day_index), label=float(ticker_index))
            for day_index in range(3)
            for ticker_index in range(10)
        ]
        report = evaluate_cross_sectional_predictions(
            [_prediction(item) for item in labels],
            labels,
            config=CrossSectionalEvaluationConfig(model_key="perfect", top_ns=(1, 5, 10)),
        )

        self.assertEqual(3, len(report.evaluated_dates))
        self.assertEqual(30, report.sample_count)
        self.assertAlmostEqual(1.0, report.rank_ic_mean or 0.0)
        self.assertAlmostEqual(1.0, report.quantile_monotonicity or 0.0)
        self.assertAlmostEqual(8.0, report.top_minus_bottom_mean or 0.0)
        self.assertAlmostEqual(9.0, report.top_n_metrics[1].mean_label)
        self.assertAlmostEqual(7.0, report.top_n_metrics[5].mean_label)
        self.assertAlmostEqual(1.0, report.top_n_metrics[5].positive_oracle_precision_at_n)
        self.assertAlmostEqual(5.0, report.top_n_metrics[5].average_positive_oracle_count)
        self.assertAlmostEqual(0.0, report.top_n_metrics[5].average_one_way_turnover or 0.0)
        self.assertEqual("S09", report.top_n_metrics[1].top_ticker_contributions[0].key)
        self.assertAlmostEqual(1.0, report.top_n_metrics[1].max_repeat_frequency)

    def test_reports_decomposed_label_components(self) -> None:
        feature_date = date(2026, 4, 1)
        labels = [
            _score(f"S{index}", feature_date, label=float(index) / 100.0, with_components=True)
            for index in range(5)
        ]
        report = evaluate_cross_sectional_predictions(
            [_prediction(item) for item in labels],
            labels,
            config=CrossSectionalEvaluationConfig(model_key="components", top_ns=(2,)),
        )

        self.assertEqual(1.0, report.label_component_coverage["gross_return"])
        self.assertAlmostEqual(0.055, report.top_n_metrics[2].mean_label_components["gross_return"])
        self.assertAlmostEqual(0.02, report.overall_label_component_means["risk_adjusted_return"])

    def test_equal_weight_one_way_turnover_tracks_selection_replacement(self) -> None:
        first_date = date(2026, 5, 1)
        second_date = first_date + timedelta(days=1)
        labels = [
            _score("A", first_date, label=1.0),
            _score("B", first_date, label=0.0),
            _score("A", second_date, label=0.0),
            _score("B", second_date, label=1.0),
        ]
        report = evaluate_cross_sectional_predictions(
            [_prediction(item) for item in labels],
            labels,
            config=CrossSectionalEvaluationConfig(model_key="rotating", top_ns=(1,), quantile_count=2),
        )

        top_one = report.top_n_metrics[1]
        self.assertAlmostEqual(1.0, top_one.average_one_way_turnover or 0.0)
        self.assertAlmostEqual(0.5, top_one.max_repeat_frequency)
        self.assertAlmostEqual(1.0, top_one.positive_date_rate)

    def test_constant_predictions_exclude_undefined_daily_ic(self) -> None:
        feature_date = date(2026, 6, 1)
        labels = [_score(f"S{index}", feature_date, label=float(index)) for index in range(5)]
        report = evaluate_cross_sectional_predictions(
            [_prediction(item, raw_score=1.0) for item in labels],
            labels,
            config=CrossSectionalEvaluationConfig(model_key="constant", top_ns=(2,)),
        )

        self.assertEqual(0, report.rank_ic_observation_count)
        self.assertIsNone(report.rank_ic_mean)
        self.assertIsNone(report.rank_ic_ci95)

    def test_identity_mismatch_and_missing_labels_fail_closed(self) -> None:
        feature_date = date(2026, 6, 1)
        label = _score("A", feature_date, label=1.0)
        prediction = _prediction(label)
        with self.assertRaisesRegex(ValueError, "labels are missing"):
            evaluate_cross_sectional_predictions(
                [prediction],
                [],
                config=CrossSectionalEvaluationConfig(model_key="missing", top_ns=(1,)),
            )
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            evaluate_cross_sectional_predictions(
                [replace(prediction, ticker="WRONG")],
                [label],
                config=CrossSectionalEvaluationConfig(model_key="mismatch", top_ns=(1,)),
            )
