"""Protect supported training dispatch while removing the retired implementation."""
from unittest import TestCase
from unittest.mock import patch

from app.services.trainer import SignalTrainer


class TrainerRetirementTests(TestCase):
    def test_retired_implementations_are_absent(self):
        self.assertFalse(hasattr(SignalTrainer, "_train_baseline"))
        self.assertFalse(hasattr(SignalTrainer, "_baseline_explanations"))
        self.assertFalse(hasattr(SignalTrainer, "_safe_price_series"))

    def test_retired_names_still_fail_explicitly_without_fitting(self):
        trainer = SignalTrainer()
        with patch.object(trainer, "_load_rows", return_value=[{"symbol": "FIXTURE"}]), \
             patch.object(trainer, "_train_lightgbm") as fit:
            for name in ("baseline", "local_baseline", " BASELINE "):
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "retired"):
                    trainer.train(run_name="fixture", model_type=name)
            fit.assert_not_called()

    def test_supported_aliases_preserve_dispatch_and_arguments(self):
        trainer = SignalTrainer()
        rows = [{"symbol": "FIXTURE"}]
        aliases = {
            "lightgbm": "lightgbm", "lightgbm_multifactor": "lightgbm", "lgbm": "lightgbm",
            "xgboost": "xgboost", "xgboost_multifactor": "xgboost", "xgb": "xgboost",
            "catboost": "catboost", "catboost_multifactor": "catboost", "cat": "catboost",
        }
        with patch.object(trainer, "_load_rows", return_value=rows), \
             patch.object(trainer, "_train_lightgbm", return_value=17) as fit:
            for market in ("CN", "US", "HK"):
                for alias, family in aliases.items():
                    with self.subTest(market=market, alias=alias):
                        result = trainer.train(run_name="fixture", model_type=alias,
                            signal_type="momentum", lookback_days=3, market=market,
                            tickers=["fixture", "FIXTURE"], universe="fixture_pool")
                        self.assertEqual(result, 17)
                        fit.assert_called_once_with(run_name="fixture", signal_type="momentum",
                            lookback_days=3, normalized_tickers={"FIXTURE"}, market=market,
                            universe="fixture_pool", rows=rows, model_family=family)
                        fit.reset_mock()

    def test_unknown_model_does_not_fall_back(self):
        trainer = SignalTrainer()
        with patch.object(trainer, "_load_rows", return_value=[{"symbol": "FIXTURE"}]), \
             patch.object(trainer, "_train_lightgbm") as fit:
            with self.assertRaisesRegex(RuntimeError, "Unsupported model_type"):
                trainer.train(run_name="fixture", model_type="unknown_model")
            fit.assert_not_called()
