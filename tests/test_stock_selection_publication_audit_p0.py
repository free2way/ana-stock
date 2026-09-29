from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.tables import CNSelectionPublication
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.decision_ledger import freeze_final_decisions
from app.services.stock_selection.publication_audit import (
    audit_stock_selection_publications, find_feishu_provider_receipt_candidates,
)


class _HistoryProvider:
    def __init__(self, items):
        from types import SimpleNamespace
        self.settings = SimpleNamespace(feishu_webhook_url=None, feishu_chat_id="oc_test",
                                        feishu_app_id="cli_test")
        self.items = items
        self.calls = 0

    def _has_feishu_app_config(self):
        return True

    def list_feishu_chat_messages(self, **_kwargs):
        self.calls += 1
        return self.items


class PublicationAuditP0Tests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.temp = TemporaryDirectory()
        self.store = JsonPayloadArtifactStore(Path(self.temp.name))

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def _freeze(self, db):
        report = {
            "report_date": "2026-09-11", "saved_at": "2026-09-11T20:00:00+08:00",
            "input_market_dates": {"CN": "2026-09-11"},
            "market_recommendations": [],
            "market_recommendations_meta": {"status": "not_ready"},
        }
        return freeze_final_decisions(
            report, db=db, store=self.store,
            publication_messages=[{"market": "CN", "title": "未就绪", "body": "不推荐股票"}],
            publication_channels=["feishu"],
        )

    def test_uncertain_and_missing_provider_receipt_are_listed_without_mutation(self) -> None:
        with Session(self.engine) as db:
            self._freeze(db)
            row = db.scalar(select(CNSelectionPublication))
            row.status = "UNKNOWN"
            row.attempt_count = 1
            db.commit()
            audit = audit_stock_selection_publications(
                db=db, store=self.store, now=datetime.now(timezone.utc),
            )
            self.assertEqual("review_required", audit["status"])
            self.assertIn("provider_outcome_uncertain", audit["review_required"][0]["reasons"])
            self.assertEqual(0, audit["provider_send_calls_made"])
            self.assertEqual(0, audit["database_rows_changed"])
            self.assertEqual("UNKNOWN", db.scalar(select(CNSelectionPublication)).status)
            row.status = "SENT"
            db.commit()
            audit = audit_stock_selection_publications(db=db, store=self.store)
            self.assertIn("sent_without_provider_message_id", audit["review_required"][0]["reasons"])

    def test_corrupt_frozen_message_is_reported_not_deleted(self) -> None:
        with Session(self.engine) as db:
            self._freeze(db)
            row = db.scalar(select(CNSelectionPublication))
            reference = json.loads(row.artifact_reference_json)
            path = self.store.artifact_root / reference["relative_path"]
            path.write_bytes(b"corrupted")
            audit = audit_stock_selection_publications(db=db, store=self.store)
            self.assertIn("frozen_payload_unreadable", audit["review_required"][0]["reasons"])
            self.assertTrue(path.exists())

    def test_exact_feishu_history_match_is_only_a_candidate_and_never_updates_status(self) -> None:
        with Session(self.engine) as db:
            self._freeze(db)
            row = db.scalar(select(CNSelectionPublication))
            row.status = "UNKNOWN"
            row.updated_at = datetime.now(timezone.utc)
            db.flush()
            content = {"zh_cn": {"title": "【AI 日报已生成】未就绪", "content": [
                [{"tag": "text", "text": "通知类型：AI 日报已生成\n\n不推荐股票"}],
            ]}}
            matching = {"message_id": "om_exact", "chat_id": "oc_test", "deleted": False,
                        "sender": {"id": "cli_test", "sender_type": "app"},
                        "body": {"content": json.dumps(content, ensure_ascii=False)}}
            forged_sender = {**matching, "message_id": "om_other",
                             "sender": {"id": "cli_other", "sender_type": "app"}}
            provider = _HistoryProvider([forged_sender, matching])
            result = find_feishu_provider_receipt_candidates(db=db, provider=provider, store=self.store)
            self.assertEqual(1, provider.calls)
            self.assertEqual(["om_exact"], [item["provider_message_id"] for item in result["entries"][0]["matches"]])
            self.assertEqual(0, result["database_rows_changed"])
            self.assertEqual("UNKNOWN", row.status)
