import unittest
from unittest.mock import patch

from app.services.ai_daily_report_delivery import deliver_cn_ai_daily_report_to_feishu


class AiDailyReportDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.regime_patch = patch("app.services.ai_daily_report_delivery.load_trusted_regime_snapshots", return_value={})
        self.regimes = self.regime_patch.start()
        self.addCleanup(self.regime_patch.stop)

    @patch("app.services.ai_daily_report_delivery.dispatch_publication_message")
    @patch("app.services.ai_daily_report_delivery.load_trusted_model_qualifications")
    @patch("app.services.ai_daily_report_delivery.save_ai_daily_report")
    @patch("app.services.ai_daily_report_delivery.render_ai_daily_report_push_messages")
    @patch("app.services.ai_daily_report_delivery.build_ai_daily_report")
    @patch("app.services.ai_daily_report_delivery.PushNotificationService")
    def test_ready_report_is_sent_only_to_feishu(
        self,
        notifier_class,
        build_report,
        render_messages,
        save_report,
        trusted_qualification,
        dispatch,
    ) -> None:
        report = {
            "report_date": "2026-08-21",
            "market_recommendations": [],
            "market_recommendations_meta": {"status": "ready", "target_snapshot_date": "2026-08-21"},
            "model_qualification": {"CN": {"status": "QUALIFIED", "protocol_approved": True,
                                           "protocol_id": "test-approved-protocol",
                                           "model_artifact_sha256": "a" * 64}},
        }
        trusted_qualification.return_value = {"CN": {**report["model_qualification"]["CN"],
                                                       "approved_by": "test-owner",
                                                       "approved_at": "2026-08-20T12:00:00+08:00"}}
        self.regimes.return_value = {"CN": {"market": "CN", "snapshot_date": "2026-08-21",
            "generated_at": "2026-08-21T18:00:00+08:00", "risk_regime": "risk_on", "buy_gate": "ALLOW",
            "max_position_scale": 1.0}}
        build_report.return_value = report
        render_messages.return_value = [
            {"title": "日报 1", "body": "内容 1"},
            {"title": "日报 2", "body": "内容 2"},
        ]
        notifier_class.return_value.send_event.return_value = {
            "status": "success",
            "sent": ["feishu"],
            "failed": [],
        }
        dispatch.side_effect = lambda **kwargs: kwargs["notifier"].send_event(
            event_type=kwargs["event_type"], title=kwargs["message"]["title"],
            body=kwargs["message"]["body"], channels=kwargs["channels"],
        )

        result = deliver_cn_ai_daily_report_to_feishu()

        self.assertEqual("success", result["status"])
        self.assertEqual("daily_report", result["delivery_kind"])
        self.assertEqual(2, result["messages"])
        self.assertEqual(["feishu"], result["sent"])
        self.assertIs(report, save_report.call_args.args[0])
        self.assertEqual(["feishu"], save_report.call_args.kwargs["publication_channels"])
        self.assertEqual(2, len(save_report.call_args.kwargs["publication_messages"]))
        for call in notifier_class.return_value.send_event.call_args_list:
            self.assertEqual(["feishu"], call.kwargs["channels"])

    @patch("app.services.ai_daily_report_delivery.dispatch_publication_message")
    @patch("app.services.ai_daily_report_delivery.load_trusted_model_qualifications", return_value={})
    @patch("app.services.ai_daily_report_delivery.save_ai_daily_report")
    @patch("app.services.ai_daily_report_delivery.build_ai_daily_report")
    @patch("app.services.ai_daily_report_delivery.PushNotificationService")
    def test_not_ready_report_sends_notice_without_candidates(
        self,
        notifier_class,
        build_report,
        save_report,
        trusted_qualification,
        dispatch,
    ) -> None:
        report = {
            "report_date": "2026-08-21",
            "market_recommendations_meta": {"status": "not_ready", "note": "候选快照缺失"},
        }
        build_report.return_value = report
        notifier_class.return_value.send_event.return_value = {
            "status": "success",
            "sent": ["feishu"],
            "failed": [],
        }
        dispatch.side_effect = lambda **kwargs: kwargs["notifier"].send_event(
            event_type=kwargs["event_type"], title=kwargs["message"]["title"],
            body=kwargs["message"]["body"], channels=kwargs["channels"],
        )

        result = deliver_cn_ai_daily_report_to_feishu()

        self.assertEqual("readiness_notice", result["delivery_kind"])
        call = notifier_class.return_value.send_event.call_args
        self.assertEqual("ai_report", call.kwargs["event_type"])
        self.assertEqual(["feishu"], call.kwargs["channels"])
        self.assertIn("候选快照缺失", call.kwargs["body"])
        self.assertIs(report, save_report.call_args.args[0])
        self.assertEqual(["feishu"], save_report.call_args.kwargs["publication_channels"])
        self.assertEqual("CN", save_report.call_args.kwargs["publication_messages"][0]["market"])


if __name__ == "__main__":
    unittest.main()
