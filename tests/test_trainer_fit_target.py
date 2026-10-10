"""Drawdown-penalty-as-fit-target switch on `SignalTrainer`.

Covers the `trainer_fit_on_risk_adjusted` option end to end:

* the default (False) fits the executable ``net_return`` -- zero regression;
* True fits the same label's ``risk_adjusted_return``
  (``net - lambda*|path_drawdown|``), with a fallback to the net target when a
  label profile does not carry it;
* the run config / artifact metadata record `fit_target`, `drawdown_penalty`
  and `random_seed`;
* the OOS metric keys are unchanged in both modes
  (``mean_risk_adjusted_return`` + ``mean_net_return``).
"""

from __future__ import annotations

import math
from contextlib import ExitStack
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.services.stock_selection.training_window import (
    select_training_window as _real_select_training_window,
)
from app.services.trainer import SignalTrainer


def _fixture_rows(symbols: list[str], sessions: int) -> tuple[list[dict], list[str]]:
    """Deterministic oscillating prices so drawdown penalties are non-zero."""

    start = date(2025, 1, 1)
    dates = [(start + timedelta(days=index)).isoformat() for index in range(sessions)]
    rows: list[dict] = []
    for symbol in symbols:
        for index, trade_date in enumerate(dates):
            close = 100.0 * (1.0 + 0.02 * math.sin(index / 4.0))
            rows.append(
                {
                    "symbol": symbol,
                    "date": trade_date,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": 1_000_000 + index * 1_000,
                }
            )
    return rows, dates


class FitTargetHarness:
    """Runs one real (small) walk-forward fit, capturing the fit-target inputs."""

    def __init__(self, overrides: dict | None = None) -> None:
        self.overrides = overrides or {}
        self.raw_target_calls: list[list[float]] = []
        self.windows: list[list[dict]] = []
        self.create_config: dict = {}
        self.merged: dict = {}
        self.artifact_meta: dict = {}

    def run(self) -> "FitTargetHarness":
        symbols = [f"6000{index:02d}.SH" for index in range(15)]
        sessions = 340
        rows, dates = _fixture_rows(symbols, sessions)
        trainer = SignalTrainer()
        if self.overrides:
            trainer.settings = trainer.settings.model_copy(update=self.overrides)

        configs: list[dict] = []
        _original_winsorize = SignalTrainer._winsorize_targets

        def _record_winsorize(_self, values, config):  # noqa: ANN001
            self.raw_target_calls.append([float(value) for value in values])
            return _original_winsorize(_self, values, config)

        def _recording_select_train_window(train_pool, *, policy, feature_count):  # noqa: ANN001
            window, audit = _real_select_training_window(
                train_pool, policy=policy, feature_count=feature_count
            )
            self.windows.append(list(window))
            return window, audit

        def _record_merge_config(_run_id: int, payload: dict) -> None:
            configs.append(dict(payload))

        model = MagicMock()
        model.feature_importances_ = [1.0] * len(trainer._feature_names(lookback_days=3))
        repositories = (
            "SymbolRepository",
            "ModelRunRepository",
            "PredictionWriteRepository",
            "PredictionDetailRepository",
            "PredictionExplanationRepository",
        )
        with ExitStack() as stack:
            stack.enter_context(patch("app.services.trainer.SessionLocal"))
            mocks = {
                name: stack.enter_context(patch(f"app.services.trainer.{name}")).return_value
                for name in repositories
            }
            mocks["SymbolRepository"].list_symbols.return_value = [
                SimpleNamespace(ticker=symbol, id=index + 1)
                for index, symbol in enumerate(symbols)
            ]
            repo = mocks["ModelRunRepository"]
            repo.create_run.return_value = SimpleNamespace(id=9876)
            repo.merge_config.side_effect = _record_merge_config
            stack.enter_context(
                patch(
                    "app.services.trainer.get_latest_lake_trade_date",
                    return_value=dates[-1],
                )
            )
            stack.enter_context(
                patch.object(SignalTrainer, "_winsorize_targets", _record_winsorize)
            )
            stack.enter_context(
                patch(
                    "app.services.trainer.select_training_window",
                    _recording_select_train_window,
                )
            )
            stack.enter_context(
                patch(
                    "app.services.trainer.lgb",
                    SimpleNamespace(LGBMRegressor=MagicMock(return_value=model)),
                )
            )
            for name, value in {
                "_load_symbol_feature_context": {},
                "_load_oos_score_calibration": ([], {}),
                "_build_score_calibration": [],
                "_build_detail_row": {},
                "_build_lightgbm_explanations": [],
            }.items():
                stack.enter_context(patch.object(trainer, name, return_value=value))
            stack.enter_context(
                patch.object(
                    trainer,
                    "_predict_scores",
                    side_effect=lambda _model, values: [1.0] * len(values),
                )
            )
            persist = stack.enter_context(
                patch.object(trainer, "_persist_model_outputs", return_value=9876)
            )
            trainer._train_lightgbm(
                run_name="fit-target-fixture",
                signal_type="momentum",
                lookback_days=3,
                normalized_tickers=set(symbols),
                market="CN",
                universe="fixture",
                rows=rows,
            )

        self.create_config = dict(repo.create_run.call_args.kwargs["config"])
        self.merged = {}
        for payload in configs:
            self.merged.update(payload)
        self.artifact_meta = dict(persist.call_args.kwargs["model_metadata"])
        return self


def _expected_net_targets(window: list[dict]) -> list[float]:
    trainer = SignalTrainer()
    return [trainer._safe_float(sample.get("target")) for sample in window]


def _expected_risk_adjusted_targets(window: list[dict]) -> list[float]:
    trainer = SignalTrainer()
    trainer.settings = trainer.settings.model_copy(
        update={"trainer_fit_on_risk_adjusted": True}
    )
    return [trainer._fit_target_value(sample) for sample in window]


class FitTargetConfigTests(TestCase):
    def test_default_fits_net_return(self) -> None:
        trainer = SignalTrainer()
        self.assertEqual("net_return", trainer._fit_target_config())
        sample = {
            "target": 0.10,
            "target_profile": {"risk_adjusted_return": 0.05, "net_return": 0.10},
        }
        self.assertEqual(0.10, trainer._fit_target_value(sample))

    def test_switch_on_fits_risk_adjusted_return(self) -> None:
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_fit_on_risk_adjusted": True}
        )
        self.assertEqual("risk_adjusted_return", trainer._fit_target_config())
        sample = {
            "target": 0.10,
            "target_profile": {"risk_adjusted_return": 0.05, "net_return": 0.10},
        }
        self.assertEqual(0.05, trainer._fit_target_value(sample))

    def test_switch_on_falls_back_to_net_target_without_profile_field(self) -> None:
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_fit_on_risk_adjusted": True}
        )
        sample = {"target": 0.10, "target_profile": {}}
        self.assertEqual(0.10, trainer._fit_target_value(sample))

    def test_default_random_seed_is_42(self) -> None:
        trainer = SignalTrainer()
        self.assertEqual(42, trainer._resolve_random_seed())
        trainer.settings = trainer.settings.model_copy(
            update={"trainer_random_seed": 7}
        )
        self.assertEqual(7, trainer._resolve_random_seed())


class FitTargetTrainingTests(TestCase):
    def test_default_matrix_uses_net_return_zero_regression(self) -> None:
        harness = FitTargetHarness().run()

        self.assertEqual("net_return", harness.create_config["fit_target"])
        self.assertEqual("net_return", harness.artifact_meta["fit_target"])
        self.assertEqual(0.25, harness.create_config["drawdown_penalty"])
        self.assertEqual(42, harness.create_config["random_seed"])

        self.assertTrue(harness.raw_target_calls)
        self.assertEqual(len(harness.raw_target_calls), len(harness.windows))
        for raw_targets, window in zip(
            harness.raw_target_calls, harness.windows, strict=True
        ):
            self.assertEqual(_expected_net_targets(window), raw_targets)
            # Prices oscillate, so the penalized target must differ somewhere;
            # this proves the switch is what selects the fit target.
        self.assertTrue(
            any(
                _expected_net_targets(window) != _expected_risk_adjusted_targets(window)
                for window in harness.windows
            )
        )

    def test_switch_on_matrix_uses_risk_adjusted_return(self) -> None:
        harness = FitTargetHarness(
            overrides={"trainer_fit_on_risk_adjusted": True, "trainer_random_seed": 7}
        ).run()

        self.assertEqual("risk_adjusted_return", harness.create_config["fit_target"])
        self.assertEqual("risk_adjusted_return", harness.artifact_meta["fit_target"])
        self.assertEqual(0.25, harness.create_config["drawdown_penalty"])
        self.assertEqual(7, harness.create_config["random_seed"])

        self.assertTrue(harness.raw_target_calls)
        for raw_targets, window in zip(
            harness.raw_target_calls, harness.windows, strict=True
        ):
            self.assertEqual(_expected_risk_adjusted_targets(window), raw_targets)

    def test_oos_metric_keys_are_unchanged_in_both_modes(self) -> None:
        for overrides in ({}, {"trainer_fit_on_risk_adjusted": True}):
            harness = FitTargetHarness(overrides=overrides).run()
            evaluation = harness.merged["oos_evaluation"]
            self.assertIn("mean_risk_adjusted_return", evaluation)
            self.assertIn("mean_net_return", evaluation)
            self.assertEqual(
                "walk_forward_oos_evaluation_v1", evaluation["schema_version"]
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
