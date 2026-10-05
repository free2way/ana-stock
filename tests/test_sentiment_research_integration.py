"""Optional wiring of the forward-only ``sentiment_v1`` family into research data.

Covers the four hard problems of the integration:

1. price long history + sentiment last year: out-of-coverage cells are ``None``
   (never ``0``) and the coverage window / missing policy are recorded;
2. dual decision cutoffs: the pre-open path never sees the same-day EOD feature
   and cannot be enabled silently;
3. cross-sectional missing semantics: the sentiment family excludes unknown
   factors instead of scoring them as a neutral zero;
4. the switch is off by default, so an existing price-only dataset is unchanged.
"""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.services.stock_selection.experiment_framework import freeze_dataset_hash
from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline,
    FactorDirection,
    FactorObservation,
    FactorSpec,
    MissingFactorPolicy,
)
from app.services.stock_selection.factor_sets import (
    factor_pipeline_for_factor_set,
    original_price_factor_specs,
    sentiment_factor_set,
)
from app.services.stock_selection.production_data import build_production_research_dataset
from app.services.stock_selection.sample_builder import SampleBuildConfig
from app.services.stock_selection.sentiment_features import (
    FEATURE_NAMES,
    SENTIMENT_FACTOR_SET_KEY,
    HithinkSentimentObservation,
)
from app.services.stock_selection.sentiment_research import (
    SENTIMENT_COVERAGE_MISSING_POLICY,
    SentimentResearchError,
    SentimentResearchFeatureConfig,
    build_sentiment_research_join,
)
from app.services.stock_selection.universe import SecurityMetadata, UniverseRuleConfig


def _obs(
    name: str,
    day: str,
    item,
    *,
    slot: str | None = None,
    source_reference: str | None = None,
) -> HithinkSentimentObservation:
    return HithinkSentimentObservation(
        feature_name=name,
        trade_date=date.fromisoformat(day),
        provider="hithink",
        source_reference=source_reference or f"hithink:{name}:{day}",
        fetched_at="2026-09-30T16:05:00+08:00",
        data={"timestamp": 1, "item": item},
        slot=slot,
    )


def _base_features(dates, tickers=("600000.SS", "600001.SS")):
    return {
        (ticker, feature_date): {"momentum_5d": 1.0 + index}
        for index, ticker in enumerate(tickers)
        for feature_date in dates
    }


class SentimentResearchWindowTests(TestCase):
    def test_out_of_coverage_dates_are_none_and_metadata_records_coverage(self) -> None:
        before = date(2026, 9, 25)
        covered_start = date(2026, 9, 29)
        covered_end = date(2026, 9, 30)
        observations = [
            _obs(
                "limit_up_pool",
                "2026-09-29",
                [{"thscode": "600000.SH", "continue_day_cnt": 7}],
            ),
            _obs(
                "limit_up_pool",
                "2026-09-30",
                [{"thscode": "600000.SH", "continue_day_cnt": 1}],
            ),
        ]
        join = build_sentiment_research_join(
            _base_features((before, covered_start, covered_end)),
            config=SentimentResearchFeatureConfig(enabled=True),
            observations=observations,
        )
        assert join is not None

        out_of_coverage = join.features_by_key[("600000.SS", before)]
        for name in FEATURE_NAMES:
            with self.subTest(feature=name):
                self.assertIsNone(out_of_coverage[name])
        self.assertEqual(1.0, join.features_by_key[("600000.SS", covered_start)]["limit_up_flag"])
        self.assertEqual(7.0, join.features_by_key[("600000.SS", covered_start)]["limit_up_streak"])
        # Non-exhaustive families stay unknown rather than becoming a zero flag.
        self.assertIsNone(join.features_by_key[("600000.SS", covered_start)]["hot_rank"])

        metadata = join.metadata()
        self.assertEqual("2026-09-29", metadata["sentiment_coverage_start"])
        self.assertEqual("2026-09-30", metadata["sentiment_coverage_end"])
        self.assertEqual(SENTIMENT_COVERAGE_MISSING_POLICY, metadata["sentiment_missing_policy"])
        self.assertEqual("eod", metadata["sentiment_decision_cutoff"])
        self.assertFalse(metadata["sentiment_auction_path_enabled"])
        self.assertEqual("not_enabled", metadata["sentiment_auction_path_status"])
        self.assertEqual(SENTIMENT_FACTOR_SET_KEY, join.factor_set_key)
        self.assertEqual(tuple(FEATURE_NAMES), join.feature_names)

    def test_join_is_disabled_by_default_and_returns_none(self) -> None:
        self.assertIsNone(
            build_sentiment_research_join(
                _base_features((date(2026, 9, 30),)),
                config=None,
                observations=[],
            )
        )
        self.assertIsNone(
            build_sentiment_research_join(
                _base_features((date(2026, 9, 30),)),
                config=SentimentResearchFeatureConfig(),
                observations=[],
            )
        )

    def test_no_observations_means_empty_coverage_and_all_none(self) -> None:
        day = date(2026, 9, 30)
        join = build_sentiment_research_join(
            _base_features((day,)),
            config=SentimentResearchFeatureConfig(enabled=True),
            observations=[],
        )
        assert join is not None
        self.assertIsNone(join.coverage_start)
        self.assertIsNone(join.coverage_end)
        self.assertEqual(0, join.covered_date_count)
        for name in FEATURE_NAMES:
            self.assertIsNone(join.features_by_key[("600000.SS", day)][name])

    def test_base_feature_name_collision_fails_closed(self) -> None:
        with self.assertRaises(SentimentResearchError):
            build_sentiment_research_join(
                {(("600000.SS"), date(2026, 9, 30)): {"hot_rank": 1.0}},
                config=SentimentResearchFeatureConfig(enabled=True),
                observations=[],
            )


class SentimentDualCutoffTests(TestCase):
    def _observations(self):
        return [
            _obs(
                "limit_up_pool",
                "2026-09-29",
                [{"thscode": "600000.SH", "continue_day_cnt": 7}],
            ),
            _obs(
                "limit_up_pool",
                "2026-09-30",
                [{"thscode": "600000.SH", "continue_day_cnt": 1}],
            ),
            _obs(
                "auction_snapshot",
                "2026-09-30",
                [{"thscode": "600000.SH", "auction_pct": 0.35, "auction_volume_ratio": 1.12}],
                slot="final",
            ),
        ]

    def test_eod_cutoff_uses_same_day_eod_for_the_next_session_decision(self) -> None:
        dates = (date(2026, 9, 29), date(2026, 9, 30))
        eod = build_sentiment_research_join(
            _base_features(dates),
            config=SentimentResearchFeatureConfig(enabled=True, decision_cutoff="eod"),
            observations=self._observations(),
        )
        assert eod is not None
        self.assertEqual("16:00", eod.cutoff_time_local)
        self.assertEqual(1.0, eod.features_by_key[("600000.SS", dates[1])]["limit_up_streak"])
        # A 16:00 cutoff also admits the same-day pre-open auction snapshot.
        self.assertAlmostEqual(0.35, eod.features_by_key[("600000.SS", dates[1])]["auction_pct"])

    def test_preopen_cutoff_excludes_same_day_eod_and_needs_explicit_switch(self) -> None:
        dates = (date(2026, 9, 29), date(2026, 9, 30))
        preopen = build_sentiment_research_join(
            _base_features(dates),
            config=SentimentResearchFeatureConfig(
                enabled=True,
                decision_cutoff="preopen_auction",
                auction_path_enabled=True,
            ),
            observations=self._observations(),
        )
        assert preopen is not None
        self.assertEqual("09:25", preopen.cutoff_time_local)
        # Same-day end-of-day featured data is unknown at the pre-open cutoff.
        self.assertEqual(7.0, preopen.features_by_key[("600000.SS", dates[1])]["limit_up_streak"])
        self.assertAlmostEqual(
            0.35, preopen.features_by_key[("600000.SS", dates[1])]["auction_pct"]
        )
        self.assertEqual("enabled", preopen.metadata()["sentiment_auction_path_status"])

        with self.assertRaisesRegex(ValueError, "auction_path_enabled"):
            SentimentResearchFeatureConfig(enabled=True, decision_cutoff="preopen_auction")


class MissingFactorPolicyTests(TestCase):
    def _observation(self, ticker: str, features) -> FactorObservation:
        return FactorObservation(
            observation_id=ticker,
            ticker=ticker,
            feature_date=date(2026, 9, 30),
            horizon_days=5,
            features=features,
        )

    def test_exclude_policy_never_scores_an_unknown_factor_as_zero(self) -> None:
        rows = [
            self._observation("A", {"hot_rank": None, "limit_up_flag": 1.0}),
            self._observation("B", {"hot_rank": 3.0, "limit_up_flag": 0.0}),
            self._observation("C", {"hot_rank": 1.0, "limit_up_flag": 1.0}),
        ]
        pipeline = CrossSectionalFactorPipeline(
            (
                FactorSpec("hot_rank", FactorDirection.LOWER_BETTER),
                FactorSpec("limit_up_flag"),
            ),
            missing_policy=MissingFactorPolicy.EXCLUDE,
        )
        scores = {item.ticker: item for item in pipeline.transform(rows)}
        first = scores["A"]
        self.assertNotIn("hot_rank", first.factor_values)
        self.assertIn("hot_rank", first.missing_factors)
        self.assertEqual(MissingFactorPolicy.EXCLUDE.value, first.missing_policy)
        # A's composite is exactly its one present factor, not diluted by a fake zero.
        self.assertAlmostEqual(
            first.factor_values["limit_up_flag"], first.composite_score
        )

    def test_legacy_neutral_zero_default_is_preserved(self) -> None:
        rows = [
            self._observation("A", {"momentum": None, "risk": 1.0}),
            self._observation("B", {"momentum": 2.0, "risk": 2.0}),
        ]
        pipeline = CrossSectionalFactorPipeline((FactorSpec("momentum"), FactorSpec("risk")))
        first = next(item for item in pipeline.transform(rows) if item.ticker == "A")
        self.assertEqual(0.0, first.factor_values["momentum"])
        self.assertIn("momentum", first.missing_factors)
        self.assertEqual(MissingFactorPolicy.NEUTRAL_ZERO.value, first.missing_policy)

    def test_sentiment_family_selects_exclude_and_frozen_families_do_not(self) -> None:
        self.assertEqual(
            MissingFactorPolicy.EXCLUDE,
            factor_pipeline_for_factor_set(sentiment_factor_set()).missing_policy,
        )
        price_pipeline = CrossSectionalFactorPipeline(original_price_factor_specs())
        self.assertEqual(MissingFactorPolicy.NEUTRAL_ZERO, price_pipeline.missing_policy)


class SentimentDatasetWiringTests(TestCase):
    def setUp(self) -> None:
        self.dates = [date(2026, 1, 2) + timedelta(days=index) for index in range(90)]
        self.tickers = ("600000.SS", "600001.SS")
        self.rows = []
        for ticker_index, ticker in enumerate(self.tickers):
            for date_index, trade_date in enumerate(self.dates):
                close = 20.0 + ticker_index * 2.0 + date_index * (0.03 + ticker_index * 0.005)
                self.rows.append(
                    {
                        "symbol": ticker,
                        "date": trade_date.isoformat(),
                        "open": close - 0.05,
                        "high": close + 0.20,
                        "low": close - 0.20,
                        "close": close,
                        "volume": 2_000_000.0 + ticker_index * 100_000.0,
                    }
                )
        self.metadata = {
            ticker: SecurityMetadata(ticker=ticker, listing_date=self.dates[0])
            for ticker in self.tickers
        }
        self.rules = UniverseRuleConfig(
            market="CN",
            min_price=1.0,
            min_adv20=1_000_000.0,
            min_avg_volume20=100_000.0,
            min_history_sessions=20,
        )

    def _build(self, **kwargs):
        return build_production_research_dataset(
            self.rows,
            market="CN",
            metadata=self.metadata,
            industries={ticker: "TECH" for ticker in self.tickers},
            universe_rules=self.rules,
            sample_config=SampleBuildConfig(market="CN", horizons=(5,)),
            source_version="lake-fixture-v1",
            **kwargs,
        )

    def test_default_off_is_zero_change(self) -> None:
        baseline = self._build()
        disabled = self._build(
            sentiment_research_config=SentimentResearchFeatureConfig(enabled=False)
        )
        self.assertIsNone(baseline.sentiment_feature_result)
        self.assertIsNone(disabled.sentiment_feature_result)
        self.assertEqual({}, baseline.sentiment_metadata)
        self.assertEqual(baseline.feature_result, disabled.feature_result)
        self.assertEqual(baseline.sample_result, disabled.sample_result)
        for sample in baseline.sample_result.samples:
            self.assertFalse(set(sample.features) & set(FEATURE_NAMES))

    def test_enabled_sentiment_joins_inside_coverage_and_none_outside(self) -> None:
        coverage_dates = self.dates[-10:]
        observations = [
            _obs(
                "limit_up_pool",
                trade_date.isoformat(),
                [{"thscode": "600000.SH", "continue_day_cnt": float(index + 1)}],
            )
            for index, trade_date in enumerate(coverage_dates)
        ]
        dataset = self._build(
            sentiment_research_config=SentimentResearchFeatureConfig(enabled=True),
            sentiment_observations=observations,
        )
        self.assertIsNotNone(dataset.sentiment_feature_result)
        metadata = dataset.sentiment_metadata
        self.assertEqual(coverage_dates[0].isoformat(), metadata["sentiment_coverage_start"])
        self.assertEqual(coverage_dates[-1].isoformat(), metadata["sentiment_coverage_end"])
        self.assertEqual(SENTIMENT_COVERAGE_MISSING_POLICY, metadata["sentiment_missing_policy"])
        self.assertEqual("eod", metadata["sentiment_decision_cutoff"])
        self.assertTrue(
            dataset.feature_result.feature_set_version.startswith("price_plus_sentiment_v1:")
        )
        self.assertEqual(len(FEATURE_NAMES) + 12, len(dataset.feature_result.feature_names))

        samples = {
            (sample.ticker, sample.feature_date): sample
            for sample in dataset.sample_result.samples
        }
        early = samples[("600000.SS", self.dates[60])]
        for name in FEATURE_NAMES:
            with self.subTest(feature=name):
                self.assertIsNone(early.features[name])
        in_coverage = samples[("600000.SS", coverage_dates[0])]
        self.assertEqual(1.0, in_coverage.features["limit_up_flag"])
        self.assertEqual(1.0, in_coverage.features["limit_up_streak"])


class SentimentDatasetHashTests(TestCase):
    def _components(self, **overrides):
        components = {
            "factor_set_key": "sentiment_v1",
            "source_version": "hithink_sentiment_features_v1:fixture",
            "coverage_start": "2025-10-09",
            "coverage_end": "2026-10-09",
            "missing_policy": SENTIMENT_COVERAGE_MISSING_POLICY,
            "decision_cutoff": "eod",
            "cutoff_time_local": "16:00",
            "auction_path_enabled": False,
        }
        components.update(overrides)
        return components

    def _hash(self, sentiment=None):
        return freeze_dataset_hash(
            market="CN",
            factor_set_key=SENTIMENT_FACTOR_SET_KEY,
            label_version="label_v1",
            universe_version="universe_v1",
            source_version="lake-fixture-v1",
            sentiment=sentiment,
        )

    def test_default_hash_has_no_sentiment_components(self) -> None:
        baseline = self._hash()
        self.assertFalse(
            any(key.startswith("sentiment_") for key in baseline.components)
        )

    def test_sentiment_coverage_and_cutoff_enter_the_hash(self) -> None:
        baseline = self._hash()
        with_eod = self._hash(self._components())
        self.assertNotEqual(baseline.dataset_hash, with_eod.dataset_hash)
        self.assertEqual("2025-10-09", with_eod.components["sentiment_coverage_start"])
        self.assertEqual(SENTIMENT_COVERAGE_MISSING_POLICY, with_eod.components["sentiment_missing_policy"])
        self.assertEqual(with_eod.dataset_hash, self._hash(self._components()).dataset_hash)

        preopen = self._hash(
            self._components(
                decision_cutoff="preopen_auction",
                cutoff_time_local="09:25",
                auction_path_enabled=True,
            )
        )
        self.assertNotEqual(with_eod.dataset_hash, preopen.dataset_hash)

    def test_sentiment_hash_requires_explicit_coverage_keys(self) -> None:
        incomplete = self._components()
        incomplete.pop("coverage_start")
        with self.assertRaisesRegex(ValueError, "coverage_start"):
            self._hash(incomplete)

    def test_join_hash_components_round_trip(self) -> None:
        day = date(2026, 9, 30)
        join = build_sentiment_research_join(
            _base_features((day,)),
            config=SentimentResearchFeatureConfig(enabled=True),
            observations=[_obs("limit_up_pool", "2026-09-30", [{"thscode": "600000.SH"}])],
        )
        assert join is not None
        with_join = self._hash(join.hash_components())
        self.assertEqual(
            "2026-09-30", with_join.components["sentiment_coverage_start"]
        )
        self.assertEqual("eod", with_join.components["sentiment_decision_cutoff"])


class SentimentExperimentSwitchTests(TestCase):
    def _args(self, factor_set_key: str, metadata_path: Path | None = None) -> Namespace:
        return Namespace(
            factor_set_key=factor_set_key, sentiment_metadata_json=metadata_path
        )

    def _metadata(self, **overrides) -> dict:
        payload = {
            "factor_set_key": "sentiment_v1",
            "source_version": "hithink_sentiment_features_v1:fixture",
            "coverage_start": "2025-10-09",
            "coverage_end": "2026-10-09",
            "missing_policy": SENTIMENT_COVERAGE_MISSING_POLICY,
            "decision_cutoff": "eod",
            "cutoff_time_local": "16:00",
            "auction_path_enabled": False,
        }
        payload.update(overrides)
        return payload

    def test_sentiment_factor_set_without_metadata_fails_closed(self) -> None:
        from scripts.run_selection_experiment import _load_sentiment_components

        with self.assertRaisesRegex(SystemExit, "sentiment-metadata-json"):
            _load_sentiment_components(self._args("sentiment_v1"))
        # A price factor set needs no sentiment metadata and is untouched.
        self.assertIsNone(_load_sentiment_components(self._args("original_v1")))

    def test_metadata_requires_the_sentiment_factor_set(self) -> None:
        from scripts.run_selection_experiment import _load_sentiment_components

        with TemporaryDirectory() as directory:
            path = Path(directory) / "sentiment.json"
            path.write_text(json.dumps(self._metadata()), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "sentiment_v1"):
                _load_sentiment_components(self._args("original_v1", path))
            loaded = _load_sentiment_components(self._args("sentiment_v1", path))
        assert loaded is not None
        self.assertEqual("sentiment_v1", loaded["factor_set_key"])

