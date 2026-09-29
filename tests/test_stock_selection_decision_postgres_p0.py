"""P0 decision/outbox transaction checks against the dedicated test PostgreSQL."""
from __future__ import annotations

import os
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from uuid import uuid4

from sqlalchemy import delete, func, select
from tests.postgres_safety import create_verified_test_engine
from sqlalchemy.orm import Session

from app.models.tables import CNSelectionDecision, CNSelectionPublication, WorkspaceSnapshot
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.artifact_audit import audit_final_decision_artifacts
from app.services.stock_selection.decision_ledger import dispatch_publication_message, freeze_final_decisions


class _Notifier:
    def __init__(self) -> None:
        self.calls = 0

    def send_event(self, *, event_type, title, body, channels):
        self.calls += 1
        return {"status": "success", "sent": channels, "failed": [], "provider_message_id": "om_isolated_test"}


class _CrashNotifier:
    def __init__(self) -> None:
        self.calls = 0

    def send_event(self, *, event_type, title, body, channels):
        self.calls += 1
        raise SystemExit("simulated process exit while provider call is in flight")


class _ErrorNotifier:
    def __init__(self) -> None:
        self.calls = 0

    def send_event(self, *, event_type, title, body, channels):
        self.calls += 1
        raise RuntimeError("mock provider timeout")


class _UnreadableStore:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def write(self, *args, **kwargs):
        return self.delegate.write(*args, **kwargs)

    def read(self, *_args, **_kwargs):
        raise RuntimeError("injected cold artifact read failure")


class _ExitBeforeWriteStore:
    def write(self, *_args, **_kwargs):
        os._exit(41)


def _crash_worker(stage: str, protocol_id: str, artifact_root: str) -> None:
    """A separate OS process, restricted to the configured dedicated test DB."""
    engine = create_verified_test_engine()
    report = {
        "report_date": "2026-09-11", "saved_at": "2026-09-11T20:00:00+08:00",
        "input_market_dates": {"CN": "2026-09-11"},
        "market_recommendations": [{"ticker": "600000.SS", "symbol_status": "ready"}],
        "market_recommendations_meta": {"status": "ready", "market_health": "graded"},
        "model_qualification": {"CN": {"protocol_id": protocol_id}},
    }
    message = {"market": "CN", "title": "isolated process test", "body": "not a real message"}
    with Session(engine) as db:
        actual = db.connection().exec_driver_sql("select current_database()").scalar()
        if not str(actual or "").endswith("_test"):
            raise RuntimeError("crash worker refused non-test PostgreSQL database")
        store = _ExitBeforeWriteStore() if stage == "before_cold_write" else JsonPayloadArtifactStore(Path(artifact_root))
        freeze_final_decisions(
            report, db=db, store=store, commit=stage == "after_outbox_commit",
            publication_messages=[message], publication_channels=["test"],
        )
        os._exit({"after_cold_before_commit": 42, "after_outbox_commit": 43}[stage])


class DecisionPostgresP0Tests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_verified_test_engine()
        cls.addClassCleanup(cls.engine.dispose)

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.store = JsonPayloadArtifactStore(Path(self.temp.name))
        self.protocol_id = f"isolated-p0-{uuid4().hex}"
        self.snapshot_ids: list[int] = []
        self.decision_ids: list[str] = []

    def tearDown(self) -> None:
        # Only rows carrying this test's random protocol/receipt IDs are touched.
        with Session(self.engine) as db:
            decision_ids = db.scalars(select(CNSelectionDecision.decision_id).where(
                CNSelectionDecision.protocol_id == self.protocol_id
            )).all()
            if decision_ids:
                db.execute(delete(CNSelectionPublication).where(
                    CNSelectionPublication.decision_id.in_(decision_ids)
                ))
                for decision_id in decision_ids:
                    db.execute(delete(WorkspaceSnapshot).where(
                        WorkspaceSnapshot.snapshot_type == "stock_selection_final_decisions:daily_report:v1",
                        WorkspaceSnapshot.payload_json.like(f"%{decision_id}%"),
                    ))
            db.execute(delete(CNSelectionDecision).where(CNSelectionDecision.protocol_id == self.protocol_id))
            if self.snapshot_ids:
                db.execute(delete(WorkspaceSnapshot).where(WorkspaceSnapshot.id.in_(self.snapshot_ids)))
            db.commit()
        self.temp.cleanup()

    def _run_crash_stage(self, stage: str, expected_exit: int) -> None:
        process = get_context("spawn").Process(
            target=_crash_worker, args=(stage, self.protocol_id, self.temp.name),
        )
        process.start()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            self.fail(f"crash worker did not reach {stage}")
        self.assertEqual(expected_exit, process.exitcode, f"crash worker failed at {stage}")

    def test_process_crash_before_cold_write_leaves_no_artifact_or_index(self) -> None:
        self._run_crash_stage("before_cold_write", 41)
        with Session(self.engine) as db:
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision).where(
                CNSelectionDecision.protocol_id == self.protocol_id
            )))
        self.assertEqual([], list(Path(self.temp.name).rglob("*.json.gz")))

    def test_process_crash_after_cold_write_before_commit_rolls_back_index(self) -> None:
        self._run_crash_stage("after_cold_before_commit", 42)
        with Session(self.engine) as db:
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision).where(
                CNSelectionDecision.protocol_id == self.protocol_id
            )))
            audit = audit_final_decision_artifacts(db=db, store=self.store, orphan_grace_seconds=0)
        self.assertTrue(audit["candidate_orphans"])
        self.assertFalse(audit["deletion_performed"])

    def test_process_crash_after_outbox_commit_recovers_without_duplicate(self) -> None:
        self._run_crash_stage("after_outbox_commit", 43)
        message = {"market": "CN", "title": "isolated process test", "body": "not a real message"}
        notifier = _Notifier()
        with Session(self.engine) as db:
            rows = db.scalars(select(CNSelectionDecision).where(
                CNSelectionDecision.protocol_id == self.protocol_id
            )).all()
            self.assertEqual(1, len(rows))
            self.decision_ids.append(rows[0].decision_id)
            publication = db.scalar(select(CNSelectionPublication).where(
                CNSelectionPublication.decision_id == rows[0].decision_id
            ))
            self.assertIsNotNone(publication)
            self.assertEqual("PENDING", publication.status)
            receipt = freeze_final_decisions(
                self._report(), db=db, store=self.store,
                publication_messages=[message], publication_channels=["test"],
            )
            self.snapshot_ids.append(receipt["snapshot_id"])
            self.assertEqual(rows[0].decision_id, receipt["decision_ids"]["CN"])
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionPublication).where(
                CNSelectionPublication.decision_id == rows[0].decision_id
            )))
            first = dispatch_publication_message(
                db=db, notifier=notifier, receipt=receipt, ordinal=1,
                message=message, event_type="isolated_test", channels=["test"], store=self.store,
            )
            self.assertEqual(["test"], first["sent"])
        with Session(self.engine) as db:
            repeated = dispatch_publication_message(
                db=db, notifier=notifier, receipt=receipt, ordinal=1,
                message=message, event_type="isolated_test", channels=["test"], store=self.store,
            )
            self.assertEqual("SENT", repeated["skipped"][0]["status"])
            self.assertEqual(1, notifier.calls)

    def _report(self) -> dict:
        return {
            "report_date": "2026-09-11", "saved_at": "2026-09-11T20:00:00+08:00",
            "input_market_dates": {"CN": "2026-09-11"},
            "market_recommendations": [{"ticker": "600000.SS", "symbol_status": "ready"}],
            "market_recommendations_meta": {"status": "ready", "market_health": "graded"},
            "model_qualification": {"CN": {"protocol_id": self.protocol_id}},
        }

    def test_pg_rollback_leaves_no_decision_or_outbox_pointer(self) -> None:
        message = {"market": "CN", "title": "isolated test", "body": "not sent"}
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(
                self._report(), db=db, store=self.store, commit=False,
                publication_messages=[message], publication_channels=["test"],
            )
            decision_id = receipt["decision_ids"]["CN"]
            snapshot_id = receipt["snapshot_id"]
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionDecision).where(
                CNSelectionDecision.decision_id == decision_id
            )))
            db.rollback()
        with Session(self.engine) as db:
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision).where(
                CNSelectionDecision.decision_id == decision_id
            )))
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionPublication).where(
                CNSelectionPublication.decision_id == decision_id
            )))
            self.assertIsNone(db.get(WorkspaceSnapshot, snapshot_id))

    def test_pg_unverified_cold_artifact_cannot_create_index(self) -> None:
        with Session(self.engine) as db:
            with self.assertRaisesRegex(RuntimeError, "cold artifact read failure"):
                freeze_final_decisions(
                    self._report(), db=db, store=_UnreadableStore(self.store), commit=False,
                )
            db.rollback()
        with Session(self.engine) as db:
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision).where(
                CNSelectionDecision.protocol_id == self.protocol_id
            )))

    def test_pg_commit_repeated_freeze_and_send_are_idempotent(self) -> None:
        message = {"market": "CN", "title": "isolated test", "body": "one frozen payload"}
        notifier = _Notifier()
        with Session(self.engine) as db:
            first = freeze_final_decisions(
                self._report(), db=db, store=self.store,
                publication_messages=[message], publication_channels=["test"],
            )
            self.snapshot_ids.append(first["snapshot_id"])
            self.decision_ids.append(first["decision_ids"]["CN"])
        with Session(self.engine) as db:
            second = freeze_final_decisions(
                self._report(), db=db, store=self.store,
                publication_messages=[message], publication_channels=["test"],
            )
            self.snapshot_ids.append(second["snapshot_id"])
            self.assertEqual(first["decision_ids"], second["decision_ids"])
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionDecision).where(
                CNSelectionDecision.protocol_id == self.protocol_id
            )))
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionPublication).where(
                CNSelectionPublication.decision_id == self.decision_ids[0]
            )))
            dispatched = dispatch_publication_message(
                db=db, notifier=notifier, receipt=second, ordinal=1,
                message=message, event_type="isolated_test", channels=["test"], store=self.store,
            )
            self.assertEqual(["test"], dispatched["sent"])
        with Session(self.engine) as db:
            repeated = dispatch_publication_message(
                db=db, notifier=notifier, receipt=second, ordinal=1,
                message=message, event_type="isolated_test", channels=["test"], store=self.store,
            )
            self.assertEqual("SENT", repeated["skipped"][0]["status"])
            self.assertEqual(1, notifier.calls)
            row = db.scalar(select(CNSelectionPublication).where(
                CNSelectionPublication.decision_id == self.decision_ids[0]
            ))
            self.assertEqual("om_isolated_test", row.provider_message_id)

    def test_pg_crash_after_claim_does_not_blindly_resend(self) -> None:
        message = {"market": "CN", "title": "isolated crash test", "body": "uncertain provider outcome"}
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(
                self._report(), db=db, store=self.store,
                publication_messages=[message], publication_channels=["test"],
            )
            self.snapshot_ids.append(receipt["snapshot_id"])
            self.decision_ids.append(receipt["decision_ids"]["CN"])
        crash_notifier = _CrashNotifier()
        with Session(self.engine) as db, self.assertRaises(SystemExit):
            dispatch_publication_message(
                db=db, notifier=crash_notifier, receipt=receipt, ordinal=1,
                message=message, event_type="isolated_test", channels=["test"], store=self.store,
            )
        retry_notifier = _Notifier()
        with Session(self.engine) as db:
            row = db.scalar(select(CNSelectionPublication).where(
                CNSelectionPublication.decision_id == self.decision_ids[0]
            ))
            self.assertEqual("SENDING", row.status)
            self.assertEqual(1, row.attempt_count)
            retry = dispatch_publication_message(
                db=db, notifier=retry_notifier, receipt=receipt, ordinal=1,
                message=message, event_type="isolated_test", channels=["test"], store=self.store,
            )
            self.assertEqual("SENDING", retry["skipped"][0]["status"])
            self.assertEqual(0, retry_notifier.calls)

    def test_pg_provider_exception_becomes_unknown_without_retry(self) -> None:
        message = {"market": "CN", "title": "isolated timeout test", "body": "uncertain provider outcome"}
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(
                self._report(), db=db, store=self.store,
                publication_messages=[message], publication_channels=["test"],
            )
            self.snapshot_ids.append(receipt["snapshot_id"])
            self.decision_ids.append(receipt["decision_ids"]["CN"])
        error_notifier = _ErrorNotifier()
        with Session(self.engine) as db:
            failed = dispatch_publication_message(
                db=db, notifier=error_notifier, receipt=receipt, ordinal=1,
                message=message, event_type="isolated_test", channels=["test"], store=self.store,
            )
            self.assertEqual("failed", failed["status"])
        with Session(self.engine) as db:
            row = db.scalar(select(CNSelectionPublication).where(
                CNSelectionPublication.decision_id == self.decision_ids[0]
            ))
            self.assertEqual("UNKNOWN", row.status)
            self.assertEqual(1, row.attempt_count)
            retry = dispatch_publication_message(
                db=db, notifier=error_notifier, receipt=receipt, ordinal=1,
                message=message, event_type="isolated_test", channels=["test"], store=self.store,
            )
            self.assertEqual("UNKNOWN", retry["skipped"][0]["status"])
            self.assertEqual(1, error_notifier.calls)

    def test_pg_same_day_revision_abstain_and_duplicate_rejection(self) -> None:
        with Session(self.engine) as db:
            original = freeze_final_decisions(self._report(), db=db, store=self.store)
            self.snapshot_ids.append(original["snapshot_id"])
            self.decision_ids.append(original["decision_ids"]["CN"])
            revised_report = self._report()
            revised_report["market_recommendations"] = [{"ticker": "000001.SZ", "symbol_status": "ready"}]
            revised = freeze_final_decisions(revised_report, db=db, store=self.store)
            self.snapshot_ids.append(revised["snapshot_id"])
            self.decision_ids.append(revised["decision_ids"]["CN"])
            abstain_report = self._report()
            abstain_report["market_recommendations"] = []
            abstain = freeze_final_decisions(abstain_report, db=db, store=self.store)
            self.snapshot_ids.append(abstain["snapshot_id"])
            self.decision_ids.append(abstain["decision_ids"]["CN"])
            rows = db.scalars(select(CNSelectionDecision).where(
                CNSelectionDecision.protocol_id == self.protocol_id
            ).order_by(CNSelectionDecision.id)).all()
            self.assertEqual(3, len(rows))
            self.assertEqual(rows[0].decision_id, rows[1].supersedes_decision_id)
            self.assertEqual(rows[1].decision_id, rows[2].supersedes_decision_id)
            self.assertEqual("ABSTAIN", rows[2].decision_type)
            self.assertEqual(0, rows[2].candidate_count)
            duplicate_report = self._report()
            duplicate_report["market_recommendations"] *= 2
            with self.assertRaisesRegex(ValueError, "Duplicate CN candidate"):
                freeze_final_decisions(duplicate_report, db=db, store=self.store)
            self.assertEqual(3, db.scalar(select(func.count()).select_from(CNSelectionDecision).where(
                CNSelectionDecision.protocol_id == self.protocol_id
            )))
