from __future__ import annotations

import html
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.ai_chat import AI_CHAT_PROVIDER_PRESETS, load_ai_chat_config, masked_api_key, save_ai_chat_config
from app.services.auto_analysis import auto_analysis_service
from app.services.auth import is_authenticated, login_redirect
from app.services.close_review_scheduler import close_review_scheduler_service
from app.services.kronos_validation import load_latest_kronos_validation
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
    active_links = {
        "notifications": "/settings/notifications",
        "ai_chat": "/settings/ai-chat",
        "kronos": "/settings/kronos",
    }
    active_base = active_links.get(active_path, "/settings")
    en_href = f"{active_base}?lang=en"
    zh_href = f"{active_base}?lang=zh"
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
          .status-pill {{ display:inline-flex; align-items:center; justify-content:center; padding:5px 9px; border-radius:999px; border:1px solid rgba(144,163,184,0.2); color:var(--muted); font-size:12px; font-weight:800; white-space:nowrap; }}
          .status-pill.success {{ color:#9ff3d5; border-color:rgba(61,217,182,0.32); background:rgba(61,217,182,0.08); }}
          .status-pill.warning {{ color:#ffd08a; border-color:rgba(255,190,92,0.32); background:rgba(255,190,92,0.08); }}
          .status-pill.idle {{ color:#9fb0c2; background:rgba(144,163,184,0.08); }}
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
          code,.code-box {{ font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; }}
          .code-box {{ overflow:auto; white-space:pre-wrap; word-break:break-word; padding:12px; border-radius:14px; border:1px solid rgba(144,163,184,0.18); background:rgba(3,9,15,0.42); color:#cde7f7; font-size:12px; line-height:1.55; }}
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
                <a class="top-pill" href="{en_href}">EN</a>
                <a class="top-pill" href="{zh_href}">中文</a>
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


def _kronos_config_summary(lang: str) -> dict:
    settings = get_settings()
    enabled = bool(settings.kronos_enabled)
    runner_configured = bool(settings.kronos_runner_command)
    repo_configured = bool(settings.kronos_repo_path)
    if enabled and runner_configured:
        status = "ready"
        label = "已配置" if lang == "zh" else "Configured"
        hint = "Kronos 二次验证会在预计算尾部运行。" if lang == "zh" else "Kronos validation runs at the tail of the precompute pipeline."
    elif enabled:
        status = "not_configured"
        label = "待配置" if lang == "zh" else "Needs setup"
        hint = "已启用接入层，但还没有配置独立 runner。" if lang == "zh" else "Integration is enabled, but the external runner is not configured yet."
    else:
        status = "disabled"
        label = "已关闭" if lang == "zh" else "Disabled"
        hint = "当前不会执行 Kronos 二次验证。" if lang == "zh" else "Kronos validation will not run."
    return {
        "enabled": enabled,
        "runner_configured": runner_configured,
        "repo_configured": repo_configured,
        "status": status,
        "label": label,
        "hint": hint,
        "model": settings.kronos_model_name,
        "device": settings.kronos_device,
        "candidate_limit": settings.kronos_candidate_limit,
        "history_limit": settings.kronos_history_limit,
        "min_history": settings.kronos_min_history,
        "horizon_days": settings.kronos_prediction_horizon_days,
        "timeout_seconds": settings.kronos_timeout_seconds,
        "runner_command": settings.kronos_runner_command or "",
        "repo_path": settings.kronos_repo_path or "",
    }


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
    kronos = _kronos_config_summary(lang)
    with SessionLocal() as db:
        ai_chat_config = load_ai_chat_config(db)
    configured = {
        "wechat": bool(settings.wechat_webhook_url),
        "feishu": bool(settings.feishu_webhook_url),
        "telegram": bool(settings.telegram_bot_token and settings.telegram_chat_id),
        "ai_chat": ai_chat_config.is_configured,
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
            <div><div class="subtle">{'AI 问答' if lang == 'zh' else 'AI Q&A'}</div><div class="ticker">{('已配置' if lang == 'zh' else 'Configured') if ai_chat_config.is_configured else ('未配置' if lang == 'zh' else 'Missing')} · {ai_chat_config.provider_name}</div></div>
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
              <a class="quick-link" href="/settings/ai-chat?lang={lang}">
                <div class="ticker">{'AI 问答配置' if lang == 'zh' else 'AI Q&A Config'}</div>
                <div class="section-copy">{'设置 Provider、Base URL、模型和 API Key。' if lang == 'zh' else 'Set provider, base URL, model, and API key.'}</div>
              </a>
              <a class="quick-link" href="/settings/kronos?lang={lang}">
                <div class="ticker">{'Kronos 二次验证' if lang == 'zh' else 'Kronos Validation'}</div>
                <div class="section-copy">{'查看 K 线基础模型 runner 是否已接入。' if lang == 'zh' else 'Check whether the K-line foundation-model runner is connected.'}</div>
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
              <div class="list-row"><div><div class="ticker">{'AI 问答' if lang == 'zh' else 'AI Q&A'}</div><div class="subtle">{html.escape(ai_chat_config.provider_name)} · {html.escape(ai_chat_config.model or '-')} · {html.escape(masked_api_key(ai_chat_config) or '-')}</div></div><div class="ticker">{'已配置' if configured['ai_chat'] and lang == 'zh' else ('未配置' if lang == 'zh' else ('Configured' if configured['ai_chat'] else 'Missing'))}</div></div>
              <div class="list-row"><div><div class="ticker">{'Kronos 二次验证' if lang == 'zh' else 'Kronos Validation'}</div><div class="subtle">{html.escape(kronos['model'])} · {html.escape(kronos['device'])} · {html.escape(kronos['hint'])}</div></div><div><span class="status-pill {'success' if kronos['status'] == 'ready' else ('warning' if kronos['status'] == 'not_configured' else 'idle')}">{html.escape(kronos['label'])}</span></div></div>
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


@router.get("/kronos", response_class=HTMLResponse)
def kronos_settings_page(request: Request) -> str:
    if not is_authenticated(request):
        return login_redirect("/settings/kronos")
    lang = resolve_request_lang(request, default="zh")
    kronos = _kronos_config_summary(lang)
    status_class = "success" if kronos["status"] == "ready" else ("warning" if kronos["status"] == "not_configured" else "idle")
    with SessionLocal() as db:
        snapshot = load_latest_kronos_validation(db) or {}
        recent_job = next(
            (
                item
                for item in DataJobRepository(db).list_recent_jobs(limit=80)
                if str(item.get("job_type") or "").lower() == "kronos_validation"
            ),
            None,
        )
    payload = snapshot.get("payload") if isinstance(snapshot, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    candidate_count = int(payload.get("candidate_count") or 0)
    validated_count = int(payload.get("validated_count") or 0)
    pending_count = int(payload.get("not_configured_count") or 0)
    skipped_count = int(payload.get("skipped_count") or 0)
    snapshot_status = str(payload.get("status") or "-")
    snapshot_time = _display_time(str(snapshot.get("created_at") or "")) if snapshot else "-"
    job_status = str((recent_job or {}).get("status") or "-")
    job_time = _display_time((recent_job or {}).get("finished_at") or (recent_job or {}).get("started_at"))
    env_example = "\n".join(
        [
            f"PQW_KRONOS_ENABLED={'true' if kronos['enabled'] else 'false'}",
            'PQW_KRONOS_REPO_PATH="/Volumes/STORAGE_Jackyhu/code/Kronos"',
            'PQW_KRONOS_RUNNER_COMMAND="/Volumes/STORAGE_Jackyhu/code/ana/.venv-kronos/bin/python /Volumes/STORAGE_Jackyhu/code/ana/scripts/kronos_runner.py"',
            f"PQW_KRONOS_MODEL_NAME={kronos['model']}",
            f"PQW_KRONOS_DEVICE={kronos['device']}",
            f"PQW_KRONOS_CANDIDATE_LIMIT={kronos['candidate_limit']}",
        ]
    )
    setup_commands = "\n".join(
        [
            "python3.11 -m venv .venv-kronos",
            ".venv-kronos/bin/pip install torch transformers huggingface_hub pandas accelerate sentencepiece",
            "git clone https://github.com/shiyu-coder/Kronos /Volumes/STORAGE_Jackyhu/code/Kronos",
        ]
    )
    body_html = f"""
      <section class="hero">
        <article class="card">
          <span class="eyebrow">{'Kronos 二次验证' if lang == 'zh' else 'Kronos Validation'}</span>
          <h1>{'把 Top 候选再交给 K 线基础模型复核' if lang == 'zh' else 'Validate top candidates with a K-line foundation model'}</h1>
          <p class="lead">{'Kronos 不替代 LightGBM 和多模型共振，而是放在预计算尾部做“路径验证”：趋势候选是否仍有 1-3 日延续空间、是否存在明显回撤风险。' if lang == 'zh' else 'Kronos does not replace LightGBM or multi-model confluence. It runs at the tail of precompute as a path validator for 1-3 day continuation and drawdown risk.'}</p>
          <div class="chip-row" style="margin-top:12px;">
            <span class="status-pill {status_class}">{html.escape(kronos['label'])}</span>
            <span class="top-pill">{'模型' if lang == 'zh' else 'Model'} · {html.escape(kronos['model'])}</span>
            <span class="top-pill">{'设备' if lang == 'zh' else 'Device'} · {html.escape(kronos['device'])}</span>
          </div>
        </article>
        <article class="card">
          <span class="eyebrow">{'最新快照' if lang == 'zh' else 'Latest Snapshot'}</span>
          <div class="list-stack">
            <div><div class="subtle">{'快照状态' if lang == 'zh' else 'Snapshot status'}</div><div class="ticker">{html.escape(snapshot_status)}</div></div>
            <div><div class="subtle">{'候选 / 已验证' if lang == 'zh' else 'Candidates / validated'}</div><div class="ticker">{candidate_count} / {validated_count}</div></div>
            <div><div class="subtle">{'待配置 / 跳过' if lang == 'zh' else 'Pending setup / skipped'}</div><div class="ticker">{pending_count} / {skipped_count}</div></div>
            <div><div class="subtle">{'生成时间' if lang == 'zh' else 'Generated at'}</div><div class="ticker">{html.escape(snapshot_time)}</div></div>
          </div>
        </article>
      </section>
      <section class="workspace">
        <div class="stack">
          <article class="card">
            <span class="eyebrow">{'当前配置' if lang == 'zh' else 'Current Configuration'}</span>
            <div class="list-stack">
              <div class="list-row"><div><div class="ticker">PQW_KRONOS_ENABLED</div><div class="subtle">{'是否启用 Kronos 接入层' if lang == 'zh' else 'Whether the Kronos integration is enabled'}</div></div><div class="ticker">{'true' if kronos['enabled'] else 'false'}</div></div>
              <div class="list-row"><div><div class="ticker">PQW_KRONOS_RUNNER_COMMAND</div><div class="subtle">{'独立 Python runner，建议单独虚拟环境，避免污染主应用依赖。' if lang == 'zh' else 'External Python runner; a separate virtualenv is recommended.'}</div></div><div><span class="status-pill {'success' if kronos['runner_configured'] else 'warning'}">{'已配置' if kronos['runner_configured'] and lang == 'zh' else ('待配置' if lang == 'zh' else ('Configured' if kronos['runner_configured'] else 'Missing'))}</span></div></div>
              <div class="list-row"><div><div class="ticker">PQW_KRONOS_REPO_PATH</div><div class="subtle">{'Kronos 源码目录，runner 会把它加入 sys.path。' if lang == 'zh' else 'Kronos source path; the runner adds it to sys.path.'}</div></div><div><span class="status-pill {'success' if kronos['repo_configured'] else 'warning'}">{'已配置' if kronos['repo_configured'] and lang == 'zh' else ('待配置' if lang == 'zh' else ('Configured' if kronos['repo_configured'] else 'Missing'))}</span></div></div>
              <div class="list-row"><div><div class="ticker">{'候选上限 / 历史窗口' if lang == 'zh' else 'Candidate cap / history window'}</div><div class="subtle">{'用于控制二次验证耗时，页面筛选不依赖实时运行。' if lang == 'zh' else 'Controls validation cost; page filtering does not run Kronos live.'}</div></div><div class="ticker">{kronos['candidate_limit']} / {kronos['history_limit']}</div></div>
              <div class="list-row"><div><div class="ticker">{'最近任务' if lang == 'zh' else 'Latest job'}</div><div class="subtle">{html.escape(str((recent_job or {}).get('message') or '-'))}</div></div><div class="ticker">{html.escape(job_status)} · {html.escape(job_time)}</div></div>
            </div>
          </article>
          <article class="card">
            <span class="eyebrow">{'手动验证' if lang == 'zh' else 'Manual Validation'}</span>
            <p class="section-copy">{'这个按钮会重新构建候选池并刷新 Kronos 快照。未配置 runner 时也会生成“待验证池”，不会影响行情刷新、模型训练和 AI 日报主流程。' if lang == 'zh' else 'This rebuilds the candidate pool and refreshes the Kronos snapshot. Without a runner it still creates a pending validation pool and does not block the main pipeline.'}</p>
            <form action="/jobs/kronos-validation" method="post" class="form-actions">
              <input type="hidden" name="redirect_to" value="/settings/kronos?lang={lang}" />
              <button type="submit">{'刷新 Kronos 验证快照' if lang == 'zh' else 'Refresh Kronos snapshot'}</button>
              <a class="top-pill" href="/dashboard/ops?lang={lang}">{'打开任务中心' if lang == 'zh' else 'Open Jobs Center'}</a>
            </form>
          </article>
        </div>
        <div class="stack">
          <article class="card">
            <span class="eyebrow">{'推荐环境变量' if lang == 'zh' else 'Recommended environment variables'}</span>
            <div class="code-box">{html.escape(env_example)}</div>
          </article>
          <article class="card">
            <span class="eyebrow">{'独立运行环境' if lang == 'zh' else 'Separate runtime'}</span>
            <p class="section-copy">{'当前主应用 Python 是较新的版本，PyTorch/Kronos 建议放进独立 .venv-kronos。这样即便模型依赖安装失败，也不会拖垮主应用。' if lang == 'zh' else 'The main app uses a newer Python runtime, so PyTorch/Kronos should live in .venv-kronos. That keeps model dependency failures from breaking the app.'}</p>
            <div class="code-box">{html.escape(setup_commands)}</div>
          </article>
          <article class="card">
            <span class="eyebrow">{'验收标准' if lang == 'zh' else 'Acceptance Criteria'}</span>
            <div class="list-stack">
              <div class="list-row"><div><div class="ticker">{'未配置时' if lang == 'zh' else 'When not configured'}</div><div class="subtle">{'任务显示待配置，不阻塞预计算和 AI 日报。' if lang == 'zh' else 'Job is shown as needs setup and does not block precompute or AI reports.'}</div></div></div>
              <div class="list-row"><div><div class="ticker">{'配置后' if lang == 'zh' else 'When configured'}</div><div class="subtle">{'候选行出现 Kronos 分数、1/3 日预期、最大回撤和验证结论。' if lang == 'zh' else 'Candidate rows include Kronos score, 1/3-day expectation, max drawdown, and decision.'}</div></div></div>
              <div class="list-row"><div><div class="ticker">{'使用方式' if lang == 'zh' else 'Usage'}</div><div class="subtle">{'只作为二次确认，不把单一模型输出当成买入指令。' if lang == 'zh' else 'Use as secondary confirmation, never as a standalone buy instruction.'}</div></div></div>
            </div>
          </article>
        </div>
      </section>
    """
    return _settings_shell(
        lang=lang,
        title="Kronos 二次验证" if lang == "zh" else "Kronos Validation",
        lead="",
        body_html=body_html,
        active_path="kronos",
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
    notification_types = [
        {
            "type": "系统更新完成" if lang == "zh" else "System update done",
            "trigger": "A股收盘刷新完成" if lang == "zh" else "A-share close refresh finished",
            "summary": "刷新条数、技术快照数量、自选深度分析数量" if lang == "zh" else "Refresh rows, technical snapshots, watchlist analysis count",
        },
        {
            "type": "模型训练完成" if lang == "zh" else "Model training done",
            "trigger": "A股 LightGBM 训练完成" if lang == "zh" else "A-share LightGBM training finished",
            "summary": "训练标的数量、写入预测数量" if lang == "zh" else "Trained symbol count and predictions written",
        },
        {
            "type": "核心预计算完成" if lang == "zh" else "Core precompute done",
            "trigger": "核心模型快照完成" if lang == "zh" else "Core screener snapshots finished",
            "summary": "核心快照数量、失败模板数量、尾部任务状态" if lang == "zh" else "Core snapshot count, failed templates, tail-job status",
        },
        {
            "type": "选股推荐完成" if lang == "zh" else "Stock picks ready",
            "trigger": "AI 日报生成/发送" if lang == "zh" else "AI report generated/sent",
            "summary": "持仓总结、A股可执行买入池、观察池、美股 Top 5" if lang == "zh" else "Portfolio review, A-share buy pool, watch pool, U.S. Top 5",
        },
        {
            "type": "持仓风险提醒" if lang == "zh" else "Portfolio risk alert",
            "trigger": "持仓风险异常" if lang == "zh" else "Portfolio risk anomaly",
            "summary": "预留类型：后续用于止损、仓位漂移、事件风险提醒" if lang == "zh" else "Reserved for stops, drift, and event-risk alerts",
        },
    ]
    notification_type_rows_html = "".join(
        "<article class='quick-link'>"
        f"<div class='ticker'>{item['type']}</div>"
        f"<div class='section-copy'>{'触发' if lang == 'zh' else 'Trigger'}：{item['trigger']}</div>"
        f"<div class='subtle'>{'摘要' if lang == 'zh' else 'Summary'}：{item['summary']}</div>"
        "</article>"
        for item in notification_types
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
          <article class="card">
            <span class="eyebrow">{'通知类型' if lang == 'zh' else 'Notification Types'}</span>
            <h2 class="section-title">{'Telegram 里会看到清晰的事件前缀' if lang == 'zh' else 'Telegram messages now use clear event prefixes'}</h2>
            <p class="section-copy">{'例如：【系统更新完成】表示行情/任务状态，【选股推荐完成】表示 AI 日报和候选池已经生成，避免把系统日志和交易候选混在一起。' if lang == 'zh' else 'For example: [System update done] is about data/job readiness, while [Stock picks ready] is about AI reports and candidate pools.'}</p>
            <div class="quick-grid" style="margin-top:12px;">{notification_type_rows_html}</div>
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


@router.get("/ai-chat", response_class=HTMLResponse)
def ai_chat_settings_page(request: Request) -> str:
    if not is_authenticated(request):
        return login_redirect("/settings/ai-chat")
    lang = resolve_request_lang(request, default="zh")
    with SessionLocal() as db:
        config = load_ai_chat_config(db)
    provider_options = "".join(
        f"<option value='{html.escape(key, quote=True)}'{' selected' if config.provider == key else ''}>{html.escape(item['label'])}</option>"
        for key, item in AI_CHAT_PROVIDER_PRESETS.items()
    )
    preset_cards = "".join(
        "<article class='quick-link'>"
        f"<div class='ticker'>{html.escape(item['label'])}</div>"
        f"<div class='section-copy'>{html.escape(item['base_url'])}</div>"
        f"<div class='subtle'>{'默认模型' if lang == 'zh' else 'Default model'}：{html.escape(item['model'])}</div>"
        f"{('<div class=\"subtle\">' + ('可用模型' if lang == 'zh' else 'Model hint') + '：' + html.escape(str(item.get('model_hint'))) + '</div>') if item.get('model_hint') else ''}"
        "</article>"
        for item in AI_CHAT_PROVIDER_PRESETS.values()
    )
    body_html = f"""
      <section class="hero">
        <article class="card">
          <span class="eyebrow">{'AI Provider' if lang == 'zh' else 'AI Provider'}</span>
          <h1>{'配置 AI 问答模型' if lang == 'zh' else 'Configure AI Q&A model'}</h1>
          <p class="lead">{'AI 问答使用 OpenAI-compatible Chat Completions 接口。只要服务商兼容 /chat/completions，就可以在这里配置。API Key 会保存到应用数据库里，请只在你自己的受控环境中使用。' if lang == 'zh' else 'AI Q&A uses an OpenAI-compatible Chat Completions endpoint. Any provider compatible with /chat/completions can be configured here. The API key is saved in the app database, so use this only in your controlled environment.'}</p>
        </article>
        <article class="card">
          <span class="eyebrow">{'当前状态' if lang == 'zh' else 'Current State'}</span>
          <div class="list-stack">
            <div><div class="subtle">Provider</div><div class="ticker">{html.escape(config.provider_name)}</div></div>
            <div><div class="subtle">Model</div><div class="ticker">{html.escape(config.model or '-')}</div></div>
            <div><div class="subtle">API Key</div><div class="ticker">{html.escape(masked_api_key(config) or ('未设置' if lang == 'zh' else 'Missing'))}</div></div>
            <div><div class="subtle">Status</div><div class="ticker">{('已配置' if lang == 'zh' else 'Configured') if config.is_configured else ('未配置' if lang == 'zh' else 'Not configured')}</div></div>
          </div>
        </article>
      </section>
      <section class="workspace">
        <div class="stack">
          <article class="card">
            <span class="eyebrow">{'保存配置' if lang == 'zh' else 'Save Configuration'}</span>
            <form action="/settings/ai-chat" method="post">
              <input type="hidden" name="lang" value="{lang}" />
              <div class="form-grid">
                <label>
                  <div class="subtle">Provider</div>
                  <select name="provider" id="ai-provider">{provider_options}</select>
                </label>
                <label>
                  <div class="subtle">{'显示名称' if lang == 'zh' else 'Display Name'}</div>
                  <input name="provider_name" id="ai-provider-name" value="{html.escape(config.provider_name, quote=True)}" />
                </label>
                <label>
                  <div class="subtle">Base URL</div>
                  <input name="base_url" id="ai-base-url" value="{html.escape(config.base_url, quote=True)}" />
                </label>
                <label>
                  <div class="subtle">Model</div>
                  <input name="model" id="ai-model" list="ai-model-hints" value="{html.escape(config.model, quote=True)}" />
                  <datalist id="ai-model-hints">
                    <option value="gemini-2.5-flash"></option>
                    <option value="gemini-2.5-pro"></option>
                    <option value="gemini-1.5-flash"></option>
                    <option value="gemini-1.5-pro"></option>
                    <option value="gpt-4.1-mini"></option>
                    <option value="deepseek-chat"></option>
                    <option value="qwen-plus"></option>
                    <option value="moonshot-v1-8k"></option>
                  </datalist>
                </label>
                <label>
                  <div class="subtle">API Key</div>
                  <input name="api_key" type="password" placeholder="{'留空则保留当前 Key' if lang == 'zh' else 'Leave blank to keep current key'}" />
                </label>
                <label>
                  <div class="subtle">Temperature</div>
                  <input name="temperature" type="number" step="0.1" min="0" max="1.5" value="{config.temperature:.1f}" />
                </label>
                <label>
                  <div class="subtle">Timeout seconds</div>
                  <input name="timeout_seconds" type="number" step="1" min="5" max="120" value="{config.timeout_seconds:.0f}" />
                </label>
              </div>
              <div class="form-actions">
                <label class="checkline"><input type="checkbox" name="clear_api_key" value="1" /> {'清除当前 API Key' if lang == 'zh' else 'Clear current API key'}</label>
                <button type="submit">{'保存 AI 配置' if lang == 'zh' else 'Save AI Config'}</button>
                <a class="top-pill" href="/ai-chat?lang={lang}">{'打开 AI 问答' if lang == 'zh' else 'Open AI Q&A'}</a>
              </div>
            </form>
          </article>
        </div>
        <div class="stack">
          <article class="card">
            <span class="eyebrow">{'Provider 预设' if lang == 'zh' else 'Provider Presets'}</span>
            <div class="quick-grid">{preset_cards}</div>
          </article>
          <article class="card">
            <span class="eyebrow">{'使用建议' if lang == 'zh' else 'Usage Notes'}</span>
            <div class="list-stack">
              <div class="list-row"><div><div class="ticker">{'优先低温度' if lang == 'zh' else 'Prefer low temperature'}</div><div class="subtle">{'股票复盘不是写作文，建议 temperature 0.1-0.3。' if lang == 'zh' else 'Trading review is not creative writing; 0.1-0.3 is recommended.'}</div></div></div>
              <div class="list-row"><div><div class="ticker">{'不要直接照单买' if lang == 'zh' else 'Do not copy blindly'}</div><div class="subtle">{'AI 问答会读取你的应用上下文，但仍然只能作为分析与检查清单。' if lang == 'zh' else 'AI Q&A reads your app context, but it is still an analysis/checklist assistant.'}</div></div></div>
              <div class="list-row"><div><div class="ticker">{'兼容接口' if lang == 'zh' else 'Compatible endpoint'}</div><div class="subtle">{'当前按 OpenAI-compatible /chat/completions 调用。' if lang == 'zh' else 'Current calls use OpenAI-compatible /chat/completions.'}</div></div></div>
              <div class="list-row"><div><div class="ticker">Gemini Model ID</div><div class="subtle">{'Gemini 需要填写 API model id，例如 gemini-2.5-flash；不要填写 Gemini 3.1 Pro / Gemini 3.1 Flash Preview 这类展示名。' if lang == 'zh' else 'Gemini requires API model ids such as gemini-2.5-flash; do not use display names such as Gemini 3.1 Pro.'}</div></div></div>
            </div>
          </article>
        </div>
      </section>
      <script>
        const presets = {json.dumps(AI_CHAT_PROVIDER_PRESETS, ensure_ascii=False)};
        const select = document.getElementById('ai-provider');
        select?.addEventListener('change', () => {{
          const item = presets[select.value];
          if (!item) return;
          document.getElementById('ai-provider-name').value = item.label || '';
          document.getElementById('ai-base-url').value = item.base_url || '';
          document.getElementById('ai-model').value = item.model || '';
        }});
      </script>
    """
    return _settings_shell(
        lang=lang,
        title="AI 问答配置" if lang == "zh" else "AI Q&A Settings",
        lead="",
        body_html=body_html,
        active_path="ai_chat",
    )


@router.post("/ai-chat")
def save_ai_chat_settings(
    request: Request,
    lang: str = Form("zh"),
    provider: str = Form("compatible"),
    provider_name: str = Form("OpenAI Compatible"),
    base_url: str = Form("https://api.openai.com/v1"),
    model: str = Form("gpt-4.1-mini"),
    api_key: str = Form(""),
    temperature: float = Form(0.2),
    timeout_seconds: float = Form(30.0),
    clear_api_key: str | None = Form(None),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/settings/ai-chat")
    normalized_lang = "zh" if lang == "zh" else "en"
    with SessionLocal() as db:
        save_ai_chat_config(
            db,
            provider=provider,
            provider_name=provider_name,
            base_url=base_url,
            model=model,
            api_key="" if clear_api_key else api_key,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            keep_existing_key=not bool(clear_api_key),
        )
    return RedirectResponse(url=f"/settings/ai-chat?lang={normalized_lang}", status_code=303)
