from __future__ import annotations

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

    def available_channels(self) -> list[str]:
        channels: list[str] = []
        if self.settings.wechat_webhook_url:
            channels.append("wechat")
        if self.settings.feishu_webhook_url:
            channels.append("feishu")
        if self.settings.telegram_bot_token and self.settings.telegram_chat_id:
            channels.append("telegram")
        return channels

    def send_text(self, *, title: str, body: str, channels: list[str] | None = None) -> dict:
        selected = self.available_channels() if channels is None else channels
        sent: list[str] = []
        failed: list[dict] = []
        for channel in selected:
            try:
                if channel == "wechat":
                    self._send_wechat(title=title, body=body)
                elif channel == "feishu":
                    self._send_feishu(title=title, body=body)
                elif channel == "telegram":
                    self._send_telegram(title=title, body=body)
                else:
                    raise RuntimeError(f"Unsupported channel: {channel}")
                sent.append(channel)
            except Exception as exc:
                failed.append({"channel": channel, "message": str(exc)})
        status = "success" if sent and not failed else "partial" if sent else "failed"
        return {
            "status": status,
            "sent": sent,
            "failed": failed,
        }

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

    def _send_feishu(self, *, title: str, body: str) -> None:
        webhook = self.settings.feishu_webhook_url
        if not webhook:
            raise RuntimeError("PQW_FEISHU_WEBHOOK_URL is not configured.")
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
