from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import delete, func, select
from tests.postgres_safety import create_verified_test_engine
from sqlalchemy.orm import sessionmaker

from app.models.base import Base
from app.models.tables import (
    LivePrediction,
    ModelChartSignal,
    ModelRun,
    Prediction,
    PredictionDetail,
    PredictionExplanation,
    PredictionTradePlan,
    Symbol,
    USLivePrediction,
    USModelChartSignal,
    USPrediction,
    USPredictionDetail,
    USPredictionExplanation,
    USPredictionTradePlan,
)
from app.services.model_output_importer import ExternalModelOutputImporter


class ExternalModelPhysicalStorageTests(unittest.TestCase):
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
        self.created_run_ids: list[int] = []
        self.created_tickers: list[str] = []

    def tearDown(self) -> None:
        with self.SessionLocal() as db:
            for run_id in self.created_run_ids:
                shared_ids = list(
                    db.scalars(
                        select(Prediction.id).where(
                            Prediction.model_run_id == run_id
                        )
                    )
                )
                if shared_ids:
                    db.execute(
                        delete(PredictionExplanation).where(
                            PredictionExplanation.prediction_id.in_(shared_ids)
                        )
                    )
                    db.execute(
                        delete(PredictionDetail).where(
                            PredictionDetail.prediction_id.in_(shared_ids)
                        )
                    )
                db.execute(
                    delete(LivePrediction).where(
                        LivePrediction.model_run_id == run_id
                    )
                )
                db.execute(
                    delete(USLivePrediction).where(
                        USLivePrediction.model_run_id == run_id
                    )
                )
                db.execute(
                    delete(ModelChartSignal).where(
                        ModelChartSignal.model_run_id == run_id
                    )
                )
                db.execute(
                    delete(USModelChartSignal).where(
                        USModelChartSignal.model_run_id == run_id
                    )
                )
                db.execute(
                    delete(PredictionTradePlan).where(
                        PredictionTradePlan.prediction_id.in_(shared_ids)
                    )
                )
                us_prediction_ids = list(
                    db.scalars(
                        select(USPrediction.id).where(
                            USPrediction.model_run_id == run_id
                        )
                    )
                )
                db.execute(
                    delete(USPredictionTradePlan).where(
                        USPredictionTradePlan.prediction_id.in_(us_prediction_ids)
                    )
                )
                db.execute(
                    delete(Prediction).where(Prediction.model_run_id == run_id)
                )
                db.execute(
                    delete(USPrediction).where(USPrediction.model_run_id == run_id)
                )
                db.execute(delete(ModelRun).where(ModelRun.id == run_id))
            if self.created_tickers:
                db.execute(
                    delete(Symbol).where(Symbol.ticker.in_(self.created_tickers))
                )
            db.commit()

    def _csv(self, ticker: str) -> Path:
        target = Path(self.temporary.name) / f"{ticker}.csv"
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "ticker",
                    "trade_date",
                    "score",
                    "confidence",
                    "signal_label",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "ticker": ticker,
                    "trade_date": "2026-08-21",
                    "score": "0.81",
                    "confidence": "0.77",
                    "signal_label": "BUY",
                }
            )
        return target

    def _import(self, *, legacy_mirror: bool) -> tuple[dict, str]:
        ticker = f"EXT{uuid.uuid4().hex[:10].upper()}"
        self.created_tickers.append(ticker)
        csv_path = self._csv(ticker)
        with patch(
            "app.services.model_output_importer.SessionLocal",
            self.SessionLocal,
        ), patch(
            "app.services.model_output_importer.legacy_mirror_write_enabled",
            return_value=legacy_mirror,
        ):
            result = ExternalModelOutputImporter().import_csv(
                csv_path,
                run_name=f"external-physical-{ticker}",
                model_type="qlib_external",
                market="US",
                universe="external-physical-test",
            )
        self.created_run_ids.append(int(result["model_run_id"]))
        return result, ticker

    def test_dual_write_uses_us_physical_table_without_publishing_live(self) -> None:
        result, ticker = self._import(legacy_mirror=True)
        run_id = int(result["model_run_id"])
        with self.SessionLocal() as db:
            symbol_id = int(
                db.scalar(select(Symbol.id).where(Symbol.ticker == ticker))
            )
            legacy_count = int(
                db.scalar(
                    select(func.count(Prediction.id)).where(
                        Prediction.model_run_id == run_id,
                        Prediction.symbol_id == symbol_id,
                    )
                )
                or 0
            )
            physical_count = int(
                db.scalar(
                    select(func.count(USPrediction.id)).where(
                        USPrediction.model_run_id == run_id,
                        USPrediction.symbol_id == symbol_id,
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
                    select(func.count(USLivePrediction.id)).where(
                        USLivePrediction.model_run_id == run_id
                    )
                ) or 0)
            )
            status = db.scalar(select(ModelRun.status).where(ModelRun.id == run_id))

        self.assertEqual("success", status)
        self.assertEqual(1, legacy_count)
        self.assertEqual(1, physical_count)
        self.assertEqual(0, live_count)
        self.assertTrue(result["physical_market_table"])
        self.assertTrue(result["legacy_hot_dual_write"])

    def test_physical_only_import_and_late_explanations_never_touch_shared_tables(self) -> None:
        result, ticker = self._import(legacy_mirror=False)
        run_id = int(result["model_run_id"])
        with patch(
            "app.services.model_output_importer.SessionLocal",
            self.SessionLocal,
        ), patch(
            "app.services.model_output_importer.legacy_mirror_write_enabled",
            return_value=False,
        ), patch(
            "app.services.repository.legacy_mirror_write_enabled",
            return_value=False,
        ):
            explanation_count = ExternalModelOutputImporter().import_explanations_rows(
                [
                    {
                        "ticker": ticker,
                        "trade_date": "2026-08-21",
                        "feature_name": "external_factor",
                        "feature_value": "1.25",
                        "contribution": "0.19",
                        "direction": "positive",
                        "display_order": "1",
                    }
                ],
                model_run_id=run_id,
            )
            chart_count = ExternalModelOutputImporter().import_chart_signals_rows(
                [
                    {
                        "ticker": ticker,
                        "trade_date": "2026-08-21",
                        "score": "0.81",
                        "signal_label": "BUY",
                    }
                ],
                model_run_id=run_id,
            )
            trade_plan_count = ExternalModelOutputImporter().import_trade_plan_payload(
                [
                    {
                        "ticker": ticker,
                        "trade_date": "2026-08-21",
                        "entry_low": "100",
                        "entry_high": "102",
                        "stop_type": "hard",
                    }
                ],
                model_run_id=run_id,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "no physical hot prediction parent",
            ):
                ExternalModelOutputImporter().import_trade_plan_payload(
                    [
                        {
                            "ticker": ticker,
                            "trade_date": "2026-08-20",
                            "entry_low": "99",
                        }
                    ],
                    model_run_id=run_id,
                )

        with self.SessionLocal() as db:
            legacy_predictions = int(
                db.scalar(
                    select(func.count(Prediction.id)).where(
                        Prediction.model_run_id == run_id
                    )
                )
                or 0
            )
            legacy_explanations = int(
                db.scalar(
                    select(func.count(PredictionExplanation.id))
                    .join(Prediction, Prediction.id == PredictionExplanation.prediction_id)
                    .where(Prediction.model_run_id == run_id)
                )
                or 0
            )
            physical_details = int(
                db.scalar(
                    select(func.count(USPredictionDetail.id))
                    .join(
                        USPrediction,
                        USPrediction.id == USPredictionDetail.prediction_id,
                    )
                    .where(USPrediction.model_run_id == run_id)
                )
                or 0
            )
            physical_explanations = int(
                db.scalar(
                    select(func.count(USPredictionExplanation.id))
                    .join(
                        USPrediction,
                        USPrediction.id
                        == USPredictionExplanation.prediction_id,
                    )
                    .where(USPrediction.model_run_id == run_id)
                )
                or 0
            )
            legacy_chart_signals = int(
                db.scalar(
                    select(func.count(ModelChartSignal.id)).where(
                        ModelChartSignal.model_run_id == run_id
                    )
                )
                or 0
            )
            physical_chart_signals = int(
                db.scalar(
                    select(func.count(USModelChartSignal.id)).where(
                        USModelChartSignal.model_run_id == run_id
                    )
                )
                or 0
            )
            legacy_trade_plans = int(
                db.scalar(
                    select(func.count(PredictionTradePlan.id))
                    .join(
                        Prediction,
                        Prediction.id == PredictionTradePlan.prediction_id,
                    )
                    .where(Prediction.model_run_id == run_id)
                )
                or 0
            )
            physical_trade_plans = int(
                db.scalar(
                    select(func.count(USPredictionTradePlan.id))
                    .join(
                        USPrediction,
                        USPrediction.id == USPredictionTradePlan.prediction_id,
                    )
                    .where(USPrediction.model_run_id == run_id)
                )
                or 0
            )

        self.assertEqual(1, explanation_count)
        self.assertEqual(1, chart_count)
        self.assertEqual(1, trade_plan_count)
        self.assertEqual(0, legacy_predictions)
        self.assertEqual(0, legacy_explanations)
        self.assertEqual(1, physical_details)
        self.assertEqual(1, physical_explanations)
        self.assertEqual(0, legacy_chart_signals)
        self.assertEqual(1, physical_chart_signals)
        self.assertEqual(0, legacy_trade_plans)
        self.assertEqual(1, physical_trade_plans)
        self.assertFalse(result["legacy_hot_dual_write"])


if __name__ == "__main__":
    unittest.main()
