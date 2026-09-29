import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import polars as pl

from app.services.prediction_artifacts import (
    PREDICTION_FILE,
    PredictionArtifactWriter,
    PredictionPublicationLimitError,
    read_prediction_artifact_rows,
    read_prediction_explanation_artifact_rows,
    select_hot_explanation_rows,
    select_hot_prediction_rows,
    verify_prediction_artifact,
)
from app.services.prediction_archive_migration import compare_prediction_artifact_rows
from app.services.prediction_artifact_restore import restore_prediction_artifact_to_sqlite
from app.services.repository import PredictionExplanationRepository, PredictionRepository
from app.services.model_evaluation import _selected_prediction_rows
from app.services.trainer import SignalTrainer


class PredictionArtifactTests(unittest.TestCase):
    def _prediction_rows(self):
        return [
            {"symbol_id": 1, "trade_date": "2026-08-20", "score": 0.8, "rank_value": 1.0},
            {"symbol_id": 2, "trade_date": "2026-08-20", "score": 0.4, "rank_value": 2.0},
        ]

    def test_writer_publishes_verified_immutable_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = PredictionArtifactWriter(root=Path(directory))
            first = writer.write(
                model_run_id=7,
                market="CN",
                prediction_rows=self._prediction_rows(),
                detail_rows=[
                    {
                        "symbol_id": 1,
                        "trade_date": "2026-08-20",
                        "confidence": 0.7,
                        "signal_label": "BUY",
                    }
                ],
                explanation_rows=[
                    {
                        "symbol_id": 1,
                        "trade_date": "2026-08-20",
                        "feature_name": "momentum_20d",
                        "feature_value": 0.1,
                        "contribution": 0.2,
                        "direction": "positive",
                        "display_order": 1,
                    }
                ],
                model_metadata={"model": "fixture", "model_type": "lightgbm"},
            )
            verification = verify_prediction_artifact(first["artifact_path"])
            frame = pl.read_parquet(Path(first["artifact_path"]).parent / PREDICTION_FILE)

            self.assertEqual("success", verification["status"])
            self.assertEqual(2, first["row_count"])
            self.assertEqual(1, first["detail_row_count"])
            self.assertEqual(1, first["explanation_row_count"])
            self.assertEqual("fixture", first["model"])
            self.assertEqual(2, frame.height)

            second = writer.write(
                model_run_id=7,
                market="CN",
                prediction_rows=self._prediction_rows(),
                model_metadata={"model": "fixture", "model_type": "lightgbm"},
                detail_rows=[
                    {
                        "symbol_id": 1,
                        "trade_date": "2026-08-20",
                        "confidence": 0.7,
                        "signal_label": "BUY",
                    }
                ],
                explanation_rows=[
                    {
                        "symbol_id": 1,
                        "trade_date": "2026-08-20",
                        "feature_name": "momentum_20d",
                        "feature_value": 0.1,
                        "contribution": 0.2,
                        "direction": "positive",
                        "display_order": 1,
                    }
                ],
            )
            self.assertEqual(first["content_sha256"], second["content_sha256"])

    def test_writer_rejects_changed_content_for_published_run(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = PredictionArtifactWriter(root=Path(directory))
            writer.write(
                model_run_id=8,
                market="CN",
                prediction_rows=self._prediction_rows(),
            )
            changed = self._prediction_rows()
            changed[0]["score"] = 0.9
            with self.assertRaises(RuntimeError):
                writer.write(model_run_id=8, market="CN", prediction_rows=changed)

    def test_publication_above_threshold_uses_staging_and_records_estimates(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = PredictionArtifactWriter(
                root=Path(directory),
                staging_threshold_rows=2,
                max_rows=10,
                max_estimated_bytes=10_000_000,
            )
            manifest = writer.write(
                model_run_id=81,
                market="CN",
                prediction_rows=self._prediction_rows(),
                detail_rows=[
                    {"symbol_id": 1, "trade_date": "2026-08-20", "confidence": 0.7}
                ],
            )

            plan = manifest["prediction_publication_plan"]
            self.assertEqual("allowed", plan["status"])
            self.assertEqual("staging_atomic_publish", plan["mode"])
            self.assertTrue(plan["staging_required"])
            self.assertEqual(3, plan["total_rows"])
            self.assertGreater(plan["estimated_bytes"], 0)
            self.assertEqual(2, plan["row_counts"]["predictions"])
            self.assertEqual(1, plan["row_counts"]["details"])

    def test_publication_limit_fails_closed_before_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "artifacts"
            writer = PredictionArtifactWriter(
                root=root,
                staging_threshold_rows=1,
                max_rows=2,
                max_estimated_bytes=10_000_000,
            )

            with self.assertRaises(PredictionPublicationLimitError) as raised:
                writer.write(
                    model_run_id=82,
                    market="CN",
                    prediction_rows=self._prediction_rows(),
                    detail_rows=[
                        {"symbol_id": 1, "trade_date": "2026-08-20", "confidence": 0.7}
                    ],
                )

            self.assertEqual("blocked", raised.exception.plan["status"])
            self.assertTrue(raised.exception.plan["violations"])
            self.assertFalse(root.exists())

    def test_failed_publication_removes_unpublished_staging_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writer = PredictionArtifactWriter(
                root=root,
                staging_threshold_rows=1,
                max_rows=10,
                max_estimated_bytes=10_000_000,
            )

            with patch(
                "app.services.prediction_artifacts._write_parquet",
                side_effect=RuntimeError("fixture write failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "fixture write failure"):
                    writer.write(
                        model_run_id=83,
                        market="CN",
                        prediction_rows=self._prediction_rows(),
                    )

            self.assertFalse((root / "model_run_id=83").exists())
            self.assertEqual([], list(root.glob(".model_run_id=83.*.tmp")))

    def test_reader_pushes_symbol_and_date_filters_into_cold_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = PredictionArtifactWriter(root=Path(directory)).write(
                model_run_id=10,
                market="CN",
                prediction_rows=self._prediction_rows(),
                detail_rows=[
                    {"symbol_id": 1, "trade_date": "2026-08-20", "confidence": 0.7},
                    {"symbol_id": 2, "trade_date": "2026-08-20", "confidence": 0.5},
                ],
            )

            rows = read_prediction_artifact_rows(
                manifest["artifact_path"],
                symbol_ids=[2],
                trade_dates=["2026-08-20"],
            )

            self.assertEqual(1, len(rows))
            self.assertEqual(2, rows[0]["symbol_id"])
            self.assertEqual(0.5, rows[0]["confidence"])

    def test_explanation_reader_pushes_symbol_and_date_filters(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = PredictionArtifactWriter(root=Path(directory)).write(
                model_run_id=101,
                market="CN",
                prediction_rows=self._prediction_rows(),
                explanation_rows=[
                    {
                        "symbol_id": 1,
                        "trade_date": "2026-08-20",
                        "feature_name": "momentum_20d",
                        "feature_value": 0.1,
                        "contribution": 0.2,
                        "direction": "positive",
                        "display_order": 1,
                    },
                    {
                        "symbol_id": 2,
                        "trade_date": "2026-08-20",
                        "feature_name": "volume_ratio_20d",
                        "feature_value": 1.2,
                        "contribution": -0.1,
                        "direction": "negative",
                        "display_order": 1,
                    },
                ],
            )

            rows = read_prediction_explanation_artifact_rows(
                manifest["artifact_path"],
                symbol_ids=[2],
                trade_dates=["2026-08-20"],
            )

            self.assertEqual(1, len(rows))
            self.assertEqual("volume_ratio_20d", rows[0]["feature_name"])

    def test_full_artifact_parity_compares_predictions_details_and_explanations(self):
        prediction_rows = self._prediction_rows()
        detail_rows = [{"symbol_id": 1, "trade_date": "2026-08-20", "confidence": 0.7}]
        explanation_rows = [
            {
                "symbol_id": 1,
                "trade_date": "2026-08-20",
                "feature_name": "momentum_20d",
                "feature_value": 0.1,
                "contribution": 0.2,
                "direction": "positive",
                "display_order": 1,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            manifest = PredictionArtifactWriter(root=Path(directory)).write(
                model_run_id=11,
                market="CN",
                prediction_rows=prediction_rows,
                detail_rows=detail_rows,
                explanation_rows=explanation_rows,
            )

            parity = compare_prediction_artifact_rows(
                manifest["artifact_path"],
                prediction_rows=prediction_rows,
                detail_rows=detail_rows,
                explanation_rows=explanation_rows,
            )

            self.assertEqual("success", parity["status"])
            self.assertTrue(all(item["exact_match"] for item in parity["tables"].values()))

    def test_restore_drill_rebuilds_tables_and_top_k(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = PredictionArtifactWriter(root=root / "artifacts").write(
                model_run_id=12,
                market="CN",
                prediction_rows=self._prediction_rows(),
                detail_rows=[
                    {
                        "symbol_id": 1,
                        "trade_date": "2026-08-20",
                        "confidence": 0.7,
                        "risk_score": float("nan"),
                        "expected_return_5d": -0.0,
                    }
                ],
                explanation_rows=[
                    {
                        "symbol_id": 1,
                        "trade_date": "2026-08-20",
                        "feature_name": "momentum_20d",
                        "feature_value": 0.1,
                        "contribution": 0.2,
                        "direction": "positive",
                        "display_order": 1,
                    }
                ],
            )

            result = restore_prediction_artifact_to_sqlite(
                manifest["artifact_path"],
                destination=root / "restore.sqlite3",
                top_k=1,
            )
            self.assertEqual("prediction-artifact-restore-v2", result["restore_version"])
            self.assertEqual("isolated_sqlite", result["restore_target"])
            self.assertEqual("CN", result["market"])
            self.assertEqual("prediction-artifact-v1", result["artifact_schema_version"])
            self.assertEqual(0, result["production_rows_changed"])

            self.assertEqual("success", result["status"])
            self.assertTrue(result["top_k"]["exact_match"])
            self.assertTrue(all(item["exact_match"] for item in result["parity"]["tables"].values()))

    def test_run_reader_falls_back_to_verified_cold_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = PredictionArtifactWriter(root=Path(directory)).write(
                model_run_id=13,
                market="CN",
                prediction_rows=self._prediction_rows(),
                detail_rows=[
                    {
                        "symbol_id": 1,
                        "trade_date": "2026-08-20",
                        "confidence": 0.7,
                        "signal_label": "BUY",
                    }
                ],
            )
            artifact = SimpleNamespace(
                model_run_id=13,
                status="verified",
                artifact_path=manifest["artifact_path"],
                max_trade_date="2026-08-20",
            )
            symbol = SimpleNamespace(
                id=1,
                ticker="000001.SZ",
                name="平安银行",
                market="CN",
                sector="Finance",
                industry="Bank",
            )
            db = MagicMock()
            db.scalar.side_effect = [None, artifact, None]
            db.execute.return_value.all.return_value = []
            db.scalars.return_value.all.return_value = [symbol]

            rows = PredictionRepository(db).list_predictions_for_run(
                13,
                market="CN",
                tickers=["000001.SZ"],
            )

            self.assertEqual(1, len(rows))
            self.assertEqual("cold_parquet", rows[0]["source_layer"])
            self.assertEqual("BUY", rows[0]["signal_label"])

    def test_run_reader_does_not_touch_cold_storage_in_rollback_mode(self):
        db = MagicMock()
        db.scalar.return_value = None
        db.execute.return_value.all.return_value = []

        with patch("app.services.repository.read_prediction_artifact_rows") as cold_reader:
            rows = PredictionRepository(db, cold_reads_enabled=False).list_predictions_for_run(
                13,
                market="CN",
            )

        self.assertEqual([], rows)
        cold_reader.assert_not_called()

    def test_explanation_state_reports_hot_materialization_source(self):
        db = MagicMock()
        symbol_result = MagicMock()
        symbol_result.first.return_value = SimpleNamespace(id=1, market="CN")
        prediction_result = MagicMock()
        prediction_result.first.return_value = SimpleNamespace(
            id=10,
            model_run_id=21,
            trade_date="2026-08-20",
        )
        db.execute.side_effect = [symbol_result, prediction_result]
        db.scalars.return_value.all.return_value = [
            SimpleNamespace(
                feature_name="momentum_20d",
                feature_value=0.1,
                contribution=0.2,
                direction="positive",
                display_order=1,
            )
        ]

        state = PredictionExplanationRepository(db).get_latest_state_for_ticker(
            "000001.SZ"
        )

        self.assertEqual("materialized_hot", state["status"])
        self.assertEqual("cn_prediction_explanations", state["source_layer"])
        self.assertEqual("momentum_20d", state["rows"][0]["feature_name"])

    def test_explanation_state_explicitly_reports_not_materialized(self):
        db = MagicMock()
        symbol_result = MagicMock()
        symbol_result.first.return_value = SimpleNamespace(id=1, market="CN")
        prediction_result = MagicMock()
        prediction_result.first.return_value = SimpleNamespace(
            id=10,
            model_run_id=21,
            trade_date="2026-08-20",
        )
        db.execute.side_effect = [symbol_result, prediction_result]
        db.scalars.return_value.all.return_value = []
        db.scalar.return_value = SimpleNamespace(explanation_count=0)

        state = PredictionExplanationRepository(db).get_latest_state_for_ticker(
            "000001.SZ"
        )

        self.assertEqual("not_materialized", state["status"])
        self.assertEqual(
            "no_explanations_in_verified_artifact",
            state["reason"],
        )

    def test_model_evaluation_falls_back_to_cold_top_n(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = PredictionArtifactWriter(root=Path(directory)).write(
                model_run_id=14,
                market="CN",
                prediction_rows=self._prediction_rows(),
            )
            artifact = SimpleNamespace(status="verified", artifact_path=manifest["artifact_path"])
            symbols = [
                SimpleNamespace(id=1, ticker="000001.SZ", market="CN"),
                SimpleNamespace(id=2, ticker="000002.SZ", market="CN"),
            ]
            db = MagicMock()
            db.execute.return_value.all.return_value = []
            db.scalar.return_value = artifact
            db.scalars.return_value.all.return_value = symbols

            rows = _selected_prediction_rows(
                db,
                run=SimpleNamespace(id=14),
                market="CN",
                recent_trade_dates=1,
                top_n=1,
            )

            self.assertEqual(1, len(rows))
            self.assertEqual(1, rows[0][0].rank_value)
            self.assertEqual("000001.SZ", rows[0][1].ticker)

    def test_model_evaluation_skips_cold_storage_in_rollback_mode(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = []

        with patch("app.services.model_evaluation.read_prediction_artifact_rows") as cold_reader:
            rows = _selected_prediction_rows(
                db,
                run=SimpleNamespace(id=14),
                market="CN",
                recent_trade_dates=1,
                top_n=1,
                cold_reads_enabled=False,
            )

        self.assertEqual([], rows)
        cold_reader.assert_not_called()

    def test_verifier_detects_file_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = PredictionArtifactWriter(root=Path(directory)).write(
                model_run_id=9,
                market="US",
                prediction_rows=self._prediction_rows(),
            )
            prediction_path = Path(manifest["artifact_path"]).parent / PREDICTION_FILE
            with prediction_path.open("ab") as handle:
                handle.write(b"tampered")

            result = verify_prediction_artifact(manifest["artifact_path"])
            self.assertEqual("failed", result["status"])
            self.assertIn(f"sha256_mismatch:{PREDICTION_FILE}", result["errors"])

    def test_hot_selector_keeps_recent_full_dates_and_old_top_k(self):
        rows = [
            {
                "symbol_id": symbol_id,
                "trade_date": trade_date,
                "score": 1.0 / rank,
                "rank_value": float(rank),
            }
            for trade_date in ("2026-08-18", "2026-08-19", "2026-08-20")
            for rank, symbol_id in enumerate((1, 2, 3), start=1)
        ]
        selected = select_hot_prediction_rows(rows, full_trade_days=2, top_k=2)
        old_rows = [row for row in selected if row["trade_date"] == "2026-08-18"]

        self.assertEqual(8, len(selected))
        self.assertEqual([1.0, 2.0], [row["rank_value"] for row in old_rows])

    def test_explanation_selector_keeps_top_boundary_and_holdings_only(self):
        predictions = [
            {
                "symbol_id": symbol_id,
                "trade_date": "2026-08-20",
                "score": 1.0 / rank,
                "rank_value": float(rank),
            }
            for symbol_id, rank in (
                (1, 1),
                (2, 50),
                (3, 51),
                (4, 55),
                (5, 56),
                (6, 100),
            )
        ]
        explanations = [
            {
                "symbol_id": row["symbol_id"],
                "trade_date": row["trade_date"],
                "feature_name": "momentum_20d",
            }
            for row in predictions
        ]
        explanations.append(
            {
                "symbol_id": 1,
                "trade_date": "2026-08-19",
                "feature_name": "old_feature",
            }
        )

        selected = select_hot_explanation_rows(
            explanations,
            prediction_rows=predictions,
            top_k=50,
            holding_symbol_ids={6},
            boundary_radius=5,
        )

        self.assertEqual({1, 2, 3, 4, 6}, {row["symbol_id"] for row in selected})
        self.assertFalse(any(row["trade_date"] == "2026-08-19" for row in selected))

    def test_failed_detail_write_removes_online_predictions_and_marks_run_failed(self):
        trainer = SignalTrainer.__new__(SignalTrainer)
        trainer.settings = SimpleNamespace(
            prediction_artifacts_enabled=True,
            prediction_hot_write_mode="legacy_full",
            prediction_hot_full_trade_days=5,
            prediction_hot_top_k=100,
        )

        class FakeDB:
            rollbacks = 0

            def rollback(self):
                self.rollbacks += 1

        class FakeModelRepo:
            completions = []
            configs = []

            def merge_config(self, run_id, updates, **_kwargs):
                self.configs.append((run_id, dict(updates)))

            def complete_run(self, run_id, status, artifact_path=None, **_kwargs):
                self.completions.append((run_id, status, artifact_path))

        class FakePredictionRepo:
            writes = []

            def replace_for_model_run(self, run_id, rows, **_kwargs):
                self.writes.append((run_id, list(rows)))
                return len(rows)

        class FailingDetailRepo:
            def replace_for_model_run(self, _run_id, _rows, **_kwargs):
                raise RuntimeError("fixture detail failure")

        class FakeExplanationRepo:
            def replace_for_model_run(self, _run_id, _rows, **_kwargs):
                return 0

        class FakeArtifactWriter:
            def plan(self, **_kwargs):
                return {
                    "plan_version": "prediction-publication-plan-v1",
                    "status": "allowed",
                    "mode": "atomic_temp_publish",
                    "total_rows": 2,
                }

            def write(self, **_kwargs):
                return {
                    "model_run_id": 10,
                    "artifact_path": "/tmp/model_run_id=10/manifest.json",
                    "manifest_sha256": "abc",
                }

        class FakeArtifactRepo:
            statuses = []

            def upsert_manifest(self, _manifest, status):
                self.statuses.append(status)

            def set_status(self, _run_id, status):
                self.statuses.append(status)

        db = FakeDB()
        model_repo = FakeModelRepo()
        prediction_repo = FakePredictionRepo()
        artifact_repo = FakeArtifactRepo()
        with patch("app.services.trainer.PredictionArtifactWriter", return_value=FakeArtifactWriter()), patch(
            "app.services.trainer.PredictionArtifactRepository", return_value=artifact_repo
        ), patch(
            "app.services.trainer.LivePredictionRepository"
        ), patch(
            "app.services.trainer.MarketHotPredictionRepository"
        ), patch.object(
            trainer,
            "_holding_symbol_ids",
            return_value=set(),
        ):
            with self.assertRaises(RuntimeError):
                trainer._persist_model_outputs(
                    db=db,
                    model_repo=model_repo,
                    prediction_repo=prediction_repo,
                    detail_repo=FailingDetailRepo(),
                    explanation_repo=FakeExplanationRepo(),
                    run_id=10,
                    market="CN",
                    signal_rows=self._prediction_rows(),
                    detail_rows=[],
                    explanation_rows=[],
                    model_metadata={"model": "fixture"},
                )

        self.assertEqual(2, len(prediction_repo.writes))
        self.assertEqual([], prediction_repo.writes[-1][1])
        self.assertEqual("failed_run", artifact_repo.statuses[-1])
        self.assertEqual("failed", model_repo.completions[-1][1])

    def test_trainer_publishes_explicit_storage_contract(self):
        trainer = SignalTrainer.__new__(SignalTrainer)
        trainer.settings = SimpleNamespace(
            prediction_artifacts_enabled=True,
            prediction_hot_write_mode="compact",
            prediction_hot_full_trade_days=5,
            prediction_hot_top_k=100,
            prediction_hot_explanation_limit=50,
        )
        writer = MagicMock()
        writer.plan.return_value = {
            "plan_version": "prediction-publication-plan-v1",
            "status": "allowed",
            "mode": "atomic_temp_publish",
            "total_rows": 2,
        }
        writer.write.return_value = {
            "model_run_id": 21,
            "artifact_path": "/tmp/model_run_id=21/manifest.json",
            "manifest_sha256": "fixture",
        }
        model_repo = MagicMock()
        prediction_repo = MagicMock()
        prediction_repo.replace_for_model_run.return_value = 2
        detail_repo = MagicMock()
        explanation_repo = MagicMock()
        db = MagicMock()

        with patch("app.services.trainer.PredictionArtifactWriter", return_value=writer), patch(
            "app.services.trainer.PredictionArtifactRepository"
        ), patch(
            "app.services.trainer.LivePredictionRepository"
        ) as live_repository, patch(
            "app.services.trainer.MarketHotPredictionRepository"
        ) as physical_repository:
            count = trainer._persist_model_outputs(
                db=db,
                model_repo=model_repo,
                prediction_repo=prediction_repo,
                detail_repo=detail_repo,
                explanation_repo=explanation_repo,
                run_id=21,
                market="CN",
                signal_rows=self._prediction_rows(),
                detail_rows=[],
                explanation_rows=[],
                model_metadata={"model": "fixture"},
            )

        contract = writer.write.call_args.kwargs["model_metadata"]["prediction_storage_contract"]
        self.assertEqual(2, count)
        self.assertEqual("prediction-storage-v1", contract["contract_version"])
        self.assertEqual("compact", contract["hot_write_mode"])
        self.assertEqual(5, contract["hot_full_trade_days"])
        self.assertEqual(100, contract["hot_top_k"])
        self.assertEqual(50, contract["hot_explanation_limit"])
        self.assertEqual([], contract["hot_explanation_holding_symbol_ids"])
        self.assertTrue(contract["legacy_hot_dual_write"])
        publication_plan = writer.write.call_args.kwargs["publication_plan"]
        self.assertEqual("allowed", publication_plan["status"])
        self.assertEqual(2, publication_plan["total_rows"])
        self.assertEqual(2, model_repo.merge_config.call_count)
        self.assertEqual(
            (21, {"prediction_publication_plan": publication_plan}),
            model_repo.merge_config.call_args_list[0].args,
        )
        timing_update = model_repo.merge_config.call_args_list[1].args[1][
            "prediction_publication_timing"
        ]
        self.assertEqual(
            "prediction-publication-timing-v2",
            timing_update["timing_version"],
        )
        self.assertEqual(2, timing_update["cold_rows"])
        self.assertEqual(2, timing_update["hot_prediction_rows"])
        self.assertIn("postgresql_write_phase_ms", timing_update)
        self.assertGreater(timing_update["process_peak_rss_bytes"], 0)
        self.assertIn("python_tracemalloc_peak_bytes", timing_update)
        self.assertIn("python_publication_incremental_peak_bytes", timing_update)
        self.assertTrue(timing_update["postgresql_atomic_publish"])
        self.assertEqual(
            "prediction-publication-transaction-v1",
            timing_update["postgresql_transaction_version"],
        )
        self.assertIn("postgresql_commit_ms", timing_update)
        prediction_repo.replace_for_model_run.assert_called_once()
        self.assertFalse(
            prediction_repo.replace_for_model_run.call_args.kwargs["commit"]
        )
        self.assertFalse(detail_repo.replace_for_model_run.call_args.kwargs["commit"])
        self.assertFalse(
            explanation_repo.replace_for_model_run.call_args.kwargs["commit"]
        )
        self.assertFalse(
            physical_repository.return_value.publish_from_legacy_mirror.call_args.kwargs[
                "commit"
            ]
        )
        self.assertFalse(
            live_repository.return_value.publish_from_physical_hot.call_args.kwargs[
                "commit"
            ]
        )
        self.assertFalse(model_repo.complete_run.call_args.kwargs["commit"])
        db.commit.assert_called_once_with()

    def test_trainer_keeps_full_cold_explanations_but_bounds_hot_materialization(self):
        trainer = SignalTrainer.__new__(SignalTrainer)
        trainer.settings = SimpleNamespace(
            prediction_artifacts_enabled=True,
            prediction_hot_write_mode="legacy_full",
            prediction_hot_full_trade_days=5,
            prediction_hot_top_k=100,
            prediction_hot_explanation_limit=50,
            prediction_hot_explanation_boundary_radius=5,
        )
        signal_rows = [
            {
                "symbol_id": rank,
                "trade_date": "2026-08-22",
                "score": 1.0 / rank,
                "rank_value": float(rank),
            }
            for rank in range(1, 101)
        ]
        explanation_rows = [
            {
                "symbol_id": rank,
                "trade_date": "2026-08-22",
                "feature_name": "momentum_20d",
                "display_order": 1,
            }
            for rank in range(1, 101)
        ]
        publication_plan = {
            "plan_version": "prediction-publication-plan-v1",
            "status": "allowed",
            "mode": "atomic_temp_publish",
            "total_rows": 200,
        }
        writer = MagicMock()
        writer.plan.return_value = publication_plan
        writer.write.return_value = {
            "model_run_id": 22,
            "artifact_path": "/tmp/model_run_id=22/manifest.json",
            "manifest_sha256": "fixture",
        }
        explanation_repo = MagicMock()
        prediction_repo = MagicMock()
        prediction_repo.replace_for_model_run.return_value = len(signal_rows)

        with patch(
            "app.services.trainer.PredictionArtifactWriter",
            return_value=writer,
        ), patch(
            "app.services.trainer.PredictionArtifactRepository"
        ), patch(
            "app.services.trainer.LivePredictionRepository"
        ), patch(
            "app.services.trainer.MarketHotPredictionRepository"
        ), patch.object(
            trainer,
            "_holding_symbol_ids",
            return_value={100},
        ):
            trainer._persist_model_outputs(
                db=MagicMock(),
                model_repo=MagicMock(),
                prediction_repo=prediction_repo,
                detail_repo=MagicMock(),
                explanation_repo=explanation_repo,
                run_id=22,
                market="CN",
                signal_rows=signal_rows,
                detail_rows=[],
                explanation_rows=explanation_rows,
                model_metadata={"model": "fixture"},
            )

        self.assertEqual(100, len(writer.write.call_args.kwargs["explanation_rows"]))
        hot_rows = explanation_repo.replace_for_model_run.call_args.args[1]
        self.assertEqual(56, len(hot_rows))
        self.assertEqual(
            {*range(1, 56), 100},
            {int(row["symbol_id"]) for row in hot_rows},
        )
        contract = writer.write.call_args.kwargs["model_metadata"]["prediction_storage_contract"]
        self.assertEqual([100], contract["hot_explanation_holding_symbol_ids"])

    def test_trainer_can_publish_physical_hot_rows_without_legacy_writes(self):
        trainer = SignalTrainer.__new__(SignalTrainer)
        trainer.settings = SimpleNamespace(
            prediction_artifacts_enabled=True,
            prediction_hot_write_mode="compact",
            prediction_hot_full_trade_days=5,
            prediction_hot_top_k=100,
            prediction_hot_explanation_limit=50,
            prediction_hot_explanation_boundary_radius=5,
            market_physical_hot_markets="CN",
            market_physical_hot_dual_write_legacy=True,
        )
        signal_rows = self._prediction_rows()
        writer = MagicMock()
        writer.plan.return_value = {
            "plan_version": "prediction-publication-plan-v1",
            "status": "allowed",
            "mode": "atomic_temp_publish",
            "total_rows": len(signal_rows),
        }
        writer.write.return_value = {
            "model_run_id": 23,
            "artifact_path": "/tmp/model_run_id=23/manifest.json",
            "manifest_sha256": "fixture",
        }
        prediction_repo = MagicMock()
        detail_repo = MagicMock()
        explanation_repo = MagicMock()
        physical_repo = MagicMock()

        with patch(
            "app.services.trainer.PredictionArtifactWriter",
            return_value=writer,
        ), patch(
            "app.services.trainer.PredictionArtifactRepository"
        ), patch(
            "app.services.trainer.LivePredictionRepository"
        ), patch(
            "app.services.trainer.MarketHotPredictionRepository",
            return_value=physical_repo,
        ), patch(
            "app.services.trainer.legacy_mirror_write_enabled",
            return_value=False,
        ), patch.object(
            trainer,
            "_holding_symbol_ids",
            return_value=set(),
        ):
            count = trainer._persist_model_outputs(
                db=MagicMock(),
                model_repo=MagicMock(),
                prediction_repo=prediction_repo,
                detail_repo=detail_repo,
                explanation_repo=explanation_repo,
                run_id=23,
                market="CN",
                signal_rows=signal_rows,
                detail_rows=[],
                explanation_rows=[],
                model_metadata={"model": "fixture"},
            )

        self.assertEqual(len(signal_rows), count)
        prediction_repo.replace_for_model_run.assert_not_called()
        detail_repo.replace_for_model_run.assert_not_called()
        explanation_repo.replace_for_model_run.assert_not_called()
        physical_repo.publish_for_model_run.assert_called_once()
        contract = writer.write.call_args.kwargs["model_metadata"][
            "prediction_storage_contract"
        ]
        self.assertFalse(contract["legacy_hot_dual_write"])


if __name__ == "__main__":
    unittest.main()
