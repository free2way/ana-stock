from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.services.auth import is_authenticated, login_redirect
from app.services.push_notifications import PushNotificationService


router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/notifications", response_class=HTMLResponse)
def notification_settings_page(request: Request) -> str:
    if not is_authenticated(request):
        return login_redirect("/settings/notifications")
    settings = get_settings()
    notifier = PushNotificationService()
    channels = notifier.available_channels()
    return f"""
    <!DOCTYPE html>
    <html lang="zh">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>通知配置</title>
        <style>
          body {{ margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f5efe2; color:#1f2937; }}
          .wrap {{ max-width: 960px; margin:0 auto; padding:28px 20px 56px; }}
          .card {{ background:#fffdf7; border:1px solid #d6cfc2; border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:#dff5ef; color:#0f766e; font-size:12px; font-weight:700; margin-bottom:12px; }}
          .muted {{ color:#6b7280; font-size:14px; }}
          a {{ color:#0f766e; text-decoration:none; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div style="margin-bottom:16px;"><a href="/dashboard?lang=zh">← 返回 dashboard</a></div>
          <section class="card">
            <div class="eyebrow">Notifications</div>
            <div class="muted">当前已配置渠道：{", ".join(channels) if channels else "无"}</div>
            <div class="muted" style="margin-top:12px;">企业微信 webhook：{"已配置" if settings.wechat_webhook_url else "未配置"}</div>
            <div class="muted" style="margin-top:6px;">飞书 webhook：{"已配置" if settings.feishu_webhook_url else "未配置"}</div>
            <div class="muted" style="margin-top:6px;">Telegram 机器人：{"已配置" if settings.telegram_bot_token and settings.telegram_chat_id else "未配置"}</div>
            <div class="muted" style="margin-top:16px;">请通过环境变量配置：</div>
            <div class="muted">PQW_WECHAT_WEBHOOK_URL</div>
            <div class="muted">PQW_FEISHU_WEBHOOK_URL</div>
            <div class="muted">PQW_TELEGRAM_BOT_TOKEN</div>
            <div class="muted">PQW_TELEGRAM_CHAT_ID</div>
          </section>
        </main>
      </body>
    </html>
    """
