from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.tables import AppSetting, CNSelectionDecision, CNSelectionPublication, WorkspaceSnapshot
from app.services.ai_daily_report import save_ai_daily_report, render_ai_daily_report_push_messages, load_ai_daily_report
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.publication_guard import (
    APPROVAL_REGISTRY_KEY, load_trusted_regime_snapshots, prepare_report_for_publication,
)
from tests.test_stock_selection_decision_transaction_p0 import _report, _regime


class RegimePublicationIntegrationTests(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.temp = TemporaryDirectory()
        self.store = JsonPayloadArtifactStore(Path(self.temp.name))
        self.raw = _report()
        self.raw["decision_cutoff_at"] = "2026-09-11T20:00:00+08:00"
        self.approval = {"CN": {**self.raw["model_qualification"]["CN"], "approved_by": "fixture-owner",
                               "approved_at": "2026-09-11T18:00:00+08:00"}}

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def guard(self, report=None, snapshot=None):
        return prepare_report_for_publication(self.raw if report is None else report,
            approved_qualifications=self.approval, trusted_regime_snapshots=_regime() if snapshot is None else snapshot)

    def seed(self, db, snapshot=None):
        db.add(AppSetting(key=APPROVAL_REGISTRY_KEY, value=json.dumps(self.approval), updated_at=self.raw["decision_cutoff_at"]))
        self.add_snapshot(db, snapshot or _regime()["CN"])

    def add_snapshot(self, db, snapshot):
        db.add(WorkspaceSnapshot(snapshot_type="market_regime_snapshot:CN", snapshot_date=snapshot["snapshot_date"],
            payload_json=json.dumps(snapshot), created_at=snapshot["generated_at"]))
        db.commit()

    def test_blocked_snapshot_removes_approved_picks_and_ai_cannot_self_allow(self):
        report = deepcopy(self.raw)
        report["market_context"] = {"buy_gate": "ALLOW", "risk_regime": "risk_on"}
        for state in ("crash", "rebound_failed", "unknown"):
            result = self.guard(report, {"CN": {**_regime()["CN"], "risk_regime": state}})
            self.assertEqual([], result["market_recommendations"])
            self.assertEqual("market_regime_blocked", result["market_recommendations_meta"]["publication_blocker"])
            self.assertEqual(1, len(result["publication_blocked_candidates"]["CN"]))
            text = "\n".join(item["body"] for item in render_ai_daily_report_push_messages(result))
            self.assertNotIn("600000.SS", text)
            self.assertIn("暂停", text)

    def test_missing_stale_future_and_wrong_market_snapshots_block(self):
        for snapshot in ({}, {"CN": {**_regime()["CN"], "snapshot_date": "2026-09-10"}},
                         {"CN": {**_regime()["CN"], "generated_at": "2026-09-11T21:00:00+08:00"}},
                         {"CN": {**_regime()["CN"], "market": "US"}}):
            self.assertEqual([], self.guard(snapshot=snapshot)["market_recommendations"])

    def test_guard_is_idempotent_and_preserves_exclusion_audit(self):
        snapshot = {"CN": {**_regime()["CN"], "buy_gate": "BLOCK"}}
        once = self.guard(snapshot=snapshot)
        self.assertEqual(once, self.guard(once, snapshot))

    def test_approval_cannot_expand_product_beyond_top5(self):
        self.approval["CN"]["top_n"] = 10
        report = deepcopy(self.raw)
        report["market_recommendations"] = [{"ticker": f"60000{i}.SS", "symbol_status": "ready"} for i in range(7)]
        result = self.guard(report)
        self.assertEqual(5, len(result["market_recommendations"]))
        self.assertEqual(2, len(result["publication_blocked_candidates"]["CN"]))

    def test_conflicting_input_market_dates_block(self):
        report = deepcopy(self.raw)
        report["market_recommendations_meta"]["target_snapshot_date"] = "2026-09-10"
        self.assertEqual([], self.guard(report)["market_recommendations"])

    def test_us_only_flow_is_not_subject_to_cn_gate(self):
        report = {"report_date": "2026-09-11", "us_model_recommendations": [{"ticker": "AAPL", "symbol_status": "ready"}],
                  "us_model_recommendations_meta": {"status": "ready", "market_health": "graded"},
                  "model_qualification": {"US": self.approval["CN"]}}
        result = prepare_report_for_publication(report, approved_qualifications={"US": self.approval["CN"]})
        self.assertEqual(1, len(result["us_model_recommendations"]))
        self.assertNotIn("market_recommendations", result)

    def test_render_freeze_report_and_outbox_share_guarded_decision(self):
        crash = {**_regime()["CN"], "risk_regime": "crash", "buy_gate": "BLOCK", "max_position_scale": 0.0}
        with Session(self.engine) as db, patch("app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore", return_value=self.store):
            self.seed(db, crash)
            report = self.guard(snapshot=load_trusted_regime_snapshots(db=db))
            messages = render_ai_daily_report_push_messages(report)
            receipt = save_ai_daily_report(report, db=db, publication_messages=messages, publication_channels=["feishu"])
            self.assertEqual("NOT_READY", receipt["market_summary"]["CN"]["decision"])
            decision = db.scalar(select(CNSelectionDecision))
            self.assertEqual(0, decision.candidate_count)
            stored = self.store.read(json.loads(decision.artifact_reference_json))
            self.assertEqual("BLOCK", stored["markets"]["CN"]["metadata"]["regime_policy"]["buy_gate"])
            self.assertEqual("2026-09-11", stored["markets"]["CN"]["input_market_date"])
            self.assertEqual([], load_ai_daily_report(db=db)["market_recommendations"])
            for row in db.scalars(select(CNSelectionPublication)).all():
                self.assertNotIn("600000.SS", self.store.read(json.loads(row.artifact_reference_json))["body"])

    def test_changed_risk_after_render_rejects_before_any_decision_or_outbox(self):
        with Session(self.engine) as db:
            self.seed(db)
            report = self.guard()
            messages = render_ai_daily_report_push_messages(report)
            self.add_snapshot(db, {**_regime()["CN"], "risk_regime": "crash"})
            with self.assertRaisesRegex(RuntimeError, "render again"):
                save_ai_daily_report(report, db=db, publication_messages=messages, publication_channels=["feishu"])
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision)))
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionPublication)))

    def test_arbitrary_message_cannot_bypass_guarded_candidate_list(self):
        with Session(self.engine) as db:
            self.seed(db)
            report = self.guard()
            with self.assertRaisesRegex(RuntimeError, "message does not match"):
                save_ai_daily_report(report, db=db, publication_messages=[{"market": "CN", "title": "买入", "body": "买入 000001.SZ"}], publication_channels=["feishu"])
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionPublication)))

    def test_allowed_report_saves_idempotently_and_keeps_input_date(self):
        with Session(self.engine) as db, patch("app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore", return_value=self.store):
            self.seed(db)
            report = self.guard()
            messages = render_ai_daily_report_push_messages(report)
            first = save_ai_daily_report(report, db=db, publication_messages=messages, publication_channels=["feishu"])
            second = save_ai_daily_report(report, db=db, publication_messages=messages, publication_channels=["feishu"])
            self.assertEqual(first["decision_ids"], second["decision_ids"])
            self.assertEqual("2026-09-11", report["input_market_dates"]["CN"])
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionDecision)))
            self.assertEqual(2, db.scalar(select(func.count()).select_from(CNSelectionPublication)))

    def test_changed_budget_gets_distinct_economic_decision_identity(self):
        with Session(self.engine) as db, patch("app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore", return_value=self.store):
            self.seed(db)
            first_report = self.guard()
            first = save_ai_daily_report(first_report, db=db)
            snapshot = {**_regime()["CN"], "max_position_scale": 0.2}
            self.add_snapshot(db, snapshot)
            second_report = self.guard(snapshot={"CN": snapshot})
            second = save_ai_daily_report(second_report, db=db)
            self.assertNotEqual(first["decision_ids"], second["decision_ids"])

    def test_readiness_only_canonical_message_can_be_frozen(self):
        snapshot = {**_regime()["CN"], "risk_regime": "crash"}
        with Session(self.engine) as db, patch("app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore", return_value=self.store):
            self.seed(db, snapshot)
            report = self.guard(snapshot={"CN": snapshot})
            message = render_ai_daily_report_push_messages(report)[1]
            save_ai_daily_report(report, db=db, publication_messages=[message], publication_channels=["feishu"])
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionPublication)))
