from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest import TestCase
from zoneinfo import ZoneInfo

from app.services.stock_selection.production_data import (
    PriceFeatureConfig,
    build_price_features,
    build_production_research_dataset,
    default_price_factor_specs,
    normalize_price_rows,
)
from app.services.stock_selection.feature_availability import (
    FUNDAMENTAL_FEATURE_NAMES,
    PointInTimeFeatureRecord,
)
from app.services.stock_selection.point_in_time_features import PointInTimeFeatureJoinConfig
from app.services.stock_selection.sample_builder import SampleBuildConfig
from app.services.stock_selection.universe import SecurityMetadata, UniverseRuleConfig


class ProductionResearchDataTests(TestCase):
    def setUp(self) -> None:
        self.dates = [date(2026, 1, 2) + timedelta(days=index) for index in range(75)]
        self.tickers = ("AAA", "BBB", "CCC", "DDD")
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
            market="US",
            min_price=3.0,
            min_adv20=1_000_000.0,
            min_avg_volume20=100_000.0,
            min_history_sessions=20,
        )

    def test_builds_point_in_time_features_universe_benchmarks_and_labels(self) -> None:
        dataset = build_production_research_dataset(
            self.rows,
            market="US",
            metadata=self.metadata,
            industries={"AAA": "TECH", "BBB": "TECH", "CCC": "TECH", "DDD": None},
            universe_rules=self.rules,
            sample_config=SampleBuildConfig(market="US", horizons=(1, 3, 5)),
            source_version="lake-fixture-v1",
        )

        self.assertEqual(75, len(dataset.trading_dates))
        self.assertEqual(4, dataset.feature_result.ticker_count)
        self.assertEqual(
            tuple(item.name for item in default_price_factor_specs()),
            dataset.feature_result.feature_names,
        )
        self.assertGreater(dataset.universe_result.included_count, 0)
        self.assertGreater(dataset.benchmark_result.industry_peer_count, 0)
        self.assertGreater(dataset.benchmark_result.market_fallback_count, 0)
        self.assertGreater(dataset.sample_result.eligible_count, 0)
        self.assertTrue(
            all(item.feature_date < item.label_available_date for item in dataset.sample_result.samples)
        )
        self.assertTrue(
            dataset.sample_result.dataset_version.startswith("stock_selection_dataset_v2:US:")
        )

    def test_future_price_change_does_not_change_past_features(self) -> None:
        bars, _ = normalize_price_rows(self.rows)
        baseline = build_price_features(bars)
        changed_rows = [dict(item) for item in self.rows]
        changed_rows[-1]["open"] = 9_999.0
        changed_rows[-1]["high"] = 10_001.0
        changed_rows[-1]["low"] = 9_998.0
        changed_rows[-1]["close"] = 10_000.0
        changed_bars, _ = normalize_price_rows(changed_rows)
        changed = build_price_features(changed_bars)

        past_key = ("DDD", self.dates[-2])
        self.assertEqual(
            baseline.features_by_key[past_key],
            changed.features_by_key[past_key],
        )

    def test_historical_industry_membership_is_resolved_per_signal_date(self) -> None:
        switch_date = self.dates[45]
        baseline_industries = {
            (ticker, trade_date): "TECH"
            for ticker in self.tickers
            for trade_date in self.dates
        }
        changed_industries = dict(baseline_industries)
        for trade_date in self.dates:
            if trade_date >= switch_date:
                changed_industries[("AAA", trade_date)] = "FINANCE"
                changed_industries[("CCC", trade_date)] = "FINANCE"
                changed_industries[("DDD", trade_date)] = "FINANCE"
                changed_industries[("BBB", trade_date)] = "TECH"
        common = {
            "rows": self.rows,
            "market": "US",
            "metadata": self.metadata,
            "universe_rules": self.rules,
            "sample_config": SampleBuildConfig(market="US", horizons=(1,)),
            "source_version": "lake-fixture-v1",
        }
        baseline = build_production_research_dataset(
            industries=baseline_industries,
            **common,
        )
        changed = build_production_research_dataset(
            industries=changed_industries,
            **common,
        )

        past_key = ("AAA", self.dates[40], 1)
        changed_key = ("AAA", self.dates[50], 1)
        self.assertEqual(
            baseline.benchmark_result.industry_returns[past_key],
            changed.benchmark_result.industry_returns[past_key],
        )
        self.assertNotEqual(
            baseline.benchmark_result.industry_returns[changed_key],
            changed.benchmark_result.industry_returns[changed_key],
        )

    def test_invalid_rows_are_audited_and_feature_window_is_validated(self) -> None:
        invalid = {
            "symbol": "BAD",
            "date": self.dates[0].isoformat(),
            "open": 10.0,
            "high": 9.0,
            "low": 8.0,
            "close": 10.0,
            "volume": 100.0,
        }
        bars, invalid_count = normalize_price_rows([*self.rows, invalid])
        self.assertEqual(1, invalid_count)
        self.assertNotIn("BAD", bars)
        with self.assertRaisesRegex(ValueError, "shorter than"):
            PriceFeatureConfig(minimum_history_sessions=20)

    def test_optional_point_in_time_features_merge_only_after_daily_gate(self) -> None:
        feature_date = self.dates[-10]
        available = datetime.combine(
            feature_date,
            time(hour=15),
            tzinfo=ZoneInfo("America/New_York"),
        )
        values = {
            "pe_ttm": 20.0,
            "dividend_yield": 0.02,
            "market_cap": 10_000_000_000.0,
            "roe_avg_3y": 0.12,
            "net_profit_yoy": 0.15,
            "revenue_yoy": 0.10,
            "debt_to_assets": 0.40,
        }
        records = [
            PointInTimeFeatureRecord(
                record_id=f"{ticker}:{feature_name}:v1",
                market="US",
                ticker=ticker,
                feature_name=feature_name,
                value=value,
                event_time=available,
                available_time=available,
                ingested_time=available,
                source="fixture",
                revision_id="v1",
            )
            for ticker in self.tickers
            for feature_name, value in values.items()
        ]
        dataset = build_production_research_dataset(
            self.rows,
            market="US",
            metadata=self.metadata,
            industries={ticker: "TECH" for ticker in self.tickers},
            universe_rules=self.rules,
            sample_config=SampleBuildConfig(market="US", horizons=(1,)),
            source_version="lake-fixture-v1",
            point_in_time_feature_records=records,
            point_in_time_feature_config=PointInTimeFeatureJoinConfig(
                market="US",
                timezone_name="America/New_York",
            ),
            point_in_time_source_version="pit-fixture-v1",
        )
        self.assertIsNotNone(dataset.point_in_time_feature_result)
        assert dataset.point_in_time_feature_result is not None
        self.assertIn(feature_date, dataset.point_in_time_feature_result.enabled_dates)
        sample = next(
            item for item in dataset.sample_result.samples if item.feature_date == feature_date
        )
        self.assertAlmostEqual(0.05, sample.features["positive_earnings_yield"])
        self.assertEqual(set(FUNDAMENTAL_FEATURE_NAMES), set(values))
