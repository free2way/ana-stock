from __future__ import annotations

from unittest import TestCase

from app.services.stock_selection.factor_pipeline import FactorDirection
from app.services.stock_selection.factor_sets import (
    get_research_factor_set,
    research_factor_sets,
)
from app.services.stock_selection.production_research import (
    ProductionResearchRunConfig,
    p1_feature_config_for_factor_set,
)


class ResearchFactorSetTests(TestCase):
    def test_candidate_sets_are_frozen_unique_and_versioned(self) -> None:
        factor_sets = research_factor_sets()

        self.assertEqual(
            {
                "original_v1",
                "stable_low_risk_v1",
                "mean_reversion_v1",
                "decorrelated_v1",
                "trend_head_v1",
                "quality_value_shadow_v1",
                "p1_residual_momentum_v1",
                "p1_liquidity_crowding_v1",
                "p1_open_intraday_structure_v1",
            },
            set(factor_sets),
        )
        versions = {item.version() for item in factor_sets.values()}
        self.assertEqual(len(factor_sets), len(versions))
        self.assertTrue(all(item.version() == item.version() for item in factor_sets.values()))

    def test_mean_reversion_reverses_diagnosed_trend_directions(self) -> None:
        factor_set = get_research_factor_set("mean_reversion_v1")
        directions = {item.name: item.direction for item in factor_set.specs}

        self.assertEqual(FactorDirection.LOWER_BETTER, directions["momentum_60d"])
        self.assertEqual(FactorDirection.LOWER_BETTER, directions["ma20_slope_5d"])
        self.assertEqual(FactorDirection.LOWER_BETTER, directions["close_location_20d"])
        self.assertNotIn("dollar_volume_log", directions)

    def test_trend_head_set_freezes_training_only_audit_winners(self) -> None:
        factor_set = get_research_factor_set("trend_head_v1")
        directions = {item.name: item.direction for item in factor_set.specs}

        self.assertEqual(
            {
                "momentum_5d",
                "momentum_60d",
                "price_vs_ma20",
                "ma20_slope_5d",
            },
            set(directions),
        )
        self.assertTrue(
            all(direction == FactorDirection.HIGHER_BETTER for direction in directions.values())
        )
        self.assertTrue(all(item.weight == 1.0 for item in factor_set.specs))

    def test_decorrelated_set_avoids_formally_detected_pairs(self) -> None:
        names = set(get_research_factor_set("decorrelated_v1").feature_names)
        correlated_pairs = (
            {"ma20_slope_5d", "momentum_20d"},
            {"close_location_20d", "price_vs_ma20"},
            {"intraday_range_5d", "volatility_20d"},
            {"momentum_20d", "price_vs_ma20"},
        )

        self.assertTrue(all(len(names & pair) <= 1 for pair in correlated_pairs))

    def test_quality_value_shadow_set_is_frozen_and_fundamental_only(self) -> None:
        factor_set = get_research_factor_set("quality_value_shadow_v1")
        directions = {item.name: item.direction for item in factor_set.specs}

        self.assertEqual(FactorDirection.HIGHER_BETTER, directions["positive_earnings_yield"])
        self.assertEqual(FactorDirection.HIGHER_BETTER, directions["roe_avg_3y"])
        self.assertEqual(FactorDirection.LOWER_BETTER, directions["debt_to_assets"])
        self.assertNotIn("momentum_20d", directions)

    def test_p1_information_families_are_separate_research_only_candidates(self) -> None:
        residual = get_research_factor_set("p1_residual_momentum_v1")
        liquidity = get_research_factor_set("p1_liquidity_crowding_v1")
        structure = get_research_factor_set("p1_open_intraday_structure_v1")

        self.assertEqual(
            {"residual_momentum_20d", "industry_relative_momentum_20d"},
            set(residual.feature_names),
        )
        self.assertEqual(
            {"notional_volume_zscore_20d", "amihud_illiquidity_20d_per_million"},
            set(liquidity.feature_names),
        )
        self.assertEqual({"opening_gap_1d", "intraday_return_1d"}, set(structure.feature_names))
        self.assertTrue(all("Research-only" in item.thesis for item in (residual, liquidity, structure)))
        self.assertIsNotNone(p1_feature_config_for_factor_set(residual))
        self.assertIsNone(p1_feature_config_for_factor_set(get_research_factor_set("original_v1")))

    def test_research_config_rejects_unknown_factor_set(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown factor_set_key"):
            ProductionResearchRunConfig(market="CN", factor_set_key="missing")

    def test_research_config_requires_enough_history_for_walk_forward(self) -> None:
        with self.assertRaisesRegex(ValueError, "feature warm-up"):
            ProductionResearchRunConfig(
                market="CN",
                horizons=(3,),
                prediction_date_count=60,
                minimum_training_dates=120,
                history_limit_per_symbol=300,
            )

    def test_required_history_includes_universe_training_and_oos_windows(self) -> None:
        config = ProductionResearchRunConfig(
            market="CN",
            horizons=(3,),
            prediction_date_count=60,
            minimum_training_dates=120,
            history_limit_per_symbol=320,
        )

        self.assertEqual(305, config.minimum_required_history_sessions)

    def test_research_config_rejects_unknown_or_duplicate_models(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported model"):
            ProductionResearchRunConfig(market="CN", model_keys=("random_forest",))
        with self.assertRaisesRegex(ValueError, "duplicates"):
            ProductionResearchRunConfig(market="CN", model_keys=("ridge", "ridge"))
        with self.assertRaisesRegex(ValueError, "must not be negative"):
            ProductionResearchRunConfig(market="CN", round_trip_cost_bps=-1.0)
