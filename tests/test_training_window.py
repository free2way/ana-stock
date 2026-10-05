from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import numpy as np
import polars as pl

from app.core.config import Settings
from app.services.stock_selection.training_window import (
    TrainingWindowBlocked,
    TrainingWindowPolicy,
    estimate_training_fit_bytes,
    select_training_window,
)
from app.services.trainer import SignalTrainer
from scripts.audit_training_window_capacity import audit_capacity


def samples(days=260, per_day=3):
    return [{"symbol": f"FIXTURE{i:03}", "trade_date": (date(2025, 1, 1) + timedelta(days=day)).isoformat(),
             "features": {"f": day}, "target": 0.01} for day in range(days) for i in range(per_day)]


class TrainingWindowTests(TestCase):
    def test_exact_newest_252_dates_complete_groups_and_order_independent(self):
        rows = samples()
        selected, audit = select_training_window(rows, policy=TrainingWindowPolicy(), feature_count=28)
        reverse, _ = select_training_window(list(reversed(rows)), policy=TrainingWindowPolicy(), feature_count=28)
        self.assertEqual(selected, reverse)
        self.assertEqual(252 * 3, len(selected))
        self.assertEqual(252, audit["selected_date_count"])
        self.assertEqual({3}, set(audit["date_sample_counts"].values()))
        self.assertEqual(rows[8 * 3]["trade_date"], audit["start_date"])

    def test_oversized_window_blocks_instead_of_selecting_fewer_dates(self):
        with self.assertRaises(TrainingWindowBlocked) as ctx:
            select_training_window(samples(), policy=TrainingWindowPolicy(max_rows=750), feature_count=28)
        self.assertEqual("complete_window_exceeds_row_budget", ctx.exception.audit["block_reason"])
        self.assertEqual(252, ctx.exception.audit["selected_date_count"])
        self.assertEqual(756, ctx.exception.audit["selected_sample_count"])

    def test_insufficient_dates_and_empty_pool_block(self):
        for rows, reason in ((samples(251), "insufficient_mature_feature_dates"), ([], "empty_training_pool")):
            with self.assertRaises(TrainingWindowBlocked) as ctx:
                select_training_window(rows, policy=TrainingWindowPolicy(), feature_count=28)
            self.assertEqual(reason, ctx.exception.audit["block_reason"])

    def test_memory_estimate_checked_before_matrix_allocation(self):
        with self.assertRaises(TrainingWindowBlocked) as ctx:
            select_training_window(samples(), policy=TrainingWindowPolicy(max_estimated_fit_bytes=1000), feature_count=28)
        self.assertEqual("complete_window_exceeds_estimated_fit_budget", ctx.exception.audit["block_reason"])
        self.assertEqual(756 * 28 * 8, ctx.exception.audit["dense_matrix_bytes"])

    def test_explicit_short_research_window_not_marked_252(self):
        selected, audit = select_training_window(samples(), policy=TrainingWindowPolicy(date_count=60), feature_count=28)
        self.assertEqual(180, len(selected))
        self.assertFalse(audit["meets_252_date_research_window"])

    def test_legacy_us_budget_keeps_whole_dates_and_rejects_oversized_single_date(self):
        policy = TrainingWindowPolicy(mode="legacy_row_budget_v1", max_rows=7)
        selected, audit = select_training_window(samples(4), policy=policy, feature_count=2)
        self.assertEqual(6, len(selected))
        self.assertIsNone(audit["requested_date_count"])
        with self.assertRaises(TrainingWindowBlocked):
            select_training_window(samples(1, 8), policy=policy, feature_count=2)

    def test_invalid_config_and_dates_rejected(self):
        for kwargs in ({"mode": "silent_fallback"}, {"max_rows": True}, {"date_count": 0}, {"max_estimated_fit_bytes": -1}):
            with self.assertRaises(ValueError):
                TrainingWindowPolicy(**kwargs)
        with self.assertRaises(ValueError):
            select_training_window([{"trade_date": "20250101"}], policy=TrainingWindowPolicy(), feature_count=28)

    def test_cn_and_us_settings_are_separate(self):
        trainer = SignalTrainer()
        trainer.settings = Settings(_env_file=None)
        self.assertEqual("complete_dates_v1", trainer._training_window_policy("CN").mode)
        self.assertEqual(252, trainer._training_window_policy("CN").date_count)
        self.assertEqual("legacy_row_budget_v1", trainer._training_window_policy("US").mode)
        self.assertEqual(120000, trainer._training_window_policy("US").max_rows)

    def test_calibration_predicts_in_bounded_batches_and_rejects_wrong_count(self):
        trainer = SignalTrainer()
        rows = samples(50, 100)
        with patch.object(trainer, "_predict_scores", side_effect=lambda model, matrix: [0.01] * len(matrix)) as predict:
            buckets = trainer._build_score_calibration(model=object(), train_window=rows, feature_names=["f"])
            self.assertEqual([4096, 904], [len(call.args[1]) for call in predict.call_args_list])
            self.assertEqual(5000, sum(bucket["sample_count"] for bucket in buckets))
        with patch.object(trainer, "_predict_scores", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "count"):
                trainer._build_score_calibration(model=object(), train_window=rows, feature_names=["f"])

    def test_predict_supports_compact_numpy_matrix(self):
        model = SimpleNamespace(predict=lambda matrix: [0.1] * len(matrix))
        self.assertEqual([0.1, 0.1], SignalTrainer()._predict_scores(model, np.zeros((2, 1))))

    def test_capacity_preflight_is_read_only_and_uses_shared_estimate(self):
        with TemporaryDirectory() as directory:
            paths = []
            dates = ["2026-09-18", "2026-09-21"]
            for trade_date in dates:
                path = Path(directory) / f"date={trade_date}" / "part.parquet"
                path.parent.mkdir()
                pl.DataFrame({"date": [trade_date, trade_date], "symbol": ["A", "B"]}).write_parquet(path)
                paths.append(path)
            report = audit_capacity(market="CN", dates=dates, paths=paths, requested_date_count=2,
                                    feature_count=28, max_rows=4, max_estimated_fit_bytes=10_000)
            self.assertEqual("READY", report["status"])
            self.assertEqual(4, report["row_count"])
            self.assertEqual(estimate_training_fit_bytes(sample_count=4, feature_count=28)["estimated_fit_bytes"],
                             report["estimated_fit_bytes"])
            self.assertFalse(report["production_training_started"])
            self.assertFalse(report["postgres_accessed"])
            self.assertEqual(2, len(report["source_files"]))

    def test_capacity_preflight_blocks_short_or_over_budget_source(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "date=2026-09-21" / "part.parquet"
            path.parent.mkdir()
            pl.DataFrame({"date": ["2026-09-21"] * 2, "symbol": ["A", "B"]}).write_parquet(path)
            report = audit_capacity(market="CN", dates=["2026-09-21"], paths=[path], requested_date_count=2,
                                    feature_count=28, max_rows=1, max_estimated_fit_bytes=100)
            self.assertEqual("BLOCKED", report["status"])
            self.assertEqual(
                {"insufficient_lake_partitions", "raw_rows_exceed_row_budget", "raw_rows_exceed_estimated_fit_budget"},
                set(report["block_reasons"]),
            )


class ScheduledWindowIntegrationTests(TestCase):
    def run_fixture(self, *, days=340, max_rows=1_500_000, expected_failure=None):
        rows = samples(days, 100)
        calendar = sorted({row["trade_date"] for row in rows})
        for row in rows:
            index = (date.fromisoformat(row["trade_date"]) - date(2025, 1, 1)).days
            mature = calendar[index+6] if index+6 < days else None
            row.update(target=0.01 if mature else None, label_end_date=mature, label_available_date=mature)
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(update={"trainer_cn_window_dates": 252, "trainer_cn_window_max_rows": max_rows})
        model = MagicMock(feature_importances_=[1.0])
        with ExitStack() as stack:
            stack.enter_context(patch("app.services.trainer.SessionLocal"))
            repositories = {name: stack.enter_context(patch(f"app.services.trainer.{name}")).return_value for name in (
                "SymbolRepository", "ModelRunRepository", "PredictionWriteRepository", "PredictionDetailRepository", "PredictionExplanationRepository")}
            repositories["SymbolRepository"].list_symbols.return_value = [SimpleNamespace(ticker=f"FIXTURE{i:03}", id=i+1) for i in range(100)]
            repo = repositories["ModelRunRepository"]
            repo.create_run.return_value = SimpleNamespace(id=9999)
            stack.enter_context(patch("app.services.trainer.get_latest_lake_trade_date", return_value=calendar[-1]))
            stack.enter_context(patch("app.services.trainer.lgb", SimpleNamespace(LGBMRegressor=MagicMock(return_value=model))))
            for name, value in {"_feature_names": ["f"], "_load_symbol_feature_context": {}, "_build_lightgbm_samples": rows,
                "_load_oos_score_calibration": ([], {}), "_build_score_calibration": [], "_build_detail_row": {}, "_build_lightgbm_explanations": []}.items():
                stack.enter_context(patch.object(trainer, name, return_value=value))
            stack.enter_context(patch.object(trainer, "_predict_scores", side_effect=lambda _, matrix: [0.01] * len(matrix)))
            summary = stack.enter_context(patch.object(trainer, "_summarize_target_profile", return_value={}))
            persist = stack.enter_context(patch.object(trainer, "_persist_model_outputs", return_value=9999))
            args = dict(run_name="fixture", signal_type="momentum", lookback_days=3, market="CN", universe="fixture", rows=[],
                        normalized_tickers={f"FIXTURE{i:03}" for i in range(100)})
            if expected_failure:
                with self.assertRaises(TrainingWindowBlocked) as ctx:
                    trainer._train_lightgbm(**args)
                self.assertEqual(expected_failure, ctx.exception.audit["block_reason"])
                model.fit.assert_not_called()
                persist.assert_not_called()
                repo.complete_run.assert_called_once_with(9999, status="failed", artifact_path=None)
                self.assertEqual(expected_failure, repo.merge_config.call_args.args[1]["training_window_blocker"]["block_reason"])
            else:
                self.assertEqual(9999, trainer._train_lightgbm(**args))
        return repo, model, persist, summary

    def test_scheduled_loop_uses_252_dates_and_artifact_describes_last_actual_fit(self):
        repo, model, persist, summary = self.run_fixture()
        audits = repo.merge_config.call_args.args[1]["training_window_audits"]
        self.assertEqual(len(audits), model.fit.call_count)
        for audit, fit in zip(audits, model.fit.call_args_list):
            self.assertEqual(252, audit["date_count"])
            self.assertEqual((25200, 1), fit.args[0].shape)
            self.assertEqual(np.float64, fit.args[0].dtype)
            self.assertEqual(25200, audit["window_selection"]["selected_sample_count"])
            self.assertLess(audit["end_date"], audit["prediction_date"])
        self.assertEqual(audits[0]["start_date"], repo.create_run.call_args.kwargs["train_start"])
        self.assertEqual(audits[-1]["end_date"], max(row["trade_date"] for row in summary.call_args.args[0]))
        self.assertEqual(audits, persist.call_args.kwargs["model_metadata"]["training_window_audits"])

    def test_short_history_fails_before_fit_and_does_not_publish(self):
        self.run_fixture(days=50, expected_failure="insufficient_mature_feature_dates")

    def test_row_budget_fails_before_fit_without_partial_window(self):
        self.run_fixture(max_rows=25000, expected_failure="complete_window_exceeds_row_budget")
