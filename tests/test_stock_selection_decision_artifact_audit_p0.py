from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import Base
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.artifact_audit import audit_final_decision_artifacts
from app.services.stock_selection.decision_ledger import freeze_final_decisions


class DecisionArtifactAuditP0Tests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.temp = TemporaryDirectory()
        self.store = JsonPayloadArtifactStore(Path(self.temp.name))
        self.report = {
            "report_date": "2026-09-11", "saved_at": "2026-09-11T20:00:00+08:00",
            "market_recommendations": [], "market_recommendations_meta": {"status": "not_ready"},
        }

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def test_committed_reference_is_verified_and_not_orphaned(self) -> None:
        with Session(self.engine) as db:
            freeze_final_decisions(self.report, db=db, store=self.store)
            result = audit_final_decision_artifacts(db=db, store=self.store, orphan_grace_seconds=0)
        self.assertEqual("pass", result["status"])
        self.assertEqual(1, result["references_checked"])
        self.assertEqual(1, result["files_scanned"])
        self.assertEqual([], result["candidate_orphans"])

    def test_rolled_back_cold_write_is_candidate_orphan_not_deleted(self) -> None:
        with Session(self.engine) as db:
            freeze_final_decisions(self.report, db=db, store=self.store, commit=False)
            db.rollback()
            result = audit_final_decision_artifacts(db=db, store=self.store, orphan_grace_seconds=0)
        self.assertEqual("review_required", result["status"])
        self.assertEqual(0, result["references_checked"])
        self.assertEqual(1, len(result["candidate_orphans"]))
        self.assertFalse(result["deletion_performed"])
        self.assertTrue((self.store.artifact_root / result["candidate_orphans"][0]).exists())
