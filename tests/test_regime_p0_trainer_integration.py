"""Exercise the scheduled trainer loop without database writes or real fitting."""
from contextlib import ExitStack
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.services.trainer import SignalTrainer
from app.services.stock_selection.training_weights import TRAINING_WEIGHT_POLICY


class TrainingWeightIntegrationTests(TestCase):
    def test_actual_fit_receives_date_balanced_weights_and_persists_each_fit_audit(self):
        dates = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(50)]
        tickers = [f"FIXTURE{i:03}" for i in range(100)]
        samples = [{"symbol": ticker, "trade_date": day,
                    "features": {"fixture_day": i},
                    "target": 0.01 if i + 6 < len(dates) else None,
                    "label_end_date": dates[i + 6] if i + 6 < len(dates) else None,
                    "label_available_date": dates[i + 6] if i + 6 < len(dates) else None}
                   for i, day in enumerate(dates) for ticker in tickers]
        model = MagicMock(feature_importances_=[1.0])
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(update={"trainer_cn_window_dates": 10})
        repositories = ("SymbolRepository", "ModelRunRepository", "PredictionWriteRepository",
                        "PredictionDetailRepository", "PredictionExplanationRepository")
        with ExitStack() as stack:
            stack.enter_context(patch("app.services.trainer.SessionLocal"))
            mocks = {name: stack.enter_context(patch(f"app.services.trainer.{name}")).return_value
                     for name in repositories}
            mocks["SymbolRepository"].list_symbols.return_value = [
                SimpleNamespace(ticker=ticker, id=i + 1) for i, ticker in enumerate(tickers)]
            repo = mocks["ModelRunRepository"]
            repo.create_run.return_value = SimpleNamespace(id=9876)
            stack.enter_context(patch("app.services.trainer.get_latest_lake_trade_date", return_value=dates[-1]))
            stack.enter_context(patch("app.services.trainer.lgb", SimpleNamespace(LGBMRegressor=MagicMock(return_value=model))))
            values = {"_feature_names": ["fixture_day"], "_load_symbol_feature_context": {},
                      "_build_lightgbm_samples": samples, "_load_oos_score_calibration": ([], {}),
                      "_build_score_calibration": [], "_build_detail_row": {},
                      "_build_lightgbm_explanations": []}
            for name, value in values.items():
                stack.enter_context(patch.object(trainer, name, return_value=value))
            stack.enter_context(patch.object(trainer, "_predict_scores", side_effect=lambda _, rows: [0.01] * len(rows)))
            persist = stack.enter_context(patch.object(trainer, "_persist_model_outputs", return_value=9876))
            run_id = trainer._train_lightgbm(run_name="fixture", signal_type="momentum", lookback_days=3,
                normalized_tickers=set(tickers), market="CN", universe="fixture", rows=[])

        self.assertEqual(9876, run_id)
        initial_config = repo.create_run.call_args.kwargs["config"]
        self.assertEqual(TRAINING_WEIGHT_POLICY, initial_config["training_weight_policy"])
        self.assertEqual(10, initial_config["training_window_policy"]["date_count"])
        # With the default executable label profile the run contract must carry
        # the confirmed next-open net-return target, never the legacy composite.
        self.assertEqual(
            "confirmed_next_open_fixed_exit_fill_cost_v2", initial_config["target_profile"]
        )
        self.assertEqual("executable_next_open_net_return", initial_config["score_semantics"])
        audit_config = repo.merge_config.call_args.args[1]
        audits = audit_config["training_window_audits"]
        self.assertGreater(len(audits), 1)
        self.assertEqual(len(model.fit.call_args_list), len(audits))
        for fit, audit in zip(model.fit.call_args_list, audits, strict=True):
            grouped = {}
            for features, weight in zip(fit.args[0], fit.kwargs["sample_weight"], strict=True):
                grouped.setdefault(features[0], set()).add(weight)
            self.assertTrue(all(len(weights) == 1 for weights in grouped.values()))
            self.assertEqual(len(grouped), audit["date_count"])
            self.assertEqual(10, audit["date_count"])
            self.assertEqual(len(fit.args[0]), audit["sample_count"])
            self.assertLess(audit["end_date"], audit["prediction_date"])
            self.assertFalse(audit["meets_252_session_research_window"])
        artifact_meta = persist.call_args.kwargs["model_metadata"]
        self.assertEqual(audits, artifact_meta["training_window_audits"])
        self.assertEqual(initial_config["score_contract_version"], artifact_meta["score_contract_version"])
