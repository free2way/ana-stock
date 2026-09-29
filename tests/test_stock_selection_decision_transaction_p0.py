from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.tables import AppSetting, CNSelectionDecision, CNSelectionPublication, USSelectionPublication, WorkspaceSnapshot
from app.services.ai_daily_report import (
    load_ai_daily_report, render_ai_daily_report_message,
    render_ai_daily_report_push_messages, save_ai_daily_report,
)
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.decision_ledger import (
    dispatch_publication_message, freeze_final_decisions,
)
from app.services.stock_selection.publication_guard import (
    APPROVAL_REGISTRY_KEY, load_trusted_model_qualifications, prepare_report_for_publication,
)


def _report(ticker="600000.SS"):
    return {
        "report_date": "2026-09-11", "saved_at": "2026-09-11T20:00:00+08:00",
        "input_market_dates": {"CN": "2026-09-11"},
        "market_recommendations": [{"ticker": ticker, "symbol_status": "ready", "model_score": 1}],
        "market_recommendations_meta": {"status": "ready", "market_health": "graded"},
        "model_qualification": {"CN": {"status": "QUALIFIED", "protocol_approved": True,
                                       "protocol_id": "test-approved-protocol",
                                       "model_artifact_sha256": "a" * 64}},
    }


def _regime():
    return {"CN": {"market": "CN", "snapshot_date": "2026-09-11",
        "generated_at": "2026-09-11T18:00:00+08:00", "risk_regime": "risk_on",
        "buy_gate": "ALLOW", "max_position_scale": 1.0}}


class _Notifier:
    def __init__(self):
        self.calls = 0

    def send_event(self, *, event_type, title, body, channels):
        self.calls += 1
        return {"status": "success", "sent": channels, "failed": [],
                "provider_message_id": "om_test"}


class _TimeoutNotifier(_Notifier):
    def send_event(self, *, event_type, title, body, channels):
        self.calls += 1
        return {"status": "failed", "sent": [],
                "failed": [{"channel": channels[0], "message": "provider request timeout after acceptance"}]}


class DecisionTransactionP0Tests(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.temp = TemporaryDirectory()
        self.store = JsonPayloadArtifactStore(Path(self.temp.name))

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def test_repeat_decision_and_outbox_are_idempotent_and_revision_supersedes(self):
        message = {"market": "CN", "title": "日报", "body": "冻结名单"}
        with Session(self.engine) as db:
            first = freeze_final_decisions(_report(), db=db, store=self.store,
                                           publication_messages=[message], publication_channels=["feishu"])
            second = freeze_final_decisions(_report(), db=db, store=self.store,
                                            publication_messages=[message], publication_channels=["feishu"])
            self.assertEqual(first["decision_ids"], second["decision_ids"])
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionDecision)))
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionPublication)))
            changed = _report("000001.SZ")
            third = freeze_final_decisions(changed, db=db, store=self.store)
            rows = db.scalars(select(CNSelectionDecision).order_by(CNSelectionDecision.id)).all()
            self.assertEqual(2, len(rows))
            self.assertEqual(first["decision_ids"]["CN"], rows[-1].supersedes_decision_id)
            self.assertNotEqual(first["decision_ids"], third["decision_ids"])

    def test_rollback_cannot_leave_pg_decision_or_outbox_index(self):
        message = {"market": "CN", "title": "日报", "body": "内容"}
        with Session(self.engine) as db:
            freeze_final_decisions(_report(), db=db, store=self.store, commit=False,
                                   publication_messages=[message], publication_channels=["feishu"])
            db.rollback()
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision)))
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionPublication)))
            self.assertEqual(0, db.scalar(select(func.count()).select_from(WorkspaceSnapshot)))

    def test_report_setting_failure_rolls_back_frozen_index_and_history(self):
        with Session(self.engine) as db, patch(
            "app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore",
            return_value=self.store,
        ), patch(
            "app.services.ai_daily_report.AppSettingRepository.set",
            side_effect=RuntimeError("injected setting failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                save_ai_daily_report(_report(), db=db)
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision)))
            self.assertEqual(0, db.scalar(select(func.count()).select_from(WorkspaceSnapshot)))

    def test_successful_save_commits_report_and_market_index_together(self):
        report = _report()
        with Session(self.engine) as db, patch(
            "app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore",
            return_value=self.store,
        ):
            receipt = save_ai_daily_report(report, db=db)
            self.assertEqual(1, db.scalar(select(func.count()).select_from(CNSelectionDecision)))
            self.assertEqual(2, db.scalar(select(func.count()).select_from(WorkspaceSnapshot)))
            loaded = load_ai_daily_report(db=db)
            self.assertEqual(receipt["decision_ids"], loaded["final_decision_receipt"]["decision_ids"])
            self.assertEqual(receipt["decision_ids"], report["final_decision_receipt"]["decision_ids"])

    def test_qualification_change_between_render_and_save_rejects_stale_message(self):
        report = _report()
        messages = render_ai_daily_report_push_messages(report)
        self.assertIn("600000.SS", messages[1]["body"])
        with Session(self.engine) as db:
            with self.assertRaisesRegex(RuntimeError, "render again"):
                save_ai_daily_report(
                    report, db=db, publication_messages=messages,
                    publication_channels=["feishu"],
                )
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision)))
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionPublication)))

    def test_sent_delivery_key_is_not_resent(self):
        message = {"market": "CN", "title": "日报", "body": "内容"}
        notifier = _Notifier()
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(_report(), db=db, store=self.store,
                                             publication_messages=[message], publication_channels=["feishu"])
            first = dispatch_publication_message(
                db=db, notifier=notifier, receipt=receipt, ordinal=1, message=message,
                event_type="stock_recommendation", channels=["feishu"], store=self.store,
            )
            self.assertEqual(["feishu"], first["sent"])
            repeated_receipt = freeze_final_decisions(_report(), db=db, store=self.store,
                                                       publication_messages=[message], publication_channels=["feishu"])
            second = dispatch_publication_message(
                db=db, notifier=notifier, receipt=repeated_receipt, ordinal=1, message=message,
                event_type="stock_recommendation", channels=["feishu"], store=self.store,
            )
            self.assertEqual(1, notifier.calls)
            self.assertEqual("SENT", second["skipped"][0]["status"])
            row = db.scalar(select(CNSelectionPublication))
            self.assertEqual("om_test", row.provider_message_id)

    def test_portfolio_summary_is_a_durable_cn_intent_not_a_direct_send(self):
        report = _report()
        messages = render_ai_daily_report_push_messages(report)
        self.assertEqual(["CN", "CN"], [item["market"] for item in messages])
        notifier = _Notifier()
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(report, db=db, store=self.store,
                                             publication_messages=messages,
                                             publication_channels=["feishu"])
            self.assertEqual(2, db.scalar(select(func.count()).select_from(CNSelectionPublication)))
            for ordinal, message in enumerate(messages, start=1):
                dispatch_publication_message(
                    db=db, notifier=notifier, receipt=receipt, ordinal=ordinal, message=message,
                    event_type="stock_recommendation", channels=["feishu"], store=self.store,
                )
            for ordinal, message in enumerate(messages, start=1):
                repeated = dispatch_publication_message(
                    db=db, notifier=notifier, receipt=receipt, ordinal=ordinal, message=message,
                    event_type="stock_recommendation", channels=["feishu"], store=self.store,
                )
                self.assertEqual("SENT", repeated["skipped"][0]["status"])
        self.assertEqual(2, notifier.calls)

    def test_us_only_report_does_not_create_phantom_cn_message(self):
        report = {
            "report_date": "2026-09-11", "saved_at": "2026-09-11T20:00:00+08:00",
            "market_recommendations_meta": {"status": "not_requested"},
            "us_model_recommendations": [],
            "us_model_recommendations_meta": {"status": "not_ready"},
        }
        messages = render_ai_daily_report_push_messages(report)
        self.assertEqual(["US"], [item["market"] for item in messages])
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(report, db=db, store=self.store,
                                             publication_messages=messages,
                                             publication_channels=["feishu"])
            self.assertNotIn("CN", receipt["decision_ids"])
            self.assertEqual(1, db.scalar(select(func.count()).select_from(USSelectionPublication)))

    def test_message_without_any_durable_intent_cannot_send_even_if_market_is_null(self):
        notifier = _Notifier()
        with Session(self.engine) as db:
            with self.assertRaisesRegex(RuntimeError, "no durable publication intent"):
                dispatch_publication_message(
                    db=db, notifier=notifier,
                    receipt={"publications": [], "decision_ids": {}}, ordinal=1,
                    message={"market": None, "title": "summary", "body": "AI 追加买入名单"},
                    event_type="stock_recommendation", channels=["feishu"], store=self.store,
                )
        self.assertEqual(0, notifier.calls)

    def test_ambiguous_timeout_is_unknown_and_never_blindly_resent(self):
        message = {"market": "CN", "title": "日报", "body": "内容"}
        notifier = _TimeoutNotifier()
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(_report(), db=db, store=self.store,
                                             publication_messages=[message], publication_channels=["feishu"])
            dispatch_publication_message(
                db=db, notifier=notifier, receipt=receipt, ordinal=1, message=message,
                event_type="stock_recommendation", channels=["feishu"], store=self.store,
            )
            repeated = freeze_final_decisions(_report(), db=db, store=self.store,
                                              publication_messages=[message], publication_channels=["feishu"])
            result = dispatch_publication_message(
                db=db, notifier=notifier, receipt=repeated, ordinal=1, message=message,
                event_type="stock_recommendation", channels=["feishu"], store=self.store,
            )
            self.assertEqual(1, notifier.calls)
            self.assertEqual("UNKNOWN", result["skipped"][0]["status"])

    def test_changed_message_or_corrupt_cold_artifact_never_crosses_provider_boundary(self):
        message = {"market": "CN", "title": "日报", "body": "冻结内容"}
        notifier = _Notifier()
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(_report(), db=db, store=self.store,
                                             publication_messages=[message], publication_channels=["feishu"])
            changed = {**message, "body": "AI 后处理新增买入建议"}
            with self.assertRaisesRegex(RuntimeError, "differs from the frozen"):
                dispatch_publication_message(
                    db=db, notifier=notifier, receipt=receipt, ordinal=1, message=changed,
                    event_type="stock_recommendation", channels=["feishu"], store=self.store,
                )
            self.assertEqual(0, notifier.calls)
            row = db.scalar(select(CNSelectionPublication))
            self.assertEqual("PENDING", row.status)
            reference = json.loads(row.artifact_reference_json)
            (self.store.artifact_root / reference["relative_path"]).write_bytes(b"corrupted")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                dispatch_publication_message(
                    db=db, notifier=notifier, receipt=receipt, ordinal=1, message=message,
                    event_type="stock_recommendation", channels=["feishu"], store=self.store,
                )
            self.assertEqual(0, notifier.calls)
            self.assertEqual("PENDING", row.status)

    def test_explicit_abstain_has_zero_candidates_and_no_watch_replacement(self):
        report = _report()
        report["market_recommendations"] = []
        report["market_watch_recommendations"] = [{"ticker": "000001.SZ"}]
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(report, db=db, store=self.store)
            row = db.scalar(select(CNSelectionDecision).where(
                CNSelectionDecision.decision_id == receipt["decision_ids"]["CN"]
            ))
            self.assertEqual("ABSTAIN", row.decision_type)
            self.assertEqual(0, row.candidate_count)

    def test_unqualified_and_suspended_top_are_never_published_or_replaced(self):
        raw = _report()
        raw["market_recommendations"].append({"ticker": "000001.SZ", "symbol_status": "ready"})
        raw["market_recommendations"][0]["symbol_status"] = "suspended"
        raw["model_qualification"]["CN"]["top_n"] = 1
        guarded = prepare_report_for_publication(raw, approved_qualifications={
            "CN": {**raw["model_qualification"]["CN"], "approved_by": "test-owner",
                   "approved_at": "2026-09-11T12:00:00+08:00"},
        }, trusted_regime_snapshots=_regime())
        self.assertEqual([], guarded["market_recommendations"])
        self.assertEqual("symbol_not_tradable", guarded["publication_blocked_candidates"]["CN"][0]["publication_block_reason"])
        self.assertEqual("outside_frozen_top_n", guarded["publication_blocked_candidates"]["CN"][1]["publication_block_reason"])
        unapproved = deepcopy(raw)
        unapproved.pop("model_qualification")
        guarded = prepare_report_for_publication(unapproved)
        self.assertEqual([], guarded["market_recommendations"])
        self.assertEqual("not_ready", guarded["market_recommendations_meta"]["status"])

    def test_report_cannot_self_approve_qualification(self):
        raw = _report()
        guarded = prepare_report_for_publication(raw)
        self.assertEqual([], guarded["market_recommendations"])
        self.assertEqual({}, guarded["model_qualification"])
        self.assertEqual("not_ready", guarded["market_recommendations_meta"]["status"])

    def test_trusted_registry_requires_identity_match_and_complete_approval(self):
        raw = _report()
        approval = {**raw["model_qualification"]["CN"], "approved_by": "test-owner",
                    "approved_at": "2026-09-11T12:00:00+08:00"}
        with Session(self.engine) as db:
            setting = AppSetting(key=APPROVAL_REGISTRY_KEY, value=json.dumps({"CN": approval}),
                                 updated_at="2026-09-11T12:00:00+08:00")
            db.add(setting)
            db.commit()
            trusted = load_trusted_model_qualifications(db=db)
            self.assertIn("CN", trusted)
            self.assertEqual(1, len(prepare_report_for_publication(
                raw, approved_qualifications=trusted, trusted_regime_snapshots=_regime(),
            )["market_recommendations"]))
            forged = deepcopy(raw)
            forged["model_qualification"]["CN"]["model_artifact_sha256"] = "b" * 64
            self.assertEqual([], prepare_report_for_publication(
                forged, approved_qualifications=trusted,
            )["market_recommendations"])
            setting.value = json.dumps({"CN": {**approval, "approved_by": ""}})
            db.commit()
            self.assertEqual({}, load_trusted_model_qualifications(db=db))

    def test_empty_unqualified_cache_cannot_leak_strategy_names_into_push(self):
        raw = _report()
        raw["market_recommendations"] = []
        raw.pop("model_qualification")
        raw["strategy"] = {"bullets": ["优先买入 600000.SS"]}
        guarded = prepare_report_for_publication(raw)
        text = "\n".join(row["body"] for row in render_ai_daily_report_push_messages(guarded))
        self.assertNotIn("600000.SS", text)
        self.assertIn("暂不可发布", text)
        full_text = render_ai_daily_report_message(guarded)
        self.assertNotIn("600000.SS", full_text)

    def test_empty_ready_market_is_not_ready_without_model_approval(self):
        raw = _report()
        raw["market_recommendations"] = []
        raw.pop("model_qualification")
        guarded = prepare_report_for_publication(raw)
        self.assertEqual("not_ready", guarded["market_recommendations_meta"]["status"])
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(guarded, db=db, store=self.store)
        self.assertEqual("NOT_READY", receipt["market_summary"]["CN"]["decision"])

    def test_unqualified_watch_rows_are_observation_ready_but_decision_stays_blocked(self):
        raw = _report()
        raw["market_recommendations"] = []
        raw["market_watch_recommendations"] = [{"ticker": "600000.SS", "name": "浦发银行"}]
        raw.pop("model_qualification")
        guarded = prepare_report_for_publication(raw)
        self.assertEqual("observation_ready", guarded["market_recommendations_meta"]["status"])
        text = "\n".join(item["body"] for item in render_ai_daily_report_push_messages(guarded))
        self.assertIn("浦发银行（600000.SS）", text)
        self.assertIn("不可执行", text)
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(guarded, db=db, store=self.store)
        self.assertEqual("NOT_READY", receipt["market_summary"]["CN"]["decision"])

    def test_unrequested_us_market_does_not_create_economic_decision(self):
        raw = _report()
        raw["us_model_recommendations"] = []
        raw["us_model_recommendations_meta"] = {"status": "not_requested"}
        guarded = prepare_report_for_publication(raw)
        with Session(self.engine) as db:
            receipt = freeze_final_decisions(guarded, db=db, store=self.store)
        self.assertEqual({"CN"}, set(receipt["decision_ids"]))

    def test_wrong_market_and_duplicate_names_fail_before_any_index_write(self):
        with Session(self.engine) as db:
            for tickers in (("AAPL",), ("600000.SS", "600000.SS")):
                report = _report()
                report["market_recommendations"] = [{"ticker": ticker} for ticker in tickers]
                with self.assertRaises(ValueError):
                    freeze_final_decisions(report, db=db, store=self.store)
            self.assertEqual(0, db.scalar(select(func.count()).select_from(CNSelectionDecision)))
