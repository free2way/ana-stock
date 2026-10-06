"""EXCLUDE missing-factor semantics must reach the model matrix layer.

Legacy context: the cross-sectional pipeline already dropped unknown cells for
sparse families (``sentiment_v1``), but the LightGBM/Ridge matrices still read
``factor_values.get(name, 0.0)`` — turning every unknown cell into a fabricated
zero.  These tests pin the plumbed-through contract:

* ``EXCLUDE`` → NaN in the LightGBM matrix (native missing split), and
  training-window median + explicit missing indicator for Ridge;
* ``NEUTRAL_ZERO`` → the legacy ``0.0`` zero-fill, unchanged;
* out-of-coverage rows are filtered at assembly and reported in metadata.

Everything here is small-scale and DB-free.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from unittest import TestCase

from app.services.stock_selection.factor_baseline import (
    fit_ridge_factor_baseline,
    indicator_feature_names,
)
from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorObservation,
    FactorScore,
    FactorSpec,
    MissingFactorPolicy,
    model_feature_row,
    resolve_model_missing_policy,
)
from app.services.stock_selection.production_data import (
    build_production_research_dataset,
    filter_samples_to_sentiment_coverage,
)
from app.services.stock_selection.ranker import RankerConfig, fit_cross_sectional_ranker
from app.services.stock_selection.research_runner import (
    WalkForwardComparisonConfig,
    run_walk_forward_model_comparison,
)
from app.services.stock_selection.sample_builder import SampleBuildConfig
from app.services.stock_selection.schemas import LabeledSample
from app.services.stock_selection.sentiment_features import (
    FEATURE_NAMES,
    HithinkSentimentObservation,
)
from app.services.stock_selection.sentiment_research import (
    SentimentResearchFeatureConfig,
    build_sentiment_research_join,
)
from app.services.stock_selection.universe import SecurityMetadata, UniverseRuleConfig


def _labeled(
    ticker: str,
    feature_date: date,
    *,
    sentiment: float | None,
    price: float,
    label: float,
    horizon: int = 3,
) -> LabeledSample:
    features: dict[str, float] = {"price": price}
    if sentiment is not None:
        features["sentiment"] = sentiment
    return LabeledSample(
        sample_id=f"{feature_date.isoformat()}:{ticker}",
        market="US",
        ticker=ticker,
        feature_date=feature_date,
        label_start_date=feature_date + timedelta(days=1),
        label_end_date=feature_date + timedelta(days=horizon),
        label_available_date=feature_date + timedelta(days=horizon),
        horizon_days=horizon,
        label_value=label,
        features=features,
        dataset_version="missing-policy-fixture-v1",
    )


def _observation(ticker: str, feature_date: date, *, sentiment=None, price: float) -> FactorObservation:
    features: dict[str, float | None] = {"sentiment": sentiment, "price": price}
    return FactorObservation(
        observation_id=f"live:{ticker}",
        ticker=ticker,
        feature_date=feature_date,
        horizon_days=3,
        features=features,
    )


def _two_scores(policy: MissingFactorPolicy) -> dict[str, FactorScore]:
    rows = [
        FactorObservation("A", "A", date(2026, 9, 30), 3, {"sentiment": None, "price": 1.0}),
        FactorObservation("B", "B", date(2026, 9, 30), 3, {"sentiment": 2.0, "price": 2.0}),
    ]
    pipeline = CrossSectionalFactorPipeline(
        (FactorSpec("sentiment"), FactorSpec("price")), missing_policy=policy
    )
    return {item.sample_id: item for item in pipeline.transform(rows)}


class ModelFeatureRowTests(TestCase):
    def test_exclude_missing_factor_becomes_nan_not_zero(self) -> None:
        scores = _two_scores(MissingFactorPolicy.EXCLUDE)
        self.assertNotIn("sentiment", scores["A"].factor_values)
        row = model_feature_row(scores["A"], ("sentiment", "price"))
        self.assertTrue(math.isnan(row[0]))
        self.assertTrue(math.isfinite(row[1]))

    def test_neutral_zero_missing_factor_keeps_legacy_zero(self) -> None:
        scores = _two_scores(MissingFactorPolicy.NEUTRAL_ZERO)
        self.assertEqual(0.0, scores["A"].factor_values["sentiment"])
        row = model_feature_row(scores["A"], ("sentiment", "price"))
        self.assertEqual(0.0, row[0])

    def test_present_non_finite_cell_still_fails_closed(self) -> None:
        broken = FactorScore(
            sample_id="broken",
            ticker="broken",
            feature_date=date(2026, 9, 30),
            label_available_date=None,
            horizon_days=3,
            factor_values={"sentiment": math.inf, "price": 1.0},
            missing_factors=(),
            composite_score=0.0,
            cross_sectional_rank=0.5,
            label_value=None,
            missing_policy=MissingFactorPolicy.EXCLUDE.value,
        )
        with self.assertRaisesRegex(ValueError, "not finite"):
            model_feature_row(broken, ("sentiment", "price"))

    def test_mixed_missing_policies_fail_closed(self) -> None:
        exclude = next(iter(_two_scores(MissingFactorPolicy.EXCLUDE).values()))
        neutral = next(iter(_two_scores(MissingFactorPolicy.NEUTRAL_ZERO).values()))
        with self.assertRaisesRegex(ValueError, "single missing-factor policy"):
            resolve_model_missing_policy([exclude, neutral])


class LightGBMMatrixTests(TestCase):
    def _training(self) -> list[LabeledSample]:
        start = date(2026, 5, 1)
        samples: list[LabeledSample] = []
        for day_index in range(6):
            feature_date = start + timedelta(days=day_index * 7)
            for ticker_index in range(8):
                sentiment = None if ticker_index % 2 == 0 else float(ticker_index)
                samples.append(
                    _labeled(
                        f"S{ticker_index:02d}",
                        feature_date,
                        sentiment=sentiment,
                        price=float(10 - ticker_index) + day_index * 0.01,
                        label=float(ticker_index) + day_index * 0.05,
                    )
                )
        return samples

    def _pipeline(self, policy: MissingFactorPolicy) -> CrossSectionalFactorPipeline:
        return CrossSectionalFactorPipeline(
            (FactorSpec("sentiment"), FactorSpec("price")), missing_policy=policy
        )

    def test_lambdarank_fits_and_predicts_with_exclude_nan_cells(self) -> None:
        pipeline = self._pipeline(MissingFactorPolicy.EXCLUDE)
        model = fit_cross_sectional_ranker(
            pipeline.transform(self._training()),
            config=RankerConfig(
                feature_names=("sentiment", "price"),
                horizon_days=3,
                min_group_size=5,
                n_estimators=20,
                learning_rate=0.1,
                num_leaves=7,
                min_child_samples=2,
                subsample=1.0,
                colsample_bytree=1.0,
            ),
            prediction_date=date(2026, 7, 1),
        )
        self.assertEqual(MissingFactorPolicy.EXCLUDE.value, model.missing_policy)

        inference = pipeline.transform(
            [
                _observation("L0", date(2026, 7, 1), sentiment=None, price=1.0),
                _observation("L1", date(2026, 7, 1), sentiment=3.0, price=2.0),
            ]
        )
        predictions = model.predict(inference)
        self.assertEqual(2, len(predictions))
        self.assertTrue(all(math.isfinite(item.raw_score) for item in predictions))

    def test_lambdarank_rejects_inference_under_a_different_policy(self) -> None:
        exclude = self._pipeline(MissingFactorPolicy.EXCLUDE)
        model = fit_cross_sectional_ranker(
            exclude.transform(self._training()),
            config=RankerConfig(
                feature_names=("sentiment", "price"),
                horizon_days=3,
                min_group_size=5,
                n_estimators=10,
                min_child_samples=2,
                subsample=1.0,
                colsample_bytree=1.0,
            ),
            prediction_date=date(2026, 7, 1),
        )
        neutral = CrossSectionalFactorPipeline((FactorSpec("sentiment"), FactorSpec("price")))
        with self.assertRaisesRegex(ValueError, "missing-factor policy"):
            model.predict(
                neutral.transform([_observation("L0", date(2026, 7, 1), sentiment=1.0, price=1.0)])
            )


class RidgeMatrixTests(TestCase):
    def _exclude_scores(self) -> tuple[FactorScore, ...]:
        start = date(2026, 6, 1)
        samples: list[LabeledSample] = []
        for day_index in range(3):
            feature_date = start + timedelta(days=day_index)
            for ticker_index in range(4):
                samples.append(
                    _labeled(
                        f"R{ticker_index}",
                        feature_date,
                        sentiment=None if ticker_index == 0 else float(ticker_index),
                        price=float(4 - ticker_index) + day_index * 0.1,
                        label=float(ticker_index),
                    )
                )
        pipeline = CrossSectionalFactorPipeline(
            (FactorSpec("sentiment"), FactorSpec("price")),
            missing_policy=MissingFactorPolicy.EXCLUDE,
        )
        return pipeline.transform(samples)

    def test_ridge_exclude_imputes_median_and_adds_indicator_columns(self) -> None:
        scores = self._exclude_scores()
        model = fit_ridge_factor_baseline(
            scores,
            factor_names=("sentiment", "price"),
            horizon_days=3,
            prediction_date=date(2026, 7, 1),
            alpha=1.0,
        )
        self.assertEqual(MissingFactorPolicy.EXCLUDE.value, model.missing_policy)
        self.assertEqual(
            ("sentiment__missing", "price__missing"),
            indicator_feature_names(model.factor_names),
        )
        # One coefficient per factor plus one per missing indicator.
        self.assertEqual(4, len(model.coefficients))
        self.assertEqual(2, len(model.feature_medians))
        # Sample count is preserved: imputation keeps every row in the panel.
        self.assertEqual(len(scores), model.training_sample_count)

        predictions = model.predict(scores)
        self.assertEqual(len(scores), len(predictions))
        self.assertTrue(all(math.isfinite(item.raw_score) for item in predictions))

    def test_ridge_neutral_zero_keeps_legacy_width_and_zero_fill(self) -> None:
        start = date(2026, 6, 1)
        samples = [
            _labeled(
                f"N{index}",
                start,
                sentiment=None if index == 0 else float(index),
                price=float(index) + 1.0,
                label=float(index),
            )
            for index in range(3)
        ]
        neutral = CrossSectionalFactorPipeline((FactorSpec("sentiment"), FactorSpec("price")))
        scores = neutral.transform(samples)
        model = fit_ridge_factor_baseline(
            scores,
            factor_names=("sentiment", "price"),
            horizon_days=3,
            prediction_date=date(2026, 7, 1),
            alpha=1.0,
        )
        self.assertEqual(MissingFactorPolicy.NEUTRAL_ZERO.value, model.missing_policy)
        self.assertEqual((), model.feature_medians)
        self.assertEqual(2, len(model.coefficients))
        self.assertEqual(0.0, next(item for item in scores if item.ticker == "N0").factor_values["sentiment"])


class WalkForwardExcludeIntegrationTests(TestCase):
    """End-to-end (no DB) walk-forward run over an ``EXCLUDE`` sentiment panel."""

    def test_equal_weight_ridge_and_lambdarank_all_survive_unknown_cells(self) -> None:
        trading_dates = [date(2026, 1, 2) + timedelta(days=index) for index in range(18)]
        prediction_dates = trading_dates[10:14]
        samples: list[LabeledSample] = []
        for date_index in range(15):
            feature_date = trading_dates[date_index]
            for ticker_index in range(8):
                # A quarter of the cells are unknown even inside the panel.
                samples.append(
                    _labeled(
                        f"S{ticker_index:02d}",
                        feature_date,
                        sentiment=None if ticker_index in {0, 3} else float(ticker_index),
                        price=float(8 - ticker_index) + date_index * 0.02,
                        label=float(ticker_index) + date_index * 0.02,
                    )
                )
        pipeline = CrossSectionalFactorPipeline(
            (FactorSpec("sentiment"), FactorSpec("price")),
            missing_policy=MissingFactorPolicy.EXCLUDE,
        )
        result = run_walk_forward_model_comparison(
            samples,
            trading_dates=trading_dates,
            prediction_dates=prediction_dates,
            factor_pipeline=pipeline,
            ranker_config=RankerConfig(
                feature_names=("sentiment", "price"),
                horizon_days=3,
                min_group_size=5,
                n_estimators=25,
                learning_rate=0.1,
                num_leaves=7,
                min_child_samples=2,
                subsample=1.0,
                colsample_bytree=1.0,
            ),
            config=WalkForwardComparisonConfig(
                horizon_days=3,
                purge_sessions=3,
                minimum_training_dates=4,
                minimum_training_samples=24,
                ridge_alpha=1.0,
                top_ns=(2, 4),
                quantile_count=4,
                require_all_prediction_dates=True,
            ),
        )
        self.assertEqual(
            {"equal_weight", "ridge", "lambdarank"}, set(result.reports)
        )
        self.assertEqual(tuple(prediction_dates), result.common_evaluated_dates)
        self.assertTrue(
            all(
                fold.model_status["lambdarank"] == "success"
                and fold.model_status["ridge"] == "success"
                for fold in result.fold_audits
            )
        )
        for model_key, predictions in result.predictions.items():
            self.assertEqual(32, len(predictions), model_key)
            self.assertTrue(
                all(math.isfinite(item.raw_score) for item in predictions), model_key
            )
        # The matrix values differ from the legacy zero-fill only where a cell is
        # genuinely unknown, so the two contracts must not produce identical OOS
        # scores for the sparse column.
        neutral = CrossSectionalFactorPipeline(
            (FactorSpec("sentiment"), FactorSpec("price"))
        )
        neutral_result = run_walk_forward_model_comparison(
            samples,
            trading_dates=trading_dates,
            prediction_dates=prediction_dates,
            factor_pipeline=neutral,
            ranker_config=RankerConfig(
                feature_names=("sentiment", "price"),
                horizon_days=3,
                min_group_size=5,
                n_estimators=25,
                learning_rate=0.1,
                num_leaves=7,
                min_child_samples=2,
                subsample=1.0,
                colsample_bytree=1.0,
            ),
            config=WalkForwardComparisonConfig(
                horizon_days=3,
                purge_sessions=3,
                minimum_training_dates=4,
                minimum_training_samples=24,
                ridge_alpha=1.0,
                top_ns=(2, 4),
                quantile_count=4,
                require_all_prediction_dates=True,
            ),
        )
        exclude_ridge = {item.sample_id: item.raw_score for item in result.predictions["ridge"]}
        neutral_ridge = {
            item.sample_id: item.raw_score for item in neutral_result.predictions["ridge"]
        }
        self.assertNotEqual(exclude_ridge, neutral_ridge)


class SentimentCoverageFilterTests(TestCase):
    def _join(self):
        covered = (date(2026, 9, 29), date(2026, 9, 30))
        base = {("600000.SS", trade_date): {"momentum_5d": 1.0} for trade_date in covered}
        observations = [
            HithinkSentimentObservation(
                feature_name="limit_up_pool",
                trade_date=trade_date,
                provider="hithink",
                source_reference=f"hithink:limit_up_pool:{trade_date.isoformat()}",
                fetched_at="2026-09-30T16:05:00+08:00",
                data={"timestamp": 1, "item": [{"thscode": "600000.SH", "continue_day_cnt": 1}]},
            )
            for trade_date in covered
        ]
        join = build_sentiment_research_join(
            base,
            config=SentimentResearchFeatureConfig(enabled=True),
            observations=observations,
        )
        assert join is not None
        return join

    def _sample(self, feature_date: date) -> LabeledSample:
        return _labeled("600000.SS", feature_date, sentiment=None, price=1.0, label=0.1)

    def test_out_of_coverage_samples_are_filtered_and_counted(self) -> None:
        join = self._join()
        samples = [
            self._sample(date(2026, 9, 25)),  # before coverage
            self._sample(date(2026, 9, 29)),  # inside
            self._sample(date(2026, 9, 30)),  # inside
            self._sample(date(2026, 10, 1)),  # after coverage
        ]
        retained, result = filter_samples_to_sentiment_coverage(samples, join)
        assert result is not None
        self.assertEqual(2, len(retained))
        self.assertEqual(
            {date(2026, 9, 29), date(2026, 9, 30)},
            {item.feature_date for item in retained},
        )
        metadata = result.metadata()
        self.assertEqual(4, metadata["sentiment_input_sample_count"])
        self.assertEqual(2, metadata["sentiment_retained_sample_count"])
        self.assertEqual(2, metadata["sentiment_filtered_sample_count"])
        self.assertEqual("2026-09-29", metadata["sentiment_coverage_start"])
        self.assertTrue(metadata["sentiment_coverage_filter_applied"])

    def test_filter_is_a_noop_without_a_sentiment_join(self) -> None:
        samples = [self._sample(date(2026, 9, 25))]
        retained, result = filter_samples_to_sentiment_coverage(samples, None)
        self.assertIsNone(result)
        self.assertEqual(tuple(samples), retained)


class SentimentDatasetMetadataTests(TestCase):
    def _rows(self):
        dates = [date(2026, 1, 2) + timedelta(days=index) for index in range(90)]
        rows = []
        for ticker_index, ticker in enumerate(("600000.SS", "600001.SS")):
            for date_index, trade_date in enumerate(dates):
                close = 20.0 + ticker_index * 2.0 + date_index * 0.03
                rows.append(
                    {
                        "symbol": ticker,
                        "date": trade_date.isoformat(),
                        "open": close - 0.05,
                        "high": close + 0.20,
                        "low": close - 0.20,
                        "close": close,
                        "volume": 2_000_000.0,
                    }
                )
        return dates, rows

    def _dataset(self, *, with_sentiment: bool):
        dates, rows = self._rows()
        tickers = ("600000.SS", "600001.SS")
        metadata = {ticker: SecurityMetadata(ticker=ticker, listing_date=dates[0]) for ticker in tickers}
        kwargs = {}
        if with_sentiment:
            covered = dates[-10:]
            kwargs = {
                "sentiment_research_config": SentimentResearchFeatureConfig(enabled=True),
                "sentiment_observations": [
                    HithinkSentimentObservation(
                        feature_name="limit_up_pool",
                        trade_date=trade_date,
                        provider="hithink",
                        source_reference=f"hithink:limit_up_pool:{trade_date.isoformat()}",
                        fetched_at="2026-09-30T16:05:00+08:00",
                        data={"timestamp": 1, "item": [{"thscode": "600000.SH", "continue_day_cnt": 1}]},
                    )
                    for trade_date in covered
                ],
            }
        return build_production_research_dataset(
            rows,
            market="CN",
            metadata=metadata,
            industries={ticker: "TECH" for ticker in tickers},
            universe_rules=UniverseRuleConfig(
                market="CN",
                min_price=1.0,
                min_adv20=1_000_000.0,
                min_avg_volume20=100_000.0,
                min_history_sessions=20,
            ),
            sample_config=SampleBuildConfig(market="CN", horizons=(5,)),
            source_version="lake-fixture-v1",
            **kwargs,
        )

    def test_coverage_filter_stats_surface_in_dataset_metadata(self) -> None:
        from dataclasses import replace

        dataset = self._dataset(with_sentiment=True)
        self.assertIsNotNone(dataset.sentiment_feature_result)
        for name in FEATURE_NAMES:
            self.assertIn(name, dataset.feature_result.feature_names)

        retained, coverage_filter = filter_samples_to_sentiment_coverage(
            dataset.sample_result.samples, dataset.sentiment_feature_result
        )
        assert coverage_filter is not None
        self.assertLess(len(retained), len(dataset.sample_result.samples))
        self.assertGreater(coverage_filter.filtered_sample_count, 0)

        filtered_dataset = replace(
            dataset,
            sample_result=replace(
                dataset.sample_result,
                samples=retained,
                label_contract={
                    **dataset.sample_result.label_contract,
                    **coverage_filter.metadata(),
                },
            ),
        )
        surfaced = filtered_dataset.sentiment_metadata
        self.assertEqual(
            coverage_filter.filtered_sample_count,
            surfaced["sentiment_filtered_sample_count"],
        )
        self.assertEqual(
            len(dataset.sample_result.samples),
            surfaced["sentiment_input_sample_count"],
        )

    def test_dataset_without_sentiment_keeps_empty_metadata(self) -> None:
        self.assertEqual({}, self._dataset(with_sentiment=False).sentiment_metadata)
