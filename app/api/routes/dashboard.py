import csv
import json
from io import StringIO
from urllib.parse import urlencode
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db_session
from app.models.schema import SymbolCreate
from app.services.ai_daily_report import build_ai_daily_report, load_ai_daily_report, render_ai_daily_report_message, save_ai_daily_report
from app.services.auth import is_authenticated, login_redirect
from app.services.close_review_scheduler import close_review_scheduler_service
from app.services.focus_pool import enrich_focus_pool_with_symbols, load_today_focus_pool
from app.services.dashboard_summary import load_dashboard_summary, load_recent_jobs_summary
from app.services.market_intelligence import build_market_narrative_brief
from app.services.market_news import MarketNewsService
from app.services.market_sync import sync_market_data
from app.services.model_signal_summary import build_model_state, build_signal_label, enrich_model_output, model_confidence
from app.services.repository import (
    ConceptSnapshotRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    SymbolRepository,
    TechnicalSnapshotRepository,
    WatchlistRepository,
)
from app.services.runtime_cache import get_or_set
from app.services.screener import ScreenerService
from app.services.symbol_details import SymbolDataService


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


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
    ai_daily_report = load_ai_daily_report(db=db)
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
            <div class="muted">{' / '.join(item.get('ticker') for item in continuous_rows_source[:3]) or '-'}</div>
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
        f"<div class='signal-date'>{item['trade_date']}</div>"
        f"<div style='margin-bottom:8px;'><span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;background:{build_model_state(item.get('score'), lang=lang)['bg']};color:{build_model_state(item.get('score'), lang=lang)['fg']};font-weight:800;font-size:12px;'>{build_model_state(item.get('score'), lang=lang)['label']}</span></div>"
        f"<div class='signal-score'>{item['score']:.6f}</div>"
        f"<div style='margin-top:6px;'>{_signal_pill(item.get('score'), lang=lang, compact=True)}</div>"
        f"<div class='signal-foot'>{latest_model['name'] if latest_model else ('最新模型' if lang == 'zh' else 'Latest model')}"
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
        f"<div class='muted' style='margin-top:8px;'><strong>{item.get('ticker')}</strong> · {item.get('verdict') or '-'} · {item.get('headline') or '-'}</div>"
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
    return load_dashboard_summary(
        db,
        lookback_runs=lookback_runs,
        market_context_loader=lambda latest_signals: _build_market_context(
            db,
            latest_signals,
            lookback_runs=lookback_runs,
        ),
    )


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
        cache_key = ticker.upper()
        if cache_key in model_meta_cache:
            return model_meta_cache[cache_key]
        detail = signal_repo.get_latest_model_output_for_ticker(ticker)
        if detail is None:
            detail = {"ticker": ticker, "score": score}
        elif detail.get("score") is None and score is not None:
            detail["score"] = score
        enriched = enrich_model_output(detail, lang="en") or {"score": score, "state": build_model_state(score, lang="en")}
        trade_plan = trade_plan_repo.get_latest_for_ticker(ticker) or {}
        model_meta_cache[cache_key] = {
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
        return model_meta_cache[cache_key]

    def _top_execution_tags(ticker_details: list[dict]) -> list[str]:
        counts: dict[str, int] = {}
        for detail in ticker_details:
            for tag in detail.get("execution_tags") or []:
                normalized = str(tag).strip()
                if not normalized:
                    continue
                counts[normalized] = counts.get(normalized, 0) + 1
        return [
            tag
            for tag, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ][:2]

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
        concept_snapshots.append(
            {
                "trade_date": snapshot["trade_date"],
                "counts": counts,
            }
        )

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


def _get_concept_from_summary(summary: dict, concept_slug: str) -> dict | None:
    return next(
        (item for item in summary["market_context"]["concept_tracker"] if item["slug"] == concept_slug),
        None,
    )


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
def dashboard_summary(lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> dict:
    return _load_summary(db, lookback_runs=_clamp_lookback_runs(lookback_runs))


@router.get("/data-sources", response_class=HTMLResponse)
def dashboard_data_sources(request: Request, lang: str = "en", db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard/data-sources")
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
    symbol_rows = "".join(
        f"<tr><td><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td><td>{item['name'] or item['ticker']}</td><td>{item['provider'] or '-'}</td><td>{item['status'] or '-'}</td><td>{item['last_synced_date'] or '-'}</td><td class='message-cell'>{item['message'] or '-'}</td></tr>"
        for item in sync_states
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
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
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
    summary = _load_summary(db, lookback_runs=lookback_runs)
    concept = _get_concept_from_summary(summary, concept_slug)
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
    for detail in concept["ticker_details"]:
        state_label, state_bg, state_fg = _concept_ticker_watch_state(watchlist_map, detail["ticker"], lang)
        existing = watchlist_map.get(detail["ticker"])
        history = symbol_data_service.get_history(detail["ticker"], limit=10)
        five_day_move = None
        if len(history) >= 6:
            start_close = history[-6].get("close")
            end_close = history[-1].get("close")
            if start_close not in (None, 0) and end_close is not None:
                five_day_move = ((float(end_close) / float(start_close)) - 1) * 100
        twenty_day_history = symbol_data_service.get_history(detail["ticker"], limit=20)
        twenty_day_move = None
        if len(twenty_day_history) >= 2:
            start_close = twenty_day_history[0].get("close")
            end_close = twenty_day_history[-1].get("close")
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
            f"<td><div>{detail['score']:.4f}</div><div style='margin-top:6px;'>{_dashboard_model_badge(detail.get('state'), confidence=detail.get('confidence'), compact=True)}</div><div style='margin-top:6px;'>{_signal_pill(detail.get('score'), lang=lang, strength=int(detail.get('signal_strength') or 0), compact=True)}</div><div style='margin-top:6px;font-size:12px;color:#6b7280;'>{('Pct ' + format(float(detail.get('percentile')), '.1f') + '%') if detail.get('percentile') is not None else ''}{(' · ' if detail.get('percentile') is not None and detail.get('target_horizon_days') is not None else '')}{('H ' + str(int(detail.get('target_horizon_days'))) + 'd') if detail.get('target_horizon_days') is not None else ''}{(' · ' if (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None) and detail.get('model_reward_risk_ratio') is not None else '')}{('R/R ' + format(float(detail.get('model_reward_risk_ratio')), '.2f')) if detail.get('model_reward_risk_ratio') is not None else ''}{(' · ' if (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None) and detail.get('conviction_bucket') else '')}{detail.get('conviction_bucket') or ''}{(' · ' if detail.get('position_size_hint') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket')) else '')}{detail.get('position_size_hint') or ''}{(' · ' if detail.get('entry_style') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket') or detail.get('position_size_hint')) else '')}{detail.get('entry_style') or ''}{(' · ' if detail.get('execution_tags') and (detail.get('percentile') is not None or detail.get('target_horizon_days') is not None or detail.get('model_reward_risk_ratio') is not None or detail.get('conviction_bucket') or detail.get('position_size_hint') or detail.get('entry_style')) else '')}{' / '.join((detail.get('execution_tags') or [])[:2])}</div></td>"
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
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{concept['concept_name']}</title>
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
          .wrap {{ max-width: 980px; margin: 0 auto; padding: 28px 20px 56px; }}
          .card {{ background: var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .metric {{ font-size: 30px; font-weight: 800; margin: 8px 0; }}
          .muted {{ color: var(--muted); font-size: 14px; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:16px; }}
          .action-grid {{ display:grid; gap:16px; grid-template-columns: minmax(260px, 1fr) minmax(280px, 1.2fr); margin-bottom:16px; }}
          .mini-grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); }}
          .mini-card {{ background:#f9f7f0; border:1px solid var(--line); border-radius:16px; padding:14px; }}
          .mini-top {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:6px; }}
          .mini-score {{ font-size:12px; font-weight:800; color:#0f766e; background:#dff5ef; padding:4px 8px; border-radius:999px; }}
          .mini-name {{ color:var(--muted); font-size:13px; margin-bottom:10px; min-height:34px; }}
          .mini-metrics {{ display:flex; justify-content:space-between; gap:10px; color:#374151; font-size:12px; font-weight:700; margin-top:8px; }}
          .compare-row {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }}
          .compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#f3f4f6; color:#374151; text-decoration:none; font-size:12px; font-weight:800; }}
          .compare-pill.active {{ background:#dff5ef; color:#0f766e; }}
          table {{ width:100%; border-collapse:collapse; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); }}
          th {{ color: var(--muted); font-weight: 600; }}
          a {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
          .banner {{ margin-bottom:16px; padding:14px 16px; border-radius:16px; background:#dff5ef; color:#0f766e; font-weight:700; }}
          .stack {{ display:grid; gap:12px; }}
          input, button {{ border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; }}
          button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
          .checkline {{ display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }}
          .action-link {{ display:inline-flex; align-items:center; padding:10px 12px; border-radius:12px; background:#eef8f5; color:#0f766e; text-decoration:none; font-weight:700; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          {banner_html}
          <div class="card">
            <a href="/dashboard">← {_concept_tr(lang, 'back_to_dashboard')}</a>
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
              <article class="card" style="margin-bottom:0;background:#f9f7f0;">
                <div class="eyebrow">{_concept_tr(lang, 'five_day')}</div>
                <div class="metric">{avg_move_5d_display}</div>
                <div class="muted">Average 5-day move across tickers inside this concept.</div>
              </article>
              <article class="card" style="margin-bottom:0;background:#f9f7f0;">
                <div class="eyebrow">{_concept_tr(lang, 'twenty_day')}</div>
                <div class="metric">{avg_move_20d_display}</div>
                <div class="muted">Average 20-day move across tickers inside this concept.</div>
              </article>
              <article class="card" style="margin-bottom:0;background:#f9f7f0;">
                <div class="eyebrow">{_concept_tr(lang, 'breadth')}</div>
                <div class="metric">{breadth_display}</div>
                <div class="muted">{_concept_tr(lang, 'breadth_help')}</div>
              </article>
              <article class="card" style="margin-bottom:0;background:#f9f7f0;">
                <div class="eyebrow">{_concept_tr(lang, 'buy_signal_count')}</div>
                <div class="metric">{int(concept.get('buy_signal_count') or 0)}</div>
                <div class="muted">{_concept_tr(lang, 'buy_signal_count_help')}</div>
              </article>
              <article class="card" style="margin-bottom:0;background:#f9f7f0;">
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
            <table>
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
            </table>
          </section>
        </main>
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
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="yfinance")
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
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="yfinance")
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
        results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="yfinance")
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
        results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="yfinance")
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
        results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="yfinance")
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
    summary = _load_summary(db, lookback_runs=lookback_runs)
    market_context = summary["market_context"]
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)

    rows_source = list(market_context.get("continuous_leaders", []))
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
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{_concept_tr(lang, 'continuous_detail')}</title>
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
          .wrap {{ max-width: 1080px; margin: 0 auto; padding: 28px 20px 56px; }}
          .card {{ background: var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .metric {{ font-size: 30px; font-weight: 800; margin: 8px 0; }}
          .muted {{ color: var(--muted); font-size: 14px; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:16px; }}
          .compare-row {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }}
          .compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#f3f4f6; color:#374151; text-decoration:none; font-size:12px; font-weight:800; }}
          .compare-pill.active {{ background:#dff5ef; color:#0f766e; }}
          table {{ width:100%; border-collapse:collapse; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); }}
          th {{ color: var(--muted); font-weight: 600; }}
          a {{ color: var(--accent); text-decoration: none; font-weight: 700; }}
          .stack {{ display:grid; gap:12px; }}
          input, button, select {{ border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; }}
          button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
          .checkbox-row {{ display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }}
          .action-link {{ display:inline-flex; align-items:center; padding:10px 12px; border-radius:12px; background:#eef8f5; color:#0f766e; text-decoration:none; font-weight:700; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div class="card">
            <a href="/dashboard">← {_concept_tr(lang, 'back_to_dashboard')}</a>
            {lang_switch}
            <div class="eyebrow" style="margin-top:12px;">{_concept_tr(lang, 'continuous_leaders')}</div>
            <div class="metric">{_concept_tr(lang, 'continuous_detail')}</div>
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
          </section>
        </main>
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
    summary = _load_summary(db, lookback_runs=lookback_runs)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    rows_source = list(summary["market_context"].get("continuous_leaders", []))
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
    summary = _load_summary(db, lookback_runs=lookback_runs)
    market_context = summary["market_context"]

    heatmap_rows = list(market_context["sector_heatmap"])
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
        for item in market_context["market_distribution"]
    ) or f"<tr><td colspan='2'>{'暂无信号分布' if lang == 'zh' else 'No signal distribution yet'}</td></tr>"
    concept_rows_source = list(market_context["concept_tracker"])
    if signal_filter != "ALL":
        concept_rows_source = [
            item for item in concept_rows_source
            if any(str(detail.get("signal_label") or "").strip().upper() == signal_filter for detail in item.get("ticker_details", []))
        ]
    if min_signal_strength > 0:
        concept_rows_source = [
            item for item in concept_rows_source
            if int(item.get("max_signal_strength") or 0) >= min_signal_strength
        ]
    if min_buy_signal_count > 0:
        concept_rows_source = [
            item for item in concept_rows_source
            if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item for item in concept_rows_source
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        concept_rows_source = [
            item for item in concept_rows_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    concept_rows = "".join(
        "<tr>"
        f"<td id='concept-{item['slug']}'><a href='/dashboard/concepts/{item['slug']}?{urlencode({'lookback_runs': lookback_runs, 'lang': lang, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}'>{item['concept_name']}</a></td>"
        f"<td>{item['hits']}</td><td>{item['previous_hits']}</td><td>{'+' if item['delta_hits'] > 0 else ''}{item['delta_hits']}</td><td>{item['streak']}</td><td>{_sparkline_svg(item['history'])}</td><td>{_percent_chip(item.get('avg_move_5d'))}</td><td>{_breadth_chip(item.get('breadth_pct'))}</td><td>{int(item.get('buy_signal_count') or 0)}</td><td>{int(item.get('max_signal_strength') or 0)}</td><td>{' · '.join(item.get('execution_tags') or []) or '-'}</td><td>{item['avg_score']:.4f}</td><td>{', '.join(item['tickers'])}</td>"
        "</tr>"
        for item in concept_rows_source
    ) or f"<tr><td colspan='13'>{'暂无概念数据' if lang == 'zh' else 'No concept data yet'}</td></tr>"
    lookback_pills = _lookback_pills("/dashboard/market", selected=lookback_runs, extra_params={"lang": lang, "heatmap_sort": heatmap_sort, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter})
    heatmap_sort_pills = "".join(
        f"<a href='/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': mode, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if heatmap_sort == mode else ''}'>{label}</a>"
        for mode, label in (
            ("hits", _dt(lang, "sort_by_hits")),
            ("five_day", _dt(lang, "sort_by_5d")),
            ("breadth", _dt(lang, "sort_by_breadth")),
            ("score", _dt(lang, "sort_by_score")),
        )
    )
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
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'市场脉冲' if lang == 'zh' else 'Market Pulse'}</title>
        <style>
          :root {{ --bg:#f5efe2; --panel:#fffdf7; --ink:#1f2937; --muted:#6b7280; --line:#d6cfc2; --accent:#0f766e; --accent-soft:#dff5ef; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#fff6d8 0,transparent 30%),radial-gradient(circle at top right,#d9f3ee 0,transparent 35%),var(--bg); }}
          .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }}
          .card {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .toolbar,.compare-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .pill,.compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#eef8f5; color:#0f766e; font-size:13px; font-weight:700; text-decoration:none; }}
          .compare-pill.active {{ background:#0f766e; color:#fff; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }}
          .heat-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); margin-top:12px; }}
          .heat-tile {{ color:#fff; border-radius:16px; padding:14px; min-height:110px; display:flex; flex-direction:column; justify-content:space-between; text-decoration:none; box-shadow:0 8px 24px rgba(15,118,110,0.12); }}
          .heat-label {{ font-weight:800; line-height:1.3; }}
          .heat-metric {{ font-size:22px; font-weight:800; }}
          .heat-meta {{ font-size:12px; opacity:0.92; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          a {{ color:#0f766e; text-decoration:none; font-weight:700; }}
        </style>
      </head>
      <body>
        <main class="wrap">
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
              <div class="muted">{'查看板块热力、热力排序和信号分布。' if lang == 'zh' else 'Open sector heat, sorting controls, and signal distribution.'}</div>
              <div style="margin-top:12px;"><a class="pill" href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}">{'打开板块热力图' if lang == 'zh' else 'Open Sector Heatmap'}</a></div>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'concept_activity_tracker')}</div>
              <div class="muted">{'查看概念共振、异动追踪和概念 drill-down。' if lang == 'zh' else 'Open concept resonance, activity tracking, and drill-down views.'}</div>
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
            <div style="font-size:32px;font-weight:800;margin:6px 0;">{market_context['resonance_score']:.1f}%</div>
            <div class="muted">{_dt(lang, 'concept_resonance_help')}</div>
            <div class="muted" style="margin-top:8px;">{_dt(lang, 'tracked_signals')}: {market_context['tracked_signal_count']}</div>
          </section>
          <section class="card">
            <div class="eyebrow">{'市场入口' if lang == 'zh' else 'Market Shortcuts'}</div>
            <div class="compare-row">
              <a href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="compare-pill active">{_dt(lang, 'sector_heatmap')}</a>
              <a href="/dashboard/market/concepts?lang={lang}&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="compare-pill">{_dt(lang, 'concept_activity_tracker')}</a>
            </div>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'sector_heatmap')}</div>
            <div class="muted">{_dt(lang, 'sector_heatmap_help')}</div>
            <div class="compare-row" style="margin-top:10px;">
              <span class="muted">{_dt(lang, 'heatmap_sort')}:</span>
              {heatmap_sort_pills}
            </div>
            <div class="heat-grid">{''.join(heatmap_tiles.split('</a>')[:4]) + ('</a>' if heatmap_tiles and '</a>' in heatmap_tiles else '') if heatmap_tiles.startswith('<a ') else heatmap_tiles}</div>
            <div class="muted" style="margin-top:10px;"><a href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}">{'查看完整板块热力图 →' if lang == 'zh' else 'Open full heatmap →'}</a></div>
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
              <div style="font-size:32px;font-weight:800;margin:6px 0;">{market_context['resonance_score']:.1f}%</div>
              <div class="muted">{_dt(lang, 'concept_resonance_help')}</div>
              <div class="muted" style="margin-top:8px;">{_dt(lang, 'tracked_signals')}: {market_context['tracked_signal_count']}</div>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{_dt(lang, 'concept_activity_tracker')}</div>
            <table>
              <thead><tr><th>{_dt(lang, 'concept')}</th><th>{_dt(lang, 'hits')}</th><th>{_dt(lang, 'prev')}</th><th>{_dt(lang, 'delta_hits')}</th><th>{_dt(lang, 'streak')}</th><th>{_dt(lang, 'trend')}</th><th>{_dt(lang, 'five_day')}</th><th>{_dt(lang, 'breadth')}</th><th>{_concept_tr(lang, 'buy_signal_count')}</th><th>{_concept_tr(lang, 'max_signal_strength')}</th><th>{'执行提醒' if lang == 'zh' else 'Execution Tags'}</th><th>{_dt(lang, 'avg_score')}</th><th>{_dt(lang, 'tickers')}</th></tr></thead>
              <tbody>{''.join(concept_rows.split('</tr>')[:4]) + ('</tr>' if concept_rows and '</tr>' in concept_rows else '') if concept_rows.startswith('<tr>') else concept_rows}</tbody>
            </table>
            <div class="muted" style="margin-top:10px;"><a href="/dashboard/market/concepts?lang={lang}&lookback_runs={lookback_runs}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}">{'查看完整概念追踪 →' if lang == 'zh' else 'Open full concept tracker →'}</a></div>
          </section>
        </main>
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
    summary = _load_summary(db, lookback_runs=lookback_runs)
    market_context = summary["market_context"]

    heatmap_rows = list(market_context["sector_heatmap"])
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
        for item in market_context["market_distribution"]
    ) or f"<tr><td colspan='2'>{'暂无信号分布' if lang == 'zh' else 'No signal distribution yet'}</td></tr>"
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
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'板块热力图' if lang == 'zh' else 'Sector Heatmap'}</title>
        <style>
          :root {{ --bg:#f5efe2; --panel:#fffdf7; --ink:#1f2937; --muted:#6b7280; --line:#d6cfc2; --accent:#0f766e; --accent-soft:#dff5ef; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#fff6d8 0,transparent 30%),radial-gradient(circle at top right,#d9f3ee 0,transparent 35%),var(--bg); }}
          .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }}
          .card {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .toolbar,.compare-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .pill,.compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#eef8f5; color:#0f766e; font-size:13px; font-weight:700; text-decoration:none; }}
          .compare-pill.active {{ background:#0f766e; color:#fff; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }}
          .heat-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); margin-top:12px; }}
          .heat-tile {{ color:#fff; border-radius:16px; padding:14px; min-height:110px; display:flex; flex-direction:column; justify-content:space-between; text-decoration:none; box-shadow:0 8px 24px rgba(15,118,110,0.12); }}
          .heat-label {{ font-weight:800; line-height:1.3; }}
          .heat-metric {{ font-size:22px; font-weight:800; }}
          .heat-meta {{ font-size:12px; opacity:0.92; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          a {{ color:#0f766e; text-decoration:none; font-weight:700; }}
        </style>
      </head>
      <body>
        <main class="wrap">
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
              <div style="font-size:32px;font-weight:800;margin:6px 0;">{market_context['resonance_score']:.1f}%</div>
              <div class="muted">{_dt(lang, 'concept_resonance_help')}</div>
              <div class="muted" style="margin-top:8px;">{_dt(lang, 'tracked_signals')}: {market_context['tracked_signal_count']}</div>
            </article>
          </section>
        </main>
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
    summary = _load_summary(db, lookback_runs=lookback_runs)
    market_context = summary["market_context"]
    concept_rows_source = list(market_context["concept_tracker"])
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
        f"<td>{item['hits']}</td><td>{item['previous_hits']}</td><td>{'+' if item['delta_hits'] > 0 else ''}{item['delta_hits']}</td><td>{item['streak']}</td><td>{_sparkline_svg(item['history'])}</td><td>{_percent_chip(item.get('avg_move_5d'))}</td><td>{_breadth_chip(item.get('breadth_pct'))}</td><td>{int(item.get('buy_signal_count') or 0)}</td><td>{int(item.get('max_signal_strength') or 0)}</td><td>{' · '.join(item.get('execution_tags') or []) or '-'}</td><td>{item['avg_score']:.4f}</td><td>{', '.join(item['tickers'])}</td>"
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
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'概念异动追踪' if lang == 'zh' else 'Concept Activity Tracker'}</title>
        <style>
          :root {{ --bg:#f5efe2; --panel:#fffdf7; --ink:#1f2937; --muted:#6b7280; --line:#d6cfc2; --accent:#0f766e; --accent-soft:#dff5ef; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#fff6d8 0,transparent 30%),radial-gradient(circle at top right,#d9f3ee 0,transparent 35%),var(--bg); }}
          .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }}
          .card {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .toolbar,.compare-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .pill, .compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#eef8f5; color:#0f766e; font-size:13px; font-weight:700; text-decoration:none; }}
          .compare-pill.active {{ background:#0f766e; color:#fff; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          a {{ color:#0f766e; text-decoration:none; font-weight:700; }}
        </style>
      </head>
      <body>
        <main class="wrap">
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
              <article class="card" style="margin:0;background:#f9f7f0;">
                <div class="eyebrow">{_dt(lang, 'tagged_names')}</div>
                <div style="font-size:28px;font-weight:800;margin:6px 0;">{tagged_names}</div>
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
            <div class="eyebrow">{_dt(lang, 'concept_activity_tracker')}</div>
            <div class="compare-row">
              <a href="/dashboard/market/concepts/export?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter, 'concept_sort_by': concept_sort_by, 'concept_sort_order': concept_sort_order})}" class="pill">Export CSV</a>
            </div>
            <table>
              <thead><tr><th>{_concept_tracker_sort_link('concept', _dt(lang, 'concept'))}</th><th>{_concept_tracker_sort_link('hits', _dt(lang, 'hits'))}</th><th>{_dt(lang, 'prev')}</th><th>{_concept_tracker_sort_link('delta', _dt(lang, 'delta_hits'))}</th><th>{_concept_tracker_sort_link('streak', _dt(lang, 'streak'))}</th><th>{_dt(lang, 'trend')}</th><th>{_concept_tracker_sort_link('five_day', _dt(lang, 'five_day'))}</th><th>{_concept_tracker_sort_link('breadth', _dt(lang, 'breadth'))}</th><th>{_concept_tracker_sort_link('buy_count', _concept_tr(lang, 'buy_signal_count'))}</th><th>{_concept_tracker_sort_link('max_strength', _concept_tr(lang, 'max_signal_strength'))}</th><th>{'执行提醒' if lang == 'zh' else 'Execution Tags'}</th><th>{_concept_tracker_sort_link('score', _dt(lang, 'avg_score'))}</th><th>{_dt(lang, 'tickers')}</th></tr></thead>
              <tbody>{concept_rows}</tbody>
            </table>
          </section>
        </main>
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
    summary = _load_summary(db, lookback_runs=lookback_runs)
    concept_rows_source = list(summary["market_context"]["concept_tracker"])
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
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    summary = _load_summary(db, lookback_runs=lookback_runs)
    auto_analysis = summary["auto_analysis"]
    latest_backtest = summary["latest_backtest"]
    recent_model_runs = summary["recent_model_runs"]
    sync_states = summary["sync_states"]
    recent_jobs = summary["recent_jobs"]
    dashboard_redirect = "/dashboard/ops?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})

    def status_badge(status: str) -> str:
        tone = {
            "success": ("#dcfce7", "#166534"),
            "failed": ("#fee2e2", "#991b1b"),
            "partial": ("#fef3c7", "#92400e"),
            "running": ("#dbeafe", "#1d4ed8"),
        }.get(status, ("#e5e7eb", "#374151"))
        return f"<span style='display:inline-block;padding:4px 8px;border-radius:999px;background:{tone[0]};color:{tone[1]};font-size:12px;font-weight:700;'>{status}</span>"

    recent_job_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td><td>{item['job_type']}</td><td>{status_badge(item['status'])}</td><td>{item['started_at']}</td><td>{item['finished_at'] or '-'}</td><td><code>{json.dumps(item['params']) if item['params'] else '-'}</code></td><td>{item['message'] or '-'}</td>"
        "</tr>"
        for item in recent_jobs
    ) or f"<tr><td colspan='7'>{'暂无任务' if lang == 'zh' else 'No jobs yet'}</td></tr>"
    sync_rows = "".join(
        f"<tr><td><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td><td>{item['provider']}</td><td>{item['last_synced_date'] or '-'}</td><td>{item['status'] or '-'}</td></tr>"
        for item in sync_states
    ) or f"<tr><td colspan='4'>{'暂无同步记录' if lang == 'zh' else 'No sync history yet'}</td></tr>"
    model_rows = "".join(
        f"<tr><td>{item['id']}</td><td>{item['name']}</td><td>{item['status']}</td><td><code>{item['config_json'] or '-'}</code></td><td>{item['created_at']}</td></tr>"
        for item in recent_model_runs
    ) or f"<tr><td colspan='5'>{'暂无模型运行' if lang == 'zh' else 'No model runs yet'}</td></tr>"
    backtest_pre = json.dumps(latest_backtest, indent=2) if latest_backtest else ("暂无回测" if lang == "zh" else "No backtest yet")
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'运维操作台' if lang == 'zh' else 'Operations'}</title>
        <style>
          :root {{ --bg:#f5efe2; --panel:#fffdf7; --ink:#1f2937; --muted:#6b7280; --line:#d6cfc2; --accent:#0f766e; --accent-soft:#dff5ef; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#fff6d8 0,transparent 30%),radial-gradient(circle at top right,#d9f3ee 0,transparent 35%),var(--bg); }}
          .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }}
          .card {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .pill, .action-link {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#eef8f5; color:#0f766e; font-size:13px; font-weight:700; text-decoration:none; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          form {{ display:grid; gap:10px; }}
          input, select, button {{ border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; }}
          button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
          code, pre {{ white-space:pre-wrap; word-break:break-word; }}
          pre {{ margin:0; font-size:13px; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div class="toolbar">
            <a href="/dashboard?lang={lang}&lookback_runs={lookback_runs}" class="pill">← {'返回总览' if lang == 'zh' else 'Back to dashboard'}</a>
            <a href="/dashboard/market?lang={lang}&lookback_runs={lookback_runs}" class="pill">{'市场脉冲' if lang == 'zh' else 'Market Pulse'}</a>
            <a href="/dashboard/ops?lang=en&lookback_runs={lookback_runs}" class="pill">English</a>
            <a href="/dashboard/ops?lang=zh&lookback_runs={lookback_runs}" class="pill">中文</a>
          </div>
          <div class="card">
            <div class="eyebrow">{'运维操作台' if lang == 'zh' else 'Operations'}</div>
            <h1 style="margin:0 0 8px;">{'同步、训练与回测' if lang == 'zh' else 'Sync, Training, and Backtests'}</h1>
            <p class="muted">{'把重操作和任务历史从首页拆出来，方便专注执行。' if lang == 'zh' else 'A dedicated page for heavy actions and recent job history.'}</p>
          </div>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{'同步中心' if lang == 'zh' else 'Sync Center'}</div>
              <div class="muted">{'处理行情、概念和基本面同步。' if lang == 'zh' else 'Handle market, concept, and fundamental sync.'}</div>
              <div style="margin-top:12px;"><a class="action-link" href="/dashboard/ops/sync?lang={lang}&lookback_runs={lookback_runs}">{'打开同步中心' if lang == 'zh' else 'Open Sync Center'}</a></div>
            </article>
            <article class="card">
              <div class="eyebrow">{'模型运行' if lang == 'zh' else 'Model Runs'}</div>
              <div class="muted">{'查看最近训练结果并从这里直接回测。' if lang == 'zh' else 'Review recent runs and trigger backtests directly.'}</div>
              <div style="margin-top:12px;"><a class="action-link" href="/dashboard/ops/models?lang={lang}&lookback_runs={lookback_runs}">{'打开模型运行' if lang == 'zh' else 'Open Model Runs'}</a></div>
            </article>
            <article class="card">
              <div class="eyebrow">{'任务记录' if lang == 'zh' else 'Job History'}</div>
              <div class="muted">{'专门查看任务状态、参数和消息。' if lang == 'zh' else 'Inspect task statuses, params, and messages.'}</div>
              <div style="margin-top:12px;"><a class="action-link" href="/dashboard/ops/jobs?lang={lang}&lookback_runs={lookback_runs}">{'打开任务记录' if lang == 'zh' else 'Open Job History'}</a></div>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'json_shortcuts')}</div>
              <div><a href="/dashboard/summary?lang={lang}&lookback_runs={lookback_runs}">{_dt(lang, 'dashboard_summary_json')}</a></div>
              <div><a href="/signals/latest">{_dt(lang, 'latest_signals_json')}</a></div>
              <div><a href="/backtests/latest/curve">{_dt(lang, 'latest_backtest_curve_json')}</a></div>
              <div><a href="/jobs/sync-states">{_dt(lang, 'sync_states_json')}</a></div>
            </article>
          </section>
        </main>
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
    summary = _load_summary(db, lookback_runs=lookback_runs)
    sync_states = summary["sync_states"]
    recent_jobs = summary["recent_jobs"]
    dashboard_redirect = "/dashboard/ops/sync?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    close_review_status = close_review_scheduler_service.get_status()
    cn_universe_job = next((item for item in recent_jobs if item["job_type"] == "sync_cn_symbol_universe"), None)
    cn_init_job = next((item for item in recent_jobs if item["job_type"] == "init_cn_market_data"), None)
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
    sync_rows = "".join(
        f"<tr><td><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td><td>{item['provider']}</td><td>{item['last_synced_date'] or '-'}</td><td>{item['status'] or '-'}</td></tr>"
        for item in sync_states
    ) or f"<tr><td colspan='4'>{'暂无同步记录' if lang == 'zh' else 'No sync history yet'}</td></tr>"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'同步中心' if lang == 'zh' else 'Sync Center'}</title>
        <style>
          :root {{ --bg:#f5efe2; --panel:#fffdf7; --ink:#1f2937; --muted:#6b7280; --line:#d6cfc2; --accent:#0f766e; --accent-soft:#dff5ef; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#fff6d8 0,transparent 30%),radial-gradient(circle at top right,#d9f3ee 0,transparent 35%),var(--bg); }}
          .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }}
          .card {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
          .toolbar,.grid {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; align-items:center; }}
          .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .pill, .action-link {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#eef8f5; color:#0f766e; text-decoration:none; font-size:13px; font-weight:700; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          form {{ display:grid; gap:10px; }}
          input, select, button {{ border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; }}
          button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div class="toolbar">
            <a href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}" class="pill">← {'返回运维操作台' if lang == 'zh' else 'Back to Operations'}</a>
            <a href="/dashboard/ops/sync?lang=en&lookback_runs={lookback_runs}" class="pill">English</a>
            <a href="/dashboard/ops/sync?lang=zh&lookback_runs={lookback_runs}" class="pill">中文</a>
          </div>
          <div class="card">
            <div class="eyebrow">{'同步中心' if lang == 'zh' else 'Sync Center'}</div>
            <h1 style="margin:0 0 8px;">{'行情与基本面同步' if lang == 'zh' else 'Market and Fundamental Sync'}</h1>
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
            <div class="muted" style="margin-top:8px;">{'最近初始化任务' if lang == 'zh' else 'Latest Init Job'}: <strong>{(cn_init_job or {}).get('status', 'idle')}</strong></div>
            <div style="margin-top:10px;height:12px;border-radius:999px;background:#efe7d7;overflow:hidden;">
              <div style="height:100%;width:{cn_progress_pct}%;background:linear-gradient(90deg,#0f766e,#34d399);"></div>
            </div>
            <div class="muted" style="margin-top:8px;">{cn_progress_pct}% ({cn_sync_success_count}/{cn_symbol_count})</div>
            <div style="margin-top:12px;">
              <a class="action-link" href="/screeners?lang={lang}&market=CN&universe=full_market&model_template=cn_bullish_ma_stack">{'去全市场技术选股' if lang == 'zh' else 'Open Full-Market Technical Screener'}</a>
            </div>
          </section>
          <section class="card">
            <div class="eyebrow">{'收盘自动复盘' if lang == 'zh' else 'Post-Close Review'}</div>
            <div class="muted">{'当前状态' if lang == 'zh' else 'Status'}: <strong>{('开启' if close_review_status['enabled'] else '关闭') if lang == 'zh' else ('Enabled' if close_review_status['enabled'] else 'Disabled')}</strong></div>
            <div class="muted">{'计划时间' if lang == 'zh' else 'Scheduled Time'}: <strong>{close_review_status['run_hour']:02d}:{close_review_status['run_minute']:02d}</strong> CST</div>
            <div class="muted">{'下次运行' if lang == 'zh' else 'Next Run'}: <strong>{close_review_status.get('next_run_at') or '-'}</strong></div>
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
                <select name="provider"><option value="yfinance">yfinance</option></select>
                <input type="text" name="start_date" placeholder="YYYY-MM-DD" />
                <input type="text" name="end_date" placeholder="YYYY-MM-DD" />
                <button type="submit">{_dt(lang, 'sync_market_data')}</button>
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
                <select name="provider"><option value="yfinance">yfinance</option></select>
                <button type="submit">{'初始化 A 股全市场数据' if lang == 'zh' else 'Init CN Market Data'}</button>
              </form>
              <div style="height:10px;"></div>
              <div class="eyebrow">{'刷新 A 股最近行情' if lang == 'zh' else 'Refresh Recent CN Market Data'}</div>
              <form action="/jobs/refresh-cn-market-data" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="number" name="days_back" min="2" step="1" value="7" placeholder="{ '刷新最近天数' if lang == 'zh' else 'Refresh Recent Days' }" />
                <input type="number" name="limit" min="0" step="1" value="0" placeholder="{ '股票数量限制（0 代表全部）' if lang == 'zh' else 'Limit (0 for all)' }" />
                <select name="provider"><option value="yfinance">yfinance</option></select>
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
            <table><thead><tr><th>{_dt(lang, 'ticker')}</th><th>{_dt(lang, 'provider')}</th><th>{_dt(lang, 'last_sync')}</th><th>{_dt(lang, 'status')}</th></tr></thead><tbody>{sync_rows}</tbody></table>
          </section>
        </main>
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
    dashboard_redirect = "/dashboard/ops/models?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    model_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td><td>{item['name']}</td><td>{item['status']}</td><td><code>{item['config_json'] or '-'}</code></td><td>{item['created_at']}</td>"
        f"<td><form action='/jobs/backtest' method='post' style='margin:0;'><input type='hidden' name='redirect_to' value='{dashboard_redirect}' /><input type='hidden' name='top_n' value='1' /><input type='hidden' name='model_run_id' value='{item['id']}' /><button type='submit' style='padding:8px 10px;font-size:12px;'>{_dt(lang, 'backtest_this_run')}</button></form></td>"
        "</tr>"
        for item in recent_model_runs
    ) or f"<tr><td colspan='6'>{'暂无模型运行' if lang == 'zh' else 'No model runs yet'}</td></tr>"
    backtest_pre = json.dumps(latest_backtest, indent=2) if latest_backtest else ("暂无回测" if lang == "zh" else "No backtest yet")
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>{'模型运行' if lang == 'zh' else 'Model Runs'}</title>
      <style>
        :root {{ --bg:#f5efe2; --panel:#fffdf7; --ink:#1f2937; --muted:#6b7280; --line:#d6cfc2; --accent:#0f766e; --accent-soft:#dff5ef; }}
        * {{ box-sizing:border-box; }} body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#fff6d8 0,transparent 30%),radial-gradient(circle at top right,#d9f3ee 0,transparent 35%),var(--bg); }}
        .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }} .card {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
        .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }} .pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#eef8f5; color:#0f766e; text-decoration:none; font-size:13px; font-weight:700; }}
        .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
        .muted {{ color:var(--muted); font-size:14px; }} table {{ width:100%; border-collapse:collapse; font-size:14px; }} th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }} th {{ color:var(--muted); font-weight:600; }}
        input, select, button {{ border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; }} button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }} pre, code {{ white-space:pre-wrap; word-break:break-word; }}
      </style></head>
      <body><main class="wrap">
        <div class="toolbar">
          <a href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}" class="pill">← {'返回运维操作台' if lang == 'zh' else 'Back to Operations'}</a>
          <a href="/dashboard/ops/models?lang=en&lookback_runs={lookback_runs}" class="pill">English</a>
          <a href="/dashboard/ops/models?lang=zh&lookback_runs={lookback_runs}" class="pill">中文</a>
        </div>
        <div class="card"><div class="eyebrow">{'模型运行' if lang == 'zh' else 'Model Runs'}</div><h1 style="margin:0 0 8px;">{'训练与回测视图' if lang == 'zh' else 'Training and Backtest View'}</h1><p class="muted">{'专门查看最近模型运行并从这里回测。' if lang == 'zh' else 'Review recent model runs and trigger backtests from here.'}</p></div>
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
          <table><thead><tr><th>ID</th><th>{_dt(lang, 'name')}</th><th>{_dt(lang, 'status')}</th><th>{_dt(lang, 'config')}</th><th>{_dt(lang, 'created')}</th><th>{_dt(lang, 'action')}</th></tr></thead><tbody>{model_rows}</tbody></table>
        </section>
        <section class="card"><div class="eyebrow">{_dt(lang, 'backtest_summary')}</div><pre>{backtest_pre}</pre></section>
      </main></body></html>
    """


@router.get("/ops/jobs", response_class=HTMLResponse)
def dashboard_ops_jobs_page(request: Request, lang: str = "en", lookback_runs: int = 5, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ops/jobs")
    lang = "zh" if lang == "zh" else "en"
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    recent_jobs = load_recent_jobs_summary(db, limit=8)
    def status_badge(status: str) -> str:
        tone = {"success":("#dcfce7","#166534"),"failed":("#fee2e2","#991b1b"),"partial":("#fef3c7","#92400e"),"running":("#dbeafe","#1d4ed8")}.get(status,("#e5e7eb","#374151"))
        return f"<span style='display:inline-block;padding:4px 8px;border-radius:999px;background:{tone[0]};color:{tone[1]};font-size:12px;font-weight:700;'>{status}</span>"
    recent_job_rows = "".join(
        "<tr>" f"<td>{item['id']}</td><td>{item['job_type']}</td><td>{status_badge(item['status'])}</td><td>{item['started_at']}</td><td>{item['finished_at'] or '-'}</td><td><code>{json.dumps(item['params']) if item['params'] else '-'}</code></td><td>{item['message'] or '-'}</td>" "</tr>"
        for item in recent_jobs
    ) or f"<tr><td colspan='7'>{'暂无任务' if lang == 'zh' else 'No jobs yet'}</td></tr>"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>{'任务记录' if lang == 'zh' else 'Job History'}</title>
      <style>
        :root {{ --bg:#f5efe2; --panel:#fffdf7; --ink:#1f2937; --muted:#6b7280; --line:#d6cfc2; --accent:#0f766e; --accent-soft:#dff5ef; }}
        * {{ box-sizing:border-box; }} body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#fff6d8 0,transparent 30%),radial-gradient(circle at top right,#d9f3ee 0,transparent 35%),var(--bg); }}
        .wrap {{ max-width:1080px; margin:0 auto; padding:32px 20px 56px; }} .card {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; }}
        .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }} .pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:#eef8f5; color:#0f766e; text-decoration:none; font-size:13px; font-weight:700; }}
        .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
        .muted {{ color:var(--muted); font-size:14px; }} table {{ width:100%; border-collapse:collapse; font-size:14px; }} th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }} th {{ color:var(--muted); font-weight:600; }} code {{ white-space:pre-wrap; word-break:break-word; }}
      </style></head>
      <body><main class="wrap">
        <div class="toolbar">
          <a href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}" class="pill">← {'返回运维操作台' if lang == 'zh' else 'Back to Operations'}</a>
          <a href="/dashboard/ops/jobs?lang=en&lookback_runs={lookback_runs}" class="pill">English</a>
          <a href="/dashboard/ops/jobs?lang=zh&lookback_runs={lookback_runs}" class="pill">中文</a>
        </div>
        <div class="card"><div class="eyebrow">{'任务记录' if lang == 'zh' else 'Job History'}</div><h1 style="margin:0 0 8px;">{'最近任务与参数' if lang == 'zh' else 'Recent Jobs and Parameters'}</h1><p class="muted">{'单独查看任务成功、失败和参数详情。' if lang == 'zh' else 'Inspect recent success, failure, and run parameters in one place.'}</p></div>
        <section class="card"><div class="eyebrow">{_dt(lang, 'recent_jobs')}</div><table><thead><tr><th>ID</th><th>{_dt(lang, 'type')}</th><th>{_dt(lang, 'status')}</th><th>{_dt(lang, 'started')}</th><th>{_dt(lang, 'finished')}</th><th>{_dt(lang, 'params')}</th><th>{_dt(lang, 'message')}</th></tr></thead><tbody>{recent_job_rows}</tbody></table></section>
      </main></body></html>
    """


@router.get("", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    lang = "zh" if request.query_params.get("lang") == "zh" else "en"
    session_mode = str(request.query_params.get("mode", "monitor")).lower()
    if session_mode not in {"premarket", "monitor", "postmarket"}:
        session_mode = "monitor"
    lookback_runs = _clamp_lookback_runs(request.query_params.get("lookback_runs", 5))
    heatmap_sort = str(request.query_params.get("heatmap_sort", "hits"))
    continuous_sort_by = str(request.query_params.get("continuous_sort_by", "hits"))
    continuous_sort_order = str(request.query_params.get("continuous_sort_order", "desc"))
    continuous_market = str(request.query_params.get("continuous_market", "ALL")).upper()
    continuous_state = str(request.query_params.get("continuous_state", "ALL")).upper()
    summary = _load_summary(db, lookback_runs=lookback_runs)
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
              <div class="metric">{latest_model['name'] if latest_model else 'None'}</div>
              <div class="muted">{_dt(lang, 'status')}: {latest_model['status'] if latest_model else '-'}</div>
              <div class="muted">{_dt(lang, 'type')}: {latest_model['model_type'] if latest_model else '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'backtest')}</div>
              <div class="metric">{latest_backtest['status'] if latest_backtest else 'None'}</div>
              <div class="muted">{_dt(lang, 'run')}: {latest_backtest['name'] if latest_backtest else '-'}</div>
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
    report = load_ai_daily_report(db=db) or {
        "mood": "-",
        "headline": "暂无可用的 A股 AI 日报，请先运行收盘复盘或手动生成。",
        "strategy": {"headline": "-", "playbook": "-", "bullets": []},
        "rows": [],
        "buy_the_dip_rows": [],
    }

    rows_html = "".join(
        "<tr>"
        f"<td>{item.get('ticker')}</td>"
        f"<td>{item.get('name') or item.get('ticker')}</td>"
        f"<td>{item.get('verdict') or '-'}</td>"
        f"<td>{item.get('confidence') or '-'}</td>"
        f"<td>{item.get('strategy') or '-'}</td>"
        f"<td>{item.get('headline') or '-'}</td>"
        f"<td>{item.get('summary') or '-'}</td>"
        "</tr>"
        for item in (report.get("rows") or [])
    ) or "<tr><td colspan='7'>No AI daily report yet.</td></tr>"
    buy_the_dip_html = "".join(
        "<tr>"
        f"<td>{item.get('ticker')}</td>"
        f"<td>{item.get('name') or item.get('ticker')}</td>"
        f"<td>{item.get('quant_rank') or '-'}</td>"
        f"<td>{item.get('trend_score') or '-'}</td>"
        f"<td>{item.get('strategy') or '-'}</td>"
        f"<td>{(item.get('buy_zone') or {}).get('low', '-')} - {(item.get('buy_zone') or {}).get('high', '-')}</td>"
        "</tr>"
        for item in (report.get("buy_the_dip_rows") or [])
    ) or "<tr><td colspan='6'>No Buy The Dip candidates yet.</td></tr>"

    return f"""
    <!DOCTYPE html>
    <html lang="zh">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>A股 AI 每日决策面板</title>
        <style>
          body {{ margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f5efe2; color:#1f2937; }}
          .wrap {{ max-width: 1100px; margin:0 auto; padding:28px 20px 56px; }}
          .card {{ background:#fffdf7; border:1px solid #d6cfc2; border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:#dff5ef; color:#0f766e; font-size:12px; font-weight:700; margin-bottom:12px; }}
          .metric {{ font-size:28px; font-weight:800; margin:4px 0 8px; }}
          .muted {{ color:#6b7280; font-size:14px; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; margin-top:12px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid #d6cfc2; vertical-align:top; }}
          th {{ color:#6b7280; font-weight:600; }}
          a {{ color:#0f766e; text-decoration:none; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div style="margin-bottom:16px;"><a href="/dashboard?lang=zh">← 返回 dashboard</a></div>
          <section class="card">
            <div class="eyebrow">A-Share AI Daily Report</div>
            <div class="metric">{report.get('mood') or '-'}</div>
            <div class="muted">A股复盘: {report.get('headline') or '-'}</div>
            <div style="margin-top:12px;padding:14px;border-radius:14px;background:#f8faf7;border:1px solid #d6cfc2;">
              <div style="font-weight:800;margin-bottom:6px;">{(report.get('strategy') or {}).get('headline') or '-'}</div>
              <div class="muted">{(report.get('strategy') or {}).get('playbook') or '-'}</div>
              <div style="margin-top:8px;">
                {"".join(f"<div class='muted'>• {item}</div>" for item in ((report.get('strategy') or {}).get('bullets') or [])) or "<div class='muted'>-</div>"}
              </div>
            </div>
            <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
              <a href="/dashboard/ai-daily-report/message">打开 A股推送文本</a>
              <form action="/jobs/send-ai-daily-report" method="post" style="display:inline;">
                <input type="hidden" name="redirect_to" value="/dashboard/ai-daily-report" />
                <button type="submit">发送 A股日报到已配置渠道</button>
              </form>
            </div>
            <table>
              <thead>
                <tr><th>代码</th><th>名称</th><th>结论</th><th>置信度</th><th>策略</th><th>Headline</th><th>Summary</th></tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <div style="margin-top:18px;font-weight:800;">Buy The Dip 10</div>
            <table>
              <thead>
                <tr><th>代码</th><th>名称</th><th>量化分</th><th>趋势分</th><th>策略</th><th>回踩区</th></tr>
              </thead>
              <tbody>{buy_the_dip_html}</tbody>
            </table>
          </section>
        </main>
      </body>
    </html>
    """


@router.get("/ai-daily-report/message", response_class=HTMLResponse)
def dashboard_ai_daily_report_message(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/ai-daily-report/message")
    report = load_ai_daily_report(db=db) or {
        "mood": "-",
        "headline": "暂无可用的 A股 AI 日报，请先运行收盘复盘或手动生成。",
        "strategy": {"headline": "-", "playbook": "-", "bullets": []},
        "rows": [],
        "buy_the_dip_rows": [],
    }
    message = render_ai_daily_report_message(report)
    return f"""
    <!DOCTYPE html>
    <html lang="zh">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>A股 AI 日报推送文本</title>
        <style>
          body {{ margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f5efe2; color:#1f2937; }}
          .wrap {{ max-width: 960px; margin:0 auto; padding:28px 20px 56px; }}
          .card {{ background:#fffdf7; border:1px solid #d6cfc2; border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:#dff5ef; color:#0f766e; font-size:12px; font-weight:700; margin-bottom:12px; }}
          textarea {{ width:100%; min-height:420px; border:1px solid #d6cfc2; border-radius:14px; padding:14px; font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; background:#fff; color:#1f2937; }}
          a {{ color:#0f766e; text-decoration:none; }}
          .muted {{ color:#6b7280; font-size:14px; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div style="margin-bottom:16px;"><a href="/dashboard/ai-daily-report">← 返回 A股 AI 每日决策面板</a></div>
          <section class="card">
            <div class="eyebrow">A-Share Push Ready</div>
            <div class="muted">下面这段文本默认只包含 A 股，可直接复制到 Telegram、企业微信、飞书或邮件。</div>
            <textarea readonly>{message}</textarea>
          </section>
        </main>
      </body>
    </html>
    """
