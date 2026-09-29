import unittest
from unittest.mock import MagicMock, patch

from app.services.push_notifications import PushNotificationService


def _response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def _app_service() -> PushNotificationService:
    service = PushNotificationService()
    service.settings.feishu_webhook_url = None
    service.settings.feishu_app_id = "cli_test"
    service.settings.feishu_app_secret = "secret"
    service.settings.feishu_chat_id = "oc_test"
    service.settings.feishu_api_base_url = "https://open.feishu.cn/open-apis"
    return service


class PushNotificationServiceTests(unittest.TestCase):
    def test_feishu_app_configuration_enables_channel(self) -> None:
        service = _app_service()

        self.assertIn("feishu", service.available_channels())

    def test_feishu_app_sends_post_message_to_chat_id(self) -> None:
        service = _app_service()

        with patch(
            "app.services.push_notifications.httpx.post",
            side_effect=[
                _response({"code": 0, "tenant_access_token": "tenant-token", "expire": 7200}),
                _response({"code": 0, "msg": "success", "data": {"message_id": "om_test"}}),
            ],
        ) as mocked_post:
            result = service.send_text(title="测试标题", body="测试正文", channels=["feishu"])

        self.assertEqual("success", result["status"])
        self.assertEqual(["feishu"], result["sent"])
        self.assertEqual([], result["failed"])
        self.assertEqual("om_test", result["provider_message_id"])
        send_call = mocked_post.call_args_list[1]
        self.assertTrue(send_call.args[0].endswith("/im/v1/messages"))
        self.assertEqual({"receive_id_type": "chat_id"}, send_call.kwargs["params"])
        self.assertEqual("Bearer tenant-token", send_call.kwargs["headers"]["Authorization"])
        self.assertEqual("oc_test", send_call.kwargs["json"]["receive_id"])
        self.assertEqual("post", send_call.kwargs["json"]["msg_type"])
        self.assertIn("测试标题", send_call.kwargs["json"]["content"])

    def test_feishu_app_reports_business_error_without_leaking_secret(self) -> None:
        service = _app_service()

        with patch(
            "app.services.push_notifications.httpx.post",
            return_value=_response({"code": 10003, "msg": "invalid app"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "code=10003") as exc_info:
                service._send_feishu_app(title="测试", body="测试")

        self.assertNotIn("secret", str(exc_info.exception))

    def test_feishu_chat_list_paginates(self) -> None:
        service = _app_service()

        with patch(
            "app.services.push_notifications.httpx.post",
            return_value=_response({"code": 0, "tenant_access_token": "tenant-token"}),
        ), patch(
            "app.services.push_notifications.httpx.get",
            side_effect=[
                _response(
                    {
                        "code": 0,
                        "data": {
                            "items": [{"chat_id": "oc_one", "name": "通知一群"}],
                            "has_more": True,
                            "page_token": "next-page",
                        },
                    }
                ),
                _response(
                    {
                        "code": 0,
                        "data": {
                            "items": [{"chat_id": "oc_two", "name": "通知二群"}],
                            "has_more": False,
                        },
                    }
                ),
            ],
        ) as mocked_get:
            chats = service.list_feishu_chats()

        self.assertEqual(["oc_one", "oc_two"], [chat["chat_id"] for chat in chats])
        self.assertEqual("next-page", mocked_get.call_args_list[1].kwargs["params"]["page_token"])

    def test_feishu_history_reconciliation_is_read_only_and_bounded(self) -> None:
        service = _app_service()
        with patch(
            "app.services.push_notifications.httpx.post",
            return_value=_response({"code": 0, "tenant_access_token": "tenant-token"}),
        ), patch(
            "app.services.push_notifications.httpx.get",
            return_value=_response({"code": 0, "data": {"items": [{"message_id": "om_test"}],
                                                       "has_more": False}}),
        ) as mocked_get:
            result = service.list_feishu_chat_messages(start_time=1, end_time=2)
        self.assertEqual(["om_test"], [item["message_id"] for item in result])
        mocked_get.assert_called_once()
        self.assertEqual("chat", mocked_get.call_args.kwargs["params"]["container_id_type"])
        self.assertEqual("oc_test", mocked_get.call_args.kwargs["params"]["container_id"])


if __name__ == "__main__":
    unittest.main()
