from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.decision_ledger import build_final_decision_payload, freeze_final_decisions
from app.services.workspace_snapshot_retention import select_workspace_snapshot_retention
from app.services.ai_daily_report import save_ai_daily_report
from app.services.ai_daily_report_delivery import deliver_cn_ai_daily_report_to_feishu


def report():
    return {"report_date": "2026-09-11", "saved_at": "2026-09-11T21:00:00+08:00",
            "market_recommendations": [],
            "market_watch_recommendations": [{"ticker": "600000.SS", "name": "浦发银行"}],
            "market_recommendations_meta": {"status": "ready", "target_snapshot_date": "2026-09-11"},
            "input_market_dates": {"CN": "2026-09-11", "US": "2026-09-10"},
            "us_model_recommendations": [{"ticker": "ZZZ"}, {"ticker": "AAA"}],
            "rows": [{"ticker": "PRIVATE_HOLDING", "amount": 12345}],
            "market_candidates_all": [{"ticker": "NEVER_SUBSTITUTE"}]}


class FinalDecisionLedgerTests(TestCase):
    def test_empty_pool_order_market_separation_and_deep_copy(self):
        source = report()
        frozen = build_final_decision_payload(source)
        self.assertEqual("ABSTAIN", frozen["markets"]["CN"]["decision"])
        self.assertEqual([], frozen["markets"]["CN"]["candidates"])
        self.assertEqual(["ZZZ", "AAA"], [row["ticker"] for row in frozen["markets"]["US"]["candidates"]])
        self.assertNotIn("PRIVATE_HOLDING", str(frozen))
        self.assertNotIn("NEVER_SUBSTITUTE", str(frozen))
        source["market_watch_recommendations"][0]["name"] = "CHANGED"
        self.assertEqual("浦发银行", frozen["markets"]["CN"]["watch_candidates"][0]["name"])

    def test_not_ready_is_not_abstain_and_hk_has_own_group(self):
        source = report()
        source["market_recommendations_meta"]["status"] = "not_ready"
        source["hk_model_recommendations"] = [{"ticker": "0700.HK"}]
        frozen = build_final_decision_payload(source)
        self.assertEqual("NOT_READY", frozen["markets"]["CN"]["decision"])
        self.assertEqual("0700.HK", frozen["markets"]["HK"]["candidates"][0]["ticker"])
        self.assertEqual([], frozen["markets"]["HK"]["push_top5_candidates"])

    def test_ambiguous_legacy_or_malformed_candidates_cannot_be_published(self):
        for value in (None, {}, ""):
            source = report()
            source["market_recommendations"] = value
            with self.assertRaises(ValueError):
                build_final_decision_payload(source)

    def test_only_first_five_are_renderable_push_candidates(self):
        source = report()
        source["us_model_recommendations"] = [{"ticker": str(i)} for i in range(8)]
        frozen = build_final_decision_payload(source)
        self.assertEqual(8, len(frozen["markets"]["US"]["candidates"]))
        self.assertEqual(5, len(frozen["markets"]["US"]["push_top5_candidates"]))
        self.assertFalse(frozen["markets"]["CN"]["watch_published_in_push"])

    @patch("app.services.stock_selection.decision_ledger.WorkspaceSnapshotRepository")
    def test_cold_hash_reference_and_integrity(self, repository):
        repository.return_value.create_snapshot.return_value = SimpleNamespace(id=99)
        with TemporaryDirectory() as root:
            store = JsonPayloadArtifactStore(Path(root))
            receipt = freeze_final_decisions(report(), db=object(), store=store)
            self.assertEqual(99, receipt["snapshot_id"])
            self.assertEqual(build_final_decision_payload(report()), store.read(receipt["artifact"]))
            self.assertEqual("prepared_for_publication", receipt["state"])
            self.assertNotIn("candidates", repository.return_value.create_snapshot.call_args.kwargs["payload"])
            with patch.object(store, "read", side_effect=RuntimeError("corrupt")):
                with self.assertRaises(RuntimeError):
                    freeze_final_decisions(report(), db=object(), store=store)
            self.assertEqual(1, repository.return_value.create_snapshot.call_count)

    def test_all_evidence_is_protected_from_calendar_retention(self):
        rows = []
        for prefix in ("stock_selection_shadow_daily:", "stock_selection_shadow_evaluation:", "stock_selection_final_decisions:"):
            for _ in range(3):
                rows.append({"id": len(rows) + 1, "snapshot_type": prefix + "v1", "snapshot_date": "2025-01-01"})
        result = select_workspace_snapshot_retention(rows, today=date(2026, 9, 12))
        self.assertEqual([], result["candidate_delete_ids"])

    @patch("app.services.stock_selection.publication_guard.load_trusted_regime_snapshots", return_value={})
    @patch("app.services.ai_daily_report.get_latest_lake_trade_date", return_value="2026-09-11")
    @patch("app.services.stock_selection.publication_guard.load_trusted_model_qualifications", return_value={})
    @patch("app.services.ai_daily_report.WorkspaceSnapshotRepository")
    @patch("app.services.ai_daily_report.AppSettingRepository")
    @patch("app.services.stock_selection.decision_ledger.freeze_final_decisions")
    def test_save_links_receipt_and_does_not_publish_on_freeze_failure(self, freeze, settings, history, approval, lake, regime):
        freeze.return_value = {"snapshot_id": 99}
        result = save_ai_daily_report(report(), db=object())
        self.assertEqual({"snapshot_id": 99}, result)
        self.assertEqual(result, history.return_value.create_snapshot.call_args.kwargs["payload"]["final_decision_receipt"])
        freeze.side_effect = RuntimeError("freeze failed")
        settings.reset_mock()
        history.reset_mock()
        with self.assertRaises(RuntimeError):
            save_ai_daily_report(report(), db=object())
        settings.assert_not_called()
        history.assert_not_called()

    @patch("app.services.ai_daily_report_delivery.load_trusted_regime_snapshots", return_value={})
    @patch("app.services.ai_daily_report_delivery.PushNotificationService")
    @patch("app.services.ai_daily_report_delivery.load_trusted_model_qualifications", return_value={})
    @patch("app.services.ai_daily_report_delivery.save_ai_daily_report", side_effect=RuntimeError("freeze failed"))
    @patch("app.services.ai_daily_report_delivery.build_ai_daily_report", return_value=report())
    def test_failed_freeze_prevents_feishu_send(self, build, save, approval, notifier, regime):
        with self.assertRaises(RuntimeError):
            deliver_cn_ai_daily_report_to_feishu()
        notifier.assert_not_called()
