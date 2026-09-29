from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import delete, func, select
from tests.postgres_safety import create_verified_test_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models.base import Base
from app.models.tables import (
    CNLivePrediction,
    CNPrediction,
    CNPredictionDetail,
    CNPredictionExplanation,
    LivePrediction,
    ModelRun,
    Prediction,
    PredictionDetail,
    PredictionExplanation,
    Symbol,
)
from app.services.market_hot_predictions import MarketHotPredictionRepository
from app.services.repository import (
    LivePredictionRepository,
    ModelRunRepository,
    PredictionDetailRepository,
    PredictionExplanationRepository,
    PredictionWriteRepository,
)
from app.services.time_utils import app_now_iso


class PredictionPublicationTransactionTests(unittest.TestCase):
    """Prove that every online PostgreSQL output shares one transaction."""

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
        suffix = uuid.uuid4().hex[:12].upper()
        now = app_now_iso()
        with self.SessionLocal() as db:
            symbol = Symbol(
                ticker=f"ATOMIC{suffix}",
                name="Atomic publication fixture",
                market="CN",
                exchange="TEST",
                is_active=1,
                created_at=now,
                updated_at=now,
            )
            db.add(symbol)
            db.flush()
            run = ModelRun(
                name=f"atomic-publication-{suffix}",
                model_type="lightgbm_multifactor",
                market="CN",
                universe="atomic-publication-test",
                train_start="2026-01-01",
                train_end="2026-07-31",
                test_start="2026-08-01",
                test_end="2026-08-21",
                config_json="{}",
                artifact_path=None,
                status="running",
                created_at=now,
                finished_at=None,
            )
            db.add(run)
            db.commit()
            self.symbol_id = int(symbol.id)
            self.run_id = int(run.id)

        self.signal_rows = [
            {
                "symbol_id": self.symbol_id,
                "trade_date": "2026-08-21",
                "score": 0.73,
                "rank_value": 1.0,
            }
        ]
        self.detail_rows = [
            {
                "symbol_id": self.symbol_id,
                "trade_date": "2026-08-21",
                "confidence": 0.71,
                "signal_label": "BUY",
                "signal_strength": 0.63,
            }
        ]
        self.explanation_rows = [
            {
                "symbol_id": self.symbol_id,
                "trade_date": "2026-08-21",
                "feature_name": "momentum_20d",
                "feature_value": 0.12,
                "contribution": 0.08,
                "direction": "positive",
                "display_order": 1,
            }
        ]

    def tearDown(self) -> None:
        with self.SessionLocal() as db:
            shared_prediction_ids = list(
                db.scalars(
                    select(Prediction.id).where(
                        Prediction.model_run_id == self.run_id
                    )
                )
            )
            if shared_prediction_ids:
                db.execute(
                    delete(PredictionExplanation).where(
                        PredictionExplanation.prediction_id.in_(
                            shared_prediction_ids
                        )
                    )
                )
                db.execute(
                    delete(PredictionDetail).where(
                        PredictionDetail.prediction_id.in_(shared_prediction_ids)
                    )
                )
            db.execute(
                delete(LivePrediction).where(
                    LivePrediction.model_run_id == self.run_id
                )
            )
            db.execute(
                delete(CNLivePrediction).where(
                    CNLivePrediction.model_run_id == self.run_id
                )
            )
            db.execute(
                delete(Prediction).where(Prediction.model_run_id == self.run_id)
            )
            # CN prediction children use ON DELETE CASCADE.
            db.execute(
                delete(CNPrediction).where(
                    CNPrediction.model_run_id == self.run_id
                )
            )
            db.execute(delete(ModelRun).where(ModelRun.id == self.run_id))
            db.execute(delete(Symbol).where(Symbol.id == self.symbol_id))
            db.commit()

    def _stage_complete_publication(self, db: Session) -> None:
        PredictionWriteRepository(db).replace_for_model_run(
            self.run_id,
            self.signal_rows,
            commit=False,
        )
        PredictionDetailRepository(db).replace_for_model_run(
            self.run_id,
            self.detail_rows,
            commit=False,
        )
        PredictionExplanationRepository(db).replace_for_model_run(
            self.run_id,
            self.explanation_rows,
            commit=False,
        )
        MarketHotPredictionRepository(db).publish_for_model_run(
            model_run_id=self.run_id,
            market="CN",
            prediction_rows=self.signal_rows,
            detail_rows=self.detail_rows,
            explanation_rows=self.explanation_rows,
            commit=False,
        )
        with patch(
            "app.services.repository.get_settings",
            return_value=SimpleNamespace(
                market_physical_live_dual_write_legacy=True
            ),
        ):
            LivePredictionRepository(db).publish_for_model_run(
                model_run_id=self.run_id,
                market="CN",
                prediction_rows=self.signal_rows,
                detail_rows=self.detail_rows,
                commit=False,
            )
        ModelRunRepository(db).complete_run(
            self.run_id,
            status="success",
            artifact_path="/tmp/atomic-publication-fixture/manifest.json",
            commit=False,
        )

    def _observed_state(self, db: Session) -> dict[str, int | str | None]:
        shared_prediction_ids = select(Prediction.id).where(
            Prediction.model_run_id == self.run_id
        )
        cn_prediction_ids = select(CNPrediction.id).where(
            CNPrediction.model_run_id == self.run_id
        )
        return {
            "legacy_predictions": int(
                db.scalar(
                    select(func.count(Prediction.id)).where(
                        Prediction.model_run_id == self.run_id
                    )
                )
                or 0
            ),
            "legacy_details": int(
                db.scalar(
                    select(func.count(PredictionDetail.id)).where(
                        PredictionDetail.prediction_id.in_(shared_prediction_ids)
                    )
                )
                or 0
            ),
            "legacy_explanations": int(
                db.scalar(
                    select(func.count(PredictionExplanation.id)).where(
                        PredictionExplanation.prediction_id.in_(
                            shared_prediction_ids
                        )
                    )
                )
                or 0
            ),
            "cn_predictions": int(
                db.scalar(
                    select(func.count(CNPrediction.id)).where(
                        CNPrediction.model_run_id == self.run_id
                    )
                )
                or 0
            ),
            "cn_details": int(
                db.scalar(
                    select(func.count(CNPredictionDetail.id)).where(
                        CNPredictionDetail.prediction_id.in_(cn_prediction_ids)
                    )
                )
                or 0
            ),
            "cn_explanations": int(
                db.scalar(
                    select(func.count(CNPredictionExplanation.id)).where(
                        CNPredictionExplanation.prediction_id.in_(
                            cn_prediction_ids
                        )
                    )
                )
                or 0
            ),
            "legacy_live": int(
                db.scalar(
                    select(func.count(LivePrediction.id)).where(
                        LivePrediction.model_run_id == self.run_id
                    )
                )
                or 0
            ),
            "cn_live": int(
                db.scalar(
                    select(func.count(CNLivePrediction.id)).where(
                        CNLivePrediction.model_run_id == self.run_id
                    )
                )
                or 0
            ),
            "run_status": db.scalar(
                select(ModelRun.status).where(ModelRun.id == self.run_id)
            ),
        }

    def assert_unpublished(self, state: dict[str, int | str | None]) -> None:
        self.assertEqual("running", state["run_status"])
        self.assertTrue(
            all(value == 0 for key, value in state.items() if key != "run_status"),
            state,
        )

    def test_cross_session_visibility_rollback_and_commit_are_atomic(self) -> None:
        with self.SessionLocal() as publisher, self.SessionLocal() as observer:
            self._stage_complete_publication(publisher)

            # A separate PostgreSQL transaction cannot observe any staged
            # output or a successful run status before the publisher commits.
            self.assert_unpublished(self._observed_state(observer))

            publisher.rollback()
            observer.rollback()
            self.assert_unpublished(self._observed_state(observer))

            self._stage_complete_publication(publisher)
            publisher.commit()
            observer.rollback()
            committed = self._observed_state(observer)

        self.assertEqual("success", committed["run_status"])
        self.assertTrue(
            all(value == 1 for key, value in committed.items() if key != "run_status"),
            committed,
        )


if __name__ == "__main__":
    unittest.main()
