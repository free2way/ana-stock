from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete, func, select
from tests.postgres_safety import create_verified_test_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.tables import (
    CNLivePrediction,
    CNPrediction,
    LivePrediction,
    ModelRun,
    Prediction,
    PredictionArtifact,
    Symbol,
)
from app.services.kronos_backtest import (
    KronosHistoricalPanel,
    _persist_panel_run,
    build_kronos_historical_panel,
)
from app.services.prediction_artifacts import PredictionArtifactWriter
from app.services.repository import PredictionWriteRepository
from app.services.time_utils import app_now_iso


def _row(ticker: str, date: str, score: float, decision: str = "Kronos 支持") -> dict:
    return {
        "ticker": ticker,
        "market": "CN",
        "latest_date": date,
        "kronos_status": "READY",
        "kronos_score": score,
        "kronos_expected_return_3d_pct": score / 10,
        "kronos_decision": decision,
    }


class KronosBacktestPanelTests(unittest.TestCase):
    def test_uses_latest_same_session_snapshot_and_excludes_stale_reruns(self) -> None:
        snapshots = [
            {
                "id": 1,
                "snapshot_date": "2026-05-07",
                "created_at": "2026-05-07T18:00:00+08:00",
                "payload": {"model_name": "NeoQuasar/Kronos-mini", "rows": [_row("A", "2026-05-07", 70), _row("B", "2026-05-07", 60)]},
            },
            {
                "id": 2,
                "snapshot_date": "2026-05-07",
                "created_at": "2026-05-07T19:00:00+08:00",
                "payload": {"model_name": "NeoQuasar/Kronos-mini", "rows": [_row("A", "2026-05-07", 75), _row("B", "2026-05-07", 65)]},
            },
            {
                "id": 3,
                "snapshot_date": "2026-05-08",
                "created_at": "2026-05-08T18:00:00+08:00",
                "payload": {"model_name": "NeoQuasar/Kronos-mini", "rows": [_row("A", "2026-05-07", 99), _row("B", "2026-05-07", 99)]},
            },
        ]

        panel = build_kronos_historical_panel(snapshots, min_candidates_per_date=2)

        self.assertEqual((2,), panel.snapshot_ids)
        self.assertEqual(("2026-05-07",), panel.signal_dates)
        self.assertEqual(0.75, panel.kronos_predictions[0]["score"])

    def test_kronos_rank_is_independent_of_upstream_candidate_order(self) -> None:
        panel = build_kronos_historical_panel(
            [
                {
                    "id": 4,
                    "snapshot_date": "2026-05-08",
                    "created_at": "2026-05-08T18:00:00+08:00",
                    "payload": {
                        "rows": [
                            _row("BASE_FIRST", "2026-05-08", 50, "Kronos 中性"),
                            _row("KRONOS_FIRST", "2026-05-08", 80),
                        ]
                    },
                }
            ],
            min_candidates_per_date=2,
        )

        self.assertEqual("BASE_FIRST", panel.baseline_predictions[0]["ticker"])
        self.assertEqual("KRONOS_FIRST", panel.kronos_predictions[0]["ticker"])
        self.assertEqual(1.0, panel.kronos_predictions[0]["rank_value"])


class KronosPhysicalStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_verified_test_engine()
        cls.addClassCleanup(cls.engine.dispose)
        Base.metadata.create_all(cls.engine)
        cls.SessionLocal = sessionmaker(
            bind=cls.engine,
            autoflush=False,
            autocommit=False,
            future=True,
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        suffix = uuid.uuid4().hex[:10].upper()
        self.tickers = [f"KRA{suffix}.SS", f"KRB{suffix}.SS"]
        now = app_now_iso()
        with self.SessionLocal() as db:
            for ticker in self.tickers:
                db.add(
                    Symbol(
                        ticker=ticker,
                        name=ticker,
                        market="CN",
                        exchange="SSE",
                        sector="TEST",
                        industry="TEST",
                        is_active=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
            db.commit()
        self.created_run_ids: list[int] = []

    def tearDown(self) -> None:
        with self.SessionLocal() as db:
            for run_id in self.created_run_ids:
                db.execute(
                    delete(PredictionArtifact).where(
                        PredictionArtifact.model_run_id == run_id
                    )
                )
                db.execute(
                    delete(LivePrediction).where(
                        LivePrediction.model_run_id == run_id
                    )
                )
                db.execute(
                    delete(CNLivePrediction).where(
                        CNLivePrediction.model_run_id == run_id
                    )
                )
                db.execute(
                    delete(Prediction).where(Prediction.model_run_id == run_id)
                )
                db.execute(
                    delete(CNPrediction).where(CNPrediction.model_run_id == run_id)
                )
                db.execute(delete(ModelRun).where(ModelRun.id == run_id))
            db.execute(delete(Symbol).where(Symbol.ticker.in_(self.tickers)))
            db.commit()

    def _persist(self, *, legacy_mirror: bool) -> tuple[int, Path]:
        dates = ("2026-08-18", "2026-08-19", "2026-08-20")
        predictions = tuple(
            {
                "ticker": ticker,
                "trade_date": trade_date,
                "score": 0.9 if index == 0 else 0.7,
                "rank_value": float(index + 1),
            }
            for trade_date in dates
            for index, ticker in enumerate(self.tickers)
        )
        panel = KronosHistoricalPanel(
            market="CN",
            model_name="NeoQuasar/Kronos-mini",
            start_date=dates[0],
            end_date=dates[-1],
            snapshot_ids=(1, 2, 3),
            signal_dates=dates,
            kronos_predictions=predictions,
            baseline_predictions=predictions,
            decision_counts={"Kronos 支持": len(predictions)},
        )
        artifact_root = Path(self.temporary.name) / "prediction_runs"
        writer = PredictionArtifactWriter(root=artifact_root)
        settings = SimpleNamespace(
            market_physical_hot_dual_write_legacy=True,
            prediction_hot_full_trade_days=1,
            prediction_hot_top_k=1,
        )
        with self.SessionLocal() as db, patch(
            "app.services.kronos_backtest.get_settings",
            return_value=settings,
        ), patch(
            "app.services.kronos_backtest.PredictionArtifactWriter",
            return_value=writer,
        ), patch(
            "app.services.kronos_backtest.legacy_mirror_write_enabled",
            return_value=legacy_mirror,
        ):
            run = _persist_panel_run(
                db,
                panel=panel,
                name=f"kronos-physical-{uuid.uuid4().hex}",
                model_type="kronos_mini_validator",
                prediction_rows=predictions,
                config={"source": "test"},
            )
            run_id = int(run.id)
        self.created_run_ids.append(run_id)
        return run_id, artifact_root / f"model_run_id={run_id}" / "manifest.json"

    def _assert_storage(self, *, run_id: int, legacy_count: int) -> None:
        with self.SessionLocal() as db:
            artifact = db.scalar(
                select(PredictionArtifact).where(
                    PredictionArtifact.model_run_id == run_id
                )
            )
            physical_count = int(
                db.scalar(
                    select(func.count(CNPrediction.id)).where(
                        CNPrediction.model_run_id == run_id
                    )
                )
                or 0
            )
            actual_legacy_count = int(
                db.scalar(
                    select(func.count(Prediction.id)).where(
                        Prediction.model_run_id == run_id
                    )
                )
                or 0
            )
            live_count = int(
                (db.scalar(
                    select(func.count(LivePrediction.id)).where(
                        LivePrediction.model_run_id == run_id
                    )
                ) or 0)
                + (db.scalar(
                    select(func.count(CNLivePrediction.id)).where(
                        CNLivePrediction.model_run_id == run_id
                    )
                ) or 0)
            )
            run = db.scalar(select(ModelRun).where(ModelRun.id == run_id))
            routed_rows = PredictionWriteRepository(db).list_for_model_run(run_id)

        self.assertIsNotNone(artifact)
        self.assertEqual("verified", artifact.status)
        self.assertEqual(6, artifact.prediction_count)
        self.assertEqual(4, physical_count)
        self.assertEqual(legacy_count, actual_legacy_count)
        self.assertEqual(0, live_count)
        self.assertEqual(4, len(routed_rows))
        self.assertTrue(all(isinstance(row, CNPrediction) for row in routed_rows))
        self.assertEqual("success", run.status)
        config = json.loads(run.config_json or "{}")
        self.assertEqual(
            "cn_predictions",
            config["prediction_storage_contract"]["primary_market_table"],
        )
        self.assertFalse(config["prediction_storage_contract"]["live_publication"])

    def test_dual_write_keeps_only_compact_rows_in_cn_postgresql(self) -> None:
        run_id, manifest_path = self._persist(legacy_mirror=True)
        self.assertTrue(manifest_path.is_file())
        self._assert_storage(run_id=run_id, legacy_count=4)

    def test_physical_only_write_never_touches_shared_prediction_table(self) -> None:
        run_id, manifest_path = self._persist(legacy_mirror=False)
        self.assertTrue(manifest_path.is_file())
        self._assert_storage(run_id=run_id, legacy_count=0)


if __name__ == "__main__":
    unittest.main()
