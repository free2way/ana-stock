from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.auto_analysis import auto_analysis_service
from app.services.auth import is_authenticated, login_redirect
from app.services.close_review_scheduler import close_review_scheduler_service
from app.services.push_notifications import PushNotificationService
from app.services.repository import DataJobRepository, SymbolRepository
from app.services.time_utils import format_app_datetime
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html


router = APIRouter(prefix="/settings", tags=["settings"])


def _provider_strategy_view(lang: str) -> dict:
    if lang == "zh":
        return {
            "title": "数据策略",
            "copy": "先看系统如何自动选 provider，再看当前自动任务实际会用什么默认策略。",
            "price": "价格 `auto`：A 股优先 TuShare，其他市场优先 yfinance。",
            "fundamental": "基本面 `auto`：A 股走 TuShare，美股/港股走 OpenBB 或 yfinance fundamentals。",
            "concept": "概念 `auto`：当前 A 股概念映射统一走 TuShare。",
            "execution": "执行与实时：后续会收敛到 `execution / realtime` 层，不混进研究数据层。",
        }
    return {
        "title": "Data Strategy",
        "copy": "Review how the app chooses providers automatically first, then what the automation layer currently defaults to.",
        "price": "Price `auto`: CN prefers TuShare, while other markets default to yfinance.",
        "fundamental": "Fundamentals `auto`: CN uses TuShare, while US/HK uses OpenBB or yfinance fundamentals.",
        "concept": "Concept `auto`: current CN concept mapping is standardized on TuShare.",
        "execution": "Execution and realtime are reserved for the future `execution / realtime` layer instead of the research data layer.",
    }


def _settings_shell(*, lang: str, title: str, lead: str, body_html: str, active_path: str) -> str:
    nav_html = render_workspace_nav_html(lang=lang, active_key="settings")
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{title}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          {WORKSPACE_COMPACT_STYLE}
          {WORKSPACE_SIDEBAR_STYLE}
          .topbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:16px; flex-wrap:wrap; }}
          .chip-row {{ display:flex; flex-wrap:wrap; gap:10px; }}
          .top-pill {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--muted); font-size:13px; font-weight:700; }}
          .hero {{ display:grid; grid-template-columns:minmax(0,1.3fr) minmax(260px,0.85fr); gap:12px; margin-bottom:12px; }}
          .workspace {{ display:grid; grid-template-columns:minmax(0,1.05fr) minmax(300px,0.95fr); gap:12px; }}
          .stack,.list-stack,.quick-grid {{ display:grid; gap:12px; }}
          .quick-grid {{ grid-template-columns:repeat(auto-fit, minmax(220px,1fr)); }}
          .quick-link {{ display:block; padding:14px; border-radius:16px; border:1px solid var(--line); background:rgba(21,34,49,0.82); }}
          .quick-link:hover {{ border-color:var(--accent); box-shadow:0 12px 28px rgba(61,217,182,0.08); }}
          form {{ margin:0; }}
          button {{
            width:auto;
            background:var(--accent);
            color:#041119;
            font-weight:800;
            cursor:pointer;
          }}
          .form-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); }}
          .form-actions {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; }}
          .checkline {{ display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }}
          .checkline input {{ width:auto; }}
          h1 {{ margin:10px 0 8px; font-size:34px; line-height:1.04; letter-spacing:-0.03em; }}
          .section-title {{ margin:0 0 4px; font-size:20px; }}
          .lead,.section-copy,.subtle,.muted {{ color:var(--muted); }}
          .lead,.section-copy {{ font-size:14px; line-height:1.5; }}
          .ticker {{ font-weight:800; font-size:15px; color:var(--ink); }}
          .list-row {{ display:flex; justify-content:space-between; align-items:flex-start; gap:12px; padding:10px 0; border-top:1px solid rgba(144,163,184,0.12); }}
          .list-row:first-child {{ border-top:none; padding-top:0; }}
          @media (max-width:1100px) {{ .hero,.workspace {{ grid-template-columns:1fr; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'设置' if lang == 'zh' else 'Settings'}</h1>
              <p>{'把通知、交付、系统入口收在同一套工作台里。' if lang == 'zh' else 'Keep notifications, delivery, and system entry points inside one unified workspace.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'建议把设置页看成运维入口，而不是孤立的表单页。' if lang == 'zh' else 'Treat settings as an operations entry point rather than an isolated form page.'}</div>
          </aside>
          <main class="main">
            <div class="topbar">
              <div class="chip-row">
                <span class="top-pill">{'工作台模式' if lang == 'zh' else 'Workspace mode'}</span>
              </div>
              <div class="chip-row">
                <a class="top-pill" href="{active_path == 'notifications' and '/settings/notifications?lang=en' or '/settings?lang=en'}">EN</a>
                <a class="top-pill" href="{active_path == 'notifications' and '/settings/notifications?lang=zh' or '/settings?lang=zh'}">中文</a>
              </div>
            </div>
            {body_html}
          </main>
        </div>
      </body>
    </html>
    """


def _display_time(value: str | None) -> str:
    return format_app_datetime(value)


def _latest_cn_refresh_summary(lang: str) -> dict:
    with SessionLocal() as db:
        recent_jobs = DataJobRepository(db).list_recent_jobs(limit=10)
        refresh_job = next(
            (
                item
                for item in recent_jobs
                if str(item.get("job_type") or "").lower() == "cn_close_review"
                and str(item.get("status") or "").lower() == "success"
            ),
            None,
        )
        cn_total = len([symbol for symbol in SymbolRepository(db).list_symbols() if (symbol.market or "").upper() == "CN"])
    import re
    message = str((refresh_job or {}).get("message") or "")
    match = re.search(r"light CN refresh\s+(\d+)\s+symbol", message, re.IGNORECASE)
    refreshed = int(match.group(1)) if match else None
    summary = f"{refreshed}/{cn_total}" if refreshed is not None and cn_total else (str(refreshed) if refreshed is not None else "-")
    label = (
        f"最近成功刷新 {refreshed}/{cn_total} 只 A 股" if refreshed is not None and lang == "zh"
        else (f"Latest successful refresh {refreshed}/{cn_total} CN symbols" if refreshed is not None else ("暂无最近全市场刷新结果" if lang == "zh" else "No recent CN refresh result"))
    )
    return {"summary": summary, "label": label}


@router.get("", response_class=HTMLResponse)
def settings_home_page(request: Request) -> str:
    if not is_authenticated(request):
        return login_redirect("/settings")
    lang = resolve_request_lang(request, default="zh")
    settings = get_settings()
    notifier = PushNotificationService()
    channels = notifier.available_channels()
    auto_status = auto_analysis_service.get_status()
    close_review_status = close_review_scheduler_service.get_status()
    strategy = _provider_strategy_view(lang)
    latest_cn_refresh = _latest_cn_refresh_summary(lang)
    configured = {
        "wechat": bool(settings.wechat_webhook_url),
        "feishu": bool(settings.feishu_webhook_url),
        "telegram": bool(settings.telegram_bot_token and settings.telegram_chat_id),
    }
    total_configured = sum(1 for value in configured.values() if value)
    auto_provider = str(auto_status.get("provider") or "auto")
    close_provider = str(close_review_status.get("provider") or "auto")
    refresh_limit = int(close_review_status.get("refresh_limit") or 0)
    auto_enabled_text = "已开启" if (lang == "zh" and auto_status.get("enabled")) else ("已关闭" if lang == "zh" else ("Enabled" if auto_status.get("enabled") else "Disabled"))
    close_enabled_text = "已开启" if (lang == "zh" and close_review_status.get("enabled")) else ("已关闭" if lang == "zh" else ("Enabled" if close_review_status.get("enabled") else "Disabled"))
    auto_provider_options = "".join(
        f"<option value='{value}'{' selected' if auto_provider == value else ''}>{label}</option>"
        for value, label in (
            ("auto", "Auto"),
            ("tushare", "TuShare"),
            ("yfinance", "yfinance"),
            ("openbb", "OpenBB"),
        )
    )
    close_provider_options = "".join(
        f"<option value='{value}'{' selected' if close_provider == value else ''}>{label}</option>"
        for value, label in (
            ("auto", "Auto"),
            ("tushare", "TuShare"),
            ("yfinance", "yfinance"),
        )
    )
    body_html = f"""
      <section class="hero">
        <article class="card">
          <span class="eyebrow">{'设置总览' if lang == 'zh' else 'Settings Overview'}</span>
          <h1>{'系统配置、交付与数据策略' if lang == 'zh' else 'System configuration, delivery, and data strategy'}</h1>
          <p class="lead">{'从实际使用上，这里应该先回答三件事：自动任务有没有开、结果会不会送达，以及系统默认会从哪里取数。' if lang == 'zh' else 'In practice, this page should answer three things first: are automation jobs enabled, will outputs be delivered, and where will the system fetch data by default?'}</p>
        </article>
        <article class="card">
          <span class="eyebrow">{'当前状态' if lang == 'zh' else 'Current State'}</span>
          <div class="list-stack">
            <div><div class="subtle">{'已启用渠道' if lang == 'zh' else 'Enabled channels'}</div><div class="ticker">{", ".join(channels) if channels else ('无' if lang == 'zh' else 'None')}</div></div>
            <div><div class="subtle">{'已配置数量' if lang == 'zh' else 'Configured count'}</div><div class="ticker">{total_configured}</div></div>
            <div><div class="subtle">{'自动分析' if lang == 'zh' else 'Auto analysis'}</div><div class="ticker">{auto_enabled_text} · {auto_provider}</div></div>
            <div><div class="subtle">{'收盘复盘' if lang == 'zh' else 'Close review'}</div><div class="ticker">{close_enabled_text} · {close_provider}</div></div>
          </div>
        </article>
      </section>
      <section class="workspace">
        <div class="stack">
          <article class="card">
            <span class="eyebrow">{'快速入口' if lang == 'zh' else 'Quick Actions'}</span>
            <div class="quick-grid">
              <a class="quick-link" href="/settings/notifications?lang={lang}">
                <div class="ticker">{'通知配置' if lang == 'zh' else 'Notifications'}</div>
                <div class="section-copy">{'查看 Telegram、企业微信、飞书是否已就绪。' if lang == 'zh' else 'Review Telegram, WeCom, and Feishu readiness.'}</div>
              </a>
              <a class="quick-link" href="/dashboard/ops?lang={lang}">
                <div class="ticker">{'任务中心' if lang == 'zh' else 'Jobs Center'}</div>
                <div class="section-copy">{'回看自动任务、训练、回测和异常提示。' if lang == 'zh' else 'Review automation, training, backtests, and alerts.'}</div>
              </a>
              <a class="quick-link" href="/dashboard/data-sources?lang={lang}">
                <div class="ticker">{'数据状态' if lang == 'zh' else 'Data Status'}</div>
                <div class="section-copy">{'确认行情和概念数据的新鲜度与来源。' if lang == 'zh' else 'Confirm market/concept data freshness and providers.'}</div>
              </a>
              <a class="quick-link" href="/dashboard/ops?lang={lang}">
                <div class="ticker">{'系统任务策略' if lang == 'zh' else 'Automation Policy'}</div>
                <div class="section-copy">{'查看自动分析、收盘复盘和 provider 默认策略。' if lang == 'zh' else 'Review auto-analysis, close review, and provider defaults.'}</div>
              </a>
            </div>
          </article>
          <article class="card">
            <span class="eyebrow">{strategy['title']}</span>
            <h2 class="section-title">{'系统默认如何选数据源' if lang == 'zh' else 'How the system chooses data providers by default'}</h2>
            <div class="list-stack">
              <div class="list-row"><div><div class="ticker">Price / Auto</div><div class="subtle">{strategy['price']}</div></div></div>
              <div class="list-row"><div><div class="ticker">Fundamental / Auto</div><div class="subtle">{strategy['fundamental']}</div></div></div>
              <div class="list-row"><div><div class="ticker">Concept / Auto</div><div class="subtle">{strategy['concept']}</div></div></div>
              <div class="list-row"><div><div class="ticker">Execution / Realtime</div><div class="subtle">{strategy['execution']}</div></div></div>
            </div>
          </article>
        </div>
        <div class="stack">
          <article class="card">
            <span class="eyebrow">{'配置摘要' if lang == 'zh' else 'Configuration Summary'}</span>
            <div class="list-stack">
              <div class="list-row"><div><div class="ticker">Telegram</div><div class="subtle">PQW_TELEGRAM_BOT_TOKEN / PQW_TELEGRAM_CHAT_ID</div></div><div class="ticker">{'已配置' if configured['telegram'] and lang == 'zh' else ('未配置' if lang == 'zh' else ('Configured' if configured['telegram'] else 'Missing'))}</div></div>
              <div class="list-row"><div><div class="ticker">{'企业微信' if lang == 'zh' else 'WeCom'}</div><div class="subtle">PQW_WECHAT_WEBHOOK_URL</div></div><div class="ticker">{'已配置' if configured['wechat'] and lang == 'zh' else ('未配置' if lang == 'zh' else ('Configured' if configured['wechat'] else 'Missing'))}</div></div>
              <div class="list-row"><div><div class="ticker">{'飞书' if lang == 'zh' else 'Feishu'}</div><div class="subtle">PQW_FEISHU_WEBHOOK_URL</div></div><div class="ticker">{'已配置' if configured['feishu'] and lang == 'zh' else ('未配置' if lang == 'zh' else ('Configured' if configured['feishu'] else 'Missing'))}</div></div>
            </div>
          </article>
          <article class="card">
            <span class="eyebrow">{'自动任务配置' if lang == 'zh' else 'Automation Configuration'}</span>
            <div class="list-stack">
              <div class="list-row"><div><div class="ticker">{'自动分析' if lang == 'zh' else 'Auto analysis'}</div><div class="subtle">{'默认 provider / 下次运行' if lang == 'zh' else 'Default provider / next run'}</div></div><div class="ticker">{auto_provider} · {_display_time(auto_status.get('next_run_at'))}</div></div>
              <div class="list-row"><div><div class="ticker">{'收盘复盘' if lang == 'zh' else 'Close review'}</div><div class="subtle">{'默认 provider / 计划时间 / 全市场轻刷新范围' if lang == 'zh' else 'Default provider / schedule / market light refresh scope'}</div></div><div class="ticker">{close_provider} · {close_review_status.get('run_hour', 0):02d}:{close_review_status.get('run_minute', 0):02d} · {('全市场' if refresh_limit == 0 else f'前 {refresh_limit} 只') if lang == 'zh' else ('All CN' if refresh_limit == 0 else f'Top {refresh_limit}')}</div></div>
              <div class="list-row"><div><div class="ticker">{'最近全市场轻刷新结果' if lang == 'zh' else 'Latest light refresh result'}</div><div class="subtle">{latest_cn_refresh['label']}</div></div><div class="ticker">{latest_cn_refresh['summary']}</div></div>
              <div class="list-row"><div><div class="ticker">{'当前建议' if lang == 'zh' else 'Current guidance'}</div><div class="subtle">{'如果主要覆盖 A 股，优先用 TuShare 或 auto。' if lang == 'zh' else 'If CN coverage matters most, prefer TuShare or auto.'}</div></div></div>
            </div>
          </article>
          <article class="card">
            <span class="eyebrow">{'快速配置' if lang == 'zh' else 'Quick Configuration'}</span>
            <h2 class="section-title">{'直接修改自动任务开关与默认源' if lang == 'zh' else 'Adjust automation toggles and default providers directly'}</h2>
            <div class="stack">
              <form action="/jobs/auto-analysis/config" method="post">
                <input type="hidden" name="redirect_to" value="/settings?lang={lang}" />
                <div class="ticker" style="margin-bottom:10px;">{'自动分析' if lang == 'zh' else 'Auto analysis'}</div>
                <div class="form-grid">
                  <label>
                    <div class="subtle">{'默认 provider' if lang == 'zh' else 'Default provider'}</div>
                    <select name="provider">{auto_provider_options}</select>
                  </label>
                  <label>
                    <div class="subtle">{'间隔小时' if lang == 'zh' else 'Interval hours'}</div>
                    <input type="text" name="interval_hours" value="{auto_status.get('interval_hours') or 24}" />
                  </label>
                  <label>
                    <div class="subtle">{'观察窗口' if lang == 'zh' else 'Lookback days'}</div>
                    <input type="text" name="lookback_days" value="{auto_status.get('lookback_days') or 3}" />
                  </label>
                </div>
                <div class="form-actions">
                  <label class="checkline"><input type="checkbox" name="enabled" value="1" {'checked' if auto_status.get('enabled') else ''} /> {'启用自动分析' if lang == 'zh' else 'Enable auto analysis'}</label>
                  <button type="submit">{'保存自动分析配置' if lang == 'zh' else 'Save auto analysis'}</button>
                </div>
              </form>
              <form action="/jobs/close-review/config" method="post">
                <input type="hidden" name="redirect_to" value="/settings?lang={lang}" />
                <div class="ticker" style="margin-bottom:10px;">{'收盘复盘' if lang == 'zh' else 'Close review'}</div>
                <div class="form-grid">
                  <label>
                    <div class="subtle">{'默认 provider' if lang == 'zh' else 'Default provider'}</div>
                    <select name="provider">{close_provider_options}</select>
                  </label>
                  <label>
                    <div class="subtle">{'运行小时' if lang == 'zh' else 'Run hour'}</div>
                    <input type="text" name="run_hour" value="{close_review_status.get('run_hour', 18)}" />
                  </label>
                  <label>
                    <div class="subtle">{'运行分钟' if lang == 'zh' else 'Run minute'}</div>
                    <input type="text" name="run_minute" value="{close_review_status.get('run_minute', 0)}" />
                  </label>
                  <label>
                    <div class="subtle">{'全市场轻刷新范围' if lang == 'zh' else 'Market light refresh scope'}</div>
                    <input type="text" name="refresh_limit" value="{refresh_limit}" />
                    <div class="subtle" style="margin-top:6px;">{'Parquet 模式下建议 0，全市场刷新会写入 lake，不再生成 CSV。' if lang == 'zh' else 'In Parquet mode, 0 is recommended: refresh the full market into the lake without generating CSV.'}</div>
                  </label>
                </div>
                <div class="form-actions">
                  <label class="checkline"><input type="checkbox" name="enabled" value="1" {'checked' if close_review_status.get('enabled') else ''} /> {'启用收盘复盘' if lang == 'zh' else 'Enable close review'}</label>
                  <button type="submit">{'保存收盘复盘配置' if lang == 'zh' else 'Save close review'}</button>
                </div>
              </form>
            </div>
          </article>
        </div>
      </section>
    """
    return _settings_shell(
        lang=lang,
        title="设置" if lang == "zh" else "Settings",
        lead="",
        body_html=body_html,
        active_path="settings",
    )


@router.get("/notifications", response_class=HTMLResponse)
def notification_settings_page(request: Request) -> str:
    if not is_authenticated(request):
        return login_redirect("/settings/notifications")
    lang = resolve_request_lang(request, default="zh")
    settings = get_settings()
    notifier = PushNotificationService()
    channels = notifier.available_channels()
    configured = {
        "wechat": bool(settings.wechat_webhook_url),
        "feishu": bool(settings.feishu_webhook_url),
        "telegram": bool(settings.telegram_bot_token and settings.telegram_chat_id),
    }
    total_configured = sum(1 for value in configured.values() if value)
    channel_rows = [
        {
            "label": "企业微信" if lang == "zh" else "WeCom",
            "status": "已配置" if configured["wechat"] and lang == "zh" else ("未配置" if lang == "zh" else ("Configured" if configured["wechat"] else "Not configured")),
            "hint": "PQW_WECHAT_WEBHOOK_URL",
        },
        {
            "label": "飞书" if lang == "zh" else "Feishu",
            "status": "已配置" if configured["feishu"] and lang == "zh" else ("未配置" if lang == "zh" else ("Configured" if configured["feishu"] else "Not configured")),
            "hint": "PQW_FEISHU_WEBHOOK_URL",
        },
        {
            "label": "Telegram",
            "status": "已配置" if configured["telegram"] and lang == "zh" else ("未配置" if lang == "zh" else ("Configured" if configured["telegram"] else "Not configured")),
            "hint": "PQW_TELEGRAM_BOT_TOKEN / PQW_TELEGRAM_CHAT_ID",
        },
    ]
    channel_rows_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{item['label']}</div><div class='subtle'>{item['hint']}</div></div>"
        f"<div class='row-right'><span class='status-pill {'success' if ('已配置' in item['status'] or 'Configured' in item['status']) else 'idle'}'>{item['status']}</span></div>"
        "</article>"
        for item in channel_rows
    )
    body_html = f"""
      <section class="hero">
        <article class="card">
          <span class="eyebrow">{'通知配置' if lang == 'zh' else 'Notifications'}</span>
          <h1>{'推送配置与交付状态' if lang == 'zh' else 'Delivery setup and status'}</h1>
          <p class="lead">{'从交易员视角，这页最重要的是两件事：自动分析结果能不能稳定送达，以及现在到底有哪些通道可用。' if lang == 'zh' else 'From a trader workflow perspective, this page answers two questions first: will automated outputs be delivered reliably, and which channels are actually usable right now?'}</p>
        </article>
        <article class="card">
          <span class="eyebrow">{'当前结论' if lang == 'zh' else 'Current Summary'}</span>
          <div class="list-stack">
            <div><div class="subtle">{'可用通道' if lang == 'zh' else 'Available channels'}</div><div class="ticker">{", ".join(channels) if channels else ('无' if lang == 'zh' else 'None')}</div></div>
            <div><div class="subtle">{'已配置数量' if lang == 'zh' else 'Configured count'}</div><div class="ticker">{total_configured}</div></div>
            <div><div class="subtle">{'建议' if lang == 'zh' else 'Recommendation'}</div><div class="ticker">{'至少保留 1 个稳定通道' if lang == 'zh' else 'Keep at least one reliable channel'}</div></div>
          </div>
        </article>
      </section>
      <section class="workspace">
        <div class="stack">
          <article class="card">
            <span class="eyebrow">{'渠道状态' if lang == 'zh' else 'Channel Status'}</span>
            <h2 class="section-title">{'现在有哪些通道真的可用' if lang == 'zh' else 'Which channels are actually ready'}</h2>
            <div class="list-stack">{channel_rows_html}</div>
          </article>
        </div>
        <div class="stack">
          <article class="card">
            <span class="eyebrow">{'环境变量' if lang == 'zh' else 'Environment Variables'}</span>
            <h2 class="section-title">{'当前通过环境变量配置' if lang == 'zh' else 'Configured via environment variables'}</h2>
            <div class="list-stack">
              <div class="ticker">PQW_WECHAT_WEBHOOK_URL</div>
              <div class="ticker">PQW_FEISHU_WEBHOOK_URL</div>
              <div class="ticker">PQW_TELEGRAM_BOT_TOKEN</div>
              <div class="ticker">PQW_TELEGRAM_CHAT_ID</div>
            </div>
          </article>
          <article class="card">
            <span class="eyebrow">{'建议' if lang == 'zh' else 'Suggestion'}</span>
            <p class="section-copy">{'如果你依赖自动分析和 AI 日报，建议至少配置 Telegram 或企业微信其中一个，确保结果不会只停留在任务中心。' if lang == 'zh' else 'If you rely on automated analysis and AI reports, configure at least Telegram or WeCom so results do not stay trapped in the jobs page.'}</p>
          </article>
        </div>
      </section>
    """
    return _settings_shell(
        lang=lang,
        title="通知配置" if lang == "zh" else "Notifications",
        lead="",
        body_html=body_html,
        active_path="notifications",
    )
