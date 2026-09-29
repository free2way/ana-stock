from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.stock_selection.factor_pipeline import FactorDirection, FactorScore, FactorSpec
from app.services.stock_selection.p1_factor_selection import (
    P1FactorSelectionConfig,
    select_p1_factors_training_only,
)
from app.services.stock_selection.p1_factors import (
    P1PriceVolumeFeatureConfig,
    P1_PRICE_VOLUME_FACTOR_NAMES,
    p1_factor_catalog_version,
    p1_factor_definitions,
)
from app.services.stock_selection.production_data import build_production_research_dataset
from app.services.stock_selection.sample_builder import SampleBuildConfig
from app.services.stock_selection.universe import SecurityMetadata, UniverseRuleConfig


class P1FactorEngineeringTests(TestCase):
    def setUp(self) -> None:
        self.dates = [date(2026, 1, 2) + timedelta(days=index) for index in range(80)]
        self.tickers = ("AAA", "BBB", "CCC", "DDD")
        self.rows = []
        for ticker_index, ticker in enumerate(self.tickers):
            for date_index, trade_date in enumerate(self.dates):
                close = 20.0 + ticker_index * 2.0 + date_index * (0.025 + ticker_index * 0.006)
                open_price = close * (1.0 - 0.002 * (ticker_index + 1))
                self.rows.append({
                    "symbol": ticker,
                    "date": trade_date.isoformat(),
                    "open": open_price,
                    "high": close * 1.01,
                    "low": open_price * 0.99,
                    "close": close,
                    "volume": 1_500_000.0 + ticker_index * 100_000.0 + date_index * 7_500.0,
                })
        self.metadata = {
            ticker: SecurityMetadata(ticker=ticker, listing_date=self.dates[0])
            for ticker in self.tickers
        }
        self.industries = {
            (ticker, trade_date): "TECH" if ticker != "DDD" else "FINANCE"
            for ticker in self.tickers
            for trade_date in self.dates
        }
        self.rules = UniverseRuleConfig(
            market="US", min_price=3.0, min_adv20=1_000_000.0,
            min_avg_volume20=100_000.0, min_history_sessions=20,
        )

    def _dataset(self, rows=None, *, include_p1=True):
        return build_production_research_dataset(
            rows or self.rows,
            market="US",
            metadata=self.metadata,
            industries=self.industries,
            universe_rules=self.rules,
            sample_config=SampleBuildConfig(market="US", horizons=(1, 3, 5)),
            source_version="p1-fixture-v1",
            p1_feature_config=P1PriceVolumeFeatureConfig() if include_p1 else None,
        )

    def test_metadata_freezes_units_availability_missing_policy_and_blocked_sources(self):
        definitions = p1_factor_definitions()
        for name in P1_PRICE_VOLUME_FACTOR_NAMES:
            item = definitions[name]
            self.assertEqual("READY_RESEARCH_ONLY", item.engineering_status)
            self.assertTrue(item.unit)
            self.assertTrue(item.available_at)
            self.assertTrue(item.missing_policy)
            self.assertTrue(item.source_requirement)
        self.assertEqual(
            "BLOCKED_PENDING_HISTORICAL_COVERAGE_AUDIT",
            definitions["northbound_net_inflow_5d"].engineering_status,
        )
        self.assertEqual("blocked_never_zero_fill", definitions["main_fund_net_amount_5d"].missing_policy)
        self.assertNotIn("turnover_rate_20d", definitions)
        self.assertTrue(p1_factor_catalog_version().startswith("p1_factor_catalog_v1:"))
        self.assertEqual(p1_factor_catalog_version(), p1_factor_catalog_version())

    def test_opt_in_dataset_builds_auditable_factors_and_preserves_missing_industry(self):
        dataset = self._dataset()
        self.assertIsNotNone(dataset.p1_feature_result)
        assert dataset.p1_feature_result is not None
        self.assertEqual(set(P1_PRICE_VOLUME_FACTOR_NAMES), set(dataset.p1_feature_result.feature_names))
        self.assertTrue(all(
            dataset.p1_feature_result.coverage_by_factor[name] > 0
            for name in P1_PRICE_VOLUME_FACTOR_NAMES
        ))
        finance_key = ("DDD", self.dates[-2])
        tech_key = ("AAA", self.dates[-2])
        self.assertNotIn(
            "industry_relative_momentum_20d",
            dataset.p1_feature_result.features_by_key[finance_key],
        )
        self.assertIn(
            "industry_relative_momentum_20d",
            dataset.p1_feature_result.features_by_key[tech_key],
        )
        self.assertGreater(dataset.p1_feature_result.missing_industry_peer_count, 0)
        base = self._dataset(include_p1=False)
        self.assertIsNone(base.p1_feature_result)
        self.assertTrue(set(P1_PRICE_VOLUME_FACTOR_NAMES).isdisjoint(base.feature_result.feature_names))

        static_only = {ticker: "TECH" for ticker in self.tickers}
        static_dataset = build_production_research_dataset(
            self.rows, market="US", metadata=self.metadata, industries=static_only,
            universe_rules=self.rules,
            sample_config=SampleBuildConfig(market="US", horizons=(1,)),
            source_version="p1-static-industry-fixture-v1",
            p1_feature_config=P1PriceVolumeFeatureConfig(),
        )
        assert static_dataset.p1_feature_result is not None
        self.assertEqual(
            0,
            static_dataset.p1_feature_result.coverage_by_factor["industry_relative_momentum_20d"],
        )

    def test_future_bar_change_cannot_change_past_p1_features(self):
        baseline = self._dataset()
        changed_rows = [dict(item) for item in self.rows]
        last = changed_rows[-1]
        last.update({"open": 9000.0, "high": 10001.0, "low": 8999.0, "close": 10000.0, "volume": 99_000_000.0})
        changed = self._dataset(changed_rows)
        key = ("DDD", self.dates[-2])
        assert baseline.p1_feature_result is not None and changed.p1_feature_result is not None
        self.assertEqual(
            baseline.p1_feature_result.features_by_key[key],
            changed.p1_feature_result.features_by_key[key],
        )


class P1TrainingOnlySelectionTests(TestCase):
    def _scores(self):
        start = date(2026, 1, 2)
        rows = []
        for day_index in range(24):
            feature_date = start + timedelta(days=day_index)
            for ticker_index in range(25):
                target = ticker_index / 100.0 + day_index / 10000.0
                missing = ("duplicate_good",) if ticker_index >= 20 else ()
                rows.append(FactorScore(
                    sample_id=f"train:{feature_date}:{ticker_index}",
                    ticker=f"S{ticker_index:02d}", feature_date=feature_date,
                    label_available_date=feature_date + timedelta(days=2), horizon_days=3,
                    factor_values={"good": target, "duplicate_good": target * 2, "bad": -target},
                    missing_factors=missing, composite_score=target,
                    cross_sectional_rank=ticker_index / 24, label_value=target,
                ))
        return rows

    @staticmethod
    def _specs():
        return (
            FactorSpec("good", FactorDirection.HIGHER_BETTER),
            FactorSpec("duplicate_good", FactorDirection.HIGHER_BETTER),
            FactorSpec("bad", FactorDirection.HIGHER_BETTER),
        )

    def test_selection_uses_only_mature_training_rows_and_clusters_redundancy(self):
        cutoff = date(2026, 2, 1)
        rows = self._scores()
        result = select_p1_factors_training_only(
            rows,
            specs=self._specs(),
            config=P1FactorSelectionConfig(
                training_cutoff_date=cutoff,
                minimum_cross_section_size=20,
                minimum_date_count=20,
            ),
        )
        self.assertEqual("READY_RESEARCH_ONLY", result.status)
        self.assertEqual(("good",), result.selected_factor_names)
        self.assertEqual("redundant_with:good", result.rejected_factor_reasons["duplicate_good"])
        self.assertEqual("review_direction", result.rejected_factor_reasons["bad"])
        self.assertTrue(all(item < cutoff for item in result.evaluated_dates))

        oos = FactorScore(
            sample_id="oos", ticker="OOS", feature_date=cutoff + timedelta(days=1),
            label_available_date=cutoff + timedelta(days=4), horizon_days=3,
            factor_values={"good": -999.0, "duplicate_good": 999.0, "bad": 999.0},
            missing_factors=(), composite_score=0.0, cross_sectional_rank=0.0, label_value=999.0,
        )
        repeated = select_p1_factors_training_only(
            [*rows, oos], specs=self._specs(),
            config=P1FactorSelectionConfig(
                training_cutoff_date=cutoff,
                minimum_cross_section_size=20,
                minimum_date_count=20,
            ),
        )
        self.assertEqual(result.selection_version, repeated.selection_version)

    def test_selection_fails_when_no_labels_are_mature_before_cutoff(self):
        row = self._scores()[0]
        with self.assertRaisesRegex(ValueError, "no label-mature training rows"):
            select_p1_factors_training_only(
                [row], specs=self._specs(),
                config=P1FactorSelectionConfig(training_cutoff_date=row.feature_date),
            )
