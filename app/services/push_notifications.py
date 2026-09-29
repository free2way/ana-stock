from __future__ import annotations

import json

import httpx

from app.core.config import get_settings


class PushNotificationService:
    TELEGRAM_MAX_TEXT = 3800
    EVENT_LABELS = {
        "system_update": "系统更新完成",
        "model_training": "模型训练完成",
        "precompute": "核心预计算完成",
        "stock_recommendation": "选股推荐完成",
        "ai_report": "AI 日报已生成",
        "risk_alert": "持仓风险提醒",
    }

    def __init__(self) -> None:
        self.settings = get_settings()

    def _has_feishu_app_config(self) -> bool:
        return bool(
            self.settings.feishu_app_id
            and self.settings.feishu_app_secret
            and self.settings.feishu_chat_id
        )

    def available_channels(self) -> list[str]:
        channels: list[str] = []
        if self.settings.wechat_webhook_url:
            channels.append("wechat")
        if self.settings.feishu_webhook_url or self._has_feishu_app_config():
            channels.append("feishu")
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            channels.append("telegram")
        return channels

    def send_text(self, *, title: str, body: str, channels: list[str] | None = None) -> dict:
        selected = self.available_channels() if channels is None else channels
        sent: list[str] = []
        failed: list[dict] = []
        provider_message_ids: dict[str, str] = {}
        for channel in selected:
            try:
                if channel == "wechat":
                    self._send_wechat(title=title, body=body)
                elif channel == "feishu":
                    message_id = self._send_feishu(title=title, body=body)
                    if message_id:
                        provider_message_ids[channel] = message_id
                elif channel == "telegram":
                    self._send_telegram(title=title, body=body)
                else:
                    raise RuntimeError(f"Unsupported channel: {channel}")
                sent.append(channel)
            except Exception as exc:
                failed.append({"channel": channel, "message": str(exc)})
        status = "success" if sent and not failed else "partial" if sent else "failed"
        result = {
            "status": status,
            "sent": sent,
            "failed": failed,
        }
        if provider_message_ids:
            result["provider_message_ids"] = provider_message_ids
            if len(provider_message_ids) == 1:
                result["provider_message_id"] = next(iter(provider_message_ids.values()))
        return result

    def send_event(
        self,
        *,
        event_type: str,
        title: str,
        body: str,
        channels: list[str] | None = None,
    ) -> dict:
        label = self.EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())
        event_title = f"【{label}】{title}"
        event_body = f"通知类型：{label}\n\n{body}".strip()
        return self.send_text(title=event_title, body=event_body, channels=channels)

    def _send_wechat(self, *, title: str, body: str) -> None:
        webhook = self.settings.wechat_webhook_url
        if not webhook:
            raise RuntimeError("PQW_WECHAT_WEBHOOK_URL is not configured.")
        payload = {"msgtype": "markdown", "markdown": {"content": f"## {title}\n\n{body}"}}
        response = httpx.post(webhook, json=payload, timeout=15.0)
        response.raise_for_status()

    def _send_feishu(self, *, title: str, body: str) -> str | None:
        webhook = self.settings.feishu_webhook_url
        if webhook:
            self._send_feishu_webhook(webhook=webhook, title=title, body=body)
            return None
        if not self._has_feishu_app_config():
            raise RuntimeError(
                "Feishu is not configured. Set PQW_FEISHU_WEBHOOK_URL, or set "
                "PQW_FEISHU_APP_ID, PQW_FEISHU_APP_SECRET and PQW_FEISHU_CHAT_ID."
            )
        return self._send_feishu_app(title=title, body=body)

    def _send_feishu_webhook(self, *, webhook: str, title: str, body: str) -> None:
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [
                            [{"tag": "text", "text": body}],
                        ],
                    }
                }
            },
        }
        response = httpx.post(webhook, json=payload, timeout=15.0)
        response.raise_for_status()

    def _feishu_api_url(self, path: str) -> str:
        return f"{self.settings.feishu_api_base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _parse_feishu_api_response(response: httpx.Response, *, operation: str) -> dict:
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Feishu {operation} returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Feishu {operation} returned an invalid response.")
        code = payload.get("code", 0)
        if code != 0:
            message = str(payload.get("msg") or payload.get("message") or "unknown error")
            raise RuntimeError(f"Feishu {operation} failed (code={code}): {message}")
        return payload

    def _get_feishu_tenant_access_token(self) -> str:
        app_id = self.settings.feishu_app_id
        app_secret = self.settings.feishu_app_secret
        if not app_id or not app_secret:
            raise RuntimeError("PQW_FEISHU_APP_ID and PQW_FEISHU_APP_SECRET are required.")
        response = httpx.post(
            self._feishu_api_url("auth/v3/tenant_access_token/internal"),
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=15.0,
        )
        payload = self._parse_feishu_api_response(response, operation="token request")
        token = str(payload.get("tenant_access_token") or "").strip()
        if not token:
            raise RuntimeError("Feishu token request succeeded without tenant_access_token.")
        return token

    def _send_feishu_app(self, *, title: str, body: str) -> str | None:
        chat_id = self.settings.feishu_chat_id
        if not chat_id:
            raise RuntimeError("PQW_FEISHU_CHAT_ID is not configured.")
        token = self._get_feishu_tenant_access_token()
        content = {
            "zh_cn": {
                "title": title,
                "content": [[{"tag": "text", "text": body}]],
            }
        }
        response = httpx.post(
            self._feishu_api_url("im/v1/messages"),
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "post",
                "content": json.dumps(content, ensure_ascii=False),
            },
            timeout=15.0,
        )
        payload = self._parse_feishu_api_response(response, operation="message send")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return str(data.get("message_id") or "").strip() or None

    def list_feishu_chats(self) -> list[dict]:
        """List chats visible to the configured Feishu application bot."""
        token = self._get_feishu_tenant_access_token()
        chats: list[dict] = []
        page_token: str | None = None
        while True:
            params: dict[str, str | int] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            response = httpx.get(
                self._feishu_api_url("im/v1/chats"),
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            payload = self._parse_feishu_api_response(response, operation="chat list")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            items = data.get("items") if isinstance(data, dict) else []
            if isinstance(items, list):
                chats.extend(item for item in items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "").strip()
            if not page_token:
                break
        return chats

    def list_feishu_chat_messages(
        self, *, start_time: int, end_time: int, max_pages: int = 10,
    ) -> list[dict]:
        """Read a bounded chat-history window for delivery reconciliation only."""
        if not self.settings.feishu_chat_id or not self._has_feishu_app_config():
            raise RuntimeError("Feishu app chat configuration is required for history reconciliation.")
        if int(end_time) <= int(start_time) or max_pages < 1:
            raise ValueError("A bounded positive Feishu history window is required.")
        token = self._get_feishu_tenant_access_token()
        items: list[dict] = []
        page_token: str | None = None
        for _page in range(min(int(max_pages), 20)):
            params: dict[str, str | int] = {
                "container_id_type": "chat", "container_id": self.settings.feishu_chat_id,
                "start_time": str(int(start_time)), "end_time": str(int(end_time)),
                "page_size": 50,
            }
            if page_token:
                params["page_token"] = page_token
            response = httpx.get(
                self._feishu_api_url("im/v1/messages"), params=params,
                headers={"Authorization": f"Bearer {token}"}, timeout=15.0,
            )
            try:
                payload = self._parse_feishu_api_response(response, operation="chat history")
            except httpx.HTTPStatusError as exc:
                try:
                    error_payload = exc.response.json()
                except ValueError:
                    error_payload = {}
                code = error_payload.get("code") if isinstance(error_payload, dict) else None
                raise RuntimeError(
                    f"Feishu chat history HTTP {exc.response.status_code}, code={code}"
                ) from exc
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            page_items = data.get("items") if isinstance(data.get("items"), list) else []
            items.extend(item for item in page_items if isinstance(item, dict))
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "").strip()
            if not page_token:
                break
        return items

    def _send_telegram(self, *, title: str, body: str) -> None:
        token = self.settings.telegram_bot_token
        chat_id = self.settings.telegram_chat_id
        if not token:
            raise RuntimeError("PQW_TELEGRAM_BOT_TOKEN is not configured.")
        if not chat_id:
            raise RuntimeError("PQW_TELEGRAM_CHAT_ID is not configured.")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        text = f"{title}\n\n{body}".strip()
        if len(text) > self.TELEGRAM_MAX_TEXT:
            text = text[: self.TELEGRAM_MAX_TEXT - 12].rstrip() + "\n\n[已截断]"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        response = httpx.post(url, json=payload, timeout=15.0)
        response.raise_for_status()
