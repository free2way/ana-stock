from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from unittest import TestCase

from app.services.stock_selection.labels import PriceBar
from app.services.stock_selection.evaluation import (
    CrossSectionalEvaluationConfig,
    evaluate_cross_sectional_predictions,
)
from app.services.stock_selection.factor_baseline import equal_weight_predictions
from app.services.stock_selection.factor_pipeline import CrossSectionalFactorPipeline, FactorSpec
from app.services.stock_selection.sample_builder import SampleBuildConfig, build_training_samples
from app.services.stock_selection.schemas import UniverseSnapshot
from app.services.stock_selection.walk_forward import PointInTimeTrainingPool


class StockSelectionSampleBuilderTests(TestCase):
    def setUp(self) -> None:
        start = date(2026, 7, 1)
        self.dates = [start + timedelta(days=index) for index in range(7)]
        self.bars = [
            PriceBar(
                trade_date=trade_date,
                open=100.0 + index,
                high=102.0 + index,
                low=99.0 + index,
                close=101.0 + index,
                volume=1_000_000.0,
            )
            for index, trade_date in enumerate(self.dates)
        ]
        self.snapshot = UniverseSnapshot(
            snapshot_id="snapshot-1",
            market="US",
            trade_date=self.dates[1],
            ticker="AAA",
            included=True,
            source_as_of=datetime(2026, 7, 2, 20, 0, tzinfo=timezone.utc),
            universe_version="pit_universe_v1:US:fixture",
            price=101.0,
            adv20=100_000_000.0,
            volume=1_000_000.0,
        )
        self.config = SampleBuildConfig(
            market="US",
            horizons=(1, 3, 5),
            round_trip_cost_bps=20.0,
            drawdown_penalty=0.25,
            feature_set_version="fixture-features-v1",
        )

    def _build(self, **overrides):
        market_returns = {(self.dates[1], horizon): 0.01 for horizon in (1, 3, 5)}
        industry_returns = {("AAA", self.dates[1], horizon): 0.015 for horizon in (1, 3, 5)}
        params = {
            "trading_dates": self.dates,
            "bars_by_ticker": {"AAA": self.bars},
            "universe_snapshots": [self.snapshot],
            "features_by_key": {("AAA", self.dates[1]): {"momentum_20d": 0.12}},
            "market_returns": market_returns,
            "industry_returns": industry_returns,
            "entry_exclusions": {},
            "config": self.config,
        }
        params.update(overrides)
        return build_training_samples(**params)

    def test_builds_independent_horizon_samples_from_exact_market_sessions(self) -> None:
        result = self._build()
        by_horizon = {sample.horizon_days: sample for sample in result.samples}

        self.assertEqual({1, 3, 5}, set(by_horizon))
        self.assertEqual(self.dates[2], by_horizon[1].label_start_date)
        self.assertEqual(self.dates[2], by_horizon[1].label_end_date)
        self.assertEqual(self.dates[4], by_horizon[3].label_end_date)
        self.assertEqual(self.dates[6], by_horizon[5].label_end_date)
        self.assertEqual(3, result.eligible_count)
        self.assertTrue(result.dataset_version.startswith("stock_selection_dataset_v2:US:"))
        self.assertEqual(
            {
                "gross_return",
                "net_return",
                "market_excess_return",
                "industry_excess_return",
                "path_drawdown",
                "risk_adjusted_return",
                "profit_indicator",
            },
            set(by_horizon[1].label_components),
        )
        self.assertEqual(
            by_horizon[1].label_value,
            by_horizon[1].label_components["risk_adjusted_return"],
        )
        self.assertAlmostEqual(
            ((self.bars[2].close / self.bars[2].open) - 1.0) - 0.002 - 0.015
            - 0.25 * abs(min((self.bars[2].low / self.bars[2].open) - 1.0, 0.0)),
            by_horizon[1].label_value,
        )

    def test_missing_market_session_is_not_silently_replaced_by_later_bar(self) -> None:
        bars_without_entry = [bar for bar in self.bars if bar.trade_date != self.dates[2]]
        result = self._build(bars_by_ticker={"AAA": bars_without_entry})
        self.assertEqual(0, len(result.samples))
        self.assertEqual(3, result.exclusion_counts["missing_price_path"])

    def test_non_tradable_sample_is_retained_for_audit_but_excluded_from_training_pool(self) -> None:
        result = self._build(entry_exclusions={("AAA", self.dates[2]): "no_open_fill"})
        self.assertEqual(0, result.eligible_count)
        self.assertEqual(3, result.excluded_count)
        self.assertTrue(all(not sample.tradable for sample in result.samples))

        three_day = next(sample for sample in result.samples if sample.horizon_days == 3)
        pool = PointInTimeTrainingPool(
            [three_day],
            trading_dates=self.dates,
            feature_date=lambda item: item.feature_date,
            label_end_date=lambda item: item.label_end_date,
            label_available_date=lambda item: item.label_available_date,
            sample_id=lambda item: item.sample_id,
            purge_sessions=3,
        )
        self.assertEqual((), pool.advance(self.dates[5]))

    def test_relative_returns_are_required_by_default(self) -> None:
        result = self._build(market_returns={}, industry_returns={})
        self.assertEqual(0, len(result.samples))
        self.assertEqual(3, result.exclusion_counts["missing_market_return"])

    def test_golden_loss_is_identical_in_training_sample_and_evaluation(self) -> None:
        bars = [
            PriceBar(self.dates[0], 99.0, 100.0, 98.0, 99.0, 1_000_000.0),
            PriceBar(self.dates[1], 100.0, 110.0, 99.0, 105.0, 1_000_000.0),
            PriceBar(self.dates[2], 106.0, 110.0, 104.0, 108.0, 1_000_000.0),
            PriceBar(self.dates[3], 107.0, 109.0, 103.0, 107.0, 1_000_000.0),
            PriceBar(self.dates[4], 103.0, 104.0, 99.0, 101.0, 1_000_000.0),
            PriceBar(self.dates[5], 98.0, 100.0, 95.0, 96.0, 1_000_000.0),
        ]
        snapshot = replace(
            self.snapshot,
            trade_date=self.dates[0],
            source_as_of=datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
        )
        result = build_training_samples(
            trading_dates=self.dates[:6],
            bars_by_ticker={"AAA": bars},
            universe_snapshots=[snapshot],
            features_by_key={("AAA", self.dates[0]): {"momentum": 1.0}},
            market_returns={},
            industry_returns={},
            entry_exclusions={},
            config=SampleBuildConfig(
                market="US",
                horizons=(5,),
                round_trip_cost_bps=40.0,
                drawdown_penalty=0.0,
                require_relative_returns=False,
                target_mode="net_return",
                label_version="next_open_fixed_horizon_net_profit_v1",
                feature_set_version="golden-v1",
            ),
        )
        sample = result.samples[0]
        scores = CrossSectionalFactorPipeline((FactorSpec("momentum"),)).transform((sample,))
        report = evaluate_cross_sectional_predictions(
            equal_weight_predictions(scores),
            scores,
            config=CrossSectionalEvaluationConfig(
                model_key="golden-loss",
                top_ns=(1,),
                quantile_count=2,
            ),
        )

        self.assertAlmostEqual(-0.044, sample.label_value or 0.0, places=12)
        self.assertEqual(0.0, sample.label_components["profit_indicator"])
        self.assertAlmostEqual(-0.044, report.top_n_metrics[1].mean_label, places=12)
        self.assertEqual(0.0, report.top_n_metrics[1].positive_date_rate)
        self.assertEqual(0.0, report.top_n_metrics[1].mean_label_components["profit_indicator"])
