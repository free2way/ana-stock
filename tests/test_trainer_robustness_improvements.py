"""Training-side robustness improvements on `SignalTrainer`.

Covers the four requested changes:
1. point-in-time tradable-universe filtering of the training rows,
2. net-return winsorization + a robust (Huber) regression objective,
3. drawdown-penalized `risk_adjusted_return` reused by the OOS metric,
4. the horizon-sized embargo and the per-trade-date cross-sectional
   feature transform.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from unittest import TestCase, skipIf
from unittest.mock import patch

import numpy as np

from app.services.execution_costs import FillCostModel
from app.services.stock_selection.executable_outcomes import (
    ExecutionEligibility,
    confirmed_outcome,
)
from app.services.stock_selection.labels import PriceBar
from app.services.trainer import SignalTrainer, lgb


def _session_rows(
    symbol: str,
    sessions: int,
    *,
    close: float = 10.0,
    volume: float = 6_000_000.0,
    start: date = date(2026, 1, 5),
) -> list[dict]:
    rows: list[dict] = []
    for index in range(sessions):
        rows.append(
            {
                "symbol": symbol,
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": volume,
            }
        )
    return rows


class UniverseFilterTests(TestCase):
    def test_history_and_adv_rules_are_point_in_time(self) -> None:
        rows = _session_rows("600000.SS", 160, volume=1_000_000.0)
        for row in rows[140:]:
            row["volume"] = 6_000_000.0

        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_universe_min_history_sessions": 120}
        )
        kept, stats = trainer._apply_pit_universe_filter(rows, market="CN")

        self.assertTrue(stats["applied"])
        self.assertEqual("CN", stats["rule_market"])
        self.assertEqual(160, stats["input_rows"])
        self.assertEqual(119, stats["exclusion_counts"]["insufficient_history"])
        # Short-history rows also carry a thin trailing ADV; a naive whole-series
        # (future-borrowing) average would have admitted the later ones. The
        # count therefore spans every thin-ADV row, not only post-warmup rows.
        self.assertEqual(155, stats["exclusion_counts"]["low_adv20"])
        self.assertEqual(5, stats["output_rows"])
        self.assertEqual(5, len(kept))
        self.assertNotIn(rows[130]["date"], {row["date"] for row in kept})
        self.assertNotIn(rows[154]["date"], {row["date"] for row in kept})
        self.assertIn(rows[155]["date"], {row["date"] for row in kept})
        self.assertIn(rows[159]["date"], {row["date"] for row in kept})

    def test_appending_future_sessions_never_changes_earlier_decisions(self) -> None:
        rows = _session_rows("600000.SS", 160, volume=1_000_000.0)
        for row in rows[140:]:
            row["volume"] = 6_000_000.0
        future = _session_rows(
            "600000.SS", 20, start=date(2026, 1, 5) + timedelta(days=160)
        )
        for row in future:
            row["volume"] = 6_000_000.0

        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_universe_min_history_sessions": 120}
        )
        base_kept, _ = trainer._apply_pit_universe_filter(rows, market="CN")
        extended_kept, _ = trainer._apply_pit_universe_filter(rows + future, market="CN")

        base_dates = {row["date"] for row in base_kept}
        extended_dates = {
            row["date"] for row in extended_kept if row["date"] <= rows[-1]["date"]
        }
        self.assertEqual(base_dates, extended_dates)

    def test_signal_day_limit_up_lock_and_low_price_are_excluded(self) -> None:
        rows = _session_rows("600001.SS", 130)
        rows[125]["close"] = rows[124]["close"] * 1.10
        rows[125]["high"] = rows[125]["close"] * 1.01
        rows[129]["close"] = 0.5
        rows[129]["high"] = 0.51
        rows[129]["low"] = 0.49

        trainer = SignalTrainer()
        kept, stats = trainer._apply_pit_universe_filter(rows, market="CN")
        kept_dates = {row["date"] for row in kept}

        self.assertEqual(1, stats["exclusion_counts"]["signal_day_limit_up_locked"])
        self.assertEqual(1, stats["exclusion_counts"]["low_price"])
        self.assertNotIn(rows[125]["date"], kept_dates)
        self.assertNotIn(rows[129]["date"], kept_dates)
        self.assertIn(rows[124]["date"], kept_dates)
        # ST / suspension state is absent from the lake schema and must be
        # reported as unapplied rather than claimed.
        self.assertEqual(["st_security", "suspended"], stats["unapplied_rules"])

    def test_disabled_switch_returns_every_row(self) -> None:
        rows = _session_rows("600002.SS", 130, close=0.5, volume=1_000.0)
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_universe_filter_enabled": False}
        )
        kept, stats = trainer._apply_pit_universe_filter(rows, market="CN")
        self.assertFalse(stats["applied"])
        self.assertFalse(stats["enabled"])
        self.assertEqual(len(rows), len(kept))
        self.assertEqual("trainer_universe_filter_enabled=false", stats["skipped_reason"])

    def test_market_is_inferred_from_tickers_when_unset(self) -> None:
        rows = _session_rows("600003.SS", 130)
        trainer = SignalTrainer()
        _kept, stats = trainer._apply_pit_universe_filter(rows, market=None)
        self.assertTrue(stats["applied"])
        self.assertEqual("CN", stats["rule_market"])

    def test_load_rows_applies_filter_and_records_stats(self) -> None:
        rows = _session_rows("600020.SS", 130)
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_universe_min_history_sessions": 120}
        )
        with patch("app.services.trainer.load_lake_rows", return_value=list(rows)), patch.object(
            trainer, "_attach_adjusted_basis", return_value=0
        ):
            loaded = trainer._load_rows(market="CN")
        self.assertEqual(11, len(loaded))
        stats = trainer._universe_filter_stats
        self.assertIsNotNone(stats)
        self.assertTrue(stats["applied"])
        self.assertEqual(
            119, stats["exclusion_counts"]["insufficient_history"]
        )
        self.assertEqual(130, stats["input_rows"])
        self.assertTrue(stats["history_rule"]["enabled"])
        self.assertEqual(120, stats["history_rule"]["effective_min_history_sessions"])

    def test_history_rule_defaults_to_disabled_for_trainer(self) -> None:
        # 1a: the lake slice handed to training is shorter than the shared
        # universe warm-up, so the trainer's history rule is off by default
        # while the liquidity rule still applies.
        rows = _session_rows("600021.SS", 130)

        trainer = SignalTrainer()
        self.assertEqual(0, trainer.settings.trainer_universe_min_history_sessions)
        kept, stats = trainer._apply_pit_universe_filter(rows, market="CN")

        self.assertTrue(stats["applied"])
        self.assertFalse(stats["history_rule"]["enabled"])
        self.assertEqual(0, stats["rules"]["min_history_sessions"])
        self.assertEqual(120, stats["history_rule"]["universe_default_min_history_sessions"])
        self.assertNotIn("insufficient_history", stats["exclusion_counts"])
        # Every row survives: liquidity passes and history is not enforced.
        self.assertEqual(130, stats["output_rows"])
        self.assertEqual(130, len(kept))

        trainer.settings = trainer.settings.model_copy(
            update={"trainer_universe_min_history_sessions": 120}
        )
        kept_enabled, enabled_stats = trainer._apply_pit_universe_filter(rows, market="CN")
        self.assertEqual(119, enabled_stats["exclusion_counts"]["insufficient_history"])
        self.assertEqual(11, len(kept_enabled))


class WinsorizeAndObjectiveTests(TestCase):
    def test_winsorize_clips_to_linear_quantile_bounds(self) -> None:
        trainer = SignalTrainer()
        config = {
            "enabled": True,
            "lower_quantile": 0.025,
            "upper_quantile": 0.975,
        }
        values = [float(index) for index in range(41)]
        clipped, audit = trainer._winsorize_targets(values, config)

        self.assertEqual(1.0, audit["lower_bound"])
        self.assertEqual(39.0, audit["upper_bound"])
        self.assertEqual(2, audit["winsorized_count"])
        self.assertEqual(1.0, clipped[0])
        self.assertEqual(39.0, clipped[-1])
        self.assertEqual(5.0, clipped[5])

    def test_winsorize_disabled_is_identity(self) -> None:
        trainer = SignalTrainer()
        values = [float(index) for index in range(41)]
        clipped, audit = trainer._winsorize_targets(
            values, {"enabled": False, "lower_quantile": 0.025, "upper_quantile": 0.975}
        )
        self.assertEqual(values, clipped)
        self.assertEqual(0, audit["winsorized_count"])

    def test_winsorize_quantiles_resolve_per_market(self) -> None:
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={
                "trainer_cn_label_winsorize_lower": 0.05,
                "trainer_us_label_winsorize_lower": 0.01,
            }
        )
        self.assertEqual(0.05, trainer._label_winsorize_config("CN")["lower_quantile"])
        self.assertEqual(0.01, trainer._label_winsorize_config("US")["lower_quantile"])

    @skipIf(lgb is None, "lightgbm is not installed")
    def test_default_objective_is_huber(self) -> None:
        trainer = SignalTrainer()
        config = trainer._resolve_objective()
        self.assertEqual("huber", config["objective"])
        params = trainer._build_regressor("lightgbm", config).get_params()
        self.assertEqual("huber", params["objective"])
        self.assertEqual(0.9, params["alpha"])

    @skipIf(lgb is None, "lightgbm is not installed")
    def test_l2_objective_restores_legacy_regression(self) -> None:
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_objective": "l2"}
        )
        config = trainer._resolve_objective()
        self.assertEqual("l2", config["objective"])
        params = trainer._build_regressor("lightgbm", config).get_params()
        self.assertEqual("regression", params["objective"])


class DrawdownPenaltyTests(TestCase):
    def _bars(self) -> tuple[list[PriceBar], list[date]]:
        dates = [date(2026, 9, 8) + timedelta(days=index) for index in range(6)]
        closes = (10.0, 10.5, 11.0, 10.5, 10.0, 11.0)
        bars = [
            PriceBar(day, 10.0, 11.0, 9.5, close, 1_000_000.0)
            for day, close in zip(dates, closes, strict=True)
        ]
        return bars, dates

    def test_confirmed_outcome_applies_penalty_and_keeps_net(self) -> None:
        bars, dates = self._bars()
        cost = FillCostModel(8, 12)
        label = confirmed_outcome(
            bars,
            signal_date=dates[0],
            trading_dates=dates,
            horizon_days=5,
            market="CN",
            cost_model=cost,
            eligibility=ExecutionEligibility(True, True),
            drawdown_penalty=0.25,
        )
        expected_net = cost.round_trip(label.entry_price, label.exit_price)["net_return"]
        self.assertAlmostEqual(expected_net, label.net_return, places=12)
        self.assertAlmostEqual(-0.05, label.path_drawdown, places=12)
        self.assertAlmostEqual(
            expected_net - 0.25 * 0.05, label.risk_adjusted_return, places=12
        )

    def test_confirmed_outcome_default_preserves_prior_behaviour(self) -> None:
        bars, dates = self._bars()
        label = confirmed_outcome(
            bars,
            signal_date=dates[0],
            trading_dates=dates,
            horizon_days=5,
            market="CN",
            cost_model=FillCostModel(8, 12),
            eligibility=ExecutionEligibility(True, True),
        )
        self.assertAlmostEqual(label.net_return, label.risk_adjusted_return, places=12)

    def test_trainer_label_profile_penalizes_and_keeps_net(self) -> None:
        rows = _session_rows("600010.SS", 6)
        closes = (10.0, 10.5, 11.0, 10.5, 10.0, 11.0)
        for row, close in zip(rows, closes, strict=True):
            row["close"] = close
            row["high"] = close + 0.5
            row["low"] = 9.5
        trainer = SignalTrainer()
        target, profile, _version = trainer._build_executable_net_return_target(
            symbol_rows=rows, index=0, horizon_days=5, market="CN", limit_band_pct=10.0
        )

        self.assertIsNotNone(target)
        self.assertEqual(0.25, profile["drawdown_penalty"])
        self.assertAlmostEqual(target, profile["net_return"], places=12)
        self.assertAlmostEqual(-0.05, profile["path_drawdown"], places=12)
        self.assertAlmostEqual(
            target - 0.25 * 0.05, profile["risk_adjusted_return"], places=12
        )
        # The OOS metric must cite the penalized return, not the raw net.
        metric = SignalTrainer._oos_metric_value(
            {"target": target, "target_profile": profile}
        )
        self.assertAlmostEqual(profile["risk_adjusted_return"], metric, places=12)
        self.assertLess(metric, target)


class EmbargoAndFeatureTransformTests(TestCase):
    def test_embargo_defaults_to_horizon_and_is_configurable(self) -> None:
        trainer = SignalTrainer()
        self.assertEqual(6, trainer._resolve_embargo_sessions(6))
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_embargo_sessions": 0}
        )
        self.assertEqual(0, trainer._resolve_embargo_sessions(6))
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_embargo_sessions": 10}
        )
        self.assertEqual(10, trainer._resolve_embargo_sessions(6))
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_embargo_sessions": -1}
        )
        with self.assertRaises(RuntimeError):
            trainer._resolve_embargo_sessions(6)

    def test_cross_sectional_transform_is_deterministic(self) -> None:
        samples = [
            {"trade_date": "2026-01-05", "features": {"a": value}}
            for value in (1.0, 2.0, 3.0)
        ]
        trainer = SignalTrainer()
        first = trainer._cross_sectional_feature_matrix(samples, ["a"])
        second = trainer._cross_sectional_feature_matrix(samples, ["a"])
        self.assertTrue(np.array_equal(first, second))
        self.assertAlmostEqual(0.6745, abs(float(first[0][0])), places=4)
        self.assertAlmostEqual(0.0, float(first[1][0]), places=12)
        self.assertAlmostEqual(-float(first[0][0]), float(first[2][0]), places=12)

    def test_cross_sectional_transform_uses_only_same_date(self) -> None:
        early = [
            {"trade_date": "2026-01-05", "features": {"a": value}}
            for value in (1.0, 2.0, 3.0)
        ]
        later = [
            {"trade_date": "2026-01-06", "features": {"a": value}}
            for value in (100.0, 200.0, 300.0)
        ]
        trainer = SignalTrainer()
        only_early = trainer._cross_sectional_feature_matrix(early, ["a"])
        combined = trainer._cross_sectional_feature_matrix(early + later, ["a"])
        self.assertTrue(np.array_equal(only_early, combined[:3]))

        perturbed = [
            {"trade_date": "2026-01-06", "features": {"a": value}}
            for value in (-1e6, 1e6, 3e6)
        ]
        after = trainer._cross_sectional_feature_matrix(early + perturbed, ["a"])
        self.assertTrue(np.array_equal(only_early, after[:3]))

    def test_cross_sectional_transform_keeps_degenerate_cross_sections(self) -> None:
        trainer = SignalTrainer()
        single = [{"trade_date": "2026-01-05", "features": {"a": 5.0}}]
        self.assertEqual(
            [[5.0]], trainer._cross_sectional_feature_matrix(single, ["a"]).tolist()
        )
        constant = [
            {"trade_date": "2026-01-05", "features": {"a": 7.0}} for _ in range(3)
        ]
        self.assertEqual(
            [[7.0], [7.0], [7.0]],
            trainer._cross_sectional_feature_matrix(constant, ["a"]).tolist(),
        )

    def test_cross_sectional_transform_can_be_disabled(self) -> None:
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_feature_transform_enabled": False}
        )
        samples = [
            {"trade_date": "2026-01-05", "features": {"a": 1.0}},
            {"trade_date": "2026-01-05", "features": {"a": 3.0}},
        ]
        self.assertEqual(
            [[1.0], [3.0]],
            trainer._cross_sectional_feature_matrix(samples, ["a"]).tolist(),
        )
        self.assertFalse(trainer._feature_transform_config()["enabled"])

    def test_feature_transform_config_records_method(self) -> None:
        config = SignalTrainer()._feature_transform_config()
        self.assertTrue(config["enabled"])
        self.assertEqual("cross_sectional_winsor_mad_zscore", config["method"])
        self.assertEqual("per_trade_date", config["scope"])
        self.assertTrue(math.isfinite(config["zscore_clip"]))


if __name__ == "__main__":
    import unittest

    unittest.main()
