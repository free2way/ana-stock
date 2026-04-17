import csv
import html
import json
import re
from io import StringIO
from urllib.parse import urlencode
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db_session
from app.models.schema import SymbolCreate
from app.services.ai_daily_report import (
    build_ai_daily_report,
    build_close_review_action_feed,
    list_ai_daily_report_history,
    load_ai_daily_report,
    load_ai_daily_report_history_item,
    render_ai_daily_report_message,
    save_ai_daily_report,
)
from app.services.auth import is_authenticated, login_redirect
from app.services.auto_analysis import auto_analysis_service
from app.services.close_review_scheduler import close_review_scheduler_service
from app.services.focus_pool import enrich_focus_pool_with_symbols, load_today_focus_pool
from app.services.dashboard_summary import load_dashboard_summary, load_recent_jobs_summary
from app.services.market_intelligence import build_market_narrative_brief
from app.services.market_lake import load_lake_price_history
from app.services.market_news import MarketNewsService
from app.services.market_sync import sync_market_data
from app.services.model_signal_summary import build_model_state, build_signal_label, enrich_model_output, model_confidence
from app.services.portfolio_book import load_portfolio_positions
from app.services.price_snapshot import load_latest_close
from app.services.push_notifications import PushNotificationService
from app.services.repository import (
    BacktestRepository,
    ConceptSnapshotRepository,
    DataJobRepository,
    ModelRunRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    PriceSyncStateRepository,
    SymbolRepository,
    TechnicalSnapshotRepository,
    WatchlistRepository,
)
from app.services.runtime_cache import get_or_set
from app.services.screener import ScreenerService
from app.services.social_signals import social_signal_summary
from app.services.symbol_details import SymbolDataService
from app.services.time_utils import format_app_datetime
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html
from app.services.workspace_snapshots import (
    SNAPSHOT_CONTINUOUS_LEADERS,
    SNAPSHOT_DASHBOARD_NLP,
    SNAPSHOT_HOME_PORTFOLIO,
    SNAPSHOT_HOME_WATCHLIST,
    SNAPSHOT_MARKET_HEATMAP_WORKSPACE,
    SNAPSHOT_MARKET_WORKSPACE,
    SNAPSHOT_MARKET_WORKSPACE_MONITOR,
    SNAPSHOT_MODEL_CANDIDATES,
    SNAPSHOT_PIPELINE_STATUS,
    load_latest_workspace_snapshot,
)


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _provider_strategy_view(lang: str) -> dict:
    if lang == "zh":
        return {
            "title": "Provider 策略",
            "copy": "先看系统如何自动选源，再看最近一次实际上用了哪个数据源。",
            "price_auto": "价格数据 `auto`：A 股优先 TuShare，其他市场优先 yfinance。",
            "price_openbb": "价格数据 `openbb`：走 OpenBB 包装层，并保留现有 fallback 能力。",
            "fund_auto": "基本面 `auto`：A 股走 TuShare，美股/港股走 OpenBB 或 yfinance fundamentals。",
            "concept_auto": "概念数据 `auto`：当前 A 股概念映射统一走 TuShare。",
            "execution": "执行与实时：后续会放到 `execution / realtime` 层，而不是混进研究数据层。",
            "ops_title": "当前 provider 口径",
            "ops_copy": "任务配置里现在推荐优先用 `auto` 或按市场选择默认 provider。",
        }
    return {
        "title": "Provider Strategy",
        "copy": "Check how the app chooses providers automatically first, then compare that with the provider actually used most recently.",
        "price_auto": "Price `auto`: CN prefers TuShare, while other markets default to yfinance.",
        "price_openbb": "Price `openbb`: goes through the OpenBB wrapper and keeps the existing fallback behavior.",
        "fund_auto": "Fundamentals `auto`: CN uses TuShare, while US/HK uses OpenBB or yfinance fundamentals.",
        "concept_auto": "Concept `auto`: current CN concept mapping is standardized on TuShare.",
        "execution": "Execution and realtime are reserved for the future `execution / realtime` layer instead of the research data layer.",
        "ops_title": "Current provider policy",
        "ops_copy": "Job configuration now prefers `auto` or a market-aware default provider.",
    }


def _dashboard_home_panels(
    *,
    session_mode: str,
    latest_signals: list[dict],
    focus_items: list[dict],
    risk_overview: dict,
) -> dict:
    cache_key = json.dumps(
        {
            "mode": session_mode,
            "signals": [
                {
                    "ticker": item.get("ticker"),
                    "trade_date": item.get("trade_date"),
                    "score": item.get("score"),
                }
                for item in latest_signals[:5]
            ],
            "focus": [
                {
                    "ticker": item.get("ticker"),
                    "reason": item.get("selection_reason"),
                }
                for item in focus_items[:5]
            ],
            "risk_tags": risk_overview.get("top_tags", []),
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _load() -> dict:
        try:
            snapshot_boards = ScreenerService().build_market_snapshot(market="CN", limit_per_board=4, mode=session_mode)
        except Exception:
            snapshot_boards = []
        snapshot_top_lines: list[str] = []
        for board in snapshot_boards[:2]:
            rows = board.get("rows") or []
            if not rows:
                continue
            top = rows[0]
            snapshot_top_lines.append(
                f"{board['title_zh']}: {top.get('ticker')} · {int(top.get('snapshot_score') or 0)}"
            )
        market_narrative = build_market_narrative_brief(
            latest_signals=latest_signals,
            focus_items=focus_items,
            risk_overview=risk_overview,
            snapshot_lines=snapshot_top_lines,
        )
        try:
            market_headlines = MarketNewsService().fetch_headlines(limit=3)
        except Exception:
            market_headlines = []
        return {
            "snapshot_boards": snapshot_boards,
            "snapshot_top_lines": snapshot_top_lines,
            "market_narrative": market_narrative,
            "market_headlines": market_headlines,
        }

    return get_or_set("dashboard_home_panels", cache_key, ttl_seconds=90.0, loader=_load)


def _signal_status_tone(status: str | None) -> str:
    normalized = str(status or "").upper()
    if normalized == "READY":
        return "sig-buy"
    if normalized in {"REVIEW", "DEFER"}:
        return "sig-watch"
    if normalized == "BLOCKED":
        return "sig-sell"
    return "sig-hold"


def _dashboard_signal_action_sets(latest_signals: list[dict]) -> dict[str, list[dict]]:
    actionable: list[dict] = []
    blocked: list[dict] = []
    trim_review: list[dict] = []
    for item in latest_signals:
        status = str(item.get("tradability_status") or "").upper()
        score = float(item.get("score") or 0.0)
        priority = item.get("priority") if item.get("priority") is not None else 99
        enriched = {
            **item,
            "status_tone": _signal_status_tone(status),
            "status_label": status or "UNKNOWN",
            "target_weight_pct": round(float(item.get("target_weight") or 0.0) * 100.0, 1) if item.get("target_weight") is not None else None,
            "risk_flags_text": "/".join((item.get("risk_flags") or [])[:2]) or "-",
            "sort_key": (priority, -score, item.get("ticker") or ""),
        }
        if status == "READY":
            actionable.append(enriched)
        elif status == "BLOCKED":
            blocked.append(enriched)
        elif status in {"REVIEW", "DEFER"}:
            trim_review.append(enriched)
    actionable.sort(key=lambda item: item["sort_key"])
    blocked.sort(key=lambda item: item["sort_key"])
    trim_review.sort(key=lambda item: item["sort_key"])
    return {"actionable": actionable[:5], "blocked": blocked[:5], "trim_review": trim_review[:5]}


def _dashboard_trading_regime(
    *,
    latest_signals: list[dict],
    risk_overview: dict,
    lang: str,
) -> dict[str, str]:
    signal_sets = _dashboard_signal_action_sets(latest_signals)
    actionable = len(signal_sets["actionable"])
    blocked = len(signal_sets["blocked"])
    review = len(signal_sets["trim_review"])
    top_tags = [str(tag).lower() for tag in (risk_overview.get("top_tags") or [])]

    if actionable >= max(3, blocked + 1) and review <= actionable:
        label = "进攻" if lang == "zh" else "Offense"
        detail = (
            "可执行候选多于受阻与复核，今天可以先配风险。"
            if lang == "zh"
            else "More names are actionable than blocked or under review, so risk can be added selectively."
        )
    elif blocked >= max(2, actionable) or any("drawdown" in tag or "gap" in tag for tag in top_tags):
        label = "防守" if lang == "zh" else "Defense"
        detail = (
            "受阻候选和风险标记偏多，今天先控制风险再谈加仓。"
            if lang == "zh"
            else "Blocked candidates and risk tags dominate, so risk control should come before adding exposure."
        )
    else:
        label = "平衡" if lang == "zh" else "Balanced"
        detail = (
            "可执行与复核信号并存，适合边做边核。"
            if lang == "zh"
            else "Actionable and review names are mixed, so proceed selectively and verify as you go."
        )
    return {"label": label, "detail": detail}


def _dashboard_watchlist_map(db: Session) -> dict[str, dict]:
    def _load() -> dict[str, dict]:
        watchlist_repo = WatchlistRepository(db)
        watchlist = watchlist_repo.get_or_create_default()
        return watchlist_repo.list_ticker_map(watchlist.id)

    return get_or_set("dashboard_watchlist_map", "default", ttl_seconds=30.0, loader=_load)


def _dashboard_focus_items() -> list[dict]:
    def _load() -> list[dict]:
        return enrich_focus_pool_with_symbols(load_today_focus_pool())[:3]

    return get_or_set("dashboard_focus_items", "today", ttl_seconds=30.0, loader=_load)


def _render_dashboard_home_panels_fragment(
    *,
    db: Session,
    lang: str,
    lookback_runs: int,
    session_mode: str,
    latest_signals: list[dict],
    recent_jobs: list[dict],
    market_context: dict,
    continuous_sort_by: str,
    continuous_sort_order: str,
    continuous_market: str,
    continuous_state: str,
) -> str:
    def _job_suggested_action(item: dict) -> str | None:
        status = str(item.get("status") or "").lower()
        job_type = str(item.get("job_type") or "").lower()
        message = str(item.get("message") or "").lower()
        if status == "success":
            return None
        if "guce.yahoo.com" in message or "nodename nor servname" in message or "yahoo" in message:
            return "建议动作：切换到 TuShare 或稍后重试。" if lang == "zh" else "Suggested action: switch to TuShare or retry later."
        if "no sync-enabled watchlist stocks found" in message:
            return "建议动作：先在自选股里开启同步，再重跑自动分析。" if lang == "zh" else "Suggested action: enable sync for watchlist names, then rerun auto analysis."
        if "not_configured" in status or "not configured" in message:
            return "建议动作：先检查相关数据源或 webhook 配置。" if lang == "zh" else "Suggested action: verify the related data source or webhook configuration first."
        if status == "partial":
            if "refresh" in job_type or "sync" in job_type:
                return "建议动作：可重跑一次，或缩小批次后再试。" if lang == "zh" else "Suggested action: rerun once, or retry with a smaller batch."
            return "建议动作：打开任务记录页查看详情。" if lang == "zh" else "Suggested action: open the job history page for details."
        if status == "failed":
            if "close_review" in job_type or "watchlist_auto_analysis" in job_type:
                return "建议动作：先看任务详情，再手动重跑这条链路。" if lang == "zh" else "Suggested action: inspect the job details, then rerun the workflow manually."
            return "建议动作：打开任务记录页查看失败原因。" if lang == "zh" else "Suggested action: open the job history page to inspect the failure."
        return None

    watchlist_map = _dashboard_watchlist_map(db)
    continuous_rows_source = list(market_context.get("continuous_leaders", []))
    for item in continuous_rows_source:
        existing = watchlist_map.get(item["ticker"])
        if existing is None:
            state_key = "OFF"
        elif existing.get("sync_enabled") and existing.get("sync_status") == "success":
            state_key = "READY"
        elif existing.get("sync_enabled"):
            state_key = "WAITING"
        else:
            state_key = "IN"
        item["continuous_state_key"] = state_key
    if continuous_market != "ALL":
        continuous_rows_source = [item for item in continuous_rows_source if item.get("market") == continuous_market]
    if continuous_state != "ALL":
        continuous_rows_source = [item for item in continuous_rows_source if item.get("continuous_state_key") == continuous_state]

    def _continuous_sort_rank(item: dict) -> tuple:
        if continuous_sort_by == "ticker":
            return (item["ticker"],)
        if continuous_sort_by == "score":
            return (float(item.get("score") or 0.0), item["ticker"])
        if continuous_sort_by == "trend":
            history = item.get("score_history") or []
            last_delta = (history[-1] - history[0]) if len(history) >= 2 else 0.0
            return (float(last_delta), item["ticker"])
        return (int(item.get("hits") or 0), float(item.get("score") or 0.0), item["ticker"])

    continuous_rows_source.sort(key=_continuous_sort_rank, reverse=continuous_sort_order != "asc")
    continuous_rows_parts: list[str] = []
    for item in continuous_rows_source:
        state_label, state_bg, state_fg = _concept_ticker_watch_state(watchlist_map, item["ticker"], lang)
        continuous_rows_parts.append(
            "<article class='leader-card'>"
            f"<div class='leader-top'><a class='leader-ticker' href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a><span class='leader-market'>{item['market']}</span></div>"
            f"<div class='leader-name'>{item['name']}</div>"
            f"<div class='leader-metrics'><span class='leader-chip'>{item['hits']}/{item['runs']} {'次' if lang == 'zh' else 'hits'}</span><span class='leader-chip'>{item['score']:.4f}</span></div>"
            f"<div style='margin:0 0 8px 0;'>{_dashboard_model_badge(item.get('state'), confidence=item.get('confidence'), compact=True)}</div>"
            f"<div style='margin-bottom:8px;'>{_signal_pill(item.get('score'), lang=lang, strength=int(item.get('signal_strength') or 0), compact=True)}</div>"
            f"<div class='leader-trend'>{_score_sparkline_svg(item.get('score_history', []))}</div>"
            f"<div class='leader-foot'><span>{item.get('trade_date') or '-'}</span><span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;background:{state_bg};color:{state_fg};font-size:12px;font-weight:800;white-space:nowrap;'>{state_label}</span></div>"
            "</article>"
        )
    continuous_rows = "".join(continuous_rows_parts[:3]) or f"<div class='muted'>{'暂无连续强势股' if lang == 'zh' else 'No continuous leaders yet'}</div>"
    focus_items = _dashboard_focus_items()
    focus_lines = [
        f"{item.get('ticker')} · {' / '.join((item.get('matched_patterns') or [])[:2]) or (item.get('selection_reason') or '-')}"
        for item in focus_items
    ]
    risk_overview = market_context.get("risk_overview", {})
    home_panels = _dashboard_home_panels(
        session_mode=session_mode,
        latest_signals=latest_signals,
        focus_items=focus_items,
        risk_overview=risk_overview,
    )
    signal_sets = _dashboard_signal_action_sets(latest_signals)
    ai_daily_report = _load_cached_ai_daily_report(db)
    snapshot_top_lines = list(home_panels.get("snapshot_top_lines") or [])
    market_narrative = home_panels["market_narrative"]
    market_headlines = home_panels["market_headlines"]
    recent_job_lines = []
    for item in recent_jobs[:3]:
        status = str(item.get("status") or "unknown").lower()
        if status == "success":
            bg, fg = "#dcfce7", "#166534"
        elif status == "partial":
            bg, fg = "#fef3c7", "#92400e"
        else:
            bg, fg = "#fee2e2", "#991b1b"
        suggested_action = _job_suggested_action(item)
        action_html = (
            f"<div class='muted' style='margin-top:4px;font-weight:700;color:#92400e;'>{suggested_action}</div>"
            if suggested_action
            else ""
        )
        recent_job_lines.append(
            "<div style='display:flex;gap:8px;align-items:flex-start;margin-bottom:8px;'>"
            f"<span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;background:{bg};color:{fg};font-size:12px;font-weight:800;white-space:nowrap;'>{status.upper()}</span>"
            "<div>"
            f"<div class='muted'>{item.get('job_type') or '-'} · {(item.get('message') or '-')}</div>"
            f"{action_html}"
            "</div>"
            "</div>"
        )
    actionable_rows = "".join(
        "<div style='display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{item.get('ticker')}</div><div class='muted'>{item.get('name') or item.get('ticker')}</div><div class='muted'>{item.get('execution_note') or item.get('entry_trigger') or '-'}</div></div>"
        f"<div style='text-align:right;'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='muted'>{(str(item.get('target_weight_pct')) + '%') if item.get('target_weight_pct') is not None else '-'}</div></div>"
        "</div>"
        for item in signal_sets["actionable"]
    ) or f"<div class='muted'>{'暂无可执行候选' if lang == 'zh' else 'No actionable candidates yet'}</div>"
    blocked_rows = "".join(
        "<div style='display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{item.get('ticker')}</div><div class='muted'>{item.get('name') or item.get('ticker')}</div><div class='muted'>{item.get('block_reason') or item.get('execution_note') or '-'}</div></div>"
        f"<div style='text-align:right;'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='muted'>{item.get('risk_flags_text')}</div></div>"
        "</div>"
        for item in signal_sets["blocked"]
    ) or f"<div class='muted'>{'暂无受阻候选' if lang == 'zh' else 'No blocked candidates'}</div>"
    review_rows = "".join(
        "<div style='display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{item.get('ticker')}</div><div class='muted'>{item.get('name') or item.get('ticker')}</div><div class='muted'>{item.get('execution_note') or item.get('invalidation_condition') or '-'}</div></div>"
        f"<div style='text-align:right;'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='muted'>{item.get('risk_flags_text')}</div></div>"
        "</div>"
        for item in signal_sets["trim_review"]
    ) or f"<div class='muted'>{'暂无复核队列' if lang == 'zh' else 'No review queue yet'}</div>"
    return f"""
      <article class="card">
        <div class="eyebrow">{'今日行动板' if lang == 'zh' else 'Today Action Board'}</div>
        <div class="muted">{'把今天最该先看的快照榜首、连续强势股和重点盯盘池放在一起。' if lang == 'zh' else 'A compact board that combines top snapshot names, continuous leaders, and today focus items.'}</div>
        <div class="stack" style="margin-top:12px;">
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{'市场快照' if lang == 'zh' else 'Market Snapshot'}</div>
            <div class="muted">{'<br/>'.join(snapshot_top_lines) or '-'}</div>
          </div>
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{'连续强势股' if lang == 'zh' else 'Continuous Leaders'}</div>
            <div class="muted">{" / ".join(f"{item.get('ticker')} · {item.get('name') or item.get('ticker')}" for item in continuous_rows_source[:3]) or '-'}</div>
          </div>
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{'今日重点盯盘池' if lang == 'zh' else 'Today Focus Pool'}</div>
            <div class="muted">{'<br/>'.join(focus_lines) or '-'}</div>
          </div>
          <div class="stack">
            <a class="action-link" href="/screeners/market-snapshot?lang={lang}&mode={session_mode}">{'打开市场快照榜单' if lang == 'zh' else 'Open Market Snapshot'}</a>
            <a class="action-link" href="/screeners/focus/today?lang={lang}">{'打开今日重点盯盘池' if lang == 'zh' else 'Open Today Focus Pool'}</a>
            <a class="action-link" href="/watchlist?lang={lang}&mode={session_mode}">{_dt(lang, 'open_watchlist')}</a>
          </div>
        </div>
      </article>
      <article class="card">
        <div class="eyebrow">{'交易候选' if lang == 'zh' else 'Actionable Candidates'}</div>
        <div class="muted">{'先看今天能做、要复核、以及不能做的票。' if lang == 'zh' else 'Start with what is actionable, what needs review, and what is blocked.'}</div>
        <div class="stack" style="margin-top:12px;">
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{'可执行' if lang == 'zh' else 'Ready'}</div>
            {actionable_rows}
          </div>
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{'待复核 / 减仓' if lang == 'zh' else 'Review / Trim'}</div>
            {review_rows}
          </div>
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{'受阻候选' if lang == 'zh' else 'Blocked'}</div>
            {blocked_rows}
          </div>
        </div>
      </article>
      <article class="card">
        <div class="eyebrow">{'市场叙事' if lang == 'zh' else 'Market Narrative'}</div>
        <div class="muted">{market_narrative.get('headline') or '-'}</div>
        <div class="stack" style="margin-top:12px;">
          {"".join(f"<div class='muted'>{item}</div>" for item in (market_narrative.get('bullets') or [])) or "<div class='muted'>-</div>"}
          {"".join(f"<div class='muted'><a href='{item.get('link') or '#'}' target='_blank' rel='noreferrer'>{item.get('title')}</a></div>" for item in market_headlines) if market_headlines else ""}
        </div>
      </article>
      <article class="card">
        <div class="eyebrow">{'最近任务状态' if lang == 'zh' else 'Recent Job Status'}</div>
        <div class="muted">{'直接看最近任务是否成功、部分完成，还是失败。' if lang == 'zh' else 'A compact view of whether the latest jobs finished successfully, partially, or failed.'}</div>
        <div class="stack" style="margin-top:12px;">
          {"".join(recent_job_lines) or f"<div class='muted'>{'暂无任务记录' if lang == 'zh' else 'No recent jobs yet'}</div>"}
          <a class="action-link" href="/dashboard/ops/jobs?lang={lang}">{'打开任务记录页' if lang == 'zh' else 'Open Job History'}</a>
        </div>
      </article>
      {_render_ai_daily_report_card(ai_daily_report)}
      <article class="card">
        <div class="eyebrow">{_dt(lang, 'continuous_leaders')}</div>
        <div class="muted">{_dt(lang, 'continuous_help', runs=lookback_runs)}</div>
        <div style="margin:10px 0 12px;">
          <a href="/dashboard/continuous-leaders?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'continuous_sort_by': continuous_sort_by, 'continuous_sort_order': continuous_sort_order, 'continuous_market': continuous_market, 'continuous_state': continuous_state})}" class="action-link">{_dt(lang, 'open_continuous_leaders')}</a>
        </div>
        <div class="leader-grid">{continuous_rows}</div>
      </article>
    """


def _render_dashboard_top_fragment(
    *,
    lang: str,
    latest_signals: list[dict],
    latest_model: dict | None,
    risk_overview: dict,
) -> str:
    signal_items = "".join(
        "<article class='signal-card'>"
        f"<div class='signal-top'><a class='signal-ticker' href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a><span class='signal-rank'>#{int(item['rank_value'])}</span></div>"
        f"<div class='signal-date'>{item.get('name') or item['ticker']}</div>"
        f"<div class='signal-date'>{item['trade_date']}</div>"
        f"<div style='margin-bottom:8px;'><span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;background:{build_model_state(item.get('score'), lang=lang)['bg']};color:{build_model_state(item.get('score'), lang=lang)['fg']};font-weight:800;font-size:12px;'>{build_model_state(item.get('score'), lang=lang)['label']}</span></div>"
        f"<div class='signal-score'>{item['score']:.6f}</div>"
        f"<div style='margin-top:6px;'>{_signal_pill(item.get('score'), lang=lang, compact=True)}</div>"
        f"<div class='signal-foot' title='{latest_model['name'] if latest_model else ('最新模型' if lang == 'zh' else 'Latest model')}'>{_compact_run_name(latest_model['name'], 24) if latest_model else ('最新模型' if lang == 'zh' else 'Latest model')}"
        f"{' · ' + str(model_confidence(item.get('score'))) + '%' if model_confidence(item.get('score')) is not None else ''}</div>"
        "</article>"
        for item in latest_signals[:3]
    ) or f"<div class='muted'>{'暂无信号' if lang == 'zh' else 'No signals yet'}</div>"
    risk_tag_html = "".join(
        f"<span class='leader-chip'>{item['tag']} · {item['count']}</span>"
        for item in risk_overview.get("top_tags", [])
    ) or f"<span class='muted'>{_dt(lang, 'no_execution_risks')}</span>"
    risk_example_html = "".join(
        f"<span class='leader-chip'>{item['ticker']} · {' / '.join(item.get('tags') or [])}</span>"
        for item in risk_overview.get("examples", [])
    )
    return f"""
      <section class="card" style="margin-bottom:16px;">
        <div class="eyebrow">{_dt(lang, 'risk_overview')}</div>
        <div class="grid" style="margin-bottom:0;">
          <article class="card" style="margin-bottom:0;background:#f9f7f0;">
            <div class="eyebrow">{_dt(lang, 'tagged_names')}</div>
            <div class="metric">{int(risk_overview.get('tagged_names') or 0)}</div>
            <div class="muted">{_dt(lang, 'risk_examples')}</div>
          </article>
          <article class="card" style="margin-bottom:0;background:#f9f7f0;">
            <div class="eyebrow">{_dt(lang, 'common_risks')}</div>
            <div class="leader-metrics">{risk_tag_html}</div>
            <div class="muted">{_dt(lang, 'risk_examples')}: {risk_example_html or '-'}</div>
          </article>
        </div>
      </section>
      <section class="card" style="margin-bottom:16px;">
        <div class="eyebrow">{_dt(lang, 'latest_signals')}</div>
        <div class="muted" style="margin-bottom:10px;">{'首页只保留最新前三只信号，完整视图请去选股器或个股页。' if lang == 'zh' else 'Only the latest top 3 signals stay on the home page. Use Screeners or Insight pages for the full view.'}</div>
        <div class="signal-grid">{signal_items}</div>
      </section>
    """


def _render_ai_daily_report_card(report: dict | None) -> str:
    payload = report or {}
    rows = payload.get("rows") or []
    strategy = payload.get("strategy") or {}
    preview = "".join(
        (
            f"<div class='muted' style='margin-top:8px;'><strong>{item.get('ticker')}</strong> · {item.get('name') or item.get('ticker')} · {item.get('verdict') or '-'} · 仓位 {item.get('target_weight') or '-'} · {item.get('tradability_status') or '-'}"
            f"<br/>触发 {item.get('entry_trigger') or '-'} · 失效 {item.get('invalidation_condition') or '-'}"
            f"<br/>周期 {item.get('time_horizon') or '-'} · 滑点 {item.get('max_slippage_bps') or '-'}bps · {item.get('liquidity_bucket') or '-'} 桶</div>"
        )
        for item in rows[:3]
    ) or "<div class='muted' style='margin-top:8px;'>No AI daily report yet.</div>"
    return (
        "<article class='card'>"
        "<div class='eyebrow'>AI Daily Report</div>"
        f"<div class='metric' style='font-size:24px;'>{payload.get('mood') or '-'}</div>"
        f"<div class='muted'>{payload.get('headline') or 'Run watchlist auto analysis to generate a daily AI dashboard.'}</div>"
        f"<div class='muted' style='margin-top:8px;'><strong>{strategy.get('headline') or '-'}</strong></div>"
        f"<div class='muted' style='margin-top:6px;'>{strategy.get('playbook') or '-'}</div>"
        f"{preview}"
        "<div style='margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;'><a class='pill' href='/dashboard/ai-daily-report'>Open AI Daily Dashboard</a><a class='pill' href='/dashboard/ai-daily-report/message'>Push Ready Text</a></div>"
        "</article>"
    )


CONCEPT_TEXT = {
    "en": {
        "back_to_dashboard": "Back to dashboard",
        "concept_detail": "Concept Detail",
        "detail_subtitle": "A deeper look at the latest model hits inside this concept.",
        "continuous_leaders": "Continuous Leaders",
        "continuous_detail": "Continuous Leader Detail",
        "continuous_subtitle": "Stocks that keep showing up across recent model snapshots, with watchlist actions and quick filtering.",
        "market_filter": "Market",
        "state_filter": "Watchlist State",
        "apply_filters": "Apply Filters",
        "hits": "Hits",
        "hits_help": "Current Top-N names inside this concept",
        "delta": "Delta",
        "delta_help": "Change versus the previous Top-N snapshot",
        "streak": "Streak",
        "streak_help": "Consecutive snapshots with at least one hit",
        "trend": "Trend",
        "trend_help": "Recent Top-N concept hit trend",
        "follow_this_concept": "Follow This Concept",
        "auto_enable_sync": "Auto-enable Sync for added stocks",
        "sync_now": "Sync concept stocks now",
        "add_concept_stocks": "Add Concept Stocks To Watchlist",
        "top_n_watch": "Top-N Watch",
        "top_n_help": "Only add top N tickers from the current sort order",
        "sync_selected_top_n": "Sync selected top N now",
        "add_top_n": "Add Top N To Watchlist",
        "top_movers_comparison": "Top Movers Comparison",
        "top_by_model": "Top by Model",
        "top_by_20d": "Top by 20D",
        "ready_first": "Ready First",
        "last": "Last",
        "ticker_breakdown": "Ticker Breakdown",
        "ticker": "Ticker",
        "name": "Name",
        "model_score": "Model Score",
        "five_day": "5D %",
        "twenty_day": "20D %",
        "breadth": "Breadth",
        "breadth_help": "Share of concept members with positive recent performance.",
        "concept_strength": "Concept Strength",
        "concept_strength_subtitle": "Price-based confirmation for whether this concept is actually moving, not just getting model attention.",
        "buy_signal_count": "Buy Signals",
        "buy_signal_count_help": "How many tracked tickers inside this concept currently show a Buy signal.",
        "max_signal_strength": "Max Signal Strength",
        "max_signal_strength_help": "The strongest model signal currently found inside this concept.",
        "watchlist": "Watchlist",
        "last_sync": "Last Sync",
        "actions": "Actions",
        "add": "Add",
        "sync": "Sync",
        "open": "Open",
        "insight": "Insight",
        "not_in_watchlist": "Not In Watchlist",
        "ready": "Ready",
        "waiting": "Waiting",
        "in_watchlist": "In Watchlist",
        "no_tickers": "No tickers yet",
        "not_enough_price_history": "Not enough price history yet.",
        "lang_en": "English",
        "lang_zh": "中文",
    },
    "zh": {
        "back_to_dashboard": "返回总览",
        "concept_detail": "概念详情",
        "detail_subtitle": "查看这个概念在最近模型 Top-N 中的命中构成。",
        "continuous_leaders": "连续强势股",
        "continuous_detail": "连续强势股详情",
        "continuous_subtitle": "查看最近几次模型快照中持续出现的股票，并直接进行筛选、自选和同步操作。",
        "market_filter": "市场",
        "state_filter": "自选状态",
        "apply_filters": "应用筛选",
        "hits": "命中数",
        "hits_help": "当前 Top-N 中属于该概念的股票数量",
        "delta": "变化值",
        "delta_help": "相对上一次 Top-N 快照的变化",
        "streak": "连续性",
        "streak_help": "连续多少次快照里至少出现过一只命中股",
        "trend": "趋势",
        "trend_help": "最近几次 Top-N 中这个概念的热度走势",
        "follow_this_concept": "跟踪这个概念",
        "auto_enable_sync": "加入后自动开启同步",
        "sync_now": "立即同步概念股票",
        "add_concept_stocks": "将概念股加入自选",
        "top_n_watch": "前 N 名跟踪",
        "top_n_help": "只加入当前排序下前 N 个股票",
        "sync_selected_top_n": "立即同步选中的前 N 名",
        "add_top_n": "将前 N 名加入自选",
        "top_movers_comparison": "强势股对比",
        "top_by_model": "按模型分数",
        "top_by_20d": "按20日强度",
        "ready_first": "优先已就绪",
        "last": "最新",
        "ticker_breakdown": "股票明细",
        "ticker": "代码",
        "name": "名称",
        "model_score": "模型分数",
        "five_day": "5日涨跌",
        "twenty_day": "20日涨跌",
        "breadth": "上涨广度",
        "breadth_help": "概念内近期表现为正的股票占比。",
        "concept_strength": "概念强弱",
        "concept_strength_subtitle": "用价格表现确认这个概念是否真的在走强，而不只是模型命中集中。",
        "buy_signal_count": "买点股数量",
        "buy_signal_count_help": "这个概念里当前显示买点信号的股票数量。",
        "max_signal_strength": "最强信号强度",
        "max_signal_strength_help": "这个概念里当前最强模型信号的强度值。",
        "watchlist": "自选状态",
        "last_sync": "最近同步",
        "actions": "操作",
        "add": "加入",
        "sync": "同步",
        "open": "打开",
        "insight": "分析页",
        "not_in_watchlist": "未加入自选",
        "ready": "已就绪",
        "waiting": "同步中",
        "in_watchlist": "已在自选",
        "no_tickers": "暂无股票",
        "not_enough_price_history": "价格历史还不够。",
        "lang_en": "English",
        "lang_zh": "中文",
    },
}


def _concept_tr(lang: str, key: str) -> str:
    return CONCEPT_TEXT["zh" if lang == "zh" else "en"][key]


DASHBOARD_TEXT = {
    "en": {
        "title": "Personal Quant Workbench",
        "hero": "Personal Quant Workbench",
        "lead": "A local research cockpit for watchlists, screeners, model signals, concept resonance, and replay-friendly stock analysis.",
        "open_watchlist": "Open Watchlist",
        "open_screener": "Open Screener",
        "data_sources": "Data Sources",
        "logout": "Logout",
        "lang_en": "English",
        "lang_zh": "中文",
        "stock_insight_search": "Stock Insight Search",
        "search_placeholder": "Type a ticker like ASTS",
        "open_insight_page": "Open Insight Page",
        "search_help": "This view turns market data into a trend score, buy zone, take-profit zone, and risk level.",
        "auto_analysis": "Auto Analysis",
        "on": "On",
        "off": "Off",
        "enabled": "Enabled",
        "disabled": "Disabled",
        "every_hours": "Every {hours} hour(s)",
        "next_run": "Next run",
        "turn_off": "Turn Off",
        "turn_on": "Turn On",
        "data_source": "Data Source",
        "current_dominant_provider": "Current dominant provider across synced symbols",
        "open_detailed_source_page": "Open detailed source page",
        "latest_model": "Latest Model",
        "status": "Status",
        "type": "Type",
        "signals": "Signals",
        "latest_date": "Latest date",
        "top_ticker": "Top ticker",
        "backtest": "Backtest",
        "run": "Run",
        "period": "Period",
        "concept_resonance": "Concept Resonance",
        "concept_resonance_help": "How concentrated the latest Top-N signals are inside the strongest tracked concept.",
        "tracked_signals": "Tracked signals",
        "snapshot_window": "Snapshot Window",
        "snapshot_help": "Heatmap, concept resonance, and activity tracking are currently based on the most recent {runs} model snapshots.",
        "quick_actions": "Quick Actions",
        "seed_sample_data": "Seed Sample Data",
        "tickers": "Tickers",
        "provider": "Provider",
        "start": "Start",
        "end": "End",
        "sync_market_data": "Sync Market Data",
        "pipeline_tickers": "Pipeline Tickers",
        "pipeline_run_name": "Pipeline Run Name",
        "signal": "Signal",
        "lookback": "Lookback",
        "top_n": "Top N",
        "run_full_pipeline": "Run Full Pipeline",
        "auto_analyze_my_watchlist": "Auto analyze my watchlist",
        "interval_hours": "Interval Hours",
        "start_date": "Start Date",
        "refresh_cn_concepts": "Refresh CN concepts during auto analysis",
        "save_auto_analysis": "Save Auto Analysis",
        "run_watchlist_analysis_now": "Run Watchlist Analysis Now",
        "normalize_only": "Normalize only",
        "build_dataset": "Build Dataset",
        "cn_tickers": "CN Tickers",
        "sync_cn_fundamentals": "Sync CN Fundamentals",
        "cn_concept_tickers": "CN Concept Tickers",
        "sync_cn_concepts": "Sync CN Concepts",
        "us_hk_tickers": "US / HK Tickers",
        "sync_us_hk_fundamentals": "Sync US/HK Fundamentals",
        "run_name": "Run Name",
        "run_training": "Run Training",
        "model_run_id": "Model Run ID",
        "leave_blank_latest": "Leave blank for latest",
        "run_backtest": "Run Backtest",
        "json_shortcuts": "JSON Shortcuts",
        "dashboard_summary_json": "Dashboard Summary JSON",
        "latest_signals_json": "Latest Signals JSON",
        "latest_backtest_curve_json": "Latest Backtest Curve JSON",
        "sync_states_json": "Sync States JSON",
        "latest_signals": "Latest Signals",
        "risk_overview": "Risk Overview",
        "tagged_names": "Tagged names",
        "common_risks": "Common risks",
        "risk_examples": "Examples",
        "no_execution_risks": "No execution warnings across the current focus list.",
        "ticker": "Ticker",
        "date": "Date",
        "score": "Score",
        "rank": "Rank",
        "sector_heatmap": "Sector Heatmap",
        "sector_heatmap_help": "A quick view of where the latest model picks cluster. Stronger tiles now blend Top-N concentration with concept-level 5D strength and breadth.",
        "heatmap_sort": "Heatmap Sort",
        "sort_by_hits": "Top-N Hits",
        "sort_by_5d": "5D Strength",
        "sort_by_breadth": "Breadth",
        "sort_by_score": "Avg Score",
        "signal_distribution": "Signal Distribution",
        "market": "Market",
        "top_n_hits": "Top-N Hits",
        "name": "Name",
        "tickers": "Tickers",
        "continuous_leaders": "Continuous Leaders",
        "hits": "Hits",
        "continuous_help": "Stocks that kept showing up across the most recent {runs} model snapshots. This is the quickest way to spot persistent strength instead of one-off spikes.",
        "open_continuous_leaders": "Open Continuous Leaders",
        "watchlist_state": "Watchlist State",
        "all": "All",
        "ready": "Ready",
        "waiting": "Waiting",
        "off_state": "Off",
        "apply_leader_filters": "Apply Leader Filters",
        "auto_enable_sync": "Auto-enable Sync",
        "sync_top_n_now": "Sync top N now",
        "latest_signal_date": "Latest Signal Date",
        "watchlist": "Watchlist",
        "action": "Action",
        "add": "Add",
        "sync": "Sync",
        "open": "Open",
        "add_top_n_continuous_leaders": "Add Top N Continuous Leaders",
        "concept_activity_tracker": "Concept Activity Tracker",
        "concept": "Concept",
        "prev": "Prev",
        "delta_hits": "Δ Hits",
        "streak": "Streak",
        "trend": "Trend",
        "five_day": "5D",
        "breadth": "Breadth",
        "avg_score": "Avg Score",
        "sync_states": "Sync States",
        "last_sync": "Last Sync",
        "backtest_summary": "Backtest Summary",
        "recent_model_runs": "Recent Model Runs",
        "config": "Config",
        "created": "Created",
        "backtest_this_run": "Backtest This Run",
        "equity_curve": "Equity Curve",
        "recent_jobs": "Recent Jobs",
        "started": "Started",
        "finished": "Finished",
        "params": "Params",
        "message": "Message",
        "concept_data_note": "CN concepts: {freshness} · {as_of}",
    },
    "zh": {
        "title": "个人量化工作台",
        "hero": "个人量化工作台",
        "lead": "一个本地研究控制台，用来管理自选、选股器、模型信号、概念共振和适合复盘的个股分析。",
        "open_watchlist": "打开自选股",
        "open_screener": "打开选股器",
        "data_sources": "数据来源",
        "logout": "退出登录",
        "lang_en": "English",
        "lang_zh": "中文",
        "stock_insight_search": "个股分析搜索",
        "search_placeholder": "输入股票代码，例如 ASTS",
        "open_insight_page": "打开分析页",
        "search_help": "这个页面会把行情转成趋势评分、买入区、止盈区和风险位。",
        "auto_analysis": "自动分析",
        "on": "开启",
        "off": "关闭",
        "enabled": "已启用",
        "disabled": "已停用",
        "every_hours": "每 {hours} 小时运行一次",
        "next_run": "下次运行",
        "turn_off": "关闭",
        "turn_on": "开启",
        "data_source": "数据源",
        "current_dominant_provider": "当前同步股票里最主要的数据源",
        "open_detailed_source_page": "打开数据源详情页",
        "latest_model": "最新模型",
        "status": "状态",
        "type": "类型",
        "signals": "信号",
        "latest_date": "最新日期",
        "top_ticker": "最高分股票",
        "backtest": "回测",
        "run": "运行",
        "period": "区间",
        "concept_resonance": "概念共振",
        "concept_resonance_help": "最新 Top-N 信号集中在最强概念中的程度。",
        "tracked_signals": "跟踪信号数",
        "snapshot_window": "快照窗口",
        "snapshot_help": "热力图、概念共振和概念追踪都基于最近 {runs} 次模型快照。",
        "quick_actions": "快捷操作",
        "seed_sample_data": "注入样例数据",
        "tickers": "股票代码",
        "provider": "数据源",
        "start": "开始日期",
        "end": "结束日期",
        "sync_market_data": "同步行情",
        "pipeline_tickers": "流水线股票",
        "pipeline_run_name": "流水线运行名",
        "signal": "信号",
        "lookback": "回看窗口",
        "top_n": "前 N 名",
        "run_full_pipeline": "运行完整流水线",
        "auto_analyze_my_watchlist": "自动分析我的自选股",
        "interval_hours": "间隔小时数",
        "start_date": "开始日期",
        "refresh_cn_concepts": "自动分析时刷新 A 股概念",
        "save_auto_analysis": "保存自动分析设置",
        "run_watchlist_analysis_now": "立即运行自选股分析",
        "normalize_only": "仅标准化",
        "build_dataset": "构建数据集",
        "cn_tickers": "A股代码",
        "sync_cn_fundamentals": "同步 A 股基本面",
        "cn_concept_tickers": "A股概念股票",
        "sync_cn_concepts": "同步 A 股概念",
        "us_hk_tickers": "美股 / 港股代码",
        "sync_us_hk_fundamentals": "同步美股/港股基本面",
        "run_name": "运行名称",
        "run_training": "运行训练",
        "model_run_id": "模型运行 ID",
        "leave_blank_latest": "留空表示使用最新模型",
        "run_backtest": "运行回测",
        "json_shortcuts": "JSON 快捷入口",
        "dashboard_summary_json": "Dashboard 摘要 JSON",
        "latest_signals_json": "最新信号 JSON",
        "latest_backtest_curve_json": "最新回测曲线 JSON",
        "sync_states_json": "同步状态 JSON",
        "latest_signals": "最新信号",
        "risk_overview": "风险概览",
        "tagged_names": "带提醒股票数",
        "common_risks": "常见提醒",
        "risk_examples": "示例股票",
        "no_execution_risks": "当前重点股票里暂无执行提醒。",
        "ticker": "代码",
        "date": "日期",
        "score": "分数",
        "rank": "排名",
        "sector_heatmap": "板块热力图",
        "sector_heatmap_help": "快速查看最新模型命中集中在哪些概念。热力同时参考 Top-N 集中度、概念 5 日强弱和上涨广度。",
        "heatmap_sort": "热力图排序",
        "sort_by_hits": "按命中数",
        "sort_by_5d": "按 5 日强度",
        "sort_by_breadth": "按上涨广度",
        "sort_by_score": "按平均分数",
        "signal_distribution": "信号分布",
        "market": "市场",
        "top_n_hits": "Top-N 命中数",
        "name": "名称",
        "tickers": "股票",
        "continuous_leaders": "连续强势股",
        "hits": "命中数",
        "continuous_help": "最近 {runs} 次模型快照中持续出现的股票，更适合发现连续强势而不是一次性异动。",
        "open_continuous_leaders": "打开连续强势股",
        "watchlist_state": "自选状态",
        "all": "全部",
        "ready": "已就绪",
        "waiting": "同步中",
        "off_state": "未开启",
        "apply_leader_filters": "应用筛选",
        "auto_enable_sync": "自动开启同步",
        "sync_top_n_now": "立即同步前 N 名",
        "latest_signal_date": "最新信号日期",
        "watchlist": "自选",
        "action": "操作",
        "add": "加入",
        "sync": "同步",
        "open": "打开",
        "add_top_n_continuous_leaders": "将前 N 名连续强势股加入自选",
        "concept_activity_tracker": "概念异动追踪",
        "concept": "概念",
        "prev": "前值",
        "delta_hits": "变化",
        "streak": "连续性",
        "trend": "趋势",
        "five_day": "5日",
        "breadth": "广度",
        "avg_score": "平均分",
        "sync_states": "同步状态",
        "last_sync": "最近同步",
        "backtest_summary": "回测摘要",
        "recent_model_runs": "最近模型运行",
        "config": "配置",
        "created": "创建时间",
        "backtest_this_run": "回测这个运行",
        "equity_curve": "净值曲线",
        "recent_jobs": "最近任务",
        "started": "开始",
        "finished": "完成",
        "params": "参数",
        "message": "消息",
        "concept_data_note": "A股概念：{freshness} · {as_of}",
    },
}


def _dt(lang: str, key: str, **kwargs) -> str:
    value = DASHBOARD_TEXT["zh" if lang == "zh" else "en"][key]
    return value.format(**kwargs) if kwargs else value


def _lookback_options() -> list[int]:
    return [3, 5, 10]


def _clamp_lookback_runs(value: int | None) -> int:
    try:
        numeric = int(value) if value is not None else None
    except (TypeError, ValueError):
        numeric = None
    if numeric in _lookback_options():
        return int(numeric)
    return 5


def _lookback_pills(base_path: str, *, selected: int, extra_params: dict[str, str] | None = None) -> str:
    params = extra_params or {}
    lang = params.get("lang", "en")
    pills = []
    for option in _lookback_options():
        query = urlencode({**params, "lookback_runs": option})
        pills.append(
            f"<a href='{base_path}?{query}' class='compare-pill{' active' if selected == option else ''}'>"
            f"{option} {'次' if lang == 'zh' else 'runs'}"
            "</a>"
        )
    return "".join(pills)


def _load_summary(db: Session, *, lookback_runs: int = 5) -> dict:
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    cache_key = json.dumps({"lookback_runs": lookback_runs}, sort_keys=True, ensure_ascii=False)

    def _load() -> dict:
        return load_dashboard_summary(
            db,
            lookback_runs=lookback_runs,
            market_context_loader=lambda latest_signals: _build_market_context(
                db,
                latest_signals,
                lookback_runs=lookback_runs,
            ),
        )

    return get_or_set("dashboard_summary_bundle", cache_key, ttl_seconds=60.0, loader=_load)


def _lightweight_market_context(latest_signals: list[dict]) -> dict:
    risk_counts: dict[str, int] = {}
    tagged_examples: list[dict] = []
    for item in latest_signals:
        tags = [str(tag).strip() for tag in (item.get("risk_flags") or item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        tagged_examples.append(
            {
                "ticker": item.get("ticker"),
                "tags": tags[:2],
                "signal_strength": int(item.get("signal_strength") or 0),
            }
        )
    tagged_examples.sort(key=lambda entry: (-entry["signal_strength"], str(entry.get("ticker") or "")))
    return {
        "market_distribution": [],
        "top_concepts": [],
        "sector_heatmap": [],
        "concept_tracker": [],
        "continuous_leaders": [],
        "risk_overview": {
            "tagged_names": len(tagged_examples),
            "top_tags": [
                {"tag": tag, "count": count}
                for tag, count in sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
            ],
            "examples": tagged_examples[:3],
        },
        "resonance_score": 0.0,
        "tracked_signal_count": len(latest_signals),
    }


def _load_home_summary(db: Session, *, lookback_runs: int = 5) -> dict:
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    cache_key = json.dumps({"lookback_runs": lookback_runs, "kind": "home"}, sort_keys=True, ensure_ascii=False)

    def _load() -> dict:
        return load_dashboard_summary(
            db,
            lookback_runs=lookback_runs,
            market_context_loader=_lightweight_market_context,
        )

    return get_or_set("dashboard_home_summary_bundle", cache_key, ttl_seconds=60.0, loader=_load)


def _load_ops_summary(db: Session) -> dict:
    def _load() -> dict:
        sync_repo = PriceSyncStateRepository(db)
        model_repo = ModelRunRepository(db)
        backtest_repo = BacktestRepository(db)
        job_repo = DataJobRepository(db)
        return {
            "generated_at": datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat(),
            "auto_analysis": auto_analysis_service.get_status(db=db),
            "latest_model": model_repo.get_latest_run_summary() or {},
            "recent_model_runs": model_repo.list_recent_runs(limit=8),
            "latest_backtest": backtest_repo.get_latest_backtest_summary() or {},
            "recent_jobs": job_repo.list_recent_jobs(limit=8),
            "sync_overview": sync_repo.get_status_overview(),
            "recent_sync_states": sync_repo.list_recent_states_with_symbols(limit=5),
        }

    return get_or_set("dashboard_ops_summary_bundle", "latest", ttl_seconds=30.0, loader=_load)


def _load_cached_ai_daily_report(db: Session) -> dict:
    return get_or_set(
        "dashboard_ai_daily_report",
        "latest",
        ttl_seconds=45.0,
        loader=lambda: load_ai_daily_report(db=db) or {},
    )


def _display_time(value: str | None, *, with_tz: bool = False) -> str:
    return format_app_datetime(value, with_tz=with_tz)


def _report_outcome_rows(report: dict, *, report_date: str | None) -> list[dict]:
    rows = report.get("market_recommendations") or report.get("rows") or []
    outcome_rows: list[dict] = []
    for item in rows[:5]:
        ticker = str(item.get("ticker") or "").strip().upper()
        market = str(item.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US")
        history = load_lake_price_history(market=market, ticker=ticker, limit=260)
        baseline = None
        if report_date:
            prior_or_same = [row for row in history if str(row.get("date") or "") <= str(report_date)]
            if prior_or_same:
                baseline = prior_or_same[-1]
        if baseline is None and history:
            baseline = history[0]
        latest = history[-1] if history else None
        try:
            baseline_close = float((baseline or {}).get("close"))
        except (TypeError, ValueError):
            baseline_close = None
        try:
            latest_close = float((latest or {}).get("close"))
        except (TypeError, ValueError):
            latest_close = None
        baseline_date = str((baseline or {}).get("date") or "-")
        latest_date = str((latest or {}).get("date") or "-")
        return_pct = None
        status = "pending"
        if baseline_close and latest_close and latest_date > baseline_date:
            return_pct = (latest_close / baseline_close - 1.0) * 100.0
            if return_pct >= 3:
                status = "hit"
            elif return_pct <= -3:
                status = "miss"
            else:
                status = "watch"
        outcome_rows.append(
            {
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "market": market,
                "baseline_date": baseline_date,
                "baseline_close": baseline_close,
                "latest_date": latest_date,
                "latest_close": latest_close,
                "return_pct": return_pct,
                "status": status,
            }
        )
    return outcome_rows


def _report_outcome_summary(outcome_rows: list[dict], *, lang: str) -> str:
    measured = [row for row in outcome_rows if row.get("return_pct") is not None]
    if not measured:
        return "暂无后续交易日价格，先保留待观察。" if lang == "zh" else "No later trading-day prices yet; keep this report pending."
    avg_return = sum(float(row.get("return_pct") or 0.0) for row in measured) / len(measured)
    hit_count = sum(1 for row in measured if float(row.get("return_pct") or 0.0) > 0)
    if lang == "zh":
        return f"已可验证 {len(measured)} 只，平均收益 {avg_return:.2f}%，上涨命中 {hit_count}/{len(measured)}。"
    return f"{len(measured)} names are measurable, average return {avg_return:.2f}%, positive hits {hit_count}/{len(measured)}."


def _fmt_optional_float(value: object, *, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _outcome_status_label(status: str | None, *, lang: str) -> str:
    normalized = str(status or "").lower()
    if lang == "zh":
        return {
            "pending": "待观察",
            "hit": "命中",
            "miss": "失效",
            "watch": "观察",
        }.get(normalized, "-")
    return {
        "pending": "Pending",
        "hit": "Hit",
        "miss": "Miss",
        "watch": "Watch",
    }.get(normalized, "-")


def _dashboard_home_signal(score: float | None, lang: str) -> tuple[str, str]:
    label = build_signal_label(score, lang=lang) or ("观察" if lang == "zh" else "Watch")
    normalized = str(label).strip().lower()
    if normalized in {"buy", "买入"}:
        return label, "sig-buy"
    if normalized in {"sell", "卖出"}:
        return label, "sig-sell"
    if normalized in {"watch", "观察"}:
        return label, "sig-watch"
    return label, "sig-hold"


def _dashboard_home_watchlist_rows(db: Session, *, lang: str, session_mode: str) -> list[dict]:
    watchlist_repo = WatchlistRepository(db)
    prediction_repo = PredictionRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    items = watchlist_repo.list_items(watchlist.id)
    tickers = [item["ticker"] for item in items]
    outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)
    ranked: list[dict] = []
    for item in items:
        model_output = outputs.get(item["ticker"]) or {}
        score = model_output.get("score")
        confidence = int(model_output.get("confidence") or model_confidence(score) or 0)
        label, tone = _dashboard_home_signal(score, lang)
        decision = str(label).upper()
        mode_rank = confidence * 2 + int(round(float(score or 0.0) * 100))
        if session_mode == "postmarket":
            mode_rank += int(round(float(score or 0.0) * 100))
        ranked.append(
            {
                "ticker": item["ticker"],
                "name": item.get("name") or item["ticker"],
                "market": item.get("market") or "-",
                "score": float(score or 0.0),
                "confidence": confidence,
                "decision": decision,
                "signal_label": label,
                "signal_tone": tone,
                "mode_rank": mode_rank,
            }
        )
    ranked.sort(key=lambda item: (-item["mode_rank"], item["ticker"]))
    return ranked[:8]


def _dashboard_home_portfolio_rows(db: Session, *, lang: str) -> tuple[list[dict], dict]:
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    rows: list[dict] = []
    total_market_value = 0.0
    total_cost = 0.0
    for item in load_portfolio_positions():
        overview = symbol_repo.get_overview(item["ticker"]) or {
            "ticker": item["ticker"],
            "name": item.get("name"),
            "market": item.get("market"),
        }
        latest_signal = None
        predictions = prediction_repo.list_symbol_predictions(item["ticker"], limit=1, latest_run_only=True)
        if predictions:
            latest_signal = predictions[0]
        latest_price = float(load_latest_close(item["ticker"]) or 0.0)
        quantity = float(item.get("quantity") or 0.0)
        cost_basis = float(item.get("cost_basis") or 0.0)
        market_value = latest_price * quantity
        cost_value = cost_basis * quantity
        pnl = market_value - cost_value
        pnl_pct = ((latest_price / cost_basis) - 1.0) * 100 if cost_basis else 0.0
        total_market_value += market_value
        total_cost += cost_value
        signal_label, signal_tone = _dashboard_home_signal((latest_signal or {}).get("score"), lang)
        rows.append(
            {
                "ticker": item["ticker"],
                "name": overview.get("name") or item["ticker"],
                "market": overview.get("market") or item.get("market") or "-",
                "latest_price": latest_price,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "signal_label": signal_label,
                "signal_tone": signal_tone,
            }
        )
    rows.sort(key=lambda item: (-abs(item["market_value"]), item["ticker"]))
    totals = {
        "market_value": total_market_value,
        "cost": total_cost,
        "pnl": total_market_value - total_cost,
        "pnl_pct": ((total_market_value / total_cost) - 1.0) * 100 if total_cost else 0.0,
    }
    return rows[:8], totals


def _compact_label(value: str | None, limit: int = 28) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _compact_run_name(value: str | None, limit: int = 24) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    if "_" not in text:
        return _compact_label(text, limit=limit)
    parts = [part for part in text.split("_") if part]
    if len(parts) >= 3:
        prefix = "_".join(parts[:2])
        suffix = parts[-1]
        compact = f"{prefix}…{suffix}"
        if len(compact) <= limit:
            return compact
    return _compact_label(text, limit=limit)


def _compact_job_type(value: str | None, limit: int = 22) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    aliases = {
        "watchlist_auto_analysis": "watchlist_analysis",
        "cn_close_review": "close_review",
        "sync_cn_symbol_universe": "cn_universe_sync",
        "init_cn_market_data": "cn_market_init",
        "refresh_cn_market_data": "cn_market_refresh",
        "rebuild_technical_snapshots": "tech_snapshots",
    }
    text = aliases.get(text, text)
    return _compact_run_name(text, limit=limit)


def _compact_json_summary(value: object, limit: int = 56) -> str:
    if value in (None, "", {}):
        return "-"
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return _compact_label(text, limit=limit)


def _latest_cn_refresh_summary(db: Session, recent_jobs: list[dict] | None = None, *, lang: str = "zh") -> dict:
    jobs = recent_jobs or DataJobRepository(db).list_recent_jobs(limit=10)
    refresh_job = next(
        (
            item
            for item in jobs
            if str(item.get("job_type") or "").lower() == "cn_close_review"
            and str(item.get("status") or "").lower() == "success"
        ),
        None,
    )
    cn_total = len([symbol for symbol in SymbolRepository(db).list_symbols() if (symbol.market or "").upper() == "CN"])
    message = str((refresh_job or {}).get("message") or "")
    match = re.search(r"light CN refresh\s+(\d+)\s+symbol", message, re.IGNORECASE)
    refreshed = int(match.group(1)) if match else None
    summary = f"{refreshed}/{cn_total}" if refreshed is not None and cn_total else (str(refreshed) if refreshed is not None else "-")
    if refreshed is not None:
        label = f"本轮刷新 {refreshed}/{cn_total} 只 A 股" if lang == "zh" else f"Refreshed {refreshed}/{cn_total} CN symbols"
    else:
        label = "暂无全市场刷新结果" if lang == "zh" else "No CN refresh result yet"
    return {
        "job": refresh_job,
        "refreshed": refreshed,
        "total": cn_total,
        "summary": summary,
        "label": label,
    }


def _payload_rows(snapshot: dict | None) -> list[dict]:
    payload = (snapshot or {}).get("payload")
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else []


def _render_dashboard_workspace(
    *,
    lang: str,
    session_mode: str,
    lookback_runs: int,
    summary: dict,
    watchlist_rows: list[dict],
    portfolio_rows: list[dict],
    model_candidate_rows: list[dict],
    portfolio_totals: dict,
    portfolio_meta: dict,
    pipeline_payload: dict,
    recent_jobs: list[dict],
    banner_html: str,
    nlp_payload: dict,
) -> str:
    generated_at = summary["generated_at"]
    auto_analysis = summary["auto_analysis"]
    market_context = summary["market_context"]
    latest_model = summary["latest_model"] or {}
    top_signals = model_candidate_rows or (summary["latest_signals"] or [])[:5]
    risk_overview = market_context.get("risk_overview", {})
    risk_tags = risk_overview.get("top_tags") or []
    lead_text = (
        "把自选、持仓、模型结果和自动任务放回同一个主工作台。"
        if lang == "zh"
        else "Bring watchlist, portfolio, model output, and automated jobs into one workspace."
    )
    nav_html = render_workspace_nav_html(lang=lang, active_key="home", lookback_runs=lookback_runs)
    watchlist_html = "".join(
        "<article class='list-row'>"
        f"<div><a class='ticker' href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a><div class='subtle'>{item['name']} · {item['market']}</div></div>"
        f"<div class='row-right'><span class='signal {item['signal_tone']}'>{item['signal_label']}</span><div class='mini-metric'>{item['confidence']}%</div></div>"
        "</article>"
        for item in watchlist_rows
    ) or f"<div class='empty'>{'还没有自选股' if lang == 'zh' else 'No watchlist names yet'}</div>"
    portfolio_html = "".join(
        "<article class='list-row'>"
        f"<div><a class='ticker' href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a><div class='subtle'>{item['name']} · {item['market']}</div></div>"
        f"<div class='row-right'><div class='mini-metric {'neg' if item['pnl'] < 0 else 'pos'}'>{item['pnl_pct']:.1f}%</div><span class='signal {item['signal_tone']}'>{item['signal_label']}</span></div>"
        "</article>"
        for item in portfolio_rows
    ) or f"<div class='empty'>{'还没有持仓' if lang == 'zh' else 'No positions yet'}</div>"
    top_signal_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('trade_date') or '-'}</div><div class='subtle'>{_compact_label(item.get('reason_summary'), 72) if item.get('reason_summary') else (item.get('name') or '-')}</div></div>"
        f"<div class='row-right'><span class='signal {item.get('signal_tone') or _dashboard_home_signal(item.get('score'), lang)[1]}'>{item.get('signal_label') or _dashboard_home_signal(item.get('score'), lang)[0]}</span></div>"
        "</article>"
        for item in top_signals
    ) or f"<div class='empty'>{'暂无模型结果' if lang == 'zh' else 'No model output yet'}</div>"
    recent_jobs_html = "".join(
        "<article class='job-row'>"
        f"<div><div class='job-type' title='{item.get('job_type') or '-'}'>{_compact_job_type(item.get('job_type'), 20) or '-'}</div><div class='subtle'>{_display_time(item.get('started_at') or item.get('created_at'))}</div></div>"
        f"<div class='job-status {str(item.get('status') or '').lower()}'>{item.get('status') or '-'}</div>"
        "</article>"
        for item in recent_jobs[:5]
    ) or f"<div class='empty'>{'暂无任务记录' if lang == 'zh' else 'No jobs yet'}</div>"
    action_focus_rows = (portfolio_meta or {}).get("watch_items") or []
    signal_sets = _dashboard_signal_action_sets(summary.get("latest_signals") or [])
    regime_view = _dashboard_trading_regime(
        latest_signals=summary.get("latest_signals") or [],
        risk_overview=risk_overview,
        lang=lang,
    )
    close_review_action_feed = (pipeline_payload or {}).get("close_review_action_feed") if isinstance(pipeline_payload, dict) else None
    if not isinstance(close_review_action_feed, dict):
        close_review_action_feed = build_close_review_action_feed(_load_cached_ai_daily_report(db), lang=lang)
    action_queue_count = len(signal_sets["actionable"])
    risk_reduction_count = len(signal_sets["trim_review"]) + len(action_focus_rows[:3])
    blocked_count = len(signal_sets["blocked"])
    action_focus_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('action_reason') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal sig-watch'>{item.get('action_priority') or '-'}</span><div class='mini-metric'>{item.get('action_hint') or '-'}</div></div>"
        "</article>"
        for item in action_focus_rows[:3]
    ) or f"<div class='empty'>{'暂无动作焦点' if lang == 'zh' else 'No action focus yet'}</div>"
    actionable_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('execution_note') or item.get('entry_trigger') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='mini-metric'>{(str(item.get('target_weight_pct')) + '%') if item.get('target_weight_pct') is not None else '-'}</div></div>"
        "</article>"
        for item in signal_sets["actionable"][:3]
    ) or f"<div class='empty'>{'暂无可执行候选' if lang == 'zh' else 'No actionable candidates yet'}</div>"
    blocked_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('block_reason') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='mini-metric'>{item.get('risk_flags_text')}</div></div>"
        "</article>"
        for item in signal_sets["blocked"][:3]
    ) or f"<div class='empty'>{'暂无受阻候选' if lang == 'zh' else 'No blocked candidates'}</div>"
    news_opportunities_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('summary_text') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal sig-buy'>{item.get('sentiment_label') or '-'}</span><div class='mini-metric'>{item.get('headline_count') or 0}</div></div>"
        "</article>"
        for item in (nlp_payload.get("opportunities") or [])[:3]
    ) or f"<div class='empty'>{'暂无新闻驱动机会' if lang == 'zh' else 'No news opportunities yet'}</div>"
    news_risks_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('summary_text') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal sig-sell'>{item.get('sentiment_label') or '-'}</span><div class='mini-metric'>{' / '.join(item.get('risk_tags') or []) or '-'}</div></div>"
        "</article>"
        for item in (nlp_payload.get("risks") or [])[:3]
    ) or f"<div class='empty'>{'暂无新闻风险提醒' if lang == 'zh' else 'No news risks yet'}</div>"
    close_review_action_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('execution_note') or item.get('entry_trigger') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal sig-buy'>{item.get('tradability_status') or '-'}</span><div class='mini-metric'>{item.get('target_weight') or '-'}</div></div>"
        "</article>"
        for item in (close_review_action_feed.get("actionable") or [])[:3]
    ) or f"<div class='empty'>{'暂无盘后可执行动作' if lang == 'zh' else 'No close-review actions yet'}</div>"
    pipeline_job_map = {
        "refresh": next((item for item in recent_jobs if str(item.get("job_type") or "").lower() == "cn_close_review"), None),
        "analysis": next((item for item in recent_jobs if str(item.get("job_type") or "").lower() == "watchlist_auto_analysis"), None),
    }
    pipeline_rows_html = "".join(
        "<article class='job-row'>"
        f"<div><div class='job-type'>{label}</div><div class='subtle'>{_display_time((job or {}).get('finished_at') or (job or {}).get('started_at'))}</div></div>"
        f"<div class='job-status {str((job or {}).get('status') or 'unknown').lower()}'>{(job or {}).get('status') or ('unknown' if lang == 'en' else '未知')}</div>"
        "</article>"
        for label, job in (
            (("收盘刷新与快照" if lang == "zh" else "Close Review Refresh"), pipeline_job_map["refresh"]),
            (("自动分析与训练" if lang == "zh" else "Auto Analysis and Train"), pipeline_job_map["analysis"]),
        )
    )
    risk_tags_html = "".join(f"<span class='chip'>{tag}</span>" for tag in risk_tags[:4]) or f"<span class='chip'>{'风险平稳' if lang == 'zh' else 'Risk stable'}</span>"
    latest_model_full_label = latest_model.get("name") or latest_model.get("model_type") or ("尚未训练" if lang == "zh" else "Not trained")
    latest_model_label = _compact_run_name(latest_model_full_label, limit=26)
    latest_model_time = _display_time(latest_model.get("finished_at") or latest_model.get("created_at"))
    latest_model_status = latest_model.get("status") or ("unknown" if lang == "en" else "未知")
    trust_score = int((pipeline_payload or {}).get("trust_score") or 0)
    if lang == "zh":
        trust_label = "可信度较高" if trust_score >= 75 else ("需要人工复核" if trust_score < 55 else "可用但建议复核")
        action_mix_text = f"高 {((portfolio_meta or {}).get('action_mix') or {}).get('high', 0)} / 中 {((portfolio_meta or {}).get('action_mix') or {}).get('medium', 0)} / 低 {((portfolio_meta or {}).get('action_mix') or {}).get('low', 0)}"
    else:
        trust_label = "Higher trust" if trust_score >= 75 else ("Needs review" if trust_score < 55 else "Usable with review")
        action_mix_text = f"H {((portfolio_meta or {}).get('action_mix') or {}).get('high', 0)} / M {((portfolio_meta or {}).get('action_mix') or {}).get('medium', 0)} / L {((portfolio_meta or {}).get('action_mix') or {}).get('low', 0)}"
    exposure_text = f"{(portfolio_meta or {}).get('top_sector') or '-'} · {(portfolio_meta or {}).get('concentration_pct') or 0}%"
    auto_status_label = auto_analysis.get("status") or ("running" if auto_analysis.get("enabled") else "idle")
    auto_status_text = (
        "自动任务会把结果直接回流到这里。"
        if lang == "zh"
        else "Automated jobs should flow their output back here."
    )
    lang_toggle = (
        f"<a class='top-pill' href='/dashboard?lang=en&mode={session_mode}&lookback_runs={lookback_runs}'>EN</a>"
        f"<a class='top-pill' href='/dashboard?lang=zh&mode={session_mode}&lookback_runs={lookback_runs}'>中文</a>"
    )
    mode_toggle = "".join(
        f"<a class='top-pill{' active' if value == session_mode else ''}' href='/dashboard?lang={lang}&mode={value}&lookback_runs={lookback_runs}'>{label}</a>"
        for value, label in (
            ("premarket", "盘前" if lang == "zh" else "Premarket"),
            ("monitor", "盘中" if lang == "zh" else "Monitor"),
            ("postmarket", "盘后" if lang == "zh" else "Postmarket"),
        )
    )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'PQW 工作台' if lang == 'zh' else 'PQW Workspace'}</title>
        <style>
          :root {{
            --bg:#071018;
            --bg-soft:#0d1722;
            --panel:#111c28;
            --panel-2:#152231;
            --panel-3:#1a2a3c;
            --ink:#e6edf3;
            --muted:#90a3b8;
            --line:#223246;
            --accent:#3dd9b6;
            --accent-2:#52a8ff;
            --danger:#ff6b81;
            --warn:#f6c85f;
            --good:#4ade80;
          }}
          * {{ box-sizing:border-box; }}
          body {{
            margin:0;
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color:var(--ink);
            background:
              radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),
              radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),
              linear-gradient(180deg, #08111a 0%, #071018 100%);
          }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns: 280px minmax(0, 1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .brand {{ margin-bottom:28px; }}
          .content {{ padding:28px; }}
          .topbar {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; flex-wrap:wrap; margin-bottom:20px; }}
          .hero h2 {{ margin:0 0 10px; font-size:38px; line-height:1.02; max-width:760px; }}
          .hero p {{ margin:0; color:var(--muted); font-size:15px; max-width:720px; }}
          .top-actions {{ display:flex; gap:10px; flex-wrap:wrap; }}
          .top-pill {{
            display:inline-flex; align-items:center; justify-content:center;
            min-height:38px; padding:0 14px; border-radius:999px; border:1px solid var(--line);
            background:rgba(17,28,40,0.72); color:var(--muted); font-weight:700; font-size:13px;
          }}
          .top-pill.active {{ color:var(--ink); border-color:rgba(82,168,255,0.35); background:rgba(82,168,255,0.16); }}
          .banner {{ margin-bottom:18px; padding:14px 16px; border-radius:16px; background:#172534; border:1px solid var(--line); }}
          .summary-grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); margin-bottom:18px; }}
          .card {{
            background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98));
            border:1px solid var(--line);
            border-radius:22px;
            padding:18px;
            box-shadow:0 24px 48px rgba(0,0,0,0.18);
          }}
          .eyebrow {{ display:inline-flex; margin-bottom:10px; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.10); color:var(--accent); font-size:11px; font-weight:800; letter-spacing:0.05em; text-transform:uppercase; }}
          .metric {{ font-size:30px; font-weight:800; line-height:1; margin:0 0 8px; }}
          .metric.metric-compact {{ font-size:18px; line-height:1.25; word-break:break-word; overflow-wrap:anywhere; }}
          .muted {{ color:var(--muted); font-size:13px; line-height:1.5; }}
          .workspace {{ display:grid; gap:18px; grid-template-columns:minmax(0, 1.35fr) minmax(340px, 0.8fr); }}
          .stack {{ display:grid; gap:18px; }}
          .panel-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:14px; }}
          .panel-head h3 {{ margin:0; font-size:22px; }}
          .panel-head p {{ margin:6px 0 0; color:var(--muted); font-size:13px; }}
          .list-stack {{ display:grid; gap:10px; }}
          .list-row, .signal-row, .job-row {{
            display:flex; justify-content:space-between; gap:12px; align-items:center;
            padding:14px; border-radius:16px; background:rgba(11,19,29,0.82); border:1px solid rgba(34,50,70,0.92);
          }}
          .row-right {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }}
          .ticker {{ font-weight:800; font-size:15px; }}
          .subtle {{ color:var(--muted); font-size:12px; margin-top:4px; }}
          .signal {{ display:inline-flex; align-items:center; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; }}
          .sig-buy {{ background:rgba(74,222,128,0.14); color:#8af0a6; }}
          .sig-sell {{ background:rgba(255,107,129,0.14); color:#ff93a4; }}
          .sig-watch {{ background:rgba(82,168,255,0.14); color:#89c2ff; }}
          .sig-hold {{ background:rgba(246,200,95,0.14); color:#ffd982; }}
          .mini-metric {{ font-weight:800; font-size:13px; color:var(--ink); }}
          .mini-metric.pos {{ color:#8af0a6; }}
          .mini-metric.neg {{ color:#ff93a4; }}
          .chip-row {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }}
          .chip {{ display:inline-flex; align-items:center; padding:7px 10px; border-radius:999px; background:rgba(82,168,255,0.10); border:1px solid rgba(82,168,255,0.18); color:#9acbff; font-size:12px; font-weight:700; }}
          .cta-row {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
          .cta {{
            display:inline-flex; align-items:center; justify-content:center;
            min-height:40px; padding:0 14px; border-radius:14px; font-weight:800; font-size:13px;
            border:1px solid var(--line); background:rgba(17,28,40,0.8);
          }}
          .cta.primary {{ background:linear-gradient(180deg, rgba(61,217,182,0.26), rgba(61,217,182,0.14)); border-color:rgba(61,217,182,0.28); }}
          .job-status {{ padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; text-transform:uppercase; }}
          .job-status.success {{ background:rgba(74,222,128,0.14); color:#8af0a6; }}
          .job-status.failed {{ background:rgba(255,107,129,0.14); color:#ff93a4; }}
          .job-status.partial {{ background:rgba(246,200,95,0.14); color:#ffd982; }}
          .job-status.running {{ background:rgba(82,168,255,0.14); color:#89c2ff; }}
          .job-status.unknown {{ background:rgba(144,163,184,0.14); color:#c0cfde; }}
          .job-type {{ font-weight:700; font-size:13px; }}
          .empty {{ padding:18px; border-radius:16px; background:rgba(11,19,29,0.65); border:1px dashed var(--line); color:var(--muted); font-size:13px; }}
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
            .workspace, .summary-grid {{ grid-template-columns:1fr; }}
          }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'量化工作台' if lang == 'zh' else 'Trading Workspace'}</h1>
              <p>{lead_text}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">
              <div class="eyebrow">{'自动化' if lang == 'zh' else 'Automation'}</div>
              <div class="metric" style="font-size:20px;margin-bottom:6px;">{auto_status_label}</div>
              <div class="muted">{auto_status_text}</div>
              <div class="chip-row">
                <span class="chip">{'模式' if lang == 'zh' else 'Mode'}: {session_mode}</span>
                <span class="chip">{'更新' if lang == 'zh' else 'Updated'}: {generated_at}</span>
              </div>
            </div>
          </aside>
          <main class="content">
            {banner_html}
            <section class="topbar">
              <div class="hero">
                <h2>{'先看自选和持仓，再进入模型选股与任务中心。' if lang == 'zh' else 'Start with watchlist and positions, then move into model picks and jobs.'}</h2>
                <p>{lead_text}</p>
              </div>
              <div class="top-actions">
                {mode_toggle}
                {lang_toggle}
              </div>
            </section>

            <section class="summary-grid">
              <article class="card">
                <div class="eyebrow">{'Trading Regime' if lang == 'en' else '交易节奏'}</div>
                <div class="metric">{regime_view['label']}</div>
                <div class="muted">{regime_view['detail']}</div>
                <div class="chip-row"><span class="chip">{'模型可信度' if lang == 'zh' else 'Model trust'}: {trust_score}</span></div>
              </article>
              <article class="card">
                <div class="eyebrow">{'Action Queue' if lang == 'en' else '行动队列'}</div>
                <div class="metric">{action_queue_count}</div>
                <div class="muted">{'今天优先看的可执行候选。' if lang == 'zh' else 'Ready-to-trade names that deserve attention first.'}</div>
              </article>
              <article class="card">
                <div class="eyebrow">{'Risk Reduction' if lang == 'en' else '减风险队列'}</div>
                <div class="metric">{risk_reduction_count}</div>
                <div class="muted">{'先减谁、先复核谁，应该在这里看。' if lang == 'zh' else 'This is the queue for trims and review-first names.'}</div>
              </article>
              <article class="card">
                <div class="eyebrow">{'Blocked Candidates' if lang == 'en' else '受阻候选'}</div>
                <div class="metric">{blocked_count}</div>
                <div class="muted">{'这些名字今天不适合直接做。' if lang == 'zh' else 'These names should not be traded directly today.'}</div>
                <div class="chip-row"><span class="chip">{'暴露' if lang == 'zh' else 'Exposure'}: {exposure_text}</span></div>
              </article>
              <article class="card">
                <div class="eyebrow">{'Latest Model' if lang == 'en' else '最新模型'}</div>
                <div class="metric metric-compact" title="{latest_model_full_label}">{latest_model_label}</div>
                <div class="muted">{latest_model_time}</div>
                <div class="chip-row"><span class="chip">{'状态' if lang == 'zh' else 'Status'}: {latest_model_status}</span><span class="chip">{trust_label}</span></div>
              </article>
            </section>

            <section class="workspace">
              <div class="stack">
                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'今日首页' if lang == 'zh' else 'Home Board'}</div>
                      <h3>{'自选股票' if lang == 'zh' else 'Watchlist'}</h3>
                      <p>{'把最该看的股票直接放在第一屏。' if lang == 'zh' else 'Keep the most relevant names in the first screenful.'}</p>
                    </div>
                    <a class="cta" href="/watchlist?lang={lang}&mode={session_mode}">{'打开完整自选' if lang == 'zh' else 'Open watchlist'}</a>
                  </div>
                  <div class="list-stack">{watchlist_html}</div>
                </article>

                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'持仓总览' if lang == 'zh' else 'Portfolio'}</div>
                      <h3>{'持仓股票' if lang == 'zh' else 'Positions'}</h3>
                      <p>{'把盈亏、风险态度和关注顺序放在一起。' if lang == 'zh' else 'Show PnL, posture, and review priority together.'}</p>
                    </div>
                    <a class="cta" href="/portfolio">{'打开持仓页' if lang == 'zh' else 'Open portfolio'}</a>
                  </div>
                  <div class="muted" style="margin-bottom:12px;">{(portfolio_meta or {}).get('risk_summary') or ('先看组合暴露，再看单票盈亏。' if lang == 'zh' else 'Check portfolio exposure before single-name PnL.')}</div>
                  <div class="list-stack">{portfolio_html}</div>
                </article>
              </div>

              <div class="stack">
                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'组合暴露' if lang == 'zh' else 'Exposure'}</div>
                      <h3>{'组合层先看什么' if lang == 'zh' else 'Portfolio-level first look'}</h3>
                      <p>{'先确认行业集中度和动作优先级，再看单票明细。' if lang == 'zh' else 'Confirm concentration and action priority before drilling into single names.'}</p>
                    </div>
                  </div>
                  <div class="list-stack">
                    <article class="signal-row">
                      <div><div class="ticker">{'最大行业暴露' if lang == 'zh' else 'Top sector exposure'}</div><div class="subtle">{exposure_text}</div></div>
                      <div class="row-right"><div class="mini-metric">{(portfolio_meta or {}).get('top_market') or '-'}</div></div>
                    </article>
                    <article class="signal-row">
                      <div><div class="ticker">{'动作优先级分布' if lang == 'zh' else 'Action priority mix'}</div><div class="subtle">{'高优先级仓位越多，越需要人工复核。' if lang == 'zh' else 'More high-priority names means more manual review is needed.'}</div></div>
                      <div class="row-right"><div class="mini-metric">{action_mix_text}</div></div>
                    </article>
                  </div>
                </article>
                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'模型机会' if lang == 'zh' else 'Model Opportunities'}</div>
                      <h3>{'模型选股入口' if lang == 'zh' else 'Model Picks'}</h3>
                      <p>{'下一步不再先看一堆参数，而是先从模板进入。' if lang == 'zh' else 'Lead with templates first instead of a wall of parameters.'}</p>
                    </div>
                  </div>
                  <div class="list-stack">{top_signal_html}</div>
                  <div class="cta-row">
                    <a class="cta primary" href="/screeners?lang={lang}">{'进入模型选股' if lang == 'zh' else 'Open screeners'}</a>
                    <a class="cta" href="/dashboard/continuous-leaders?lang={lang}&lookback_runs={lookback_runs}">{'连续强势' if lang == 'zh' else 'Continuous leaders'}</a>
                  </div>
                </article>
                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'新闻驱动机会' if lang == 'zh' else 'News Opportunities'}</div>
                      <h3>{'新闻层面今天先看什么' if lang == 'zh' else 'What news suggests today'}</h3>
                      <p>{'这里读取收盘后新闻增强 job 的预存结果，不在首页做实时 NLP。' if lang == 'zh' else 'This reads precomputed post-close news results instead of running NLP live.'}</p>
                    </div>
                  </div>
                  <div class="list-stack">{news_opportunities_html}</div>
                </article>
                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'新闻风险提醒' if lang == 'zh' else 'News Risks'}</div>
                      <h3>{'先避开什么' if lang == 'zh' else 'What to avoid first'}</h3>
                      <p>{'这里汇总负面情绪和文本风险标签。' if lang == 'zh' else 'This summarizes negative tone and text risk tags.'}</p>
                    </div>
                  </div>
                  <div class="list-stack">{news_risks_html}</div>
                </article>

                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'盘后动作' if lang == 'zh' else 'Close Review Actions'}</div>
                      <h3>{'收盘复盘后先做什么' if lang == 'zh' else 'What to do after close review'}</h3>
                      <p>{close_review_action_feed.get('summary') or ('把复盘结果直接翻译成动作。' if lang == 'zh' else 'Translate the close review directly into actions.')}</p>
                    </div>
                    <a class="cta" href="/dashboard/ai-daily-report">{'打开 AI 日报' if lang == 'zh' else 'Open AI report'}</a>
                  </div>
                  <div class="list-stack">{close_review_action_html}</div>
                </article>

                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'减风险队列' if lang == 'zh' else 'Risk Reduction Queue'}</div>
                      <h3>{'优先处理哪些仓位' if lang == 'zh' else 'Which positions need action first'}</h3>
                      <p>{'先看该减、该复核、以及偏离目标仓位的仓位。' if lang == 'zh' else 'Start with trims, review names, and holdings that are far from target weight.'}</p>
                    </div>
                    <a class="cta" href="/portfolio">{'打开持仓页' if lang == 'zh' else 'Open portfolio'}</a>
                  </div>
                  <div class="list-stack">{action_focus_html}</div>
                </article>

                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'任务中心' if lang == 'zh' else 'Jobs'}</div>
                      <h3>{'自动任务结果' if lang == 'zh' else 'Automated Results'}</h3>
                      <p>{'让 job 的结果自然回到首页，而不是让你自己去找。' if lang == 'zh' else 'Bring job outcomes back to the home screen.'}</p>
                    </div>
                    <a class="cta" href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}">{'打开任务中心' if lang == 'zh' else 'Open jobs'}</a>
                  </div>
                  <div class="list-stack" style="margin-bottom:12px;">{pipeline_rows_html}</div>
                  <div class="list-stack">{recent_jobs_html}</div>
                </article>

                <article class="card">
                  <div class="panel-head">
                    <div>
                      <div class="eyebrow">{'导航建议' if lang == 'zh' else 'Suggested Flow'}</div>
                      <h3>{'推荐使用路径' if lang == 'zh' else 'Suggested Path'}</h3>
                    </div>
                  </div>
                  <div class="cta-row">
                    <a class="cta primary" href="/watchlist?lang={lang}&mode={session_mode}">{'先看自选' if lang == 'zh' else 'Review watchlist'}</a>
                    <a class="cta" href="/portfolio">{'再看持仓' if lang == 'zh' else 'Review portfolio'}</a>
                    <a class="cta" href="/screeners?lang={lang}">{'再做模型选股' if lang == 'zh' else 'Run screeners'}</a>
                    <a class="cta" href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}">{'最后看自动任务' if lang == 'zh' else 'Check jobs last'}</a>
                  </div>
                </article>
              </div>
            </section>
          </main>
        </div>
      </body>
    </html>
    """


def _sparkline_svg(values: list[int]) -> str:
    if not values:
        return "<span class='muted'>-</span>"
    width = 108
    height = 32
    left_pad = 4
    right_pad = 4
    top_pad = 4
    bottom_pad = 4
    min_value = min(values)
    max_value = max(values)
    span = max(max_value - min_value, 1)
    step = (width - left_pad - right_pad) / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = left_pad + index * step
        y = top_pad + (height - top_pad - bottom_pad) * (1 - ((value - min_value) / span))
        points.append(f"{x:.2f},{y:.2f}")
    stroke = "#0f766e" if values[-1] >= values[0] else "#b91c1c"
    return (
        f"<svg viewBox='0 0 {width} {height}' width='108' height='32' aria-label='trend sparkline'>"
        f"<rect x='0' y='0' width='{width}' height='{height}' rx='8' fill='#f8faf7'></rect>"
        f"<polyline fill='none' stroke='{stroke}' stroke-width='2.5' points='{' '.join(points)}'></polyline>"
        f"<circle cx='{points[-1].split(',')[0]}' cy='{points[-1].split(',')[1]}' r='3' fill='{stroke}'></circle>"
        "</svg>"
    )


def _price_sparkline_svg(values: list[float]) -> str:
    if not values:
        return "<span class='muted'>-</span>"
    width = 150
    height = 48
    left_pad = 6
    right_pad = 6
    top_pad = 6
    bottom_pad = 6
    min_value = min(values)
    max_value = max(values)
    span = max(max_value - min_value, 0.000001)
    step = (width - left_pad - right_pad) / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = left_pad + index * step
        y = top_pad + (height - top_pad - bottom_pad) * (1 - ((value - min_value) / span))
        points.append(f"{x:.2f},{y:.2f}")
    stroke = "#0f766e" if values[-1] >= values[0] else "#b91c1c"
    return (
        f"<svg viewBox='0 0 {width} {height}' width='150' height='48' aria-label='price sparkline'>"
        f"<rect x='0' y='0' width='{width}' height='{height}' rx='10' fill='#f8faf7'></rect>"
        f"<polyline fill='none' stroke='{stroke}' stroke-width='2.5' points='{' '.join(points)}'></polyline>"
        f"<circle cx='{points[-1].split(',')[0]}' cy='{points[-1].split(',')[1]}' r='3' fill='{stroke}'></circle>"
        "</svg>"
    )


def _score_sparkline_svg(values: list[float]) -> str:
    if not values:
        return "<span class='muted'>-</span>"
    width = 108
    height = 32
    left_pad = 4
    right_pad = 4
    top_pad = 4
    bottom_pad = 4
    min_value = min(values)
    max_value = max(values)
    span = max(max_value - min_value, 0.000001)
    step = (width - left_pad - right_pad) / max(len(values) - 1, 1)
    points = []
    for index, value in enumerate(values):
        x = left_pad + index * step
        y = top_pad + (height - top_pad - bottom_pad) * (1 - ((value - min_value) / span))
        points.append(f"{x:.2f},{y:.2f}")
    stroke = "#0f766e" if values[-1] >= values[0] else "#b91c1c"
    return (
        f"<svg viewBox='0 0 {width} {height}' width='108' height='32' aria-label='score sparkline'>"
        f"<rect x='0' y='0' width='{width}' height='{height}' rx='8' fill='#f8faf7'></rect>"
        f"<polyline fill='none' stroke='{stroke}' stroke-width='2.5' points='{' '.join(points)}'></polyline>"
        f"<circle cx='{points[-1].split(',')[0]}' cy='{points[-1].split(',')[1]}' r='3' fill='{stroke}'></circle>"
        "</svg>"
    )


def _mini_signal_direction(score: float | None) -> tuple[str, str] | None:
    if score is None:
        return None
    if score >= 0.18:
        return ("B", "#15803d")
    if score <= -0.05:
        return ("S", "#b91c1c")
    if score >= 0.05:
        return ("W", "#a16207")
    return None


def _price_signal_sparkline_svg(history_rows: list[dict], prediction_history: list[dict]) -> str:
    closes = [float(row["close"]) for row in history_rows if row.get("close") is not None]
    if not closes:
        return "<span class='muted'>-</span>"
    width = 150
    height = 56
    left_pad = 6
    right_pad = 6
    top_pad = 8
    bottom_pad = 8
    min_value = min(closes)
    max_value = max(closes)
    span = max(max_value - min_value, 0.000001)
    step = (width - left_pad - right_pad) / max(len(closes) - 1, 1)
    points = []
    point_meta = []
    for index, row in enumerate(history_rows):
        close_value = row.get("close")
        if close_value is None:
            continue
        x = left_pad + index * step
        y = top_pad + (height - top_pad - bottom_pad) * (1 - ((float(close_value) - min_value) / span))
        points.append(f"{x:.2f},{y:.2f}")
        point_meta.append((row.get("date"), x, y, row))
    stroke = "#0f766e" if closes[-1] >= closes[0] else "#b91c1c"
    signal_map = {row["trade_date"]: row for row in prediction_history if row.get("trade_date")}
    markers: list[str] = []
    hover_targets: list[str] = []
    for date_value, x, y, row in point_meta:
        signal = signal_map.get(date_value)
        marker = _mini_signal_direction(signal.get("score") if signal else None)
        signal_text = ""
        if signal:
            label, _ = marker if marker else ("", "")
            score_value = signal.get("score")
            signal_text = f" | {label} {float(score_value):.3f}" if score_value is not None and label else ""
        hover_targets.append(
            f"<circle cx='{x:.2f}' cy='{y:.2f}' r='7' fill='transparent'>"
            f"<title>{date_value} | Close {float(row['close']):.2f}{signal_text}</title>"
            "</circle>"
        )
        if not marker:
            continue
        label, color = marker
        marker_y = max(12.0, y - 10.0)
        markers.append(f"<circle cx='{x:.2f}' cy='{marker_y:.2f}' r='6' fill='{color}' opacity='0.95'></circle>")
        markers.append(f"<text x='{x:.2f}' y='{marker_y + 3:.2f}' text-anchor='middle' font-size='7.5' font-weight='800' fill='#fff'>{label}</text>")
    return (
        f"<svg viewBox='0 0 {width} {height}' width='150' height='56' aria-label='price signal sparkline'>"
        f"<rect x='0' y='0' width='{width}' height='{height}' rx='10' fill='#f8faf7'></rect>"
        f"<polyline fill='none' stroke='{stroke}' stroke-width='2.5' points='{' '.join(points)}'></polyline>"
        f"<circle cx='{points[-1].split(',')[0]}' cy='{points[-1].split(',')[1]}' r='3' fill='{stroke}'></circle>"
        f"{''.join(markers)}"
        f"{''.join(hover_targets)}"
        "</svg>"
    )


def _concept_slug(name: str) -> str:
    return (
        name.lower()
        .replace(" ", "-")
        .replace("/", "-")
        .replace("&", "and")
        .replace("__", "-")
    )


def _window_return_pct(history: list[dict], sessions: int) -> float | None:
    if len(history) < sessions + 1:
        return None
    start_close = history[-(sessions + 1)].get("close")
    end_close = history[-1].get("close")
    if start_close in (None, 0) or end_close is None:
        return None
    return round(((float(end_close) / float(start_close)) - 1.0) * 100.0, 2)


def _concept_price_strength(symbol_data_service: SymbolDataService, tickers: list[str]) -> dict:
    five_day_values: list[float] = []
    twenty_day_values: list[float] = []
    advancing = 0
    declining = 0
    for ticker in tickers:
        history = symbol_data_service.get_history(ticker, limit=25)
        move_5 = _window_return_pct(history, 5)
        move_20 = _window_return_pct(history, 20)
        if move_5 is not None:
            five_day_values.append(move_5)
            if move_5 > 0:
                advancing += 1
            elif move_5 < 0:
                declining += 1
        if move_20 is not None:
            twenty_day_values.append(move_20)
    breadth_base = advancing + declining
    breadth = round((advancing / breadth_base) * 100.0, 1) if breadth_base else None
    return {
        "avg_move_5d": round(sum(five_day_values) / len(five_day_values), 2) if five_day_values else None,
        "avg_move_20d": round(sum(twenty_day_values) / len(twenty_day_values), 2) if twenty_day_values else None,
        "breadth_pct": breadth,
    }


def _build_market_context(db: Session, latest_signals: list[dict], *, lookback_runs: int = 5) -> dict:
    signature = [
        {
            "ticker": item.get("ticker"),
            "trade_date": item.get("trade_date"),
            "score": item.get("score"),
        }
        for item in latest_signals[:20]
    ]
    cache_key = json.dumps({"lookback_runs": lookback_runs, "signals": signature}, sort_keys=True, ensure_ascii=False)

    def _load() -> dict:
        tickers = [item["ticker"] for item in latest_signals if item.get("ticker")]
        symbol_repo = SymbolRepository(db)
        concept_repo = ConceptSnapshotRepository(db)
        signal_repo = PredictionRepository(db)
        trade_plan_repo = PredictionTradePlanRepository(db)
        symbol_data_service = SymbolDataService()

        market_counts: dict[str, int] = {}
        for ticker in tickers:
            symbol = symbol_repo.get_by_ticker(ticker)
            market = (symbol.market if symbol and symbol.market else "OTHER").upper()
            market_counts[market] = market_counts.get(market, 0) + 1

        market_distribution = [
            {"market": market, "count": count}
            for market, count in sorted(market_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]

        concept_rows = concept_repo.list_latest_for_tickers(tickers)
        model_meta_cache: dict[str, dict] = {}

        def model_meta_for_ticker(ticker: str, *, score: float | None = None) -> dict:
            inner_key = ticker.upper()
            if inner_key in model_meta_cache:
                return model_meta_cache[inner_key]
            detail = signal_repo.get_latest_model_output_for_ticker(ticker)
            if detail is None:
                detail = {"ticker": ticker, "score": score}
            elif detail.get("score") is None and score is not None:
                detail["score"] = score
            enriched = enrich_model_output(detail, lang="en") or {"score": score, "state": build_model_state(score, lang="en")}
            trade_plan = trade_plan_repo.get_latest_for_ticker(ticker) or {}
            model_meta_cache[inner_key] = {
                "state": enriched.get("state") or build_model_state(score, lang="en"),
                "confidence": enriched.get("confidence"),
                "summary_text": enriched.get("summary_text"),
                "bullish_prob": enriched.get("bullish_prob"),
                "bearish_prob": enriched.get("bearish_prob"),
                "risk_score": enriched.get("risk_score"),
                "regime_label": enriched.get("regime_label"),
                "conviction_bucket": enriched.get("conviction_bucket"),
                "position_size_hint": enriched.get("position_size_hint"),
                "entry_style": enriched.get("entry_style"),
                "signal_label": enriched.get("signal_label"),
                "signal_strength": enriched.get("signal_strength"),
                "percentile": enriched.get("percentile"),
                "target_horizon_days": enriched.get("target_horizon_days"),
                "expected_drawdown_20d": enriched.get("expected_drawdown_20d"),
                "model_reward_risk_ratio": enriched.get("model_reward_risk_ratio"),
                "execution_tags": list(trade_plan.get("execution_tags") or []),
            }
            return model_meta_cache[inner_key]

        def _top_execution_tags(ticker_details: list[dict]) -> list[str]:
            counts: dict[str, int] = {}
            for detail in ticker_details:
                for tag in detail.get("execution_tags") or []:
                    normalized = str(tag).strip()
                    if not normalized:
                        continue
                    counts[normalized] = counts.get(normalized, 0) + 1
            return [tag for tag, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))][:2]

        concept_map: dict[str, dict] = {}
        score_lookup = {item["ticker"]: float(item.get("score") or 0.0) for item in latest_signals}
        for row in concept_rows:
            name = row["concept_name"]
            item = concept_map.setdefault(
                name,
                {
                    "concept_name": name,
                    "concept_code": row.get("concept_code"),
                    "hits": 0,
                    "score_total": 0.0,
                    "tickers": [],
                    "ticker_details": [],
                    "as_of_date": row.get("as_of_date"),
                },
            )
            item["hits"] += 1
            item["score_total"] += score_lookup.get(row["ticker"], 0.0)
            if row["ticker"] not in item["tickers"]:
                item["tickers"].append(row["ticker"])
                item["ticker_details"].append(
                    {
                        "ticker": row["ticker"],
                        "name": row.get("name"),
                        "score": score_lookup.get(row["ticker"], 0.0),
                        **model_meta_for_ticker(row["ticker"], score=score_lookup.get(row["ticker"], 0.0)),
                    }
                )

        top_concepts = sorted(
            (
                {
                    "concept_name": value["concept_name"],
                    "concept_code": value["concept_code"],
                    "hits": value["hits"],
                    "avg_score": round(value["score_total"] / max(value["hits"], 1), 4),
                    "tickers": value["tickers"],
                    "ticker_details": sorted(value["ticker_details"], key=lambda detail: detail["score"], reverse=True),
                    "as_of_date": value["as_of_date"],
                    "max_signal_strength": max((int(detail.get("signal_strength") or 0) for detail in value["ticker_details"]), default=0),
                    "buy_signal_count": sum(
                        1 for detail in value["ticker_details"]
                        if str(detail.get("signal_label") or "").strip().upper() == "BUY"
                    ),
                    "execution_tags": _top_execution_tags(value["ticker_details"]),
                    **_concept_price_strength(symbol_data_service, value["tickers"]),
                }
                for value in concept_map.values()
            ),
            key=lambda item: (-item["hits"], -item["avg_score"], item["concept_name"]),
        )[:12]
        top_hits = top_concepts[0]["hits"] if top_concepts else 0
        resonance_score = round(top_hits / max(len(latest_signals), 1) * 100, 1) if latest_signals else 0.0

        sector_heatmap = []
        for item in top_concepts[:8]:
            move_boost = int(max(item.get("avg_move_5d") or 0.0, 0.0) * 3)
            breadth_boost = int(((item.get("breadth_pct") or 0.0) / 100.0) * 16)
            intensity = min(100, 20 + item["hits"] * 18 + int(max(item["avg_score"], 0) * 120) + move_boost + breadth_boost)
            sector_heatmap.append(
                {
                    "label": item["concept_name"],
                    "slug": _concept_slug(item["concept_name"]),
                    "hits": item["hits"],
                    "avg_score": item["avg_score"],
                    "avg_move_5d": item.get("avg_move_5d"),
                    "avg_move_20d": item.get("avg_move_20d"),
                    "breadth_pct": item.get("breadth_pct"),
                    "max_signal_strength": item.get("max_signal_strength"),
                    "buy_signal_count": item.get("buy_signal_count"),
                    "execution_tags": item.get("execution_tags", []),
                    "intensity": intensity,
                }
            )

        snapshots = signal_repo.list_recent_prediction_snapshots(top_n=10, limit_runs=max(lookback_runs, 2))
        concept_snapshots: list[dict] = []
        for snapshot in snapshots:
            snapshot_tickers = [item["ticker"] for item in snapshot["items"]]
            snapshot_rows = concept_repo.list_latest_for_tickers(snapshot_tickers)
            counts: dict[str, int] = {}
            for row in snapshot_rows:
                counts[row["concept_name"]] = counts.get(row["concept_name"], 0) + 1
            concept_snapshots.append({"trade_date": snapshot["trade_date"], "counts": counts})

        tracker_rows: list[dict] = []
        for item in top_concepts:
            current_hits = item["hits"]
            previous_hits = 0
            streak = 0
            history: list[int] = []
            for snapshot in concept_snapshots:
                count = snapshot["counts"].get(item["concept_name"], 0)
                history.append(count)
            if len(history) > 1:
                previous_hits = history[1]
            for count in history:
                if count > 0:
                    streak += 1
                else:
                    break
            tracker_rows.append(
                {
                    "concept_name": item["concept_name"],
                    "hits": current_hits,
                    "previous_hits": previous_hits,
                    "delta_hits": current_hits - previous_hits,
                    "streak": streak,
                    "avg_score": item["avg_score"],
                    "avg_move_5d": item.get("avg_move_5d"),
                    "avg_move_20d": item.get("avg_move_20d"),
                    "breadth_pct": item.get("breadth_pct"),
                    "max_signal_strength": item.get("max_signal_strength"),
                    "buy_signal_count": item.get("buy_signal_count"),
                    "execution_tags": item.get("execution_tags", []),
                    "tickers": item["tickers"],
                    "ticker_details": item["ticker_details"],
                    "history": history,
                    "slug": _concept_slug(item["concept_name"]),
                }
            )
        tracker_rows.sort(key=lambda row: (-row["delta_hits"], -row["hits"], row["concept_name"]))

        latest_signal_map = {item["ticker"]: item for item in latest_signals}
        continuous_leaders: list[dict] = []
        ticker_hit_counts: dict[str, int] = {}
        ticker_score_history: dict[str, list[float]] = {}
        for snapshot in snapshots:
            for item in snapshot["items"]:
                ticker = item["ticker"]
                ticker_hit_counts[ticker] = ticker_hit_counts.get(ticker, 0) + 1
                ticker_score_history.setdefault(ticker, []).append(float(item.get("score") or 0.0))

        for ticker, hits in ticker_hit_counts.items():
            if hits <= 0:
                continue
            symbol = symbol_repo.get_by_ticker(ticker)
            latest_signal = latest_signal_map.get(ticker, {})
            continuous_leaders.append(
                {
                    "ticker": ticker,
                    "name": (symbol.name if symbol and symbol.name else ticker),
                    "market": (symbol.market if symbol and symbol.market else "OTHER").upper(),
                    "hits": hits,
                    "runs": lookback_runs,
                    "score": round(float(latest_signal.get("score") or 0.0), 4),
                    "score_history": ticker_score_history.get(ticker, []),
                    "trade_date": latest_signal.get("trade_date"),
                    **model_meta_for_ticker(ticker, score=float(latest_signal.get("score") or 0.0)),
                }
            )
        continuous_leaders.sort(key=lambda item: (-item["hits"], -item["score"], item["ticker"]))

        risk_counts: dict[str, int] = {}
        tagged_examples: list[dict] = []
        seen_tickers: set[str] = set()
        combined_details: list[dict] = []
        for concept in top_concepts:
            combined_details.extend(concept.get("ticker_details") or [])
        combined_details.extend(continuous_leaders)
        for detail in combined_details:
            ticker = str(detail.get("ticker") or "").upper()
            if not ticker or ticker in seen_tickers:
                continue
            seen_tickers.add(ticker)
            tags = [str(tag).strip() for tag in (detail.get("execution_tags") or []) if str(tag).strip()]
            if not tags:
                continue
            for tag in tags:
                risk_counts[tag] = risk_counts.get(tag, 0) + 1
            tagged_examples.append(
                {
                    "ticker": detail.get("ticker"),
                    "tags": tags[:2],
                    "signal_strength": int(detail.get("signal_strength") or 0),
                }
            )
        tagged_examples.sort(key=lambda item: (-item["signal_strength"], item["ticker"] or ""))
        risk_overview = {
            "tagged_names": len(tagged_examples),
            "top_tags": [
                {"tag": tag, "count": count}
                for tag, count in sorted(risk_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
            ],
            "examples": tagged_examples[:3],
        }

        return {
            "market_distribution": market_distribution,
            "top_concepts": top_concepts,
            "sector_heatmap": sector_heatmap,
            "concept_tracker": tracker_rows[:12],
            "continuous_leaders": continuous_leaders[:10],
            "risk_overview": risk_overview,
            "resonance_score": resonance_score,
            "tracked_signal_count": len(latest_signals),
        }

    return get_or_set("dashboard_market_context", cache_key, ttl_seconds=60.0, loader=_load)


def _get_concept_from_summary(summary: dict, concept_slug: str) -> dict | None:
    return next(
        (item for item in (summary.get("market_context") or {}).get("concept_tracker", []) if item.get("slug") == concept_slug),
        None,
    )


def _ticker_links_html(tickers: list[str], *, lang: str, limit: int = 18) -> str:
    normalized = [str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()]
    if not normalized:
        return "-"
    links = [
        f"<a href='/insights/{ticker}?lang={lang}'>{ticker}</a>"
        for ticker in normalized[:limit]
    ]
    if len(normalized) > limit:
        links.append(f"<span class='muted'>+{len(normalized) - limit}</span>")
    return ", ".join(links)


def _enrich_heatmap_ticker_details(db: Session, ticker_details: list[dict], *, lang: str) -> list[dict]:
    tickers = [str(detail.get("ticker") or "").strip().upper() for detail in ticker_details if detail.get("ticker")]
    if not tickers:
        return []
    needs_lookup = [
        ticker
        for ticker, detail in zip(tickers, ticker_details)
        if detail.get("name") is None or detail.get("score") is None
    ]
    overviews = SymbolRepository(db).list_overviews_for_tickers(needs_lookup) if needs_lookup else {}
    latest_outputs = PredictionRepository(db).get_latest_model_outputs_for_tickers(needs_lookup) if needs_lookup else {}
    enriched_rows: list[dict] = []
    for raw_detail in ticker_details:
        ticker = str(raw_detail.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        latest = latest_outputs.get(ticker) or {}
        overview = overviews.get(ticker) or {}
        score = raw_detail.get("score")
        if score is None:
            score = latest.get("score")
        if score is None:
            score = raw_detail.get("trend_score")
        if score is None:
            score = 0.0
        enriched = enrich_model_output({**latest, "ticker": ticker, "score": score}, lang="en") if latest else {}
        signal_label = (
            raw_detail.get("signal_label")
            or latest.get("signal_label")
            or enriched.get("signal_label")
        )
        signal_strength = (
            raw_detail.get("signal_strength")
            if raw_detail.get("signal_strength") is not None
            else latest.get("signal_strength")
        )
        enriched_rows.append(
            {
                "ticker": ticker,
                "name": raw_detail.get("name") or overview.get("name") or latest.get("name") or ticker,
                "score": float(score or 0.0),
                "state": raw_detail.get("state") or enriched.get("state") or build_model_state(float(score or 0.0), lang="en"),
                "confidence": raw_detail.get("confidence") or latest.get("confidence") or enriched.get("confidence"),
                "percentile": raw_detail.get("percentile") or latest.get("percentile") or enriched.get("percentile"),
                "target_horizon_days": raw_detail.get("target_horizon_days") or latest.get("target_horizon_days") or enriched.get("target_horizon_days"),
                "model_reward_risk_ratio": raw_detail.get("model_reward_risk_ratio") or latest.get("model_reward_risk_ratio") or enriched.get("model_reward_risk_ratio"),
                "conviction_bucket": raw_detail.get("conviction_bucket") or latest.get("conviction_bucket") or enriched.get("conviction_bucket"),
                "position_size_hint": raw_detail.get("position_size_hint") or latest.get("position_size_hint") or enriched.get("position_size_hint"),
                "entry_style": raw_detail.get("entry_style") or latest.get("entry_style") or enriched.get("entry_style"),
                "signal_label": signal_label,
                "signal_strength": int(signal_strength or 0),
                "execution_tags": raw_detail.get("execution_tags") or latest.get("execution_tags") or enriched.get("execution_tags") or [],
            }
        )
    return sorted(enriched_rows, key=lambda detail: float(detail.get("score") or 0.0), reverse=True)


def _get_heatmap_concept_from_snapshot(db: Session, concept_slug: str, *, lang: str) -> dict | None:
    snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    payload = (snapshot or {}).get("payload") or {}
    heatmap_rows = payload.get("sector_heatmap") or []
    matched = next(
        (
            item
            for item in heatmap_rows
            if str(item.get("slug") or "") == concept_slug
            or _concept_slug(str(item.get("label") or "")) == concept_slug
            or str(item.get("label") or "") == concept_slug
        ),
        None,
    )
    if matched is None:
        return None
    ticker_details = _enrich_heatmap_ticker_details(db, matched.get("ticker_details") or [], lang=lang)
    tickers = [detail["ticker"] for detail in ticker_details]
    hits = int(matched.get("hits") or len(ticker_details) or 0)
    return {
        "concept_name": matched.get("label") or concept_slug,
        "concept_code": None,
        "slug": matched.get("slug") or concept_slug,
        "hits": hits,
        "previous_hits": 0,
        "delta_hits": 0,
        "streak": 1 if hits else 0,
        "history": [hits],
        "tickers": tickers,
        "ticker_details": ticker_details,
        "avg_score": float(matched.get("avg_score") or 0.0),
        "avg_move_5d": matched.get("avg_move_5d"),
        "avg_move_20d": matched.get("avg_move_20d"),
        "breadth_pct": matched.get("breadth_pct"),
        "buy_signal_count": int(matched.get("buy_signal_count") or 0),
        "max_signal_strength": int(matched.get("max_signal_strength") or 0),
        "execution_tags": matched.get("execution_tags") or [],
        "as_of_date": (snapshot or {}).get("created_at"),
        "source": "heatmap_snapshot",
    }


def _get_concept_for_detail(db: Session, summary: dict, concept_slug: str, *, lang: str) -> dict | None:
    concept = _get_concept_from_summary(summary, concept_slug)
    if concept is not None:
        return concept
    return _get_heatmap_concept_from_snapshot(db, concept_slug, lang=lang)


def _concept_tracker_rows_from_heatmap_snapshot(db: Session) -> list[dict]:
    snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    payload = (snapshot or {}).get("payload") or {}
    rows: list[dict] = []
    for item in payload.get("sector_heatmap") or []:
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        ticker_details = item.get("ticker_details") or []
        tickers = [str(detail.get("ticker") or "").strip().upper() for detail in ticker_details if detail.get("ticker")]
        hits = int(item.get("hits") or len(tickers) or 0)
        rows.append(
            {
                "concept_name": label,
                "concept_code": None,
                "slug": item.get("slug") or _concept_slug(label),
                "hits": hits,
                "previous_hits": 0,
                "delta_hits": 0,
                "streak": 1 if hits else 0,
                "history": [hits],
                "avg_move_5d": item.get("avg_move_5d"),
                "breadth_pct": item.get("breadth_pct"),
                "buy_signal_count": int(item.get("buy_signal_count") or 0),
                "max_signal_strength": int(item.get("max_signal_strength") or 0),
                "execution_tags": item.get("execution_tags") or [],
                "avg_score": float(item.get("avg_score") or 0.0),
                "tickers": tickers,
                "ticker_details": ticker_details,
            }
        )
    return rows


def _load_concept_tracker_rows(db: Session, *, lookback_runs: int) -> list[dict]:
    rows = _concept_tracker_rows_from_heatmap_snapshot(db)
    if rows:
        return rows
    summary = _load_summary(db, lookback_runs=lookback_runs)
    return list((summary.get("market_context") or {}).get("concept_tracker") or [])


def _dashboard_model_badge(state: dict | None, *, confidence: int | None = None, compact: bool = False) -> str:
    if not state:
        return ""
    confidence_html = ""
    if confidence is not None:
        confidence_html = f"<span style='opacity:0.78;margin-left:6px;'>{confidence}%</span>"
    padding = "4px 8px" if compact else "6px 10px"
    font_size = "11px" if compact else "12px"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:{padding};border-radius:999px;"
        f"background:{state['bg']};color:{state['fg']};font-size:{font_size};font-weight:800;white-space:nowrap;'>"
        f"{state['label']}{confidence_html}</span>"
    )


def _signal_pill(score: float | None, *, lang: str, strength: int | None = None, compact: bool = False) -> str:
    label = build_signal_label(score, lang=lang) or ("Hold" if lang == "en" else "持有")
    key = label.lower()
    bg = "#f3f4f6"
    fg = "#374151"
    if key in {"buy", "买点"}:
        bg, fg = "#dcfce7", "#166534"
    elif key in {"watch", "观察"}:
        bg, fg = "#fef3c7", "#92400e"
    elif key in {"sell", "卖点"}:
        bg, fg = "#fee2e2", "#991b1b"
    else:
        bg, fg = "#e5e7eb", "#374151"
    suffix = f" · {int(strength)}" if strength is not None else ""
    padding = "4px 8px" if compact else "6px 10px"
    font_size = "11px" if compact else "12px"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:{padding};border-radius:999px;"
        f"background:{bg};color:{fg};font-size:{font_size};font-weight:800;white-space:nowrap;'>{label}{suffix}</span>"
    )


def _add_concept_tickers_to_watchlist(
    *,
    db: Session,
    concept: dict,
    auto_enable_sync: bool = False,
) -> tuple[int, int, int]:
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    added = 0
    already_in_watchlist = 0
    sync_enabled_count = 0

    for detail in concept["ticker_details"]:
        ticker = detail["ticker"]
        existing = watchlist_map.get(ticker)
        if existing:
            already_in_watchlist += 1
            if auto_enable_sync and not existing.get("sync_enabled"):
                updated = watchlist_repo.set_sync_enabled(existing["item_id"], True)
                if updated is not None:
                    sync_enabled_count += 1
                    existing["sync_enabled"] = 1
            continue

        existing_symbol = symbol_repo.get_by_ticker(ticker)
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=detail.get("name"),
                market=existing_symbol.market if existing_symbol else None,
                exchange=existing_symbol.exchange if existing_symbol else None,
            )
        )
        watchlist_item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        if auto_enable_sync:
            watchlist_repo.set_sync_enabled(watchlist_item.id, True)
            sync_enabled_count += 1
        watchlist_map[ticker] = {
            "item_id": watchlist_item.id,
            "symbol_id": symbol.id,
            "ticker": ticker,
            "name": detail.get("name"),
            "market": symbol.market,
            "sync_enabled": 1 if auto_enable_sync else 0,
        }
        added += 1

    return added, already_in_watchlist, sync_enabled_count


def _add_specific_tickers_to_watchlist(
    *,
    db: Session,
    tickers: list[str],
    auto_enable_sync: bool = False,
) -> tuple[int, int, int]:
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    added = 0
    already_in_watchlist = 0
    sync_enabled_count = 0

    for ticker in tickers:
        existing = watchlist_map.get(ticker)
        if existing:
            already_in_watchlist += 1
            if auto_enable_sync and not existing.get("sync_enabled"):
                updated = watchlist_repo.set_sync_enabled(existing["item_id"], True)
                if updated is not None:
                    sync_enabled_count += 1
                    existing["sync_enabled"] = 1
            continue

        existing_symbol = symbol_repo.get_by_ticker(ticker)
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=existing_symbol.name if existing_symbol else ticker,
                market=existing_symbol.market if existing_symbol else None,
                exchange=existing_symbol.exchange if existing_symbol else None,
            )
        )
        watchlist_item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        if auto_enable_sync:
            watchlist_repo.set_sync_enabled(watchlist_item.id, True)
            sync_enabled_count += 1
        watchlist_map[ticker] = {
            "item_id": watchlist_item.id,
            "symbol_id": symbol.id,
            "ticker": ticker,
            "name": symbol.name,
            "market": symbol.market,
            "sync_enabled": 1 if auto_enable_sync else 0,
        }
        added += 1

    return added, already_in_watchlist, sync_enabled_count


def _concept_ticker_watch_state(watchlist_map: dict[str, dict], ticker: str, lang: str) -> tuple[str, str, str]:
    existing = watchlist_map.get(ticker)
    if not existing:
        return (_concept_tr(lang, "not_in_watchlist"), "#f3f4f6", "#6b7280")
    if existing.get("sync_enabled") and existing.get("sync_status") == "success":
        return (_concept_tr(lang, "ready"), "#dcfce7", "#166534")
    if existing.get("sync_enabled"):
        return (_concept_tr(lang, "waiting"), "#fef3c7", "#92400e")
    return (_concept_tr(lang, "in_watchlist"), "#eef8f5", "#0f766e")


def _concept_sort_rank(sort_by: str, detail: dict) -> tuple:
    if sort_by == "ticker":
        return (detail["ticker"],)
    if sort_by == "score":
        return (-float(detail.get("score") or 0.0), detail["ticker"])
    if sort_by == "name":
        return ((detail.get("name") or detail["ticker"]).lower(), detail["ticker"])
    if sort_by == "five_day":
        return (-float(detail.get("five_day_move") or -9999.0), detail["ticker"])
    if sort_by == "watchlist":
        return (-int(detail.get("watch_state_rank") or 0), detail["ticker"])
    if sort_by == "last_sync":
        return (detail.get("last_synced_date") or "", detail["ticker"])
    return (-float(detail.get("score") or 0.0), detail["ticker"])


def _percent_chip(value: float | None) -> str:
    if value is None:
        return "-"
    if value > 0:
        bg, fg, prefix = "#dcfce7", "#166534", "+"
    elif value < 0:
        bg, fg, prefix = "#fee2e2", "#991b1b", ""
    else:
        bg, fg, prefix = "#f3f4f6", "#374151", ""
    return (
        f"<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-size:12px;font-weight:800;white-space:nowrap;'>{prefix}{value:.1f}%</span>"
    )


def _breadth_chip(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 65:
        bg, fg = "#dcfce7", "#166534"
    elif value >= 50:
        bg, fg = "#fef3c7", "#92400e"
    else:
        bg, fg = "#fee2e2", "#991b1b"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-size:12px;font-weight:800;white-space:nowrap;'>{value:.0f}% up</span>"
    )


def _concept_sort_link(concept_slug: str, current_sort_by: str, current_sort_order: str, column: str, lang: str, comparison_sort: str) -> str:
    next_order = "asc" if current_sort_by == column and current_sort_order == "desc" else "desc"
    arrow = ""
    if current_sort_by == column:
        arrow = " ↓" if current_sort_order == "desc" else " ↑"
    query = urlencode({"sort_by": column, "sort_order": next_order, "lang": lang, "comparison_sort": comparison_sort})
    return f"/dashboard/concepts/{concept_slug}?{query}"


def _comparison_sort_rank(mode: str, detail: dict) -> tuple:
    if mode == "momentum_20d":
        return (-float(detail.get("twenty_day_move") or -9999.0), -float(detail.get("score") or 0.0), detail["ticker"])
    if mode == "watchlist_ready":
        return (-int(detail.get("watch_state_rank") or 0), -float(detail.get("score") or 0.0), detail["ticker"])
    return (-float(detail.get("score") or 0.0), -float(detail.get("twenty_day_move") or -9999.0), detail["ticker"])


def _market_concept_sort_key(sort_by: str, item: dict) -> tuple:
    if sort_by == "concept":
        return (str(item.get("concept_name") or "").lower(),)
    if sort_by == "hits":
        return (int(item.get("hits") or 0), float(item.get("avg_score") or 0.0))
    if sort_by == "delta":
        return (int(item.get("delta_hits") or 0), int(item.get("hits") or 0))
    if sort_by == "streak":
        return (int(item.get("streak") or 0), int(item.get("hits") or 0))
    if sort_by == "five_day":
        return (float(item.get("avg_move_5d") or -9999.0), float(item.get("avg_score") or 0.0))
    if sort_by == "breadth":
        return (float(item.get("breadth_pct") or -1.0), float(item.get("avg_score") or 0.0))
    if sort_by == "buy_count":
        return (int(item.get("buy_signal_count") or 0), int(item.get("max_signal_strength") or 0))
    if sort_by == "max_strength":
        return (int(item.get("max_signal_strength") or 0), int(item.get("buy_signal_count") or 0))
    if sort_by == "score":
        return (float(item.get("avg_score") or 0.0), int(item.get("hits") or 0))
    return (int(item.get("delta_hits") or 0), int(item.get("hits") or 0), str(item.get("concept_name") or "").lower())


def _matches_execution_tag_filter(tags: list[str] | None, execution_tag_filter: str) -> bool:
    normalized = str(execution_tag_filter or "").strip().lower()
    if not normalized or normalized == "all":
        return True
    requested = [part.strip() for part in normalized.split(",") if part.strip() and part.strip() != "all"]
    if not requested:
        return True
    values = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    return any(tag in values for tag in requested)


def _excludes_execution_tag_filter(tags: list[str] | None, exclude_execution_tag_filter: str) -> bool:
    normalized = str(exclude_execution_tag_filter or "").strip().lower()
    if not normalized or normalized == "all":
        return True
    requested = [part.strip() for part in normalized.split(",") if part.strip() and part.strip() != "all"]
    if not requested:
        return True
    values = [str(tag).strip().lower() for tag in (tags or []) if str(tag).strip()]
    return not any(tag in values for tag in requested)


@router.get("/summary")
def dashboard_summary(request: Request, lookback_runs: int = 5, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    return _load_summary(db, lookback_runs=_clamp_lookback_runs(lookback_runs))


@router.get("/data-sources", response_class=HTMLResponse)
def dashboard_data_sources(request: Request, lang: str = "en", db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard/data-sources")
    lang = resolve_request_lang(request)
    lookback_runs = _clamp_lookback_runs(request.query_params.get("lookback_runs", 5))
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    data_sources = summary["data_sources"]
    sync_states = summary["sync_states"]
    concept_data = data_sources["concept_data"] or {}
    synced_count = len(sync_states)
    provider_count = len(data_sources["current_provider_breakdown"])
    primary_provider = data_sources["primary_provider"] or "-"
    concept_freshness = concept_data.get("freshness") or "-"
    provider_strategy = _provider_strategy_view(lang)
    ds_text = {
        "en": {
            "title": "Data Sources",
            "hero": "Where This App Gets Data",
            "lead": "Use this page to understand the live provider mix, sync freshness, and the fallback strategy behind market, profile, and concept data.",
            "workspace": "Workspace",
            "model_picks": "Model Picks",
            "market": "Market",
            "jobs": "Jobs",
            "data": "Data",
            "settings": "Settings",
            "status_title": "Data Status",
            "status_copy": "Provider freshness and actual sync sources",
            "updated": "Updated",
            "primary_provider": "Primary Provider",
            "primary_provider_help": "The most common provider across the latest sync history.",
            "primary_provider_label": "Primary provider",
            "tracked_providers": "Tracked Providers",
            "tracked_providers_help": "How many different providers appear in current sync records.",
            "tracked_symbols": "Tracked Symbols",
            "tracked_symbols_help": "Symbols with sync metadata stored locally.",
            "synced_symbols": "Synced symbols",
            "concept_freshness": "Concept freshness",
            "freshness": "Freshness",
            "cn_concepts": "CN Concepts",
            "latest_as_of": "Latest as-of date",
            "concepts_across_symbols": "{concepts} concepts across {symbols} symbols",
            "focus": "What To Check First",
            "focus_copy": "Start with provider concentration, concept freshness, and the latest per-symbol sync rows before drilling into details.",
            "top_summary": "Data Strategy and Current Sources",
            "top_summary_copy": "This page should answer two questions quickly: where data is supposed to come from, and what provider the app actually used most recently.",
            "strategy": "Fallback Strategy",
            "strategy_copy": "These are the intended source cascades the app follows when fetching and enriching data.",
            "strategy_prices": "Price History Path",
            "strategy_profiles": "Profile Path",
            "strategy_concepts": "Concept Path",
            "open_jobs": "Open Task Center",
            "open_workspace": "Back to workspace",
            "provider_rows": "Latest provider mix",
            "current_mix": "Current provider mix",
            "recent_sync": "Latest sync rows",
            "per_symbol_sync_source": "Recent Per-Symbol Sync State",
            "stocks": "Stocks",
            "no_provider_usage": "No provider usage yet",
            "no_sync_history": "No sync history yet",
        },
        "zh": {
            "title": "数据来源",
            "hero": "这个应用的数据来自哪里",
            "lead": "这个页面用来解释当前数据源分布、同步新鲜度，以及行情、资料、概念数据背后的回退策略。",
            "workspace": "工作台",
            "model_picks": "模型选股",
            "market": "市场概览",
            "jobs": "任务中心",
            "data": "数据状态",
            "settings": "设置",
            "status_title": "数据状态",
            "status_copy": "数据源新鲜度与实际同步来源",
            "updated": "最近更新",
            "primary_provider": "主要数据源",
            "primary_provider_help": "最近同步记录里占比最高的数据源。",
            "primary_provider_label": "主要数据源",
            "tracked_providers": "已跟踪数据源",
            "tracked_providers_help": "当前同步记录里出现过的不同数据源数量。",
            "tracked_symbols": "已跟踪股票",
            "tracked_symbols_help": "本地数据库中保存了同步元数据的股票数量。",
            "synced_symbols": "已同步股票数",
            "concept_freshness": "概念数据新鲜度",
            "freshness": "新鲜度",
            "cn_concepts": "A股概念",
            "latest_as_of": "最新日期",
            "concepts_across_symbols": "{concepts} 个概念，覆盖 {symbols} 只股票",
            "focus": "先看什么",
            "focus_copy": "先看数据源集中度、概念数据日期，再看最近几条逐股同步状态，确认整体是否健康。",
            "top_summary": "数据策略与当前来源",
            "top_summary_copy": "这页应该先回答两个问题：系统理论上该从哪里取数，以及最近一次实际上用了哪个数据源。",
            "strategy": "回退策略",
            "strategy_copy": "这里展示应用在拉取和补全数据时预期遵循的数据源路径。",
            "strategy_prices": "行情路径",
            "strategy_profiles": "资料路径",
            "strategy_concepts": "概念路径",
            "open_jobs": "打开任务中心",
            "open_workspace": "返回工作台",
            "provider_rows": "最近数据源分布",
            "current_mix": "当前数据源分布",
            "recent_sync": "最近同步记录",
            "per_symbol_sync_source": "逐股最新同步状态",
            "stocks": "股票数",
            "no_provider_usage": "暂无数据源使用记录",
            "no_sync_history": "暂无同步记录",
        },
    }["zh" if lang == "zh" else "en"]
    history_steps = "".join(f"<li>{step}</li>" for step in data_sources["historical_price_strategy"])
    profile_steps = "".join(f"<li>{step}</li>" for step in data_sources["symbol_profile_strategy"])
    concept_steps = "".join(f"<li>{step}</li>" for step in data_sources["concept_strategy"])
    provider_rows = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{item['provider']}</div><div class='subtle'>{ds_text['stocks']}</div></div>"
        f"<div class='row-right'><div class='mini-metric'>{item['count']}</div></div>"
        "</article>"
        for item in data_sources["current_provider_breakdown"][:6]
    ) or f"<div class='empty'>{ds_text['no_provider_usage']}</div>"
    symbol_rows = "".join(
        "<article class='sync-row'>"
        f"<div><a class='ticker' href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a><div class='subtle'>{item['name'] or item['ticker']} · {item['provider'] or '-'}</div><div class='subtle'>{item['message'] or '-'}</div></div>"
        f"<div class='row-right'><div class='mini-metric'>{item['last_synced_date'] or '-'}</div><span class='status-pill {str(item['status'] or 'idle').lower()}'>{item['status'] or '-'}</span></div>"
        "</article>"
        for item in sync_states[:8]
    ) or f"<div class='empty'>{ds_text['no_sync_history']}</div>"
    nav_html = render_workspace_nav_html(lang=lang, active_key="data", lookback_runs=lookback_runs)
    metrics_html = "".join(
        "<article class='metric-card'>"
        f"<div class='metric-label'>{label}</div>"
        f"<div class='metric-value'>{value}</div>"
        f"<div class='metric-meta'>{meta}</div>"
        "</article>"
        for label, value, meta in [
            (ds_text["primary_provider"], primary_provider, ds_text["primary_provider_help"]),
            (ds_text["tracked_providers"], provider_count, ds_text["tracked_providers_help"]),
            (ds_text["tracked_symbols"], synced_count, ds_text["tracked_symbols_help"]),
            (ds_text["concept_freshness"], concept_freshness, f"{ds_text['latest_as_of']}: {concept_data.get('latest_as_of_date') or '-'}"),
        ]
    )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{ds_text['title']}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .topbar,.chip-row,.action-row,.row-right {{ display:flex; flex-wrap:wrap; gap:10px; }}
          .topbar {{ justify-content:space-between; align-items:center; margin-bottom:24px; }}
          .top-pill,.cta,.mini-metric,.status-pill {{ display:inline-flex; align-items:center; justify-content:center; }}
          .top-pill,.cta {{ padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--muted); font-size:13px; font-weight:700; }}
          .cta.primary {{ background:linear-gradient(135deg, rgba(61,217,182,0.28), rgba(82,168,255,0.24)); color:var(--ink); }}
          .hero {{ display:grid; grid-template-columns:minmax(0,1.4fr) minmax(280px,0.9fr); gap:16px; margin-bottom:16px; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.06em; text-transform:uppercase; }}
          h1 {{ margin:14px 0 10px; font-size:40px; line-height:1.02; letter-spacing:-0.03em; }}
          .section-title {{ margin:0 0 6px; font-size:22px; }}
          .lead,.section-copy,.subtle,.metric-meta,li,.empty {{ color:var(--muted); }}
          .lead,.section-copy {{ font-size:15px; line-height:1.6; }}
          .metrics-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin:16px 0; }}
          .metric-card {{ padding:18px; border-radius:20px; background:rgba(21,34,49,0.82); border:1px solid var(--line); }}
          .metric-label {{ color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:0.05em; }}
          .metric-value {{ margin-top:12px; font-size:26px; font-weight:800; letter-spacing:-0.03em; word-break:break-word; }}
          .workspace-grid {{ display:grid; grid-template-columns:minmax(0,1.05fr) minmax(320px,0.95fr); gap:16px; align-items:start; }}
          .stack,.list-stack {{ display:grid; gap:16px; }}
          .list-row,.sync-row {{ display:flex; justify-content:space-between; align-items:flex-start; gap:14px; padding:14px 0; border-top:1px solid rgba(144,163,184,0.12); }}
          .list-row:first-child,.sync-row:first-child {{ border-top:none; padding-top:0; }}
          .ticker {{ font-weight:800; font-size:15px; color:var(--ink); }}
          .subtle {{ margin-top:4px; font-size:12px; line-height:1.45; }}
          .mini-metric {{ padding:7px 10px; border-radius:999px; background:rgba(82,168,255,0.12); color:#b9dcff; font-size:12px; font-weight:700; }}
          .status-pill {{ padding:7px 10px; border-radius:999px; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:0.04em; background:rgba(144,163,184,0.14); color:#b4c5d8; }}
          .status-pill.success {{ background:rgba(74,222,128,0.14); color:#8df0aa; }} .status-pill.failed {{ background:rgba(255,107,129,0.16); color:#ff9aaa; }} .status-pill.partial {{ background:rgba(246,200,95,0.16); color:#ffd98a; }} .status-pill.running {{ background:rgba(82,168,255,0.16); color:#9bd0ff; }}
          ul {{ margin:10px 0 0 18px; padding:0; }} li {{ margin:8px 0; line-height:1.55; }}
          @media (max-width:1180px) {{ .metrics-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .workspace-grid, .hero {{ grid-template-columns:1fr; }} }}
          @media (max-width:900px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} }}
          @media (max-width:640px) {{ .main {{ padding:20px 16px 36px; }} h1 {{ font-size:30px; }} .metrics-grid {{ grid-template-columns:1fr; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand"><span class="brand-tag">PQW</span><h1>{ds_text['status_title']}</h1><p>{ds_text['status_copy']}</p></div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{ds_text['top_summary_copy']}</div>
          </aside>
          <main class="main">
            <div class="topbar">
              <div class="chip-row"><span class="top-pill">{ds_text['updated']}: {_display_time(summary.get('generated_at'), with_tz=True)}</span><span class="top-pill">{ds_text['primary_provider_label']}: {primary_provider}</span><span class="top-pill">{ds_text['synced_symbols']}: {synced_count}</span></div>
              <div class="chip-row"><a class="top-pill" href="/dashboard/data-sources?lang=en&lookback_runs={lookback_runs}">EN</a><a class="top-pill" href="/dashboard/data-sources?lang=zh&lookback_runs={lookback_runs}">中文</a></div>
            </div>
            <section class="hero">
              <article class="card"><span class="eyebrow">{ds_text['top_summary']}</span><h1>{ds_text['hero']}</h1><p class="lead">{ds_text['lead']}</p><div class="action-row"><a class="cta primary" href="/dashboard?lang={lang}&lookback_runs={lookback_runs}">{ds_text['open_workspace']}</a><a class="cta" href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}">{ds_text['open_jobs']}</a></div></article>
              <article class="card"><span class="eyebrow">{ds_text['focus']}</span><h2 class="section-title">{ds_text['status_title']}</h2><p class="section-copy">{ds_text['focus_copy']}</p><div class="list-stack"><div><div class="subtle">{ds_text['primary_provider']}</div><div class="ticker">{primary_provider}</div></div><div><div class="subtle">{ds_text['concept_freshness']}</div><div class="ticker">{concept_freshness}</div></div><div><div class="subtle">{ds_text['latest_as_of']}</div><div class="ticker">{concept_data.get('latest_as_of_date') or '-'}</div></div><div><div class="subtle">{ds_text['tracked_providers']}</div><div class="ticker">{provider_count}</div></div></div></article>
            </section>
            <section class="metrics-grid">{metrics_html}</section>
            <section class="workspace-grid">
              <div class="stack">
                <article class="card"><span class="eyebrow">{ds_text['strategy']}</span><h2 class="section-title">{ds_text['strategy_copy']}</h2><div class="workspace-grid" style="grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px;"><article class="metric-card"><div class="metric-label">{ds_text['strategy_prices']}</div><ul>{history_steps}</ul></article><article class="metric-card"><div class="metric-label">{ds_text['strategy_profiles']}</div><ul>{profile_steps}</ul></article><article class="metric-card"><div class="metric-label">{ds_text['strategy_concepts']}</div><ul>{concept_steps}</ul></article></div></article>
                <article class="card"><span class="eyebrow">{provider_strategy['title']}</span><h2 class="section-title">{provider_strategy['copy']}</h2><div class="list-stack"><article class="list-row"><div><div class="ticker">Price / Auto</div><div class="subtle">{provider_strategy['price_auto']}</div></div></article><article class="list-row"><div><div class="ticker">Price / OpenBB</div><div class="subtle">{provider_strategy['price_openbb']}</div></div></article><article class="list-row"><div><div class="ticker">Fundamental / Auto</div><div class="subtle">{provider_strategy['fund_auto']}</div></div></article><article class="list-row"><div><div class="ticker">Concept / Auto</div><div class="subtle">{provider_strategy['concept_auto']}</div></div></article><article class="list-row"><div><div class="ticker">Execution / Realtime</div><div class="subtle">{provider_strategy['execution']}</div></div></article></div></article>
                <article class="card"><span class="eyebrow">{ds_text['recent_sync']}</span><h2 class="section-title">{ds_text['per_symbol_sync_source']}</h2><div class="list-stack">{symbol_rows}</div></article>
              </div>
              <div class="stack">
                <article class="card"><span class="eyebrow">{ds_text['current_mix']}</span><h2 class="section-title">{ds_text['provider_rows']}</h2><div class="list-stack">{provider_rows}</div></article>
                <article class="card"><span class="eyebrow">{ds_text['cn_concepts']}</span><h2 class="section-title">{ds_text['concept_freshness']}</h2><p class="section-copy">{ds_text['concepts_across_symbols'].format(concepts=concept_data.get('concept_count'), symbols=concept_data.get('symbol_count'))}</p><div class="list-stack"><div><div class="subtle">{ds_text['latest_as_of']}</div><div class="ticker">{concept_data.get('latest_as_of_date') or '-'}</div></div><div><div class="subtle">{ds_text.get('freshness', 'Freshness' if lang == 'en' else '新鲜度')}</div><div class="ticker">{concept_freshness}</div></div></div></article>
              </div>
            </section>
          </main>
        </div>
      </body>
    </html>
    """
    lang = "zh" if lang == "zh" else "en"
    summary = _load_summary(db)
    data_sources = summary["data_sources"]
    sync_states = summary["sync_states"]
    ds_text = {
        "en": {
            "back": "Back to dashboard",
            "primary_provider_label": "Primary provider",
            "synced_symbols": "Synced symbols",
            "title": "Data Sources",
            "hero": "Where This App Gets Data",
            "lead": "This page separates the app's intended data strategy from the provider each stock actually used most recently.",
            "primary_provider": "Primary Provider",
            "primary_provider_help": "Dominant provider across the current sync history.",
            "tracked_providers": "Tracked Providers",
            "tracked_providers_help": "Distinct providers currently present in sync records.",
            "tracked_symbols": "Tracked Symbols",
            "tracked_symbols_help": "Symbols with stored sync metadata in the local database.",
            "cn_concepts": "CN Concepts",
            "latest_as_of": "Latest as-of date",
            "concepts_across_symbols": "{concepts} concepts across {symbols} symbols",
            "historical_prices": "Historical Prices",
            "company_profiles": "Company Profiles",
            "cn_concept_mapping": "CN Concept Mapping",
            "provider_breakdown": "Provider Breakdown",
            "stocks": "Stocks",
            "per_symbol_sync_source": "Per Symbol Sync Source",
            "ticker": "Ticker",
            "name": "Name",
            "provider": "Provider",
            "status": "Status",
            "last_sync": "Last Sync",
            "message": "Message",
            "no_provider_usage": "No provider usage yet",
            "no_sync_history": "No sync history yet",
            "lang_en": "English",
            "lang_zh": "中文",
        },
        "zh": {
            "back": "返回总览",
            "primary_provider_label": "主要数据源",
            "synced_symbols": "已同步股票数",
            "title": "数据来源",
            "hero": "这个应用的数据来自哪里",
            "lead": "这个页面区分了应用预期的数据策略，以及每只股票最近一次实际使用的数据源。",
            "primary_provider": "主要数据源",
            "primary_provider_help": "当前同步记录里占比最高的数据源。",
            "tracked_providers": "已跟踪数据源",
            "tracked_providers_help": "当前同步记录里出现过的不同数据源数量。",
            "tracked_symbols": "已跟踪股票",
            "tracked_symbols_help": "本地数据库中保存了同步元数据的股票数量。",
            "cn_concepts": "A股概念",
            "latest_as_of": "最新日期",
            "concepts_across_symbols": "{concepts} 个概念，覆盖 {symbols} 只股票",
            "historical_prices": "历史行情",
            "company_profiles": "公司资料",
            "cn_concept_mapping": "A股概念映射",
            "provider_breakdown": "数据源分布",
            "stocks": "股票数",
            "per_symbol_sync_source": "逐股同步来源",
            "ticker": "代码",
            "name": "名称",
            "provider": "数据源",
            "status": "状态",
            "last_sync": "最近同步",
            "message": "消息",
            "no_provider_usage": "暂无数据源使用记录",
            "no_sync_history": "暂无同步记录",
            "lang_en": "English",
            "lang_zh": "中文",
        },
    }["zh" if lang == "zh" else "en"]
    provider_rows = "".join(
        f"<tr><td>{item['provider']}</td><td>{item['count']}</td></tr>"
        for item in data_sources["current_provider_breakdown"]
    ) or f"<tr><td colspan='2'>{ds_text['no_provider_usage']}</td></tr>"
    visible_sync_states = sync_states[:200]
    symbol_rows = "".join(
        f"<tr><td><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td><td title='{item['name'] or item['ticker']}'>{_compact_label(item['name'] or item['ticker'], 20)}</td><td>{item['provider'] or '-'}</td><td>{item['status'] or '-'}</td><td>{item['last_synced_date'] or '-'}</td><td class='message-cell' title='{item['message'] or '-'}'>{_compact_label(item['message'] or '-', 56)}</td></tr>"
        for item in visible_sync_states
    ) or f"<tr><td colspan='6'>{ds_text['no_sync_history']}</td></tr>"
    history_steps = "".join(f"<li>{step}</li>" for step in data_sources["historical_price_strategy"])
    profile_steps = "".join(f"<li>{step}</li>" for step in data_sources["symbol_profile_strategy"])
    concept_steps = "".join(f"<li>{step}</li>" for step in data_sources["concept_strategy"])
    synced_count = len(sync_states)
    provider_count = len(data_sources["current_provider_breakdown"])
    concept_data = data_sources["concept_data"]
    lang_switch = (
        f"<a href='/dashboard/data-sources?lang=en' class='pill'>{ds_text['lang_en'] if lang == 'en' else 'English'}</a>"
        f"<a href='/dashboard/data-sources?lang=zh' class='pill'>{ds_text['lang_zh'] if lang == 'zh' else '中文'}</a>"
    )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Data Sources</title>
        <style>
          :root {{
            --bg: #f5efe2;
            --panel: #fffdf7;
            --ink: #1f2937;
            --muted: #6b7280;
            --line: #d6cfc2;
            --accent: #0f766e;
            --accent-soft: #dff5ef;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, #fff6d8 0, transparent 30%),
              radial-gradient(circle at top right, #d9f3ee 0, transparent 35%),
              var(--bg);
          }}
          .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }}
          .card {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; margin-bottom:16px; box-shadow:0 8px 24px rgba(31,41,55,0.05); }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          h1 {{ margin:0 0 8px; font-size:38px; line-height:1.05; }}
          .lead {{ margin:0; color:var(--muted); max-width:760px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .metric {{ font-size:28px; font-weight:700; margin:6px 0; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:18px; }}
          .pill {{ display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border-radius:999px; background:#eef8f5; color:#0f766e; font-size:13px; font-weight:700; }}
          a {{ color:#0f766e; text-decoration:none; font-weight:700; }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:14px; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; min-width:760px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }}
          th {{ color:var(--muted); font-weight:600; }}
          .message-cell {{
            max-width: 340px;
            white-space: normal;
            word-break: break-word;
            overflow-wrap: anywhere;
            line-height: 1.45;
            color: #374151;
          }}
          ul {{ margin:10px 0 0 18px; padding:0; }}
          li {{ margin:6px 0; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(280px, 1fr)); margin-bottom:16px; }}
          code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; background: #f3f4f6; padding: 2px 6px; border-radius: 8px; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div class="toolbar">
            <a href="/dashboard?lang={lang}">← {ds_text['back']}</a>
            <span class="pill">{ds_text['primary_provider_label']}: {data_sources['primary_provider'] or '-'}</span>
            <span class="muted">{ds_text['synced_symbols']}: {synced_count}</span>
            {lang_switch}
          </div>
          <div class="card">
            <div class="eyebrow">{ds_text['title']}</div>
            <h1>{ds_text['hero']}</h1>
            <p class="lead">{ds_text['lead']}</p>
          </div>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{ds_text['primary_provider']}</div>
              <div class="metric">{data_sources['primary_provider'] or 'None'}</div>
              <div class="muted">{ds_text['primary_provider_help']}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{ds_text['tracked_providers']}</div>
              <div class="metric">{provider_count}</div>
              <div class="muted">{ds_text['tracked_providers_help']}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{ds_text['tracked_symbols']}</div>
              <div class="metric">{synced_count}</div>
              <div class="muted">{ds_text['tracked_symbols_help']}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{ds_text['cn_concepts']}</div>
              <div class="metric">{concept_data['freshness']}</div>
              <div class="muted">{ds_text['latest_as_of']}: {concept_data['latest_as_of_date'] or '-'}</div>
              <div class="muted">{ds_text['concepts_across_symbols'].format(concepts=concept_data['concept_count'], symbols=concept_data['symbol_count'])}</div>
            </article>
          </div>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{ds_text['historical_prices']}</div>
              <ul>{history_steps}</ul>
            </article>
            <article class="card">
              <div class="eyebrow">{ds_text['company_profiles']}</div>
              <ul>{profile_steps}</ul>
            </article>
            <article class="card">
              <div class="eyebrow">{ds_text['cn_concept_mapping']}</div>
              <ul>{concept_steps}</ul>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{ds_text['provider_breakdown']}</div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>{ds_text['provider']}</th><th>{ds_text['stocks']}</th></tr></thead>
                <tbody>{provider_rows}</tbody>
              </table>
            </div>
          </section>
          <section class="card">
            <div class="eyebrow">{ds_text['per_symbol_sync_source']}</div>
            <div class="muted" style="margin-bottom:10px;">{('仅展示最近 200 条同步记录。' if lang == 'zh' else 'Showing the latest 200 per-symbol sync rows.')}</div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>{ds_text['ticker']}</th><th>{ds_text['name']}</th><th>{ds_text['provider']}</th><th>{ds_text['status']}</th><th>{ds_text['last_sync']}</th><th>{ds_text['message']}</th></tr></thead>
                <tbody>{symbol_rows}</tbody>
              </table>
            </div>
          </section>
        </main>
      </body>
    </html>
    """


@router.get("/concepts/{concept_slug}", response_class=HTMLResponse)
def dashboard_concept_detail(
    request: Request,
    concept_slug: str,
    message: str | None = None,
    lang: str = "en",
    sort_by: str = "score",
    sort_order: str = "desc",
    comparison_sort: str = "model_score",
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    lookback_runs: int = 5,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/concepts/{concept_slug}")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    concept = _get_concept_for_detail(db, summary, concept_slug, lang=lang)
    if concept is None:
        return HTMLResponse("<h1>Concept not found</h1>", status_code=404)

    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    symbol_data_service = SymbolDataService()
    prediction_repo = PredictionRepository(db)
    ticker_csv = ",".join(detail["ticker"] for detail in concept["ticker_details"])
    banner_html = (
        f"<div class='banner'>{message}</div>"
        if message
        else ""
    )
    ticker_detail_rows: list[dict] = []
    compute_price_moves = concept.get("source") != "heatmap_snapshot" or len(concept["ticker_details"]) <= 60
    for detail in concept["ticker_details"]:
        state_label, state_bg, state_fg = _concept_ticker_watch_state(watchlist_map, detail["ticker"], lang)
        existing = watchlist_map.get(detail["ticker"])
        five_day_move = None
        twenty_day_move = None
        if compute_price_moves:
            history = symbol_data_service.get_history(detail["ticker"], limit=21)
            if len(history) >= 6:
                start_close = history[-6].get("close")
                end_close = history[-1].get("close")
                if start_close not in (None, 0) and end_close is not None:
                    five_day_move = ((float(end_close) / float(start_close)) - 1) * 100
            if len(history) >= 20:
                start_close = history[-20].get("close")
                end_close = history[-1].get("close")
                if start_close not in (None, 0) and end_close is not None:
                    twenty_day_move = ((float(end_close) / float(start_close)) - 1) * 100
        ticker_detail_rows.append(
            {
                **detail,
                "display_signal_label": build_signal_label(detail.get("score"), lang=lang) or ("Hold" if lang == "en" else "持有"),
                "watch_state_label": state_label,
                "watch_state_bg": state_bg,
                "watch_state_fg": state_fg,
                "watch_state_rank": 0 if existing is None else (3 if existing.get("sync_enabled") and existing.get("sync_status") == "success" else 2 if existing.get("sync_enabled") else 1),
                "last_synced_date": existing.get("last_synced_date") if existing else None,
                "existing": existing,
                "five_day_move": five_day_move,
                "twenty_day_move": twenty_day_move,
            }
        )

    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    if signal_filter != "ALL":
        def _signal_key(detail: dict) -> str:
            label = build_signal_label(detail.get("score"), lang="en") or "Hold"
            return label.upper()
        ticker_detail_rows = [detail for detail in ticker_detail_rows if _signal_key(detail) == signal_filter]
    if min_signal_strength > 0:
        ticker_detail_rows = [
            detail for detail in ticker_detail_rows
            if int(detail.get("signal_strength") or 0) >= min_signal_strength
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        ticker_detail_rows = [
            detail for detail in ticker_detail_rows
            if _matches_execution_tag_filter(detail.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        ticker_detail_rows = [
            detail for detail in ticker_detail_rows
            if _excludes_execution_tag_filter(detail.get("execution_tags"), exclude_execution_tag_filter)
        ]

    ticker_detail_rows.sort(key=lambda item: _concept_sort_rank(sort_by, item))
    if sort_order == "desc":
        ticker_detail_rows.reverse()

    ticker_row_list: list[str] = []
    for detail in ticker_detail_rows:
        existing = detail["existing"]
        single_action_button = ""
        if existing is None:
            single_action_button = (
                f"<form action='/dashboard/concepts/{concept_slug}/ticker-action' method='post' style='display:inline-block;margin:0;'>"
                f"<input type='hidden' name='ticker' value='{detail['ticker']}' />"
                f"<input type='hidden' name='action' value='add' />"
                f"<input type='hidden' name='lang' value='{lang}' />"
                f"<button type='submit'>{_concept_tr(lang, 'add')}</button>"
                "</form>"
            )
        elif existing.get("sync_enabled") and existing.get("sync_status") == "success":
            single_action_button = (
                f"<a href='/insights/{detail['ticker']}?lang={lang}' class='action-link'>{_concept_tr(lang, 'open')}</a>"
            )
        else:
            single_action_button = (
                f"<form action='/dashboard/concepts/{concept_slug}/ticker-action' method='post' style='display:inline-block;margin:0;'>"
                f"<input type='hidden' name='ticker' value='{detail['ticker']}' />"
                f"<input type='hidden' name='action' value='sync' />"
                f"<input type='hidden' name='lang' value='{lang}' />"
                f"<button type='submit'>{_concept_tr(lang, 'sync')}</button>"
                "</form>"
            )
        ticker_row_list.append(
            "<tr>"
            f"<td><a href='/insights/{detail['ticker']}?lang={lang}'>{detail['ticker']}</a></td>"
            f"<td>{detail.get('name') or detail['ticker']}</td>"
            f"<td><div>{float(detail.get('score') or 0.0):.4f}</div><div style='margin-top:6px;'>{_dashboard_model_badge(detail.get('state'), confidence=detail.get('confidence'), compact=True)}</div><div style='margin-top:6px;'>{_signal_pill(detail.get('score'), lang=lang, strength=int(detail.get('signal_strength') or 0), compact=True)}</div><div style='margin-top:6px;font-size:12px;color:#6b7280;'>{('Pct ' + format(float(detail.get('percentile')), '.1f') + '%') if detail.get('percentile') is not None else ''}{(' · ' if detail.get('percentile') is not None and detail.get('target_horizon_days') is not None else '')}{('H ' + str(int(detail.get('target_horizon_days'))) + 'd') if detail.get('target_horizon_days') is not None else ''}{(' · ' if (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None) and detail.get('model_reward_risk_ratio') is not None else '')}{('R/R ' + format(float(detail.get('model_reward_risk_ratio')), '.2f')) if detail.get('model_reward_risk_ratio') is not None else ''}{(' · ' if (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None) and detail.get('conviction_bucket') else '')}{detail.get('conviction_bucket') or ''}{(' · ' if detail.get('position_size_hint') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket')) else '')}{detail.get('position_size_hint') or ''}{(' · ' if detail.get('entry_style') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket') or detail.get('position_size_hint')) else '')}{detail.get('entry_style') or ''}{(' · ' if detail.get('execution_tags') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket') or detail.get('position_size_hint') or detail.get('entry_style')) else '')}{' / '.join((detail.get('execution_tags') or [])[:2])}</div></td>"
            f"<td>{_percent_chip(detail['five_day_move'])}</td>"
            f"<td><span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;background:{detail['watch_state_bg']};color:{detail['watch_state_fg']};font-size:12px;font-weight:800;white-space:nowrap;'>{detail['watch_state_label']}</span></td>"
            f"<td>{detail.get('last_synced_date') or '-'}</td>"
            f"<td style='white-space:nowrap;'>{single_action_button} <a href='/insights/{detail['ticker']}?lang={lang}' class='action-link' style='margin-left:8px;'>{_concept_tr(lang, 'insight')}</a></td>"
            "</tr>"
        )
    ticker_rows = "".join(ticker_row_list) or f"<tr><td colspan='7'>{_concept_tr(lang, 'no_tickers')}</td></tr>"
    sparkline = _sparkline_svg(concept["history"])
    sorted_ticker_csv = ",".join(detail["ticker"] for detail in ticker_detail_rows)
    comparison_rows = sorted(ticker_detail_rows, key=lambda item: _comparison_sort_rank(comparison_sort, item))[:3]
    comparison_cards = []
    for detail in comparison_rows:
        history = symbol_data_service.get_history(detail["ticker"], limit=20)
        closes = [float(row["close"]) for row in history if row.get("close") is not None]
        prediction_history = prediction_repo.list_symbol_predictions(detail["ticker"], limit=120, latest_run_only=True)
        latest_close = closes[-1] if closes else None
        latest_close_html = f"<span>Last {latest_close:.2f}</span>" if latest_close is not None else "<span>Last -</span>"
        twenty_day_html = (
            f"<span>20D {'+' if detail['twenty_day_move'] > 0 else ''}{detail['twenty_day_move']:.1f}%</span>"
            if detail['twenty_day_move'] is not None
            else "<span>20D -</span>"
        )
        comparison_tag_html = "".join(
            "<span style='background:#fff7ed;color:#c2410c;padding:4px 8px;border-radius:999px;border:1px solid #fed7aa;'>"
            f"{tag}"
            "</span>"
            for tag in (detail.get("execution_tags") or [])[:2]
        )
        comparison_cards.append(
            "<article class='mini-card'>"
            f"<div class='mini-top'><a href='/insights/{detail['ticker']}?lang={lang}'>{detail['ticker']}</a><span class='mini-score'>{_concept_tr(lang, 'model_score').lower()} {detail['score']:.3f}</span></div>"
            f"<div class='mini-name'>{detail.get('name') or detail['ticker']}</div>"
            f"<div style='margin-bottom:10px;'>{_dashboard_model_badge(detail.get('state'), confidence=detail.get('confidence'), compact=True)}</div>"
            f"<div style='margin-bottom:8px;'>{_signal_pill(detail.get('score'), lang=lang, strength=int(detail.get('signal_strength') or 0), compact=True)}</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;color:#6b7280;font-size:12px;font-weight:700;'><span>{('Pct ' + format(float(detail.get('percentile')), '.1f') + '%') if detail.get('percentile') is not None else 'Pct -'}</span><span>{('H ' + str(int(detail.get('target_horizon_days'))) + 'd') if detail.get('target_horizon_days') is not None else 'H -'}</span><span>{('R/R ' + format(float(detail.get('model_reward_risk_ratio')), '.2f')) if detail.get('model_reward_risk_ratio') is not None else 'R/R -'}</span><span>{detail.get('conviction_bucket') or ('Conviction -' if lang == 'en' else '信念 -')}</span><span>{detail.get('position_size_hint') or ('Sizing -' if lang == 'en' else '仓位 -')}</span><span>{detail.get('entry_style') or ('Entry -' if lang == 'en' else '进场 -')}</span>{comparison_tag_html}</div>"
            f"{_price_signal_sparkline_svg(history, prediction_history)}"
            "<div class='mini-metrics'>"
            f"{latest_close_html}"
            f"{twenty_day_html}"
            + "</div></article>"
        )
    comparison_html = "".join(comparison_cards) or f"<div class='muted'>{_concept_tr(lang, 'not_enough_price_history')}</div>"
    comparison_tabs = "".join(
        (
            f"<a href='/dashboard/concepts/{concept_slug}?{urlencode({'sort_by': sort_by, 'sort_order': sort_order, 'comparison_sort': mode, 'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' "
            f"class='compare-pill{' active' if comparison_sort == mode else ''}'>{label}</a>"
        )
        for mode, label in (
            ("model_score", _concept_tr(lang, "top_by_model")),
            ("momentum_20d", _concept_tr(lang, "top_by_20d")),
            ("watchlist_ready", _concept_tr(lang, "ready_first")),
        )
    )
    lang_switch = (
        f"<div style='display:flex;gap:8px;align-items:center;margin-top:12px;'>"
        f"<a href='/dashboard/concepts/{concept_slug}?{urlencode({'lang': 'en', 'sort_by': sort_by, 'sort_order': sort_order, 'comparison_sort': comparison_sort, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if lang != 'zh' else ''}'>{_concept_tr('en', 'lang_en')}</a>"
        f"<a href='/dashboard/concepts/{concept_slug}?{urlencode({'lang': 'zh', 'sort_by': sort_by, 'sort_order': sort_order, 'comparison_sort': comparison_sort, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if lang == 'zh' else ''}'>{_concept_tr('zh', 'lang_zh')}</a>"
        "</div>"
    )
    lookback_pills = _lookback_pills(
        f"/dashboard/concepts/{concept_slug}",
        selected=lookback_runs,
        extra_params={"lang": lang, "sort_by": sort_by, "sort_order": sort_order, "comparison_sort": comparison_sort, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter},
    )
    signal_pills = "".join(
        f"<a href='/dashboard/concepts/{concept_slug}?{urlencode({'lang': lang, 'sort_by': sort_by, 'sort_order': sort_order, 'comparison_sort': comparison_sort, 'lookback_runs': lookback_runs, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
        for mode, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    avg_move_5d_display = "-"
    if concept.get("avg_move_5d") is not None:
        avg_move_5d_display = f"{'+' if concept.get('avg_move_5d', 0) > 0 else ''}{float(concept['avg_move_5d']):.1f}%"
    avg_move_20d_display = "-"
    if concept.get("avg_move_20d") is not None:
        avg_move_20d_display = f"{'+' if concept.get('avg_move_20d', 0) > 0 else ''}{float(concept['avg_move_20d']):.1f}%"
    breadth_display = f"{float(concept['breadth_pct']):.0f}%" if concept.get("breadth_pct") is not None else "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{concept['concept_name']}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-soft:rgba(61,217,182,0.12); }}
          * {{ box-sizing: border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; min-width:0; }}
          .wrap {{ max-width:1180px; margin:0 auto; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .metric {{ font-size: 30px; font-weight: 800; margin: 8px 0; }}
          .muted {{ color: var(--muted); font-size: 14px; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:16px; }}
          .action-grid {{ display:grid; gap:16px; grid-template-columns: minmax(260px, 1fr) minmax(280px, 1.2fr); margin-bottom:16px; }}
          .mini-grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); }}
          .mini-card {{ background:rgba(21,34,49,0.82); border:1px solid var(--line); border-radius:18px; padding:14px; }}
          .mini-top {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:6px; }}
          .mini-score {{ font-size:12px; font-weight:800; color:var(--accent); background:var(--accent-soft); padding:4px 8px; border-radius:999px; }}
          .mini-name {{ color:var(--muted); font-size:13px; margin-bottom:10px; min-height:34px; }}
          .mini-metrics {{ display:flex; justify-content:space-between; gap:10px; color:var(--ink); font-size:12px; font-weight:700; margin-top:8px; }}
          .compare-row {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }}
          .compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); text-decoration:none; font-size:12px; font-weight:800; }}
          .compare-pill.active {{ background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }}
          table {{ width:100%; min-width:980px; border-collapse:collapse; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color: var(--muted); font-weight: 600; }}
          .banner {{ margin-bottom:16px; padding:14px 16px; border-radius:16px; background:rgba(61,217,182,0.12); color:var(--accent); font-weight:700; border:1px solid rgba(61,217,182,0.24); }}
          .stack {{ display:grid; gap:12px; }}
          input, button {{ border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; background:#0f1823; color:var(--ink); }}
          button {{ background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; border-color:transparent; font-weight:800; cursor:pointer; }}
          .checkline {{ display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }}
          .action-link {{ display:inline-flex; align-items:center; padding:10px 12px; border-radius:12px; background:rgba(61,217,182,0.12); color:var(--accent); border:1px solid rgba(61,217,182,0.2); text-decoration:none; font-weight:800; }}
          .sidebar-foot {{ margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }}
          @media (max-width:1100px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} .action-grid {{ grid-template-columns:1fr; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'概念详情' if lang == 'zh' else 'Concept Detail'}</h1>
              <p>{'从板块热力图进入后，查看命中股票、模型信号和加入自选动作。' if lang == 'zh' else 'Drill from market heat into member names, model signals, and watchlist actions.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'这页回答“这个概念里哪些股票被模型命中，以及哪些值得加入自选”。' if lang == 'zh' else 'This page answers which names inside a concept were hit by the model and which deserve watchlist attention.'}</div>
          </aside>
          <main class="main">
            <div class="wrap">
              {banner_html}
          <div class="card">
            <div class="compare-row">
              <a class="compare-pill" href="/dashboard/market?lang={lang}&lookback_runs={lookback_runs}">← {'返回市场概况' if lang == 'zh' else 'Back to Market'}</a>
              <a class="compare-pill" href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}">{'板块热力图' if lang == 'zh' else 'Sector Heatmap'}</a>
              <a class="compare-pill" href="/dashboard/market/concepts?lang={lang}&lookback_runs={lookback_runs}">{'概念追踪' if lang == 'zh' else 'Concept Tracker'}</a>
            </div>
            {lang_switch}
            <div class="eyebrow" style="margin-top:12px;">{_concept_tr(lang, 'concept_detail')}</div>
            <div class="metric">{concept['concept_name']}</div>
            <div class="muted">{_concept_tr(lang, 'detail_subtitle')}</div>
          </div>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{_concept_tr(lang, 'hits')}</div>
              <div class="metric">{concept['hits']}</div>
              <div class="muted">{_concept_tr(lang, 'hits_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{_concept_tr(lang, 'delta')}</div>
              <div class="metric">{'+' if concept['delta_hits'] > 0 else ''}{concept['delta_hits']}</div>
              <div class="muted">{_concept_tr(lang, 'delta_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{_concept_tr(lang, 'streak')}</div>
              <div class="metric">{concept['streak']}</div>
              <div class="muted">{_concept_tr(lang, 'streak_help')}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{_concept_tr(lang, 'trend')}</div>
              {sparkline}
              <div class="muted">{_concept_tr(lang, 'trend_help')}</div>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{_concept_tr(lang, 'concept_strength')}</div>
            <div class="muted" style="margin-bottom:14px;">{_concept_tr(lang, 'concept_strength_subtitle')}</div>
            <div class="grid" style="margin-bottom:0;">
              <article class="card" style="margin-bottom:0;background:rgba(21,34,49,0.82);">
                <div class="eyebrow">{_concept_tr(lang, 'five_day')}</div>
                <div class="metric">{avg_move_5d_display}</div>
                <div class="muted">Average 5-day move across tickers inside this concept.</div>
              </article>
              <article class="card" style="margin-bottom:0;background:rgba(21,34,49,0.82);">
                <div class="eyebrow">{_concept_tr(lang, 'twenty_day')}</div>
                <div class="metric">{avg_move_20d_display}</div>
                <div class="muted">Average 20-day move across tickers inside this concept.</div>
              </article>
              <article class="card" style="margin-bottom:0;background:rgba(21,34,49,0.82);">
                <div class="eyebrow">{_concept_tr(lang, 'breadth')}</div>
                <div class="metric">{breadth_display}</div>
                <div class="muted">{_concept_tr(lang, 'breadth_help')}</div>
              </article>
              <article class="card" style="margin-bottom:0;background:rgba(21,34,49,0.82);">
                <div class="eyebrow">{_concept_tr(lang, 'buy_signal_count')}</div>
                <div class="metric">{int(concept.get('buy_signal_count') or 0)}</div>
                <div class="muted">{_concept_tr(lang, 'buy_signal_count_help')}</div>
              </article>
              <article class="card" style="margin-bottom:0;background:rgba(21,34,49,0.82);">
                <div class="eyebrow">{_concept_tr(lang, 'max_signal_strength')}</div>
                <div class="metric">{int(concept.get('max_signal_strength') or 0)}</div>
                <div class="muted">{_concept_tr(lang, 'max_signal_strength_help')}</div>
              </article>
            </div>
          </section>
          <section class="card">
            <div class="eyebrow">Snapshot Window</div>
            <div class="compare-row">{lookback_pills}</div>
            <div class="eyebrow" style="margin-top:12px;">Signal Filter</div>
            <div class="compare-row">{signal_pills}</div>
            <form action="/dashboard/concepts/{concept_slug}" method="get" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));align-items:end;margin-top:12px;">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="hidden" name="sort_by" value="{sort_by}" />
              <input type="hidden" name="sort_order" value="{sort_order}" />
              <input type="hidden" name="comparison_sort" value="{comparison_sort}" />
              <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
              <input type="hidden" name="signal_filter" value="{signal_filter}" />
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Execution Tag" if lang == "en" else "执行提醒标签"}</label>
                <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Exclude Tag" if lang == "en" else "排除标签"}</label>
                <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div style="grid-column:1 / -1;">
                <div class="muted" style="margin-bottom:6px;">{"Quick Tags" if lang == "en" else "快捷标签"}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;">
                  <button type="button" onclick="appendExecutionTag('/dashboard/concepts/{concept_slug}', 'execution_tag_filter', 'gap-risk')">gap-risk</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/concepts/{concept_slug}', 'execution_tag_filter', 'earnings-soon')">earnings-soon</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/concepts/{concept_slug}', 'execution_tag_filter', 'thin-liquidity')">thin-liquidity</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/concepts/{concept_slug}', 'exclude_execution_tag_filter', 'gap-risk')">{"exclude gap-risk" if lang == "en" else "排除 gap-risk"}</button>
                  <button type="button" onclick="clearExecutionTags('/dashboard/concepts/{concept_slug}')">{"Clear Tags" if lang == "en" else "清空标签"}</button>
                </div>
              </div>
              <datalist id="execution-tag-options">
                <option value="gap-risk"></option>
                <option value="earnings-soon"></option>
                <option value="thin-liquidity"></option>
              </datalist>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Buy Count" if lang == "en" else "最少买点数"}</label>
                <input type="number" name="min_buy_signal_count" min="0" step="1" value="{min_buy_signal_count}" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Strength" if lang == "en" else "最低强度"}</label>
                <input type="number" name="min_signal_strength" min="0" max="100" step="1" value="{min_signal_strength}" />
              </div>
              <button type="submit">{_concept_tr(lang, 'apply_filters')}</button>
            </form>
            <div class="muted">Concept trend and delta are currently based on the most recent {lookback_runs} model snapshots.</div>
          </section>
          <section class="action-grid">
            <article class="card">
              <div class="eyebrow">{_concept_tr(lang, 'follow_this_concept')}</div>
              <form action="/dashboard/concepts/{concept_slug}/watchlist" method="post" class="stack">
                <input type="hidden" name="tickers_csv" value="{ticker_csv}" />
                <input type="hidden" name="lang" value="{lang}" />
                <label class="checkline">
                  <input type="checkbox" name="auto_enable_sync" value="1" />
                  {_concept_tr(lang, 'auto_enable_sync')}
                </label>
                <label class="checkline">
                  <input type="checkbox" name="sync_after_add" value="1" />
                  {_concept_tr(lang, 'sync_now')}
                </label>
                <button type="submit">{_concept_tr(lang, 'add_concept_stocks')}</button>
              </form>
            </article>
            <article class="card">
              <div class="eyebrow">{_concept_tr(lang, 'top_n_watch')}</div>
              <form action="/dashboard/concepts/{concept_slug}/watchlist-top" method="post" class="stack">
                <input type="hidden" name="tickers_csv" value="{sorted_ticker_csv}" />
                <input type="hidden" name="lang" value="{lang}" />
                <label class="muted">{_concept_tr(lang, 'top_n_help')}</label>
                <input type="number" name="top_n" min="1" max="{max(len(ticker_detail_rows), 1)}" value="{min(max(len(ticker_detail_rows), 1), 3)}" />
                <label class="checkline">
                  <input type="checkbox" name="auto_enable_sync" value="1" />
                  {_concept_tr(lang, 'auto_enable_sync')}
                </label>
                <label class="checkline">
                  <input type="checkbox" name="sync_after_add" value="1" />
                  {_concept_tr(lang, 'sync_selected_top_n')}
                </label>
                <button type="submit">{_concept_tr(lang, 'add_top_n')}</button>
              </form>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{_concept_tr(lang, 'top_movers_comparison')}</div>
            <div class="compare-row">{comparison_tabs}</div>
            <div class="mini-grid">{comparison_html}</div>
          </section>
          <section class="card">
            <div class="eyebrow">{_concept_tr(lang, 'ticker_breakdown')}</div>
            <div class="table-wrap"><table>
              <thead>
                <tr>
                  <th><a href="{_concept_sort_link(concept_slug, sort_by, sort_order, 'ticker', lang, comparison_sort)}&lookback_runs={lookback_runs}">{_concept_tr(lang, 'ticker')}{' ↓' if sort_by == 'ticker' and sort_order == 'desc' else ' ↑' if sort_by == 'ticker' else ''}</a></th>
                  <th><a href="{_concept_sort_link(concept_slug, sort_by, sort_order, 'name', lang, comparison_sort)}&lookback_runs={lookback_runs}">{_concept_tr(lang, 'name')}{' ↓' if sort_by == 'name' and sort_order == 'desc' else ' ↑' if sort_by == 'name' else ''}</a></th>
                  <th><a href="{_concept_sort_link(concept_slug, sort_by, sort_order, 'score', lang, comparison_sort)}&lookback_runs={lookback_runs}">{_concept_tr(lang, 'model_score')}{' ↓' if sort_by == 'score' and sort_order == 'desc' else ' ↑' if sort_by == 'score' else ''}</a></th>
                  <th><a href="{_concept_sort_link(concept_slug, sort_by, sort_order, 'five_day', lang, comparison_sort)}&lookback_runs={lookback_runs}">{_concept_tr(lang, 'five_day')}{' ↓' if sort_by == 'five_day' and sort_order == 'desc' else ' ↑' if sort_by == 'five_day' else ''}</a></th>
                  <th><a href="{_concept_sort_link(concept_slug, sort_by, sort_order, 'watchlist', lang, comparison_sort)}&lookback_runs={lookback_runs}">{_concept_tr(lang, 'watchlist')}{' ↓' if sort_by == 'watchlist' and sort_order == 'desc' else ' ↑' if sort_by == 'watchlist' else ''}</a></th>
                  <th><a href="{_concept_sort_link(concept_slug, sort_by, sort_order, 'last_sync', lang, comparison_sort)}&lookback_runs={lookback_runs}">{_concept_tr(lang, 'last_sync')}{' ↓' if sort_by == 'last_sync' and sort_order == 'desc' else ' ↑' if sort_by == 'last_sync' else ''}</a></th>
                  <th>{_concept_tr(lang, 'actions')}</th>
                </tr>
              </thead>
              <tbody>{ticker_rows}</tbody>
            </table></div>
          </section>
            </div>
          </main>
        </div>
        <script>
          function appendExecutionTag(formAction, inputName, tag) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const input = form.querySelector(`input[name="${{inputName}}"]`);
            if (!input) return;
            const values = input.value.split(",").map((item) => item.trim()).filter(Boolean);
            if (!values.includes(tag)) {{
              values.push(tag);
            }}
            input.value = values.join(", ");
            input.focus();
          }}

          function clearExecutionTags(formAction) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const includeInput = form.querySelector('input[name="execution_tag_filter"]');
            const excludeInput = form.querySelector('input[name="exclude_execution_tag_filter"]');
            if (includeInput) includeInput.value = "";
            if (excludeInput) excludeInput.value = "";
            if (includeInput) includeInput.focus();
          }}
        </script>
      </body>
    </html>
    """


@router.post("/concepts/{concept_slug}/watchlist")
def dashboard_concept_add_to_watchlist(
    request: Request,
    concept_slug: str,
    tickers_csv: str = Form(""),
    lang: str = Form("en"),
    auto_enable_sync: str | None = Form(None),
    sync_after_add: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/concepts/{concept_slug}")
    summary = _load_summary(db)
    concept = _get_concept_from_summary(summary, concept_slug)
    if concept is None:
        return RedirectResponse(url="/dashboard?job_status=failed&job_message=Concept+not+found", status_code=303)

    auto_sync_enabled = auto_enable_sync == "1"
    sync_now = sync_after_add == "1"
    added, already_in_watchlist, sync_enabled_count = _add_concept_tickers_to_watchlist(
        db=db,
        concept=concept,
        auto_enable_sync=auto_sync_enabled,
    )

    sync_message = ""
    if sync_now and tickers_csv.strip():
        tickers = [ticker.strip() for ticker in tickers_csv.split(",") if ticker.strip()]
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="auto")
        success_count = sum(1 for item in results if item.get("status") == "success")
        sync_message = f" · Synced {success_count}/{len(tickers)}"

    if added:
        message = f"Added {added} concept stock(s) to watchlist"
    elif already_in_watchlist:
        message = "All concept stocks are already in your watchlist"
    else:
        message = "No concept stocks available to add"
    if sync_enabled_count:
        message += f" · Sync enabled for {sync_enabled_count}"
    message += sync_message
    return RedirectResponse(
        url=f"/dashboard/concepts/{concept_slug}?{urlencode({'message': message, 'lang': lang})}",
        status_code=303,
    )


@router.post("/concepts/{concept_slug}/watchlist-top")
def dashboard_concept_add_top_to_watchlist(
    request: Request,
    concept_slug: str,
    tickers_csv: str = Form(""),
    top_n: int = Form(3),
    lang: str = Form("en"),
    auto_enable_sync: str | None = Form(None),
    sync_after_add: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/concepts/{concept_slug}")

    tickers = [ticker.strip() for ticker in tickers_csv.split(",") if ticker.strip()]
    if top_n > 0:
        tickers = tickers[:top_n]
    auto_sync_enabled = auto_enable_sync == "1"
    sync_now = sync_after_add == "1"

    added, already_in_watchlist, sync_enabled_count = _add_specific_tickers_to_watchlist(
        db=db,
        tickers=tickers,
        auto_enable_sync=auto_sync_enabled,
    )
    sync_message = ""
    if sync_now and tickers:
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="auto")
        success_count = sum(1 for item in results if item.get("status") == "success")
        sync_message = f" · Synced {success_count}/{len(tickers)}"

    if added:
        message = f"Added top {len(tickers)} concept stock(s) to watchlist"
    elif already_in_watchlist:
        message = "Selected concept stocks are already in your watchlist"
    else:
        message = "No concept stocks available to add"
    if sync_enabled_count:
        message += f" · Sync enabled for {sync_enabled_count}"
    message += sync_message
    return RedirectResponse(
        url=f"/dashboard/concepts/{concept_slug}?{urlencode({'message': message, 'lang': lang})}",
        status_code=303,
    )


@router.post("/concepts/{concept_slug}/ticker-action")
def dashboard_concept_ticker_action(
    request: Request,
    concept_slug: str,
    ticker: str = Form(...),
    action: str = Form(...),
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/concepts/{concept_slug}")
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    existing = watchlist_map.get(ticker)
    symbol = symbol_repo.get_by_ticker(ticker)

    if existing is None:
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=symbol.name if symbol else ticker,
                market=symbol.market if symbol else None,
                exchange=symbol.exchange if symbol else None,
            )
        )
        item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        existing = {
            "item_id": item.id,
            "ticker": ticker,
            "sync_enabled": 0,
        }

    if action == "sync":
        watchlist_repo.set_sync_enabled(existing["item_id"], True)
        results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="auto")
        result = results[0] if results else None
        if result and result.get("status") == "success":
            message = f"Synced {ticker} with {result['rows']} rows"
        elif result:
            message = f"Sync failed for {ticker}: {result.get('message', 'Unknown error')}"
        else:
            message = f"Sync did not return a result for {ticker}"
    else:
        message = f"Added {ticker} to watchlist"

    return RedirectResponse(
        url=f"/dashboard/concepts/{concept_slug}?{urlencode({'message': message, 'lang': lang})}",
        status_code=303,
    )


@router.post("/continuous-leaders/action")
def dashboard_continuous_leader_action(
    request: Request,
    ticker: str = Form(...),
    action: str = Form(...),
    lang: str = Form("en"),
    lookback_runs: int = Form(5),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    existing = watchlist_map.get(ticker)
    symbol = symbol_repo.get_by_ticker(ticker)

    if existing is None:
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=symbol.name if symbol else ticker,
                market=symbol.market if symbol else None,
                exchange=symbol.exchange if symbol else None,
            )
        )
        item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        existing = {
            "item_id": item.id,
            "ticker": ticker,
            "sync_enabled": 0,
        }

    if action == "sync":
        watchlist_repo.set_sync_enabled(existing["item_id"], True)
        results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="auto")
        result = results[0] if results else None
        if result and result.get("status") == "success":
            message = f"Synced {ticker} with {result['rows']} rows"
        elif result:
            message = f"Sync failed for {ticker}: {result.get('message', 'Unknown error')}"
        else:
            message = f"Sync did not return a result for {ticker}"
    else:
        message = f"Added {ticker} to watchlist"

    return RedirectResponse(
        url=f"/dashboard?{urlencode({'lang': lang, 'job_message': message, 'lookback_runs': lookback_runs})}",
        status_code=303,
    )


@router.post("/continuous-leaders/watchlist-top")
def dashboard_continuous_leaders_add_top(
    request: Request,
    tickers_csv: str = Form(""),
    top_n: int = Form(3),
    lang: str = Form("en"),
    lookback_runs: int = Form(5),
    auto_enable_sync: str | None = Form(None),
    sync_after_add: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    tickers = [ticker.strip() for ticker in tickers_csv.split(",") if ticker.strip()]
    if top_n > 0:
        tickers = tickers[:top_n]
    auto_sync_enabled = auto_enable_sync == "1"
    sync_now = sync_after_add == "1"

    added, already_in_watchlist, sync_enabled_count = _add_specific_tickers_to_watchlist(
        db=db,
        tickers=tickers,
        auto_enable_sync=auto_sync_enabled,
    )
    sync_message = ""
    if sync_now and tickers:
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="auto")
        success_count = sum(1 for item in results if item.get("status") == "success")
        sync_message = f" · Synced {success_count}/{len(tickers)}"

    if added:
        message = f"Added top {len(tickers)} continuous leader(s) to watchlist"
    elif already_in_watchlist:
        message = "Selected continuous leaders are already in your watchlist"
    else:
        message = "No continuous leaders available to add"
    if sync_enabled_count:
        message += f" · Sync enabled for {sync_enabled_count}"
    message += sync_message
    return RedirectResponse(
        url=f"/dashboard?{urlencode({'lang': lang, 'job_message': message, 'lookback_runs': lookback_runs})}",
        status_code=303,
    )


@router.get("/continuous-leaders", response_class=HTMLResponse)
def dashboard_continuous_leaders_page(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    continuous_sort_by: str = "hits",
    continuous_sort_order: str = "desc",
    continuous_market: str = "ALL",
    continuous_state: str = "ALL",
    continuous_signal: str = "ALL",
    min_signal_strength: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/continuous-leaders")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    continuous_market = continuous_market.upper()
    continuous_state = continuous_state.upper()
    continuous_signal = continuous_signal.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    continuous_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_CONTINUOUS_LEADERS)
    continuous_rows_snapshot = ((continuous_snapshot or {}).get("payload") or {}).get("rows") if isinstance(continuous_snapshot, dict) else None
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)

    rows_source = list(continuous_rows_snapshot or summary["market_context"].get("continuous_leaders", []))
    for item in rows_source:
        existing = watchlist_map.get(item["ticker"])
        if existing is None:
            state_key = "OFF"
        elif existing.get("sync_enabled") and existing.get("sync_status") == "success":
            state_key = "READY"
        elif existing.get("sync_enabled"):
            state_key = "WAITING"
        else:
            state_key = "IN"
        item["continuous_state_key"] = state_key

    if continuous_market != "ALL":
        rows_source = [item for item in rows_source if item.get("market") == continuous_market]
    if continuous_state != "ALL":
        rows_source = [item for item in rows_source if item.get("continuous_state_key") == continuous_state]
    if continuous_signal != "ALL":
        rows_source = [
            item for item in rows_source
            if str(item.get("signal_label") or "").strip().upper() == continuous_signal
        ]
    if min_signal_strength > 0:
        rows_source = [
            item for item in rows_source
            if int(item.get("signal_strength") or 0) >= min_signal_strength
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        rows_source = [
            item for item in rows_source
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        rows_source = [
            item for item in rows_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, object]] = []
    tagged_names = 0
    for item in rows_source:
        tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        tagged_names += 1
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        risk_examples.append({"ticker": item.get("ticker"), "tags": tags[:2]})
    risk_examples = risk_examples[:3]
    risk_top_tags = sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]

    def sort_rank(item: dict) -> tuple:
        if continuous_sort_by == "ticker":
            return (item["ticker"],)
        if continuous_sort_by == "score":
            return (float(item.get("score") or 0.0), item["ticker"])
        if continuous_sort_by == "signal":
            return (float(item.get("signal_strength") or 0.0), item["ticker"])
        if continuous_sort_by == "trend":
            history = item.get("score_history") or []
            last_delta = (history[-1] - history[0]) if len(history) >= 2 else 0.0
            return (float(last_delta), item["ticker"])
        return (int(item.get("hits") or 0), float(item.get("score") or 0.0), item["ticker"])

    rows_source.sort(key=sort_rank, reverse=continuous_sort_order != "asc")

    def sort_link(field: str) -> str:
        next_order = "asc" if continuous_sort_by == field and continuous_sort_order == "desc" else "desc"
        return "/dashboard/continuous-leaders?" + urlencode(
            {
                "lang": lang,
                "lookback_runs": lookback_runs,
                "continuous_sort_by": field,
                "continuous_sort_order": next_order,
                "continuous_market": continuous_market,
                "continuous_state": continuous_state,
                "continuous_signal": continuous_signal,
                "min_signal_strength": min_signal_strength,
                "execution_tag_filter": execution_tag_filter,
                "exclude_execution_tag_filter": exclude_execution_tag_filter,
            }
        )

    rows_html_parts: list[str] = []
    for item in rows_source:
        existing = watchlist_map.get(item["ticker"])
        state_label, state_bg, state_fg = _concept_ticker_watch_state(watchlist_map, item["ticker"], lang)
        if existing is None:
            action_html = (
                "<form action='/dashboard/continuous-leaders/action' method='post' style='display:inline-block;margin:0;'>"
                f"<input type='hidden' name='ticker' value='{item['ticker']}' />"
                "<input type='hidden' name='action' value='add' />"
                f"<input type='hidden' name='lookback_runs' value='{lookback_runs}' />"
                "<button type='submit' style='padding:8px 10px;font-size:12px;'>"
                f"{_concept_tr(lang, 'add')}"
                "</button></form>"
            )
        elif existing.get("sync_enabled") and existing.get("sync_status") == "success":
            action_html = f"<a href='/insights/{item['ticker']}?lang={lang}' class='action-link'>{_concept_tr(lang, 'open')}</a>"
        else:
            action_html = (
                "<form action='/dashboard/continuous-leaders/action' method='post' style='display:inline-block;margin:0;'>"
                f"<input type='hidden' name='ticker' value='{item['ticker']}' />"
                "<input type='hidden' name='action' value='sync' />"
                f"<input type='hidden' name='lookback_runs' value='{lookback_runs}' />"
                "<button type='submit' style='padding:8px 10px;font-size:12px;'>"
                f"{_concept_tr(lang, 'sync')}"
                "</button></form>"
            )
        rows_html_parts.append(
            "<tr>"
            f"<td><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td>"
            f"<td>{item['name']}</td>"
            f"<td>{item['market']}</td>"
            f"<td>{item['hits']}/{item['runs']}</td>"
            f"<td><div>{item['score']:.4f}</div><div style='margin-top:6px;'>{_dashboard_model_badge(item.get('state'), confidence=item.get('confidence'), compact=True)}</div><div style='margin-top:6px;font-size:12px;color:#6b7280;'>{('Pct ' + format(float(item.get('percentile')), '.1f') + '%') if item.get('percentile') is not None else ''}{(' · ' if item.get('percentile') is not None and item.get('model_reward_risk_ratio') is not None else '')}{('R/R ' + format(float(item.get('model_reward_risk_ratio')), '.2f')) if item.get('model_reward_risk_ratio') is not None else ''}{(' · ' if (item.get('percentile') is not None or item.get('model_reward_risk_ratio') is not None) and item.get('conviction_bucket') else '')}{item.get('conviction_bucket') or ''}{(' · ' if item.get('position_size_hint') and (item.get('percentile') is not None or item.get('model_reward_risk_ratio') is not None or item.get('conviction_bucket')) else '')}{item.get('position_size_hint') or ''}{(' · ' if item.get('entry_style') and (item.get('percentile') is not None or item.get('model_reward_risk_ratio') is not None or item.get('conviction_bucket') or item.get('position_size_hint')) else '')}{item.get('entry_style') or ''}{(' · ' if item.get('execution_tags') and (item.get('percentile') is not None or item.get('model_reward_risk_ratio') is not None or item.get('conviction_bucket') or item.get('position_size_hint') or item.get('entry_style')) else '')}{' / '.join((item.get('execution_tags') or [])[:2])}</div></td>"
            f"<td>{item.get('signal_label') or ('Hold' if lang == 'en' else '持有')} · {int(item.get('signal_strength') or 0)}</td>"
            f"<td>{_score_sparkline_svg(item.get('score_history', []))}</td>"
            f"<td>{item.get('trade_date') or '-'}</td>"
            f"<td><span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;background:{state_bg};color:{state_fg};font-size:12px;font-weight:800;white-space:nowrap;'>{state_label}</span></td>"
            f"<td>{action_html}</td>"
            "</tr>"
        )
    rows_html = "".join(rows_html_parts) or "<tr><td colspan='10'>No continuous leaders yet</td></tr>"
    tickers_csv = ",".join(item["ticker"] for item in rows_source)
    lookback_pills = _lookback_pills(
        "/dashboard/continuous-leaders",
        selected=lookback_runs,
        extra_params={
            "lang": lang,
            "continuous_sort_by": continuous_sort_by,
            "continuous_sort_order": continuous_sort_order,
            "continuous_market": continuous_market,
            "continuous_state": continuous_state,
            "continuous_signal": continuous_signal,
            "min_signal_strength": min_signal_strength,
            "execution_tag_filter": execution_tag_filter,
            "exclude_execution_tag_filter": exclude_execution_tag_filter,
        },
    )
    lang_switch = (
        f"<div style='display:flex;gap:8px;align-items:center;margin-top:12px;'>"
        f"<a href='/dashboard/continuous-leaders?{urlencode({'lang': 'en', 'lookback_runs': lookback_runs, 'continuous_sort_by': continuous_sort_by, 'continuous_sort_order': continuous_sort_order, 'continuous_market': continuous_market, 'continuous_state': continuous_state, 'continuous_signal': continuous_signal, 'min_signal_strength': min_signal_strength, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if lang != 'zh' else ''}'>{_concept_tr('en', 'lang_en')}</a>"
        f"<a href='/dashboard/continuous-leaders?{urlencode({'lang': 'zh', 'lookback_runs': lookback_runs, 'continuous_sort_by': continuous_sort_by, 'continuous_sort_order': continuous_sort_order, 'continuous_market': continuous_market, 'continuous_state': continuous_state, 'continuous_signal': continuous_signal, 'min_signal_strength': min_signal_strength, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if lang == 'zh' else ''}'>{_concept_tr('zh', 'lang_zh')}</a>"
        "</div>"
    )
    signal_options = "".join(
        f"<option value='{value}' {'selected' if continuous_signal == value else ''}>{label}</option>"
        for value, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    risk_top_tags_html = "".join(
        f"<span class='compare-pill'>{tag} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_dt(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['ticker']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{_concept_tr(lang, 'continuous_detail')}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-soft:rgba(61,217,182,0.12); }}
          * {{ box-sizing: border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at top right, rgba(61,217,182,0.12), transparent 24%),linear-gradient(180deg,#08111a 0%,#071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:1120px; margin:0 auto; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .metric {{ font-size: 30px; font-weight: 800; margin: 8px 0; color:var(--ink); }}
          .muted {{ color: var(--muted); font-size: 14px; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:16px; }}
          .compare-row,.toolbar {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; align-items:center; }}
          .compare-pill,.pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); text-decoration:none; font-size:12px; font-weight:800; }}
          .compare-pill.active,.pill.active {{ background:rgba(61,217,182,0.14); color:var(--ink); border-color:rgba(61,217,182,0.28); }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }}
          table {{ width:100%; min-width:980px; border-collapse:collapse; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); white-space:nowrap; vertical-align:top; }}
          th {{ color: var(--muted); font-weight: 600; }}
          .stack {{ display:grid; gap:12px; }}
          input, button, select {{ border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; background:#0f1823; color:var(--ink); }}
          button {{ background:var(--accent); color:#041119; border-color:var(--accent); font-weight:800; }}
          .checkbox-row {{ display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }}
          .action-link {{ display:inline-flex; align-items:center; padding:10px 12px; border-radius:12px; background:rgba(61,217,182,0.10); color:var(--accent); font-weight:700; }}
          h1 {{ margin:0 0 8px; font-size:38px; line-height:1.05; letter-spacing:-0.03em; }}
          .sidebar-foot {{ margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }}
          @media (max-width: 1120px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .content {{ padding:20px 16px 36px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{_concept_tr(lang, 'continuous_leaders')}</h1>
              <p>{'查看最近几次模型快照里持续入选、持续走强的股票，并快速加入自选。' if lang == 'zh' else 'Review names that keep recurring across recent model snapshots and move them into the watchlist quickly.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'这页聚焦连续入选和持续走强的股票，适合做盘前优先级排序。' if lang == 'zh' else 'This page focuses on names that keep recurring and staying strong, useful for pre-market prioritization.'}</div>
          </aside>
          <main class="content">
        <div class="wrap">
          <div class="toolbar">
            <a href="/dashboard?lang={lang}" class="pill">← {_concept_tr(lang, 'back_to_dashboard')}</a>
            <a href="/watchlist?lang={lang}" class="pill">{'观察池' if lang == 'zh' else 'Watchlist'}</a>
            <a href="/dashboard/market?lang={lang}&lookback_runs={lookback_runs}" class="pill">{'市场概览' if lang == 'zh' else 'Market Overview'}</a>
            {lang_switch}
          </div>
          <div class="card">
            <div class="eyebrow">{_concept_tr(lang, 'continuous_leaders')}</div>
            <h1>{_concept_tr(lang, 'continuous_detail')}</h1>
            <div class="muted">{_concept_tr(lang, 'continuous_subtitle')}</div>
          </div>
          <section class="card">
            <div class="eyebrow">Snapshot Window</div>
            <div class="compare-row">{lookback_pills}</div>
          </section>
          <section class="card">
            <form action="/dashboard/continuous-leaders" method="get" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));align-items:end;">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
              <input type="hidden" name="continuous_sort_by" value="{continuous_sort_by}" />
              <input type="hidden" name="continuous_sort_order" value="{continuous_sort_order}" />
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{_concept_tr(lang, 'market_filter')}</label>
                <select name="continuous_market">
                  <option value="ALL" {'selected' if continuous_market == 'ALL' else ''}>All</option>
                  <option value="CN" {'selected' if continuous_market == 'CN' else ''}>CN</option>
                  <option value="HK" {'selected' if continuous_market == 'HK' else ''}>HK</option>
                  <option value="US" {'selected' if continuous_market == 'US' else ''}>US</option>
                </select>
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{_concept_tr(lang, 'state_filter')}</label>
                <select name="continuous_state">
                  <option value="ALL" {'selected' if continuous_state == 'ALL' else ''}>All</option>
                  <option value="READY" {'selected' if continuous_state == 'READY' else ''}>Ready</option>
                  <option value="WAITING" {'selected' if continuous_state == 'WAITING' else ''}>Waiting</option>
                  <option value="OFF" {'selected' if continuous_state == 'OFF' else ''}>Off</option>
                </select>
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Signal" if lang == "en" else "信号"}</label>
                <select name="continuous_signal">
                  {signal_options}
                </select>
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Strength" if lang == "en" else "最低强度"}</label>
                <input type="number" name="min_signal_strength" min="0" max="100" step="1" value="{min_signal_strength}" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Execution Tag" if lang == "en" else "执行提醒标签"}</label>
                <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Exclude Tag" if lang == "en" else "排除标签"}</label>
                <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div style="grid-column:1 / -1;">
                <div class="muted" style="margin-bottom:6px;">{"Quick Tags" if lang == "en" else "快捷标签"}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;">
                  <button type="button" onclick="appendExecutionTag('/dashboard/continuous-leaders', 'execution_tag_filter', 'gap-risk')">gap-risk</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/continuous-leaders', 'execution_tag_filter', 'earnings-soon')">earnings-soon</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/continuous-leaders', 'execution_tag_filter', 'thin-liquidity')">thin-liquidity</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/continuous-leaders', 'exclude_execution_tag_filter', 'gap-risk')">{"exclude gap-risk" if lang == "en" else "排除 gap-risk"}</button>
                  <button type="button" onclick="clearExecutionTags('/dashboard/continuous-leaders')">{"Clear Tags" if lang == "en" else "清空标签"}</button>
                </div>
              </div>
              <datalist id="execution-tag-options">
                <option value="gap-risk"></option>
                <option value="earnings-soon"></option>
                <option value="thin-liquidity"></option>
              </datalist>
              <button type="submit">{_concept_tr(lang, 'apply_filters')}</button>
            </form>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'risk_overview')}</div>
            <div class="grid">
              <article class="card" style="margin:0;background:#f9f7f0;">
                <div class="eyebrow">{_dt(lang, 'tagged_names')}</div>
                <div class="metric">{tagged_names}</div>
                <div class="muted">{_dt(lang, 'risk_examples')}</div>
              </article>
              <article class="card" style="margin:0;background:#f9f7f0;">
                <div class="eyebrow">{_dt(lang, 'common_risks')}</div>
                <div class="compare-row" style="margin-bottom:8px;">{risk_top_tags_html}</div>
                <div class="muted">{_dt(lang, 'risk_examples')}: {risk_examples_html}</div>
              </article>
            </div>
          </section>
          <section class="card">
            <form action="/dashboard/continuous-leaders/export" method="get" style="display:flex;justify-content:flex-end;gap:8px;margin-bottom:14px;flex-wrap:wrap;">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
              <input type="hidden" name="continuous_sort_by" value="{continuous_sort_by}" />
              <input type="hidden" name="continuous_sort_order" value="{continuous_sort_order}" />
              <input type="hidden" name="continuous_market" value="{continuous_market}" />
              <input type="hidden" name="continuous_state" value="{continuous_state}" />
              <input type="hidden" name="continuous_signal" value="{continuous_signal}" />
              <input type="hidden" name="min_signal_strength" value="{min_signal_strength}" />
              <input type="hidden" name="execution_tag_filter" value="{execution_tag_filter}" />
              <input type="hidden" name="exclude_execution_tag_filter" value="{exclude_execution_tag_filter}" />
              <button type="submit">Export CSV</button>
            </form>
            <form action="/dashboard/continuous-leaders/watchlist-top" method="post" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));align-items:end;margin-bottom:16px;">
              <input type="hidden" name="tickers_csv" value="{tickers_csv}" />
              <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">Top N</label>
                <input type="number" name="top_n" min="1" max="{max(len(rows_source), 1)}" value="{min(max(len(rows_source), 1), 3)}" />
              </div>
              <label class="checkbox-row">
                <input type="checkbox" name="auto_enable_sync" value="1" />
                {_concept_tr(lang, 'auto_enable_sync')}
              </label>
              <label class="checkbox-row">
                <input type="checkbox" name="sync_after_add" value="1" />
                {_concept_tr(lang, 'sync_selected_top_n')}
              </label>
              <button type="submit">{_concept_tr(lang, 'add_top_n')}</button>
            </form>
            <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th><a href="{sort_link('ticker')}">{_concept_tr(lang, 'ticker')}</a></th>
                  <th>{_concept_tr(lang, 'name')}</th>
                  <th>{_concept_tr(lang, 'market_filter')}</th>
                  <th><a href="{sort_link('hits')}">{_concept_tr(lang, 'hits')}</a></th>
                  <th><a href="{sort_link('score')}">{_concept_tr(lang, 'model_score')}</a></th>
                  <th><a href="{sort_link('signal')}">Signal</a></th>
                  <th><a href="{sort_link('trend')}">{_concept_tr(lang, 'trend')}</a></th>
                  <th>{_concept_tr(lang, 'last')}</th>
                  <th>{_concept_tr(lang, 'watchlist')}</th>
                  <th>{_concept_tr(lang, 'actions')}</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            </div>
          </section>
        </div>
          </main>
        </div>
        <script>
          function appendExecutionTag(formAction, inputName, tag) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const input = form.querySelector(`input[name="${{inputName}}"]`);
            if (!input) return;
            const values = input.value.split(",").map((item) => item.trim()).filter(Boolean);
            if (!values.includes(tag)) {{
              values.push(tag);
            }}
            input.value = values.join(", ");
            input.focus();
          }}

          function clearExecutionTags(formAction) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const includeInput = form.querySelector('input[name="execution_tag_filter"]');
            const excludeInput = form.querySelector('input[name="exclude_execution_tag_filter"]');
            if (includeInput) includeInput.value = "";
            if (excludeInput) excludeInput.value = "";
            if (includeInput) includeInput.focus();
          }}
        </script>
      </body>
    </html>
    """


@router.get("/continuous-leaders/export")
def dashboard_continuous_leaders_export(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    continuous_sort_by: str = "hits",
    continuous_sort_order: str = "desc",
    continuous_market: str = "ALL",
    continuous_state: str = "ALL",
    continuous_signal: str = "ALL",
    min_signal_strength: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    db: Session = Depends(get_db_session),
) -> Response:
    if not is_authenticated(request):
        return login_redirect("/dashboard/continuous-leaders")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    continuous_market = continuous_market.upper()
    continuous_state = continuous_state.upper()
    continuous_signal = continuous_signal.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    continuous_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_CONTINUOUS_LEADERS)
    continuous_rows_snapshot = ((continuous_snapshot or {}).get("payload") or {}).get("rows") if isinstance(continuous_snapshot, dict) else None
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    rows_source = list(continuous_rows_snapshot or summary["market_context"].get("continuous_leaders", []))
    for item in rows_source:
        existing = watchlist_map.get(item["ticker"])
        if existing is None:
            item["continuous_state_key"] = "OFF"
        elif existing.get("sync_enabled") and existing.get("sync_status") == "success":
            item["continuous_state_key"] = "READY"
        elif existing.get("sync_enabled"):
            item["continuous_state_key"] = "WAITING"
        else:
            item["continuous_state_key"] = "IN"
    if continuous_market != "ALL":
        rows_source = [item for item in rows_source if item.get("market") == continuous_market]
    if continuous_state != "ALL":
        rows_source = [item for item in rows_source if item.get("continuous_state_key") == continuous_state]
    if continuous_signal != "ALL":
        rows_source = [
            item for item in rows_source
            if str(item.get("signal_label") or "").strip().upper() == continuous_signal
        ]
    if min_signal_strength > 0:
        rows_source = [
            item for item in rows_source
            if int(item.get("signal_strength") or 0) >= min_signal_strength
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        rows_source = [
            item for item in rows_source
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        rows_source = [
            item for item in rows_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]

    def sort_rank(item: dict) -> tuple:
        if continuous_sort_by == "ticker":
            return (item["ticker"],)
        if continuous_sort_by == "score":
            return (float(item.get("score") or 0.0), item["ticker"])
        if continuous_sort_by == "signal":
            return (float(item.get("signal_strength") or 0.0), item["ticker"])
        if continuous_sort_by == "trend":
            history = item.get("score_history") or []
            last_delta = (history[-1] - history[0]) if len(history) >= 2 else 0.0
            return (float(last_delta), item["ticker"])
        return (int(item.get("hits") or 0), float(item.get("score") or 0.0), item["ticker"])

    rows_source.sort(key=sort_rank, reverse=continuous_sort_order != "asc")

    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "ticker",
            "name",
            "market",
            "hits",
            "runs",
            "score",
            "signal_label",
            "signal_strength",
            "conviction_bucket",
            "position_size_hint",
            "entry_style",
            "execution_tags",
            "percentile",
            "target_horizon_days",
            "expected_drawdown_20d",
            "model_reward_risk_ratio",
            "trade_date",
            "continuous_state",
        ],
    )
    writer.writeheader()
    for item in rows_source:
        writer.writerow(
            {
                "ticker": item["ticker"],
                "name": item["name"],
                "market": item["market"],
                "hits": item["hits"],
                "runs": item["runs"],
                "score": item["score"],
                "signal_label": item.get("signal_label"),
                "signal_strength": item.get("signal_strength"),
                "conviction_bucket": item.get("conviction_bucket"),
                "position_size_hint": item.get("position_size_hint"),
                "entry_style": item.get("entry_style"),
                "execution_tags": ";".join(item.get("execution_tags") or []),
                "percentile": item.get("percentile"),
                "target_horizon_days": item.get("target_horizon_days"),
                "expected_drawdown_20d": item.get("expected_drawdown_20d"),
                "model_reward_risk_ratio": item.get("model_reward_risk_ratio"),
                "trade_date": item.get("trade_date"),
                "continuous_state": item.get("continuous_state_key"),
            }
        )
    filename = f"continuous_leaders_{lookback_runs}runs.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/market", response_class=HTMLResponse)
def dashboard_market_page(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    heatmap_sort: str = "hits",
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/market")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    latest_signals = list(summary.get("latest_signals") or [])
    filtered_signals = []
    for item in latest_signals:
        label = str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper()
        if signal_filter != "ALL" and label != signal_filter:
            continue
        if min_signal_strength > 0 and int(item.get("signal_strength") or 0) < min_signal_strength:
            continue
        tags = item.get("risk_flags") or item.get("execution_tags") or []
        if execution_tag_filter and execution_tag_filter.upper() != "ALL" and not _matches_execution_tag_filter(tags, execution_tag_filter):
            continue
        if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL" and not _excludes_execution_tag_filter(tags, exclude_execution_tag_filter):
            continue
        filtered_signals.append(item)

    market_counts: dict[str, int] = {}
    tagged_names = 0
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, object]] = []
    for item in filtered_signals:
        market = str(item.get("market") or "OTHER").upper()
        market_counts[market] = market_counts.get(market, 0) + 1
        tags = [str(tag).strip() for tag in (item.get("risk_flags") or item.get("execution_tags") or []) if str(tag).strip()]
        if tags:
            tagged_names += 1
            for tag in tags:
                risk_counts[tag] = risk_counts.get(tag, 0) + 1
            risk_examples.append({"label": item.get("ticker") or "-", "tags": tags[:2]})
    risk_examples = risk_examples[:3]
    risk_top_tags = sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    market_rows = "".join(
        f"<tr><td>{market}</td><td>{count}</td></tr>"
        for market, count in sorted(market_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ) or f"<tr><td colspan='2'>{'暂无信号分布' if lang == 'zh' else 'No signal distribution yet'}</td></tr>"

    market_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_WORKSPACE)
    market_snapshot_payload = (market_snapshot or {}).get("payload") if isinstance(market_snapshot, dict) else None
    snapshot_boards = (market_snapshot_payload or {}).get("rows") if isinstance(market_snapshot_payload, dict) else None
    market_monitor_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_WORKSPACE_MONITOR)
    snapshot_ready = isinstance(market_monitor_snapshot, dict) and isinstance(snapshot_boards, list) and bool(snapshot_boards)
    if not snapshot_ready:
        snapshot_boards = []
    board_preview_html = "".join(
        "<article class='card'>"
        f"<div class='eyebrow'>{board.get('title_zh') if lang == 'zh' else board.get('title_en')}</div>"
        f"<div class='muted'>{board.get('description_zh') if lang == 'zh' else board.get('description_en')}</div>"
        f"<div style='margin-top:12px;font-size:28px;font-weight:800;'>{len(board.get('rows') or [])}</div>"
        f"<div class='muted'>{'当前候选数' if lang == 'zh' else 'Current candidates'}</div>"
        "</article>"
        for board in snapshot_boards[:4]
    ) or f"<div class='muted'>{'市场快照仍在后台预计算，稍后刷新即可。' if lang == 'zh' else 'Market snapshot boards are still being precomputed in the background. Refresh shortly.'}</div>"

    top_signal_rows = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('trade_date') or '-'}</div><div class='subtle'>{_compact_label(item.get('reason_summary') or item.get('name') or '-', 72)}</div></div>"
        f"<div class='row-right'><span class='signal {_dashboard_home_signal(item.get('score'), lang)[1]}'>{item.get('signal_label') or _dashboard_home_signal(item.get('score'), lang)[0]}</span><div class='mini-metric'>{int(item.get('signal_strength') or 0)}</div></div>"
        "</article>"
        for item in filtered_signals[:5]
    ) or f"<div class='empty'>{'暂无符合条件的候选' if lang == 'zh' else 'No candidates match the current focus'}</div>"

    lookback_pills = _lookback_pills("/dashboard/market", selected=lookback_runs, extra_params={"lang": lang, "heatmap_sort": heatmap_sort, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter})
    signal_pills = "".join(
        f"<a href='/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
        for mode, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    risk_top_tags_html = "".join(
        f"<span class='compare-pill'>{tag} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_dt(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['label']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    board_count = sum(len(board.get("rows") or []) for board in snapshot_boards)
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'市场脉冲' if lang == 'zh' else 'Market Pulse'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-soft:rgba(61,217,182,0.12); }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:1120px; margin:0 auto; }}
          .toolbar,.compare-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.05em; text-transform:uppercase; margin-bottom:12px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .pill,.compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); font-size:13px; font-weight:700; text-decoration:none; }}
          .compare-pill.active, .pill.active {{ background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }}
          .signal-row {{
            display:flex; justify-content:space-between; gap:12px; align-items:center;
            padding:14px; border-radius:16px; background:rgba(11,19,29,0.82); border:1px solid rgba(34,50,70,0.92);
          }}
          .row-right {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }}
          .ticker {{ font-weight:800; font-size:15px; }}
          .subtle {{ color:var(--muted); font-size:12px; margin-top:4px; }}
          .signal {{ display:inline-flex; align-items:center; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; }}
          .sig-buy {{ background:rgba(74,222,128,0.14); color:#8af0a6; }}
          .sig-sell {{ background:rgba(255,107,129,0.14); color:#ff93a4; }}
          .sig-watch {{ background:rgba(82,168,255,0.14); color:#89c2ff; }}
          .sig-hold {{ background:rgba(246,200,95,0.14); color:#ffd982; }}
          .mini-metric {{ font-weight:800; font-size:13px; color:var(--ink); }}
          .empty {{ padding:18px; border-radius:16px; background:rgba(11,19,29,0.65); border:1px dashed var(--line); color:var(--muted); font-size:13px; }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }}
          table {{ width:100%; min-width:640px; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }}
          th {{ color:var(--muted); font-weight:600; }}
          h1 {{ margin:0 0 8px; font-size:38px; line-height:1.04; letter-spacing:-0.03em; }}
          input, button {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); font:inherit; }}
          button {{ width:auto; background:var(--accent); color:#041119; font-weight:800; cursor:pointer; }}
          .sidebar-foot {{ margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }}
          @media (max-width: 1100px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'市场概览' if lang == 'zh' else 'Market Overview'}</h1>
              <p>{'先看市场节奏、板块热力和概念共振，再决定是否进入更细的热力图或概念追踪页。' if lang == 'zh' else 'Review market tone, sector heat, and concept resonance before drilling into deeper heatmap or concept views.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'这页只保留市场工作流入口与摘要，详细板块和概念页继续往下看。' if lang == 'zh' else 'This page keeps the market workflow summary and entry points while deeper pages handle the detail.'}</div>
          </aside>
          <main class="main">
        <div class="wrap">
          <div class="toolbar">
            <a href="/dashboard?lang={lang}" class="pill">← {'返回总览' if lang == 'zh' else 'Back to dashboard'}</a>
            <a href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}" class="pill">{'运维操作台' if lang == 'zh' else 'Operations'}</a>
            <a href="/dashboard/market?lang=en&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">English</a>
            <a href="/dashboard/market?lang=zh&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">中文</a>
          </div>
          <div class="card">
            <div class="eyebrow">{'市场脉冲' if lang == 'zh' else 'Market Pulse'}</div>
            <h1 style="margin:0 0 8px;">{'市场脉冲总览' if lang == 'zh' else 'Market Pulse Hub'}</h1>
            <p class="muted">{'把板块热力图和概念异动追踪拆成独立页面，这里只保留市场总览和入口。' if lang == 'zh' else 'Heatmaps and concept tracking now live on dedicated pages. This page keeps the overview and navigation.'}</p>
          </div>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'sector_heatmap')}</div>
              <div style="font-size:28px;font-weight:800;margin:6px 0;">{board_count}</div>
              <div class="muted">{'市场快照候选总数，适合先粗看盘面。' if lang == 'zh' else 'Total snapshot candidates for a quick market scan.'}</div>
              <div style="margin-top:12px;"><a class="pill" href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}">{'打开板块热力图' if lang == 'zh' else 'Open Sector Heatmap'}</a></div>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'concept_activity_tracker')}</div>
              <div style="font-size:28px;font-weight:800;margin:6px 0;">{len(filtered_signals)}</div>
              <div class="muted">{'当前焦点筛选下的候选数量。' if lang == 'zh' else 'Candidate count under the current focus filters.'}</div>
              <div style="margin-top:12px;"><a class="pill" href="/dashboard/market/concepts?lang={lang}&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}">{'打开概念追踪' if lang == 'zh' else 'Open Concept Tracker'}</a></div>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'snapshot_window')}</div>
            <div class="compare-row">{lookback_pills}</div>
            <div class="eyebrow" style="margin-top:12px;">{"Signal Focus" if lang == "en" else "信号聚焦"}</div>
            <div class="compare-row">{signal_pills}</div>
            <form action="/dashboard/market" method="get" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));align-items:end;">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
              <input type="hidden" name="heatmap_sort" value="{heatmap_sort}" />
              <input type="hidden" name="signal_filter" value="{signal_filter}" />
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Execution Tag" if lang == "en" else "执行提醒标签"}</label>
                <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Exclude Tag" if lang == "en" else "排除标签"}</label>
                <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div style="grid-column:1 / -1;">
                <div class="muted" style="margin-bottom:6px;">{"Quick Tags" if lang == "en" else "快捷标签"}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;">
                  <button type="button" onclick="appendExecutionTag('/dashboard/market', 'execution_tag_filter', 'gap-risk')">gap-risk</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market', 'execution_tag_filter', 'earnings-soon')">earnings-soon</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market', 'execution_tag_filter', 'thin-liquidity')">thin-liquidity</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market', 'exclude_execution_tag_filter', 'gap-risk')">{"exclude gap-risk" if lang == "en" else "排除 gap-risk"}</button>
                  <button type="button" onclick="clearExecutionTags('/dashboard/market')">{"Clear Tags" if lang == "en" else "清空标签"}</button>
                </div>
              </div>
              <datalist id="execution-tag-options">
                <option value="gap-risk"></option>
                <option value="earnings-soon"></option>
                <option value="thin-liquidity"></option>
              </datalist>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Buy Count" if lang == "en" else "最少买点数"}</label>
                <input type="number" name="min_buy_signal_count" min="0" step="1" value="{min_buy_signal_count}" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Strength" if lang == "en" else "最低强度"}</label>
                <input type="number" name="min_signal_strength" min="0" max="100" step="1" value="{min_signal_strength}" />
              </div>
              <button type="submit">{_concept_tr(lang, 'apply_filters')}</button>
            </form>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'risk_overview')}</div>
            <div class="grid">
              <article class="card">
                <div class="eyebrow">{_dt(lang, 'tagged_names')}</div>
                <div style="font-size:28px;font-weight:800;margin:6px 0;">{tagged_names}</div>
                <div class="muted">{_dt(lang, 'risk_examples')}</div>
              </article>
              <article class="card">
                <div class="eyebrow">{_dt(lang, 'common_risks')}</div>
                <div class="compare-row" style="margin-bottom:8px;">{risk_top_tags_html}</div>
                <div class="muted">{_dt(lang, 'risk_examples')}: {risk_examples_html}</div>
              </article>
            </div>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'concept_resonance')}</div>
            <div style="font-size:32px;font-weight:800;margin:6px 0;">{len(risk_top_tags)}</div>
            <div class="muted">{'这里先看执行风险是否集中，详细概念共振留给概念追踪页。' if lang == 'zh' else 'Use this page to spot execution-risk concentration first; leave full concept resonance for the concept tracker.'}</div>
            <div class="muted" style="margin-top:8px;">{_dt(lang, 'tracked_signals')}: {len(filtered_signals)}</div>
          </section>
          <section class="card">
            <div class="eyebrow">{'市场入口' if lang == 'zh' else 'Market Shortcuts'}</div>
            <div class="compare-row">
              <a href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="compare-pill active">{_dt(lang, 'sector_heatmap')}</a>
              <a href="/dashboard/market/concepts?lang={lang}&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="compare-pill">{_dt(lang, 'concept_activity_tracker')}</a>
            </div>
          </section>
          <section class="card">
            <div class="eyebrow">{'市场快照预览' if lang == 'zh' else 'Snapshot Preview'}</div>
            <div class="grid">{board_preview_html}</div>
            <div class="muted" style="margin-top:10px;"><a href="/screeners/market-snapshot?lang={lang}">{'打开市场快照榜单 →' if lang == 'zh' else 'Open market snapshot boards →'}</a></div>
          </section>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'signal_distribution')}</div>
              <table>
                <thead><tr><th>{_dt(lang, 'market')}</th><th>{_dt(lang, 'top_n_hits')}</th></tr></thead>
                <tbody>{market_rows}</tbody>
              </table>
            </article>
            <article class="card">
              <div class="eyebrow">{'轻量候选预览' if lang == 'zh' else 'Candidate Preview'}</div>
              <div class="muted">{'先看最强的几只票，完整榜单留给热力图和概念页。' if lang == 'zh' else 'Review a few strongest names here and leave the full list to the heatmap and concept pages.'}</div>
              <div style="margin-top:12px;display:grid;gap:10px;">{top_signal_rows}</div>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{'下一步怎么走' if lang == 'zh' else 'Suggested Next Steps'}</div>
            <div class="grid">
              <article class="card">
                <div class="eyebrow">{'看热力' if lang == 'zh' else 'Heatmap'}</div>
                <div class="muted">{'想看板块/概念分布，就去热力图。' if lang == 'zh' else 'Open the heatmap when you want sector and concept distribution.'}</div>
                <div style="margin-top:12px;"><a class="pill" href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}">{'进入热力图' if lang == 'zh' else 'Open heatmap'}</a></div>
              </article>
              <article class="card">
                <div class="eyebrow">{'看概念' if lang == 'zh' else 'Concepts'}</div>
                <div class="muted">{'想看概念追踪、共振和明细，就去概念页。' if lang == 'zh' else 'Open the concept page for resonance, activity tracking, and ticker detail.'}</div>
                <div style="margin-top:12px;"><a class="pill" href="/dashboard/market/concepts?lang={lang}&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}">{'进入概念追踪' if lang == 'zh' else 'Open concept tracker'}</a></div>
              </article>
              <article class="card">
                <div class="eyebrow">{'看连续强势' if lang == 'zh' else 'Persistence'}</div>
                <div class="muted">{'想看连续入选、持续走强的票，就去连续强势股。' if lang == 'zh' else 'Open continuous leaders to review names that keep showing up.'}</div>
                <div style="margin-top:12px;"><a class="pill" href="/dashboard/continuous-leaders?lang={lang}&lookback_runs={lookback_runs}">{'进入连续强势股' if lang == 'zh' else 'Open continuous leaders'}</a></div>
              </article>
            </div>
          </section>
        </div>
          </main>
        </div>
        <script>
          function appendExecutionTag(formAction, inputName, tag) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const input = form.querySelector(`input[name="${{inputName}}"]`);
            if (!input) return;
            const values = input.value.split(",").map((item) => item.trim()).filter(Boolean);
            if (!values.includes(tag)) {{
              values.push(tag);
            }}
            input.value = values.join(", ");
            input.focus();
          }}

          function clearExecutionTags(formAction) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const includeInput = form.querySelector('input[name="execution_tag_filter"]');
            const excludeInput = form.querySelector('input[name="exclude_execution_tag_filter"]');
            if (includeInput) includeInput.value = "";
            if (excludeInput) excludeInput.value = "";
            if (includeInput) includeInput.focus();
          }}
        </script>
      </body>
    </html>
    """


@router.get("/market/heatmap", response_class=HTMLResponse)
def dashboard_market_heatmap_page(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    heatmap_sort: str = "hits",
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/market/heatmap")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    heatmap_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    heatmap_payload = (heatmap_snapshot or {}).get("payload") if isinstance(heatmap_snapshot, dict) else None
    heatmap_ready = isinstance(heatmap_payload, dict) and isinstance(heatmap_payload.get("sector_heatmap"), list)
    heatmap_rows = list((heatmap_payload or {}).get("sector_heatmap") or [])
    if signal_filter == "BUY":
        heatmap_rows = [item for item in heatmap_rows if int(item.get("buy_signal_count") or 0) > 0]
    elif signal_filter != "ALL":
        heatmap_rows = [
            item for item in heatmap_rows
            if any(str(detail.get("signal_label") or "").strip().upper() == signal_filter for detail in item.get("ticker_details", []))
        ]
    if min_signal_strength > 0:
        heatmap_rows = [item for item in heatmap_rows if int(item.get("max_signal_strength") or 0) >= min_signal_strength]
    if min_buy_signal_count > 0:
        heatmap_rows = [item for item in heatmap_rows if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        heatmap_rows = [item for item in heatmap_rows if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        heatmap_rows = [item for item in heatmap_rows if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)]
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, object]] = []
    tagged_names = 0
    for item in heatmap_rows:
        tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        tagged_names += 1
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        risk_examples.append({"label": item.get("label"), "tags": tags[:2]})
    risk_examples = risk_examples[:3]
    risk_top_tags = sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    if heatmap_sort == "five_day":
        heatmap_rows.sort(key=lambda item: (float(item.get("avg_move_5d") or -9999.0), float(item.get("avg_score") or 0.0)), reverse=True)
    elif heatmap_sort == "breadth":
        heatmap_rows.sort(key=lambda item: (float(item.get("breadth_pct") or -1.0), float(item.get("avg_score") or 0.0)), reverse=True)
    elif heatmap_sort == "score":
        heatmap_rows.sort(key=lambda item: (float(item.get("avg_score") or 0.0), int(item.get("hits") or 0)), reverse=True)
    else:
        heatmap_rows.sort(key=lambda item: (int(item.get("hits") or 0), float(item.get("avg_score") or 0.0)), reverse=True)
    for item in heatmap_rows:
        avg_move = item.get("avg_move_5d")
        breadth = item.get("breadth_pct")
        item["avg_move_5d_display"] = "-" if avg_move is None else f"{'+' if float(avg_move) > 0 else ''}{float(avg_move):.1f}%"
        item["breadth_display"] = "-" if breadth is None else f"{float(breadth):.0f}% {'涨' if lang == 'zh' else 'up'}"
        item["execution_tags_display"] = " · ".join(item.get("execution_tags") or [])
    heatmap_tiles = "".join(
        f"<a href='/dashboard/concepts/{item['slug']}?{urlencode({'lookback_runs': lookback_runs, 'lang': lang, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='heat-tile' style='background:rgba(15,118,110,{min(0.92, item['intensity']/115):.2f});'>"
        f"<div class='heat-label'>{item['label']}</div>"
        f"<div class='heat-metric'>{item['hits']} {'次命中' if lang == 'zh' else 'hit(s)'}</div>"
        f"<div class='heat-meta'>{'平均分' if lang == 'zh' else 'avg'} {item['avg_score']:.3f}</div>"
        f"<div class='heat-meta'>{item['avg_move_5d_display']} · {item['breadth_display']}</div>"
        f"<div class='heat-meta'>{'买点' if lang == 'zh' else 'Buy'} {int(item.get('buy_signal_count') or 0)} · {'最强' if lang == 'zh' else 'Max'} {int(item.get('max_signal_strength') or 0)}</div>"
        f"<div class='heat-meta'>{item['execution_tags_display'] or ('执行提醒 -' if lang == 'zh' else 'Execution tags -')}</div>"
        "</a>"
        for item in heatmap_rows
    ) or f"<div class='muted'>{'暂无概念热力图，请先同步 A 股概念。' if lang == 'zh' else 'No concept heatmap yet. Sync CN concepts first.'}</div>"
    market_rows = "".join(
        f"<tr><td>{item['market']}</td><td>{item['count']}</td></tr>"
        for item in ((heatmap_payload or {}).get("market_distribution") or [])
    ) or f"<tr><td colspan='2'>{'热力图仍在后台预计算' if lang == 'zh' else 'Heatmap is still being precomputed'}</td></tr>"
    lookback_pills = _lookback_pills("/dashboard/market/heatmap", selected=lookback_runs, extra_params={"lang": lang, "heatmap_sort": heatmap_sort, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter})
    heatmap_sort_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': mode, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if heatmap_sort == mode else ''}'>{label}</a>"
        for mode, label in (
            ("hits", _dt(lang, "sort_by_hits")),
            ("five_day", _dt(lang, "sort_by_5d")),
            ("breadth", _dt(lang, "sort_by_breadth")),
            ("score", _dt(lang, "sort_by_score")),
        )
    )
    signal_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
        for mode, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    risk_top_tags_html = "".join(
        f"<span class='compare-pill'>{tag} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_dt(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['label']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    loading_hint = (
        f"<div class='card'><div class='eyebrow'>{'后台预计算' if lang == 'zh' else 'Background Precompute'}</div><p class='muted'>{'板块热力图仍在后台生成，稍后刷新即可。' if lang == 'zh' else 'Sector heatmap is still being generated in the background. Refresh shortly.'}</p></div>"
        if not heatmap_ready
        else ""
    )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'板块热力图' if lang == 'zh' else 'Sector Heatmap'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-soft:rgba(61,217,182,0.12); }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:1120px; margin:0 auto; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .toolbar,.compare-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .pill,.compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); font-size:13px; font-weight:700; text-decoration:none; }}
          .compare-pill.active {{ background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }}
          .heat-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); margin-top:12px; }}
          .heat-tile {{ color:#fff; border-radius:18px; padding:14px; min-height:110px; display:flex; flex-direction:column; justify-content:space-between; text-decoration:none; box-shadow:0 12px 26px rgba(0,0,0,0.18); }}
          .heat-label {{ font-weight:800; line-height:1.3; }}
          .heat-metric {{ font-size:22px; font-weight:800; }}
          .heat-meta {{ font-size:12px; opacity:0.92; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          .ticker-links {{ max-width:280px; line-height:1.8; }}
          .ticker-links a {{ display:inline-flex; padding:2px 7px; margin:1px 2px 1px 0; border:1px solid rgba(61,217,182,0.22); border-radius:999px; background:rgba(61,217,182,0.08); color:#bff7eb; font-size:12px; font-weight:800; }}
          input, button {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); font:inherit; }}
          button {{ width:auto; background:var(--accent); color:#041119; font-weight:800; cursor:pointer; }}
          .sidebar-foot {{ margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }}
          h1 {{ margin:0 0 8px; font-size:38px; line-height:1.04; letter-spacing:-0.03em; }}
          @media (max-width:1100px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'板块热力图' if lang == 'zh' else 'Sector Heatmap'}</h1>
              <p>{'这里专门看板块热度、信号分布和筛选后的热力排序。' if lang == 'zh' else 'Use this page for sector heat, signal distribution, and filtered ranking.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'板块页聚焦在“哪里最热、哪里最强、哪里带风险标签”。' if lang == 'zh' else 'The heatmap focuses on where the market is hottest, strongest, and carrying execution tags.'}</div>
          </aside>
          <main class="main">
        <div class="wrap">
          <div class="toolbar">
            <a href="/dashboard/market?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">← {'返回市场脉冲' if lang == 'zh' else 'Back to Market Pulse'}</a>
            <a href="/dashboard/market/concepts?lang={lang}&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">{_dt(lang, 'concept_activity_tracker')}</a>
            <a href="/dashboard/market/heatmap?lang=en&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">English</a>
            <a href="/dashboard/market/heatmap?lang=zh&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">中文</a>
          </div>
          <div class="card">
            <div class="eyebrow">{_dt(lang, 'sector_heatmap')}</div>
            <h1 style="margin:0 0 8px;">{'板块热力图' if lang == 'zh' else 'Sector Heatmap'}</h1>
            <p class="muted">{_dt(lang, 'sector_heatmap_help')}</p>
          </div>
          {loading_hint}
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'snapshot_window')}</div>
            <div class="compare-row">{lookback_pills}</div>
            <div class="eyebrow" style="margin-top:12px;">{"Signal Focus" if lang == "en" else "信号聚焦"}</div>
            <div class="compare-row">{signal_pills}</div>
            <form action="/dashboard/market/heatmap" method="get" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));align-items:end;">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
              <input type="hidden" name="heatmap_sort" value="{heatmap_sort}" />
              <input type="hidden" name="signal_filter" value="{signal_filter}" />
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Execution Tag" if lang == "en" else "执行提醒标签"}</label>
                <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Exclude Tag" if lang == "en" else "排除标签"}</label>
                <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div style="grid-column:1 / -1;">
                <div class="muted" style="margin-bottom:6px;">{"Quick Tags" if lang == "en" else "快捷标签"}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;">
                  <button type="button" onclick="appendExecutionTag('/dashboard/market/heatmap', 'execution_tag_filter', 'gap-risk')">gap-risk</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market/heatmap', 'execution_tag_filter', 'earnings-soon')">earnings-soon</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market/heatmap', 'execution_tag_filter', 'thin-liquidity')">thin-liquidity</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market/heatmap', 'exclude_execution_tag_filter', 'gap-risk')">{"exclude gap-risk" if lang == "en" else "排除 gap-risk"}</button>
                  <button type="button" onclick="clearExecutionTags('/dashboard/market/heatmap')">{"Clear Tags" if lang == "en" else "清空标签"}</button>
                </div>
              </div>
              <datalist id="execution-tag-options">
                <option value="gap-risk"></option>
                <option value="earnings-soon"></option>
                <option value="thin-liquidity"></option>
              </datalist>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Buy Count" if lang == "en" else "最少买点数"}</label>
                <input type="number" name="min_buy_signal_count" min="0" step="1" value="{min_buy_signal_count}" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Strength" if lang == "en" else "最低强度"}</label>
                <input type="number" name="min_signal_strength" min="0" max="100" step="1" value="{min_signal_strength}" />
              </div>
              <button type="submit">{_concept_tr(lang, 'apply_filters')}</button>
            </form>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'risk_overview')}</div>
            <div class="grid">
              <article class="card">
                <div class="eyebrow">{_dt(lang, 'tagged_names')}</div>
                <div style="font-size:28px;font-weight:800;margin:6px 0;">{tagged_names}</div>
                <div class="muted">{_dt(lang, 'risk_examples')}</div>
              </article>
              <article class="card">
                <div class="eyebrow">{_dt(lang, 'common_risks')}</div>
                <div class="compare-row" style="margin-bottom:8px;">{risk_top_tags_html}</div>
                <div class="muted">{_dt(lang, 'risk_examples')}: {risk_examples_html}</div>
              </article>
            </div>
          </section>
          <section class="card">
            <div class="compare-row">
              <span class="muted">{_dt(lang, 'heatmap_sort')}:</span>
              {heatmap_sort_pills}
            </div>
            <div class="heat-grid">{heatmap_tiles}</div>
          </section>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'signal_distribution')}</div>
              <table>
                <thead><tr><th>{_dt(lang, 'market')}</th><th>{_dt(lang, 'top_n_hits')}</th></tr></thead>
                <tbody>{market_rows}</tbody>
              </table>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'concept_resonance')}</div>
              <div style="font-size:32px;font-weight:800;margin:6px 0;">{float((heatmap_payload or {}).get('resonance_score') or 0.0):.1f}%</div>
              <div class="muted">{_dt(lang, 'concept_resonance_help')}</div>
              <div class="muted" style="margin-top:8px;">{_dt(lang, 'tracked_signals')}: {int((heatmap_payload or {}).get('tracked_signal_count') or 0)}</div>
            </article>
          </section>
        </div>
          </main>
        </div>
        <script>
          function appendExecutionTag(formAction, inputName, tag) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const input = form.querySelector(`input[name="${{inputName}}"]`);
            if (!input) return;
            const values = input.value.split(",").map((item) => item.trim()).filter(Boolean);
            if (!values.includes(tag)) {{
              values.push(tag);
            }}
            input.value = values.join(", ");
            input.focus();
          }}

          function clearExecutionTags(formAction) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const includeInput = form.querySelector('input[name="execution_tag_filter"]');
            const excludeInput = form.querySelector('input[name="exclude_execution_tag_filter"]');
            if (includeInput) includeInput.value = "";
            if (excludeInput) excludeInput.value = "";
            if (includeInput) includeInput.focus();
          }}
        </script>
      </body>
    </html>
    """


@router.get("/market/concepts", response_class=HTMLResponse)
def dashboard_market_concepts_page(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    concept_sort_by: str = "delta",
    concept_sort_order: str = "desc",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/market/concepts")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    concept_rows_source = _load_concept_tracker_rows(db, lookback_runs=lookback_runs)
    if signal_filter != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if any(str(detail.get("signal_label") or "").strip().upper() == signal_filter for detail in item.get("ticker_details", []))
        ]
    if min_signal_strength > 0:
        concept_rows_source = [
            item
            for item in concept_rows_source
            if any(int(detail.get("signal_strength") or 0) >= min_signal_strength for detail in item.get("ticker_details", []))
        ]
    if min_buy_signal_count > 0:
        concept_rows_source = [
            item
            for item in concept_rows_source
            if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, object]] = []
    tagged_names = 0
    for item in concept_rows_source:
        tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        tagged_names += 1
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        risk_examples.append({"concept_name": item.get("concept_name"), "tags": tags[:2]})
    risk_examples = risk_examples[:3]
    risk_top_tags = sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    concept_rows_source.sort(key=lambda item: _market_concept_sort_key(concept_sort_by, item))
    if concept_sort_order == "desc":
        concept_rows_source.reverse()

    def _concept_tracker_sort_link(column: str, label: str) -> str:
        next_order = "asc" if concept_sort_by == column and concept_sort_order == "desc" else "desc"
        arrow = ""
        if concept_sort_by == column:
            arrow = " ↓" if concept_sort_order == "desc" else " ↑"
        href = (
            f"/dashboard/market/concepts?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter, 'concept_sort_by': column, 'concept_sort_order': next_order})}"
        )
        return f"<a href='{href}'>{label}{arrow}</a>"

    concept_rows = "".join(
        "<tr>"
        f"<td id='concept-{item['slug']}'><a href='/dashboard/concepts/{item['slug']}?{urlencode({'lookback_runs': lookback_runs, 'lang': lang, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}'>{item['concept_name']}</a></td>"
        f"<td>{item['hits']}</td><td>{item['previous_hits']}</td><td>{'+' if item['delta_hits'] > 0 else ''}{item['delta_hits']}</td><td>{item['streak']}</td><td>{_sparkline_svg(item['history'])}</td><td>{_percent_chip(item.get('avg_move_5d'))}</td><td>{_breadth_chip(item.get('breadth_pct'))}</td><td>{int(item.get('buy_signal_count') or 0)}</td><td>{int(item.get('max_signal_strength') or 0)}</td><td>{' · '.join(item.get('execution_tags') or []) or '-'}</td><td>{item['avg_score']:.4f}</td><td class='ticker-links'>{_ticker_links_html(item.get('tickers') or [], lang=lang)}</td>"
        "</tr>"
        for item in concept_rows_source
    ) or f"<tr><td colspan='13'>{'暂无概念数据' if lang == 'zh' else 'No concept data yet'}</td></tr>"
    lookback_pills = _lookback_pills("/dashboard/market/concepts", selected=lookback_runs, extra_params={"lang": lang, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter, "concept_sort_by": concept_sort_by, "concept_sort_order": concept_sort_order})
    signal_pills = "".join(
        f"<a href='/dashboard/market/concepts?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter, 'concept_sort_by': concept_sort_by, 'concept_sort_order': concept_sort_order})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
        for mode, label in (
            ("ALL", "All Signals" if lang == "en" else "全部信号"),
            ("BUY", "Buy" if lang == "en" else "买点"),
            ("WATCH", "Watch" if lang == "en" else "观察"),
            ("SELL", "Sell" if lang == "en" else "卖点"),
            ("HOLD", "Hold" if lang == "en" else "持有"),
        )
    )
    risk_top_tags_html = "".join(
        f"<span class='compare-pill'>{tag} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_dt(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['concept_name']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    nav_html = render_workspace_nav_html(lang=lang, active_key="market", lookback_runs=lookback_runs)
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'概念异动追踪' if lang == 'zh' else 'Concept Activity Tracker'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-soft:rgba(61,217,182,0.12); }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:1120px; margin:0 auto; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .toolbar,.compare-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .pill,.compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); font-size:13px; font-weight:700; text-decoration:none; }}
          .compare-pill.active {{ background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          .ticker-links {{ max-width:280px; line-height:1.8; }}
          .ticker-links a {{ display:inline-flex; padding:2px 7px; margin:1px 2px 1px 0; border:1px solid rgba(61,217,182,0.22); border-radius:999px; background:rgba(61,217,182,0.08); color:#bff7eb; font-size:12px; font-weight:800; }}
          input, button {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); font:inherit; }}
          button {{ width:auto; background:var(--accent); color:#041119; font-weight:800; cursor:pointer; }}
          .sidebar-foot {{ margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }}
          h1 {{ margin:0 0 8px; font-size:38px; line-height:1.04; letter-spacing:-0.03em; }}
          @media (max-width:1100px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'概念追踪' if lang == 'zh' else 'Concept Tracker'}</h1>
              <p>{'这里聚焦概念命中、连续性、强弱变化和概念内股票构成。' if lang == 'zh' else 'Use this page to track concept hits, persistence, strength shifts, and member tickers.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'概念页更适合回答“哪些主题在持续强化，哪些只是短期异动”。' if lang == 'zh' else 'This page helps answer which themes are strengthening versus only flashing briefly.'}</div>
          </aside>
          <main class="main">
        <div class="wrap">
          <div class="toolbar">
            <a href="/dashboard/market?lang={lang}&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">← {'返回市场脉冲' if lang == 'zh' else 'Back to Market Pulse'}</a>
            <a href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">{_dt(lang, 'sector_heatmap')}</a>
            <a href="/dashboard/market/concepts?lang=en&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}&concept_sort_by={concept_sort_by}&concept_sort_order={concept_sort_order}" class="pill">English</a>
            <a href="/dashboard/market/concepts?lang=zh&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}&concept_sort_by={concept_sort_by}&concept_sort_order={concept_sort_order}" class="pill">中文</a>
          </div>
          <div class="card">
            <div class="eyebrow">{_dt(lang, 'concept_activity_tracker')}</div>
            <h1 style="margin:0 0 8px;">{'概念异动追踪' if lang == 'zh' else 'Concept Activity Tracker'}</h1>
            <p class="muted">{'专门追踪概念命中、连续性、强弱和概念内股票构成。' if lang == 'zh' else 'A focused page for concept hits, persistence, strength, and tracked tickers.'}</p>
          </div>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'snapshot_window')}</div>
            <div class="compare-row">{lookback_pills}</div>
            <div class="eyebrow" style="margin-top:12px;">{"Signal Focus" if lang == "en" else "信号聚焦"}</div>
            <div class="compare-row">{signal_pills}</div>
            <form action="/dashboard/market/concepts" method="get" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));align-items:end;">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
              <input type="hidden" name="signal_filter" value="{signal_filter}" />
              <input type="hidden" name="concept_sort_by" value="{concept_sort_by}" />
              <input type="hidden" name="concept_sort_order" value="{concept_sort_order}" />
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Execution Tag" if lang == "en" else "执行提醒标签"}</label>
                <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Exclude Tag" if lang == "en" else "排除标签"}</label>
                <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div style="grid-column:1 / -1;">
                <div class="muted" style="margin-bottom:6px;">{"Quick Tags" if lang == "en" else "快捷标签"}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;">
                  <button type="button" onclick="appendExecutionTag('/dashboard/market/concepts', 'execution_tag_filter', 'gap-risk')">gap-risk</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market/concepts', 'execution_tag_filter', 'earnings-soon')">earnings-soon</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market/concepts', 'execution_tag_filter', 'thin-liquidity')">thin-liquidity</button>
                  <button type="button" onclick="appendExecutionTag('/dashboard/market/concepts', 'exclude_execution_tag_filter', 'gap-risk')">{"exclude gap-risk" if lang == "en" else "排除 gap-risk"}</button>
                  <button type="button" onclick="clearExecutionTags('/dashboard/market/concepts')">{"Clear Tags" if lang == "en" else "清空标签"}</button>
                </div>
              </div>
              <datalist id="execution-tag-options">
                <option value="gap-risk"></option>
                <option value="earnings-soon"></option>
                <option value="thin-liquidity"></option>
              </datalist>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Buy Count" if lang == "en" else "最少买点数"}</label>
                <input type="number" name="min_buy_signal_count" min="0" step="1" value="{min_buy_signal_count}" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{"Min Strength" if lang == "en" else "最低强度"}</label>
                <input type="number" name="min_signal_strength" min="0" max="100" step="1" value="{min_signal_strength}" />
              </div>
              <button type="submit">{_concept_tr(lang, 'apply_filters')}</button>
            </form>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'risk_overview')}</div>
            <div style="display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));">
              <article class="card" style="margin:0;background:rgba(21,34,49,0.82);">
                <div class="eyebrow">{_dt(lang, 'tagged_names')}</div>
                <div style="font-size:28px;font-weight:800;margin:6px 0;">{tagged_names}</div>
                <div class="muted">{_dt(lang, 'risk_examples')}</div>
              </article>
              <article class="card" style="margin:0;background:rgba(21,34,49,0.82);">
                <div class="eyebrow">{_dt(lang, 'common_risks')}</div>
                <div class="compare-row" style="margin-bottom:8px;">{risk_top_tags_html}</div>
                <div class="muted">{_dt(lang, 'risk_examples')}: {risk_examples_html}</div>
              </article>
            </div>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'concept_activity_tracker')}</div>
            <div class="compare-row">
              <a href="/dashboard/market/concepts/export?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter, 'concept_sort_by': concept_sort_by, 'concept_sort_order': concept_sort_order})}" class="pill">Export CSV</a>
            </div>
            <table>
              <thead><tr><th>{_concept_tracker_sort_link('concept', _dt(lang, 'concept'))}</th><th>{_concept_tracker_sort_link('hits', _dt(lang, 'hits'))}</th><th>{_dt(lang, 'prev')}</th><th>{_concept_tracker_sort_link('delta', _dt(lang, 'delta_hits'))}</th><th>{_concept_tracker_sort_link('streak', _dt(lang, 'streak'))}</th><th>{_dt(lang, 'trend')}</th><th>{_concept_tracker_sort_link('five_day', _dt(lang, 'five_day'))}</th><th>{_concept_tracker_sort_link('breadth', _dt(lang, 'breadth'))}</th><th>{_concept_tracker_sort_link('buy_count', _concept_tr(lang, 'buy_signal_count'))}</th><th>{_concept_tracker_sort_link('max_strength', _concept_tr(lang, 'max_signal_strength'))}</th><th>{'执行提醒' if lang == 'zh' else 'Execution Tags'}</th><th>{_concept_tracker_sort_link('score', _dt(lang, 'avg_score'))}</th><th>{_dt(lang, 'tickers')}</th></tr></thead>
              <tbody>{concept_rows}</tbody>
            </table>
          </section>
        </div>
          </main>
        </div>
        <script>
          function appendExecutionTag(formAction, inputName, tag) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const input = form.querySelector(`input[name="${{inputName}}"]`);
            if (!input) return;
            const values = input.value.split(",").map((item) => item.trim()).filter(Boolean);
            if (!values.includes(tag)) {{
              values.push(tag);
            }}
            input.value = values.join(", ");
            input.focus();
          }}

          function clearExecutionTags(formAction) {{
            const form = document.querySelector(`form[action="${{formAction}}"]`);
            if (!form) return;
            const includeInput = form.querySelector('input[name="execution_tag_filter"]');
            const excludeInput = form.querySelector('input[name="exclude_execution_tag_filter"]');
            if (includeInput) includeInput.value = "";
            if (excludeInput) excludeInput.value = "";
            if (includeInput) includeInput.focus();
          }}
        </script>
      </body>
    </html>
    """


@router.get("/market/concepts/export")
def dashboard_market_concepts_export(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    concept_sort_by: str = "delta",
    concept_sort_order: str = "desc",
    db: Session = Depends(get_db_session),
) -> Response:
    if not is_authenticated(request):
        return login_redirect("/dashboard/market/concepts")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    concept_rows_source = _load_concept_tracker_rows(db, lookback_runs=lookback_runs)
    if signal_filter != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if any(str(detail.get("signal_label") or "").strip().upper() == signal_filter for detail in item.get("ticker_details", []))
        ]
    if min_signal_strength > 0:
        concept_rows_source = [
            item
            for item in concept_rows_source
            if any(int(detail.get("signal_strength") or 0) >= min_signal_strength for detail in item.get("ticker_details", []))
        ]
    if min_buy_signal_count > 0:
        concept_rows_source = [
            item
            for item in concept_rows_source
            if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item
            for item in concept_rows_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    concept_rows_source.sort(key=lambda item: _market_concept_sort_key(concept_sort_by, item))
    if concept_sort_order == "desc":
        concept_rows_source.reverse()

    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "concept_name",
            "hits",
            "previous_hits",
            "delta_hits",
            "streak",
            "avg_move_5d",
            "breadth_pct",
            "buy_signal_count",
            "max_signal_strength",
            "execution_tags",
            "avg_score",
            "tickers",
        ],
    )
    writer.writeheader()
    for item in concept_rows_source:
        writer.writerow(
            {
                "concept_name": item.get("concept_name"),
                "hits": item.get("hits"),
                "previous_hits": item.get("previous_hits"),
                "delta_hits": item.get("delta_hits"),
                "streak": item.get("streak"),
                "avg_move_5d": item.get("avg_move_5d"),
                "breadth_pct": item.get("breadth_pct"),
                "buy_signal_count": item.get("buy_signal_count"),
                "max_signal_strength": item.get("max_signal_strength"),
                "execution_tags": ";".join(item.get("execution_tags") or []),
                "avg_score": item.get("avg_score"),
                "tickers": ",".join(item.get("tickers") or []),
            }
        )
    filename = f"concept-tracker-{lookback_runs}runs.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/ops", response_class=HTMLResponse)
def dashboard_ops_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops")
    lang = resolve_request_lang(request)
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    summary = _load_ops_summary(db)
    auto_analysis = summary["auto_analysis"]
    latest_backtest = summary["latest_backtest"]
    recent_model_runs = summary["recent_model_runs"]
    sync_states = summary["recent_sync_states"]
    recent_jobs = summary["recent_jobs"]
    latest_model = summary["latest_model"] or {}
    sync_overview = summary["sync_overview"] or {}
    pipeline_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_PIPELINE_STATUS)
    pipeline_payload = (pipeline_snapshot or {}).get("payload") if isinstance(pipeline_snapshot, dict) else None
    if isinstance(pipeline_payload, dict):
        recent_jobs = pipeline_payload.get("recent_jobs") or recent_jobs
    model_health_rows = pipeline_payload.get("model_health") if isinstance(pipeline_payload, dict) else None
    anomaly_rows = pipeline_payload.get("anomalies") if isinstance(pipeline_payload, dict) else None
    close_review_status = close_review_scheduler_service.get_status()
    provider_strategy = _provider_strategy_view(lang)
    notifier = PushNotificationService()
    notification_channels = notifier.available_channels()
    dashboard_redirect = "/dashboard/ops?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})

    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)

    synced_count = int(sync_overview.get("total") or len(sync_states))
    sync_success_count = int(sync_overview.get("success") or sum(1 for item in sync_states if str(item.get("status") or "").lower() == "success"))
    primary_provider_counts: dict[str, int] = {}
    for provider, count in (sync_overview.get("provider_counts") or {}).items():
        primary_provider_counts[str(provider)] = int(count or 0)
    if not primary_provider_counts:
        for item in sync_states:
            provider = str(item.get("provider") or "unknown")
            primary_provider_counts[provider] = primary_provider_counts.get(provider, 0) + 1
    primary_provider = next(iter(sorted(primary_provider_counts.items(), key=lambda pair: (-pair[1], pair[0]))), None)

    refresh_job = next((item for item in recent_jobs if str(item.get("job_type") or "").lower() == "cn_close_review"), None)
    analysis_job = next((item for item in recent_jobs if str(item.get("job_type") or "").lower() == "watchlist_auto_analysis"), None)
    screener_precompute_job = next((item for item in recent_jobs if str(item.get("job_type") or "").lower() == "screener_precompute"), None)
    latest_cn_refresh = _latest_cn_refresh_summary(db, recent_jobs, lang=lang)

    def _step_status_label(job: dict | None, fallback_status: str | None = None) -> tuple[str, str]:
        status = str((job or {}).get("status") or fallback_status or "").strip().lower()
        if not status:
            status = "idle"
        if lang == "zh":
            labels = {
                "success": "成功",
                "failed": "失败",
                "partial": "部分完成",
                "running": "运行中",
                "enabled": "已开启",
                "disabled": "已关闭",
                "idle": "待运行",
            }
        else:
            labels = {
                "success": "Success",
                "failed": "Failed",
                "partial": "Partial",
                "running": "Running",
                "enabled": "Enabled",
                "disabled": "Disabled",
                "idle": "Idle",
            }
        return labels.get(status, status), status

    snapshot_rows = pipeline_payload.get("rows") if isinstance(pipeline_payload, dict) else None
    if isinstance(snapshot_rows, list) and snapshot_rows:
        pipeline_steps = [
            {
                "label": item.get("label") or item.get("step") or "-",
                "detail": _display_time(item.get("timestamp")),
                "message": item.get("message") or "-",
                "status": _step_status_label(None, str(item.get("status") or "idle")),
            }
            for item in snapshot_rows
        ]
    else:
        pipeline_steps = [
            {
                "label": "行情刷新" if lang == "zh" else "Market Refresh",
                "detail": _display_time((refresh_job or {}).get("finished_at") or (refresh_job or {}).get("started_at") or close_review_status.get("last_run_at")),
                "message": (refresh_job or {}).get("message") or (
                    "收盘后刷新行情并重建技术快照。" if lang == "zh" else "Refreshes prices and rebuilds technical snapshots after close."
                ) + (f" · {latest_cn_refresh['label']}" if latest_cn_refresh.get("refreshed") is not None else ""),
                "status": _step_status_label(refresh_job, "enabled" if close_review_status.get("enabled") else "disabled"),
            },
            {
                "label": "技术快照" if lang == "zh" else "Technical Snapshots",
                "detail": str(sync_success_count) + (f" / {synced_count}" if synced_count else ""),
                "message": (
                    f"{sync_success_count}/{synced_count} {'只股票已完成同步' if lang == 'zh' else 'symbols synced successfully'}"
                    if synced_count
                    else ("暂无同步记录" if lang == "zh" else "No sync history yet")
                ),
                "status": _step_status_label(None, "success" if sync_success_count else "idle"),
            },
            {
                "label": "模型训练" if lang == "zh" else "Model Training",
                "detail": _display_time(latest_model.get("finished_at") or latest_model.get("created_at")),
                "message": latest_model.get("name") or ("尚未训练" if lang == "zh" else "No model run yet"),
                "status": _step_status_label(None, str(latest_model.get("status") or "idle")),
            },
            {
                "label": "回测结果" if lang == "zh" else "Backtest",
                "detail": (latest_backtest.get("end_date") or _display_time(latest_backtest.get("created_at"))),
                "message": latest_backtest.get("name") or ("暂无回测" if lang == "zh" else "No backtest yet"),
                "status": _step_status_label(None, str(latest_backtest.get("status") or "idle")),
            },
            {
                "label": "AI 日报" if lang == "zh" else "AI Report",
                "detail": _display_time((analysis_job or {}).get("finished_at") or auto_analysis.get("last_run_at")),
                "message": (analysis_job or {}).get("message") or (
                    "自动分析完成后会生成日报与推送。" if lang == "zh" else "A daily report is generated after automated analysis."
                ),
                "status": _step_status_label(analysis_job, "idle"),
            },
            {
                "label": "模型预计算" if lang == "zh" else "Model Precompute",
                "detail": _display_time((screener_precompute_job or {}).get("finished_at") or (screener_precompute_job or {}).get("started_at")),
                "message": (screener_precompute_job or {}).get("message") or (
                    "收盘后会把常用模型先跑一遍并缓存结果。" if lang == "zh" else "Common screener models are precomputed and cached after the close."
                ),
                "status": _step_status_label(screener_precompute_job, "idle"),
            },
        ]
    pipeline_html = "".join(
        "<article class='pipeline-step'>"
        f"<div class='step-head'><span class='step-title'>{item['label']}</span><span class='status-pill {item['status'][1]}'>{item['status'][0]}</span></div>"
        f"<div class='step-detail'>{item['detail']}</div>"
        f"<div class='step-message'>{item['message']}</div>"
        "</article>"
        for item in pipeline_steps
    )

    recent_jobs_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{item.get('job_type') or '-'}</div><div class='subtle'>{_display_time(item.get('started_at') or item.get('created_at'))}</div></div>"
        f"<div class='row-right'><span class='status-pill {str(item.get('status') or 'idle').lower()}'>{_step_status_label(item)[0]}</span></div>"
        "</article>"
        f"<div class='row-message'>{item.get('message') or '-'}</div>"
        for item in recent_jobs[:6]
    ) or f"<div class='empty'>{'暂无任务记录' if lang == 'zh' else 'No jobs yet'}</div>"

    recent_models_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker' title='{item.get('name') or '-'}'>{_compact_run_name(item.get('name'), 28) or '-'}</div><div class='subtle'>{_display_time(item.get('created_at'))}</div></div>"
        f"<div class='row-right'><span class='status-pill {str(item.get('status') or 'idle').lower()}'>{_step_status_label(None, str(item.get('status') or 'idle'))[0]}</span></div>"
        "</article>"
        for item in recent_model_runs[:4]
    ) or f"<div class='empty'>{'暂无模型运行' if lang == 'zh' else 'No model runs yet'}</div>"

    sync_rows_html = "".join(
        "<article class='sync-row'>"
        f"<div><a class='ticker' href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a><div class='subtle'>{item.get('provider') or '-'}</div></div>"
        f"<div class='row-right'><div class='mini-metric'>{item.get('last_synced_date') or '-'}</div><span class='status-pill {str(item.get('status') or 'idle').lower()}'>{_step_status_label(None, str(item.get('status') or 'idle'))[0]}</span></div>"
        "</article>"
        for item in sync_states[:5]
    ) or f"<div class='empty'>{'暂无同步记录' if lang == 'zh' else 'No sync history yet'}</div>"

    top_metrics = [
        {
            "label": "自动分析" if lang == "zh" else "Auto Analysis",
            "value": "开启" if (lang == "zh" and auto_analysis.get("enabled")) else ("关闭" if lang == "zh" else ("On" if auto_analysis.get("enabled") else "Off")),
            "meta": f"{'下次运行' if lang == 'zh' else 'Next'}: {_display_time(auto_analysis.get('next_run_at'))}",
        },
        {
            "label": "最近训练" if lang == "zh" else "Latest Model",
            "value": _compact_run_name(latest_model.get("name") or ("尚未训练" if lang == "zh" else "Not trained"), 24),
            "meta": _display_time(latest_model.get("finished_at") or latest_model.get("created_at")),
        },
        {
            "label": "数据同步" if lang == "zh" else "Data Sync",
            "value": latest_cn_refresh.get("summary") or f"{sync_success_count}/{synced_count}",
            "meta": latest_cn_refresh.get("label") or (primary_provider[0] if primary_provider else ("暂无来源" if lang == "zh" else "No provider")),
        },
        {
            "label": "最近回测" if lang == "zh" else "Backtest",
            "value": _compact_run_name(latest_backtest.get("name") or ("暂无回测" if lang == "zh" else "No backtest"), 24),
            "meta": latest_backtest.get("status") or "-",
        },
        {
            "label": "模型预计算" if lang == "zh" else "Precompute",
            "value": (
                f"{len((screener_precompute_job or {}).get('result', {}).get('snapshots_created') or [])}/"
                f"{len((screener_precompute_job or {}).get('result', {}).get('snapshots_created') or []) + int((screener_precompute_job or {}).get('result', {}).get('failed_count') or 0)}"
                if (screener_precompute_job or {}).get("result")
                else ("待运行" if lang == "zh" else "Pending")
            ),
            "meta": (
                (screener_precompute_job or {}).get("message")
                or ("收盘后预跑常用模型" if lang == "zh" else "Precompute common screener models after the close")
            ),
        },
    ]
    metrics_html = "".join(
        "<article class='metric-card'>"
        f"<div class='metric-label'>{item['label']}</div>"
        f"<div class='metric-value' title='{item['value']}'>{item['value']}</div>"
        f"<div class='metric-meta'>{item['meta']}</div>"
        "</article>"
        for item in top_metrics
    )
    close_review_action_feed = (pipeline_payload or {}).get("close_review_action_feed") if isinstance(pipeline_payload, dict) else None
    if not isinstance(close_review_action_feed, dict):
        close_review_action_feed = build_close_review_action_feed(_load_cached_ai_daily_report(db), lang=lang)
    if isinstance(model_health_rows, list) and model_health_rows:
        model_health_html = "".join(
            "<article class='metric-card'>"
            f"<div class='metric-label'>{item.get('label') or '-'}</div>"
            f"<div class='metric-value' title='{item.get('value') or '-'}'>{_compact_label(str(item.get('value') or '-'), 28)}</div>"
            f"<div class='metric-meta'>{item.get('meta') or '-'}</div>"
            "</article>"
            for item in model_health_rows[:4]
        )
    else:
        model_health_html = "".join(
            "<article class='metric-card'>"
            f"<div class='metric-label'>{label}</div>"
            f"<div class='metric-value'>{value}</div>"
            f"<div class='metric-meta'>{meta}</div>"
            "</article>"
            for label, value, meta in (
                (("训练状态" if lang == "zh" else "Training Status"), latest_model.get("status") or "-", latest_model.get("name") or "-"),
                (("最近训练时间" if lang == "zh" else "Latest Training"), _display_time(latest_model.get("finished_at") or latest_model.get("created_at")), ("模型越近越可信" if lang == "zh" else "Fresher is usually better")),
                (("最近回测" if lang == "zh" else "Latest Backtest"), latest_backtest.get("status") or "-", _compact_run_name(latest_backtest.get("name") or "-", 28)),
                (("数据完整度" if lang == "zh" else "Data Coverage"), f"{sync_success_count}/{synced_count}", ("同步成功股票数" if lang == "zh" else "Synced symbols")),
            )
        )
    anomalies_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{item.get('title') or '-'}</div><div class='subtle'>{item.get('detail') or '-'}</div></div>"
        "</article>"
        for item in (anomaly_rows or [])
    ) or f"<div class='empty'>{'当前没有明显异常' if lang == 'zh' else 'No obvious anomalies right now'}</div>"
    if not notification_channels:
        anomalies_html = (
            "<article class='list-row'>"
            f"<div><div class='ticker'>{'通知渠道未配置' if lang == 'zh' else 'No notification channel configured'}</div>"
            f"<div class='subtle'>{'AI 日报可以生成，但当前不会自动发送；请先在设置页配置 Telegram / WeChat / Feishu。' if lang == 'zh' else 'AI reports may generate, but they will not auto-send until Telegram / WeChat / Feishu is configured in Settings.'}</div></div>"
            "</article>"
        ) + anomalies_html
    notification_status_html = "".join(
        f"<span class='chip'>{channel}</span>"
        for channel in notification_channels
    ) or f"<span class='chip'>{'未配置' if lang == 'zh' else 'Not configured'}</span>"
    close_review_action_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{item.get('ticker') or '-'}</div><div class='subtle'>{item.get('name') or item.get('ticker') or '-'}</div><div class='subtle'>{item.get('execution_note') or item.get('entry_trigger') or '-'}</div></div>"
        f"<div class='row-right'><span class='mini-metric'>{item.get('tradability_status') or '-'}</span><span class='mini-metric'>{item.get('target_weight') or '-'}</span></div>"
        "</article>"
        for item in (close_review_action_feed.get("actionable") or [])[:4]
    ) or f"<div class='empty'>{'暂无盘后动作建议' if lang == 'zh' else 'No close-review actions yet'}</div>"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'任务中心' if lang == 'zh' else 'Task Center'}</title>
        <style>
          :root {{
            --bg:#071018;
            --bg-soft:#0d1722;
            --panel:#111c28;
            --panel-2:#152231;
            --panel-3:#1a2a3c;
            --ink:#e6edf3;
            --muted:#90a3b8;
            --line:#223246;
            --accent:#3dd9b6;
            --accent-2:#52a8ff;
            --danger:#ff6b81;
            --warn:#f6c85f;
            --good:#4ade80;
          }}
          * {{ box-sizing:border-box; }}
          body {{
            margin:0;
            font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
            color:var(--ink);
            background:
              radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),
              radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),
              linear-gradient(180deg, #08111a 0%, #071018 100%);
          }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px; }}
          .topbar {{ display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin-bottom:20px; flex-wrap:wrap; }}
          .chip-row {{ display:flex; flex-wrap:wrap; gap:10px; }}
          .top-pill {{ display:inline-flex; align-items:center; justify-content:center; min-height:38px; padding:0 14px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.72); color:var(--muted); font-size:13px; font-weight:700; }}
          .top-pill.active {{ color:var(--ink); border-color:rgba(82,168,255,0.35); background:rgba(82,168,255,0.16); }}
          .hero {{ display:grid; grid-template-columns:minmax(0,1.4fr) minmax(280px,0.9fr); gap:16px; margin-bottom:16px; }}
          .card {{ background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.06em; text-transform:uppercase; }}
          h1 {{ margin:14px 0 10px; font-size:40px; line-height:1.02; letter-spacing:-0.03em; }}
          .lead {{ margin:0; color:var(--muted); font-size:15px; line-height:1.6; max-width:720px; }}
          .metrics-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin:16px 0; }}
          .metric-card {{ padding:18px; border-radius:20px; background:rgba(21,34,49,0.82); border:1px solid var(--line); }}
          .metric-label {{ color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:0.05em; }}
          .metric-value {{ margin-top:12px; font-size:26px; font-weight:800; letter-spacing:-0.03em; word-break:break-word; }}
          .metric-meta {{ margin-top:8px; color:var(--muted); font-size:13px; }}
          .workspace-grid {{ display:grid; grid-template-columns:minmax(0,1.2fr) minmax(320px,0.8fr); gap:16px; align-items:start; }}
          .stack {{ display:grid; gap:16px; }}
          .section-title {{ margin:0 0 6px; font-size:22px; }}
          .section-copy {{ margin:0 0 16px; color:var(--muted); font-size:14px; line-height:1.6; }}
          .pipeline-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }}
          .pipeline-step {{ padding:16px; border-radius:20px; background:rgba(21,34,49,0.72); border:1px solid var(--line); min-height:142px; }}
          .step-head {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
          .step-title {{ font-weight:800; font-size:16px; }}
          .step-detail {{ margin-top:16px; font-size:14px; color:var(--ink); }}
          .step-message {{ margin-top:10px; color:var(--muted); font-size:13px; line-height:1.55; }}
          .list-stack {{ display:grid; gap:12px; }}
          .list-row, .sync-row {{ display:flex; justify-content:space-between; align-items:flex-start; gap:14px; padding:14px 0; border-top:1px solid rgba(144,163,184,0.12); }}
          .list-row:first-child, .sync-row:first-child {{ border-top:none; padding-top:0; }}
          .row-right {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }}
          .ticker {{ font-weight:800; font-size:15px; color:var(--ink); }}
          .subtle {{ margin-top:4px; color:var(--muted); font-size:12px; line-height:1.45; }}
          .row-message {{ margin-top:-6px; padding:0 0 12px; color:var(--muted); font-size:13px; line-height:1.55; border-bottom:1px solid rgba(144,163,184,0.12); }}
          .row-message:last-child {{ border-bottom:none; padding-bottom:0; }}
          .mini-metric {{ padding:7px 10px; border-radius:999px; background:rgba(82,168,255,0.12); color:#b9dcff; font-size:12px; font-weight:700; }}
          .chip {{ display:inline-flex; align-items:center; padding:7px 10px; border-radius:999px; background:rgba(82,168,255,0.10); border:1px solid rgba(82,168,255,0.18); color:#9acbff; font-size:12px; font-weight:700; }}
          .status-pill {{ display:inline-flex; padding:7px 10px; border-radius:999px; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:0.04em; }}
          .status-pill.success {{ background:rgba(74,222,128,0.14); color:#8df0aa; }}
          .status-pill.failed {{ background:rgba(255,107,129,0.16); color:#ff9aaa; }}
          .status-pill.partial {{ background:rgba(246,200,95,0.16); color:#ffd98a; }}
          .status-pill.running {{ background:rgba(82,168,255,0.16); color:#9bd0ff; }}
          .status-pill.enabled {{ background:rgba(61,217,182,0.16); color:#7ff0d2; }}
          .status-pill.disabled, .status-pill.idle {{ background:rgba(144,163,184,0.14); color:#b4c5d8; }}
          .action-row {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }}
          .cta {{ display:inline-flex; align-items:center; justify-content:center; padding:10px 14px; border-radius:999px; border:1px solid var(--line); background:rgba(21,34,49,0.92); color:var(--ink); font-size:13px; font-weight:800; }}
          .cta.primary {{ background:linear-gradient(135deg, rgba(61,217,182,0.28), rgba(82,168,255,0.24)); border-color:rgba(61,217,182,0.3); }}
          .empty {{ color:var(--muted); font-size:14px; }}
          @media (max-width: 1180px) {{
            .metrics-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
            .workspace-grid, .hero {{ grid-template-columns:1fr; }}
          }}
          @media (max-width: 900px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
            .pipeline-grid {{ grid-template-columns:1fr; }}
          }}
          @media (max-width: 640px) {{
            .content {{ padding:20px 16px 36px; }}
            h1 {{ font-size:30px; }}
            .metrics-grid {{ grid-template-columns:1fr; }}
          }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'任务中心' if lang == 'zh' else 'Task Center'}</h1>
              <p>{'把自动任务、训练状态和最近结果放到一个固定入口，不再让用户去日志里找结论。' if lang == 'zh' else 'Keep automation, training state, and recent results in one fixed place instead of burying them in logs.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">
              {'收盘刷新、自动分析、训练和回测，现在都应该从这里看整体状态。' if lang == 'zh' else 'Close review, auto analysis, training, and backtests should all be tracked from here.'}
            </div>
          </aside>
          <main class="content">
            <div class="topbar">
              <div class="chip-row">
                <span class="top-pill">{'最近更新' if lang == 'zh' else 'Updated'}: {_display_time(summary.get('generated_at'), with_tz=True)}</span>
                <span class="top-pill">{'计划时间' if lang == 'zh' else 'Close Review'}: {close_review_status.get('run_hour', 0):02d}:{close_review_status.get('run_minute', 0):02d}</span>
              </div>
              <div class="chip-row">
                <a class="top-pill{' active' if lang == 'en' else ''}" href="/dashboard/ops?lang=en&lookback_runs={lookback_runs}">EN</a>
                <a class="top-pill{' active' if lang == 'zh' else ''}" href="/dashboard/ops?lang=zh&lookback_runs={lookback_runs}">中文</a>
              </div>
            </div>

            <section class="hero">
              <article class="card">
                <span class="eyebrow">{'自动流程总览' if lang == 'zh' else 'Automation Overview'}</span>
                <h1>{'今天的自动任务是否跑对了' if lang == 'zh' else 'Did today’s automation run correctly?'}</h1>
                <p class="lead">{'任务中心现在按流程展示：行情刷新、技术快照、模型训练、回测、AI 日报。你进来后先看状态和结果，再决定要不要进入同步中心或模型页做手动操作。' if lang == 'zh' else 'The page now follows the actual pipeline: market refresh, technical snapshots, model training, backtest, and AI report. Check the status and results first, then decide whether you need the sync or model pages.'}</p>
                <div class="action-row">
                  <a class="cta primary" href="/dashboard/ops/sync?lang={lang}&lookback_runs={lookback_runs}">{'打开同步中心' if lang == 'zh' else 'Open Sync Center'}</a>
                  <a class="cta" href="/dashboard/ops/models?lang={lang}&lookback_runs={lookback_runs}">{'打开模型运行' if lang == 'zh' else 'Open Model Runs'}</a>
                  <a class="cta" href="/dashboard/ops/jobs?lang={lang}&lookback_runs={lookback_runs}">{'查看任务明细' if lang == 'zh' else 'View Job History'}</a>
                </div>
              </article>
              <article class="card">
                <span class="eyebrow">{'当前安排' if lang == 'zh' else 'Current Schedule'}</span>
                <div class="list-stack" style="margin-top:14px;">
                  <div>
                    <div class="subtle">{'收盘自动复盘' if lang == 'zh' else 'Close Review'}</div>
                    <div class="ticker">{'已开启' if close_review_status.get('enabled') and lang == 'zh' else ('已关闭' if lang == 'zh' else ('Enabled' if close_review_status.get('enabled') else 'Disabled'))}</div>
                  </div>
                  <div>
                    <div class="subtle">{'下次计划运行' if lang == 'zh' else 'Next Scheduled Run'}</div>
                    <div class="ticker">{_display_time(close_review_status.get('next_run_at') or auto_analysis.get('next_run_at'))}</div>
                  </div>
                  <div>
                    <div class="subtle">{'自动分析模板' if lang == 'zh' else 'Auto Analysis Template'}</div>
                    <div class="ticker">{auto_analysis.get('signal_type') or '-'}</div>
                  </div>
                  <div>
                    <div class="subtle">{'回看窗口' if lang == 'zh' else 'Lookback Window'}</div>
                    <div class="ticker">{auto_analysis.get('lookback_days') or '-'} {'天' if lang == 'zh' else 'day(s)'}</div>
                  </div>
                  <div>
                    <div class="subtle">{'日报推送渠道' if lang == 'zh' else 'Report delivery channels'}</div>
                    <div class="chip-row" style="margin-top:8px;">{notification_status_html}</div>
                  </div>
                </div>
                <div class="action-row">
                  <form action="/jobs/send-ai-daily-report" method="post">
                    <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                    <button class="cta primary" type="submit">{'立即发送 AI 日报' if lang == 'zh' else 'Send AI Daily Report Now'}</button>
                  </form>
                  <a class="cta" href="/dashboard/ai-daily-report?lang={lang}">{'打开 AI 日报' if lang == 'zh' else 'Open AI Report'}</a>
                </div>
              </article>
            </section>

            <section class="metrics-grid">{metrics_html}</section>

            <section class="workspace-grid">
              <div class="stack">
                <article class="card">
                  <span class="eyebrow">{'流程状态板' if lang == 'zh' else 'Pipeline Board'}</span>
                  <h2 class="section-title">{'从数据到结论的五个步骤' if lang == 'zh' else 'Five steps from data to conclusion'}</h2>
                  <p class="section-copy">{'这里不再先给原始任务列表，而是先回答用户最关心的问题：今天刷数了吗、训练了吗、回测了吗、AI 日报出来了吗。' if lang == 'zh' else 'Instead of starting with raw logs, this view answers the key questions first: was data refreshed, did training run, did backtest finish, and was the AI report produced?'}</p>
                  <div class="pipeline-grid">{pipeline_html}</div>
                </article>

                <article class="card">
                  <span class="eyebrow">{'模型健康度' if lang == 'zh' else 'Model Health'}</span>
                  <h2 class="section-title">{'先确认模型今天是否可信' if lang == 'zh' else 'Check if today’s model is trustworthy'}</h2>
                  <p class="section-copy">{'专业用户通常先确认训练是否成功、回测是否正常、同步覆盖是否足够，再决定是否采纳模型结论。' if lang == 'zh' else 'Professional users usually verify training, backtest, and data coverage before trusting model conclusions.'}</p>
                  <div class="metrics-grid">{model_health_html}</div>
                </article>

                <article class="card">
                  <span class="eyebrow">{'异常提示' if lang == 'zh' else 'Alerts'}</span>
                  <h2 class="section-title">{'优先处理这些问题' if lang == 'zh' else 'Handle these issues first'}</h2>
                  <p class="section-copy">{'如果这里出现异常，交易员通常会先暂停扩大风险，再回头核对数据、训练和回测链路。' if lang == 'zh' else 'If alerts appear here, a trader would usually avoid adding risk until data, training, and backtest checks are verified.'}</p>
                  <div class="list-stack">{anomalies_html}</div>
                </article>

                <article class="card">
                  <span class="eyebrow">{'最近任务结果' if lang == 'zh' else 'Recent Jobs'}</span>
                  <h2 class="section-title">{'自动任务的最新回执' if lang == 'zh' else 'Latest automation receipts'}</h2>
                  <div class="list-stack">{recent_jobs_html}</div>
                </article>

                <article class="card">
                  <span class="eyebrow">{'盘后动作 Feed' if lang == 'zh' else 'Close Review Feed'}</span>
                  <h2 class="section-title">{'复盘后优先执行什么' if lang == 'zh' else 'What to execute after the close review'}</h2>
                  <p class="section-copy">{close_review_action_feed.get('summary') or ('把复盘结果整理成动作列表。' if lang == 'zh' else 'Turn the close review into an action list.')}</p>
                  <div class="list-stack">{close_review_action_html}</div>
                </article>
              </div>

              <div class="stack">
                <article class="card">
                  <span class="eyebrow">{'最近模型运行' if lang == 'zh' else 'Recent Model Runs'}</span>
                  <h2 class="section-title">{'训练产出' if lang == 'zh' else 'Training output'}</h2>
                  <div class="list-stack">{recent_models_html}</div>
                </article>

                <article class="card">
                  <span class="eyebrow">{'同步状态摘要' if lang == 'zh' else 'Sync Snapshot'}</span>
                  <h2 class="section-title">{'最近同步到哪里' if lang == 'zh' else 'What was synced recently'}</h2>
                  <p class="section-copy">{'用最近几条同步状态快速确认数据新鲜度，详细操作再进入同步中心。' if lang == 'zh' else 'Use the latest sync rows to confirm freshness quickly, then open Sync Center for detailed operations.'}</p>
                  <div class="list-stack">{sync_rows_html}</div>
                </article>

                <article class="card">
                  <span class="eyebrow">{provider_strategy['ops_title']}</span>
                  <h2 class="section-title">{'任务配置与 provider 策略' if lang == 'zh' else 'Job configuration and provider policy'}</h2>
                  <p class="section-copy">{provider_strategy['ops_copy']}</p>
                  <div class="list-stack">
                    <article class="list-row"><div><div class="ticker">Price / Auto</div><div class="subtle">{provider_strategy['price_auto']}</div></div></article>
                    <article class="list-row"><div><div class="ticker">Fundamental / Auto</div><div class="subtle">{provider_strategy['fund_auto']}</div></div></article>
                    <article class="list-row"><div><div class="ticker">Concept / Auto</div><div class="subtle">{provider_strategy['concept_auto']}</div></div></article>
                  </div>
                </article>

                <article class="card">
                  <span class="eyebrow">{'快捷入口' if lang == 'zh' else 'Quick Links'}</span>
                  <div class="action-row">
                    <a class="cta" href="/dashboard/summary?lang={lang}&lookback_runs={lookback_runs}">{_dt(lang, 'dashboard_summary_json')}</a>
                    <a class="cta" href="/signals/latest">{_dt(lang, 'latest_signals_json')}</a>
                    <a class="cta" href="/backtests/latest/curve">{_dt(lang, 'latest_backtest_curve_json')}</a>
                    <a class="cta" href="/jobs/sync-states">{_dt(lang, 'sync_states_json')}</a>
                  </div>
                </article>
              </div>
            </section>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/ops/sync", response_class=HTMLResponse)
def dashboard_ops_sync_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/sync")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    tushare_ready = bool(get_settings().tushare_token)
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    sync_states = summary["sync_states"]
    recent_jobs = summary["recent_jobs"]
    dashboard_redirect = "/dashboard/ops/sync?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    close_review_status = close_review_scheduler_service.get_status()
    refresh_limit = int(close_review_status.get("refresh_limit") or 0)
    refresh_scope_label = (
        ("全市场" if refresh_limit == 0 else f"前 {refresh_limit} 只")
        if lang == "zh"
        else ("All CN" if refresh_limit == 0 else f"Top {refresh_limit}")
    )
    latest_cn_refresh = _latest_cn_refresh_summary(db, recent_jobs, lang=lang)
    cn_universe_job = next((item for item in recent_jobs if item["job_type"] == "sync_cn_symbol_universe"), None)
    cn_init_job = next((item for item in recent_jobs if item["job_type"] == "init_cn_market_data"), None)
    def _load_cn_sync_stats() -> dict:
        symbol_repo = SymbolRepository(db)
        technical_snapshot_repo = TechnicalSnapshotRepository(db)
        cn_symbols = [symbol for symbol in symbol_repo.list_symbols() if (symbol.market or "").upper() == "CN"]
        cn_ticker_set = {symbol.ticker for symbol in cn_symbols}
        cn_symbol_count = len(cn_symbols)
        cn_sync_success_count = sum(
            1 for item in sync_states if item["ticker"] in cn_ticker_set and item["status"] == "success"
        )
        cn_technical_snapshot_count = len(technical_snapshot_repo.list_latest_for_market("CN"))
        cn_progress_pct = round((cn_sync_success_count / cn_symbol_count) * 100, 1) if cn_symbol_count else 0.0
        next_cn_offset = cn_sync_success_count
        default_cn_batch_size = min(500, max(100, cn_symbol_count - cn_sync_success_count)) if cn_symbol_count > cn_sync_success_count else 0
        return {
            "cn_symbol_count": cn_symbol_count,
            "cn_sync_success_count": cn_sync_success_count,
            "cn_technical_snapshot_count": cn_technical_snapshot_count,
            "cn_progress_pct": cn_progress_pct,
            "next_cn_offset": next_cn_offset,
            "default_cn_batch_size": default_cn_batch_size,
        }

    cn_stats = get_or_set(
        "dashboard_ops_cn_sync_stats",
        json.dumps({"sync_rows": len(sync_states)}, sort_keys=True),
        ttl_seconds=60.0,
        loader=_load_cn_sync_stats,
    )
    cn_symbol_count = int(cn_stats.get("cn_symbol_count") or 0)
    cn_sync_success_count = int(cn_stats.get("cn_sync_success_count") or 0)
    cn_technical_snapshot_count = int(cn_stats.get("cn_technical_snapshot_count") or 0)
    cn_progress_pct = float(cn_stats.get("cn_progress_pct") or 0.0)
    next_cn_offset = int(cn_stats.get("next_cn_offset") or 0)
    default_cn_batch_size = int(cn_stats.get("default_cn_batch_size") or 0)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)
    visible_sync_states = sync_states[:200]
    sync_rows = "".join(
        f"<tr><td><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td><td>{item['provider']}</td><td>{item['last_synced_date'] or '-'}</td><td>{item['status'] or '-'}</td></tr>"
        for item in visible_sync_states
    ) or f"<tr><td colspan='4'>{'暂无同步记录' if lang == 'zh' else 'No sync history yet'}</td></tr>"
    sync_state_note = (
        f"仅展示最近 {len(visible_sync_states)} 条，同步总数 {len(sync_states)}。"
        if lang == "zh"
        else f"Showing the latest {len(visible_sync_states)} rows out of {len(sync_states)} sync states."
    )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'同步中心' if lang == 'zh' else 'Sync Center'}</title>
        <style>
          :root {{
            --bg:#071018;
            --bg-soft:#0d1722;
            --panel:#111c28;
            --panel-2:#152231;
            --ink:#e6edf3;
            --muted:#90a3b8;
            --line:#223246;
            --accent:#3dd9b6;
            --accent-2:#52a8ff;
            --warn:#f6c85f;
          }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:1180px; margin:0 auto; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .toolbar,.grid {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; align-items:center; }}
          .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .pill, .action-link {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.7); border:1px solid var(--line); color:var(--ink); text-decoration:none; font-size:13px; font-weight:700; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          form {{ display:grid; gap:10px; }}
          input, select, button {{ border-radius:14px; border:1px solid var(--line); padding:10px 12px; font:inherit; background:rgba(21,34,49,0.82); color:var(--ink); }}
          button {{ background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; border-color:transparent; font-weight:800; }}
          h1 {{ margin:0 0 8px; font-size:36px; line-height:1.05; letter-spacing:-0.03em; }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'任务中心' if lang == 'zh' else 'Ops Center'}</h1>
              <p>{'同步、训练、回测和自动任务都从这里收口。' if lang == 'zh' else 'Sync, training, backtests, and automation all flow through this workspace.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'同步页负责把市场数据、A 股全市场初始化、技术快照和收盘复盘入口集中起来。' if lang == 'zh' else 'The sync page centralizes market data, CN universe initialization, technical snapshots, and close-review entry points.'}</div>
          </aside>
          <main class="main">
          <div class="wrap">
          <div class="toolbar">
            <a href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}" class="pill">← {'返回运维操作台' if lang == 'zh' else 'Back to Operations'}</a>
            <a href="/dashboard/ops/sync?lang=en&lookback_runs={lookback_runs}" class="pill">English</a>
            <a href="/dashboard/ops/sync?lang=zh&lookback_runs={lookback_runs}" class="pill">中文</a>
          </div>
          <div class="card">
            <div class="eyebrow">{'同步中心' if lang == 'zh' else 'Sync Center'}</div>
            <h1>{'行情与基本面同步' if lang == 'zh' else 'Market and Fundamental Sync'}</h1>
            <p class="muted">{'专门处理市场数据、概念和基本面同步。' if lang == 'zh' else 'A focused page for market, concept, and fundamental sync workflows.'}</p>
          </div>
          {
            (
              "<section class='card' style='border-color:#f59e0b;background:#fff8eb;'>"
              + f"<div class='eyebrow'>{'需要配置 TuShare' if lang == 'zh' else 'TuShare Required'}</div>"
              + (
                f"<p class='muted'>{'A 股全市场股票池、基本面和概念同步需要先配置 ' if lang == 'zh' else 'CN full-market universe, fundamentals, and concept sync require '}"
                + "<code>PQW_TUSHARE_TOKEN</code>"
                + ( "。当前未检测到 token，所以这几项 job 会返回未配置。"
                    if lang == "zh"
                    else ". No token is currently configured, so these jobs will return not configured."
                  )
                + "</p>"
              )
              + "</section>"
            ) if not tushare_ready else ""
          }
          {
            (
              "<section class='card'>"
              + f"<div class='eyebrow'>{'最近股票池同步' if lang == 'zh' else 'Latest CN Universe Sync'}</div>"
              + f"<p class='muted'>{cn_universe_job['message'] or ('暂无记录' if lang == 'zh' else 'No recent run yet')}</p>"
              + "</section>"
            ) if cn_universe_job else ""
          }
          <section class="card">
            <div class="eyebrow">{'A股全市场初始化进度' if lang == 'zh' else 'CN Market Init Progress'}</div>
            <div class="muted">{'股票池总数' if lang == 'zh' else 'Universe'}: <strong>{cn_symbol_count}</strong></div>
            <div class="muted">{'已同步行情' if lang == 'zh' else 'Price Synced'}: <strong>{cn_sync_success_count}</strong></div>
            <div class="muted">{'技术缓存' if lang == 'zh' else 'Technical Snapshots'}: <strong>{cn_technical_snapshot_count}</strong></div>
            <div class="muted">{'最近全市场轻刷新' if lang == 'zh' else 'Latest Light Refresh'}: <strong>{latest_cn_refresh.get('summary') or '-'}</strong></div>
            <div class="muted" style="margin-top:8px;">{'最近初始化任务' if lang == 'zh' else 'Latest Init Job'}: <strong>{(cn_init_job or {}).get('status', 'idle')}</strong></div>
            <div style="margin-top:10px;height:12px;border-radius:999px;background:#efe7d7;overflow:hidden;">
              <div style="height:100%;width:{cn_progress_pct}%;background:linear-gradient(90deg,#0f766e,#34d399);"></div>
            </div>
            <div class="muted" style="margin-top:8px;">{cn_progress_pct}% ({cn_sync_success_count}/{cn_symbol_count})</div>
            <div class="muted" style="margin-top:8px;">{latest_cn_refresh.get('label') or ''}</div>
            <div style="margin-top:12px;">
              <a class="action-link" href="/screeners?lang={lang}&market=CN&universe=full_market&model_template=cn_bullish_ma_stack">{'去全市场技术选股' if lang == 'zh' else 'Open Full-Market Technical Screener'}</a>
            </div>
          </section>
          <section class="card">
            <div class="eyebrow">{'收盘自动复盘' if lang == 'zh' else 'Post-Close Review'}</div>
            <div class="muted">{'当前状态' if lang == 'zh' else 'Status'}: <strong>{('开启' if close_review_status['enabled'] else '关闭') if lang == 'zh' else ('Enabled' if close_review_status['enabled'] else 'Disabled')}</strong></div>
            <div class="muted">{'计划时间' if lang == 'zh' else 'Scheduled Time'}: <strong>{close_review_status['run_hour']:02d}:{close_review_status['run_minute']:02d}</strong> Asia/Shanghai</div>
            <div class="muted">{'全市场轻刷新范围' if lang == 'zh' else 'Market Light Refresh Scope'}: <strong>{refresh_scope_label}</strong></div>
            <div class="muted">{'下次运行' if lang == 'zh' else 'Next Run'}: <strong>{_display_time(close_review_status.get('next_run_at'))}</strong></div>
            <div class="muted">{'上次运行日期' if lang == 'zh' else 'Last Run Date'}: <strong>{close_review_status.get('last_run_date') or '-'}</strong></div>
            <div class="muted">{'失败后重试冷却' if lang == 'zh' else 'Retry Cooldown'}: <strong>{close_review_status.get('retry_cooldown_minutes', 60)}</strong> {'分钟' if lang == 'zh' else 'minute(s)'}</div>
            <div class="muted">{'当日最多尝试' if lang == 'zh' else 'Max Daily Attempts'}: <strong>{close_review_status.get('max_attempts_per_day', 4)}</strong></div>
            <div style="height:10px;"></div>
            <form action="/jobs/close-review/config" method="post">
              <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
              <input type="hidden" name="enabled" value="{'false' if close_review_status['enabled'] else 'true'}" />
              <input type="hidden" name="run_hour" value="{close_review_status['run_hour']}" />
              <input type="hidden" name="run_minute" value="{close_review_status['run_minute']}" />
              <input type="hidden" name="provider" value="{close_review_status['provider']}" />
              <input type="hidden" name="days_back" value="{close_review_status['days_back']}" />
              <input type="hidden" name="overlap_days" value="{close_review_status['overlap_days']}" />
              <input type="hidden" name="refresh_limit" value="{close_review_status['refresh_limit']}" />
              <input type="hidden" name="stale_job_hours" value="{close_review_status['stale_job_hours']}" />
              <input type="hidden" name="retry_cooldown_minutes" value="{close_review_status.get('retry_cooldown_minutes', 60)}" />
              <input type="hidden" name="max_attempts_per_day" value="{close_review_status.get('max_attempts_per_day', 4)}" />
              <button type="submit">{('关闭自动复盘' if close_review_status['enabled'] else '开启自动复盘') if lang == 'zh' else ('Disable Close Review' if close_review_status['enabled'] else 'Enable Close Review')}</button>
            </form>
            <div style="height:10px;"></div>
            <form action="/jobs/run-close-review" method="post">
              <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
              <button type="submit">{'立即执行收盘复盘' if lang == 'zh' else 'Run Close Review Now'}</button>
            </form>
            <div style="height:10px;"></div>
            <form action="/jobs/cleanup-stale-jobs" method="post">
              <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
              <input type="number" name="stale_job_hours" min="1" step="1" value="{close_review_status['stale_job_hours']}" />
              <button type="submit">{'清理卡住任务' if lang == 'zh' else 'Clean Stale Jobs'}</button>
            </form>
          </section>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'sync_market_data')}</div>
              <form action="/jobs/sync-market-data" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="hidden" name="lang" value="{lang}" />
                <input type="text" name="tickers" placeholder="AAPL,MSFT" />
                <select name="provider"><option value="auto">auto</option><option value="alpaca">Alpaca</option><option value="tushare">TuShare</option><option value="yfinance">yfinance</option><option value="openbb">OpenBB</option></select>
                <input type="text" name="start_date" placeholder="YYYY-MM-DD" />
                <input type="text" name="end_date" placeholder="YYYY-MM-DD" />
                <button type="submit">{_dt(lang, 'sync_market_data')}</button>
              </form>
              <div style="height:14px;"></div>
              <div class="eyebrow">{'美股收盘批量刷新' if lang == 'zh' else 'US Grouped Daily Refresh'}</div>
              <form action="/jobs/refresh-us-grouped-daily" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <div class="muted">{'通过 Polygon grouped daily 刷新美股全市场 EOD，默认只写 Parquet，不再生成 CSV。未配置 PQW_POLYGON_API_KEY 时会返回 not_configured。' if lang == 'zh' else 'Refresh U.S. full-market EOD via Polygon grouped daily. By default it writes only Parquet and no CSV. Returns not_configured until PQW_POLYGON_API_KEY is set.'}</div>
                <input type="text" name="trade_date" placeholder="YYYY-MM-DD ({'留空自动取最近美股交易日' if lang == 'zh' else 'blank for latest US trading day'})" />
                <input type="number" name="limit" min="0" step="1" value="0" placeholder="{ '调试限制，0 代表全部' if lang == 'zh' else 'Debug limit, 0 for all' }" />
                <label class="muted" style="display:flex;gap:8px;align-items:center;"><input type="checkbox" name="write_lake" value="true" checked style="width:auto;" /> {'写入 Parquet Market Lake（推荐）' if lang == 'zh' else 'Write Parquet Market Lake (recommended)'}</label>
                <label class="muted" style="display:flex;gap:8px;align-items:center;"><input type="checkbox" name="persist_per_symbol" value="true" style="width:auto;" /> {'写入逐票 CSV（较慢；一般不建议）' if lang == 'zh' else 'Write per-symbol CSVs (slower; usually not recommended)'}</label>
                <label class="muted" style="display:flex;gap:8px;align-items:center;"><input type="checkbox" name="normalize" value="true" style="width:auto;" /> {'同时重建 normalized CSV（需勾选逐票 CSV，较慢）' if lang == 'zh' else 'Also rebuild normalized CSVs (requires per-symbol CSVs, slower)'}</label>
                <button type="submit">{'刷新美股收盘行情' if lang == 'zh' else 'Refresh US EOD'}</button>
              </form>
              <div style="height:14px;"></div>
              <div class="eyebrow">{'预计算美股模型' if lang == 'zh' else 'Precompute US Screeners'}</div>
              <form action="/jobs/precompute-us-screeners" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <div class="muted">{'先基于本地已有美股池生成模型候选快照；接入 Polygon 全市场后会自动扩大覆盖。' if lang == 'zh' else 'Precompute model snapshots from the local U.S. symbol pool; coverage expands after Polygon full-market refresh.'}</div>
                <button type="submit">{'预计算美股候选' if lang == 'zh' else 'Precompute US Candidates'}</button>
              </form>
              <div style="height:14px;"></div>
              <div class="eyebrow">{'CSV 清理检查' if lang == 'zh' else 'CSV Cleanup Check'}</div>
              <form action="/jobs/cleanup-market-csv" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="hidden" name="dry_run" value="true" />
                <input type="hidden" name="markets" value="CN,US" />
                <div class="muted">{'只检查已被 Parquet lake 覆盖、可清理的 CSV；不会删除文件。真正删除需要单独确认。' if lang == 'zh' else 'Only checks CSV files already covered by the Parquet lake; no files are deleted. Actual deletion requires separate confirmation.'}</div>
                <button type="submit">{'检查可清理 CSV' if lang == 'zh' else 'Check Cleanup Candidates'}</button>
              </form>
            </article>
            <article class="card">
              <div class="eyebrow">{'同步 A 股股票池' if lang == 'zh' else 'Sync CN Market Universe'}</div>
              <form action="/jobs/sync-cn-symbol-universe" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <div class="muted">{'从 TuShare 主列表同步 A 股全市场股票池到本地 symbols。' if lang == 'zh' else 'Sync the full A-share stock universe from TuShare into local symbols.'}</div>
                <button type="submit">{'同步 A 股股票池' if lang == 'zh' else 'Sync CN Market Universe'}</button>
              </form>
              <div style="height:10px;"></div>
              <div class="eyebrow">{'初始化 A 股全市场数据' if lang == 'zh' else 'Init CN Market Data'}</div>
              <form action="/jobs/init-cn-market-data" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="number" name="days_back" min="30" step="1" value="180" placeholder="{ '回看天数' if lang == 'zh' else 'Days Back' }" />
                <input type="number" name="offset" min="0" step="1" value="{next_cn_offset}" placeholder="{ '起始偏移（默认接着当前进度）' if lang == 'zh' else 'Offset (resume from current progress)' }" />
                <input type="number" name="batch_size" min="0" step="1" value="{default_cn_batch_size}" placeholder="{ '本批数量（0 代表直到结束）' if lang == 'zh' else 'Batch Size (0 for remaining)' }" />
                <input type="number" name="limit" min="0" step="1" value="0" placeholder="{ '兼容限制（可留 0）' if lang == 'zh' else 'Compatibility limit (optional)' }" />
                <select name="provider"><option value="auto">auto</option><option value="tushare">TuShare</option></select>
                <button type="submit">{'初始化 A 股全市场数据' if lang == 'zh' else 'Init CN Market Data'}</button>
              </form>
              <div style="height:10px;"></div>
              <div class="eyebrow">{'刷新 A 股最近行情' if lang == 'zh' else 'Refresh Recent CN Market Data'}</div>
              <form action="/jobs/refresh-cn-market-data" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="number" name="days_back" min="2" step="1" value="7" placeholder="{ '刷新最近天数' if lang == 'zh' else 'Refresh Recent Days' }" />
                <input type="number" name="limit" min="0" step="1" value="0" placeholder="{ '股票数量限制（0 代表全部）' if lang == 'zh' else 'Limit (0 for all)' }" />
                <select name="provider"><option value="auto">auto</option><option value="tushare">TuShare</option></select>
                <button type="submit">{'刷新 A 股最近行情' if lang == 'zh' else 'Refresh Recent CN Market Data'}</button>
              </form>
              <div style="height:10px;"></div>
              <div class="eyebrow">{'重建技术形态缓存' if lang == 'zh' else 'Rebuild Technical Snapshots'}</div>
              <form action="/jobs/rebuild-technical-snapshots" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <select name="market">
                  <option value="CN">CN</option>
                  <option value="ALL">ALL</option>
                </select>
                <input type="number" name="limit" min="0" step="1" value="0" placeholder="{ '股票数量限制（0 代表全部）' if lang == 'zh' else 'Limit (0 for all)' }" />
                <button type="submit">{'重建技术形态缓存' if lang == 'zh' else 'Rebuild Technical Snapshots'}</button>
              </form>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'sync_cn_fundamentals')}</div>
              <form action="/jobs/sync-cn-fundamentals" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="text" name="tickers" placeholder="600519.SH,000001.SZ" />
                <button type="submit">{_dt(lang, 'sync_cn_fundamentals')}</button>
              </form>
              <div style="height:10px;"></div>
              <div class="eyebrow">{_dt(lang, 'sync_cn_concepts')}</div>
              <form action="/jobs/sync-cn-concepts" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="text" name="tickers" placeholder="600519.SH,000001.SZ" />
                <button type="submit">{_dt(lang, 'sync_cn_concepts')}</button>
              </form>
              <div style="height:10px;"></div>
              <div class="eyebrow">{_dt(lang, 'sync_us_hk_fundamentals')}</div>
              <form action="/jobs/sync-global-fundamentals" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="text" name="tickers" placeholder="ASTS,RKLB,0700.HK,0883.HK" />
                <button type="submit">{_dt(lang, 'sync_us_hk_fundamentals')}</button>
              </form>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'sync_states')}</div>
            <div class="muted" style="margin-bottom:10px;">{sync_state_note}</div>
            <div class="table-wrap"><table><thead><tr><th>{_dt(lang, 'ticker')}</th><th>{_dt(lang, 'provider')}</th><th>{_dt(lang, 'last_sync')}</th><th>{_dt(lang, 'status')}</th></tr></thead><tbody>{sync_rows}</tbody></table></div>
          </section>
          </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/ops/models", response_class=HTMLResponse)
def dashboard_ops_models_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/models")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    summary = _load_summary(db, lookback_runs=lookback_runs)
    recent_model_runs = summary["recent_model_runs"]
    latest_backtest = summary["latest_backtest"]
    latest_backtest_summary = (latest_backtest or {}).get("summary") or {}
    tradability_summary = latest_backtest_summary.get("tradability_summary") or {}
    capacity_summary = latest_backtest_summary.get("capacity_summary") or {}
    attribution_summary = latest_backtest_summary.get("attribution_summary") or {}
    portfolio_construction_summary = latest_backtest_summary.get("portfolio_construction_summary") or {}
    dashboard_redirect = "/dashboard/ops/models?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)
    model_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td><td title='{item['name']}'>{_compact_run_name(item['name'], 28)}</td><td>{item['status']}</td><td title='{item['config_json'] or '-'}'><code>{_compact_json_summary(item['config_json'], 64)}</code></td><td>{_display_time(item['created_at'])}</td>"
        f"<td><form action='/jobs/backtest' method='post' style='margin:0;'><input type='hidden' name='redirect_to' value='{dashboard_redirect}' /><input type='hidden' name='top_n' value='1' /><input type='hidden' name='model_run_id' value='{item['id']}' /><button type='submit' style='padding:8px 10px;font-size:12px;'>{_dt(lang, 'backtest_this_run')}</button></form></td>"
        "</tr>"
        for item in recent_model_runs
    ) or f"<tr><td colspan='6'>{'暂无模型运行' if lang == 'zh' else 'No model runs yet'}</td></tr>"
    backtest_pre = json.dumps(latest_backtest, indent=2) if latest_backtest else ("暂无回测" if lang == "zh" else "No backtest yet")
    tradeability_rows = "".join(
        f"<div class='mini-row'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in (
            (("候选数" if lang == "zh" else "Candidates"), int(tradability_summary.get("total_candidates") or 0)),
            (("可成交" if lang == "zh" else "Eligible"), int(tradability_summary.get("eligible_candidates") or 0)),
            (("最终入选" if lang == "zh" else "Selected"), int(tradability_summary.get("selected_candidates") or 0)),
            (("阻塞数" if lang == "zh" else "Blocked"), int(tradability_summary.get("blocked_candidates") or 0)),
            (("通过率" if lang == "zh" else "Pass Rate"), f"{float(tradability_summary.get('pass_rate') or 0.0) * 100:.1f}%"),
            (("选中率" if lang == "zh" else "Selection Rate"), f"{float(tradability_summary.get('selection_rate') or 0.0) * 100:.1f}%"),
        )
    ) or f"<div class='muted'>{'暂无可成交摘要' if lang == 'zh' else 'No tradability summary yet'}</div>"
    top_block_reasons = tradability_summary.get("top_block_reasons") or []
    top_block_html = "".join(
        f"<div class='tag'>{reason}: {count}</div>"
        for reason, count in top_block_reasons[:5]
    ) or f"<div class='muted'>{'暂无阻塞原因' if lang == 'zh' else 'No block reasons yet'}</div>"
    capacity_rows = "".join(
        f"<div class='mini-row'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in (
            (("最小 ADV" if lang == "zh" else "Min ADV"), f"{float(capacity_summary.get('min_adv') or 0.0):,.0f}"),
            (("最大跳空" if lang == "zh" else "Max Gap"), f"{float(capacity_summary.get('max_gap_pct') or 0.0) * 100:.1f}%"),
            (("单票上限" if lang == "zh" else "Max Position"), f"{float(capacity_summary.get('max_position_weight') or 0.0) * 100:.1f}%"),
            (("行业上限" if lang == "zh" else "Max Sector"), f"{float(capacity_summary.get('max_sector_weight') or 0.0) * 100:.1f}%"),
            (("平均持股数" if lang == "zh" else "Avg Names"), f"{float(capacity_summary.get('avg_selected_names') or 0.0):.1f}"),
            (("预估总暴露" if lang == "zh" else "Estimated Gross"), f"{float(capacity_summary.get('estimated_gross_exposure') or 0.0) * 100:.1f}%"),
        )
    ) or f"<div class='muted'>{'暂无容量摘要' if lang == 'zh' else 'No capacity summary yet'}</div>"
    attribution_rows = "".join(
        f"<div class='mini-row'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in (
            (("组合总收益" if lang == "zh" else "Portfolio Return"), f"{float(attribution_summary.get('portfolio_total_return') or 0.0) * 100:.2f}%"),
            (("基准收益" if lang == "zh" else "Benchmark Return"), f"{float(attribution_summary.get('benchmark_total_return') or 0.0) * 100:.2f}%"),
            (("超额收益" if lang == "zh" else "Excess Return"), f"{float(attribution_summary.get('excess_total_return') or 0.0) * 100:.2f}%"),
            (("日均 Alpha" if lang == "zh" else "Avg Daily Alpha"), f"{float(attribution_summary.get('avg_daily_alpha') or 0.0) * 100:.3f}%"),
            (("成本拖累" if lang == "zh" else "Cost Drag"), f"{float(attribution_summary.get('cost_drag_bps') or 0.0):.1f} bps"),
        )
    ) or f"<div class='muted'>{'暂无归因摘要' if lang == 'zh' else 'No attribution summary yet'}</div>"
    construction_rows = "".join(
        f"<div class='mini-row'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in (
            (("权重规则" if lang == "zh" else "Weighting"), portfolio_construction_summary.get("weighting_rule") or "-"),
            (("持仓规则" if lang == "zh" else "Continuity"), portfolio_construction_summary.get("continuity_rule") or "-"),
            (("Top N" if lang == "zh" else "Top N"), portfolio_construction_summary.get("top_n") or 0),
            (("持有天数" if lang == "zh" else "Holding Days"), portfolio_construction_summary.get("holding_days") or 0),
            (("调仓阈值" if lang == "zh" else "Rebalance Threshold"), f"{float(portfolio_construction_summary.get('rebalance_threshold') or 0.0) * 100:.1f}%"),
            (("平均持股数" if lang == "zh" else "Avg Names"), f"{float(portfolio_construction_summary.get('avg_selected_names') or 0.0):.1f}"),
        )
    ) or f"<div class='muted'>{'暂无组合构建摘要' if lang == 'zh' else 'No construction summary yet'}</div>"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>{'模型运行' if lang == 'zh' else 'Model Runs'}</title>
      <style>
        :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-2:#52a8ff; }}
        * {{ box-sizing:border-box; }} body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
        .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }} {WORKSPACE_SIDEBAR_STYLE}
        .main {{ padding:28px 30px 48px; }} .wrap {{ max-width:1180px; margin:0 auto; }} .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
        .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }} .pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); text-decoration:none; font-size:13px; font-weight:700; }}
        .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
        .muted {{ color:var(--muted); font-size:14px; }} .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); margin-bottom:16px; }} .mini-row {{ display:flex; justify-content:space-between; gap:12px; padding:10px 0; border-top:1px solid var(--line); font-size:14px; }} .tag-row {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }} .tag {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; }} .table-wrap {{ width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }} table {{ width:100%; min-width:820px; border-collapse:collapse; font-size:14px; }} th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }} th {{ color:var(--muted); font-weight:600; }}
        input, select, button {{ border-radius:14px; border:1px solid var(--line); padding:10px 12px; font:inherit; background:rgba(21,34,49,0.82); color:var(--ink); }} button {{ background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; border-color:transparent; font-weight:800; }} pre, code {{ white-space:pre-wrap; word-break:break-word; }}
        h1 {{ margin:0 0 8px; font-size:36px; line-height:1.05; letter-spacing:-0.03em; }}
      </style></head>
      <body><div class="app"><aside class="sidebar"><div class="brand"><span class="brand-tag">PQW</span><h1>{'模型与回测' if lang == 'zh' else 'Models'}</h1><p>{'把训练、回测、可成交性和组合构建放在同一屏里审视。' if lang == 'zh' else 'Review training, backtests, tradability, and construction in one screen.'}</p></div><nav class="side-nav">{nav_html}</nav><div class="sidebar-foot">{'这个页面更像模型审查台，不只是看曲线，也看容量、阻塞和收益归因。' if lang == 'zh' else 'This is a model review desk, not just a curve page.'}</div></aside><main class="main"><div class="wrap">
        <div class="toolbar">
          <a href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}" class="pill">← {'返回运维操作台' if lang == 'zh' else 'Back to Operations'}</a>
          <a href="/dashboard/ops/models?lang=en&lookback_runs={lookback_runs}" class="pill">English</a>
          <a href="/dashboard/ops/models?lang=zh&lookback_runs={lookback_runs}" class="pill">中文</a>
        </div>
        <div class="card"><div class="eyebrow">{'模型运行' if lang == 'zh' else 'Model Runs'}</div><h1>{'训练与回测视图' if lang == 'zh' else 'Training and Backtest View'}</h1><p class="muted">{'专门查看最近模型运行并从这里回测。' if lang == 'zh' else 'Review recent model runs and trigger backtests from here.'}</p></div>
        <section class="card">
          <div class="eyebrow">{_dt(lang, 'run_training')}</div>
          <form action="/jobs/train" method="post" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));align-items:end;">
            <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
            <input type="text" name="run_name" value="baseline_momentum" placeholder="{_dt(lang, 'run_name')}" />
            <select name="signal_type"><option value="momentum">Momentum</option><option value="reversal">Reversal</option></select>
            <input type="number" name="lookback_days" min="1" step="1" value="3" />
            <button type="submit">{_dt(lang, 'run_training')}</button>
          </form>
        </section>
        <section class="card">
          <div class="eyebrow">{_dt(lang, 'recent_model_runs')}</div>
          <div class="table-wrap"><table><thead><tr><th>ID</th><th>{_dt(lang, 'name')}</th><th>{_dt(lang, 'status')}</th><th>{_dt(lang, 'config')}</th><th>{_dt(lang, 'created')}</th><th>{_dt(lang, 'action')}</th></tr></thead><tbody>{model_rows}</tbody></table></div>
        </section>
        <section class="grid">
          <article class="card">
            <div class="eyebrow">{'Tradability' if lang == 'en' else '可成交性'}</div>
            <div class="muted">{'这次回测到底有多少候选能通过交易门槛。' if lang == 'zh' else 'How many candidates actually passed the tradeability gates.'}</div>
            <div>{tradeability_rows}</div>
            <div class="tag-row">{top_block_html}</div>
          </article>
          <article class="card">
            <div class="eyebrow">{'Capacity' if lang == 'en' else '容量'}</div>
            <div class="muted">{capacity_summary.get('capacity_comment') or ('回测容量与流动性摘要。' if lang == 'zh' else 'Capacity and liquidity summary for the backtest.')}</div>
            <div>{capacity_rows}</div>
          </article>
          <article class="card">
            <div class="eyebrow">{'Attribution' if lang == 'en' else '归因'}</div>
            <div class="muted">{attribution_summary.get('alpha_source_hint') or ('先看收益来自超额，还是主要来自执行假设。' if lang == 'zh' else 'Check whether returns come from alpha or from execution assumptions.')}</div>
            <div>{attribution_rows}</div>
          </article>
          <article class="card">
            <div class="eyebrow">{'Construction' if lang == 'en' else '组合构建'}</div>
            <div class="muted">{'把权重、换手和延续规则一起看，才能知道这条曲线是不是可实现。' if lang == 'zh' else 'Review weighting, turnover, and continuity rules together to judge whether the curve is implementable.'}</div>
            <div>{construction_rows}</div>
          </article>
        </section>
        <section class="card"><div class="eyebrow">{_dt(lang, 'backtest_summary')}</div><pre>{backtest_pre}</pre></section>
      </div></main></div></body></html>
    """


@router.get("/ops/jobs", response_class=HTMLResponse)
def dashboard_ops_jobs_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/jobs")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    recent_jobs = load_recent_jobs_summary(db, limit=12)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)
    def status_badge(status: str) -> str:
        tone = {"success":("#dcfce7","#166534"),"failed":("#fee2e2","#991b1b"),"partial":("#fef3c7","#92400e"),"running":("#dbeafe","#1d4ed8")}.get(status,("#e5e7eb","#374151"))
        return f"<span style='display:inline-block;padding:4px 8px;border-radius:999px;background:{tone[0]};color:{tone[1]};font-size:12px;font-weight:700;'>{status}</span>"

    def result_summary(job: dict) -> str:
        result = job.get("result") or {}
        if not isinstance(result, dict):
            return "-"
        if str(job.get("job_type") or "").lower() == "screener_precompute":
            created = list(result.get("snapshots_created") or [])
            failed = list(result.get("failed_templates") or [])
            total = len(created) + len(failed)
            created_label = len(created)
            if not total:
                return "待写入" if lang == "zh" else "Pending"
            prefix = (
                f"成功 {created_label}/{total}"
                if lang == "zh"
                else f"{created_label}/{total} succeeded"
            )
            if failed:
                failed_models = ", ".join(
                    _compact_label(str(item.get("model_template") or "-"), 18)
                    for item in failed[:3]
                )
                suffix = (
                    f"；失败 {len(failed)} 个：{failed_models}"
                    if lang == "zh"
                    else f"; failed {len(failed)}: {failed_models}"
                )
                return prefix + suffix
            return prefix
        if "snapshots_created" in result:
            created = list(result.get("snapshots_created") or [])
            failed_count = int(result.get("failed_count") or 0)
            total = len(created) + failed_count
            return f"{len(created)}/{total}" if total else "-"
        return _compact_json_summary(result, 48)

    recent_job_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td>"
        f"<td title='{item['job_type']}'>{_compact_job_type(item['job_type'], 22)}</td>"
        f"<td>{status_badge(item['status'])}</td>"
        f"<td>{_display_time(item['started_at'])}</td>"
        f"<td>{_display_time(item['finished_at'])}</td>"
        f"<td title='{json.dumps(item['params'], ensure_ascii=False) if item['params'] else '-'}'><code>{_compact_json_summary(item['params'], 52)}</code></td>"
        f"<td>"
        f"<div title='{item['message'] or '-'}'>{_compact_label(item['message'] or '-', 42)}</div>"
        f"<div class='job-subtle' title='{result_summary(item)}'>{result_summary(item)}</div>"
        f"</td>"
        "</tr>"
        for item in recent_jobs
    ) or f"<tr><td colspan='7'>{'暂无任务' if lang == 'zh' else 'No jobs yet'}</td></tr>"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>{'任务记录' if lang == 'zh' else 'Job History'}</title>
      <style>
        :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
        * {{ box-sizing:border-box; }} body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
        .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }} {WORKSPACE_SIDEBAR_STYLE}
        .main {{ padding:28px 30px 48px; }} .wrap {{ max-width:1180px; margin:0 auto; }} .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
        .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }} .pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); text-decoration:none; font-size:13px; font-weight:700; }}
        .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
        .muted {{ color:var(--muted); font-size:14px; }} .job-subtle {{ margin-top:6px; color:var(--muted); font-size:12px; line-height:1.4; white-space:normal; }} .table-wrap {{ width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }} table {{ width:100%; min-width:960px; border-collapse:collapse; font-size:14px; }} th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }} th {{ color:var(--muted); font-weight:600; }} code {{ white-space:pre-wrap; word-break:break-word; }}
        h1 {{ margin:0 0 8px; font-size:36px; line-height:1.05; letter-spacing:-0.03em; }}
      </style></head>
      <body><div class="app"><aside class="sidebar"><div class="brand"><span class="brand-tag">PQW</span><h1>{'任务记录' if lang == 'zh' else 'Job History'}</h1><p>{'最近一次同步、训练、回测和失败信息，都在这里回看。' if lang == 'zh' else 'Review recent sync, training, backtest, and failure details here.'}</p></div><nav class="side-nav">{nav_html}</nav><div class="sidebar-foot">{'这页适合看参数、错误和状态，不适合承载复杂分析，所以保持轻量。' if lang == 'zh' else 'This page stays light and focuses on params, errors, and status history.'}</div></aside><main class="main"><div class="wrap">
        <div class="toolbar">
          <a href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}" class="pill">← {'返回运维操作台' if lang == 'zh' else 'Back to Operations'}</a>
          <a href="/dashboard/ops/jobs?lang=en&lookback_runs={lookback_runs}" class="pill">English</a>
          <a href="/dashboard/ops/jobs?lang=zh&lookback_runs={lookback_runs}" class="pill">中文</a>
        </div>
        <div class="card"><div class="eyebrow">{'任务记录' if lang == 'zh' else 'Job History'}</div><h1>{'最近任务与参数' if lang == 'zh' else 'Recent Jobs and Parameters'}</h1><p class="muted">{'单独查看任务成功、失败和参数详情。' if lang == 'zh' else 'Inspect recent success, failure, and run parameters in one place.'}</p></div>
        <section class="card"><div class="eyebrow">{_dt(lang, 'recent_jobs')}</div><div class="table-wrap"><table><thead><tr><th>ID</th><th>{_dt(lang, 'type')}</th><th>{_dt(lang, 'status')}</th><th>{_dt(lang, 'started')}</th><th>{_dt(lang, 'finished')}</th><th>{_dt(lang, 'params')}</th><th>{_dt(lang, 'message')}</th></tr></thead><tbody>{recent_job_rows}</tbody></table></div></section>
      </div></main></div></body></html>
    """


@router.get("", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    lang = resolve_request_lang(request)
    session_mode = str(request.query_params.get("mode", "monitor")).lower()
    if session_mode not in {"premarket", "monitor", "postmarket"}:
        session_mode = "monitor"
    lookback_runs = _clamp_lookback_runs(request.query_params.get("lookback_runs", 5))
    heatmap_sort = str(request.query_params.get("heatmap_sort", "hits"))
    continuous_sort_by = str(request.query_params.get("continuous_sort_by", "hits"))
    continuous_sort_order = str(request.query_params.get("continuous_sort_order", "desc"))
    continuous_market = str(request.query_params.get("continuous_market", "ALL")).upper()
    continuous_state = str(request.query_params.get("continuous_state", "ALL")).upper()
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    recent_jobs = summary["recent_jobs"]
    home_watchlist_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_HOME_WATCHLIST)
    home_portfolio_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_HOME_PORTFOLIO)
    model_candidates_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MODEL_CANDIDATES)
    pipeline_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_PIPELINE_STATUS)
    dashboard_nlp_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_DASHBOARD_NLP)
    job_status = request.query_params.get("job_status")
    job_id = request.query_params.get("job_id")
    job_message = request.query_params.get("job_message")
    banner_html = ""
    if job_status or job_message:
        tone = {
            "success": ("#10261b", "#8af0a6"),
            "failed": ("#2b1520", "#ff93a4"),
            "partial": ("#2b2412", "#ffd982"),
        }.get(job_status or "", ("#172534", "#d7e2ec"))
        banner_html = (
            f"<div class='banner' style='background:{tone[0]};color:{tone[1]};'>"
            f"Job {job_id or '-'} · {job_status or 'done'} · {job_message or 'Completed'}"
            f"</div>"
        )
    watchlist_rows = _payload_rows(home_watchlist_snapshot) or _dashboard_home_watchlist_rows(db, lang=lang, session_mode=session_mode)
    portfolio_rows = _payload_rows(home_portfolio_snapshot)
    model_candidate_rows = _payload_rows(model_candidates_snapshot)
    portfolio_payload = (home_portfolio_snapshot or {}).get("payload") if isinstance(home_portfolio_snapshot, dict) else None
    pipeline_payload = (pipeline_snapshot or {}).get("payload") if isinstance(pipeline_snapshot, dict) else None
    portfolio_totals = (portfolio_payload or {}).get("totals") if isinstance(portfolio_payload, dict) else None
    if not portfolio_rows or not isinstance(portfolio_totals, dict):
        portfolio_rows, portfolio_totals = _dashboard_home_portfolio_rows(db, lang=lang)
    recent_jobs = _payload_rows(pipeline_snapshot) and ((pipeline_snapshot or {}).get("payload") or {}).get("recent_jobs") or recent_jobs
    return _render_dashboard_workspace(
        lang=lang,
        session_mode=session_mode,
        lookback_runs=lookback_runs,
        summary=summary,
        watchlist_rows=watchlist_rows,
        portfolio_rows=portfolio_rows,
        model_candidate_rows=model_candidate_rows,
        portfolio_totals=portfolio_totals,
        portfolio_meta=portfolio_payload.get("meta") if isinstance(portfolio_payload, dict) else {},
        pipeline_payload=pipeline_payload if isinstance(pipeline_payload, dict) else {},
        recent_jobs=recent_jobs,
        banner_html=banner_html,
        nlp_payload=((dashboard_nlp_snapshot or {}).get("payload") if isinstance(dashboard_nlp_snapshot, dict) else {}) or {},
    )
    generated_at = summary["generated_at"]
    auto_analysis = summary["auto_analysis"]
    data_sources = summary["data_sources"]
    latest_model = summary["latest_model"]
    recent_model_runs = summary["recent_model_runs"]
    latest_backtest = summary["latest_backtest"]
    latest_backtest_curve = summary["latest_backtest_curve"]
    latest_signals = summary["latest_signals"]
    sync_states = summary["sync_states"]
    recent_jobs = summary["recent_jobs"]
    market_context = summary["market_context"]
    job_status = request.query_params.get("job_status")
    job_id = request.query_params.get("job_id")
    job_message = request.query_params.get("job_message")
    dashboard_redirect = "/dashboard?" + urlencode(
        {
            "lang": lang,
            "lookback_runs": lookback_runs,
            "heatmap_sort": heatmap_sort,
            "continuous_sort_by": continuous_sort_by,
            "continuous_sort_order": continuous_sort_order,
            "continuous_market": continuous_market,
            "continuous_state": continuous_state,
            "mode": session_mode,
        }
    )

    risk_overview = market_context.get("risk_overview", {})
    mode_switch = "".join(
        (
            f"<a href='/dashboard?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'continuous_sort_by': continuous_sort_by, 'continuous_sort_order': continuous_sort_order, 'continuous_market': continuous_market, 'continuous_state': continuous_state, 'mode': value})}' "
            "class='pill' "
            f"style='background:{'#0f766e' if value == session_mode else '#eef8f5'};color:{'#fff' if value == session_mode else '#0f766e'};'>{label}</a>"
        )
        for value, label in (
            ("premarket", "盘前" if lang == "zh" else "Premarket"),
            ("monitor", "盘中观察" if lang == "zh" else "Monitor"),
            ("postmarket", "盘后复盘" if lang == "zh" else "Postmarket"),
        )
    )
    top_panels_url = "/dashboard/top-fragment?" + urlencode(
        {
            "lang": lang,
            "lookback_runs": lookback_runs,
        }
    )
    home_panels_url = "/dashboard/home-panels-fragment?" + urlencode(
        {
            "lang": lang,
            "lookback_runs": lookback_runs,
            "mode": session_mode,
            "continuous_sort_by": continuous_sort_by,
            "continuous_sort_order": continuous_sort_order,
            "continuous_market": continuous_market,
            "continuous_state": continuous_state,
        }
    )
    lookback_pills = _lookback_pills("/dashboard", selected=lookback_runs, extra_params={"lang": lang, "heatmap_sort": heatmap_sort, "continuous_sort_by": continuous_sort_by, "continuous_sort_order": continuous_sort_order, "continuous_market": continuous_market, "continuous_state": continuous_state, "mode": session_mode})
    lang_switch = (
        f"<a href='/dashboard?{urlencode({'lang': 'en', 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'continuous_sort_by': continuous_sort_by, 'continuous_sort_order': continuous_sort_order, 'continuous_market': continuous_market, 'continuous_state': continuous_state, 'mode': session_mode})}' class='pill'>{_dt('en', 'lang_en')}</a>"
        f"<a href='/dashboard?{urlencode({'lang': 'zh', 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'continuous_sort_by': continuous_sort_by, 'continuous_sort_order': continuous_sort_order, 'continuous_market': continuous_market, 'continuous_state': continuous_state, 'mode': session_mode})}' class='pill'>{_dt('zh', 'lang_zh')}</a>"
    )

    banner_html = ""
    if job_status or job_message:
        tone = {
            "success": ("#dcfce7", "#166534"),
            "failed": ("#fee2e2", "#991b1b"),
            "partial": ("#fef3c7", "#92400e"),
        }.get(job_status or "", ("#e5e7eb", "#374151"))
        banner_html = (
            f"<div style='margin-bottom:18px;padding:14px 16px;border-radius:16px;"
            f"background:{tone[0]};color:{tone[1]};font-weight:600;'>"
            f"Job {job_id or '-'} · {job_status or 'done'} · {job_message or 'Completed'}"
            f"</div>"
        )

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{_dt(lang, 'title')}</title>
        <style>
          :root {{
            --bg: #f5efe2;
            --panel: #fffdf7;
            --ink: #1f2937;
            --muted: #6b7280;
            --line: #d6cfc2;
            --accent: #0f766e;
            --accent-soft: #dff5ef;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, #fff6d8 0, transparent 30%),
              radial-gradient(circle at top right, #d9f3ee 0, transparent 35%),
              var(--bg);
          }}
          .wrap {{
            max-width: 1080px;
            margin: 0 auto;
            padding: 32px 20px 56px;
          }}
          h1 {{
            margin: 0 0 8px;
            font-size: 38px;
            line-height: 1.05;
          }}
          p.lead {{
            margin: 0 0 24px;
            color: var(--muted);
            max-width: 720px;
          }}
          .grid {{
            display: grid;
            gap: 16px;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            margin-bottom: 16px;
          }}
          .card {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 8px 24px rgba(31, 41, 55, 0.05);
          }}
          .eyebrow {{
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            background: var(--accent-soft);
            color: var(--accent);
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            margin-bottom: 12px;
          }}
          .metric {{
            font-size: 28px;
            font-weight: 700;
            margin: 6px 0;
          }}
          .toolbar {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
            margin-bottom: 18px;
          }}
          .muted {{
            color: var(--muted);
            font-size: 14px;
          }}
          .pill {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: #eef8f5;
            color: #0f766e;
            font-size: 13px;
            font-weight: 700;
          }}
          .switch-row {{
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:12px;
            margin-top:12px;
          }}
          .switch-pill {{
            display:inline-flex;
            align-items:center;
            gap:8px;
            padding:8px 12px;
            border-radius:999px;
            font-size:13px;
            font-weight:700;
          }}
          .switch-pill.on {{
            background:#dcfce7;
            color:#166534;
          }}
          .switch-pill.off {{
            background:#fee2e2;
            color:#991b1b;
          }}
          button {{
            border: 1px solid #0f766e;
            background: #0f766e;
            color: #fff;
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
          }}
          button:hover {{
            background: #0c625c;
          }}
          .action-form {{
            display: grid;
            gap: 10px;
            margin-bottom: 12px;
          }}
          .nav-grid {{
            display:grid;
            gap:16px;
            grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));
            margin-bottom:16px;
          }}
          .nav-card {{
            display:block;
            text-decoration:none;
            color:inherit;
            background:linear-gradient(180deg, #fffdf7 0%, #f8faf7 100%);
            border:1px solid var(--line);
            border-radius:18px;
            padding:18px;
            box-shadow:0 8px 24px rgba(31,41,55,0.05);
          }}
          .nav-card:hover {{
            border-color:#0f766e;
            box-shadow:0 12px 28px rgba(15,118,110,0.10);
          }}
          .nav-head {{
            display:flex;
            align-items:center;
            gap:12px;
            margin-bottom:10px;
          }}
          .nav-icon {{
            width:42px;
            height:42px;
            border-radius:14px;
            display:inline-flex;
            align-items:center;
            justify-content:center;
            background:#eef8f5;
            color:#0f766e;
            font-size:12px;
            font-weight:900;
            letter-spacing:0.04em;
            border:1px solid #cde9e4;
            flex:0 0 auto;
          }}
          .nav-title {{
            font-size:18px;
            font-weight:800;
            color:#0f766e;
          }}
          .nav-kicker {{
            color:var(--muted);
            font-size:12px;
            font-weight:700;
            letter-spacing:0.04em;
            text-transform:uppercase;
          }}
          .mini-grid {{
            display:grid;
            gap:16px;
            grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));
            margin-bottom:16px;
          }}
          .signal-grid {{
            display:grid;
            gap:12px;
            grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));
          }}
          .leader-grid {{
            display:grid;
            gap:12px;
            grid-template-columns:repeat(auto-fit, minmax(240px, 1fr));
          }}
          .signal-card {{
            border:1px solid var(--line);
            border-radius:16px;
            padding:14px;
            background:linear-gradient(180deg, #fffdf7 0%, #f8faf7 100%);
          }}
          .signal-top {{
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:8px;
            margin-bottom:6px;
          }}
          .signal-ticker {{
            color:#0f766e;
            text-decoration:none;
            font-size:18px;
            font-weight:800;
          }}
          .signal-rank {{
            display:inline-flex;
            align-items:center;
            padding:4px 8px;
            border-radius:999px;
            background:#eef8f5;
            color:#0f766e;
            font-size:12px;
            font-weight:800;
          }}
          .signal-date {{
            color:var(--muted);
            font-size:13px;
            margin-bottom:10px;
          }}
          .signal-score {{
            font-size:24px;
            font-weight:800;
            color:#1f2937;
            margin-bottom:8px;
          }}
          .signal-foot {{
            color:var(--muted);
            font-size:12px;
          }}
          .leader-card {{
            border:1px solid var(--line);
            border-radius:16px;
            padding:14px;
            background:linear-gradient(180deg, #fffdf7 0%, #f8faf7 100%);
          }}
          .leader-top {{
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:8px;
            margin-bottom:6px;
          }}
          .leader-ticker {{
            color:#0f766e;
            text-decoration:none;
            font-size:18px;
            font-weight:800;
          }}
          .leader-market {{
            display:inline-flex;
            align-items:center;
            padding:4px 8px;
            border-radius:999px;
            background:#eef8f5;
            color:#0f766e;
            font-size:12px;
            font-weight:800;
          }}
          .leader-name {{
            color:var(--muted);
            font-size:13px;
            margin-bottom:10px;
          }}
          .leader-metrics {{
            display:flex;
            gap:8px;
            flex-wrap:wrap;
            margin-bottom:10px;
          }}
          .leader-chip {{
            display:inline-flex;
            align-items:center;
            padding:4px 8px;
            border-radius:999px;
            background:#f3f4f6;
            color:#374151;
            font-size:12px;
            font-weight:800;
          }}
          .leader-trend {{
            margin-bottom:10px;
          }}
          .leader-foot {{
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:10px;
            color:var(--muted);
            font-size:12px;
          }}
          .action-row {{
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
          }}
          input[type="number"] {{
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 8px 10px;
            font-size: 14px;
            width: 96px;
            background: #fff;
            color: var(--ink);
          }}
          input[type="text"] {{
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 8px 10px;
            font-size: 14px;
            width: 100%;
            background: #fff;
            color: var(--ink);
          }}
          select {{
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 8px 10px;
            font-size: 14px;
            background: #fff;
            color: var(--ink);
          }}
          .checkbox-row {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            color: var(--muted);
            font-size: 14px;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
          }}
          th, td {{
            text-align: left;
            padding: 10px 8px;
            border-bottom: 1px solid var(--line);
          }}
          th {{
            color: var(--muted);
            font-weight: 600;
          }}
          pre {{
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            font-size: 13px;
            color: #0b3b36;
          }}
          code {{
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 12px;
            background: #f3f4f6;
            padding: 2px 6px;
            border-radius: 8px;
          }}
          .heat-grid {{
            display:grid;
            gap:12px;
            grid-template-columns:repeat(auto-fit, minmax(160px, 1fr));
            margin-top:12px;
          }}
          .heat-tile {{
            color:#fff;
            border-radius:16px;
            padding:14px;
            min-height:110px;
            display:flex;
            flex-direction:column;
            justify-content:space-between;
            box-shadow:0 8px 24px rgba(15,118,110,0.12);
            text-decoration:none;
          }}
          .heat-label {{ font-weight:800; line-height:1.3; }}
          .heat-metric {{ font-size:22px; font-weight:800; }}
          .heat-meta {{ font-size:12px; opacity:0.92; }}
        </style>
        <script>
          const AUTO_REFRESH_MS = 10000;
          let refreshTimer = null;

          function scheduleRefresh() {{
            if (refreshTimer) {{
              clearTimeout(refreshTimer);
            }}
            refreshTimer = setTimeout(() => {{
              loadDashboardFragments();
              scheduleRefresh();
            }}, AUTO_REFRESH_MS);
          }}

          window.addEventListener("DOMContentLoaded", () => {{
            const checkbox = document.getElementById("auto-refresh");
            const label = document.getElementById("refresh-label");
            const button = document.getElementById("refresh-now");
            const homePanels = document.getElementById("dashboard-home-panels");
            const topPanels = document.getElementById("dashboard-top-panels");
            const homePanelsFallback = "<article class='card'><div class='eyebrow'>{'首页扩展面板' if lang == 'zh' else 'Home Panels'}</div><div class='muted'>{'加载失败，请稍后刷新。' if lang == 'zh' else 'Failed to load. Please refresh later.'}</div></article>";
            const topPanelsFallback = "<section class='card'><div class='eyebrow'>{'顶部面板' if lang == 'zh' else 'Top Panels'}</div><div class='muted'>{'加载失败，请稍后刷新。' if lang == 'zh' else 'Failed to load. Please refresh later.'}</div></section>";

            const saved = localStorage.getItem("dashboard_auto_refresh");
            const enabled = saved === null ? true : saved === "true";
            checkbox.checked = enabled;

            const updateLabel = () => {{
              label.textContent = checkbox.checked ? "{'每 10 秒自动刷新' if lang == 'zh' else 'Auto-refresh every 10s'}" : "{'已暂停自动刷新' if lang == 'zh' else 'Auto-refresh paused'}";
            }};

            const loadDashboardFragments = () => {{
              if (homePanels) {{
                fetch("{home_panels_url}", {{ credentials: "same-origin" }})
                  .then((response) => response.text())
                  .then((html) => {{
                    homePanels.innerHTML = html;
                  }})
                  .catch(() => {{
                    homePanels.innerHTML = homePanelsFallback;
                  }});
              }}

              if (topPanels) {{
                fetch("{top_panels_url}", {{ credentials: "same-origin" }})
                  .then((response) => response.text())
                  .then((html) => {{
                    topPanels.innerHTML = html;
                  }})
                  .catch(() => {{
                    topPanels.innerHTML = topPanelsFallback;
                  }});
              }}
            }};

            updateLabel();

            loadDashboardFragments();

            if (checkbox.checked) {{
              scheduleRefresh();
            }}

            checkbox.addEventListener("change", () => {{
              localStorage.setItem("dashboard_auto_refresh", String(checkbox.checked));
              updateLabel();
              if (checkbox.checked) {{
                scheduleRefresh();
              }} else if (refreshTimer) {{
                clearTimeout(refreshTimer);
              }}
            }});

            button.addEventListener("click", () => loadDashboardFragments());
          }});
        </script>
      </head>
      <body>
        <main class="wrap">
          <div class="eyebrow">{_dt(lang, 'hero')}</div>
          <h1>{_dt(lang, 'title')}</h1>
          <p class="lead">{_dt(lang, 'lead')}</p>
          {banner_html}
          <div class="toolbar">
            <span class="pill" id="refresh-label">{'每 10 秒自动刷新' if lang == 'zh' else 'Auto-refresh every 10s'}</span>
            <label class="muted" style="display:inline-flex;align-items:center;gap:8px;">
              <input type="checkbox" id="auto-refresh" checked />
              {'自动刷新' if lang == 'zh' else 'Auto refresh'}
            </label>
            <button id="refresh-now" type="button">{'立即刷新' if lang == 'zh' else 'Refresh Now'}</button>
            <span class="muted">{'最近更新' if lang == 'zh' else 'Last updated'}: {generated_at}</span>
            <a href="/watchlist?lang={lang}&mode={session_mode}" style="color:#0f766e;font-weight:700;text-decoration:none;">{_dt(lang, 'open_watchlist')}</a>
            <a href="/screeners?lang={lang}" style="color:#0f766e;font-weight:700;text-decoration:none;">{_dt(lang, 'open_screener')}</a>
            <a href="/screeners/market-snapshot?lang={lang}&mode={session_mode}" style="color:#0f766e;font-weight:700;text-decoration:none;">{'Market Snapshot' if lang == 'en' else '市场快照榜单'}</a>
            <a href="/dashboard/data-sources?lang={lang}" style="color:#0f766e;font-weight:700;text-decoration:none;">{_dt(lang, 'data_sources')}</a>
            <a href="/logout" style="color:#0f766e;font-weight:700;text-decoration:none;">{_dt(lang, 'logout')}</a>
            {lang_switch}
          </div>
          <div class="card" style="margin-bottom:16px;">
            <div class="eyebrow">{'Session Mode' if lang == 'en' else '会话模式'}</div>
            <div class="metric">{'盘前' if session_mode == 'premarket' and lang == 'zh' else '盘中观察' if session_mode == 'monitor' and lang == 'zh' else '盘后复盘' if lang == 'zh' else 'Premarket' if session_mode == 'premarket' else 'Monitor' if session_mode == 'monitor' else 'Postmarket'}</div>
            <div class="muted">{'Choose a working mode to bias quick links toward preparation, live monitoring, or review.' if lang == 'en' else '选择一个工作模式，让快捷入口更偏向盘前准备、盘中观察或盘后复盘。'}</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;">{mode_switch}</div>
          </div>
          <div class="card" style="margin-bottom:16px;">
            <div class="eyebrow">{_dt(lang, 'stock_insight_search')}</div>
            <form action="/insights/open" method="get" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="text" name="ticker" placeholder="{_dt(lang, 'search_placeholder')}" style="min-width:260px;" />
              <button type="submit">{_dt(lang, 'open_insight_page')}</button>
              <span class="muted">{_dt(lang, 'search_help')}</span>
            </form>
          </div>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'auto_analysis')}</div>
              <div class="metric">{_dt(lang, 'on') if auto_analysis['enabled'] else _dt(lang, 'off')}</div>
              <div class="muted">{_dt(lang, 'every_hours', hours=auto_analysis['interval_hours'])}</div>
              <div class="muted">{_dt(lang, 'next_run')}: {auto_analysis['next_run_at'] or '-'}</div>
              <div class="switch-row">
                <span class="switch-pill {'on' if auto_analysis['enabled'] else 'off'}">
                  {_dt(lang, 'enabled') if auto_analysis['enabled'] else _dt(lang, 'disabled')}
                </span>
                <form action="/jobs/auto-analysis/config" method="post" style="margin:0;">
                  <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                  <input type="hidden" name="enabled" value="{'false' if auto_analysis['enabled'] else 'true'}" />
                  <input type="hidden" name="interval_hours" value="{auto_analysis['interval_hours']}" />
                  <input type="hidden" name="provider" value="{auto_analysis['provider']}" />
                  <input type="hidden" name="start_date" value="{auto_analysis['start_date']}" />
                  <input type="hidden" name="signal_type" value="{auto_analysis['signal_type']}" />
                  <input type="hidden" name="lookback_days" value="{auto_analysis['lookback_days']}" />
                  <input type="hidden" name="top_n" value="{auto_analysis['top_n']}" />
                  <input type="hidden" name="sync_cn_concepts" value="{'true' if auto_analysis.get('sync_cn_concepts') else 'false'}" />
                  <button type="submit">{_dt(lang, 'turn_off') if auto_analysis['enabled'] else _dt(lang, 'turn_on')}</button>
                </form>
              </div>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'data_source')}</div>
              <div class="metric">{data_sources['primary_provider'] or 'None'}</div>
              <div class="muted">{_dt(lang, 'current_dominant_provider')}</div>
              <div class="muted">{_dt(lang, 'concept_data_note', freshness=data_sources['concept_data']['freshness'], as_of=data_sources['concept_data']['latest_as_of_date'] or '-')}</div>
              <div class="muted"><a href="/dashboard/data-sources?lang={lang}">{_dt(lang, 'open_detailed_source_page')}</a></div>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'latest_model')}</div>
              <div class="metric" title="{latest_model['name'] if latest_model else 'None'}">{_compact_run_name((latest_model or {}).get('name'), 24) if latest_model else 'None'}</div>
              <div class="muted">{_dt(lang, 'status')}: {latest_model['status'] if latest_model else '-'}</div>
              <div class="muted">{_dt(lang, 'type')}: {latest_model['model_type'] if latest_model else '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'backtest')}</div>
              <div class="metric">{latest_backtest['status'] if latest_backtest else 'None'}</div>
              <div class="muted" title="{latest_backtest['name'] if latest_backtest else '-'}">{_dt(lang, 'run')}: {_compact_run_name((latest_backtest or {}).get('name'), 24) if latest_backtest else '-'}</div>
              <div class="muted">{_dt(lang, 'period')}: {latest_backtest['start_date'] if latest_backtest else '-'} to {latest_backtest['end_date'] if latest_backtest else '-'}</div>
            </article>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">{_dt(lang, 'snapshot_window')}</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;">{lookback_pills}</div>
            <div class="muted">{_dt(lang, 'snapshot_help', runs=lookback_runs)}</div>
          </section>

          <div id="dashboard-top-panels">
            <section class="card" style="margin-bottom:16px;">
              <div class="eyebrow">{_dt(lang, 'risk_overview')}</div>
              <div class="muted">{'加载中…' if lang == 'zh' else 'Loading...'}</div>
            </section>
            <section class="card" style="margin-bottom:16px;">
              <div class="eyebrow">{_dt(lang, 'latest_signals')}</div>
              <div class="muted">{'加载中…' if lang == 'zh' else 'Loading...'}</div>
            </section>
          </div>

          <section class="nav-grid">
            <a class="nav-card" href="/dashboard/market?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}">
              <div class="nav-head">
                <span class="nav-icon">MKT</span>
                <div>
                  <div class="nav-kicker">{'市场视角' if lang == 'zh' else 'Market View'}</div>
                  <div class="nav-title">{'市场脉冲' if lang == 'zh' else 'Market Pulse'}</div>
                </div>
              </div>
              <div class="muted">{'查看板块热力图、概念共振和概念异动追踪。' if lang == 'zh' else 'See sector heatmaps, concept resonance, and concept activity in one place.'}</div>
            </a>
            <a class="nav-card" href="/dashboard/continuous-leaders?lang={lang}&lookback_runs={lookback_runs}">
              <div class="nav-head">
                <span class="nav-icon">RUN</span>
                <div>
                  <div class="nav-kicker">{'持续强势' if lang == 'zh' else 'Persistence'}</div>
                  <div class="nav-title">{_dt(lang, 'continuous_leaders')}</div>
                </div>
              </div>
              <div class="muted">{'查看最近几次模型快照里持续入选的股票。' if lang == 'zh' else 'Track stocks that keep showing up across recent model snapshots.'}</div>
            </a>
            <a class="nav-card" href="/screeners/market-snapshot?lang={lang}&mode={session_mode}">
              <div class="nav-head">
                <span class="nav-icon">SNAP</span>
                <div>
                  <div class="nav-kicker">{'盘面快照' if lang == 'zh' else 'Snapshot'}</div>
                  <div class="nav-title">{'市场快照榜单' if lang == 'zh' else 'Market Snapshot'}</div>
                </div>
              </div>
              <div class="muted">{'按当前模式直接打开强势、收口、连阳、放量榜单。' if lang == 'zh' else 'Open the market snapshot boards using the current session mode.'}</div>
            </a>
            <a class="nav-card" href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}">
              <div class="nav-head">
                <span class="nav-icon">OPS</span>
                <div>
                  <div class="nav-kicker">{'执行与任务' if lang == 'zh' else 'Execution'}</div>
                  <div class="nav-title">{'运维操作台' if lang == 'zh' else 'Operations'}</div>
                </div>
              </div>
              <div class="muted">{'集中处理同步、训练、回测和任务记录。' if lang == 'zh' else 'Handle sync, training, backtests, and recent jobs in one place.'}</div>
            </a>
            <a class="nav-card" href="/dashboard/data-sources?lang={lang}">
              <div class="nav-head">
                <span class="nav-icon">DATA</span>
                <div>
                  <div class="nav-kicker">{'来源与新鲜度' if lang == 'zh' else 'Freshness'}</div>
                  <div class="nav-title">{_dt(lang, 'data_sources')}</div>
                </div>
              </div>
              <div class="muted">{'检查数据来源、概念 freshness 和逐股同步状态。' if lang == 'zh' else 'Inspect providers, concept freshness, and per-symbol sync status.'}</div>
            </a>
            <a class="nav-card" href="/portfolio">
              <div class="nav-head">
                <span class="nav-icon">BOOK</span>
                <div>
                  <div class="nav-kicker">{'仓位与执行' if lang == 'zh' else 'Positions'}</div>
                  <div class="nav-title">{'持仓账本' if lang == 'zh' else 'Portfolio Book'}</div>
                </div>
              </div>
              <div class="muted">{'管理持仓成本、市值、盈亏和 AI 操作建议。' if lang == 'zh' else 'Track cost basis, market value, PnL, and AI trade posture for live holdings.'}</div>
            </a>
            <a class="nav-card" href="/settings/notifications">
              <div class="nav-head">
                <span class="nav-icon">PUSH</span>
                <div>
                  <div class="nav-kicker">{'推送与通知' if lang == 'zh' else 'Notifications'}</div>
                  <div class="nav-title">{'通知配置' if lang == 'zh' else 'Push Settings'}</div>
                </div>
              </div>
              <div class="muted">{'检查企业微信 / 飞书 webhook 是否配置成功。' if lang == 'zh' else 'Check whether WeChat or Feishu webhook delivery is configured.'}</div>
            </a>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">{'今日投研流程' if lang == 'zh' else 'Today Workflow'}</div>
            <div class="muted">{'按这条固定路径看盘、选股、复核和执行，可以把页面切换成本降下来。' if lang == 'zh' else 'Use this fixed path to move from discovery to review and execution with less context switching.'}</div>
            <div style="display:grid;gap:12px;grid-template-columns:repeat(auto-fit, minmax(180px, 1fr));margin-top:14px;">
              <a class="nav-card" href="/screeners/market-snapshot?lang={lang}&mode={session_mode}">
                <div class="nav-kicker">1</div>
                <div class="nav-title">{'看市场快照' if lang == 'zh' else 'Scan Snapshot'}</div>
                <div class="muted">{'先看强势、收口、连阳、放量榜。' if lang == 'zh' else 'Start with leaders, squeezes, candles, and volume boards.'}</div>
              </a>
              <a class="nav-card" href="/watchlist?lang={lang}&mode={session_mode}">
                <div class="nav-kicker">2</div>
                <div class="nav-title">{'看自选与 AI Brief' if lang == 'zh' else 'Review Watchlist'}</div>
                <div class="muted">{'把候选股沉淀到自选，先看批量 AI 摘要。' if lang == 'zh' else 'Move names into the watchlist and review batch AI briefs.'}</div>
              </a>
              <a class="nav-card" href="/dashboard/ai-daily-report">
                <div class="nav-kicker">3</div>
                <div class="nav-title">{'看 AI 日报' if lang == 'zh' else 'Read AI Daily'}</div>
                <div class="muted">{'确认今天的市场策略主线和优先级。' if lang == 'zh' else 'Confirm the market playbook and today’s priorities.'}</div>
              </a>
              <a class="nav-card" href="/portfolio">
                <div class="nav-kicker">4</div>
                <div class="nav-title">{'检查持仓' if lang == 'zh' else 'Check Portfolio'}</div>
                <div class="muted">{'结合盈亏、成本和 AI 策略复核持仓。' if lang == 'zh' else 'Review live positions with PnL, cost basis, and AI posture.'}</div>
              </a>
              <a class="nav-card" href="/settings/notifications">
                <div class="nav-kicker">5</div>
                <div class="nav-title">{'检查推送' if lang == 'zh' else 'Verify Push'}</div>
                <div class="muted">{'确认 webhook 正常，再发出 AI 日报。' if lang == 'zh' else 'Verify webhook delivery before sending the AI daily report.'}</div>
              </a>
            </div>
          </section>

          <section class="mini-grid" id="dashboard-home-panels">
            <article class="card">
              <div class="eyebrow">{'今日行动板' if lang == 'zh' else 'Today Action Board'}</div>
              <div class="muted">{'加载中…' if lang == 'zh' else 'Loading...'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{'市场叙事' if lang == 'zh' else 'Market Narrative'}</div>
              <div class="muted">{'加载中…' if lang == 'zh' else 'Loading...'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">AI Daily Report</div>
              <div class="muted">{'加载中…' if lang == 'zh' else 'Loading...'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'continuous_leaders')}</div>
              <div class="muted">{'加载中…' if lang == 'zh' else 'Loading...'}</div>
            </article>
          </section>

          <section class="mini-grid">
            <article class="card">
              <div class="eyebrow">{'下一步' if lang == 'zh' else 'What To Open Next'}</div>
              <div class="stack">
                <a class="action-link" href="/dashboard/market?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}">{'打开市场脉冲页' if lang == 'zh' else 'Open Market Pulse'}</a>
                <a class="action-link" href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}">{'打开运维操作台' if lang == 'zh' else 'Open Operations'}</a>
                <a class="action-link" href="/dashboard/continuous-leaders?lang={lang}&lookback_runs={lookback_runs}">{'打开连续强势股' if lang == 'zh' else 'Open Continuous Leaders'}</a>
                <a class="action-link" href="/watchlist?lang={lang}&mode={session_mode}">{_dt(lang, 'open_watchlist')}</a>
                <a class="action-link" href="/screeners/market-snapshot?lang={lang}&mode={session_mode}">{'打开市场快照榜单' if lang == 'zh' else 'Open Market Snapshot'}</a>
                <a class="action-link" href="/dashboard/ai-daily-report">{'打开 AI 每日决策面板' if lang == 'zh' else 'Open AI Daily Dashboard'}</a>
                <a class="action-link" href="/portfolio">{'打开持仓账本' if lang == 'zh' else 'Open Portfolio Book'}</a>
                <a class="action-link" href="/screeners?lang={lang}">{_dt(lang, 'open_screener')}</a>
              </div>
            </article>
          </section>
        </main>
      </body>
    </html>
    """


@router.get("/home-panels-fragment", response_class=HTMLResponse)
def dashboard_home_panels_fragment(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    mode: str = "monitor",
    continuous_sort_by: str = "hits",
    continuous_sort_order: str = "desc",
    continuous_market: str = "ALL",
    continuous_state: str = "ALL",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return HTMLResponse("", status_code=401)
    lang = "zh" if lang == "zh" else "en"
    session_mode = str(mode or "monitor").lower()
    if session_mode not in {"premarket", "monitor", "postmarket"}:
        session_mode = "monitor"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    summary = _load_summary(db, lookback_runs=lookback_runs)
    return _render_dashboard_home_panels_fragment(
        db=db,
        lang=lang,
        lookback_runs=lookback_runs,
        session_mode=session_mode,
        latest_signals=summary["latest_signals"],
        recent_jobs=summary["recent_jobs"],
        market_context=summary["market_context"],
        continuous_sort_by=str(continuous_sort_by or "hits"),
        continuous_sort_order=str(continuous_sort_order or "desc"),
        continuous_market=str(continuous_market or "ALL").upper(),
        continuous_state=str(continuous_state or "ALL").upper(),
    )


@router.get("/top-fragment", response_class=HTMLResponse)
def dashboard_top_fragment(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return HTMLResponse("", status_code=401)
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    summary = _load_summary(db, lookback_runs=lookback_runs)
    return _render_dashboard_top_fragment(
        lang=lang,
        latest_signals=summary["latest_signals"],
        latest_model=summary["latest_model"],
        risk_overview=summary["market_context"].get("risk_overview", {}),
    )


@router.get("/ai-daily-report", response_class=HTMLResponse)
def dashboard_ai_daily_report(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ai-daily-report")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    report = _load_cached_ai_daily_report(db) or {
        "mood": "-",
        "headline": "暂无可用的 A股 AI 日报，请先运行收盘复盘或手动生成。",
        "strategy": {"headline": "-", "playbook": "-", "bullets": []},
        "portfolio_summary": {},
        "portfolio_rows": [],
        "social_signal_summary": {"accounts": [], "actionable": []},
        "us_hotspot_validation": [],
        "market_recommendations": [],
        "rows": [],
        "buy_the_dip_rows": [],
    }
    if not report.get("social_signal_summary"):
        current_social_summary = social_signal_summary(db)
        report = {
            **report,
            "social_signal_summary": {
                "accounts": current_social_summary.get("accounts") or [],
                "actionable": current_social_summary.get("actionable") or [],
            },
        }

    portfolio_summary = report.get("portfolio_summary") or {}
    portfolio_rows_html = "".join(
        "<tr>"
        f"<td>{item.get('ticker')}</td>"
        f"<td>{item.get('name') or item.get('ticker')}</td>"
        f"<td>{item.get('quantity') or '-'}</td>"
        f"<td>{item.get('cost_basis') or '-'}</td>"
        f"<td>{item.get('latest_price') or '-'}</td>"
        f"<td>{float(item.get('pnl') or 0.0):.2f}<div class='muted'>{float(item.get('pnl_pct') or 0.0):.2f}%</div></td>"
        f"<td>{item.get('ai_verdict') or '-'}<div class='muted'>{item.get('ai_headline') or '-'}</div></td>"
        f"<td>{item.get('action_bucket') or '-'}<div class='muted'>目标: {item.get('target_weight_text') or '-'}</div></td>"
        f"<td>{item.get('ai_strategy') or '-'}<div class='muted'>触发: {item.get('entry_trigger') or '-'}</div><div class='muted'>失效: {item.get('invalidation_condition') or '-'}</div></td>"
        "</tr>"
        for item in (report.get("portfolio_rows") or [])
    ) or f"<tr><td colspan='9'>{'暂无持仓库数据' if lang == 'zh' else 'No portfolio holdings yet.'}</td></tr>"
    market_recommendation_rows = report.get("market_recommendations") or report.get("rows") or []
    rows_html = "".join(
        "<tr>"
        f"<td>{item.get('ticker')}</td>"
        f"<td>{item.get('name') or item.get('ticker')}</td>"
        f"<td>{item.get('verdict') or '-'}</td>"
        f"<td>{item.get('confidence') or '-'}</td>"
        f"<td>{item.get('quant_rank') or '-'}<div class='muted'>验证分: {item.get('verification_score') or '-'}</div></td>"
        f"<td>{item.get('strategy') or '-'}<div class='muted'>仓位: {item.get('target_weight') or '-'}</div><div class='muted'>可交易性: {item.get('tradability_status') or '-'}</div></td>"
        f"<td>{item.get('entry_trigger') or '-'}<div class='muted'>失效: {item.get('invalidation_condition') or '-'}</div></td>"
        f"<td>{item.get('time_horizon') or '-'}<div class='muted'>滑点: {item.get('max_slippage_bps') or '-'}bps · 流动性: {item.get('liquidity_bucket') or '-'}</div></td>"
        f"<td>{item.get('verification_note') or '-'}<div class='muted'>止损: {item.get('stop_loss', '-')} · {item.get('stop_loss_type') or '-'}</div></td>"
        f"<td>{item.get('headline') or '-'}<div class='muted'>{item.get('summary') or '-'}</div></td>"
        "</tr>"
        for item in market_recommendation_rows[:5]
    ) or f"<tr><td colspan='10'>{'暂无全市场推荐候选' if lang == 'zh' else 'No full-market recommendations yet.'}</td></tr>"
    social_payload = report.get("social_signal_summary") or {}
    social_signal_rows = social_payload.get("actionable") or []
    social_accounts = social_payload.get("accounts") or []
    social_signal_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('handle') or '-'))}</td>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('social_view') or '-'))}</td>"
        f"<td>{int(item.get('validation_score') or 0)}</td>"
        f"<td>{html.escape(str(item.get('model_signal_label') or '-'))}<div class='muted'>score {html.escape(str(item.get('model_score') if item.get('model_score') is not None else '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('system_action') or '-'))}</td>"
        f"<td>{html.escape(' / '.join(item.get('validation_reasons') or []) or '-')}</td>"
        "</tr>"
        for item in social_signal_rows[:8]
    ) or f"<tr><td colspan='7'>{'暂无可验证社交信号。请先在社交信号页导入已追踪账号的 X 帖子。' if lang == 'zh' else 'No validated social signals yet. Import X posts from tracked accounts first.'}</td></tr>"
    social_account_text = ", ".join(str(item.get("handle") or "") for item in social_accounts) or "-"
    us_hotspot_rows = report.get("us_hotspot_validation") or []
    us_hotspot_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('handle') or '-'))}</td>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('social_view') or '-'))}<div class='muted'>社交分 {int(item.get('validation_score') or 0)}</div></td>"
        f"<td>{html.escape(str(item.get('template') or '-'))}<div class='muted'>Top #{int(item.get('us_rank') or 0)}</div></td>"
        f"<td>{html.escape(str(item.get('action_label') or '-'))}<div class='muted'>趋势 {html.escape(str(item.get('trend_score') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('cross_validation_note') or '-'))}</td>"
        "</tr>"
        for item in us_hotspot_rows[:8]
    ) or f"<tr><td colspan='6'>{'暂无 X 热点美股与美股模型 Top 候选重合。请先导入 X 帖子，并运行美股预计算 job。' if lang == 'zh' else 'No overlap between X U.S. mentions and U.S. model top candidates yet. Import X posts and run U.S. precompute first.'}</td></tr>"

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'A股 AI 每日决策面板' if lang == 'zh' else 'AI Daily Dashboard'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:1180px; margin:0 auto; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:700; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .metric {{ font-size:32px; font-weight:800; margin:4px 0 8px; }}
          .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
          .hero-grid {{ display:grid; grid-template-columns:minmax(0,1.15fr) minmax(320px,0.85fr); gap:16px; margin-bottom:16px; }}
          .playbook {{ margin-top:12px; padding:14px; border-radius:18px; background:rgba(21,34,49,0.82); border:1px solid var(--line); }}
          .action-row {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
          .cta {{ display:inline-flex; align-items:center; justify-content:center; padding:10px 14px; border-radius:999px; border:1px solid var(--line); background:rgba(21,34,49,0.92); color:var(--ink); font-size:13px; font-weight:800; }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }}
          table {{ width:100%; min-width:1120px; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          button {{ border-radius:999px; border:1px solid transparent; padding:10px 14px; font:inherit; font-weight:800; background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; cursor:pointer; }}
          @media (max-width: 960px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} .hero-grid {{ grid-template-columns:1fr; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'AI 日报' if lang == 'zh' else 'AI Report'}</h1>
              <p>{'把 AI 每日复盘、候选动作和推送文本集中在一个稳定入口。' if lang == 'zh' else 'Keep the AI daily review, candidate actions, and push-ready text in one stable workspace.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to Dashboard'}</a>
                <a class="pill" href="/dashboard/ops?lang={lang}">{'打开任务中心' if lang == 'zh' else 'Open Task Center'}</a>
                <a class="pill" href="/dashboard/ai-daily-report/message?lang={lang}">{'打开推送文本' if lang == 'zh' else 'Open Push Text'}</a>
                <a class="pill" href="/dashboard/ai-daily-report/history?lang={lang}">{'历史记录' if lang == 'zh' else 'History'}</a>
              </div>
              <section class="hero-grid">
                <article class="card">
                  <div class="eyebrow">{'AI 每日复盘' if lang == 'zh' else 'AI Daily Review'}</div>
                  <div class="metric">{report.get('mood') or '-'}</div>
                  <div class="muted">{report.get('headline') or '-'}</div>
                  <div class="playbook">
                    <div style="font-weight:800;margin-bottom:6px;">{(report.get('strategy') or {}).get('headline') or '-'}</div>
                    <div class="muted">{(report.get('strategy') or {}).get('playbook') or '-'}</div>
                    <div style="margin-top:8px;">
                      {"".join(f"<div class='muted'>• {item}</div>" for item in ((report.get('strategy') or {}).get('bullets') or [])) or "<div class='muted'>-</div>"}
                    </div>
                  </div>
                  <div class="action-row">
                    <a class="cta" href="/dashboard/ai-daily-report/message?lang={lang}">{'打开 A股推送文本' if lang == 'zh' else 'Open push-ready text'}</a>
                    <form action="/jobs/send-ai-daily-report" method="post" style="display:inline;">
                      <input type="hidden" name="redirect_to" value="/dashboard/ai-daily-report?lang={lang}" />
                      <button type="submit">{'发送 A股日报到已配置渠道' if lang == 'zh' else 'Send report to configured channels'}</button>
                    </form>
                  </div>
                </article>
                <article class="card">
                  <div class="eyebrow">{'使用方式' if lang == 'zh' else 'How to use'}</div>
                  <div class="muted">{'日报现在分四段：持仓复核、A股全市场 Top 5、X 社交信号验证，以及 X 热点美股和美股模型候选交叉验证。' if lang == 'zh' else 'The report now has four parts: portfolio review, A-share full-market Top 5, X social validation, and X U.S. hotspot cross-validation.'}</div>
                  <div class="playbook">
                    <div style="font-weight:800;margin-bottom:6px;">{'持仓摘要' if lang == 'zh' else 'Portfolio Summary'}</div>
                    <div class="muted">{portfolio_summary.get('headline') or '-'}</div>
                    <div class="muted" style="margin-top:8px;">{portfolio_summary.get('action_note') or '-'}</div>
                    <div class="muted" style="margin-top:8px;">{'社交账号' if lang == 'zh' else 'Social accounts'}: {html.escape(social_account_text)}</div>
                  </div>
                </article>
              </section>
              <section class="card">
                <div class="eyebrow">{'一、持仓库总结' if lang == 'zh' else '1. Portfolio Review'}</div>
                <div class="muted">{portfolio_summary.get('headline') or '-'}</div>
                <div class="table-wrap"><table>
              <thead>
                <tr><th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>最新价</th><th>盈亏</th><th>AI 判断</th><th>动作桶</th><th>Note</th></tr>
              </thead>
              <tbody>{portfolio_rows_html}</tbody></table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'二、全市场扫描 Top 5' if lang == 'zh' else '2. Full-Market Top 5'}</div>
                <div class="muted">{'基于收盘后行情、模型信号、趋势结构、可交易性和可验证触发条件筛选。' if lang == 'zh' else 'Selected from post-close market data, model signal, trend structure, tradability, and verifiable triggers.'}</div>
                <div class="table-wrap"><table>
              <thead>
                <tr><th>代码</th><th>名称</th><th>结论</th><th>置信度</th><th>量化 / 验证</th><th>策略 / 仓位</th><th>触发 / 失效</th><th>周期 / 流动性</th><th>验证 / 止损</th><th>Headline / Summary</th></tr>
              </thead>
              <tbody>{rows_html}</tbody></table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'三、X 账户社交信号验证' if lang == 'zh' else '3. X Account Signal Validation'}</div>
                <div class="muted">{'这里不是直接照单买入，而是把社交观点和模型信号、触发条件、自选/持仓状态做交叉验证。' if lang == 'zh' else 'This does not copy trades directly; it cross-validates social views against model signals, triggers, watchlist, and portfolio state.'}</div>
                <div class="table-wrap"><table>
              <thead>
                <tr><th>账号</th><th>股票</th><th>观点</th><th>验证分</th><th>模型</th><th>系统动作</th><th>原因</th></tr>
              </thead>
              <tbody>{social_signal_rows_html}</tbody></table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'四、X 热点美股验证' if lang == 'zh' else '4. X U.S. Hotspot Validation'}</div>
                <div class="muted">{'把 X 帖子里提到的美股，与后台预计算的美股模型候选做交叉验证。没有重合时不强行推荐。' if lang == 'zh' else 'Cross-check U.S. tickers mentioned on X against precomputed U.S. model candidates. No overlap means no forced recommendation.'}</div>
                <div class="table-wrap"><table>
              <thead>
                <tr><th>账号</th><th>股票</th><th>X观点</th><th>美股模型</th><th>模型动作</th><th>验证结论</th></tr>
              </thead>
              <tbody>{us_hotspot_rows_html}</tbody></table></div>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/ai-daily-report/history", response_class=HTMLResponse)
def dashboard_ai_daily_report_history(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ai-daily-report/history")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    history = list_ai_daily_report_history(limit=60, db=db)
    rows_html = ""
    for item in history:
        payload = item.get("payload") or {}
        top5 = payload.get("market_recommendations") or payload.get("rows") or []
        top5_text = ", ".join(
            str(row.get("ticker") or "")
            for row in top5[:5]
            if row.get("ticker")
        ) or "-"
        portfolio_rows = payload.get("portfolio_rows") or []
        rows_html += (
            "<tr>"
            f"<td><a href='/dashboard/ai-daily-report/history/{int(item.get('id'))}?lang={lang}'>{html.escape(str(item.get('snapshot_date') or '-'))}</a>"
            f"<div class='muted'>#{int(item.get('id'))} · {_display_time(item.get('created_at'), with_tz=True)}</div></td>"
            f"<td>{html.escape(str(payload.get('mood') or '-'))}<div class='muted'>{html.escape(str(payload.get('headline') or '-'))}</div></td>"
            f"<td>{len(portfolio_rows)}</td>"
            f"<td>{html.escape(top5_text)}</td>"
            f"<td><a class='cta' href='/dashboard/ai-daily-report/history/{int(item.get('id'))}?lang={lang}'>{'打开' if lang == 'zh' else 'Open'}</a></td>"
            "</tr>"
        )
    if not rows_html:
        rows_html = (
            f"<tr><td colspan='5'>{'暂无历史日报。下一次生成或发送 AI 日报后会自动保存。' if lang == 'zh' else 'No historical reports yet. The next generated or sent AI report will be archived automatically.'}</td></tr>"
        )

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'AI 日报历史记录' if lang == 'zh' else 'AI Report History'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:1080px; margin:0 auto; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .pill,.cta {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          h2 {{ margin:0 0 8px; font-size:28px; }}
          .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }}
          table {{ width:100%; min-width:880px; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:700; }}
          @media (max-width: 960px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'日报历史' if lang == 'zh' else 'Report History'}</h1>
              <p>{'保留每次 AI 日报，方便后续对照推荐是否走出来。' if lang == 'zh' else 'Archive every AI report so later outcomes can be reviewed.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/dashboard/ai-daily-report?lang={lang}">← {'返回 AI 日报' if lang == 'zh' else 'Back to AI Report'}</a>
                <a class="pill" href="/dashboard/ops?lang={lang}">{'任务中心' if lang == 'zh' else 'Ops'}</a>
              </div>
              <section class="card">
                <div class="eyebrow">{'历史日报' if lang == 'zh' else 'Historical Reports'}</div>
                <h2>{'日报留档' if lang == 'zh' else 'Report Archive'}</h2>
                <div class="muted">{'每次生成或发送 AI 日报都会新增一条记录，不覆盖旧版本。后面我们可以在这里继续加“命中率/收益验证”。' if lang == 'zh' else 'Every generated or sent AI report is archived without overwriting older versions. Outcome tracking can be added here next.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'日期' if lang == 'zh' else 'Date'}</th><th>{'市场判断' if lang == 'zh' else 'Market View'}</th><th>{'持仓数' if lang == 'zh' else 'Holdings'}</th><th>{'Top 5' if lang == 'zh' else 'Top 5'}</th><th>{'操作' if lang == 'zh' else 'Action'}</th></tr></thead>
                  <tbody>{rows_html}</tbody>
                </table></div>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/ai-daily-report/history/{snapshot_id}", response_class=HTMLResponse)
def dashboard_ai_daily_report_history_detail(snapshot_id: int, request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect(f"/dashboard/ai-daily-report/history/{snapshot_id}")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    snapshot = load_ai_daily_report_history_item(snapshot_id, db=db)
    if snapshot is None:
        return HTMLResponse("Not found", status_code=404)
    report = snapshot.get("payload") or {}
    message = render_ai_daily_report_message(report)
    report_date = str(snapshot.get("snapshot_date") or report.get("report_date") or "")
    outcome_rows = _report_outcome_rows(report, report_date=report_date)
    outcome_summary = _report_outcome_summary(outcome_rows, lang=lang)
    outcome_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}<div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('baseline_date') or '-'))}<div class='muted'>{_fmt_optional_float(item.get('baseline_close'), digits=3)}</div></td>"
        f"<td>{html.escape(str(item.get('latest_date') or '-'))}<div class='muted'>{_fmt_optional_float(item.get('latest_close'), digits=3)}</div></td>"
        f"<td>{_fmt_optional_float(item.get('return_pct'), suffix='%', digits=2)}</td>"
        f"<td>{_outcome_status_label(item.get('status'), lang=lang)}</td>"
        "</tr>"
        for item in outcome_rows
    ) or f"<tr><td colspan='5'>{'暂无可验证记录。' if lang == 'zh' else 'No measurable records yet.'}</td></tr>"
    top5 = report.get("market_recommendations") or report.get("rows") or []
    top5_rows = "".join(
        "<tr>"
        f"<td>{index}</td>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('verdict') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('quant_rank') or '-'))}<div class='muted'>验证分 {html.escape(str(item.get('verification_score') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('entry_trigger') or '-'))}<div class='muted'>失效: {html.escape(str(item.get('invalidation_condition') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('headline') or item.get('summary') or '-'))}</td>"
        "</tr>"
        for index, item in enumerate(top5[:5], start=1)
    ) or f"<tr><td colspan='6'>{'该历史日报没有 Top 5 记录。' if lang == 'zh' else 'This archived report has no Top 5 records.'}</td></tr>"

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'AI 日报历史详情' if lang == 'zh' else 'AI Report Detail'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:1080px; margin:0 auto; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          h2 {{ margin:0 0 8px; font-size:28px; }}
          .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }}
          table {{ width:100%; min-width:980px; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:700; }}
          textarea {{ width:100%; min-height:360px; border:1px solid var(--line); border-radius:16px; padding:14px; font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; background:rgba(21,34,49,0.72); color:var(--ink); }}
          @media (max-width: 960px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'历史详情' if lang == 'zh' else 'History Detail'}</h1>
              <p>{'回看当日报告原文和 Top 5，后续用于验证准确性。' if lang == 'zh' else 'Review the archived report and Top 5 for later accuracy checks.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/dashboard/ai-daily-report/history?lang={lang}">← {'返回历史记录' if lang == 'zh' else 'Back to History'}</a>
                <a class="pill" href="/dashboard/ai-daily-report?lang={lang}">{'当前日报' if lang == 'zh' else 'Current Report'}</a>
              </div>
              <section class="card">
                <div class="eyebrow">{'历史日报' if lang == 'zh' else 'Archived Report'}</div>
                <h2>{html.escape(str(snapshot.get('snapshot_date') or '-'))}</h2>
                <div class="muted">{'保存时间' if lang == 'zh' else 'Saved at'}: {_display_time(snapshot.get('created_at'), with_tz=True)}</div>
                <div class="muted">{html.escape(str(report.get('headline') or '-'))}</div>
              </section>
              <section class="card">
                <div class="eyebrow">{'事后表现验证' if lang == 'zh' else 'Outcome Check'}</div>
                <div class="muted">{html.escape(outcome_summary)}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'基准收盘' if lang == 'zh' else 'Baseline Close'}</th><th>{'最新收盘' if lang == 'zh' else 'Latest Close'}</th><th>{'区间收益' if lang == 'zh' else 'Return'}</th><th>{'状态' if lang == 'zh' else 'Status'}</th></tr></thead>
                  <tbody>{outcome_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'当日推荐 Top 5' if lang == 'zh' else 'Daily Top 5'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>#</th><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'结论' if lang == 'zh' else 'Verdict'}</th><th>{'量化/验证' if lang == 'zh' else 'Quant/Validation'}</th><th>{'触发/失效' if lang == 'zh' else 'Trigger/Invalidation'}</th><th>Headline</th></tr></thead>
                  <tbody>{top5_rows}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'日报原文' if lang == 'zh' else 'Original Message'}</div>
                <textarea readonly>{html.escape(message)}</textarea>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/ai-daily-report/message", response_class=HTMLResponse)
def dashboard_ai_daily_report_message(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ai-daily-report/message")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    report = _load_cached_ai_daily_report(db) or {
        "mood": "-",
        "headline": "暂无可用的 A股 AI 日报，请先运行收盘复盘或手动生成。",
        "strategy": {"headline": "-", "playbook": "-", "bullets": []},
        "rows": [],
        "buy_the_dip_rows": [],
    }
    if not report.get("social_signal_summary"):
        current_social_summary = social_signal_summary(db)
        report = {
            **report,
            "social_signal_summary": {
                "accounts": current_social_summary.get("accounts") or [],
                "actionable": current_social_summary.get("actionable") or [],
            },
        }
    message = render_ai_daily_report_message(report)
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'A股 AI 日报推送文本' if lang == 'zh' else 'AI Report Push Text'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:28px 30px 48px; }}
          .wrap {{ max-width:980px; margin:0 auto; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:700; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
          textarea {{ width:100%; min-height:420px; border:1px solid var(--line); border-radius:16px; padding:14px; font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; background:rgba(21,34,49,0.72); color:var(--ink); }}
          @media (max-width: 960px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'推送文本' if lang == 'zh' else 'Push Text'}</h1>
              <p>{'把 AI 日报整理成可直接发送的文本。' if lang == 'zh' else 'Prepare the AI report as a push-ready message.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/dashboard/ai-daily-report?lang={lang}">← {'返回 AI 日报' if lang == 'zh' else 'Back to AI report'}</a>
              </div>
              <section class="card">
                <div class="eyebrow">A-Share Push Ready</div>
                <div class="muted">{'下面这段文本默认只包含 A 股，可直接复制到 Telegram、企业微信、飞书或邮件。' if lang == 'zh' else 'The text below is ready to copy into Telegram, WeCom, Feishu, or email.'}</div>
                <textarea readonly>{message}</textarea>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """
