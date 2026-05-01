import csv
import html
import json
import re
from collections import Counter
from io import StringIO
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db_session
from app.models.tables import ModelRun, Prediction, PredictionDetail, Symbol
from app.models.schema import SymbolCreate
from app.services.ai_daily_report import (
    build_trade_explain_text,
    build_ai_daily_report,
    build_close_review_action_feed,
    format_risk_flags,
    format_trade_gate_reason,
    format_trade_status,
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
from app.services.market_lake import lake_file_health_summary, load_lake_price_history, load_lake_rows
from app.services.market_news import MarketNewsService
from app.services.market_sync import sync_market_data
from app.services.model_selection_guidance import (
    ACTION_BUCKET_LABELS,
    load_model_selection_guidance_snapshot,
    summarize_model_selection_guidance,
)
from app.services.model_signal_summary import build_model_state, build_signal_label, enrich_model_output, model_confidence
from app.services.nlp_snapshots import summarize_news_rows
from app.services.portfolio_book import (
    load_portfolio_positions,
    load_portfolio_trades,
    trade_reason_bucket,
    trade_reason_label,
)
from app.services.price_snapshot import load_latest_close
from app.services.push_notifications import PushNotificationService
from app.services.realtime_quotes import load_cn_intraday_bars, load_us_intraday_bars, load_us_latest_trades
from app.services.repository import (
    BacktestRepository,
    ConceptSnapshotRepository,
    DataJobRepository,
    FundamentalSnapshotRepository,
    ModelRunRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    PriceSyncStateRepository,
    SymbolRepository,
    TechnicalSnapshotRepository,
    WatchlistRepository,
    WorkspaceSnapshotRepository,
)
from app.services.runtime_cache import get_or_set
from app.services.screener import ScreenerService
from app.services.social_signals import social_signal_summary
from app.services.symbol_details import SymbolDataService
from app.services.template_evaluation import (
    build_lightgbm_evaluation,
    build_lightgbm_prediction_evaluation,
    build_next_tesla_evaluation,
    build_technical_momentum_evaluation,
    lightgbm_bias,
    lightgbm_maturity,
    next_tesla_market_bias,
    next_tesla_maturity,
    resolve_template_group_label,
    technical_momentum_bias,
    technical_momentum_maturity,
)
from app.services.time_utils import format_app_datetime
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html
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
    SNAPSHOT_WATCHLIST_NLP,
    load_latest_workspace_snapshot,
)


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _display_job_message(message: object, *, lang: str) -> str:
    text = str(message or "").strip()
    if not text:
        return "-"
    direct_patterns = [
        (
            r"^U\.S\. lake already contains (\d{4}-\d{2}-\d{2}) with (\d+) symbols; proceeding with training and screener precompute without a fresh Polygon success\.$",
            (lambda m: f"美股本地行情已补齐到 {m.group(1)}（{m.group(2)} 只），继续训练与预计算。")
            if lang == "zh"
            else (lambda m: f"U.S. lake already has {m.group(2)} symbols for {m.group(1)}; training and screener precompute continued without waiting for a fresh Polygon success."),
        ),
        (
            r"^Retraining U\.S\. LightGBM signals after full (\d{4}-\d{2}-\d{2}) grouped-daily refresh\.$",
            (lambda m: f"正在基于完整的 {m.group(1)} 美股收盘数据重训 LightGBM 模型。")
            if lang == "zh"
            else (lambda m: f"Retraining the U.S. LightGBM model from the full {m.group(1)} grouped-daily refresh."),
        ),
        (
            r"^Trained (\d+) U\.S\. symbols from full (\d{4}-\d{2}-\d{2}) refresh, wrote (\d+) predictions and (\d+) backtest rows\.$",
            (lambda m: f"已基于完整的 {m.group(2)} 美股收盘数据完成重训：{m.group(1)} 只股票，写入 {m.group(3)} 条预测、{m.group(4)} 条回测记录。")
            if lang == "zh"
            else (lambda m: f"Completed U.S. retraining from the full {m.group(2)} close: {m.group(1)} symbols, {m.group(3)} predictions, {m.group(4)} backtest rows."),
        ),
        (
            r"^Precomputing U\.S\. screener snapshots after full (\d{4}-\d{2}-\d{2}) retrain\.$",
            (lambda m: f"正在基于完整的 {m.group(1)} 美股重训结果生成筛选快照。")
            if lang == "zh"
            else (lambda m: f"Precomputing U.S. screener snapshots from the full {m.group(1)} retrain."),
        ),
        (
            r"^Precomputed (\d+) U\.S\. screener snapshot\(s\) after full (\d{4}-\d{2}-\d{2}) retrain\.$",
            (lambda m: f"已基于完整的 {m.group(2)} 美股重训结果生成 {m.group(1)} 个筛选快照。")
            if lang == "zh"
            else (lambda m: f"Precomputed {m.group(1)} U.S. screener snapshots from the full {m.group(2)} retrain."),
        ),
    ]
    for pattern, replacement in direct_patterns:
        matched = re.match(pattern, text, flags=re.IGNORECASE)
        if matched:
            return replacement(matched) if callable(replacement) else replacement
    replacements = [
        (r"共享\s*sqlite\s*库", "共享数据库" if lang == "zh" else "shared database"),
        (r"shared sqlite (?:library|db|database)", "共享数据库" if lang == "zh" else "shared database"),
        (r"\bsqlite\s+库\b", "兼容数据库" if lang == "zh" else "compatibility database"),
        (r"\bsqlite\s+database\b", "兼容数据库" if lang == "zh" else "compatibility database"),
        (r"\bsqlite\s+db\b", "兼容数据库" if lang == "zh" else "compatibility database"),
        (r"\bSQLite\b", "兼容数据库" if lang == "zh" else "compatibility database"),
        (r"\bsqlite\b", "兼容数据库" if lang == "zh" else "compatibility database"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _reason_screen_params(*, reason: str | None, status: str | None, market: str | None, lang: str) -> dict[str, object]:
    normalized_reason = str(reason or "").strip().lower()
    normalized_status = str(status or "").strip().lower()
    market_code = str(market or "CN").strip().upper()
    if market_code not in {"CN", "US", "HK", "ALL"}:
        market_code = "CN"
    cn_core_templates = [
        "lightgbm_top_picks",
        "next_tesla_swing",
        "technical_momentum",
        "cn_volume_breakout",
        "cn_bullish_ma_stack",
    ]
    us_core_templates = [
        "lightgbm_top_picks",
        "next_tesla_swing",
        "technical_momentum",
        "tv_multi_timeframe_bullish",
        "global_growth_value",
    ]
    market_templates = cn_core_templates if market_code == "CN" else us_core_templates
    params: dict[str, object] = {
        "lang": lang,
        "run": 1,
        "model_template": "lightgbm_top_picks" if market_code in {"CN", "US", "ALL"} else "technical_momentum",
        "universe": "full_market",
        "market": market_code,
        "min_trend_score": 0,
        "sort_by": "trade_readiness_score",
        "sort_order": "asc",
    }
    key = normalized_reason or normalized_status
    if key in {"extended_after_sharp_move", "do_not_chase"}:
        params.update(
            {
                "model_template": "next_tesla_swing",
                "multi_model_templates": market_templates[:3],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "breakout_confirmation",
                "sort_by": "momentum_5",
                "sort_order": "desc",
                "action_filter": "wait_for_breakout",
            }
        )
    elif key == "too_far_from_pullback_zone":
        params.update(
            {
                "model_template": "next_tesla_swing",
                "multi_model_templates": market_templates[:3],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "buy_the_dip",
                "sort_by": "trade_readiness_score",
                "sort_order": "asc",
                "action_filter": "buy_the_dip",
            }
        )
    elif key == "signal_not_actionable":
        params.update(
            {
                "model_template": "technical_momentum",
                "multi_model_templates": market_templates[:3],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "watchlist",
                "sort_by": "model_signal_strength",
                "sort_order": "asc",
                "action_filter": "hold_and_watch",
            }
        )
    elif key == "missing_latest_price":
        params.update(
            {
                "sort_by": "trade_readiness_score",
                "sort_order": "asc",
            }
        )
    elif key == "too_many_risk_flags":
        params.update(
            {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": market_templates,
                "min_multi_model_hits": 2,
                "confluence_action_filter": "ALL",
                "sort_by": "trade_readiness_score",
                "sort_order": "asc",
            }
        )
    elif key == "low_trade_readiness":
        params.update(
            {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": market_templates[:3],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "ALL",
                "sort_by": "trade_readiness_score",
                "sort_order": "asc",
            }
        )
    return params


def _reason_screen_href(*, reason: str | None, status: str | None, market: str | None, lang: str) -> str:
    return f"/screeners?{urlencode(_reason_screen_params(reason=reason, status=status, market=market, lang=lang), doseq=True)}"


def _reason_screen_link(
    label: str,
    *,
    reason: str | None,
    status: str | None,
    market: str | None,
    lang: str,
    css_class: str = "",
) -> str:
    href = html.escape(_reason_screen_href(reason=reason, status=status, market=market, lang=lang), quote=True)
    class_attr = f" class='{css_class}'" if css_class else ""
    return f"<a{class_attr} href='{href}'>{html.escape(label)}</a>"


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


def _recent_market_heat_history(db: Session, *, limit: int = 6) -> dict[str, list[int]]:
    snapshots = WorkspaceSnapshotRepository(db).list_snapshots(
        SNAPSHOT_MARKET_HEATMAP_WORKSPACE,
        limit=max(limit * 3, limit),
    )
    points: list[dict[str, int]] = []
    for snapshot in reversed(snapshots):
        payload = snapshot.get("payload") or {}
        distribution = payload.get("market_distribution") or []
        if not isinstance(distribution, list) or not distribution:
            continue
        point = {
            str(item.get("market") or "").upper(): int(item.get("count") or 0)
            for item in distribution
            if str(item.get("market") or "").strip()
        }
        if point:
            points.append(point)
    points = points[-limit:]
    return {
        market: [int(point.get(market, 0)) for point in points]
        for market in ("CN", "US")
    }


def _mini_trend_bars(values: list[int], *, lang: str) -> str:
    normalized = [max(0, int(value)) for value in values]
    if not normalized:
        label = "暂无趋势" if lang == "zh" else "No trend"
        return f"<div class='mini-trend empty'><span>{label}</span></div>"
    top = max(normalized) or 1
    bars = "".join(
        f"<span style='height:{max(16, int((value / top) * 100))}%;'></span>"
        for value in normalized
    )
    return f"<div class='mini-trend'>{bars}</div>"


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


def _dashboard_pseudo_strength_hint(item: dict, *, lang: str) -> str:
    flags = {str(flag).strip().lower() for flag in (item.get("risk_flags") or []) if str(flag).strip()}
    if "rolled-over-after-spike" in flags:
        return "伪强势 / 冲高转弱" if lang == "zh" else "False strength / rolled over"
    if "do-not-chase" in flags and "drawdown-risk" in flags:
        return "不要追高 / 回撤风险" if lang == "zh" else "Do not chase / drawdown risk"
    return ""


def _dashboard_signal_action_sets(latest_signals: list[dict]) -> dict[str, list[dict]]:
    actionable: list[dict] = []
    blocked: list[dict] = []
    trim_review: list[dict] = []
    for item in latest_signals:
        status = str(item.get("tradability_status") or "").upper()
        score = float(item.get("score") or 0.0)
        readiness = float(item.get("trade_readiness_score") or 0.0)
        risk_flags = [str(flag).strip() for flag in (item.get("risk_flags") or []) if str(flag).strip()]
        priority = item.get("priority") if item.get("priority") is not None else 99
        block_reason = str(item.get("block_reason") or "")
        if not block_reason:
            if "missing-latest-price" in risk_flags:
                block_reason = "missing_latest_price"
            elif len(risk_flags) >= 3:
                block_reason = "too_many_risk_flags"
            elif readiness and readiness < 55:
                block_reason = "low_trade_readiness"
        enriched = {
            **item,
            "status_tone": _signal_status_tone(status),
            "status_label": status or "UNKNOWN",
            "target_weight_pct": round(float(item.get("target_weight") or 0.0) * 100.0, 1) if item.get("target_weight") is not None else None,
            "risk_flags_text": "/".join(risk_flags[:3]) or "-",
            "block_reason": block_reason or "-",
            "sort_key": (priority, -(readiness or 0.0), -score, item.get("ticker") or ""),
        }
        if status == "BLOCKED" or block_reason:
            blocked.append({**enriched, "status_tone": _signal_status_tone("BLOCKED"), "status_label": "BLOCKED"})
        elif status == "READY":
            actionable.append(enriched)
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
        display_message = _display_job_message(item.get("message"), lang=lang)
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
            f"<div class='muted'>{item.get('job_type') or '-'} · {html.escape(display_message)}</div>"
            f"{action_html}"
            "</div>"
            "</div>"
        )
    actionable_rows = "".join(
        "<div style='display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{item.get('ticker')}</div><div class='muted'>{item.get('name') or item.get('ticker')}</div><div class='muted'>{item.get('execution_note') or item.get('entry_trigger') or '-'}</div>"
        + (f"<div class='muted' style='font-weight:700;color:#f59e0b;margin-top:4px;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div style='text-align:right;'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='muted'>{(str(item.get('target_weight_pct')) + '%') if item.get('target_weight_pct') is not None else '-'}</div></div>"
        "</div>"
        for item in signal_sets["actionable"]
    ) or f"<div class='muted'>{'暂无可执行候选' if lang == 'zh' else 'No actionable candidates yet'}</div>"
    blocked_rows = "".join(
        "<div style='display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{item.get('ticker')}</div><div class='muted'>{item.get('name') or item.get('ticker')}</div><div class='muted'>{_reason_screen_link(format_trade_gate_reason(item.get('block_reason'), lang=lang), reason=item.get('block_reason'), status=item.get('status_label'), market=item.get('market'), lang=lang)}</div><div class='muted'>{_reason_screen_link('查看同类筛选' if lang == 'zh' else 'Open screener', reason=item.get('block_reason'), status=item.get('status_label'), market=item.get('market'), lang=lang)}</div></div>"
        f"<div style='text-align:right;'><span class='signal {item.get('status_tone')}'>{html.escape(format_trade_status(item.get('status_label'), lang=lang))}</span><div class='muted'>{html.escape(format_risk_flags(item.get('risk_flags') or [], lang=lang))}</div></div>"
        "</div>"
        for item in signal_sets["blocked"]
    ) or f"<div class='muted'>{'暂无受阻候选' if lang == 'zh' else 'No blocked candidates'}</div>"
    review_rows = "".join(
        "<div style='display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{item.get('ticker')}</div><div class='muted'>{item.get('name') or item.get('ticker')}</div><div class='muted'>{item.get('execution_note') or item.get('invalidation_condition') or '-'}</div>"
        + (f"<div class='muted' style='font-weight:700;color:#f59e0b;margin-top:4px;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div style='text-align:right;'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='muted'>{html.escape(format_risk_flags(item.get('risk_flags') or [], lang=lang))}</div></div>"
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
    selection_guidance_summary: dict | None = None,
) -> str:
    guidance_summary = selection_guidance_summary or {}
    snapshot_meta = guidance_summary.get("snapshot_meta") or {}
    top_model_href = str(guidance_summary.get("top_model_href") or f"/dashboard/model-performance?lang={lang}")
    top_combo_href = str(guidance_summary.get("top_combo_href") or "/screeners?lang=" + lang)
    top_model_title = html.escape(str(guidance_summary.get("top_model_title") or ("样本继续沉淀" if lang == "zh" else "Still collecting samples")))
    top_combo_title = html.escape(str(guidance_summary.get("top_combo_title") or ("组合样本继续沉淀" if lang == "zh" else "Combo samples still accumulating")))
    top_model_copy = html.escape(str(guidance_summary.get("top_model_summary") or ("当前还没有足够样本。" if lang == "zh" else "Not enough samples yet.")))
    top_combo_copy = html.escape(str(guidance_summary.get("top_combo_summary") or ("当前还没有足够组合样本。" if lang == "zh" else "Not enough combo samples yet.")))
    source_label = (
        "来源：后台快照"
        if str(snapshot_meta.get("source") or "") == "snapshot" and lang == "zh"
        else "来源：实时回退"
        if lang == "zh"
        else "Source: snapshot"
        if str(snapshot_meta.get("source") or "") == "snapshot"
        else "Source: live fallback"
    )
    source_time = html.escape(str(snapshot_meta.get("snapshot_date") or snapshot_meta.get("generated_at") or "-"))
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
        <div class="eyebrow">{'今日模型使用指导' if lang == 'zh' else "Today's Model Guidance"}</div>
        <div class="grid" style="margin-bottom:0;">
          <article class="card" style="margin-bottom:0;background:#f9f7f0;">
            <div class="eyebrow">{'优先模型' if lang == 'zh' else 'Priority Model'}</div>
            <div style="font-size:22px;font-weight:800;line-height:1.25;margin:4px 0 8px;">{top_model_title}</div>
            <div class="muted">{top_model_copy}</div>
            <div class="muted" style="margin-top:8px;">{source_label} · {source_time}</div>
            <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
              <a class='pill' href='{html.escape(top_model_href, quote=True)}'>{'用这套模型去筛股' if lang == 'zh' else 'Screen with this model'}</a>
            </div>
          </article>
          <article class="card" style="margin-bottom:0;background:#f9f7f0;">
            <div class="eyebrow">{'优先组合' if lang == 'zh' else 'Priority Combo'}</div>
            <div style="font-size:22px;font-weight:800;line-height:1.25;margin:4px 0 8px;">{top_combo_title}</div>
            <div class="muted">{top_combo_copy}</div>
            <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
              <a class='pill' href='{html.escape(top_combo_href, quote=True)}'>{'用这套组合去筛股' if lang == 'zh' else 'Screen with this combo'}</a>
              <a class='pill' href='/dashboard/model-performance?lang={lang}'>{'打开模型评测' if lang == 'zh' else 'Open Model Performance'}</a>
            </div>
          </article>
        </div>
      </section>
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
            f"<div class='muted' style='margin-top:8px;'><strong>{item.get('name') or item.get('ticker') or '-'}</strong> · {item.get('ticker') or '-'} · {item.get('verdict') or '-'} · 仓位 {item.get('target_weight') or '-'} · {item.get('tradability_status') or '-'}"
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

MARKET_PULSE_SOFT_RISK_TAGS = {
    "low-conviction",
    "drawdown-risk",
}


def _dt(lang: str, key: str, **kwargs) -> str:
    value = DASHBOARD_TEXT["zh" if lang == "zh" else "en"][key]
    return value.format(**kwargs) if kwargs else value


def _heatmap_metric_label(lang: str, metric: str) -> str:
    labels = {
        "zh": {
            "model": "模型强度",
            "five_day": "5日强弱",
            "breadth": "上涨广度",
            "buy": "买点密度",
            "flow": "资金流代理",
        },
        "en": {
            "model": "Model strength",
            "five_day": "5D strength",
            "breadth": "Breadth",
            "buy": "Buy density",
            "flow": "Flow proxy",
        },
    }
    normalized = metric if metric in {"model", "five_day", "breadth", "buy", "flow"} else "model"
    return labels["zh" if lang == "zh" else "en"][normalized]


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
        job_repo.complete_stale_running_jobs(
            job_types=["social_us_price_sync"],
            stale_after_hours=1,
            message_prefix="Ops cleanup closed a stale social U.S. price sync job.",
        )
        job_repo.complete_stale_running_jobs(
            stale_after_hours=6,
            message_prefix="Ops cleanup closed a stale running job.",
        )
        return {
            "generated_at": datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat(),
            "auto_analysis": auto_analysis_service.get_status(db=db),
            "latest_model": model_repo.get_latest_run_summary() or {},
            "recent_model_runs": model_repo.list_recent_runs(limit=8),
            "latest_backtest": backtest_repo.get_latest_backtest_summary() or {},
            "recent_jobs": job_repo.list_recent_jobs(limit=20),
            "sync_overview": sync_repo.get_status_overview(),
            "recent_sync_states": sync_repo.list_recent_states_with_symbols(limit=5),
            "lake_health": lake_file_health_summary(),
        }

    return get_or_set("dashboard_ops_summary_bundle", "latest", ttl_seconds=30.0, loader=_load)


def _load_cached_ai_daily_report(db: Session) -> dict:
    report = get_or_set(
        "dashboard_ai_daily_report",
        "latest",
        ttl_seconds=45.0,
        loader=lambda: load_ai_daily_report(db=db) or {},
    )
    return _hydrate_ai_report_names(report, db=db)


def _hydrate_ai_report_names(report: dict | None, *, db: Session) -> dict:
    payload = report or {}
    row_groups = [
        payload.get("portfolio_rows") or [],
        payload.get("market_recommendations") or [],
        payload.get("market_watch_recommendations") or [],
        payload.get("market_candidates_all") or [],
        payload.get("rows") or [],
        payload.get("buy_the_dip_rows") or [],
        payload.get("us_model_recommendations") or [],
        ((payload.get("social_signal_summary") or {}).get("actionable") or []),
        payload.get("us_hotspot_validation") or [],
    ]
    tickers: list[str] = []
    for rows in row_groups:
        for item in rows:
            ticker = str(item.get("ticker") or "").strip().upper()
            if ticker:
                tickers.append(ticker)
    if not tickers:
        return payload
    overviews = SymbolRepository(db).list_overviews_for_tickers(list(dict.fromkeys(tickers)))
    for rows in row_groups:
        for item in rows:
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            name = str(item.get("name") or "").strip()
            resolved_name = str((overviews.get(ticker) or {}).get("name") or "").strip()
            if resolved_name and (not name or name == ticker):
                item["name"] = resolved_name
    return payload


def _display_time(value: str | None, *, with_tz: bool = False) -> str:
    return format_app_datetime(value, with_tz=with_tz)


def _report_market_rows(report: dict) -> list[dict]:
    payload = report or {}
    explicit_all = payload.get("market_candidates_all")
    if isinstance(explicit_all, list) and explicit_all:
        return list(explicit_all)
    actionable = list(payload.get("market_recommendations") or payload.get("rows") or [])
    watch = list(payload.get("market_watch_recommendations") or [])
    combined: list[dict] = []
    seen: set[str] = set()
    for item in actionable + watch:
        ticker = str(item.get("ticker") or "").strip().upper()
        key = ticker or str(id(item))
        if key in seen:
            continue
        seen.add(key)
        combined.append(item)
    return combined


def _report_outcome_rows(report: dict, *, report_date: str | None) -> list[dict]:
    rows = _report_market_rows(report)
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


def _forward_return_from_history(history: list[dict], *, trade_date: str, sessions: int) -> float | None:
    if not history or sessions <= 0:
        return None
    start_index = next((index for index, row in enumerate(history) if str(row.get("date") or "") >= str(trade_date)), None)
    if start_index is None:
        return None
    end_index = start_index + sessions
    if end_index >= len(history):
        return None
    start_close = history[start_index].get("close")
    end_close = history[end_index].get("close")
    if start_close in (None, 0) or end_close is None:
        return None
    try:
        return round(((float(end_close) / float(start_close)) - 1.0) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _aggregate_window_stats(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "avg_return": None,
            "hit_rate": None,
            "strong_hit_rate": None,
            "miss_rate": None,
        }
    count = len(values)
    hit_count = sum(1 for item in values if item > 0)
    strong_hit_count = sum(1 for item in values if item >= 3.0)
    miss_count = sum(1 for item in values if item <= -3.0)
    return {
        "count": count,
        "avg_return": round(sum(values) / count, 2),
        "hit_rate": round((hit_count / count) * 100.0, 1),
        "strong_hit_rate": round((strong_hit_count / count) * 100.0, 1),
        "miss_rate": round((miss_count / count) * 100.0, 1),
    }


def _build_recommendation_validation_summary(
    db: Session,
    *,
    market: str,
    lang: str,
    selection_guidance: dict | None,
    selection_guidance_summary: dict | None = None,
    report_limit: int = 30,
) -> dict:
    windows = (1, 3, 5, 10)
    market_code = str(market or "CN").strip().upper()
    target_market = market_code if market_code in {"CN", "US"} else None
    guidance = selection_guidance or {}
    recommendations = list(guidance.get("recommendations") or [])
    combos = list(guidance.get("combos") or [])
    top_model = recommendations[0] if recommendations else {}
    top_combo = combos[0] if combos else {}
    guidance_summary = selection_guidance_summary or summarize_model_selection_guidance(selection_guidance, lang=lang)

    def _stats_for(item: dict, window: int) -> dict:
        return dict(item.get(f"stats_{window}d") or {})

    def _sample_count(item: dict) -> int:
        return max(int(_stats_for(item, window).get("count") or 0) for window in windows)

    rows: list[dict] = []
    if top_model:
        model_label = str(top_model.get("template_label") or top_model.get("template") or "-")
        action_bucket = str(top_model.get("action_bucket") or "").strip()
        if action_bucket and action_bucket not in {"ALL", "unclassified"}:
            action_label = ACTION_BUCKET_LABELS.get(action_bucket, {}).get(lang, action_bucket)
            model_label = f"{model_label} · {action_label}"
        rows.append(
            {
                "key": "priority_model",
                "label": "今日优先模型" if lang == "zh" else "Priority Model",
                "title": model_label,
                "count": _sample_count(top_model),
                "windows": {window: _stats_for(top_model, window) for window in windows},
                "note": (
                    f"强票提前覆盖 {int(top_model.get('winner_capture_count') or 0)} 只"
                    if lang == "zh"
                    else f"Captured {int(top_model.get('winner_capture_count') or 0)} strong movers"
                ),
                "href": guidance_summary.get("top_model_href"),
            }
        )
    if top_combo:
        combo_label = (top_combo.get("label") or {}).get(lang) or (top_combo.get("label") or {}).get("zh") or "-"
        rows.append(
            {
                "key": "priority_combo",
                "label": "今日优先组合" if lang == "zh" else "Priority Combo",
                "title": combo_label,
                "count": _sample_count(top_combo),
                "windows": {window: _stats_for(top_combo, window) for window in windows},
                "note": (
                    f"强票覆盖率 {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}"
                    if lang == "zh"
                    else f"Winner capture {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}"
                ),
                "href": top_combo.get("screener_href"),
            }
        )

    report_values: dict[int, list[float]] = {window: [] for window in windows}
    measured_rows = 0
    report_count = 0
    for item in list_ai_daily_report_history(limit=max(5, int(report_limit)), db=db):
        payload = item.get("payload") or {}
        report_date = str(item.get("snapshot_date") or payload.get("report_date") or "")[:10]
        if not report_date:
            continue
        rows_payload = _report_market_rows(payload)
        report_has_measurement = False
        for row in rows_payload[:5]:
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            row_market = str(row.get("market") or "").strip().upper()
            if not row_market:
                row_market = "CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US"
            if target_market and row_market != target_market:
                continue
            history = load_lake_price_history(market=row_market, ticker=ticker, limit=260)
            measured = False
            for window in windows:
                value = _forward_return_from_history(history, trade_date=report_date, sessions=window)
                if value is None:
                    continue
                report_values[window].append(float(value))
                measured = True
            if measured:
                measured_rows += 1
                report_has_measurement = True
        if report_has_measurement:
            report_count += 1
    rows.append(
        {
            "key": "ai_report_top5",
            "label": "AI 日报 Top 5" if lang == "zh" else "AI Report Top 5",
            "title": "历史日报推荐留档" if lang == "zh" else "Archived Daily Recommendations",
            "count": measured_rows,
            "windows": {window: _aggregate_window_stats(report_values[window]) for window in windows},
            "note": (
                f"已纳入 {report_count} 期日报归档"
                if lang == "zh"
                else f"Based on {report_count} archived reports"
            ),
            "href": f"/dashboard/ai-daily-report/history?lang={lang}",
        }
    )
    return {
        "rows": rows,
        "windows": windows,
        "report_count": report_count,
        "measured_rows": measured_rows,
    }


def _return_since_history_start(history: list[dict], *, trade_date: str) -> float | None:
    if not history:
        return None
    start_index = next((index for index, row in enumerate(history) if str(row.get("date") or "") >= str(trade_date)), None)
    if start_index is None or start_index >= len(history):
        return None
    start_close = history[start_index].get("close")
    latest_close = history[-1].get("close")
    if start_close in (None, 0) or latest_close is None:
        return None
    try:
        return round(((float(latest_close) / float(start_close)) - 1.0) * 100.0, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _build_watchlist_post_add_summary(db: Session, *, market: str = "ALL") -> dict:
    normalized_market = str(market or "ALL").upper()
    cache_key = json.dumps({"market": normalized_market}, sort_keys=True, ensure_ascii=False)

    def _loader() -> dict:
        watchlist_repo = WatchlistRepository(db)
        watchlist = watchlist_repo.get_or_create_default()
        items = watchlist_repo.list_items(watchlist.id)
        if normalized_market != "ALL":
            items = [item for item in items if str(item.get("market") or "").upper() == normalized_market]
        rows: list[dict] = []
        window_values: dict[int, list[float]] = {3: [], 5: [], 10: []}
        current_values: list[float] = []
        for item in items:
            ticker = str(item.get("ticker") or "").upper()
            item_market = str(item.get("market") or "CN").upper() or "CN"
            added_at = str(item.get("created_at") or "")
            added_date = added_at[:10] if len(added_at) >= 10 else added_at
            if not ticker or not added_date:
                continue
            history = load_lake_price_history(market=item_market, ticker=ticker, limit=260)
            row_payload = {
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "market": item_market,
                "added_date": added_date,
                "last_synced_date": item.get("last_synced_date"),
                "sync_status": item.get("sync_status"),
                "return_3d": _forward_return_from_history(history, trade_date=added_date, sessions=3),
                "return_5d": _forward_return_from_history(history, trade_date=added_date, sessions=5),
                "return_10d": _forward_return_from_history(history, trade_date=added_date, sessions=10),
                "current_return": _return_since_history_start(history, trade_date=added_date),
            }
            for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                value = row_payload.get(key)
                if value is not None:
                    window_values[window].append(float(value))
            if row_payload.get("current_return") is not None:
                current_values.append(float(row_payload["current_return"]))
            rows.append(row_payload)
        rows.sort(
            key=lambda item: (
                str(item.get("added_date") or ""),
                float(item.get("current_return") or -9999.0),
            ),
            reverse=True,
        )
        current_summary = _aggregate_window_stats(current_values)
        return {
            "rows": rows[:80],
            "count": len(rows),
            "windows": {window: _aggregate_window_stats(values) for window, values in window_values.items()},
            "current": {
                "count": current_summary.get("count"),
                "avg_return": current_summary.get("avg_return"),
                "hit_rate": current_summary.get("hit_rate"),
            },
        }

    return get_or_set("dashboard_watchlist_post_add_performance", cache_key, ttl_seconds=180.0, loader=_loader)


def _build_weekly_review_summary(db: Session, *, lang: str) -> dict:
    today = datetime.now(timezone.utc).astimezone().date()
    week_start = today - timedelta(days=6)
    week_start_iso = week_start.isoformat()
    cache_key = json.dumps({"week_start": week_start_iso, "lang": lang}, sort_keys=True, ensure_ascii=False)

    def _loader() -> dict:
        price_history_cache: dict[tuple[str, str], list[dict]] = {}

        def _cached_price_history(*, market_code: str, ticker: str) -> list[dict]:
            cache_key = (market_code, ticker)
            if cache_key not in price_history_cache:
                price_history_cache[cache_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
            return price_history_cache.get(cache_key) or []

        report_history = [
            item
            for item in list_ai_daily_report_history(limit=20, db=db)
            if str(item.get("snapshot_date") or "") >= week_start_iso
        ]
        top_ticker_counts: dict[str, dict] = {}
        mood_counts: dict[str, int] = {}
        report_window_values: dict[int, list[float]] = {1: [], 3: [], 5: [], 10: []}
        measured_report_rows = 0
        for item in report_history:
            payload = item.get("payload") or {}
            mood = str(payload.get("mood") or "").strip() or ("未标记" if lang == "zh" else "Unlabeled")
            mood_counts[mood] = mood_counts.get(mood, 0) + 1
            report_date = str(item.get("snapshot_date") or payload.get("report_date") or "")[:10]
            for row in _report_market_rows(payload)[:5]:
                ticker = str(row.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                bucket = top_ticker_counts.setdefault(
                    ticker,
                    {
                        "ticker": ticker,
                        "name": row.get("name") or ticker,
                        "count": 0,
                        "latest_verdict": row.get("verdict") or "-",
                    },
                )
                bucket["count"] += 1
                market_code = str(row.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US")
                history = _cached_price_history(market_code=market_code, ticker=ticker)
                row_measured = False
                for window in (1, 3, 5, 10):
                    value = _forward_return_from_history(history, trade_date=report_date, sessions=window)
                    if value is None:
                        continue
                    report_window_values[window].append(float(value))
                    row_measured = True
                if row_measured:
                    measured_report_rows += 1
        repeated_top_tickers = sorted(
            top_ticker_counts.values(),
            key=lambda item: (-int(item.get("count") or 0), str(item.get("ticker") or "")),
        )[:8]
        report_window_summary = {window: _aggregate_window_stats(values) for window, values in report_window_values.items()}

        selection_guidance = load_model_selection_guidance_snapshot(db, market="CN", allow_fallback=True)
        selection_guidance_summary = summarize_model_selection_guidance(selection_guidance, lang=lang)
        top_model = dict(selection_guidance_summary.get("top_model") or {})
        top_combo = dict(selection_guidance_summary.get("top_combo") or {})
        recommendation_validation_rows: list[dict] = []
        if top_model:
            recommendation_validation_rows.append(
                {
                    "label": "今日优先模型" if lang == "zh" else "Priority Model",
                    "title": selection_guidance_summary.get("top_model_title") or "-",
                    "href": selection_guidance_summary.get("top_model_href") or f"/dashboard/model-performance?lang={lang}",
                    "windows": {
                        1: top_model.get("stats_1d") or {},
                        3: top_model.get("stats_3d") or {},
                        5: top_model.get("stats_5d") or {},
                        10: top_model.get("stats_10d") or {},
                    },
                    "note": (
                        f"强票提前覆盖 {int(top_model.get('winner_capture_count') or 0)} 只"
                        if lang == "zh"
                        else f"Captured {int(top_model.get('winner_capture_count') or 0)} strong movers"
                    ),
                }
            )
        if top_combo:
            combo_label = (top_combo.get("label") or {}).get(lang) or (top_combo.get("label") or {}).get("zh") or "-"
            recommendation_validation_rows.append(
                {
                    "label": "今日优先组合" if lang == "zh" else "Priority Combo",
                    "title": combo_label,
                    "href": top_combo.get("screener_href") or f"/screeners?lang={lang}",
                    "windows": {
                        1: top_combo.get("stats_1d") or {},
                        3: top_combo.get("stats_3d") or {},
                        5: top_combo.get("stats_5d") or {},
                        10: top_combo.get("stats_10d") or {},
                    },
                    "note": (
                        f"强票覆盖率 {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}"
                        if lang == "zh"
                        else f"Winner capture {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}"
                    ),
                }
            )
        recommendation_validation_rows.append(
            {
                "label": "本周 AI 日报 Top 5" if lang == "zh" else "Weekly AI Report Top 5",
                "title": "日报归档自动验证" if lang == "zh" else "Archived Daily Recommendations",
                "href": f"/dashboard/ai-daily-report/history?lang={lang}",
                "windows": report_window_summary,
                "note": (
                    f"{len(report_history)} 期日报，{measured_report_rows} 个可测样本"
                    if lang == "zh"
                    else f"{len(report_history)} reports, {measured_report_rows} measurable rows"
                ),
            }
        )

        recent_jobs = DataJobRepository(db).list_recent_jobs(limit=180)
        weekly_jobs = [item for item in recent_jobs if str(item.get("started_at") or "")[:10] >= week_start_iso]
        job_status_counts: dict[str, int] = {}
        partial_or_failed_jobs: list[dict] = []
        for item in weekly_jobs:
            status = str(item.get("status") or "").lower() or "unknown"
            job_status_counts[status] = job_status_counts.get(status, 0) + 1
            if status in {"failed", "partial", "empty"}:
                partial_or_failed_jobs.append(item)

        recent_runs = [
            item
            for item in ModelRunRepository(db).list_recent_runs(limit=24)
            if str(item.get("created_at") or "")[:10] >= week_start_iso and str(item.get("status") or "").lower() == "success"
        ]
        run_rows: list[dict] = []
        model_window_values: dict[int, list[float]] = {3: [], 5: [], 10: []}
        for item in recent_runs[:8]:
            summary = _build_model_run_performance_summary(
                db,
                run_id=int(item["id"]),
                top_n=10,
                max_trade_dates=20,
                market=str(item.get("market") or "ALL"),
            )
            windows = (summary or {}).get("windows") or {}
            run_payload = {
                "id": int(item["id"]),
                "name": item.get("name") or "-",
                "market": item.get("market") or "-",
                "latest_trade_date": (summary or {}).get("latest_trade_date") or "-",
                "window_3": windows.get(3) or {},
                "window_5": windows.get(5) or {},
                "window_10": windows.get(10) or {},
            }
            for window in (3, 5, 10):
                avg_return = (windows.get(window) or {}).get("avg_return")
                if avg_return is not None:
                    model_window_values[window].append(float(avg_return))
            run_rows.append(run_payload)
        model_window_summary = {window: _aggregate_window_stats(values) for window, values in model_window_values.items()}

        weekly_trades = [
            item for item in load_portfolio_trades()
            if str(item.get("trade_date") or "") >= week_start_iso
        ]
        enriched_weekly_trades: list[dict] = []
        for item in weekly_trades:
            item_market = str(item.get("market") or "").strip().upper() or (
                "CN" if str(item.get("ticker") or "").upper().endswith((".SS", ".SZ", ".SH", ".BJ")) else "US"
            )
            ticker = str(item.get("ticker") or "").strip().upper()
            history = _cached_price_history(market_code=item_market, ticker=ticker)
            enriched_weekly_trades.append(
                {
                    **item,
                    "post_sell_return_3d": _forward_return_from_history(history, trade_date=str(item.get("trade_date") or ""), sessions=3),
                    "post_sell_return_5d": _forward_return_from_history(history, trade_date=str(item.get("trade_date") or ""), sessions=5),
                    "post_sell_return_10d": _forward_return_from_history(history, trade_date=str(item.get("trade_date") or ""), sessions=10),
                }
            )
        weekly_trades = enriched_weekly_trades
        realized_pnl = round(sum(float(item.get("realized_pnl") or 0.0) for item in weekly_trades), 2)
        winners = sum(1 for item in weekly_trades if float(item.get("realized_pnl") or 0.0) > 0)
        advice_effectiveness: dict[str, dict] = {}
        for item in weekly_trades:
            advice_key = trade_reason_bucket(item.get("reason"))
            bucket = advice_effectiveness.setdefault(
                advice_key,
                {"count": 0, "winner_count": 0, "realized_pnl": 0.0, "avg_return": 0.0},
            )
            bucket["count"] += 1
            pnl = float(item.get("realized_pnl") or 0.0)
            pnl_pct = float(item.get("realized_pnl_pct") or 0.0)
            bucket["realized_pnl"] += pnl
            bucket["avg_return"] += pnl_pct
            if pnl > 0:
                bucket["winner_count"] += 1
        advice_labels = {
            "profit_protection": "止盈/保护利润" if lang == "zh" else "Profit Protection",
            "risk_reduction": "止损/风险收缩" if lang == "zh" else "Risk Reduction",
            "rebalance": "调仓" if lang == "zh" else "Rebalance",
            "review": "复核后卖出" if lang == "zh" else "Review-led Exit",
            "event_risk": "事件风险" if lang == "zh" else "Event Risk",
            "other": "其他" if lang == "zh" else "Other",
        }
        advice_rows = []
        for key, payload in advice_effectiveness.items():
            count = int(payload.get("count") or 0)
            avg_return = (float(payload.get("avg_return") or 0.0) / count) if count else 0.0
            advice_rows.append(
                {
                    "bucket_key": key,
                    "bucket_label": advice_labels.get(key, key),
                    "count": count,
                    "winner_count": int(payload.get("winner_count") or 0),
                    "win_rate": round((int(payload.get("winner_count") or 0) / count) * 100.0, 1) if count else None,
                    "realized_pnl": round(float(payload.get("realized_pnl") or 0.0), 2),
                    "avg_return": round(avg_return, 2),
                }
            )
        advice_rows.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("bucket_label") or "")))
        structured_reason_count = sum(
            1
            for item in weekly_trades
            if str(item.get("reason") or "").strip() and str(item.get("reason") or "").strip() != "其他"
        )
        unresolved_trade_rows = [
            item for item in weekly_trades
            if str(item.get("reason") or "").strip() == "其他"
        ]
        audited_trade_rows = [
            item for item in weekly_trades
            if str(item.get("action_hint_at_exit") or "").strip() or str(item.get("action_reason_at_exit") or "").strip()
        ]

        return {
            "week_start": week_start_iso,
            "week_end": today.isoformat(),
            "report_count": len(report_history),
            "mood_counts": mood_counts,
            "repeated_top_tickers": repeated_top_tickers,
            "report_window_summary": report_window_summary,
            "measured_report_rows": measured_report_rows,
            "recommendation_validation_rows": recommendation_validation_rows,
            "job_status_counts": job_status_counts,
            "partial_or_failed_jobs": partial_or_failed_jobs[:10],
            "run_rows": run_rows,
            "model_window_summary": model_window_summary,
            "trade_rows": weekly_trades[:20],
            "trade_summary": {
                "count": len(weekly_trades),
                "realized_pnl": realized_pnl,
                "winner_count": winners,
            },
            "audit_summary": {
                "count": len(audited_trade_rows),
                "coverage_pct": round((len(audited_trade_rows) / len(weekly_trades)) * 100.0, 1) if weekly_trades else None,
            },
            "structured_reason_summary": {
                "count": structured_reason_count,
                "coverage_pct": round((structured_reason_count / len(weekly_trades)) * 100.0, 1) if weekly_trades else None,
            },
            "unresolved_trade_rows": unresolved_trade_rows[:12],
            "audited_trade_rows": audited_trade_rows[:12],
            "advice_effectiveness_rows": advice_rows,
        }

    return get_or_set("dashboard_weekly_review_summary", cache_key, ttl_seconds=300.0, loader=_loader)


def _build_trade_audit_acceptance_summary(*, lang: str) -> dict:
    today = datetime.now(timezone.utc).astimezone().date()
    week_start = today - timedelta(days=6)
    week_start_iso = week_start.isoformat()
    cache_key = json.dumps({"week_start": week_start_iso, "lang": lang}, sort_keys=True, ensure_ascii=False)

    def _loader() -> dict:
        weekly_trades = [
            item
            for item in load_portfolio_trades()
            if str(item.get("trade_date") or "") >= week_start_iso
        ]
        structured_reason_count = sum(
            1
            for item in weekly_trades
            if str(item.get("reason") or "").strip() and str(item.get("reason") or "").strip() != "其他"
        )
        audited_trade_count = sum(
            1
            for item in weekly_trades
            if str(item.get("action_hint_at_exit") or "").strip() or str(item.get("action_reason_at_exit") or "").strip()
        )
        trade_count = len(weekly_trades)
        return {
            "trade_count": trade_count,
            "structured_reason_coverage_pct": round((structured_reason_count / trade_count) * 100.0, 1) if trade_count else None,
            "trade_audit_coverage_pct": round((audited_trade_count / trade_count) * 100.0, 1) if trade_count else None,
        }

    return get_or_set("dashboard_trade_audit_acceptance", cache_key, ttl_seconds=300.0, loader=_loader)


def _audit_conclusion_for_trade(item: dict, *, lang: str) -> tuple[str, str]:
    action_hint = str(item.get("action_hint_at_exit") or "").strip().lower()
    reason = str(item.get("reason") or "").strip()
    pnl_pct = float(item.get("realized_pnl_pct") or 0.0)
    post_5d = item.get("post_sell_return_5d")
    post_10d = item.get("post_sell_return_10d")

    if not action_hint and not reason:
        return (
            "缺少审计快照" if lang == "zh" else "Missing audit snapshot",
            "这笔历史卖出没有保存当时建议，暂时只能看结果，无法判断建议与执行是否一致。"
            if lang == "zh"
            else "This historical exit did not save the advice snapshot, so we can only see the outcome for now.",
        )

    if reason == "止损/风险收缩":
        if any(token in action_hint for token in ("减", "exit", "trim", "risk", "退出")):
            return (
                "建议与执行一致" if lang == "zh" else "Advice matched execution",
                "当时系统偏向风险收缩，最终也按止损/减仓思路执行。"
                if lang == "zh"
                else "The system leaned defensive and the final action followed that risk-reduction posture.",
            )
        return (
            "执行偏保守" if lang == "zh" else "Execution was more defensive",
            "系统当时没有明确要求退出，但最终按止损/风险收缩执行。"
            if lang == "zh"
            else "The system did not explicitly call for an exit, yet the final action was more defensive.",
        )

    if reason in {"止盈/保护利润", "调仓"}:
        if any(token in action_hint for token in ("持有", "watch", "观察", "monitor")):
            return (
                "执行偏积极" if lang == "zh" else "Execution was more proactive",
                "系统更偏继续观察，但最终选择了兑现利润或调仓。"
                if lang == "zh"
                else "The system leaned toward monitoring, while the final action locked gains or rebalanced earlier.",
            )
        return (
            "建议与执行接近" if lang == "zh" else "Advice roughly matched execution",
            "系统给出的建议与最终的止盈/调仓动作大体一致。"
            if lang == "zh"
            else "The system advice broadly aligned with the eventual trim or rebalance.",
        )

    if reason == "复核后卖出":
        return (
            "人工复核主导" if lang == "zh" else "Human review led the exit",
            "这笔卖出更像复核后的主观执行，适合结合当日新闻和盘面再看。"
            if lang == "zh"
            else "This exit appears review-led and should be judged alongside the day’s news and tape.",
        )

    if post_5d is not None:
        if float(post_5d) <= -3.0:
            return (
                "卖出时机较好" if lang == "zh" else "Exit timing looked good",
                "卖出后 5 日价格继续走弱，说明这次退出至少避免了后续回撤。"
                if lang == "zh"
                else "Price kept weakening over the next 5 sessions, so the exit at least avoided further drawdown.",
            )
        if float(post_5d) >= 3.0:
            return (
                "可能偏早卖出" if lang == "zh" else "Exit may have been early",
                "卖出后 5 日价格继续上行，后续可以复盘是否过早兑现或过早止损。"
                if lang == "zh"
                else "Price continued higher over the next 5 sessions, so it is worth reviewing whether the exit was early.",
            )
    if post_10d is not None and float(post_10d) <= -5.0:
        return (
            "中期退出有效" if lang == "zh" else "Medium-term exit was effective",
            "卖出后 10 日仍明显走弱，说明这次退出在中期也具有保护效果。"
            if lang == "zh"
            else "The name remained weak over the next 10 sessions, suggesting the exit helped on a medium-term basis.",
        )

    if pnl_pct >= 0:
        return (
            "结果偏正面" if lang == "zh" else "Outcome was positive",
            "当前至少以正收益结束，但还需要更多样本判断系统建议是否长期有效。"
            if lang == "zh"
            else "The trade closed with a positive result, though more samples are needed to judge long-run advice quality.",
        )
    return (
        "结果偏负面" if lang == "zh" else "Outcome was negative",
        "这笔以负收益结束，后续适合回看是否该更早执行风险控制。"
        if lang == "zh"
        else "The trade ended negatively, so it is worth reviewing whether risk control should have happened earlier.",
    )


def _build_model_run_performance_summary(
    db: Session,
    *,
    run_id: int,
    top_n: int = 10,
    max_trade_dates: int = 20,
    market: str = "ALL",
) -> dict | None:
    run = ModelRunRepository(db).get_run_by_id(run_id)
    if run is None:
        return None
    normalized_market = str(market or "ALL").upper()
    cache_key = json.dumps(
        {
            "run_id": run_id,
            "top_n": top_n,
            "max_trade_dates": max_trade_dates,
            "market": normalized_market,
            "finished_at": run.finished_at,
            "status": run.status,
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _loader() -> dict:
        def _resolved_regime_label(detail: PredictionDetail | None, score: float | None) -> str | None:
            existing = str((detail.regime_label if detail is not None else "") or "").strip()
            if existing:
                return existing
            enriched = enrich_model_output({"score": score}, lang="en") or {}
            derived = str(enriched.get("regime_label") or "").strip()
            return derived or None

        date_stmt = (
            select(Prediction.trade_date)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Prediction.model_run_id == run_id)
            .distinct()
            .order_by(Prediction.trade_date.desc())
            .limit(max_trade_dates)
        )
        if normalized_market != "ALL":
            date_stmt = date_stmt.where(Symbol.market == normalized_market)
        selected_dates = [str(value) for value in db.scalars(date_stmt).all()]
        if not selected_dates:
            return {
                "run": {
                    "id": run.id,
                    "name": run.name,
                    "market": run.market,
                    "universe": run.universe,
                    "status": run.status,
                    "created_at": run.created_at,
                    "finished_at": run.finished_at,
                },
                "windows": {3: _aggregate_window_stats([]), 5: _aggregate_window_stats([]), 10: _aggregate_window_stats([])},
                "trade_dates": 0,
                "pick_count": 0,
                "latest_trade_date": None,
                "rows": [],
            }

        stmt = (
            select(Prediction, Symbol, PredictionDetail)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Prediction.model_run_id == run_id)
            .where(Prediction.trade_date.in_(selected_dates))
            .order_by(Prediction.trade_date.desc(), Prediction.score.desc(), Symbol.ticker.asc())
        )
        if normalized_market != "ALL":
            stmt = stmt.where(Symbol.market == normalized_market)
        rows = db.execute(stmt).all()
        if not rows:
            return {
                "run": {
                    "id": run.id,
                    "name": run.name,
                    "market": run.market,
                    "universe": run.universe,
                    "status": run.status,
                    "created_at": run.created_at,
                    "finished_at": run.finished_at,
                },
                "windows": {3: _aggregate_window_stats([]), 5: _aggregate_window_stats([]), 10: _aggregate_window_stats([])},
                "trade_dates": 0,
                "pick_count": 0,
                "latest_trade_date": None,
                "rows": [],
            }

        grouped: dict[str, list[tuple[Prediction, Symbol, PredictionDetail | None]]] = {}
        for prediction, symbol, detail in rows:
            grouped.setdefault(str(prediction.trade_date), []).append((prediction, symbol, detail))
        selected_items: list[tuple[str, Prediction, Symbol, PredictionDetail | None, str, str]] = []
        tickers_by_market: dict[str, set[str]] = {}
        for trade_date in selected_dates:
            for prediction, symbol, detail in grouped.get(trade_date, [])[:top_n]:
                ticker = str(symbol.ticker or "").upper()
                market_code = str(symbol.market or run.market or "").upper() or "CN"
                selected_items.append((trade_date, prediction, symbol, detail, market_code, ticker))
                tickers_by_market.setdefault(market_code, set()).add(ticker)
        history_cache: dict[tuple[str, str], list[dict]] = {}
        for market_code, tickers in tickers_by_market.items():
            for row in load_lake_rows(markets=[market_code], tickers=tickers, limit_per_symbol=260):
                ticker = str(row.get("symbol") or "").strip().upper()
                if ticker:
                    history_cache.setdefault((market_code, ticker), []).append(row)
        for key in list(history_cache.keys()):
            history_cache[key].sort(key=lambda item: str(item.get("date") or ""))
        window_values: dict[int, list[float]] = {3: [], 5: [], 10: []}
        pick_rows: list[dict] = []
        for trade_date, prediction, symbol, detail, market_code, ticker in selected_items:
            history = history_cache.get((market_code, ticker), [])
            row_payload = {
                "trade_date": trade_date,
                "ticker": ticker,
                "name": symbol.name or ticker,
                "market": symbol.market,
                "sector": symbol.sector,
                "industry": symbol.industry,
                "sector_group": resolve_template_group_label(
                    meta={
                        "sector": symbol.sector,
                        "industry": symbol.industry,
                        "exchange": symbol.exchange,
                        "name": symbol.name,
                    },
                    ticker=ticker,
                    market_code=market_code,
                    name=symbol.name,
                ),
                "regime_label": _resolved_regime_label(detail, prediction.score),
                "score": prediction.score,
                "signal_label": detail.signal_label if detail is not None else None,
                "signal_strength": detail.signal_strength if detail is not None else None,
                "return_3d": _forward_return_from_history(history, trade_date=trade_date, sessions=3),
                "return_5d": _forward_return_from_history(history, trade_date=trade_date, sessions=5),
                "return_10d": _forward_return_from_history(history, trade_date=trade_date, sessions=10),
            }
            for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                value = row_payload.get(key)
                if value is not None:
                    window_values[window].append(float(value))
            pick_rows.append(row_payload)
        return {
            "run": {
                "id": run.id,
                "name": run.name,
                "market": run.market,
                "universe": run.universe,
                "status": run.status,
                "created_at": run.created_at,
                "finished_at": run.finished_at,
            },
            "windows": {window: _aggregate_window_stats(values) for window, values in window_values.items()},
            "trade_dates": len(selected_dates),
            "pick_count": len(pick_rows),
            "latest_trade_date": selected_dates[0] if selected_dates else None,
            "rows": pick_rows[: min(80, len(pick_rows))],
        }

    return get_or_set("dashboard_model_run_performance", cache_key, ttl_seconds=300.0, loader=_loader)


def _build_acceptance_snapshot(db: Session, *, lang: str) -> dict:
    trade_audit = _build_trade_audit_acceptance_summary(lang=lang)
    latest_model_run_id = db.scalar(
        select(ModelRun.id)
        .where(ModelRun.status == "success")
        .order_by(ModelRun.id.desc())
        .limit(1)
    )
    regime_base_stmt = (
        select(func.count())
        .select_from(PredictionDetail)
        .join(Prediction, PredictionDetail.prediction_id == Prediction.id)
    )
    if latest_model_run_id:
        regime_base_stmt = regime_base_stmt.where(Prediction.model_run_id == int(latest_model_run_id))
    total_regime_rows = db.scalar(regime_base_stmt) or 0
    missing_regime_stmt = (
        select(func.count())
        .select_from(PredictionDetail)
        .join(Prediction, PredictionDetail.prediction_id == Prediction.id)
        .where(or_(PredictionDetail.regime_label.is_(None), func.trim(PredictionDetail.regime_label) == ""))
    )
    if latest_model_run_id:
        missing_regime_stmt = missing_regime_stmt.where(Prediction.model_run_id == int(latest_model_run_id))
    missing_regime_rows = db.scalar(
        missing_regime_stmt
    ) or 0
    regime_coverage_pct = round((1 - (float(missing_regime_rows) / max(float(total_regime_rows), 1.0))) * 100.0, 1)
    ai_report = load_ai_daily_report(db=db) or {}
    market_structure = ai_report.get("market_structure") or {}
    market_recommendation_meta = ai_report.get("market_recommendations_meta") or {}
    structure_source = str(market_structure.get("source") or "").strip() or "unknown"
    if lang == "zh":
        source_label = {
            "market_heatmap_snapshot": "后台市场快照",
            "recommendation_rows": "全市场模板主题汇总",
            "unknown": "未生成",
        }.get(structure_source, structure_source)
    else:
        source_label = {
            "market_heatmap_snapshot": "Background market snapshot",
            "recommendation_rows": "Full-market template themes",
            "unknown": "Not generated",
        }.get(structure_source, structure_source)
    model_eval_status = (
        "模型评测总览已上线（含成熟度 / 分市场 / 主导板块）"
        if lang == "zh"
        else "Model evaluation overview is live (maturity / per-market / dominant sectors)"
    )
    sector_group_status = (
        "长期行业分层已统一可读板块标签"
        if lang == "zh"
        else "Historical sector slices now use readable fallback group labels"
    )
    latest_dashboard_nlp = WorkspaceSnapshotRepository(db).get_latest_snapshot(SNAPSHOT_DASHBOARD_NLP) or {}
    dashboard_nlp_payload = (latest_dashboard_nlp.get("payload") or {}) if isinstance(latest_dashboard_nlp, dict) else {}
    if not isinstance(dashboard_nlp_payload, dict):
        dashboard_nlp_payload = {}
    dashboard_nlp_meta = dashboard_nlp_payload.get("meta")
    if not isinstance(dashboard_nlp_meta, dict):
        opportunities = dashboard_nlp_payload.get("opportunities") if isinstance(dashboard_nlp_payload.get("opportunities"), list) else []
        risks = dashboard_nlp_payload.get("risks") if isinstance(dashboard_nlp_payload.get("risks"), list) else []
        dashboard_nlp_meta = summarize_news_rows(opportunities + risks)
    if not isinstance(dashboard_nlp_meta, dict):
        dashboard_nlp_meta = {}
    news_source_summary = " · ".join(
        f"{item.get('source')}({item.get('count')})"
        for item in (dashboard_nlp_meta.get("top_sources") or [])[:3]
        if item.get("source")
    )
    market_ready_status = str(market_recommendation_meta.get("status") or "").strip().lower()
    if lang == "zh":
        ai_send_check = {
            "ready": "今日候选已就绪",
            "fallback": "当前为预测降级",
            "not_ready": "今日候选未就绪",
            "empty": "当前无可用候选",
        }.get(market_ready_status, "状态未知")
    else:
        ai_send_check = {
            "ready": "Today candidates ready",
            "fallback": "Using prediction fallback",
            "not_ready": "Today candidates not ready",
            "empty": "No available candidates",
        }.get(market_ready_status, "Unknown")
    return {
        "phase_1": "可验收" if lang == "zh" else "Ready",
        "phase_2": "基本可验收" if lang == "zh" else "Mostly ready",
        "phase_3": "基本可验收" if lang == "zh" else "Mostly ready",
        "overall_signoff": "建议阶段性签收" if lang == "zh" else "Recommend milestone sign-off",
        "trade_reason_coverage_pct": trade_audit.get("structured_reason_coverage_pct"),
        "trade_audit_coverage_pct": trade_audit.get("trade_audit_coverage_pct"),
        "trade_count": int(trade_audit.get("trade_count") or 0),
        "regime_coverage_pct": regime_coverage_pct,
        "regime_total_rows": int(total_regime_rows),
        "ai_structure_source": source_label,
        "ai_structure_headline": market_structure.get("headline") or ("暂无固定结构日报" if lang == "zh" else "No structured AI report yet"),
        "ai_send_check": ai_send_check,
        "ai_send_status": market_ready_status or "unknown",
        "ai_send_note": str(market_recommendation_meta.get("note") or "").strip(),
        "model_eval_status": model_eval_status,
        "sector_group_status": sector_group_status,
        "news_coverage_pct": dashboard_nlp_meta.get("coverage_pct"),
        "news_matched_tickers": int(dashboard_nlp_meta.get("matched_ticker_count") or 0),
        "news_ticker_count": int(dashboard_nlp_meta.get("ticker_count") or 0),
        "news_headline_total": int(dashboard_nlp_meta.get("headline_total") or 0),
        "news_source_summary": news_source_summary,
        "remaining_gaps": [
            ("AI 日报横截面仍可继续增强" if lang == "zh" else "AI report breadth can still improve"),
            ("长期归因可继续深化" if lang == "zh" else "Long-horizon attribution can still deepen"),
            ("历史补录不等同于原始实时快照" if lang == "zh" else "Historical backfill is not identical to original realtime snapshots"),
        ],
    }


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
        "us_signal_train": "us_signal_train",
        "train_us_signals": "us_signal_train",
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


SCREENER_PRECOMPUTE_STAGE_CONFIG = [
    ("screener_precompute", "总控预计算", "Staged Precompute"),
    ("screener_precompute_core", "核心模型预计算", "Core Precompute"),
    ("screener_precompute_combos", "组合预计算", "Combo Precompute"),
    ("screener_precompute_rest", "补全预计算", "Rest Precompute"),
]

US_SIGNAL_TRAIN_JOB_TYPES = ("us_signal_train", "train_us_signals")


def _job_status_text(status: str | None, lang: str = "zh") -> str:
    normalized = str(status or "").strip().lower() or "idle"
    labels_zh = {
        "success": "成功",
        "failed": "失败",
        "partial": "部分完成",
        "running": "运行中",
        "enabled": "已开启",
        "disabled": "已关闭",
        "idle": "待运行",
    }
    labels_en = {
        "success": "Success",
        "failed": "Failed",
        "partial": "Partial",
        "running": "Running",
        "enabled": "Enabled",
        "disabled": "Disabled",
        "idle": "Idle",
    }
    labels = labels_zh if lang == "zh" else labels_en
    return labels.get(normalized, normalized)


def _find_latest_job_by_type(recent_jobs: list[dict], job_type: str | list[str] | tuple[str, ...]) -> dict | None:
    if isinstance(job_type, (list, tuple, set)):
        keys = {str(item or "").strip().lower() for item in job_type if str(item or "").strip()}
    else:
        keys = {str(job_type or "").strip().lower()}
    return next((item for item in recent_jobs if str(item.get("job_type") or "").strip().lower() in keys), None)


def _summarize_screener_precompute_job(job: dict | None, *, lang: str = "zh") -> dict:
    if not isinstance(job, dict):
        return {
            "summary": "待运行" if lang == "zh" else "Pending",
            "detail": "还没有任务记录。" if lang == "zh" else "No job record yet.",
            "status": "idle",
        }
    status = str(job.get("status") or "").strip().lower() or "idle"
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    created = list(result.get("snapshots_created") or [])
    failed_templates = list(result.get("failed_templates") or [])
    failed_presets = list(result.get("failed_presets") or [])
    failed_total = int(result.get("failed_count") or 0)
    total = int(result.get("count") or 0) + failed_total
    tail_jobs_scheduled = bool(result.get("tail_jobs_scheduled"))
    message_text = str(job.get("message") or "").strip()
    if not tail_jobs_scheduled and "tail phases" in message_text.lower():
        tail_jobs_scheduled = True
    depends_on = [str(item) for item in (job.get("depends_on") or []) if str(item).strip()]
    pipeline_step = str(job.get("pipeline_step") or "").strip()
    if tail_jobs_scheduled and status == "success":
        summary = "核心已完成" if lang == "zh" else "Core Done"
    elif total > 0:
        summary = (
            f"{int(result.get('count') or 0)}/{total} 完成"
            if lang == "zh"
            else f"{int(result.get('count') or 0)}/{total} completed"
        )
    elif status == "running":
        summary = "运行中" if lang == "zh" else "Running"
    elif status == "success":
        summary = "成功" if lang == "zh" else "Success"
    else:
        summary = "待运行" if lang == "zh" else "Pending"
    detail_parts: list[str] = []
    if created:
        if created[0].get("preset_key"):
            created_names = [str(item.get("preset_label") or item.get("preset_key") or "-") for item in created[:3]]
        else:
            created_names = [str(item.get("model_template") or "-") for item in created[:3]]
        detail_parts.append(
            (
                f"已生成 {len(created)} 项：{', '.join(created_names)}"
                if lang == "zh"
                else f"Generated {len(created)} item(s): {', '.join(created_names)}"
            )
        )
    failed_items = failed_templates + failed_presets
    if failed_items:
        labels = [
            str(item.get("preset_label") or item.get("preset_key") or item.get("model_template") or "-")
            for item in failed_items[:3]
        ]
        detail_parts.append(
            (
                f"失败 {len(failed_items)} 项：{', '.join(labels)}"
                if lang == "zh"
                else f"Failed {len(failed_items)} item(s): {', '.join(labels)}"
            )
        )
    if pipeline_step:
        detail_parts.append(
            (f"阶段：{pipeline_step}" if lang == "zh" else f"Step: {pipeline_step}")
        )
    if tail_jobs_scheduled:
        detail_parts.append(
            "组合/补全已转后台继续" if lang == "zh" else "Combo/rest continue in background"
        )
    if depends_on:
        detail_parts.append(
            (
                f"依赖：{', '.join(depends_on)}"
                if lang == "zh"
                else f"Depends on: {', '.join(depends_on)}"
            )
        )
    if not detail_parts:
        detail_parts.append(job.get("message") or ("暂无附加说明" if lang == "zh" else "No additional note"))
    return {
        "summary": summary,
        "detail": " · ".join(detail_parts),
        "status": status,
    }


def _build_screener_precompute_stage_rows(recent_jobs: list[dict], *, lang: str = "zh") -> list[dict]:
    rows: list[dict] = []
    parent_job = _find_latest_job_by_type(recent_jobs, "screener_precompute")
    for job_type, label_zh, label_en in SCREENER_PRECOMPUTE_STAGE_CONFIG:
        job = _find_latest_job_by_type(recent_jobs, job_type)
        if job is None and job_type == "screener_precompute_core" and isinstance(parent_job, dict):
            parent_summary = _summarize_screener_precompute_job(parent_job, lang=lang)
            if str(parent_summary.get("summary") or "") in {"核心已完成", "Core Done"}:
                job = {
                    "job_type": job_type,
                    "status": "success",
                    "message": "Inherited core completion from parent staged precompute job.",
                    "result": {"count": 1},
                }
        summary = _summarize_screener_precompute_job(job, lang=lang)
        rows.append(
            {
                "job_type": job_type,
                "label": label_zh if lang == "zh" else label_en,
                "job": job,
                "summary": summary["summary"],
                "detail": summary["detail"],
                "status": summary["status"],
            }
        )
    return rows


def _render_screener_precompute_action_forms(
    *,
    lang: str = "zh",
    redirect_to: str,
    compact: bool = False,
    stage_rows: list[dict] | None = None,
) -> str:
    labels = {
        "run_all": "运行整条预计算链" if lang == "zh" else "Run Full Precompute Chain",
        "run_core": "只跑核心" if lang == "zh" else "Run Core Only",
        "run_combos": "只跑组合" if lang == "zh" else "Run Combos Only",
        "run_rest": "只跑补全" if lang == "zh" else "Run Rest Only",
    }
    actions = [
        ("/jobs/precompute-cn-screeners", labels["run_all"], True, "screener_precompute"),
        ("/jobs/precompute-cn-screeners-core", labels["run_core"], False, "screener_precompute_core"),
        ("/jobs/precompute-cn-screeners-combos", labels["run_combos"], False, "screener_precompute_combos"),
        ("/jobs/precompute-cn-screeners-rest", labels["run_rest"], False, "screener_precompute_rest"),
    ]
    stage_map = {str(item.get("job_type") or ""): item for item in (stage_rows or [])}
    forms = "".join(
        "<div class='action-with-note'>"
        "<form action='{action}' method='post' class='inline-form'>"
        "<input type='hidden' name='redirect_to' value='{redirect_to}' />"
        "<button class='{button_class}' type='submit'>{label}</button>"
        "</form>"
        "<div class='subtle action-receipt'>{receipt}</div>"
        "</div>".format(
            action=action,
            redirect_to=html.escape(redirect_to, quote=True),
            button_class=("cta compact primary" if compact and is_primary else "cta compact" if compact else "cta primary" if is_primary else "cta"),
            label=html.escape(label),
            receipt=html.escape(
                (
                    f"最近：{stage_map.get(job_type, {}).get('summary') or ('待运行' if lang == 'zh' else 'Pending')}"
                    + " · "
                    + (stage_map.get(job_type, {}).get("detail") or ("还没有任务记录。" if lang == "zh" else "No job record yet."))
                )
                if lang == "zh"
                else (
                    f"Latest: {stage_map.get(job_type, {}).get('summary') or 'Pending'}"
                    + " · "
                    + (stage_map.get(job_type, {}).get("detail") or "No job record yet.")
                )
            ),
        )
        for action, label, is_primary, job_type in actions
    )
    note = (
        "组合预计算依赖核心快照；如果组合失败，先补跑核心。"
        if lang == "zh"
        else "Combo precompute depends on core snapshots; rerun core first if combos fail."
    )
    return forms + f"<div class='subtle precompute-note'>{html.escape(note)}</div>"


def _render_model_selection_guidance_action_form(
    *,
    lang: str = "zh",
    redirect_to: str,
    job: dict | None = None,
    compact: bool = False,
) -> str:
    status = str((job or {}).get("status") or "idle").strip().lower()
    if lang == "zh":
        status_label = {
            "success": "成功",
            "failed": "失败",
            "partial": "部分完成",
            "running": "运行中",
            "idle": "待运行",
        }.get(status, status or "待运行")
        receipt = f"最近：{status_label} · {((job or {}).get('message') or '还没有任务记录。')}"
        note = "这条 job 会把今日优先模型、优先组合和强票反向归因写入快照，供首页、模型评测和 AI 日报直接读取。"
        label = "刷新模型使用指导"
    else:
        status_label = {
            "success": "Success",
            "failed": "Failed",
            "partial": "Partial",
            "running": "Running",
            "idle": "Pending",
        }.get(status, status or "Pending")
        receipt = f"Latest: {status_label} · {((job or {}).get('message') or 'No job record yet.')}"
        note = "This job persists the priority model, priority combo, and winner traceback snapshot for Dashboard, Model Performance, and AI Daily Report."
        label = "Refresh Model Guidance"
    return (
        "<div class='action-with-note'>"
        f"<form action='/jobs/model-selection-guidance-snapshot' method='post' class='inline-form'>"
        f"<input type='hidden' name='redirect_to' value='{html.escape(redirect_to, quote=True)}' />"
        "<input type='hidden' name='markets' value='CN' />"
        f"<button class='{('cta compact' if compact else 'cta')}' type='submit'>{html.escape(label)}</button>"
        "</form>"
        f"<div class='subtle action-receipt'>{html.escape(receipt)}</div>"
        f"<div class='subtle precompute-note'>{html.escape(note)}</div>"
        "</div>"
    )


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
    all_signal_rows = list(model_candidate_rows or []) + list(summary.get("latest_signals") or [])
    latest_trade_day = max(
        (str(item.get("trade_date") or item.get("as_of_date") or "") for item in all_signal_rows if item.get("trade_date") or item.get("as_of_date")),
        default="-",
    )
    core_precompute_job = _find_latest_job_by_type(recent_jobs, "screener_precompute_core") or _find_latest_job_by_type(recent_jobs, "screener_precompute")
    core_precompute_summary = _summarize_screener_precompute_job(core_precompute_job, lang=lang)
    ai_report_job = _find_latest_job_by_type(recent_jobs, ("send_ai_daily_report", "watchlist_auto_analysis"))
    latest_model_day = _display_time(latest_model.get("finished_at") or latest_model.get("created_at"))
    readiness_items = [
        {
            "label": "最新交易日" if lang == "zh" else "Latest Trading Day",
            "value": latest_trade_day,
            "status": "success" if latest_trade_day != "-" else "idle",
            "detail": "来自最新模型候选/信号快照。" if lang == "zh" else "From the latest candidate/signal snapshot.",
        },
        {
            "label": "最新模型训练日" if lang == "zh" else "Latest Training Date",
            "value": latest_model_day,
            "status": str(latest_model.get("status") or "idle").lower(),
            "detail": latest_model.get("name") or ("尚未训练" if lang == "zh" else "No model run yet."),
        },
        {
            "label": "核心预计算状态" if lang == "zh" else "Core Precompute",
            "value": _job_status_text(core_precompute_summary.get("status"), lang=lang),
            "status": str(core_precompute_summary.get("status") or "idle").lower(),
            "detail": core_precompute_summary.get("detail") or ("核心快照待运行。" if lang == "zh" else "Core snapshot is pending."),
        },
        {
            "label": "AI 日报状态" if lang == "zh" else "AI Report",
            "value": _job_status_text((ai_report_job or {}).get("status"), lang=lang),
            "status": str((ai_report_job or {}).get("status") or "idle").lower(),
            "detail": _display_time((ai_report_job or {}).get("finished_at") or (ai_report_job or {}).get("started_at")),
        },
    ]
    readiness_html = "".join(
        (
            "<article class='readiness-card'>"
            f"<div class='readiness-top'><span>{html.escape(item['label'])}</span><span class='job-status {html.escape(item['status'])}'>{_job_status_text(item['status'], lang=lang)}</span></div>"
            f"<div class='readiness-value' title='{html.escape(str(item['value']))}'>{html.escape(_compact_label(str(item['value']), 30))}</div>"
            f"<div class='subtle'>{html.escape(str(item['detail'] or '-'))}</div>"
            "</article>"
        )
        for item in readiness_items
    )
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
    lightgbm_home_eval = build_lightgbm_prediction_evaluation(market="ALL", recent_runs=8, top_n=40)
    lightgbm_home_windows = lightgbm_home_eval.get("windows") or {}
    lightgbm_home_sample_count = int(lightgbm_home_eval.get("sample_count") or 0)
    lightgbm_home_ranked = sorted(
        [
            (
                int(((lightgbm_home_windows.get("breakout") or {}).get(1) or {}).get("count") or 0),
                float(((lightgbm_home_windows.get("breakout") or {}).get(1) or {}).get("hit_rate") or 0.0),
                "breakout",
            ),
            (
                int(((lightgbm_home_windows.get("pullback") or {}).get(1) or {}).get("count") or 0),
                float(((lightgbm_home_windows.get("pullback") or {}).get(1) or {}).get("hit_rate") or 0.0),
                "pullback",
            ),
            (
                int(((lightgbm_home_windows.get("watch") or {}).get(1) or {}).get("count") or 0),
                float(((lightgbm_home_windows.get("watch") or {}).get(1) or {}).get("hit_rate") or 0.0),
                "watch",
            ),
        ],
        key=lambda item: (-item[0], -item[1], item[2]),
    )
    lightgbm_home_count, lightgbm_home_hit, lightgbm_home_key = lightgbm_home_ranked[0]
    if lightgbm_home_sample_count <= 0 or lightgbm_home_count <= 0:
        lightgbm_home_bias_title = "LightGBM：先观察" if lang == "zh" else "LightGBM: Observe First"
        lightgbm_home_bias_text = (
            "当前还没有足够成熟的次日样本，先把 LightGBM 当作观察面板。"
            if lang == "zh"
            else "There are not enough mature next-day samples yet, so treat LightGBM as an observation panel."
        )
        lightgbm_home_bias_style = "background:#f8fafc;border-color:#dbe4ee;color:#334155;"
    elif lightgbm_home_key == "breakout":
        lightgbm_home_bias_title = "LightGBM：今天更偏突破确认" if lang == "zh" else "LightGBM: Lean Breakout Today"
        lightgbm_home_bias_text = (
            f"优先看放量突破的名字；同类 1D 命中率 {lightgbm_home_hit:.1f}%。"
            if lang == "zh"
            else f"Prioritize names with cleaner breakout confirmation; peer 1D hit rate {lightgbm_home_hit:.1f}%."
        )
        lightgbm_home_bias_style = "background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8;"
    elif lightgbm_home_key == "pullback":
        lightgbm_home_bias_title = "LightGBM：今天更偏回踩布局" if lang == "zh" else "LightGBM: Lean Pullbacks Today"
        lightgbm_home_bias_text = (
            f"优先看回踩企稳的名字；同类 1D 命中率 {lightgbm_home_hit:.1f}%。"
            if lang == "zh"
            else f"Prioritize names resetting into support; peer 1D hit rate {lightgbm_home_hit:.1f}%."
        )
        lightgbm_home_bias_style = "background:#ecfdf5;border-color:#a7f3d0;color:#047857;"
    else:
        lightgbm_home_bias_title = "LightGBM：今天先观察" if lang == "zh" else "LightGBM: Watch First"
        lightgbm_home_bias_text = (
            f"当前 Watch 信号更占优，先把它当观察名单；同类 1D 命中率 {lightgbm_home_hit:.1f}%。"
            if lang == "zh"
            else f"Watch signals currently lead, so treat it as a monitored list first; peer 1D hit rate {lightgbm_home_hit:.1f}%."
        )
        lightgbm_home_bias_style = "background:#fff7ed;border-color:#fed7aa;color:#c2410c;"
    lightgbm_home_bias_html = (
        f"<article class='signal-row' style='{lightgbm_home_bias_style}border:1px solid;border-radius:16px;'>"
        f"<div><div class='ticker'>{html.escape(lightgbm_home_bias_title)}</div><div class='subtle' style='color:inherit;opacity:0.9;'>{html.escape(lightgbm_home_bias_text)}</div></div>"
        f"<div class='row-right'><a class='cta' href='/screeners?lang={lang}&model_template=lightgbm_top_picks&market=CN&universe=full_market&run=1'>{'打开 LightGBM' if lang == 'zh' else 'Open LightGBM'}</a></div>"
        "</article>"
    )
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
    nlp_meta = (nlp_payload.get("meta") or {}) if isinstance(nlp_payload, dict) else {}
    nlp_top_sources = " · ".join(
        f"{item.get('source')}({item.get('count')})"
        for item in (nlp_meta.get("top_sources") or [])[:3]
        if item.get("source")
    )
    nlp_meta_text = (
        (
            f"命中 {nlp_meta.get('matched_ticker_count', 0)}/{nlp_meta.get('ticker_count', 0)} 只，"
            f"累计 {nlp_meta.get('headline_total', 0)} 条新闻，覆盖率 {nlp_meta.get('coverage_pct', 0)}%。"
        )
        if lang == "zh"
        else (
            f"Matched {nlp_meta.get('matched_ticker_count', 0)}/{nlp_meta.get('ticker_count', 0)} names, "
            f"{nlp_meta.get('headline_total', 0)} headlines, {nlp_meta.get('coverage_pct', 0)}% coverage."
        )
    ) if nlp_meta else (
        "当前还没有可用的新闻命中统计。" if lang == "zh" else "No usable news coverage stats yet."
    )
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
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('execution_note') or item.get('entry_trigger') or '-'}</div>"
        + (f"<div class='subtle' style='font-weight:800;color:#f59e0b;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div class='row-right'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='mini-metric'>{(str(item.get('target_weight_pct')) + '%') if item.get('target_weight_pct') is not None else '-'}</div></div>"
        "</article>"
        for item in signal_sets["actionable"][:3]
    ) or f"<div class='empty'>{'暂无可执行候选' if lang == 'zh' else 'No actionable candidates yet'}</div>"
    blocked_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{_reason_screen_link(format_trade_gate_reason(item.get('block_reason'), lang=lang), reason=item.get('block_reason'), status=item.get('status_label'), market=item.get('market'), lang=lang)}</div><div class='subtle'>{_reason_screen_link('查看同类筛选' if lang == 'zh' else 'Open screener', reason=item.get('block_reason'), status=item.get('status_label'), market=item.get('market'), lang=lang)}</div></div>"
        f"<div class='row-right'><span class='signal {item.get('status_tone')}'>{html.escape(format_trade_status(item.get('status_label'), lang=lang))}</span><div class='mini-metric'>{html.escape(format_risk_flags(item.get('risk_flags') or [], lang=lang))}</div></div>"
        "</article>"
        for item in signal_sets["blocked"][:3]
    ) or f"<div class='empty'>{'暂无受阻候选' if lang == 'zh' else 'No blocked candidates'}</div>"
    news_opportunity_rows = sorted(
        [
            item
            for item in (nlp_payload.get("opportunities") or [])
            if float(item.get("sentiment_score") or 0.0) > 0
        ],
        key=lambda item: (
            -float(item.get("sentiment_score") or 0.0),
            -int(item.get("headline_count") or 0),
            item.get("ticker") or "",
        ),
    )[:3]
    news_risk_rows = sorted(
        [
            item
            for item in (nlp_payload.get("risks") or [])
            if float(item.get("sentiment_score") or 0.0) < 0
            or ((item.get("risk_tags") or []) and float(item.get("sentiment_score") or 0.0) <= 0)
        ],
        key=lambda item: (
            0 if float(item.get("sentiment_score") or 0.0) < 0 else 1,
            float(item.get("sentiment_score") or 0.0),
            -len(item.get("risk_tags") or []),
            -int(item.get("headline_count") or 0),
            item.get("ticker") or "",
        ),
    )[:3]
    def _news_market_section(title: str, rows: list[dict], *, risk_mode: bool = False) -> str:
        market_meta = summarize_news_rows(rows)
        source_text = " · ".join(
            f"{item.get('source')}({item.get('count')})"
            for item in (market_meta.get("top_sources") or [])[:2]
            if item.get("source")
        )
        meta_text = (
            (
                f"命中 {market_meta.get('matched_ticker_count', 0)}/{market_meta.get('ticker_count', 0)} 只，"
                f"{market_meta.get('headline_total', 0)} 条新闻，覆盖率 {market_meta.get('coverage_pct', 0)}%。"
            )
            if lang == "zh"
            else (
                f"Matched {market_meta.get('matched_ticker_count', 0)}/{market_meta.get('ticker_count', 0)} names, "
                f"{market_meta.get('headline_total', 0)} headlines, {market_meta.get('coverage_pct', 0)}% coverage."
            )
        ) if rows else ("暂无命中统计。" if lang == "zh" else "No coverage stats yet.")
        body = "".join(
            "<article class='signal-row'>"
            f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('summary_text') or '-'}</div></div>"
            + (
                f"<div class='row-right'><span class='signal sig-sell'>{('风险' if lang == 'zh' else 'risk') if float(item.get('sentiment_score') or 0.0) == 0 and (item.get('risk_tags') or []) else (item.get('sentiment_label') or '-')}</span><div class='mini-metric'>{' / '.join(item.get('risk_tags') or []) or '-'}</div></div>"
                if risk_mode
                else f"<div class='row-right'><span class='signal sig-buy'>{item.get('sentiment_label') or '-'}</span><div class='mini-metric'>{item.get('headline_count') or 0}</div></div>"
            )
            + "</article>"
            for item in rows
        ) or f"<div class='empty'>{'暂无相关快照' if lang == 'zh' else 'No matching items yet'}</div>"
        return (
            "<section class='news-market-block'>"
            + f"<div class='news-market-title'>{title}</div>"
            + f"<div class='subtle'>{meta_text}</div>"
            + (f"<div class='subtle'>{('来源' if lang == 'zh' else 'Sources')}: {source_text}</div>" if source_text else "")
            + f"<div class='list-stack'>{body}</div>"
            + "</section>"
        )

    news_opportunity_cn = [item for item in news_opportunity_rows if str(item.get("market") or "").upper() == "CN"]
    news_opportunity_us = [item for item in news_opportunity_rows if str(item.get("market") or "").upper() == "US"]
    news_risk_cn = [item for item in news_risk_rows if str(item.get("market") or "").upper() == "CN"]
    news_risk_us = [item for item in news_risk_rows if str(item.get("market") or "").upper() == "US"]
    news_opportunities_html = "".join(
        [
            _news_market_section("A股 / CN", news_opportunity_cn),
            _news_market_section("美股 / US", news_opportunity_us),
        ]
    ) if news_opportunity_rows else f"<div class='empty'>{'暂无新闻快照' if lang == 'zh' else 'No news snapshot yet'}</div>"
    news_risks_html = "".join(
        [
            _news_market_section("A股 / CN", news_risk_cn, risk_mode=True),
            _news_market_section("美股 / US", news_risk_us, risk_mode=True),
        ]
    ) if news_risk_rows else f"<div class='empty'>{'暂无新闻风险快照' if lang == 'zh' else 'No news risk snapshot yet'}</div>"
    news_monitor_html = f"""
                <article class="card compact-card">
                  <div class="panel-head compact-head">
                    <div>
                      <div class="eyebrow">{'新闻监控' if lang == 'zh' else 'News Monitor'}</div>
                      <h3>{'新闻详情已移到自选股' if lang == 'zh' else 'News details moved to Watchlist'}</h3>
                      <p>{nlp_meta_text}</p>
                    </div>
                    <div class="row-right">
                      <span class="signal {'sig-sell' if int(nlp_meta.get('negative_total') or 0) else 'sig-watch'}">{('风险 ' if lang == 'zh' else 'Risk ') + str(nlp_meta.get('negative_total', 0))}</span>
                      <div class="mini-metric">{_fmt_optional_float(nlp_meta.get('coverage_pct'), suffix='%', digits=1)}</div>
                    </div>
                  </div>
                  <div class="cta-row">
                    <a class="cta primary" href="/watchlist?lang={lang}&news_view=risk#news">{'查看新闻风险' if lang == 'zh' else 'Review news risks'}</a>
                    <a class="cta" href="/watchlist?lang={lang}&news_view=opportunity#news">{'新闻机会' if lang == 'zh' else 'News opportunities'}</a>
                    <a class="cta" href="/dashboard/ops?lang={lang}">{'覆盖诊断' if lang == 'zh' else 'Coverage diagnostics'}</a>
                  </div>
                </article>
    """
    close_review_actionable_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('entry_trigger') or item.get('execution_note') or '-'}</div>"
        + (f"<div class='subtle' style='font-weight:800;color:#f59e0b;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div class='row-right'><span class='signal sig-buy'>{item.get('tradability_status') or '-'}</span><div class='mini-metric'>{item.get('target_weight') or '-'}</div></div>"
        "</article>"
        for item in (close_review_action_feed.get("actionable") or [])[:3]
    ) or f"<div class='empty'>{'暂无主攻候选' if lang == 'zh' else 'No primary action candidates yet'}</div>"
    close_review_watch_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('execution_note') or item.get('block_reason') or '-'}</div>"
        + (f"<div class='subtle' style='font-weight:800;color:#f59e0b;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div class='row-right'><span class='signal sig-watch'>{item.get('tradability_status') or '-'}</span><div class='mini-metric'>{item.get('target_weight') or '-'}</div></div>"
        "</article>"
        for item in (close_review_action_feed.get("blocked") or [])[:3]
    ) or f"<div class='empty'>{'暂无只观察名单' if lang == 'zh' else 'No watch-only names yet'}</div>"
    close_review_risk_reduce_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('invalidation_condition') or item.get('execution_note') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal sig-sell'>{item.get('tradability_status') or '-'}</span><div class='mini-metric'>{item.get('target_weight') or '-'}</div></div>"
        "</article>"
        for item in (close_review_action_feed.get("risk_reduction") or [])[:3]
    ) or f"<div class='empty'>{'暂无减仓处理名单' if lang == 'zh' else 'No risk-reduction queue yet'}</div>"
    close_review_action_html = f"""
      <div>
        <div class="subtle" style="font-weight:700;margin-bottom:6px;">{'明日主攻' if lang == 'zh' else 'Primary Action'}</div>
        <div class="list-stack">{close_review_actionable_html}</div>
      </div>
      <div>
        <div class="subtle" style="font-weight:700;margin:12px 0 6px;">{'只观察' if lang == 'zh' else 'Watch Only'}</div>
        <div class="list-stack">{close_review_watch_html}</div>
      </div>
      <div>
        <div class="subtle" style="font-weight:700;margin:12px 0 6px;">{'减仓处理' if lang == 'zh' else 'Reduce Risk'}</div>
        <div class="list-stack">{close_review_risk_reduce_html}</div>
      </div>
    """
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
          {WORKSPACE_COMPACT_STYLE}
          {WORKSPACE_SIDEBAR_STYLE}
          .brand {{ margin-bottom:28px; }}
          .content {{ padding:20px 18px 28px; }}
          .topbar {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; flex-wrap:wrap; margin-bottom:14px; }}
          .hero h2 {{ margin:0 0 8px; font-size:32px; line-height:1.04; max-width:760px; }}
          .hero p {{ margin:0; color:var(--muted); font-size:14px; max-width:720px; }}
          .top-actions {{ display:flex; gap:10px; flex-wrap:wrap; }}
          .top-pill {{
            display:inline-flex; align-items:center; justify-content:center;
            min-height:38px; padding:0 14px; border-radius:999px; border:1px solid var(--line);
            background:rgba(17,28,40,0.72); color:var(--muted); font-weight:700; font-size:13px;
          }}
          .top-pill.active {{ color:var(--ink); border-color:rgba(82,168,255,0.35); background:rgba(82,168,255,0.16); }}
          .banner {{ margin-bottom:12px; padding:12px 14px; border-radius:14px; background:#172534; border:1px solid var(--line); }}
          .readiness-grid {{ display:grid; gap:12px; grid-template-columns:repeat(4, minmax(0, 1fr)); margin-bottom:12px; }}
          .readiness-card {{
            padding:14px 15px;
            border-radius:18px;
            border:1px solid rgba(61,217,182,0.16);
            background:
              linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94));
            box-shadow:0 12px 28px rgba(15,23,42,0.12);
            min-width:0;
          }}
          .readiness-top {{ display:flex; align-items:center; justify-content:space-between; gap:8px; color:var(--muted); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; }}
          .readiness-value {{ margin-top:10px; color:var(--ink); font-size:18px; font-weight:900; line-height:1.25; word-break:break-word; overflow-wrap:anywhere; }}
          .summary-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); margin-bottom:12px; }}
          .metric {{ font-size:26px; font-weight:800; line-height:1; margin:0 0 6px; }}
          .metric.metric-compact {{ font-size:18px; line-height:1.25; word-break:break-word; overflow-wrap:anywhere; }}
          .muted {{ color:var(--muted); font-size:13px; line-height:1.5; }}
          .workspace {{ display:grid; gap:12px; grid-template-columns:minmax(0, 1.35fr) minmax(320px, 0.82fr); }}
          .stack {{ display:grid; gap:12px; }}
          .panel-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:10px; }}
          .compact-card {{ padding:16px; }}
          .compact-head {{ margin-bottom:6px; align-items:center; }}
          .panel-head h3 {{ margin:0; font-size:20px; }}
          .panel-head p {{ margin:6px 0 0; color:var(--muted); font-size:13px; }}
          .list-stack {{ display:grid; gap:10px; }}
          .list-row, .signal-row, .job-row {{
            display:flex; justify-content:space-between; gap:12px; align-items:center;
            padding:11px; border-radius:14px; background:rgba(11,19,29,0.82); border:1px solid rgba(34,50,70,0.92);
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
          .news-market-block {{ display:grid; gap:10px; margin-bottom:12px; }}
          .news-market-block:last-child {{ margin-bottom:0; }}
          .news-market-title {{ font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; color:var(--muted); }}
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
          .job-status.idle {{ background:rgba(144,163,184,0.14); color:#c0cfde; }}
          .job-status.unknown {{ background:rgba(144,163,184,0.14); color:#c0cfde; }}
          .job-type {{ font-weight:700; font-size:13px; }}
          .empty {{ padding:18px; border-radius:16px; background:rgba(11,19,29,0.65); border:1px dashed var(--line); color:var(--muted); font-size:13px; }}
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
            .workspace, .summary-grid, .readiness-grid {{ grid-template-columns:1fr; }}
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

            <section class="readiness-grid">{readiness_html}</section>

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
                  {lightgbm_home_bias_html}
                  <div class="list-stack">{top_signal_html}</div>
                  <div class="cta-row">
                    <a class="cta primary" href="/screeners?lang={lang}">{'进入模型选股' if lang == 'zh' else 'Open screeners'}</a>
                    <a class="cta" href="/dashboard/continuous-leaders?lang={lang}&lookback_runs={lookback_runs}">{'连续强势' if lang == 'zh' else 'Continuous leaders'}</a>
                    <a class="cta" href="/dashboard/model-performance?lang={lang}">{'模型评测总览' if lang == 'zh' else 'Model Evaluation Overview'}</a>
                  </div>
                </article>
                {news_monitor_html}

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
    turnover_ratio_values: list[float] = []
    advancing = 0
    declining = 0
    up_turnover = 0.0
    down_turnover = 0.0
    flat_turnover = 0.0
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
        if len(history) >= 2:
            latest = history[-1]
            prev = history[-2]
            latest_close = latest.get("close")
            latest_volume = latest.get("volume")
            prev_close = prev.get("close")
            latest_turnover = (
                max(0.0, float(latest_close) * float(latest_volume))
                if latest_close not in (None, 0) and latest_volume not in (None, 0)
                else 0.0
            )
            if latest_turnover > 0:
                prior_turnovers = [
                    max(0.0, float(row_close) * float(row_volume))
                    for row in history[:-1]
                    for row_close, row_volume in [(row.get("close"), row.get("volume"))]
                    if row_close not in (None, 0) and row_volume not in (None, 0)
                ][-20:]
                if prior_turnovers:
                    turnover_ratio_values.append(latest_turnover / max(sum(prior_turnovers) / len(prior_turnovers), 1.0))
                if prev_close not in (None, 0):
                    if float(latest_close) > float(prev_close):
                        up_turnover += latest_turnover
                    elif float(latest_close) < float(prev_close):
                        down_turnover += latest_turnover
                    else:
                        flat_turnover += latest_turnover
    breadth_base = advancing + declining
    breadth = round((advancing / breadth_base) * 100.0, 1) if breadth_base else None
    total_turnover = up_turnover + down_turnover + flat_turnover
    turnover_ratio_20d = round(sum(turnover_ratio_values) / len(turnover_ratio_values), 2) if turnover_ratio_values else None
    up_turnover_share_pct = round((up_turnover / total_turnover) * 100.0, 1) if total_turnover else None
    signed_turnover_pct = round(((up_turnover - down_turnover) / total_turnover) * 100.0, 1) if total_turnover else None
    flow_proxy_score = None
    if turnover_ratio_20d is not None or signed_turnover_pct is not None or breadth is not None:
        ratio_component = ((turnover_ratio_20d or 1.0) - 1.0) * 26.0
        signed_component = (signed_turnover_pct or 0.0) * 0.30
        breadth_component = ((breadth or 50.0) - 50.0) * 0.32
        flow_proxy_score = round(min(100.0, max(0.0, 50.0 + ratio_component + signed_component + breadth_component)), 1)
    return {
        "avg_move_5d": round(sum(five_day_values) / len(five_day_values), 2) if five_day_values else None,
        "avg_move_20d": round(sum(twenty_day_values) / len(twenty_day_values), 2) if twenty_day_values else None,
        "breadth_pct": breadth,
        "turnover_ratio_20d": turnover_ratio_20d,
        "up_turnover_share_pct": up_turnover_share_pct,
        "signed_turnover_pct": signed_turnover_pct,
        "flow_proxy_score": flow_proxy_score,
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
                    "turnover_ratio_20d": item.get("turnover_ratio_20d"),
                    "up_turnover_share_pct": item.get("up_turnover_share_pct"),
                    "signed_turnover_pct": item.get("signed_turnover_pct"),
                    "flow_proxy_score": item.get("flow_proxy_score"),
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
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
          @media (max-width:1120px) {{ .metrics-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .workspace-grid, .hero {{ grid-template-columns:1fr; }} }}
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

    def _sync_state_chip(value: str | None) -> str:
        text = str(value or "-").strip() or "-"
        lowered = text.lower()
        bg = "#eef2f7"
        fg = "#425466"
        if any(token in lowered for token in ("success", "ready", "ok", "done", "completed", "成功")):
            bg, fg = "#dcfce7", "#166534"
        elif any(token in lowered for token in ("fail", "error", "timeout", "failed", "terminated", "失败")):
            bg, fg = "#fee2e2", "#991b1b"
        elif any(token in lowered for token in ("running", "pending", "wait", "queued", "partial", "等待", "进行")):
            bg, fg = "#dbeafe", "#1d4ed8"
        return (
            "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
            f"background:{bg};color:{fg};font-weight:800;font-size:12px;white-space:nowrap;'>{html.escape(text)}</span>"
        )

    symbol_rows = "".join(
        f"<tr><td><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td><td title='{item['name'] or item['ticker']}'>{_compact_label(item['name'] or item['ticker'], 20)}</td><td>{item['provider'] or '-'}</td><td>{_sync_state_chip(item['status'])}</td><td>{item['last_synced_date'] or '-'}</td><td class='message-cell' title='{item['message'] or '-'}'>{_compact_label(item['message'] or '-', 56)}</td></tr>"
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
          .wrap {{ max-width:1108px; margin:0 auto; padding:28px 18px 52px; }}
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
          .sync-table th:nth-child(1), .sync-table td:nth-child(1) {{
            position:sticky;
            left:0;
            z-index:3;
            min-width:110px;
            background:var(--panel);
            box-shadow:8px 0 16px rgba(31,41,55,0.06);
          }}
          .sync-table th:nth-child(2), .sync-table td:nth-child(2) {{
            position:sticky;
            left:110px;
            z-index:3;
            min-width:150px;
            background:var(--panel);
            box-shadow:8px 0 16px rgba(31,41,55,0.04);
          }}
          .sync-table th:nth-child(1), .sync-table th:nth-child(2) {{ z-index:4; }}
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
              <table class="sync-table">
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; min-width:0; }}
          .wrap {{ max-width:none; margin:0; }}
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:none; margin:0; }}
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


@router.get("/model-performance", response_class=HTMLResponse)
def dashboard_model_performance(
    request: Request,
    run_id: int | None = None,
    market: str = "CN",
    top_n: int = 10,
    max_trade_dates: int = 20,
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/model-performance")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    run_repo = ModelRunRepository(db)
    recent_runs = [item for item in run_repo.list_recent_runs(limit=8) if str(item.get("status") or "").lower() == "success"]
    aggregate_runs = [item for item in run_repo.list_recent_runs(limit=8) if str(item.get("status") or "").lower() == "success"]
    aggregate_by_model: dict[tuple[str, str], dict] = {}
    for item in aggregate_runs:
        model_key = (str(item.get("name") or "-"), str(item.get("market") or "-"))
        summary = _build_model_run_performance_summary(
            db,
            run_id=int(item["id"]),
            top_n=max(1, int(top_n)),
            max_trade_dates=max(5, int(max_trade_dates)),
            market=market,
        )
        windows = (summary or {}).get("windows") or {}
        aggregate = aggregate_by_model.setdefault(
            model_key,
            {
                "name": model_key[0],
                "market": model_key[1],
                "runs": 0,
                "latest_trade_date": None,
                "trade_dates_covered": 0,
                "sample_count": 0,
                "window_sums": {
                    3: {"weighted_return": 0.0, "count": 0, "hit_weight": 0.0},
                    5: {"weighted_return": 0.0, "count": 0, "hit_weight": 0.0},
                    10: {"weighted_return": 0.0, "count": 0, "hit_weight": 0.0},
                },
            },
        )
        aggregate["runs"] += 1
        aggregate["trade_dates_covered"] += int((summary or {}).get("trade_dates") or 0)
        aggregate["sample_count"] += int((summary or {}).get("pick_count") or 0)
        latest_trade_date = (summary or {}).get("latest_trade_date")
        if latest_trade_date and (aggregate["latest_trade_date"] is None or str(latest_trade_date) > str(aggregate["latest_trade_date"])):
            aggregate["latest_trade_date"] = latest_trade_date
        for window in (3, 5, 10):
            window_payload = windows.get(window) or {}
            count = int(window_payload.get("count") or 0)
            if count <= 0:
                continue
            aggregate["window_sums"][window]["count"] += count
            aggregate["window_sums"][window]["weighted_return"] += float(window_payload.get("avg_return") or 0.0) * count
            aggregate["window_sums"][window]["hit_weight"] += float(window_payload.get("hit_rate") or 0.0) * count
    selected_run_id = run_id or (recent_runs[0]["id"] if recent_runs else None)
    selected_run = None
    if selected_run_id is not None:
        selected_run = run_repo.get_run_by_id(int(selected_run_id))
    selected_summary = (
        _build_model_run_performance_summary(
            db,
            run_id=int(selected_run_id),
            top_n=max(1, int(top_n)),
            max_trade_dates=max(5, int(max_trade_dates)),
            market=market,
        )
        if selected_run_id is not None
        else None
    )
    selected_run_config = {}
    selected_run_artifact = {}
    if selected_run is not None:
        if selected_run.config_json:
            try:
                selected_run_config = json.loads(selected_run.config_json)
            except json.JSONDecodeError:
                selected_run_config = {}
        artifact_path = str(selected_run.artifact_path or "").strip()
        if artifact_path:
            try:
                with open(artifact_path, "r", encoding="utf-8") as artifact_file:
                    selected_run_artifact = json.load(artifact_file)
            except (OSError, json.JSONDecodeError):
                selected_run_artifact = {}
    watchlist_summary = _build_watchlist_post_add_summary(db, market=market)
    next_tesla_eval = build_next_tesla_evaluation(market=market, lookback_snapshots=15, top_n=20)
    next_tesla_maturity_state = next_tesla_maturity(next_tesla_eval, lang=lang)
    technical_momentum_eval = build_technical_momentum_evaluation(market=market, lookback_snapshots=15, top_n=40)
    technical_momentum_maturity_state = technical_momentum_maturity(technical_momentum_eval, lang=lang)
    lightgbm_eval = build_lightgbm_evaluation(market=market, lookback_snapshots=15, top_n=40)
    lightgbm_maturity_state = lightgbm_maturity(lightgbm_eval, lang=lang)
    lightgbm_prediction_eval = build_lightgbm_prediction_evaluation(market=market, recent_runs=8, top_n=40)
    selection_guidance = load_model_selection_guidance_snapshot(db, market=market, allow_fallback=True)
    selection_guidance_summary = summarize_model_selection_guidance(selection_guidance, lang=lang)
    summary_cards_html = ""
    if selected_summary is not None:
        windows = selected_summary.get("windows") or {}
        summary_cards_html = "".join(
            (
                "<article class='metric-card'>"
                f"<div class='eyebrow'>{window}{'日表现' if lang == 'zh' else 'D Window'}</div>"
                f"<div class='metric'>{_fmt_optional_float((windows.get(window) or {}).get('avg_return'), suffix='%', digits=2)}</div>"
                f"<div class='muted'>{'上涨命中率' if lang == 'zh' else 'Positive hit rate'} { _fmt_optional_float((windows.get(window) or {}).get('hit_rate'), suffix='%', digits=1) }</div>"
                f"<div class='muted'>{'强命中' if lang == 'zh' else 'Strong hit'} { _fmt_optional_float((windows.get(window) or {}).get('strong_hit_rate'), suffix='%', digits=1) } · "
                f"{'失效率' if lang == 'zh' else 'Miss rate'} { _fmt_optional_float((windows.get(window) or {}).get('miss_rate'), suffix='%', digits=1) }</div>"
                f"<div class='muted'>{'样本数' if lang == 'zh' else 'Samples'} {(windows.get(window) or {}).get('count') or 0}</div>"
                "</article>"
            )
            for window in (3, 5, 10)
        )
    symbol_context_summary = selected_run_artifact.get("symbol_context_summary") or selected_run_config.get("symbol_context_summary") or {}
    feature_families = list(selected_run_artifact.get("feature_families") or selected_run_config.get("feature_families") or [])
    target_profile_label = str(selected_run_artifact.get("target_profile") or selected_run_config.get("target_profile") or "-")
    enhancement_mode = str(selected_run_artifact.get("feature_enhancement_mode") or selected_run_config.get("feature_enhancement_mode") or "").strip().lower()
    enhancement_note = str(selected_run_artifact.get("feature_enhancement_note") or selected_run_config.get("feature_enhancement_note") or "").strip()
    listing_cov = float(symbol_context_summary.get("listing_date_coverage_pct") or 0.0)
    fund_cov = float(symbol_context_summary.get("fundamental_history_coverage_pct") or 0.0)
    concept_cov = float(symbol_context_summary.get("concept_history_coverage_pct") or 0.0)
    if enhancement_mode == "enhanced":
        diagnostic_state_label = "增强版已启用" if lang == "zh" else "Enhanced inputs active"
    elif enhancement_mode == "partial":
        diagnostic_state_label = "混合增强版" if lang == "zh" else "Partial enrichment"
    else:
        diagnostic_state_label = "价格量能版" if lang == "zh" else "Price-action mode"
    diagnostic_state_style = (
        "background:rgba(16,185,129,0.18);color:#6ee7b7;border:1px solid rgba(16,185,129,0.26);"
        if enhancement_mode == "enhanced"
        else "background:rgba(96,165,250,0.18);color:#bfdbfe;border:1px solid rgba(96,165,250,0.26);"
        if enhancement_mode == "partial"
        else "background:rgba(245,158,11,0.18);color:#fcd34d;border:1px solid rgba(245,158,11,0.28);"
    )
    feature_family_text = " / ".join(
        {
            "price_trend": "价格趋势" if lang == "zh" else "Price Trend",
            "price_extension": "价格乖离" if lang == "zh" else "Price Extension",
            "volume_intensity": "量能强度" if lang == "zh" else "Volume Intensity",
            "intraday_structure": "日内结构" if lang == "zh" else "Intraday Structure",
            "liquidity_proxy": "流动性代理" if lang == "zh" else "Liquidity Proxy",
            "listing_maturity": "上市成熟度" if lang == "zh" else "Listing Maturity",
            "board_tier": "板块层级" if lang == "zh" else "Board Tier",
        }.get(str(item), str(item))
        for item in feature_families
    ) or ("未记录" if lang == "zh" else "Not recorded")
    enhancement_action_hint = (
        "当前增强特征原料仍为空，先去任务中心运行 A 股基本面同步，再观察增强版 LightGBM 的变化。"
        if lang == "zh"
        else "Enhanced input tables are still empty. Run the CN fundamental sync from Ops first, then reassess the enriched LightGBM results."
    )
    enhancement_coverage_note = (
        enhancement_action_hint
        if enhancement_mode == "price_action_only"
        else (
            "只有这些覆盖率上来，A 股增强版 LightGBM 的收益提升才值得认真解读。"
            if lang == "zh"
            else "Only once these coverage numbers rise is it worth seriously interpreting performance as an enriched A-share LightGBM run."
        )
    )
    sync_center_href = f"/dashboard/ops/sync?lang={lang}&lookback_runs=5"
    training_diagnostic_html = f"""
      <div class="metric-grid" style="margin-top:14px;">
        <article class="metric-card">
          <div class="eyebrow">{'训练目标' if lang == 'zh' else 'Training Target'}</div>
          <div style="font-size:20px;font-weight:800;line-height:1.3;margin:6px 0 8px;">{html.escape(target_profile_label)}</div>
          <div class="muted">{'当前 LightGBM 已切到短线复合目标，优先回答次日可执行性与 1D / 3D / 5D 跟随质量。' if lang == 'zh' else 'LightGBM is now running on a short-horizon composite target focused on next-session usability and 1D / 3D / 5D follow-through.'}</div>
        </article>
        <article class="metric-card">
          <div class="eyebrow">{'特征模式' if lang == 'zh' else 'Feature Mode'}</div>
          <div style="display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;font-size:12px;font-weight:800;{diagnostic_state_style}">{html.escape(diagnostic_state_label)}</div>
          <div class="muted" style="margin-top:10px;">{html.escape(feature_family_text)}</div>
          <div class="muted" style="margin-top:8px;">{html.escape(enhancement_note or ('如果覆盖率很低，说明这次 run 仍主要依赖价格量能结构，基本面/概念增强尚未真正喂进训练。' if lang == 'zh' else 'Low coverage means the run is still relying mostly on price/volume structure rather than enriched fundamentals or concept inputs.'))}</div>
        </article>
        <article class="metric-card">
          <div class="eyebrow">{'增强特征覆盖' if lang == 'zh' else 'Enriched Coverage'}</div>
          <div class="muted">{('上市日期 ' + _fmt_optional_float(listing_cov, suffix='%', digits=1)) if lang == 'zh' else ('Listing date ' + _fmt_optional_float(listing_cov, suffix='%', digits=1))}</div>
          <div class="muted">{('基本面历史 ' + _fmt_optional_float(fund_cov, suffix='%', digits=1)) if lang == 'zh' else ('Fundamental history ' + _fmt_optional_float(fund_cov, suffix='%', digits=1))}</div>
          <div class="muted">{('概念历史 ' + _fmt_optional_float(concept_cov, suffix='%', digits=1)) if lang == 'zh' else ('Concept history ' + _fmt_optional_float(concept_cov, suffix='%', digits=1))}</div>
          <div class="muted" style="margin-top:8px;">{html.escape(enhancement_coverage_note)}</div>
          <div style="margin-top:12px;"><a class="pill" href="{html.escape(sync_center_href, quote=True)}">{'去同步中心补原料' if lang == 'zh' else 'Open Sync Center'}</a></div>
        </article>
      </div>
    """
    run_options_html = "".join(
        f"<option value='{int(item['id'])}' {'selected' if int(item['id']) == int(selected_run_id or 0) else ''}>"
        f"#{int(item['id'])} · {html.escape(str(item.get('name') or '-'))} · {html.escape(str(item.get('market') or '-'))}"
        "</option>"
        for item in recent_runs
    )
    market_options_html = "".join(
        f"<option value='{value}' {'selected' if market == value else ''}>{label}</option>"
        for value, label in (("ALL", "全部市场" if lang == "zh" else "All markets"), ("CN", "A股" if lang == "zh" else "CN"), ("US", "美股" if lang == "zh" else "US"))
    )
    reason_jump_html = "".join(
        _reason_screen_link(
            format_trade_gate_reason(reason, lang=lang),
            reason=reason,
            status=None,
            market=market,
            lang=lang,
            css_class="pill",
        )
        for reason in (
            "low_trade_readiness",
            "extended_after_sharp_move",
            "too_far_from_pullback_zone",
            "too_many_risk_flags",
            "missing_latest_price",
        )
    )
    def _guidance_bucket_label(value: str | None) -> str:
        normalized = str(value or "").strip()
        if not normalized or normalized == "unclassified":
            return "未归类" if lang == "zh" else "Unclassified"
        if normalized == "ALL":
            return "任意动作" if lang == "zh" else "Any Action"
        return ACTION_BUCKET_LABELS.get(normalized, {}).get(lang, normalized)

    def _guidance_market_label(value: str | None) -> str:
        normalized = str(value or "").upper()
        if normalized == "CN":
            return "A股" if lang == "zh" else "CN"
        if normalized == "US":
            return "美股" if lang == "zh" else "US"
        return normalized or "-"

    guidance_recommendations = list((selection_guidance or {}).get("recommendations") or [])
    guidance_combos = list((selection_guidance or {}).get("combos") or [])
    guidance_winners = list((selection_guidance or {}).get("winner_attribution") or [])
    top_guidance = guidance_recommendations[0] if guidance_recommendations else {}
    top_combo = guidance_combos[0] if guidance_combos else {}
    winner_total = int((selection_guidance or {}).get("winner_total") or 0)
    guidance_snapshot_meta = selection_guidance_summary.get("snapshot_meta") or {}
    top_guidance_href = str(selection_guidance_summary.get("top_model_href") or f"/screeners?lang={lang}&market={market}")
    top_combo_href = str(selection_guidance_summary.get("top_combo_href") or f"/screeners?lang={lang}&market={market}")
    if top_guidance:
        top_guidance_title = html.escape(str(top_guidance.get("template_label") or top_guidance.get("template") or "-"))
        if top_guidance.get("action_bucket"):
            top_guidance_title += f" · {html.escape(_guidance_bucket_label(str(top_guidance.get('action_bucket'))))}"
        top_guidance_copy = (
            f"次日均值 {_fmt_optional_float((top_guidance.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)}，"
            f"命中率 {_fmt_optional_float((top_guidance.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)}，"
            f"提前覆盖强票 {int(top_guidance.get('winner_capture_count') or 0)} 只。"
            if lang == "zh"
            else (
                f"1D avg {_fmt_optional_float((top_guidance.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)}, "
                f"hit rate {_fmt_optional_float((top_guidance.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)}, "
                f"captured {int(top_guidance.get('winner_capture_count') or 0)} strong movers."
            )
        )
    else:
        top_guidance_title = "样本继续沉淀" if lang == "zh" else "Still collecting samples"
        top_guidance_copy = "当前还没有足够样本给出明确偏好。" if lang == "zh" else "There is not enough sample depth for a clear preference yet."
    if top_combo:
        combo_label = (top_combo.get("label") or {}).get(lang) or (top_combo.get("label") or {}).get("zh") or "-"
        top_combo_title = html.escape(str(combo_label))
        top_combo_copy = (
            f"次日均值 {_fmt_optional_float((top_combo.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)}，"
            f"命中率 {_fmt_optional_float((top_combo.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)}，"
            f"强票覆盖率 {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}。"
            if lang == "zh"
            else (
                f"1D avg {_fmt_optional_float((top_combo.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)}, "
                f"hit rate {_fmt_optional_float((top_combo.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)}, "
                f"strong-mover coverage {_fmt_optional_float(top_combo.get('winner_capture_rate'), suffix='%', digits=1)}."
            )
        )
    else:
        top_combo_title = "组合样本继续沉淀" if lang == "zh" else "Combo samples are still accumulating"
        top_combo_copy = "还没有足够组合样本。" if lang == "zh" else "Not enough confluence samples yet."
    top_action_bucket = str(top_guidance.get("action_bucket") or top_combo.get("action_bucket") or "").strip()
    if top_action_bucket == "buy_the_dip":
        playbook_title = "当前更适合回踩低吸" if lang == "zh" else "Current bias: buy-the-dip"
        playbook_copy = (
            "优先等支撑承接或回踩确认，避免在急拉后追高。"
            if lang == "zh"
            else "Wait for support confirmation and avoid chasing extended moves."
        )
    elif top_action_bucket == "breakout_confirmation":
        playbook_title = "当前更适合突破确认" if lang == "zh" else "Current bias: breakout confirmation"
        playbook_copy = (
            "优先看放量突破与均线结构，突破失败或缩量时放弃。"
            if lang == "zh"
            else "Prioritize volume-backed breakouts and MA structure; stand down on failed or thin breakouts."
        )
    elif top_action_bucket == "bullish_entry":
        playbook_title = "当前更适合偏多入场" if lang == "zh" else "Current bias: bullish entry"
        playbook_copy = (
            "可关注质量和动量同时满足的候选，但仍要用风控标签过滤。"
            if lang == "zh"
            else "Focus on names where quality and momentum align, while still respecting risk tags."
        )
    else:
        playbook_title = "当前先做组合观察" if lang == "zh" else "Current bias: observe confluence"
        playbook_copy = (
            "动作桶优势还不够明显，先用多模型共振缩小候选池。"
            if lang == "zh"
            else "No action bucket has a decisive edge yet, so use confluence to narrow the list first."
        )
    discouraged_items = []
    for item in guidance_recommendations:
        stats_1d = item.get("stats_1d") or {}
        sample_count = int(item.get("sample_count") or 0)
        avg_1d = stats_1d.get("avg_return")
        hit_1d = stats_1d.get("hit_rate")
        if sample_count >= 5 and (
            (avg_1d is not None and float(avg_1d) < 0)
            or (hit_1d is not None and float(hit_1d) < 45)
        ):
            discouraged_items.append(item)
    discouraged_items = discouraged_items[-3:] if len(discouraged_items) > 3 else discouraged_items
    discouraged_text = " / ".join(
        str(item.get("template_label") or item.get("template") or "-")
        for item in discouraged_items
    ) or ("暂无明确禁用模型" if lang == "zh" else "No clear avoid-list yet")
    discouraged_copy = (
        "这些模型近期样本表现偏弱，今天不建议作为唯一入口。"
        if discouraged_items and lang == "zh"
        else "These models have weaker recent samples, so avoid using them as the only entry point today."
        if discouraged_items
        else "样本还不足以给出明确负面名单，继续用组合共振做约束。"
        if lang == "zh"
        else "There is not enough evidence for a hard avoid-list; keep using confluence as the guardrail."
    )
    guidance_decision_cards_html = (
        "<div class='decision-grid'>"
        f"<article class='decision-card primary'><div class='eyebrow'>{'今天先用' if lang == 'zh' else 'Use first today'}</div><h3>{top_guidance_title}</h3><p>{html.escape(top_guidance_copy)}</p><a class='pill' href='{html.escape(top_guidance_href, quote=True)}'>{'打开模型筛选' if lang == 'zh' else 'Open model screen'}</a></article>"
        f"<article class='decision-card'><div class='eyebrow'>{'组合优先级' if lang == 'zh' else 'Combo priority'}</div><h3>{top_combo_title}</h3><p>{html.escape(top_combo_copy)}</p><a class='pill' href='{html.escape(top_combo_href, quote=True)}'>{'打开组合筛选' if lang == 'zh' else 'Open combo screen'}</a></article>"
        f"<article class='decision-card'><div class='eyebrow'>{'打法偏向' if lang == 'zh' else 'Playbook bias'}</div><h3>{html.escape(playbook_title)}</h3><p>{html.escape(playbook_copy)}</p></article>"
        f"<article class='decision-card caution'><div class='eyebrow'>{'暂不优先' if lang == 'zh' else 'Do not prioritize'}</div><h3>{html.escape(discouraged_text)}</h3><p>{html.escape(discouraged_copy)}</p></article>"
        "</div>"
    )
    guidance_cards_html = (
        "<div class='metric-grid'>"
        f"<article class='metric-card'><div class='eyebrow'>{'当前优先模型' if lang == 'zh' else 'Priority model'}</div><div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{top_guidance_title}</div><div class='muted'>{html.escape(top_guidance_copy)}</div><div style='margin-top:12px;'><a class='pill' href='{html.escape(top_guidance_href, quote=True)}'>{'用这套模型去筛股' if lang == 'zh' else 'Screen with this model'}</a></div></article>"
        f"<article class='metric-card'><div class='eyebrow'>{'优先模型组合' if lang == 'zh' else 'Priority combo'}</div><div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{top_combo_title}</div><div class='muted'>{html.escape(top_combo_copy)}</div><div style='margin-top:12px;'><a class='pill' href='{html.escape(top_combo_href, quote=True)}'>{'用这套组合去筛股' if lang == 'zh' else 'Screen with this combo'}</a></div></article>"
        f"<article class='metric-card'><div class='eyebrow'>{'反向归因样本' if lang == 'zh' else 'Winner traceback'}</div><div class='metric'>{winner_total}</div><div class='muted'>{'近期次日涨幅不低于 3% 的强势样本，用来检查哪些模型前一天提前命中。' if lang == 'zh' else 'Recent 1D movers above 3%, used to trace which models caught them one session earlier.'}</div><div class='muted' style='margin-top:8px;'>{html.escape((('快照来源：后台预计算' if lang == 'zh' else 'Source: background snapshot') if str(guidance_snapshot_meta.get('source') or '') == 'snapshot' else ('快照缺失：当前为实时回退' if lang == 'zh' else 'Snapshot missing: live fallback in use')))} · {html.escape(str(guidance_snapshot_meta.get('snapshot_date') or guidance_snapshot_meta.get('generated_at') or '-'))}</div></article>"
        "</div>"
    )
    guidance_rows_html = ""
    for item in guidance_recommendations[:6]:
        action_label = _guidance_bucket_label(str(item.get("action_bucket") or ""))
        guidance_rows_html += (
            "<tr>"
            f"<td>{html.escape(str(item.get('template_label') or item.get('template') or '-'))}<div class='muted'>{html.escape(action_label)}</div></td>"
            f"<td>{int(item.get('sample_count') or 0)}</td>"
            f"<td>{_fmt_optional_float((item.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((item.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((item.get('stats_3d') or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((item.get('stats_3d') or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int(item.get('winner_capture_count') or 0)}<div class='muted'>{_fmt_optional_float(item.get('winner_capture_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(item.get('score'), digits=1)}</td>"
            "</tr>"
        )
    if not guidance_rows_html:
        guidance_rows_html = f"<tr><td colspan='6'>{'暂无足够样本。' if lang == 'zh' else 'Not enough samples yet.'}</td></tr>"
    combo_rows_html = ""
    for item in guidance_combos[:6]:
        combo_label = (item.get("label") or {}).get(lang) or (item.get("label") or {}).get("zh") or "-"
        template_text = " / ".join(str(value) for value in (item.get("available_templates") or [])[:4])
        combo_rows_html += (
            "<tr>"
            f"<td><a href='{html.escape(str(item.get('screener_href') or '#'))}'>{html.escape(str(combo_label))}</a><div class='muted'>{html.escape(template_text)}</div></td>"
            f"<td>{_guidance_bucket_label(str(item.get('action_bucket') or 'ALL'))}<div class='muted'>{'至少' if lang == 'zh' else 'Min'} {int(item.get('min_hits') or 2)} {'模型命中' if lang == 'zh' else 'hits'}</div></td>"
            f"<td>{_fmt_optional_float((item.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((item.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((item.get('stats_3d') or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((item.get('stats_3d') or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((item.get('stats_5d') or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((item.get('stats_5d') or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((item.get('stats_10d') or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((item.get('stats_10d') or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int(item.get('winner_capture_count') or 0)}<div class='muted'>{_fmt_optional_float(item.get('winner_capture_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not combo_rows_html:
        combo_rows_html = f"<tr><td colspan='7'>{'暂无组合评测样本。' if lang == 'zh' else 'No combo evaluation samples yet.'}</td></tr>"
    winner_rows_html = ""
    for item in guidance_winners[:10]:
        hit_labels = " / ".join(
            str(hit.get("template_label") or hit.get("template") or "")
            for hit in (item.get("hits") or [])[:3]
            if str(hit.get("template_label") or hit.get("template") or "").strip()
        ) or ("未提前命中" if lang == "zh" else "No prior hit")
        winner_rows_html += (
            "<tr>"
            f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''))}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{_guidance_market_label(str(item.get('market') or ''))}</td>"
            f"<td>{html.escape(str(item.get('signal_date') or '-'))}<div class='muted'>{html.escape(str(item.get('winner_date') or '-'))}</div></td>"
            f"<td>{_fmt_optional_float(item.get('return_1d'), suffix='%', digits=2)}</td>"
            f"<td>{int(item.get('hit_count') or 0)}<div class='muted'>{html.escape(hit_labels)}</div></td>"
            "</tr>"
        )
    if not winner_rows_html:
        winner_rows_html = f"<tr><td colspan='5'>{'暂无可归因的大涨样本。' if lang == 'zh' else 'No attributable winner samples yet.'}</td></tr>"
    validation_summary = _build_recommendation_validation_summary(
        db,
        market=market,
        lang=lang,
        selection_guidance=selection_guidance,
        selection_guidance_summary=selection_guidance_summary,
        report_limit=30,
    )
    validation_cards_html = ""
    validation_rows_html = ""
    for row in validation_summary.get("rows") or []:
        row_windows = row.get("windows") or {}
        stats_5 = row_windows.get(5) or {}
        stats_10 = row_windows.get(10) or {}
        href = str(row.get("href") or "#")
        validation_cards_html += (
            "<article class='metric-card'>"
            f"<div class='eyebrow'>{html.escape(str(row.get('label') or '-'))}</div>"
            f"<div style='font-size:20px;font-weight:800;line-height:1.3;margin:4px 0 8px;'>{html.escape(str(row.get('title') or '-'))}</div>"
            f"<div class='muted'>{html.escape(str(row.get('note') or ''))}</div>"
            f"<div class='muted' style='margin-top:8px;'>{'5日' if lang == 'zh' else '5D'} {_fmt_optional_float(stats_5.get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float(stats_5.get('hit_rate'), suffix='%', digits=1)}"
            f" · {'10日' if lang == 'zh' else '10D'} {_fmt_optional_float(stats_10.get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float(stats_10.get('hit_rate'), suffix='%', digits=1)}</div>"
            f"<div style='margin-top:12px;'><a class='pill' href='{html.escape(href, quote=True)}'>{'打开明细' if lang == 'zh' else 'Open detail'}</a></div>"
            "</article>"
        )
        validation_rows_html += (
            "<tr>"
            f"<td>{html.escape(str(row.get('label') or '-'))}<div class='muted'>{html.escape(str(row.get('title') or '-'))}</div></td>"
            f"<td>{int(row.get('count') or 0)}<div class='muted'>{html.escape(str(row.get('note') or ''))}</div></td>"
            f"<td>{_fmt_optional_float((row_windows.get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((row_windows.get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((row_windows.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((row_windows.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((row_windows.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((row_windows.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((row_windows.get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((row_windows.get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not validation_cards_html:
        validation_cards_html = f"<div class='muted'>{'当前还没有足够样本形成推荐历史验证。' if lang == 'zh' else 'Not enough samples yet for recommendation validation.'}</div>"
    if not validation_rows_html:
        validation_rows_html = f"<tr><td colspan='6'>{'当前还没有足够样本形成推荐历史验证。' if lang == 'zh' else 'Not enough samples yet for recommendation validation.'}</td></tr>"
    recent_rows_html = ""
    for item in recent_runs[:6]:
        row_summary = _build_model_run_performance_summary(
            db,
            run_id=int(item["id"]),
            top_n=max(1, int(top_n)),
            max_trade_dates=max(5, int(max_trade_dates)),
            market=market,
        )
        windows = (row_summary or {}).get("windows") or {}
        recent_rows_html += (
            "<tr>"
            f"<td><a href='/dashboard/model-performance?{urlencode({'lang': lang, 'run_id': int(item['id']), 'market': market, 'top_n': top_n, 'max_trade_dates': max_trade_dates})}'>#{int(item['id'])}</a>"
            f"<div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('market') or '-'))}<div class='muted'>{html.escape(str(item.get('universe') or '-'))}</div></td>"
            f"<td>{html.escape(str((row_summary or {}).get('latest_trade_date') or '-'))}<div class='muted'>{'样本' if lang == 'zh' else 'Picks'} {(row_summary or {}).get('pick_count') or 0}</div></td>"
            f"<td>{_fmt_optional_float((windows.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((windows.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((windows.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((windows.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float((windows.get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((windows.get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not recent_rows_html:
        recent_rows_html = f"<tr><td colspan='6'>{'暂无可用模型 run。' if lang == 'zh' else 'No successful model runs yet.'}</td></tr>"
    aggregate_rows = sorted(
        aggregate_by_model.values(),
        key=lambda item: (
            -float((((item.get("window_sums") or {}).get(5) or {}).get("hit_weight") or 0.0) / max(1, int((((item.get("window_sums") or {}).get(5) or {}).get("count") or 0)))),
            -float((((item.get("window_sums") or {}).get(5) or {}).get("weighted_return") or 0.0) / max(1, int((((item.get("window_sums") or {}).get(5) or {}).get("count") or 0)))),
            -int(item.get("runs") or 0),
            str(item.get("name") or ""),
        ),
    )
    aggregate_rows_html = ""
    for item in aggregate_rows:
        def _aggregate_metric(window: int, kind: str) -> str:
            payload = ((item.get("window_sums") or {}).get(window) or {})
            count = int(payload.get("count") or 0)
            if count <= 0:
                return "-"
            if kind == "return":
                return _fmt_optional_float(payload.get("weighted_return", 0.0) / count, suffix="%", digits=2)
            return _fmt_optional_float(payload.get("hit_weight", 0.0) / count, suffix="%", digits=1)
        aggregate_rows_html += (
            "<tr>"
            f"<td>{html.escape(str(item.get('name') or '-'))}<div class='muted'>{html.escape(str(item.get('market') or '-'))}</div></td>"
            f"<td>{int(item.get('runs') or 0)}</td>"
            f"<td>{int(item.get('trade_dates_covered') or 0)}<div class='muted'>{'样本' if lang == 'zh' else 'Samples'} {int(item.get('sample_count') or 0)}</div></td>"
            f"<td>{html.escape(str(item.get('latest_trade_date') or '-'))}</td>"
            f"<td>{_aggregate_metric(3, 'return')}<div class='muted'>{_aggregate_metric(3, 'hit')}</div></td>"
            f"<td>{_aggregate_metric(5, 'return')}<div class='muted'>{_aggregate_metric(5, 'hit')}</div></td>"
            f"<td>{_aggregate_metric(10, 'return')}<div class='muted'>{_aggregate_metric(10, 'hit')}</div></td>"
            "</tr>"
        )
    if not aggregate_rows_html:
        aggregate_rows_html = f"<tr><td colspan='7'>{'暂无模型长期汇总。' if lang == 'zh' else 'No aggregate model summary yet.'}</td></tr>"
    next_tesla_windows = next_tesla_eval.get("windows") or {}
    next_tesla_sector_windows = next_tesla_eval.get("sector_windows") or {}
    next_tesla_sector_counts = next_tesla_eval.get("sector_counts") or {}
    next_tesla_per_market = next_tesla_eval.get("per_market") or {}
    next_tesla_snapshot_total = int(next_tesla_eval.get("snapshot_total") or 0)
    next_tesla_clean_total = int(next_tesla_eval.get("clean_snapshot_total") or 0)
    next_tesla_maturity_style = (
        "background:#dcfce7;color:#166534;"
        if str(next_tesla_maturity_state.get("tone")) == "good"
        else "background:#fef3c7;color:#92400e;"
        if str(next_tesla_maturity_state.get("tone")) == "mid"
        else "background:#e5eef7;color:#37516b;"
    )
    next_tesla_rows_html = ""
    for action_key, label in (
        ("buy_the_dip", "Buy The Dip"),
        ("wait_for_breakout", "Wait For Breakout"),
    ):
        payload = next_tesla_windows.get(action_key) or {}
        next_tesla_rows_html += (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int((payload.get(3) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(5) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(10) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    def _next_tesla_sector_summary(action_key: str) -> str:
        groups = next_tesla_sector_windows.get(action_key) or {}
        counts = next_tesla_sector_counts.get(action_key) or {}
        ranked = sorted(
            set(groups.keys()) | set(counts.keys()),
            key=lambda pair: (
                -int(counts.get(pair, 0)),
                -int(((groups.get(pair) or {}).get(5) or {}).get("count") or 0),
                str(pair or ""),
            ),
        )[:3]
        return "".join(
            f"<div class='muted'>• {html.escape(str(sector or '-'))} · "
            f"{int(counts.get(sector, 0))} {'次出现' if lang == 'zh' else 'hits'}"
            + (
                f" · {_fmt_optional_float((((groups.get(sector) or {}).get(5) or {}).get('avg_return')), suffix='%', digits=2)} / {_fmt_optional_float((((groups.get(sector) or {}).get(5) or {}).get('hit_rate')), suffix='%', digits=1)}"
                if int((((groups.get(sector) or {}).get(5) or {}).get('count') or 0)) > 0
                else ""
            )
            + "</div>"
            for sector in ranked
        ) or f"<div class='muted'>-</div>"
    def _next_tesla_market_split_html() -> str:
        market_codes = [code for code in ("CN", "US") if code in next_tesla_per_market]
        if len(market_codes) <= 1:
            return ""
        return (
            "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:12px;'>"
            + "".join(
                (
                    "<div class='metric-card'>"
                    f"<div class='eyebrow'>{'A股' if code == 'CN' and lang == 'zh' else '美股' if code == 'US' and lang == 'zh' else code}</div>"
                    f"<div class='muted'>{html.escape(str(next_tesla_maturity(next_tesla_per_market.get(code) or {}, lang=lang).get('level') or '-'))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'当前偏向' if lang == 'zh' else 'Current bias'}: {html.escape(next_tesla_market_bias(next_tesla_per_market.get(code) or {}, lang=lang))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'快照' if lang == 'zh' else 'Snapshots'} {int((next_tesla_per_market.get(code) or {}).get('snapshot_total') or 0)} · {'clean 样本' if lang == 'zh' else 'Clean samples'} {int((next_tesla_per_market.get(code) or {}).get('clean_snapshot_total') or 0)}</div>"
                    "</div>"
                )
                for code in market_codes
            )
            + "</div>"
        )
    if lang == "zh":
        if int(((next_tesla_windows.get("buy_the_dip") or {}).get(5) or {}).get("count") or 0) <= 0 and int(((next_tesla_windows.get("wait_for_breakout") or {}).get(5) or {}).get("count") or 0) <= 0:
            next_tesla_takeaway = "当前还没有成熟窗口样本，先把这块当作样本沉淀看板，不宜下结论。"
        else:
            dip_hit = float((((next_tesla_windows.get("buy_the_dip") or {}).get(5) or {}).get("hit_rate") or 0.0))
            breakout_hit = float((((next_tesla_windows.get("wait_for_breakout") or {}).get(5) or {}).get("hit_rate") or 0.0))
            if dip_hit >= breakout_hit + 5:
                next_tesla_takeaway = "目前回踩买点的 5 日盈利率更高，说明这套模板近期更偏向支撑承接。"
            elif breakout_hit >= dip_hit + 5:
                next_tesla_takeaway = "目前突破确认的 5 日盈利率更高，说明这套模板近期更偏向等确认后再跟。"
            else:
                next_tesla_takeaway = "两类打法目前差距不大，更适合把它当作两套独立 playbook 来执行。"
        next_tesla_note = f"最近回看 {next_tesla_snapshot_total} 个快照，其中 {next_tesla_clean_total} 个是带 Buy The Dip / Wait For Breakout 干净标签的样本。"
    else:
        if int(((next_tesla_windows.get("buy_the_dip") or {}).get(5) or {}).get("count") or 0) <= 0 and int(((next_tesla_windows.get("wait_for_breakout") or {}).get(5) or {}).get("count") or 0) <= 0:
            next_tesla_takeaway = "There are no mature forward-return windows yet, so treat this as sample accumulation rather than a verdict."
        else:
            dip_hit = float((((next_tesla_windows.get("buy_the_dip") or {}).get(5) or {}).get("hit_rate") or 0.0))
            breakout_hit = float((((next_tesla_windows.get("wait_for_breakout") or {}).get(5) or {}).get("hit_rate") or 0.0))
            if dip_hit >= breakout_hit + 5:
                next_tesla_takeaway = "Buy-the-dip currently shows the higher 5-day hit rate, which suggests better support-follow-through lately."
            elif breakout_hit >= dip_hit + 5:
                next_tesla_takeaway = "Breakout confirmation currently shows the higher 5-day hit rate, which suggests waiting for confirmation has been cleaner lately."
            else:
                next_tesla_takeaway = "The two playbooks are currently close enough that they should be treated as separate execution styles rather than one unified edge."
    next_tesla_note = f"Reviewing the latest {next_tesla_snapshot_total} snapshots, with {next_tesla_clean_total} carrying clean Buy The Dip / Wait For Breakout labels."
    technical_windows = technical_momentum_eval.get("windows") or {}
    technical_per_market = technical_momentum_eval.get("per_market") or {}
    technical_sector_windows = technical_momentum_eval.get("sector_windows") or {}
    technical_sector_counts = technical_momentum_eval.get("sector_counts") or {}
    technical_snapshot_total = int(technical_momentum_eval.get("snapshot_total") or 0)
    technical_labeled_total = int(technical_momentum_eval.get("labeled_snapshot_total") or 0)
    technical_maturity_style = (
        "background:#dcfce7;color:#166534;"
        if str(technical_momentum_maturity_state.get("tone")) == "good"
        else "background:#fef3c7;color:#92400e;"
        if str(technical_momentum_maturity_state.get("tone")) == "mid"
        else "background:#e5eef7;color:#37516b;"
    )
    def _technical_metric_row(action_key: str, label: str) -> str:
        payload = technical_windows.get(action_key) or {}
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int((payload.get(3) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(5) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(10) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    def _technical_market_split_html() -> str:
        market_codes = [code for code in ("CN", "US") if code in technical_per_market]
        if len(market_codes) <= 1:
            return ""
        return (
            "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:12px;'>"
            + "".join(
                (
                    "<div class='metric-card'>"
                    f"<div class='eyebrow'>{'A股' if code == 'CN' and lang == 'zh' else '美股' if code == 'US' and lang == 'zh' else code}</div>"
                    f"<div class='muted'>{html.escape(str(technical_momentum_maturity(technical_per_market.get(code) or {}, lang=lang).get('level') or '-'))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'当前偏向' if lang == 'zh' else 'Current bias'}: {html.escape(technical_momentum_bias(technical_per_market.get(code) or {}, lang=lang))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'快照' if lang == 'zh' else 'Snapshots'} {int((technical_per_market.get(code) or {}).get('snapshot_total') or 0)} · {'带标签样本' if lang == 'zh' else 'Labeled samples'} {int((technical_per_market.get(code) or {}).get('labeled_snapshot_total') or 0)}</div>"
                    "</div>"
                )
                for code in market_codes
            )
            + "</div>"
        )
    def _technical_sector_summary(action_key: str) -> str:
        groups = technical_sector_windows.get(action_key) or {}
        counts = technical_sector_counts.get(action_key) or {}
        ordered = sorted(
            counts.items(),
            key=lambda item: (
                -int((((groups.get(item[0]) or {}).get(5) or {}).get("count") or 0)),
                -int(item[1] or 0),
                str(item[0] or ""),
            ),
        )[:3]
        if not ordered:
            return f"<div class='muted'>{'当前还没有足够的行业样本。' if lang == 'zh' else 'No sector concentration yet.'}</div>"
        rows = []
        for sector_label, seen_count in ordered:
            stats_5 = ((groups.get(sector_label) or {}).get(5) or {})
            rows.append(
                "<div style='padding:8px 0;border-bottom:1px solid var(--line);'>"
                f"<div style='font-weight:700;color:var(--ink);'>{html.escape(str(sector_label or '-'))}</div>"
                f"<div class='muted'>{'出现' if lang == 'zh' else 'Seen'} {int(seen_count)} {'次' if lang == 'zh' else 'times'}"
                + (
                    f" · 5D {_fmt_optional_float(stats_5.get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float(stats_5.get('hit_rate'), suffix='%', digits=1)}"
                    if int(stats_5.get('count') or 0) > 0
                    else ""
                )
                + "</div></div>"
            )
        return "".join(rows)
    if int(((technical_windows.get("buy") or {}).get(5) or {}).get("count") or 0) <= 0 and int(((technical_windows.get("watch") or {}).get(5) or {}).get("count") or 0) <= 0:
        technical_takeaway = (
            "当前还没有成熟 5 日窗口，因此更适合作为观察看板，而不是直接给出偏向判断。"
            if lang == "zh"
            else "There are no mature 5-day windows yet, so treat this as an observation panel rather than a directional verdict."
        )
    else:
        buy_hit = float((((technical_windows.get("buy") or {}).get(5) or {}).get("hit_rate") or 0.0))
        watch_hit = float((((technical_windows.get("watch") or {}).get(5) or {}).get("hit_rate") or 0.0))
        if buy_hit >= watch_hit + 5:
            technical_takeaway = (
                "近期直接 BUY 的 5 日命中率更高，说明确认后的直接跟随更顺。"
                if lang == "zh"
                else "Direct BUY currently has the higher 5-day hit rate, which suggests cleaner post-confirmation follow-through."
            )
        elif watch_hit >= buy_hit + 5:
            technical_takeaway = (
                "近期 WATCH 再确认更稳，说明动量信号更适合先观察、再等二次确认。"
                if lang == "zh"
                else "WATCH-first currently looks steadier, which suggests momentum names are rewarding confirmation more than immediate follow-through."
            )
        else:
            technical_takeaway = (
                "BUY 和 WATCH 目前差距不大，更适合当成两套节奏不同的执行模板。"
                if lang == "zh"
                else "BUY and WATCH are currently close enough to be treated as two execution tempos rather than one dominant edge."
            )
    lightgbm_windows = lightgbm_eval.get("windows") or {}
    lightgbm_per_market = lightgbm_eval.get("per_market") or {}
    lightgbm_sector_windows = lightgbm_eval.get("sector_windows") or {}
    lightgbm_sector_counts = lightgbm_eval.get("sector_counts") or {}
    lightgbm_snapshot_total = int(lightgbm_eval.get("snapshot_total") or 0)
    lightgbm_labeled_total = int(lightgbm_eval.get("labeled_snapshot_total") or 0)
    lightgbm_prediction_windows = lightgbm_prediction_eval.get("windows") or {}
    lightgbm_prediction_execution = lightgbm_prediction_eval.get("execution") or {}
    lightgbm_prediction_per_market = lightgbm_prediction_eval.get("per_market") or {}
    lightgbm_prediction_run_count = int(lightgbm_prediction_eval.get("run_count") or 0)
    lightgbm_prediction_sample_count = int(lightgbm_prediction_eval.get("sample_count") or 0)
    lightgbm_prediction_latest_trade_date = str(lightgbm_prediction_eval.get("latest_trade_date") or "")
    lightgbm_maturity_style = (
        "background:#dcfce7;color:#166534;"
        if str(lightgbm_maturity_state.get("tone")) == "good"
        else "background:#fef3c7;color:#92400e;"
        if str(lightgbm_maturity_state.get("tone")) == "mid"
        else "background:#e5eef7;color:#37516b;"
    )
    def _lightgbm_metric_row(action_key: str, label: str) -> str:
        payload = lightgbm_windows.get(action_key) or {}
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int((payload.get(1) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(3) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(5) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(10) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    def _lightgbm_market_split_html() -> str:
        market_codes = [code for code in ("CN", "US") if code in lightgbm_per_market]
        if len(market_codes) <= 1:
            return ""
        return (
            "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:12px;'>"
            + "".join(
                (
                    "<div class='metric-card'>"
                    f"<div class='eyebrow'>{'A股' if code == 'CN' and lang == 'zh' else '美股' if code == 'US' and lang == 'zh' else code}</div>"
                    f"<div class='muted'>{html.escape(str(lightgbm_maturity(lightgbm_per_market.get(code) or {}, lang=lang).get('level') or '-'))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'当前偏向' if lang == 'zh' else 'Current bias'}: {html.escape(lightgbm_bias(lightgbm_per_market.get(code) or {}, lang=lang))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'快照' if lang == 'zh' else 'Snapshots'} {int((lightgbm_per_market.get(code) or {}).get('snapshot_total') or 0)} · {'带动作样本' if lang == 'zh' else 'Action samples'} {int((lightgbm_per_market.get(code) or {}).get('labeled_snapshot_total') or 0)}</div>"
                    "</div>"
                )
                for code in market_codes
            )
            + "</div>"
        )
    def _lightgbm_sector_summary(action_key: str) -> str:
        groups = lightgbm_sector_windows.get(action_key) or {}
        counts = lightgbm_sector_counts.get(action_key) or {}
        ordered = sorted(
            counts.items(),
            key=lambda item: (
                -int((((groups.get(item[0]) or {}).get(5) or {}).get("count") or 0)),
                -int(item[1] or 0),
                str(item[0] or ""),
            ),
        )[:3]
        if not ordered:
            return f"<div class='muted'>{'当前还没有足够的行业样本。' if lang == 'zh' else 'No sector concentration yet.'}</div>"
        rows = []
        for sector_label, seen_count in ordered:
            stats_5 = ((groups.get(sector_label) or {}).get(5) or {})
            rows.append(
                "<div style='padding:8px 0;border-bottom:1px solid var(--line);'>"
                f"<div style='font-weight:700;color:var(--ink);'>{html.escape(str(sector_label or '-'))}</div>"
                f"<div class='muted'>{'出现' if lang == 'zh' else 'Seen'} {int(seen_count)} {'次' if lang == 'zh' else 'times'}"
                + (
                    f" · 5D {_fmt_optional_float(stats_5.get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float(stats_5.get('hit_rate'), suffix='%', digits=1)}"
                    if int(stats_5.get('count') or 0) > 0
                    else ""
                )
                + "</div></div>"
            )
        return "".join(rows)
    def _lightgbm_short_cycle_card(window: int, label: str) -> str:
        ranked = []
        for action_key, action_label in (("pullback", "Pullback"), ("breakout", "Breakout"), ("watch", "Watch")):
            stats = (lightgbm_windows.get(action_key) or {}).get(window) or {}
            ranked.append(
                (
                    int(stats.get("count") or 0),
                    float(stats.get("hit_rate") or 0.0),
                    float(stats.get("avg_return") or 0.0),
                    action_label,
                    stats,
                )
            )
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        count, hit_rate, avg_return, action_label, stats = ranked[0]
        if count <= 0:
            summary = "当前没有成熟样本。" if lang == "zh" else "No mature samples yet."
            detail = "先继续留样。" if lang == "zh" else "Keep collecting samples first."
        else:
            if lang == "zh":
                summary = f"{action_label} 当前更占优"
                detail = f"命中率 {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · 平均收益 {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
            else:
                summary = f"{action_label} currently leads"
                detail = f"Hit rate {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · Avg {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
        return (
            "<article class='metric-card'>"
            f"<div class='eyebrow'>{label}</div>"
            f"<div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{html.escape(summary)}</div>"
            f"<div class='muted'>{html.escape(detail)}</div>"
            f"<div class='muted' style='margin-top:8px;'>{'样本' if lang == 'zh' else 'Samples'} {count}</div>"
            "</article>"
        )
    def _lightgbm_prediction_metric_row(action_key: str, label: str) -> str:
        payload = lightgbm_prediction_windows.get(action_key) or {}
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int((payload.get(1) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(3) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(5) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float((payload.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float((payload.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    def _lightgbm_prediction_short_cycle_card(window: int, label: str) -> str:
        ranked = []
        for action_key, action_label in (("pullback", "Pullback"), ("breakout", "Breakout"), ("watch", "Watch")):
            stats = (lightgbm_prediction_windows.get(action_key) or {}).get(window) or {}
            ranked.append(
                (
                    int(stats.get("count") or 0),
                    float(stats.get("hit_rate") or 0.0),
                    float(stats.get("avg_return") or 0.0),
                    action_label,
                )
            )
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        count, hit_rate, avg_return, action_label = ranked[0]
        if count <= 0:
            summary = "当前没有成熟历史样本。" if lang == "zh" else "No mature historical samples yet."
            detail = "先继续累积跨日结果。" if lang == "zh" else "Keep accumulating cross-session results."
        else:
            summary = (
                f"{action_label} 当前更占优"
                if lang == "zh"
                else f"{action_label} currently leads"
            )
            detail = (
                f"命中率 {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · 平均收益 {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
                if lang == "zh"
                else f"Hit rate {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · Avg {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
            )
        return (
            "<article class='metric-card'>"
            f"<div class='eyebrow'>{label}</div>"
            f"<div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{html.escape(summary)}</div>"
            f"<div class='muted'>{html.escape(detail)}</div>"
            f"<div class='muted' style='margin-top:8px;'>{'样本' if lang == 'zh' else 'Samples'} {count}</div>"
            "</article>"
        )
    def _lightgbm_prediction_market_split_html() -> str:
        market_codes = [code for code in ("CN", "US") if code in lightgbm_prediction_per_market]
        if len(market_codes) <= 1:
            return ""
        cards = []
        for code in market_codes:
            payload = lightgbm_prediction_per_market.get(code) or {}
            windows = payload.get("windows") or {}
            ranked = []
            for action_key, action_label in (("pullback", "Pullback"), ("breakout", "Breakout"), ("watch", "Watch")):
                stats = (windows.get(action_key) or {}).get(1) or {}
                ranked.append(
                    (
                        int(stats.get("count") or 0),
                        float(stats.get("hit_rate") or 0.0),
                        float(stats.get("avg_return") or 0.0),
                        action_label,
                    )
                )
            ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
            count, hit_rate, avg_return, action_label = ranked[0]
            if count <= 0:
                summary = "样本观察中" if lang == "zh" else "Observation only"
                detail = "当前还没有成熟次日样本。" if lang == "zh" else "No mature next-day samples yet."
            else:
                summary = (
                    f"次日更偏 {action_label}"
                    if lang == "zh"
                    else f"1D leans {action_label}"
                )
                detail = (
                    f"命中率 {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · 平均收益 {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
                    if lang == "zh"
                    else f"Hit rate {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · Avg {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
                )
            cards.append(
                "<div class='metric-card'>"
                f"<div class='eyebrow'>{'A股' if code == 'CN' and lang == 'zh' else '美股' if code == 'US' and lang == 'zh' else code}</div>"
                f"<div style='font-size:20px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{html.escape(summary)}</div>"
                f"<div class='muted'>{html.escape(detail)}</div>"
                f"<div class='muted' style='margin-top:8px;'>{'历史样本' if lang == 'zh' else 'Historical samples'} {int(payload.get('sample_count') or 0)}</div>"
                "</div>"
            )
        return "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:12px;'>" + "".join(cards) + "</div>"
    def _lightgbm_execution_row(action_key: str, label: str) -> str:
        stats = lightgbm_prediction_execution.get(action_key) or {}
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int(stats.get('count') or 0)}</td>"
            f"<td>{_fmt_optional_float(stats.get('execution_hit_rate'), suffix='%', digits=1)}</td>"
            f"<td>{_fmt_optional_float(stats.get('avg_next_open_gap'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_optional_float(stats.get('avg_next_open_to_high'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_optional_float(stats.get('avg_next_low_drawdown'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_optional_float(stats.get('gap_blocked_rate'), suffix='%', digits=1)}</td>"
            f"<td>{_fmt_optional_float(stats.get('high_open_fail_rate'), suffix='%', digits=1)}</td>"
            "</tr>"
        )
    if int(((lightgbm_windows.get("pullback") or {}).get(5) or {}).get("count") or 0) <= 0 and int(((lightgbm_windows.get("breakout") or {}).get(5) or {}).get("count") or 0) <= 0:
        lightgbm_takeaway = (
            "当前还没有成熟 5 日窗口，因此先把 LightGBM 当作观察面板，不宜直接下动作强结论。"
            if lang == "zh"
            else "There are no mature 5-day windows yet, so treat LightGBM as an observation panel rather than an execution verdict."
        )
    else:
        pullback_hit = float((((lightgbm_windows.get("pullback") or {}).get(5) or {}).get("hit_rate") or 0.0))
        breakout_hit = float((((lightgbm_windows.get("breakout") or {}).get(5) or {}).get("hit_rate") or 0.0))
        if pullback_hit >= breakout_hit + 5:
            lightgbm_takeaway = (
                "近期 LightGBM 在回踩类机会上的 5 日命中率更高，说明更适合等支撑承接再介入。"
                if lang == "zh"
                else "LightGBM currently shows the stronger 5-day hit rate on pullback setups, which suggests waiting for support follow-through."
            )
        elif breakout_hit >= pullback_hit + 5:
            lightgbm_takeaway = (
                "近期 LightGBM 在突破类机会上的 5 日命中率更高，说明顺势确认后的跟随更顺。"
                if lang == "zh"
                else "LightGBM currently shows the stronger 5-day hit rate on breakout setups, which suggests cleaner confirmation follow-through."
            )
        else:
            lightgbm_takeaway = (
                "回踩与突破两类机会目前差距不大，更适合把它们当作两套并行执行节奏。"
                if lang == "zh"
                else "Pullback and breakout are currently close enough to be treated as parallel execution styles."
            )
    def _maturity_rank(level: str | None) -> int:
        value = str(level or "").strip().lower()
        if value in {"可比较", "comparable"}:
            return 2
        if value in {"初步参考", "early read"}:
            return 1
        return 0

    def _market_label(code: str) -> str:
        if code == "CN":
            return "A股" if lang == "zh" else "CN"
        if code == "US":
            return "美股" if lang == "zh" else "US"
        return code

    next_tesla_total_score = _maturity_rank(str(next_tesla_maturity_state.get("level") or "")) * 100 + next_tesla_clean_total
    technical_total_score = _maturity_rank(str(technical_momentum_maturity_state.get("level") or "")) * 100 + technical_labeled_total
    lightgbm_total_score = _maturity_rank(str(lightgbm_maturity_state.get("level") or "")) * 100 + lightgbm_labeled_total
    template_scores = [
        ("next_tesla", next_tesla_total_score),
        ("technical_momentum", technical_total_score),
        ("lightgbm", lightgbm_total_score),
    ]
    template_scores.sort(key=lambda item: item[1], reverse=True)
    leader_key, leader_score = template_scores[0]
    runner_up_score = template_scores[1][1] if len(template_scores) > 1 else 0
    if leader_key == "next_tesla" and leader_score >= runner_up_score + 8:
        overview_focus_title = "强趋势二次启动" if lang == "zh" else "Next Tesla Swing"
        overview_focus_copy = (
            f"当前 clean 样本 {next_tesla_clean_total} 个，成熟度为 {next_tesla_maturity_state.get('level') or '-'}，比技术动量更接近可比较状态。"
            if lang == "zh"
            else f"It currently has {next_tesla_clean_total} clean samples and a {next_tesla_maturity_state.get('level') or '-'} maturity state, making it closer to being comparable than technical momentum."
        )
    elif leader_key == "technical_momentum" and leader_score >= runner_up_score + 8:
        overview_focus_title = "技术动量" if lang == "zh" else "Technical Momentum"
        overview_focus_copy = (
            f"当前带标签样本 {technical_labeled_total} 个，成熟度为 {technical_momentum_maturity_state.get('level') or '-'}，目前更适合作为主观察模板。"
            if lang == "zh"
            else f"It currently has {technical_labeled_total} labeled samples and a {technical_momentum_maturity_state.get('level') or '-'} maturity state, which makes it the stronger observation template right now."
        )
    elif leader_key == "lightgbm" and leader_score >= runner_up_score + 8:
        overview_focus_title = "LightGBM 多因子优选" if lang == "zh" else "LightGBM Top Picks"
        overview_focus_copy = (
            f"当前带动作样本 {lightgbm_labeled_total} 个，成熟度为 {lightgbm_maturity_state.get('level') or '-'}，已经开始具备独立评测价值。"
            if lang == "zh"
            else f"It currently has {lightgbm_labeled_total} action-labeled samples and a {lightgbm_maturity_state.get('level') or '-'} maturity state, which makes it increasingly useful as a standalone evaluation track."
        )
    else:
        overview_focus_title = "三套模板都偏观察" if lang == "zh" else "All three templates are still observational"
        overview_focus_copy = (
            f"强趋势二次启动 {next_tesla_clean_total} 个 clean 样本，技术动量 {technical_labeled_total} 个带标签样本，LightGBM {lightgbm_labeled_total} 个动作样本，当前都更适合作为观察面板。"
            if lang == "zh"
            else f"Next Tesla Swing has {next_tesla_clean_total} clean samples, Technical Momentum has {technical_labeled_total} labeled samples, and LightGBM has {lightgbm_labeled_total} action samples, so all three are still better used as observation panels."
        )

    if market == "ALL":
        cn_score = (
            _maturity_rank(str(next_tesla_maturity(next_tesla_per_market.get("CN") or {}, lang=lang).get("level") or "")) * 100
            + int((next_tesla_per_market.get("CN") or {}).get("clean_snapshot_total") or 0)
            + _maturity_rank(str(technical_momentum_maturity(technical_per_market.get("CN") or {}, lang=lang).get("level") or "")) * 100
            + int((technical_per_market.get("CN") or {}).get("labeled_snapshot_total") or 0)
            + _maturity_rank(str(lightgbm_maturity(lightgbm_per_market.get("CN") or {}, lang=lang).get("level") or "")) * 100
            + int((lightgbm_per_market.get("CN") or {}).get("labeled_snapshot_total") or 0)
        )
        us_score = (
            _maturity_rank(str(next_tesla_maturity(next_tesla_per_market.get("US") or {}, lang=lang).get("level") or "")) * 100
            + int((next_tesla_per_market.get("US") or {}).get("clean_snapshot_total") or 0)
            + _maturity_rank(str(technical_momentum_maturity(technical_per_market.get("US") or {}, lang=lang).get("level") or "")) * 100
            + int((technical_per_market.get("US") or {}).get("labeled_snapshot_total") or 0)
            + _maturity_rank(str(lightgbm_maturity(lightgbm_per_market.get("US") or {}, lang=lang).get("level") or "")) * 100
            + int((lightgbm_per_market.get("US") or {}).get("labeled_snapshot_total") or 0)
        )
        if cn_score >= us_score + 8:
            overview_market_title = "A股更有参考价值" if lang == "zh" else "CN is more informative"
            overview_market_copy = (
                "A股这边至少已经开始积累 clean / 带标签样本，更适合先拿来观察模板节奏。"
                if lang == "zh"
                else "CN already has a better base of clean and labeled samples, so it is the more useful place to observe template behavior first."
            )
        elif us_score >= cn_score + 8:
            overview_market_title = "美股更有参考价值" if lang == "zh" else "US is more informative"
            overview_market_copy = (
                "美股这边当前样本沉淀更完整，更适合先看模板胜率和板块集中度。"
                if lang == "zh"
                else "US currently has the more complete sample base, making it a better place to study hit rates and sector concentration first."
            )
        else:
            overview_market_title = "A股和美股目前接近" if lang == "zh" else "CN and US are currently close"
            overview_market_copy = (
                "两个市场都还在样本沉淀期，暂时不适合只因为市场不同就下强判断。"
                if lang == "zh"
                else "Both markets are still in the sample-accumulation phase, so it is too early to draw a strong market-level preference."
            )
    else:
        overview_market_title = (
            f"当前范围：{_market_label(market)}"
            if lang == "zh"
            else f"Current scope: {_market_label(market)}"
        )
        overview_market_copy = (
            "当前页面已经按所选市场单独评测，适合先在这个市场里比较模板，再回头做跨市场判断。"
            if lang == "zh"
            else "This page is already scoped to the selected market, so compare templates inside this market first before making cross-market judgments."
        )

    top_rank = max(
        _maturity_rank(str(next_tesla_maturity_state.get("level") or "")),
        _maturity_rank(str(technical_momentum_maturity_state.get("level") or "")),
        _maturity_rank(str(lightgbm_maturity_state.get("level") or "")),
    )
    if top_rank <= 0:
        overview_verdict_title = "先观察，不急着下结论" if lang == "zh" else "Observe first, do not force a verdict"
        overview_verdict_copy = (
            "三套模板当前都更像观察面板，重点是持续留样，而不是立刻判断哪套一定更赚钱。"
            if lang == "zh"
            else "All three templates currently behave more like observation panels, so the priority is to keep collecting samples rather than forcing a winner right now."
        )
    elif top_rank == 1:
        overview_verdict_title = "可以初步参考" if lang == "zh" else "Good for an early read"
        overview_verdict_copy = (
            "已经可以开始用来观察动作偏向和板块集中，但还不适合把它当成高置信度评分卡。"
            if lang == "zh"
            else "The panel is now useful for reading action bias and sector concentration, but it is still too early to treat it as a high-confidence scorecard."
        )
    else:
        overview_verdict_title = "样本已经可比较" if lang == "zh" else "Samples are now comparable"
        overview_verdict_copy = (
            "当前可以更严肃地比较动作类型、市场差异和板块贡献，适合进入真正的模型复盘。"
            if lang == "zh"
            else "You can now more seriously compare playbooks, market differences, and sector contribution, which is enough for a more formal model review."
        )
    overview_cards_html = (
        "<div class='metric-grid'>"
        + "".join(
            (
                "<article class='metric-card'>"
                f"<div class='eyebrow'>{title}</div>"
                f"<div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{value}</div>"
                f"<div class='muted'>{copy}</div>"
                "</article>"
            )
            for title, value, copy in (
                (
                    "当前更值得看" if lang == "zh" else "Worth watching now",
                    html.escape(overview_focus_title),
                    html.escape(overview_focus_copy),
                ),
                (
                    "市场参考度" if lang == "zh" else "Market usefulness",
                    html.escape(overview_market_title),
                    html.escape(overview_market_copy),
                ),
                (
                    "一句话判断" if lang == "zh" else "Bottom line",
                    html.escape(overview_verdict_title),
                    html.escape(overview_verdict_copy),
                ),
            )
        )
        + "</div>"
    )
    aggregate_regime_groups: dict[str, dict[int, list[float]]] = {}
    for item in aggregate_runs:
        row_summary = _build_model_run_performance_summary(
            db,
            run_id=int(item["id"]),
            top_n=max(1, int(top_n)),
            max_trade_dates=max(5, int(max_trade_dates)),
            market=market,
        )
        for row in ((row_summary or {}).get("rows") or []):
            regime_key = str(row.get("regime_label") or ("未标记" if lang == "zh" else "Unlabeled"))
            bucket = aggregate_regime_groups.setdefault(regime_key, {3: [], 5: [], 10: []})
            for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                value = row.get(key)
                if value is not None:
                    bucket[window].append(float(value))
    aggregate_regime_rows_html = ""
    for regime_label, bucket in sorted(
        aggregate_regime_groups.items(),
        key=lambda pair: (-len(pair[1].get(5) or []), str(pair[0] or "")),
    ):
        stats_3 = _aggregate_window_stats(bucket.get(3) or [])
        stats_5 = _aggregate_window_stats(bucket.get(5) or [])
        stats_10 = _aggregate_window_stats(bucket.get(10) or [])
        sample_count = max(int(stats_3.get("count") or 0), int(stats_5.get("count") or 0), int(stats_10.get("count") or 0))
        aggregate_regime_rows_html += (
            "<tr>"
            f"<td>{html.escape(regime_label)}</td>"
            f"<td>{sample_count}</td>"
            f"<td>{_fmt_optional_float(stats_3.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_3.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(stats_5.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_5.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(stats_10.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_10.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not aggregate_regime_rows_html:
        aggregate_regime_rows_html = f"<tr><td colspan='5'>{'最近成功 run 还没有足够的长期环境样本。' if lang == 'zh' else 'Recent successful runs do not have enough long-horizon regime samples yet.'}</td></tr>"
    aggregate_sector_groups: dict[str, dict[int, list[float]]] = {}
    for item in aggregate_runs:
        row_summary = _build_model_run_performance_summary(
            db,
            run_id=int(item["id"]),
            top_n=max(1, int(top_n)),
            max_trade_dates=max(5, int(max_trade_dates)),
            market=market,
        )
        for row in ((row_summary or {}).get("rows") or []):
            sector_key = str(row.get("sector_group") or row.get("sector") or row.get("industry") or ("未分类" if lang == "zh" else "Unclassified"))
            bucket = aggregate_sector_groups.setdefault(sector_key, {3: [], 5: [], 10: []})
            for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
                value = row.get(key)
                if value is not None:
                    bucket[window].append(float(value))
    aggregate_sector_rows_html = ""
    for sector_label, bucket in sorted(
        aggregate_sector_groups.items(),
        key=lambda pair: (-len(pair[1].get(5) or []), str(pair[0] or "")),
    )[:20]:
        stats_3 = _aggregate_window_stats(bucket.get(3) or [])
        stats_5 = _aggregate_window_stats(bucket.get(5) or [])
        stats_10 = _aggregate_window_stats(bucket.get(10) or [])
        sample_count = max(int(stats_3.get("count") or 0), int(stats_5.get("count") or 0), int(stats_10.get("count") or 0))
        aggregate_sector_rows_html += (
            "<tr>"
            f"<td>{html.escape(sector_label)}</td>"
            f"<td>{sample_count}</td>"
            f"<td>{_fmt_optional_float(stats_3.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_3.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(stats_5.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_5.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(stats_10.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_10.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not aggregate_sector_rows_html:
        aggregate_sector_rows_html = f"<tr><td colspan='5'>{'最近成功 run 还没有足够的长期行业样本。' if lang == 'zh' else 'Recent successful runs do not have enough long-horizon sector samples yet.'}</td></tr>"
    detail_rows = (selected_summary or {}).get("rows") or []
    regime_groups: dict[str, dict[int, list[float]]] = {}
    for item in detail_rows:
        regime_key = str(item.get("regime_label") or ("未标记" if lang == "zh" else "Unlabeled"))
        bucket = regime_groups.setdefault(regime_key, {3: [], 5: [], 10: []})
        for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
            value = item.get(key)
            if value is not None:
                bucket[window].append(float(value))
    regime_rows_html = ""
    for regime_label, bucket in sorted(
        regime_groups.items(),
        key=lambda pair: (-len(pair[1].get(5) or []), str(pair[0] or "")),
    ):
        stats_3 = _aggregate_window_stats(bucket.get(3) or [])
        stats_5 = _aggregate_window_stats(bucket.get(5) or [])
        stats_10 = _aggregate_window_stats(bucket.get(10) or [])
        sample_count = max(int(stats_3.get("count") or 0), int(stats_5.get("count") or 0), int(stats_10.get("count") or 0))
        regime_rows_html += (
            "<tr>"
            f"<td>{html.escape(regime_label)}</td>"
            f"<td>{sample_count}</td>"
            f"<td>{_fmt_optional_float(stats_3.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_3.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(stats_5.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_5.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(stats_10.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_10.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not regime_rows_html:
        regime_rows_html = f"<tr><td colspan='5'>{'当前 run 还没有可用的环境分层样本。' if lang == 'zh' else 'No regime-sliced samples yet for this run.'}</td></tr>"
    sector_groups: dict[str, dict[int, list[float]]] = {}
    for item in detail_rows:
        sector_key = str(item.get("sector_group") or item.get("sector") or item.get("industry") or ("未分类" if lang == "zh" else "Unclassified"))
        bucket = sector_groups.setdefault(sector_key, {3: [], 5: [], 10: []})
        for window, key in ((3, "return_3d"), (5, "return_5d"), (10, "return_10d")):
            value = item.get(key)
            if value is not None:
                bucket[window].append(float(value))
    sector_rows_html = ""
    for sector_label, bucket in sorted(
        sector_groups.items(),
        key=lambda pair: (-len(pair[1].get(5) or []), str(pair[0] or "")),
    )[:20]:
        stats_3 = _aggregate_window_stats(bucket.get(3) or [])
        stats_5 = _aggregate_window_stats(bucket.get(5) or [])
        stats_10 = _aggregate_window_stats(bucket.get(10) or [])
        sample_count = max(int(stats_3.get("count") or 0), int(stats_5.get("count") or 0), int(stats_10.get("count") or 0))
        sector_rows_html += (
            "<tr>"
            f"<td>{html.escape(sector_label)}</td>"
            f"<td>{sample_count}</td>"
            f"<td>{_fmt_optional_float(stats_3.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_3.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(stats_5.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_5.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(stats_10.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats_10.get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not sector_rows_html:
        sector_rows_html = f"<tr><td colspan='5'>{'当前 run 还没有可用的行业分层样本。' if lang == 'zh' else 'No sector-sliced samples yet for this run.'}</td></tr>"
    watchlist_windows = (watchlist_summary or {}).get("windows") or {}
    watchlist_current = (watchlist_summary or {}).get("current") or {}
    watchlist_cards_html = "".join(
        (
            "<article class='metric-card'>"
            f"<div class='eyebrow'>{window}{'日自选后表现' if lang == 'zh' else 'D Watchlist After Add'}</div>"
            f"<div class='metric'>{_fmt_optional_float((watchlist_windows.get(window) or {}).get('avg_return'), suffix='%', digits=2)}</div>"
            f"<div class='muted'>{'上涨命中率' if lang == 'zh' else 'Positive hit rate'} { _fmt_optional_float((watchlist_windows.get(window) or {}).get('hit_rate'), suffix='%', digits=1) }</div>"
            f"<div class='muted'>{'样本数' if lang == 'zh' else 'Samples'} {(watchlist_windows.get(window) or {}).get('count') or 0}</div>"
            "</article>"
        )
        for window in (3, 5, 10)
    )
    watchlist_rows = (watchlist_summary or {}).get("rows") or []
    watchlist_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('added_date') or '-'))}</td>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))} · {html.escape(str(item.get('market') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(item.get('return_3d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_5d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_10d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('current_return'), suffix='%', digits=2)}</td>"
        f"<td>{html.escape(str(item.get('last_synced_date') or '-'))}<div class='muted'>{html.escape(str(item.get('sync_status') or '-'))}</div></td>"
        "</tr>"
        for item in watchlist_rows[:40]
    ) or f"<tr><td colspan='7'>{'当前没有可统计的自选表现。' if lang == 'zh' else 'No measurable watchlist-after-add performance yet.'}</td></tr>"
    detail_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))} · {html.escape(str(item.get('sector_group') or item.get('sector') or item.get('industry') or ('未分类' if lang == 'zh' else 'Unclassified')))}</div></td>"
        f"<td>{html.escape(str(item.get('regime_label') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('signal_label') or '-'))}<div class='muted'>{html.escape(str(item.get('signal_strength') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(item.get('score'), digits=4)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_3d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_5d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_10d'), suffix='%', digits=2)}</td>"
        "</tr>"
        for item in detail_rows[:50]
    ) or f"<tr><td colspan='8'>{'暂无可计算样本。' if lang == 'zh' else 'No measurable samples yet.'}</td></tr>"
    selected_title = html.escape(str(((selected_summary or {}).get("run") or {}).get("name") or "-"))
    selected_subtitle = (
        f"{html.escape(str(((selected_summary or {}).get('run') or {}).get('market') or '-'))} · "
        f"{html.escape(str(((selected_summary or {}).get('run') or {}).get('universe') or '-'))} · "
        f"{'最近交易日' if lang == 'zh' else 'Latest trade date'} {html.escape(str((selected_summary or {}).get('latest_trade_date') or '-'))}"
    )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'模型评测总览' if lang == 'zh' else 'Model Evaluation Overview'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:none; margin:0; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .pill,button {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          h2 {{ margin:0 0 8px; font-size:28px; }}
          .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
          .filters {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); margin-top:14px; }}
          label {{ display:block; color:var(--muted); font-size:12px; margin-bottom:6px; }}
          select,input {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0d1721; color:var(--ink); }}
          .metric-grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); margin-top:14px; }}
          .metric-card {{ border:1px solid var(--line); border-radius:18px; padding:16px; background:rgba(11,19,29,0.78); }}
          .metric {{ font-size:30px; font-weight:800; margin:6px 0; }}
          .decision-grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); margin-top:14px; }}
          .decision-card {{ border:1px solid rgba(82,168,255,0.18); border-radius:20px; padding:18px; background:rgba(11,19,29,0.82); }}
          .decision-card.primary {{ border-color:rgba(61,217,182,0.28); background:linear-gradient(180deg, rgba(61,217,182,0.13), rgba(11,19,29,0.84)); }}
          .decision-card.caution {{ border-color:rgba(246,200,95,0.24); background:linear-gradient(180deg, rgba(246,200,95,0.10), rgba(11,19,29,0.84)); }}
          .decision-card h3 {{ margin:0 0 8px; font-size:21px; line-height:1.25; }}
          .decision-card p {{ margin:0 0 12px; color:var(--muted); font-size:14px; line-height:1.55; }}
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
              <h1>{'模型评测总览' if lang == 'zh' else 'Model Evaluation Overview'}</h1>
              <p>{'先看模型近期表现、动作评测和长期分层，再决定这套模型现在值不值得继续信。' if lang == 'zh' else 'Review recent model performance, action-level evaluation, and long-horizon slices before deciding how much trust the model deserves.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to Dashboard'}</a>
                <a class="pill" href="/dashboard/ops?lang={lang}">{'任务中心' if lang == 'zh' else 'Ops'}</a>
                <a class="pill" href="/dashboard/model-performance/winner-traceback?lang={lang}&market={market}">{'强票反向归因' if lang == 'zh' else 'Winner Traceback'}</a>
              </div>
              <section class="card">
                <div class="eyebrow">{'模型闭环' if lang == 'zh' else 'Model Loop'}</div>
                <h2>{selected_title}</h2>
                <div class="muted">{selected_subtitle}</div>
                <form method="get" action="/dashboard/model-performance">
                  <input type="hidden" name="lang" value="{lang}" />
                  <div class="filters">
                    <div><label>{'模型 run' if lang == 'zh' else 'Model run'}</label><select name="run_id">{run_options_html}</select></div>
                    <div><label>{'市场' if lang == 'zh' else 'Market'}</label><select name="market">{market_options_html}</select></div>
                    <div><label>{'每日取前 N 名' if lang == 'zh' else 'Top N per day'}</label><input type="number" name="top_n" min="1" max="50" value="{int(top_n)}" /></div>
                    <div><label>{'回看交易日数' if lang == 'zh' else 'Trade dates window'}</label><input type="number" name="max_trade_dates" min="5" max="120" value="{int(max_trade_dates)}" /></div>
                  </div>
                  <div style='margin-top:14px;'><button type="submit">{'更新统计' if lang == 'zh' else 'Refresh Stats'}</button></div>
                </form>
                <div class="metric-grid">{summary_cards_html or f"<div class='muted'>{'暂无统计结果。' if lang == 'zh' else 'No stats yet.'}</div>"}</div>
                {training_diagnostic_html}
              </section>
              <section class="card">
                <div class="eyebrow">{'总览摘要' if lang == 'zh' else 'Overview Brief'}</div>
                <div class="muted">{'先回答三个问题：现在更值得看哪个模板、哪个市场更有参考价值、以及当前应把这块当观察面板还是比较面板。' if lang == 'zh' else 'Answer three questions first: which template is more worth watching, which market is more informative, and whether this should be treated as an observation panel or a comparison panel right now.'}</div>
                {overview_cards_html}
                <div style="margin-top:14px;">
                  <div class="muted" style="margin-bottom:8px;">{'常见阻断原因反查：直接跳回模型选股，看同类票为什么被拦。' if lang == 'zh' else 'Common block reasons: jump back into screeners to inspect why similar names were gated.'}</div>
                  <div class="toolbar">{reason_jump_html}</div>
                </div>
              </section>
              <section class="card">
                <div class="eyebrow">{'今日使用建议' if lang == 'zh' else "Today's Usage Guidance"}</div>
                <div class="muted">{'先按这里决定今天打开哪套模型、哪套组合、偏回踩还是突破，以及哪些模型不要单独依赖。' if lang == 'zh' else 'Use this first to decide which model, combo, and playbook bias to start from, plus which models should not be used alone.'}</div>
                {guidance_decision_cards_html}
              </section>
              <section class="card">
                <div class="eyebrow">{'选股指导 · 模型命中归因' if lang == 'zh' else 'Selection Guidance · Model Attribution'}</div>
                <div class="muted">{'这块不是看模型能筛出多少股票，而是反过来看近期真正大涨的股票，前一天到底被哪些模型或模型组合提前命中。用它来决定明天优先打开哪套模型/共振模板。' if lang == 'zh' else 'This does not reward models for producing many names. It traces recent strong movers back to the prior session and checks which models or confluence presets caught them before the move.'}</div>
                {guidance_cards_html}
                <div class="table-wrap"><table>
                  <thead><tr><th>{'模型 / 动作' if lang == 'zh' else 'Model / Playbook'}</th><th>{'样本' if lang == 'zh' else 'Samples'}</th><th>1D</th><th>3D</th><th>{'强票覆盖' if lang == 'zh' else 'Winner Capture'}</th><th>{'评分' if lang == 'zh' else 'Score'}</th></tr></thead>
                  <tbody>{guidance_rows_html}</tbody>
                </table></div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'建议组合' if lang == 'zh' else 'Suggested Combo'}</th><th>{'动作约束' if lang == 'zh' else 'Action Gate'}</th><th>1D</th><th>3D</th><th>5D</th><th>10D</th><th>{'强票覆盖' if lang == 'zh' else 'Winner Capture'}</th></tr></thead>
                  <tbody>{combo_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'推荐结果历史验证' if lang == 'zh' else 'Recommendation Validation'}</div>
                <div class="muted">{'把今天优先模型、优先组合和 AI 日报 Top 5 放到同一张表里，看它们最近 1/3/5/10 日到底准不准。这样模型使用指导就不只告诉你“今天看什么”，也会告诉你“最近这套建议有没有持续兑现”。' if lang == 'zh' else 'Put the priority model, priority combo, and AI Daily Report Top 5 onto one board and verify their 1/3/5/10-day follow-through. This turns guidance into an accountable recommendation loop instead of a one-day opinion.'}</div>
                <div class="metric-grid">{validation_cards_html}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'对象' if lang == 'zh' else 'Recommendation'}</th><th>{'样本 / 说明' if lang == 'zh' else 'Samples / Note'}</th><th>1D</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{validation_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'大涨股反向归因' if lang == 'zh' else 'Strong-Mover Traceback'}</div>
                <div class="muted">{'按近期次日涨幅较高的股票排序，展示它们前一个交易日是否被模型快照提前命中。命中数越高，说明对应组合越值得复盘。' if lang == 'zh' else 'Ranks recent high 1D movers and shows whether they were captured by model snapshots on the previous trading session.'}</div>
                <div class="toolbar" style="margin-top:12px;margin-bottom:0;"><a class="pill" href="/dashboard/model-performance/winner-traceback?lang={lang}&market={market}">{'打开独立归因视图' if lang == 'zh' else 'Open dedicated traceback view'}</a></div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'市场' if lang == 'zh' else 'Market'}</th><th>{'信号日 / 上涨日' if lang == 'zh' else 'Signal / Move Date'}</th><th>{'次日涨幅' if lang == 'zh' else '1D Move'}</th><th>{'提前命中' if lang == 'zh' else 'Prior Hits'}</th></tr></thead>
                  <tbody>{winner_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'模型历史胜率榜' if lang == 'zh' else 'Historical Model Win-Rate Board'}</div>
                <div class="muted">{'按模型名称聚合最近成功 run。主值是平均收益，下面小字是上涨命中率；同时展示覆盖交易日数与样本数，避免只看单次 run。' if lang == 'zh' else 'Aggregates recent successful runs by model name. Main values are average returns, muted values are positive hit rate, and the table also shows covered trade dates plus sample counts so you are not overreacting to a single run.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'模型' if lang == 'zh' else 'Model'}</th><th>{'Runs' if lang == 'zh' else 'Runs'}</th><th>{'覆盖交易日 / 样本' if lang == 'zh' else 'Trade Dates / Samples'}</th><th>{'最近交易日' if lang == 'zh' else 'Latest Trade Date'}</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{aggregate_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'模型评测' if lang == 'zh' else 'Template Evaluation'}</div>
                <div class="muted">{'先用最接近实盘执行的问题来评：同一套“强趋势二次启动”里，Buy The Dip 和 Wait For Breakout 哪一类最近更稳。主值是平均收益，小字是上涨命中率。' if lang == 'zh' else 'Start with the most execution-relevant question: inside the same Next Tesla Swing template, which playbook has recently been steadier — Buy The Dip or Wait For Breakout. Main values are average returns and muted values are positive hit rates.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'动作类型' if lang == 'zh' else 'Playbook'}</th><th>3D {'样本' if lang == 'zh' else 'Samples'}</th><th>3D</th><th>5D {'样本' if lang == 'zh' else 'Samples'}</th><th>5D</th><th>10D {'样本' if lang == 'zh' else 'Samples'}</th><th>10D</th></tr></thead>
                  <tbody>{next_tesla_rows_html}</tbody>
                </table></div>
                <div style="display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-top:12px;{next_tesla_maturity_style}font-weight:800;font-size:12px;">{html.escape(str(next_tesla_maturity_state.get('level') or '-'))}</div>
                <div class="muted" style="margin-top:12px;">{next_tesla_note}</div>
                <div class="muted" style="margin-top:8px;">{html.escape(str(next_tesla_maturity_state.get('summary') or ''))}</div>
                {_next_tesla_market_split_html()}
                <div style="display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-top:12px;">
                  <div class="metric-card">
                    <div class="eyebrow">Buy The Dip</div>
                    <div class="muted">{'最近更常出现在哪些板块，以及这些板块对应的 5 日表现。' if lang == 'zh' else 'Which sectors appear most often recently, along with their 5-day behavior.'}</div>
                    <div style="margin-top:10px;">{_next_tesla_sector_summary('buy_the_dip')}</div>
                  </div>
                  <div class="metric-card">
                    <div class="eyebrow">Wait For Breakout</div>
                    <div class="muted">{'如果当前更偏突破确认，这里会更快暴露出主要集中在哪些板块。' if lang == 'zh' else 'If the tape currently leans toward breakout confirmation, this block shows which sectors are dominating.'}</div>
                    <div style="margin-top:10px;">{_next_tesla_sector_summary('wait_for_breakout')}</div>
                  </div>
                </div>
                <div class="muted" style="margin-top:8px;font-weight:700;">{'结论' if lang == 'zh' else 'Takeaway'}: {next_tesla_takeaway}</div>
                <div style="margin-top:12px;"><a class="pill" href="/screeners?lang={lang}&model_template=next_tesla_swing&market={market if market in {'CN','US'} else 'CN'}&universe=full_market&run=1">{'打开模板页查看明细' if lang == 'zh' else 'Open template page for detail'}</a></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'模型评测 · 技术动量' if lang == 'zh' else 'Template Evaluation · Technical Momentum'}</div>
                <div class="muted">{'这块回答更简单的执行问题：动量模板里，直接 BUY 是否比先 WATCH 更有后续胜率。' if lang == 'zh' else 'This asks a simpler execution question: inside the momentum template, does direct BUY follow-through beat WATCH-first names.'}</div>
                <div style="display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-top:12px;{technical_maturity_style}font-weight:800;font-size:12px;">{html.escape(str(technical_momentum_maturity_state.get('level') or '-'))}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'动作' if lang == 'zh' else 'Action'}</th><th>3D {'样本' if lang == 'zh' else 'Samples'}</th><th>3D</th><th>5D {'样本' if lang == 'zh' else 'Samples'}</th><th>5D</th><th>10D {'样本' if lang == 'zh' else 'Samples'}</th><th>10D</th></tr></thead>
                  <tbody>{_technical_metric_row('buy', 'BUY')}{_technical_metric_row('watch', 'WATCH')}</tbody>
                </table></div>
                <div class="muted" style="margin-top:12px;">{('最近回看 ' + str(technical_snapshot_total) + ' 个快照，其中 ' + str(technical_labeled_total) + ' 个带 BUY / WATCH / HOLD 标签。') if lang == 'zh' else ('Reviewing the latest ' + str(technical_snapshot_total) + ' snapshots, with ' + str(technical_labeled_total) + ' carrying BUY / WATCH / HOLD labels.')}</div>
                <div class="muted" style="margin-top:8px;">{html.escape(str(technical_momentum_maturity_state.get('summary') or ''))}</div>
                {_technical_market_split_html()}
                <div style="display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-top:12px;">
                  <div class="metric-card">
                    <div class="eyebrow">BUY {'主导板块' if lang == 'zh' else 'Dominant sectors'}</div>
                    <div class="muted">{'近期直接 BUY 更常出现在哪些行业，以及这些行业对应的 5 日表现。' if lang == 'zh' else 'Which sectors show up most often for direct BUY, plus their 5-day behavior.'}</div>
                    <div style="margin-top:10px;">{_technical_sector_summary('buy')}</div>
                  </div>
                  <div class="metric-card">
                    <div class="eyebrow">WATCH {'主导板块' if lang == 'zh' else 'Dominant sectors'}</div>
                    <div class="muted">{'如果当前更偏先观察，这里会更快暴露主要集中在哪些行业。' if lang == 'zh' else 'If the tape currently leans toward watch-first, this shows which sectors are dominating that behavior.'}</div>
                    <div style="margin-top:10px;">{_technical_sector_summary('watch')}</div>
                  </div>
                </div>
                <div class="muted" style="margin-top:8px;">{html.escape(technical_takeaway)}</div>
                <div class="muted" style="margin-top:8px;font-weight:700;">{'结论' if lang == 'zh' else 'Takeaway'}: {html.escape(technical_momentum_bias(technical_momentum_eval, lang=lang))}</div>
                <div style="margin-top:12px;"><a class="pill" href="/screeners?lang={lang}&model_template=technical_momentum&market={market if market in {'CN','US'} else 'CN'}&universe=full_market&run=1">{'打开技术动量模板页' if lang == 'zh' else 'Open technical momentum template'}</a></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'模型评测 · LightGBM' if lang == 'zh' else 'Template Evaluation · LightGBM'}</div>
                <div class="muted">{'这块专门回答 LightGBM 当前更擅长哪类执行动作：回踩、突破，还是继续观察。主值是平均收益，小字是上涨命中率。' if lang == 'zh' else 'This block focuses on which execution style LightGBM is currently handling best: pullback, breakout, or watch. Main values are average returns and muted values are positive hit rates.'}</div>
                <div style="display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-top:12px;{lightgbm_maturity_style}font-weight:800;font-size:12px;">{html.escape(str(lightgbm_maturity_state.get('level') or '-'))}</div>
                <div class="metric-grid" style="margin-top:12px;">{_lightgbm_short_cycle_card(1, '次日 / 1D' if lang == 'zh' else 'Next Day / 1D')}{_lightgbm_short_cycle_card(3, '3日 / 3D' if lang == 'zh' else '3 Day / 3D')}{_lightgbm_short_cycle_card(5, '5日 / 5D' if lang == 'zh' else '5 Day / 5D')}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'动作' if lang == 'zh' else 'Action'}</th><th>1D {'样本' if lang == 'zh' else 'Samples'}</th><th>1D</th><th>3D {'样本' if lang == 'zh' else 'Samples'}</th><th>3D</th><th>5D {'样本' if lang == 'zh' else 'Samples'}</th><th>5D</th><th>10D {'样本' if lang == 'zh' else 'Samples'}</th><th>10D</th></tr></thead>
                  <tbody>{_lightgbm_metric_row('pullback', 'Pullback')}{_lightgbm_metric_row('breakout', 'Breakout')}{_lightgbm_metric_row('watch', 'Watch')}</tbody>
                </table></div>
                <div class="muted" style="margin-top:12px;">{('最近回看 ' + str(lightgbm_snapshot_total) + ' 个快照，其中 ' + str(lightgbm_labeled_total) + ' 个带 Pullback / Breakout / Watch 动作标签。') if lang == 'zh' else ('Reviewing the latest ' + str(lightgbm_snapshot_total) + ' snapshots, with ' + str(lightgbm_labeled_total) + ' carrying Pullback / Breakout / Watch labels.')}</div>
                <div class="muted" style="margin-top:8px;">{html.escape(str(lightgbm_maturity_state.get('summary') or ''))}</div>
                {_lightgbm_market_split_html()}
                <div style="display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-top:12px;">
                  <div class="metric-card">
                    <div class="eyebrow">Pullback {'主导板块' if lang == 'zh' else 'Dominant sectors'}</div>
                    <div class="muted">{'近期 LightGBM 回踩候选主要集中在哪些板块，以及这些板块对应的 5 日表现。' if lang == 'zh' else 'Which sectors dominate recent LightGBM pullback candidates, plus their 5-day behavior.'}</div>
                    <div style="margin-top:10px;">{_lightgbm_sector_summary('pullback')}</div>
                  </div>
                  <div class="metric-card">
                    <div class="eyebrow">Breakout {'主导板块' if lang == 'zh' else 'Dominant sectors'}</div>
                    <div class="muted">{'如果 LightGBM 当前更偏突破确认，这里会更快暴露主要集中在哪些行业。' if lang == 'zh' else 'If LightGBM currently leans toward breakout confirmation, this block shows which sectors are dominating that behavior.'}</div>
                    <div style="margin-top:10px;">{_lightgbm_sector_summary('breakout')}</div>
                  </div>
                </div>
                <div class="muted" style="margin-top:8px;">{html.escape(lightgbm_takeaway)}</div>
                <div class="muted" style="margin-top:8px;font-weight:700;">{'结论' if lang == 'zh' else 'Takeaway'}: {html.escape(lightgbm_bias(lightgbm_eval, lang=lang))}</div>
                <div style="margin-top:12px;"><a class="pill" href="/screeners?lang={lang}&model_template=lightgbm_top_picks&market={market if market in {'CN','US'} else 'CN'}&universe=full_market&run=1">{'打开 LightGBM 模板页' if lang == 'zh' else 'Open LightGBM template'}</a></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'历史预测验证 · LightGBM' if lang == 'zh' else 'Historical Prediction Check · LightGBM'}</div>
                <div class="muted">{'这块直接从历史 LightGBM predictions/model_runs 回看次日、3日、5日表现，更贴近“第二天能不能用”这个问题。' if lang == 'zh' else 'This block reads historical LightGBM predictions/model_runs directly and evaluates next-day, 3-day, and 5-day behavior for a closer answer to whether the model is usable on the next session.'}</div>
                <div class="metric-grid" style="margin-top:12px;">{_lightgbm_prediction_short_cycle_card(1, '次日 / 1D' if lang == 'zh' else 'Next Day / 1D')}{_lightgbm_prediction_short_cycle_card(3, '3日 / 3D' if lang == 'zh' else '3 Day / 3D')}{_lightgbm_prediction_short_cycle_card(5, '5日 / 5D' if lang == 'zh' else '5 Day / 5D')}</div>
                {_lightgbm_prediction_market_split_html()}
                <div class="table-wrap"><table>
                  <thead><tr><th>{'动作' if lang == 'zh' else 'Action'}</th><th>{'样本' if lang == 'zh' else 'Samples'}</th><th>{'可交易命中率' if lang == 'zh' else 'Execution Hit'}</th><th>{'次日高开' if lang == 'zh' else 'Open Gap'}</th><th>{'开盘到最高' if lang == 'zh' else 'Open to High'}</th><th>{'盘中最大回撤' if lang == 'zh' else 'Low Drawdown'}</th><th>{'买不到/高开阻断' if lang == 'zh' else 'Gap Blocked'}</th><th>{'高开低走' if lang == 'zh' else 'High-open Fail'}</th></tr></thead>
                  <tbody>{_lightgbm_execution_row('pullback', 'Pullback')}{_lightgbm_execution_row('breakout', 'Breakout')}{_lightgbm_execution_row('watch', 'Watch')}</tbody>
                </table></div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'动作' if lang == 'zh' else 'Action'}</th><th>1D {'样本' if lang == 'zh' else 'Samples'}</th><th>1D</th><th>3D {'样本' if lang == 'zh' else 'Samples'}</th><th>3D</th><th>5D {'样本' if lang == 'zh' else 'Samples'}</th><th>5D</th></tr></thead>
                  <tbody>{_lightgbm_prediction_metric_row('pullback', 'Pullback')}{_lightgbm_prediction_metric_row('breakout', 'Breakout')}{_lightgbm_prediction_metric_row('watch', 'Watch')}</tbody>
                </table></div>
                <div class="muted" style="margin-top:12px;">{('最近直接回看 ' + str(lightgbm_prediction_run_count) + ' 个成功 LightGBM run，累计样本 ' + str(lightgbm_prediction_sample_count) + ' 条；最新交易日 ' + (lightgbm_prediction_latest_trade_date or '-')) if lang == 'zh' else ('Directly reviewing the latest ' + str(lightgbm_prediction_run_count) + ' successful LightGBM runs with ' + str(lightgbm_prediction_sample_count) + ' samples in total; latest trade date ' + (lightgbm_prediction_latest_trade_date or '-'))}</div>
              </section>
              <section class="card">
                <div class="eyebrow">{'长期环境分层' if lang == 'zh' else 'Historical Regime Slice'}</div>
                <div class="muted">{'把最近成功 run 的样本按环境标签汇总，观察模型在不同市场状态下的长期平均收益和上涨命中率。' if lang == 'zh' else 'Aggregates recent successful runs by regime label so you can inspect long-horizon average returns and positive hit rates across market states.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'环境' if lang == 'zh' else 'Regime'}</th><th>{'样本数' if lang == 'zh' else 'Samples'}</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{aggregate_regime_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'长期行业分层' if lang == 'zh' else 'Historical Sector Slice'}</div>
                <div class="muted">{'把最近成功 run 的样本按行业/板块汇总，看模型长期主要在哪些板块更有效，哪些板块更容易拖后腿。' if lang == 'zh' else 'Aggregates recent successful runs by sector/industry so you can see which groups have historically carried the model and which have lagged.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'行业/板块' if lang == 'zh' else 'Sector / Industry'}</th><th>{'样本数' if lang == 'zh' else 'Samples'}</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{aggregate_sector_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'市场环境分层' if lang == 'zh' else 'Regime Slice'}</div>
                <div class="muted">{'按当前 run 内每条样本自带的 regime label 分层。主值是平均收益，下面小字是上涨命中率。' if lang == 'zh' else "Slices the selected run by each pick's regime label. Main values are average returns; muted values are positive hit rate."}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'环境' if lang == 'zh' else 'Regime'}</th><th>{'样本数' if lang == 'zh' else 'Samples'}</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{regime_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'当前行业分层' if lang == 'zh' else 'Sector Slice'}</div>
                <div class="muted">{'按当前 run 样本自带的行业/板块分层，适合快速判断这次表现到底集中在哪几个方向。' if lang == 'zh' else 'Slices the selected run by sector/industry so you can quickly see which groups are driving this run.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'行业/板块' if lang == 'zh' else 'Sector / Industry'}</th><th>{'样本数' if lang == 'zh' else 'Samples'}</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{sector_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'自选加入后表现' if lang == 'zh' else 'Watchlist After Add'}</div>
                <div class="muted">{'统计当前仍在自选里的股票，从加入当日开始算 3/5/10 日表现；右侧“当前”列表示从加入到最新收盘的累计收益。' if lang == 'zh' else 'Measures current watchlist names from their add date across 3/5/10 day windows. The Current column shows cumulative return from add date to latest close.'}</div>
                <div class="metric-grid">
                  {watchlist_cards_html or f"<div class='muted'>{'暂无自选表现统计。' if lang == 'zh' else 'No watchlist performance stats yet.'}</div>"}
                  <article class='metric-card'>
                    <div class='eyebrow'>{'当前累计' if lang == 'zh' else 'Current Since Add'}</div>
                    <div class='metric'>{_fmt_optional_float(watchlist_current.get('avg_return'), suffix='%', digits=2)}</div>
                    <div class='muted'>{'上涨命中率' if lang == 'zh' else 'Positive hit rate'} {_fmt_optional_float(watchlist_current.get('hit_rate'), suffix='%', digits=1)}</div>
                    <div class='muted'>{'样本数' if lang == 'zh' else 'Samples'} {watchlist_current.get('count') or 0}</div>
                  </article>
                </div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'加入日期' if lang == 'zh' else 'Added Date'}</th><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>3D</th><th>5D</th><th>10D</th><th>{'当前' if lang == 'zh' else 'Current'}</th><th>{'同步状态' if lang == 'zh' else 'Sync Status'}</th></tr></thead>
                  <tbody>{watchlist_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'最近模型 run' if lang == 'zh' else 'Recent Runs'}</div>
                <div class="muted">{'主值是平均收益，下面小字是上涨命中率。' if lang == 'zh' else 'Main values show average returns; muted values underneath show positive hit rate.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'Run' if lang == 'zh' else 'Run'}</th><th>{'市场/范围' if lang == 'zh' else 'Market/Universe'}</th><th>{'最近交易日' if lang == 'zh' else 'Latest Trade Date'}</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{recent_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'样本明细' if lang == 'zh' else 'Sample Detail'}</div>
                <div class="muted">{'默认展示最近 run 的部分 Top-N 样本，方便核对统计不是黑箱。' if lang == 'zh' else 'Shows a slice of recent Top-N picks so the aggregate stats remain auditable.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'交易日' if lang == 'zh' else 'Trade Date'}</th><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'环境' if lang == 'zh' else 'Regime'}</th><th>{'信号' if lang == 'zh' else 'Signal'}</th><th>{'模型分' if lang == 'zh' else 'Score'}</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{detail_rows_html}</tbody>
                </table></div>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/model-performance/winner-traceback", response_class=HTMLResponse)
def dashboard_model_winner_traceback(
    request: Request,
    market: str = "CN",
    min_hits: int = 0,
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/model-performance/winner-traceback")
    lang = resolve_request_lang(request)
    market_code = str(market or "CN").strip().upper()
    if market_code not in {"CN", "US", "ALL"}:
        market_code = "CN"
    min_hits = max(0, int(min_hits or 0))
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    guidance = load_model_selection_guidance_snapshot(db, market=market_code, allow_fallback=True)
    guidance_summary = summarize_model_selection_guidance(guidance, lang=lang)
    winners_all = list((guidance or {}).get("winner_attribution") or [])
    winners = [item for item in winners_all if int(item.get("hit_count") or 0) >= min_hits]
    captured = sum(1 for item in winners_all if int(item.get("hit_count") or 0) > 0)
    missed = max(0, len(winners_all) - captured)
    avg_return = (
        sum(float(item.get("return_1d") or 0.0) for item in winners_all) / len(winners_all)
        if winners_all
        else None
    )
    capture_rate = (captured / len(winners_all) * 100.0) if winners_all else None
    template_counter: Counter[str] = Counter()
    bucket_counter: Counter[str] = Counter()
    market_counter: Counter[str] = Counter(str(item.get("market") or "-") for item in winners_all)
    for item in winners_all:
        for hit in item.get("hits") or []:
            template_counter[str(hit.get("template_label") or hit.get("template") or "-")] += 1
            bucket = str(hit.get("action_bucket") or "unclassified")
            bucket_counter[ACTION_BUCKET_LABELS.get(bucket, {}).get(lang, bucket)] += 1

    def _market_label(value: str | None) -> str:
        code = str(value or "").upper()
        if code == "CN":
            return "A股" if lang == "zh" else "CN"
        if code == "US":
            return "美股" if lang == "zh" else "US"
        if code == "ALL":
            return "全部市场" if lang == "zh" else "All Markets"
        return code or "-"

    def _template_href(hit: dict, item_market: str) -> str:
        template = str(hit.get("template") or "").strip() or "technical_momentum"
        action_bucket = str(hit.get("action_bucket") or "ALL").strip() or "ALL"
        return "/screeners?" + urlencode(
            {
                "lang": lang,
                "run": 1,
                "model_template": template,
                "market": item_market if item_market in {"CN", "US"} else market_code,
                "universe": "full_market",
                "min_trend_score": 10,
                "confluence_action_filter": action_bucket,
            }
        )

    market_options_html = "".join(
        f"<a class='pill{' active' if value == market_code else ''}' href='/dashboard/model-performance/winner-traceback?{urlencode({'lang': lang, 'market': value, 'min_hits': min_hits})}'>{label}</a>"
        for value, label in (("CN", "A股" if lang == "zh" else "CN"), ("US", "美股" if lang == "zh" else "US"), ("ALL", "全部" if lang == "zh" else "All"))
    )
    hit_filters_html = "".join(
        f"<a class='pill{' active' if value == min_hits else ''}' href='/dashboard/model-performance/winner-traceback?{urlencode({'lang': lang, 'market': market_code, 'min_hits': value})}'>{label}</a>"
        for value, label in ((0, "全部强票" if lang == "zh" else "All winners"), (1, "至少命中1个模型" if lang == "zh" else "At least 1 hit"), (2, "至少命中2个模型" if lang == "zh" else "At least 2 hits"))
    )
    template_cards_html = "".join(
        "<article class='metric-card'>"
        f"<div class='eyebrow'>{'命中模型' if lang == 'zh' else 'Hit Model'}</div><div class='metric'>{count}</div><div class='muted'>{html.escape(label)}</div>"
        "</article>"
        for label, count in template_counter.most_common(4)
    ) or f"<div class='empty'>{'暂无模型提前命中。' if lang == 'zh' else 'No prior model hits yet.'}</div>"
    bucket_cards_html = "".join(
        "<article class='metric-card'>"
        f"<div class='eyebrow'>{'动作桶' if lang == 'zh' else 'Action Bucket'}</div><div class='metric'>{count}</div><div class='muted'>{html.escape(label)}</div>"
        "</article>"
        for label, count in bucket_counter.most_common(4)
    ) or f"<div class='empty'>{'暂无动作桶归因。' if lang == 'zh' else 'No action-bucket attribution yet.'}</div>"
    market_cards_html = "".join(
        "<article class='metric-card'>"
        f"<div class='eyebrow'>{'市场' if lang == 'zh' else 'Market'}</div><div class='metric'>{count}</div><div class='muted'>{html.escape(_market_label(label))}</div>"
        "</article>"
        for label, count in market_counter.most_common(3)
    )
    rows_html = ""
    for item in winners[:80]:
        item_market = str(item.get("market") or "").upper()
        hits = list(item.get("hits") or [])
        hit_links = " / ".join(
            f"<a href='{html.escape(_template_href(hit, item_market), quote=True)}'>{html.escape(str(hit.get('template_label') or hit.get('template') or '-'))}</a>"
            for hit in hits[:4]
        ) or ("未提前命中" if lang == "zh" else "No prior hit")
        action_text = " / ".join(
            ACTION_BUCKET_LABELS.get(str(hit.get("action_bucket") or ""), {}).get(lang, str(hit.get("action_bucket") or ""))
            for hit in hits[:4]
            if str(hit.get("action_bucket") or "").strip()
        ) or "-"
        rows_html += (
            "<tr>"
            f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{html.escape(_market_label(item_market))}</td>"
            f"<td>{html.escape(str(item.get('signal_date') or '-'))}<div class='muted'>{html.escape(str(item.get('winner_date') or '-'))}</div></td>"
            f"<td>{_fmt_optional_float(item.get('return_1d'), suffix='%', digits=2)}</td>"
            f"<td>{int(item.get('hit_count') or 0)}<div class='muted'>{hit_links}</div></td>"
            f"<td>{html.escape(action_text)}</td>"
            "</tr>"
        )
    if not rows_html:
        rows_html = f"<tr><td colspan='6'>{'当前筛选条件下没有强票归因样本。' if lang == 'zh' else 'No winner-attribution samples under the current filter.'}</td></tr>"
    meta = guidance_summary.get("snapshot_meta") or {}
    source_note = ("后台快照" if lang == "zh" else "Background snapshot") if str(meta.get("source") or "") == "snapshot" else ("实时回退" if lang == "zh" else "Live fallback")
    return f"""
    <!DOCTYPE html><html lang="{lang}"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><title>{'强票反向归因' if lang == 'zh' else 'Winner Traceback'}</title>
    <style>
      :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
      * {{ box-sizing:border-box; }} body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }} a {{ color:inherit; text-decoration:none; }}
      .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }} {WORKSPACE_SIDEBAR_STYLE}
      .main {{ padding:20px 18px 28px; }} .wrap {{ max-width:none; margin:0; }} .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
      .pill {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }} .pill.active {{ border-color:rgba(61,217,182,0.32); background:rgba(61,217,182,0.14); color:var(--accent); }}
      .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
      .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }} h1,h2 {{ margin:0 0 8px; }} .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
      .metric-grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); margin-top:14px; }} .metric-card {{ border:1px solid var(--line); border-radius:18px; padding:16px; background:rgba(11,19,29,0.78); }} .metric {{ font-size:30px; font-weight:900; margin:6px 0; }} .empty {{ padding:18px; border-radius:16px; background:rgba(11,19,29,0.65); border:1px dashed var(--line); color:var(--muted); }}
      .table-wrap {{ width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }} table {{ width:100%; min-width:980px; border-collapse:collapse; font-size:14px; }} th,td {{ text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); vertical-align:top; }} th {{ color:var(--muted); font-weight:700; }} td a {{ color:var(--accent); font-weight:800; }}
      @media (max-width: 960px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} }}
    </style></head><body><div class="app"><aside class="sidebar"><div class="brand"><span class="brand-tag">PQW</span><h1>{'强票归因' if lang == 'zh' else 'Winner Traceback'}</h1><p>{'从已经走出来的股票反推前一天该用什么模型。' if lang == 'zh' else 'Trace real winners back to the prior-session models that caught them.'}</p></div><nav class="side-nav">{nav_html}</nav></aside><main class="main"><div class="wrap">
      <div class="toolbar"><a class="pill" href="/dashboard/model-performance?lang={lang}&market={market_code}">← {'返回模型评测' if lang == 'zh' else 'Back to Model Performance'}</a><a class="pill" href="/screeners?lang={lang}&market={market_code}&universe=full_market&run=1">{'打开模型选股' if lang == 'zh' else 'Open Screeners'}</a>{market_options_html}{hit_filters_html}</div>
      <section class="card"><div class="eyebrow">{'强票反向归因' if lang == 'zh' else 'Strong-Mover Traceback'}</div><h2>{'哪些模型提前抓到了真正上涨的股票' if lang == 'zh' else 'Which models caught the real winners early?'}</h2><div class="muted">{'这里用近期次日涨幅不低于阈值的股票做反查：看它们前一个交易日是否被模型快照命中。' if lang == 'zh' else 'This traces recent strong one-day movers back to the previous session and checks whether model snapshots caught them.'}</div><div class="metric-grid"><article class="metric-card"><div class="eyebrow">{'强票样本' if lang == 'zh' else 'Winner samples'}</div><div class="metric">{len(winners_all)}</div><div class="muted">{_market_label(market_code)} · {source_note}</div></article><article class="metric-card"><div class="eyebrow">{'提前命中' if lang == 'zh' else 'Prior captured'}</div><div class="metric">{captured}</div><div class="muted">{'覆盖率' if lang == 'zh' else 'Capture rate'} {_fmt_optional_float(capture_rate, suffix='%', digits=1)}</div></article><article class="metric-card"><div class="eyebrow">{'未命中' if lang == 'zh' else 'Missed'}</div><div class="metric">{missed}</div><div class="muted">{'这些是后续要改进的模型盲区。' if lang == 'zh' else 'These are model blind spots to improve next.'}</div></article><article class="metric-card"><div class="eyebrow">{'平均次日涨幅' if lang == 'zh' else 'Average 1D move'}</div><div class="metric">{_fmt_optional_float(avg_return, suffix='%', digits=2)}</div><div class="muted">{'样本按涨幅和命中数排序。' if lang == 'zh' else 'Samples are ranked by hit count and move size.'}</div></article></div></section>
      <section class="card"><div class="eyebrow">{'该优先看什么模型' if lang == 'zh' else 'What to prioritize'}</div><div class="metric-grid">{template_cards_html}</div><div class="metric-grid">{bucket_cards_html}</div><div class="metric-grid">{market_cards_html}</div></section>
      <section class="card"><div class="eyebrow">{'强票明细' if lang == 'zh' else 'Winner detail'}</div><div class="muted">{'点击命中的模型名会直接跳回对应模型筛选页，方便复盘当时那套条件还能筛出什么。' if lang == 'zh' else 'Click a hit model to jump back into the corresponding screener and review what the setup would find.'}</div><div class="table-wrap"><table><thead><tr><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'市场' if lang == 'zh' else 'Market'}</th><th>{'信号日 / 上涨日' if lang == 'zh' else 'Signal / Move Date'}</th><th>{'次日涨幅' if lang == 'zh' else '1D Move'}</th><th>{'提前命中模型' if lang == 'zh' else 'Prior-hit models'}</th><th>{'动作桶' if lang == 'zh' else 'Action bucket'}</th></tr></thead><tbody>{rows_html}</tbody></table></div></section>
    </div></main></div></body></html>
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
    market_filter: str = "CN",
    kpi_focus: str = "ALL",
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
    market_filter = str(market_filter or "CN").strip().upper()
    if market_filter not in {"ALL", "CN", "US"}:
        market_filter = "CN"
    kpi_focus = str(kpi_focus or "ALL").strip().lower()
    if kpi_focus not in {"all", "focused", "buy", "risk", "boards"}:
        kpi_focus = "all"
    signal_filter = signal_filter.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    signal_repo = PredictionRepository(db)
    latest_signals = signal_repo.list_latest_signal_decisions(
        limit=40,
        market=None if market_filter == "ALL" else market_filter,
    )
    buy_hit_counts: dict[str, int] = {}
    if min_buy_signal_count > 0:
        buy_hit_counts = PredictionRepository(db).count_recent_signal_hits(
            tickers=[str(item.get("ticker") or "").strip().upper() for item in latest_signals if item.get("ticker")],
            signal_label="BUY",
            limit_runs=lookback_runs,
        )
    filtered_signals = []
    for item in latest_signals:
        row = dict(item)
        ticker = str(row.get("ticker") or "").strip().upper()
        row["snapshot_buy_hits"] = int(buy_hit_counts.get(ticker, 0))
        row["market"] = str(row.get("market") or "OTHER").upper()
        all_tags = [str(tag).strip() for tag in (row.get("risk_flags") or row.get("execution_tags") or []) if str(tag).strip()]
        row["market_risk_tags"] = [tag for tag in all_tags if tag not in MARKET_PULSE_SOFT_RISK_TAGS]
        label = str(row.get("signal_label") or build_signal_label(row.get("score"), lang=lang) or "").strip().upper()
        if market_filter != "ALL" and row["market"] != market_filter:
            continue
        if signal_filter != "ALL" and label != signal_filter:
            continue
        if min_signal_strength > 0 and int(row.get("signal_strength") or 0) < min_signal_strength:
            continue
        if min_buy_signal_count > 0 and int(row.get("snapshot_buy_hits") or 0) < min_buy_signal_count:
            continue
        tags = row.get("risk_flags") or row.get("execution_tags") or []
        if execution_tag_filter and execution_tag_filter.upper() != "ALL" and not _matches_execution_tag_filter(tags, execution_tag_filter):
            continue
        if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL" and not _excludes_execution_tag_filter(tags, exclude_execution_tag_filter):
            continue
        filtered_signals.append(row)

    market_counts: dict[str, int] = {}
    tagged_names = 0
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, object]] = []
    for item in filtered_signals:
        market = str(item.get("market") or "OTHER").upper()
        market_counts[market] = market_counts.get(market, 0) + 1
        tags = [str(tag).strip() for tag in (item.get("market_risk_tags") or []) if str(tag).strip()]
        if tags:
            tagged_names += 1
            for tag in tags:
                risk_counts[tag] = risk_counts.get(tag, 0) + 1
            risk_examples.append({"label": item.get("ticker") or "-", "tags": tags[:2]})
    risk_examples = risk_examples[:3]
    risk_top_tags = sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    signal_bucket_counts = {
        "BUY": sum(1 for item in filtered_signals if str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper() == "BUY"),
        "WATCH": sum(1 for item in filtered_signals if str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper() == "WATCH"),
        "SELL": sum(1 for item in filtered_signals if str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper() == "SELL"),
        "HOLD": sum(1 for item in filtered_signals if str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper() == "HOLD"),
    }
    if signal_bucket_counts["BUY"] >= max(signal_bucket_counts["WATCH"], signal_bucket_counts["SELL"], 1):
        market_tone = "偏进攻" if lang == "zh" else "Risk-on"
        market_tone_help = "买点候选占优，先看热力图确认主线，再从行动榜单挑股票。" if lang == "zh" else "Buy candidates lead. Confirm the theme in the heatmap, then use action boards for names."
    elif signal_bucket_counts["SELL"] > signal_bucket_counts["BUY"] or tagged_names > signal_bucket_counts["BUY"]:
        market_tone = "偏防守" if lang == "zh" else "Defensive"
        market_tone_help = "卖点或执行风险较多，先看风险标签和概念退潮。" if lang == "zh" else "Sell signals or execution risks are elevated. Start with risk tags and fading themes."
    else:
        market_tone = "观察确认" if lang == "zh" else "Watchful"
        market_tone_help = "信号不够集中，优先看连续性和广度，不急着扩大风险。" if lang == "zh" else "Signals are mixed. Prioritize persistence and breadth before adding risk."
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
    snapshot_boards = [
        board
        for board in snapshot_boards
        if market_filter == "ALL" or str(board.get("market") or "").upper() == market_filter
    ]
    heatmap_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    heatmap_payload = (heatmap_snapshot or {}).get("payload") if isinstance(heatmap_snapshot, dict) else None
    heatmap_preview_source = list((heatmap_payload or {}).get("sector_heatmap") or []) if isinstance(heatmap_payload, dict) else []
    if market_filter != "ALL":
        heatmap_preview_source = [
            item for item in heatmap_preview_source if str(item.get("market") or "").upper() == market_filter
        ]
    if signal_filter == "BUY":
        heatmap_preview_source = [item for item in heatmap_preview_source if int(item.get("buy_signal_count") or 0) > 0]
    elif signal_filter != "ALL":
        heatmap_preview_source = [
            item
            for item in heatmap_preview_source
            if any(str(detail.get("signal_label") or "").strip().upper() == signal_filter for detail in item.get("ticker_details", []))
        ]
    if min_signal_strength > 0:
        heatmap_preview_source = [
            item for item in heatmap_preview_source if int(item.get("max_signal_strength") or 0) >= min_signal_strength
        ]
    if min_buy_signal_count > 0:
        heatmap_preview_source = [
            item for item in heatmap_preview_source if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        heatmap_preview_source = [
            item for item in heatmap_preview_source if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        heatmap_preview_source = [
            item
            for item in heatmap_preview_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    heatmap_preview_rows = heatmap_preview_source[:3]
    heatmap_updated_at = (
        (heatmap_payload or {}).get("updated_at")
        or (market_snapshot_payload or {}).get("updated_at")
        or (market_monitor_snapshot or {}).get("created_at")
    )
    concept_preview_source = _load_concept_tracker_rows(db, lookback_runs=lookback_runs)
    if min_buy_signal_count > 0:
        concept_preview_source = [
            item for item in concept_preview_source if int(item.get("buy_signal_count") or 0) >= min_buy_signal_count
        ]
    concept_preview_rows = sorted(
        concept_preview_source,
        key=lambda item: (
            int(item.get("delta_hits") or 0),
            int(item.get("streak") or 0),
            int(item.get("hits") or 0),
            float(item.get("avg_score") or 0.0),
        ),
        reverse=True,
    )[:3]
    continuous_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_CONTINUOUS_LEADERS)
    continuous_payload = (continuous_snapshot or {}).get("payload") if isinstance(continuous_snapshot, dict) else None
    continuous_preview_source = list((continuous_payload or {}).get("rows") or []) if isinstance(continuous_payload, dict) else []
    if market_filter != "ALL":
        continuous_preview_source = [
            item for item in continuous_preview_source if str(item.get("market") or "").upper() == market_filter
        ]
    if signal_filter != "ALL":
        continuous_preview_source = [
            item for item in continuous_preview_source if str(item.get("signal_label") or "").strip().upper() == signal_filter
        ]
    if min_signal_strength > 0:
        continuous_preview_source = [
            item for item in continuous_preview_source if int(item.get("signal_strength") or 0) >= min_signal_strength
        ]
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        continuous_preview_source = [
            item for item in continuous_preview_source if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
        ]
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        continuous_preview_source = [
            item
            for item in continuous_preview_source
            if _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    continuous_preview_rows = sorted(
        continuous_preview_source,
        key=lambda item: (
            int(item.get("hits") or 0),
            float(item.get("score") or 0.0),
            str(item.get("ticker") or ""),
        ),
        reverse=True,
    )[:3]
    heatmap_preview_html = "".join(
        "<a class='market-mini-row' href='/dashboard/market/heatmap?{query}'>"
        "<div><strong>{label}</strong><span>{meta}</span></div>"
        "<b>{hits}</b>"
        "</a>".format(
            query=urlencode(
                {
                    "lookback_runs": lookback_runs,
                    "lang": lang,
                    "market_filter": market_filter,
                    "signal_filter": signal_filter,
                    "min_signal_strength": min_signal_strength,
                    "min_buy_signal_count": min_buy_signal_count,
                    "execution_tag_filter": execution_tag_filter,
                    "exclude_execution_tag_filter": exclude_execution_tag_filter,
                }
            ),
            label=html.escape(
                _compact_label(
                    f"{str(item.get('market') or '').upper()} · {item.get('label')}"
                    if market_filter == "ALL"
                    else item.get("label") or "-",
                    24,
                )
            ),
            meta=html.escape(
                f"{'买点' if lang == 'zh' else 'Buy'} {int(item.get('buy_signal_count') or 0)} · {'强度' if lang == 'zh' else 'Strength'} {int(item.get('max_signal_strength') or 0)}"
            ),
            hits=int(item.get("hits") or 0),
        )
        for item in heatmap_preview_rows
    ) or f"<div class='empty'>{'热力图仍在后台预计算' if lang == 'zh' else 'Heatmap is still being precomputed'}</div>"
    concept_preview_html = "".join(
        "<a class='market-mini-row' href='/dashboard/concepts/{slug}?{query}'>"
        "<div><strong>{label}</strong><span>{meta}</span></div>"
        "<b>{delta}</b>"
        "</a>".format(
            slug=item.get("slug") or _concept_slug(str(item.get("concept_name") or "")),
            query=urlencode(
                {
                    "lookback_runs": lookback_runs,
                    "lang": lang,
                    "market_filter": market_filter,
                    "signal_filter": signal_filter,
                    "min_signal_strength": min_signal_strength,
                    "min_buy_signal_count": min_buy_signal_count,
                    "execution_tag_filter": execution_tag_filter,
                    "exclude_execution_tag_filter": exclude_execution_tag_filter,
                }
            ),
            label=html.escape(_compact_label(item.get("concept_name") or "-", 24)),
            meta=html.escape(
                f"{'命中' if lang == 'zh' else 'Hits'} {int(item.get('hits') or 0)} · {'连续' if lang == 'zh' else 'Streak'} {int(item.get('streak') or 0)}"
            ),
            delta=f"{'+' if int(item.get('delta_hits') or 0) > 0 else ''}{int(item.get('delta_hits') or 0)}",
        )
        for item in concept_preview_rows
    ) or f"<div class='empty'>{'暂无概念追踪数据' if lang == 'zh' else 'No concept tracking data yet'}</div>"
    continuous_preview_html = "".join(
        "<a class='market-mini-row' href='/dashboard/continuous-leaders?{query}'>"
        "<div><strong>{label}</strong><span>{meta}</span></div>"
        "<b>{hits}</b>"
        "</a>".format(
            query=urlencode(
                {
                    "lang": lang,
                    "lookback_runs": lookback_runs,
                    "continuous_market": market_filter if market_filter != "ALL" else "US",
                }
            ),
            label=html.escape(_compact_label(f"{item.get('ticker') or '-'} · {item.get('name') or '-'}", 30)),
            meta=html.escape(
                f"{'连续命中' if lang == 'zh' else 'Hits'} {int(item.get('hits') or 0)} · {'信号' if lang == 'zh' else 'Signal'} {item.get('signal_label') or '-'}"
            ),
            hits=int(item.get("hits") or 0),
        )
        for item in continuous_preview_rows
    ) or f"<div class='empty'>{'暂无美股连续强势数据' if lang == 'zh' else 'No U.S. continuous leaders yet'}</div>"
    market_scope_title = {
        "ALL": "全部市场" if lang == "zh" else "All Markets",
        "CN": "A股" if lang == "zh" else "A-Shares",
        "US": "美股" if lang == "zh" else "U.S. Stocks",
    }[market_filter]
    market_scope_help = (
        "口径说明：这里的热度统一表示 0-100 的平均信号强度。市场概览使用最新一批候选股的 `signal_strength` 均值。"
        if lang == "zh"
        else "Methodology: heat is a unified 0-100 average signal-strength score. On Market Pulse it is the mean `signal_strength` of the latest candidates."
    )
    market_scope_signal_rows = {
        scope_market: signal_repo.list_latest_signal_decisions(limit=24, market=scope_market)
        for scope_market in ("CN", "US")
    }
    market_scope_history = _recent_market_heat_history(db, limit=6)
    market_scope_board_counts = {scope_market: 0 for scope_market in ("CN", "US")}
    for board in ((market_snapshot_payload or {}).get("rows") or []) if isinstance(market_snapshot_payload, dict) else []:
        scope_market = str(board.get("market") or "").upper()
        if scope_market in market_scope_board_counts:
            market_scope_board_counts[scope_market] += len(board.get("rows") or [])
    market_scope_cards_html = ""
    for scope_market, scope_label in (("CN", "A股" if lang == "zh" else "A-Shares"), ("US", "美股" if lang == "zh" else "U.S. Stocks")):
        scope_rows = market_scope_signal_rows.get(scope_market) or []
        scope_history = market_scope_history.get(scope_market) or []
        scope_buy_count = sum(
            1
            for item in scope_rows
            if str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper() == "BUY"
        )
        scope_risk_count = 0
        scope_strength_total = 0
        for item in scope_rows:
            tags = [
                str(tag).strip()
                for tag in (item.get("risk_flags") or item.get("execution_tags") or [])
                if str(tag).strip() and str(tag).strip() not in MARKET_PULSE_SOFT_RISK_TAGS
            ]
            if tags:
                scope_risk_count += 1
            scope_strength_total += int(item.get("signal_strength") or 0)
        scope_heat = round(scope_strength_total / max(len(scope_rows), 1), 1) if scope_rows else 0.0
        latest_scope_date = next((str(item.get("trade_date") or "").strip() for item in scope_rows if item.get("trade_date")), "-")
        scope_delta = (scope_history[-1] - scope_history[0]) if len(scope_history) >= 2 else 0
        scope_delta_tone = "up" if scope_delta > 0 else "down" if scope_delta < 0 else "flat"
        scope_delta_label = (
            (f"+{scope_delta} {'升温' if lang == 'zh' else 'warming'}" if scope_delta > 0 else f"{scope_delta} {'降温' if lang == 'zh' else 'cooling'}")
            if scope_delta != 0
            else ("持平" if lang == "zh" else "Flat")
        )
        scope_href = f"/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'market_filter': scope_market, 'kpi_focus': kpi_focus, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}"
        market_scope_cards_html += (
            f"<a class='market-scope-card{' active' if market_filter == scope_market else ''}' href='{scope_href}'>"
            f"<div class='market-scope-head'><strong>{scope_label}</strong><span>{latest_scope_date}</span></div>"
            f"<div class='market-scope-headline'><span class='market-scope-chip {scope_delta_tone}'>{scope_delta_label}</span></div>"
            f"<div class='market-scope-stats'>"
            f"<div><b>{len(scope_rows)}</b><span>{'命中' if lang == 'zh' else 'Hits'}</span></div>"
            f"<div><b>{scope_buy_count}</b><span>{'买点' if lang == 'zh' else 'Buy'}</span></div>"
            f"<div><b>{scope_heat:.1f}</b><span>{'热度' if lang == 'zh' else 'Heat'}</span></div>"
            f"</div>"
            f"<div class='market-scope-trend-wrap'><span>{'近6次快照热度' if lang == 'zh' else 'Last 6 snapshots'}</span>{_mini_trend_bars(scope_history, lang=lang)}</div>"
            f"<div class='market-scope-meta'>{'行动榜' if lang == 'zh' else 'Boards'} {market_scope_board_counts.get(scope_market, 0)} · {'风险' if lang == 'zh' else 'Risk'} {scope_risk_count}</div>"
            "</a>"
        )
    top_signal_rows = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('trade_date') or '-'} · {item.get('market') or '-'} · {int(item.get('snapshot_buy_hits') or 0)} {'次买点' if lang == 'zh' else 'buy hits'}</div><div class='subtle'>{_compact_label(item.get('reason_summary') or item.get('name') or '-', 72)}</div></div>"
        f"<div class='row-right'><span class='signal {_dashboard_home_signal(item.get('score'), lang)[1]}'>{item.get('signal_label') or _dashboard_home_signal(item.get('score'), lang)[0]}</span><div class='mini-metric'>{int(item.get('signal_strength') or 0)}</div></div>"
        "</article>"
        for item in filtered_signals[:5]
    ) or f"<div class='empty'>{'暂无符合条件的候选' if lang == 'zh' else 'No candidates match the current focus'}</div>"

    lookback_pills = _lookback_pills("/dashboard/market", selected=lookback_runs, extra_params={"lang": lang, "heatmap_sort": heatmap_sort, "market_filter": market_filter, "kpi_focus": kpi_focus, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter})
    market_pills = "".join(
        f"<a href='/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'market_filter': market, 'kpi_focus': kpi_focus, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if market_filter == market else ''}'>{label}</a>"
        for market, label in (
            ("ALL", "All Markets" if lang == "en" else "全部市场"),
            ("CN", "A-Shares" if lang == "en" else "A股"),
            ("US", "U.S." if lang == "en" else "美股"),
        )
    )
    signal_pills = "".join(
        f"<a href='/dashboard/market?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'market_filter': market_filter, 'kpi_focus': kpi_focus, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
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
    base_market_params = {
        "lang": lang,
        "lookback_runs": lookback_runs,
        "heatmap_sort": heatmap_sort,
        "market_filter": market_filter,
        "signal_filter": signal_filter,
        "min_signal_strength": min_signal_strength,
        "min_buy_signal_count": min_buy_signal_count,
        "execution_tag_filter": execution_tag_filter,
        "exclude_execution_tag_filter": exclude_execution_tag_filter,
    }

    def _market_kpi_href(focus_key: str) -> str:
        next_focus = "all" if kpi_focus == focus_key else focus_key
        return f"/dashboard/market?{urlencode({**base_market_params, 'kpi_focus': next_focus})}#market-kpi-detail"

    market_kpi_links = {
        "focused": _market_kpi_href("focused"),
        "buy": _market_kpi_href("buy"),
        "risk": _market_kpi_href("risk"),
        "boards": _market_kpi_href("boards"),
    }
    active_focus_classes = {
        key: " active" if kpi_focus == key else ""
        for key in ("focused", "buy", "risk", "boards")
    }

    focused_rows = filtered_signals
    buy_rows = [
        item
        for item in filtered_signals
        if str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper() == "BUY"
    ]
    risk_rows = [
        item
        for item in filtered_signals
        if [str(tag).strip() for tag in (item.get("market_risk_tags") or []) if str(tag).strip()]
    ]
    board_rows_flat: list[dict] = []
    for board in snapshot_boards:
        board_title = board.get("title_zh") if lang == "zh" else board.get("title_en")
        for row in board.get("rows") or []:
            board_rows_flat.append(
                {
                    **row,
                    "board_title": board_title or board.get("key") or "-",
                }
            )
    board_rows_flat.sort(
        key=lambda item: (
            -(float(item.get("snapshot_score") or 0.0)),
            -(float(item.get("trend_score") or 0.0)),
            str(item.get("ticker") or ""),
        )
    )

    def _signal_detail_rows(rows: list[dict]) -> str:
        return "".join(
            "<tr>"
            f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('signal_label') or build_signal_label(item.get('score'), lang=lang) or '-'))}</td>"
            f"<td>{int(item.get('signal_strength') or 0)}</td>"
            f"<td>{int(item.get('snapshot_buy_hits') or 0)}</td>"
            f"<td>{' · '.join(str(tag).strip() for tag in ((item.get('market_risk_tags') or []) if kpi_focus == 'risk' else (item.get('risk_flags') or item.get('execution_tags') or [])) if str(tag).strip()) or '-'}</td>"
            f"<td>{html.escape(_compact_label(item.get('reason_summary') or item.get('summary_text') or item.get('name') or '-', 92))}</td>"
            "</tr>"
            for item in rows[:24]
        ) or f"<tr><td colspan='7'>{'当前没有符合条件的记录。' if lang == 'zh' else 'No rows match the current focus.'}</td></tr>"

    def _board_detail_rows(rows: list[dict]) -> str:
        return "".join(
            "<tr>"
            f"<td>{html.escape(str(item.get('board_title') or '-'))}</td>"
            f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('action_label') or item.get('action_summary') or '-'))}</td>"
            f"<td>{float(item.get('snapshot_score') or 0.0):.2f}</td>"
            f"<td>{float(item.get('trend_score') or 0.0):.1f}</td>"
            f"<td>{float(item.get('volume_ratio') or 0.0):.2f}</td>"
            f"<td>{html.escape(_compact_label(item.get('selection_reason') or '-', 92))}</td>"
            "</tr>"
            for item in rows[:24]
        ) or f"<tr><td colspan='7'>{'当前没有行动榜候选。' if lang == 'zh' else 'No board candidates are available right now.'}</td></tr>"

    kpi_focus_meta = {
        "focused": {
            "title": "焦点候选明细" if lang == "zh" else "Focused Names Detail",
            "subtitle": "当前筛选后的市场焦点候选。" if lang == "zh" else "Market candidates remaining after the current filters.",
        },
        "buy": {
            "title": "买点信号明细" if lang == "zh" else "Buy Signal Detail",
            "subtitle": "当前筛选条件下，信号标签为 BUY 的候选。" if lang == "zh" else "Candidates whose signal label is BUY under the current filters.",
        },
        "risk": {
            "title": "强风险标签明细" if lang == "zh" else "Material Risk Detail",
            "subtitle": "当前筛选后仍带较强风险提醒的候选，不再把常见软提醒全部算进来。" if lang == "zh" else "Candidates that still carry stronger risk tags after the current filters, excluding common soft warnings.",
        },
        "boards": {
            "title": "行动榜候选明细" if lang == "zh" else "Action Board Detail",
            "subtitle": "来自今日行动榜单的预计算候选。" if lang == "zh" else "Precomputed candidates from today's action boards.",
        },
    }
    kpi_detail_section = ""
    if kpi_focus != "all":
        if kpi_focus == "boards":
            detail_table = (
                "<table><thead><tr>"
                f"<th>{'榜单' if lang == 'zh' else 'Board'}</th>"
                f"<th>{'代码 / 名称' if lang == 'zh' else 'Ticker / Name'}</th>"
                f"<th>{'动作' if lang == 'zh' else 'Action'}</th>"
                f"<th>{'快照分' if lang == 'zh' else 'Snapshot Score'}</th>"
                f"<th>{'趋势分' if lang == 'zh' else 'Trend Score'}</th>"
                f"<th>{'量比' if lang == 'zh' else 'Volume Ratio'}</th>"
                f"<th>{'原因' if lang == 'zh' else 'Reason'}</th>"
                f"</tr></thead><tbody>{_board_detail_rows(board_rows_flat)}</tbody></table>"
            )
        else:
            detail_rows = focused_rows if kpi_focus == "focused" else buy_rows if kpi_focus == "buy" else risk_rows
            detail_table = (
                "<table><thead><tr>"
                f"<th>{'代码 / 名称' if lang == 'zh' else 'Ticker / Name'}</th>"
                f"<th>{'日期' if lang == 'zh' else 'Date'}</th>"
                f"<th>{'信号' if lang == 'zh' else 'Signal'}</th>"
                f"<th>{'强度' if lang == 'zh' else 'Strength'}</th>"
                f"<th>{'窗口 BUY 次数' if lang == 'zh' else 'Window BUY Hits'}</th>"
                f"<th>{'风险标签' if lang == 'zh' else 'Risk Tags'}</th>"
                f"<th>{'摘要' if lang == 'zh' else 'Summary'}</th>"
                f"</tr></thead><tbody>{_signal_detail_rows(detail_rows)}</tbody></table>"
            )
        meta = kpi_focus_meta[kpi_focus]
        close_link = f"/dashboard/market?{urlencode(base_market_params)}#market-kpi-detail"
        kpi_detail_section = (
            f"<section id='market-kpi-detail' class='card'>"
            f"<div class='compare-row' style='justify-content:space-between;align-items:flex-start;'>"
            f"<div><div class='eyebrow'>{'明细展开' if lang == 'zh' else 'Expanded Detail'}</div>"
            f"<h2 style='margin:0 0 8px;font-size:24px;'>{meta['title']}</h2>"
            f"<div class='muted'>{meta['subtitle']}</div></div>"
            f"<a class='pill' href='{close_link}'>{'收起' if lang == 'zh' else 'Collapse'}</a>"
            f"</div><div class='table-wrap' style='margin-top:14px;'>{detail_table}</div></section>"
        )
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:none; margin:0; }}
          .toolbar,.compare-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.05em; text-transform:uppercase; margin-bottom:12px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .pill,.compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); font-size:13px; font-weight:700; text-decoration:none; }}
          .compare-pill.active, .pill.active {{ background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }}
	          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }}
	          .market-hero {{ display:grid; grid-template-columns:minmax(0,1.3fr) minmax(300px,0.7fr); gap:18px; align-items:stretch; }}
	          .market-hero h1 {{ font-size:42px; }}
	          .market-kpis {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:18px; }}
	          .market-kpi {{ padding:14px; border-radius:18px; background:rgba(11,19,29,0.7); border:1px solid rgba(61,217,182,0.11); }}
	          .market-kpi-link {{ display:block; transition:transform .16s ease, border-color .16s ease, background .16s ease; }}
	          .market-kpi-link:hover {{ transform:translateY(-1px); border-color:rgba(61,217,182,0.34); background:rgba(14,24,36,0.88); }}
	          .market-kpi-link.active {{ border-color:rgba(61,217,182,0.5); background:rgba(20,36,51,0.92); box-shadow:0 12px 28px rgba(0,0,0,0.18); }}
	          .market-kpi b {{ display:block; font-size:23px; line-height:1; margin-bottom:6px; }}
	          .market-actions {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin-bottom:16px; }}
	          .market-action {{ position:relative; overflow:hidden; display:flex; min-height:220px; flex-direction:column; justify-content:space-between; padding:20px; border-radius:24px; background:linear-gradient(180deg, rgba(17,28,40,0.98), rgba(8,16,25,0.94)); border:1px solid var(--line); box-shadow:0 18px 40px rgba(0,0,0,0.2); transition:transform .16s ease, border-color .16s ease, background .16s ease; }}
	          .market-action:hover {{ transform:translateY(-2px); border-color:rgba(61,217,182,0.36); background:linear-gradient(180deg, rgba(20,36,51,0.98), rgba(8,16,25,0.94)); }}
	          .market-action h2 {{ margin:0 0 8px; font-size:23px; letter-spacing:-0.02em; }}
	          .market-action p {{ margin:0; color:var(--muted); font-size:13px; line-height:1.55; }}
	          .market-action .metric {{ font-size:34px; font-weight:900; letter-spacing:-0.03em; }}
	          .market-action .go {{ display:inline-flex; align-items:center; align-self:flex-start; gap:6px; margin-top:14px; color:var(--accent); font-weight:900; font-size:13px; }}
	          .market-mini-list {{ display:grid; gap:8px; margin-top:12px; }}
	          .market-mini-row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; padding:10px 12px; border-radius:14px; background:rgba(11,19,29,0.72); border:1px solid rgba(34,50,70,0.82); }}
          .market-mini-row strong {{ display:block; font-size:13px; }}
          .market-mini-row span {{ display:block; margin-top:3px; color:var(--muted); font-size:11px; }}
          .market-mini-row b {{ color:var(--accent); font-size:16px; }}
          .market-scope-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }}
          .market-scope-card {{ display:block; min-height:188px; padding:16px; border-radius:18px; background:rgba(11,19,29,0.74); border:1px solid rgba(34,50,70,0.92); transition:transform .16s ease, border-color .16s ease, background .16s ease; }}
          .market-scope-card:hover {{ transform:translateY(-1px); border-color:rgba(61,217,182,0.36); background:rgba(15,25,37,0.92); }}
          .market-scope-card.active {{ border-color:rgba(61,217,182,0.5); box-shadow:0 12px 28px rgba(0,0,0,0.16); }}
          .market-scope-head {{ display:flex; justify-content:space-between; gap:12px; align-items:baseline; }}
          .market-scope-head strong {{ font-size:16px; }}
          .market-scope-head span {{ color:var(--muted); font-size:11px; }}
          .market-scope-headline {{ display:flex; justify-content:flex-start; margin-top:10px; }}
          .market-scope-chip {{ display:inline-flex; align-items:center; padding:5px 9px; border-radius:999px; font-size:11px; font-weight:800; letter-spacing:0.02em; }}
          .market-scope-chip.up {{ background:rgba(74,222,128,0.14); color:#8af0a6; }}
          .market-scope-chip.down {{ background:rgba(255,107,129,0.14); color:#ff93a4; }}
          .market-scope-chip.flat {{ background:rgba(82,168,255,0.14); color:#89c2ff; }}
          .market-scope-stats {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:14px; }}
          .market-scope-stats b {{ display:block; font-size:22px; line-height:1; }}
          .market-scope-stats span {{ display:block; margin-top:6px; color:var(--muted); font-size:11px; }}
          .market-scope-meta {{ margin-top:10px; color:var(--muted); font-size:12px; }}
          .market-scope-help {{ margin-top:12px; padding:11px 12px; border-radius:14px; border:1px dashed rgba(61,217,182,0.24); background:rgba(61,217,182,0.06); color:var(--muted); font-size:12px; line-height:1.55; }}
          .market-scope-trend-wrap {{ margin-top:12px; }}
          .market-scope-trend-wrap span {{ display:block; color:var(--muted); font-size:11px; margin-bottom:8px; }}
          .mini-trend {{ height:34px; display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:5px; align-items:end; }}
          .mini-trend span {{ display:block; border-radius:999px 999px 3px 3px; background:linear-gradient(180deg, rgba(61,217,182,0.96), rgba(61,217,182,0.28)); box-shadow:0 6px 12px rgba(0,0,0,0.14); }}
          .mini-trend.empty {{ grid-template-columns:1fr; align-items:center; }}
          .mini-trend.empty span {{ height:auto; border-radius:0; background:none; box-shadow:none; color:var(--muted); font-size:11px; }}
          .advanced-panel {{ background:rgba(17,28,40,0.7); border:1px solid rgba(34,50,70,0.74); border-radius:20px; padding:14px; }}
	          .advanced-panel summary {{ cursor:pointer; color:var(--ink); font-weight:900; }}
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
	          @media (max-width: 1100px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 10px 36px; }} .market-hero,.market-actions {{ grid-template-columns:1fr; }} .market-kpis {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
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
            <a href="/dashboard/market?lang=en&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&market_filter={market_filter}&kpi_focus={kpi_focus}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">English</a>
            <a href="/dashboard/market?lang=zh&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&market_filter={market_filter}&kpi_focus={kpi_focus}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">中文</a>
          </div>
          <div class="compare-row">{market_pills}</div>
	          <section class="card market-hero">
	            <div>
	              <div class="eyebrow">{'市场脉冲' if lang == 'zh' else 'Market Pulse'} · {market_scope_title}</div>
	              <h1>{'今天市场：' if lang == 'zh' else 'Today: '}{market_tone}</h1>
	              <p class="muted">{market_tone_help} {'当前视角：' + market_scope_title if lang == 'zh' else 'Current scope: ' + market_scope_title}.</p>
	              <div class="market-kpis">
	                <a class="market-kpi market-kpi-link{active_focus_classes['focused']}" href="{market_kpi_links['focused']}"><b>{len(filtered_signals)}</b><span class="muted">{'焦点候选' if lang == 'zh' else 'Focused names'}</span></a>
	                <a class="market-kpi market-kpi-link{active_focus_classes['buy']}" href="{market_kpi_links['buy']}"><b>{signal_bucket_counts['BUY']}</b><span class="muted">{'买点信号' if lang == 'zh' else 'Buy signals'}</span></a>
	                <a class="market-kpi market-kpi-link{active_focus_classes['risk']}" href="{market_kpi_links['risk']}"><b>{tagged_names}</b><span class="muted">{'强风险标签' if lang == 'zh' else 'Material risk'}</span></a>
	                <a class="market-kpi market-kpi-link{active_focus_classes['boards']}" href="{market_kpi_links['boards']}"><b>{board_count}</b><span class="muted">{'行动榜候选' if lang == 'zh' else 'Board names'}</span></a>
	              </div>
	            </div>
	            <div class="advanced-panel">
	              <div class="eyebrow">{'数据状态' if lang == 'zh' else 'Data Status'}</div>
	              <div class="muted">{'最近快照' if lang == 'zh' else 'Latest snapshot'}: {format_app_datetime(heatmap_updated_at, with_tz=True)}</div>
	              <div class="compare-row" style="margin:12px 0 0;">{risk_top_tags_html}</div>
	              <div class="muted">{'风险样例' if lang == 'zh' else 'Risk examples'}: {risk_examples_html}</div>
	            </div>
	          </section>
	          {kpi_detail_section}
	          <section class="market-actions">
	            <article class="market-action">
	              <div>
	                <div class="eyebrow">{'第一步' if lang == 'zh' else 'Step 1'}</div>
	                <h2>{'美股热力图' if lang == 'zh' and market_filter == 'US' else _dt(lang, 'sector_heatmap')}</h2>
	                <p>{'看资金和模型信号集中在哪些行业/主题，先确认当前市场主线。' if lang == 'zh' else 'See where model signals cluster by sector/theme and confirm the current market leadership first.'}</p>
	                <div class="market-mini-list">{heatmap_preview_html}</div>
	              </div>
	              <a class="go" href="/dashboard/market/heatmap?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&market_filter={market_filter}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}">{'打开热力图' if lang == 'zh' else 'Open heatmap'} →</a>
	            </article>
	            <article class="market-action">
	              <div>
	                <div class="eyebrow">{'第二步' if lang == 'zh' else 'Step 2'}</div>
	                <h2>{'美股连续强势跟踪' if lang == 'zh' and market_filter == 'US' else ('U.S. Continuous Leaders' if lang == 'en' and market_filter == 'US' else _dt(lang, 'concept_activity_tracker'))}</h2>
	                <p>{'美股先看连续命中和强势延续，A股继续看概念的命中变化、连续性和扩散广度。' if lang == 'zh' else 'For U.S. names, track persistence and repeated hits; for A-shares, keep using concept delta, streak, and breadth.'}</p>
	                <div class="market-mini-list">{continuous_preview_html if market_filter == 'US' else concept_preview_html}</div>
	              </div>
	              <a class="go" href="{('/dashboard/continuous-leaders?' + urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'continuous_market': market_filter if market_filter != 'ALL' else 'US'})) if market_filter == 'US' else ('/dashboard/market/concepts?' + urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'market_filter': market_filter, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter}))}">{'打开连续强势' if lang == 'zh' and market_filter == 'US' else ('Open continuous leaders' if lang == 'en' and market_filter == 'US' else ('打开概念追踪' if lang == 'zh' else 'Open concepts'))} →</a>
	            </article>
	            <article class="market-action">
	              <div>
	                <div class="eyebrow">{'第三步' if lang == 'zh' else 'Step 3'}</div>
	                <h2>{'今日行动榜单' if lang == 'zh' else 'Action Boards'}</h2>
	                <p>{'把市场主线落到股票清单，适合继续筛选、加入自选或进入个股分析。' if lang == 'zh' else 'Turn market themes into names for screening, watchlist actions, or insight review.'}</p>
	                <div class="metric">{board_count}</div>
	                <p>{'当前预计算候选数' if lang == 'zh' else 'precomputed candidates'}</p>
	              </div>
	              <a class="go" href="/screeners/market-snapshot?lang={lang}">{'打开榜单' if lang == 'zh' else 'Open boards'} →</a>
	            </article>
	          </section>
	          <section class="grid">
	            <article class="card">
	              <div class="eyebrow">{_dt(lang, 'signal_distribution')}</div>
	              <div class="market-scope-grid">{market_scope_cards_html}</div>
                <div class="market-scope-help">{market_scope_help}</div>
	            </article>
	            <article class="card">
	              <div class="eyebrow">{'轻量候选预览' if lang == 'zh' else 'Candidate Preview'}</div>
	              <div class="muted">{'这里只放少量候选，完整股票筛选请去今日行动榜单或模型选股。' if lang == 'zh' else 'Only a few names stay here. Use action boards or screeners for full screening.'}</div>
	              <div style="margin-top:12px;display:grid;gap:10px;">{top_signal_rows}</div>
	            </article>
	          </section>
	          <section class="card">
	            <details class="advanced-panel">
	              <summary>{'高级筛选和窗口设置' if lang == 'zh' else 'Advanced Filters & Window'}</summary>
	              <div class="eyebrow" style="margin-top:14px;">{_dt(lang, 'snapshot_window')}</div>
	              <div class="compare-row">{lookback_pills}</div>
	              <div class="eyebrow" style="margin-top:12px;">{'市场范围' if lang == 'zh' else 'Market Scope'}</div>
	              <div class="compare-row">{market_pills}</div>
	              <div class="eyebrow" style="margin-top:12px;">{"Signal Focus" if lang == "en" else "信号聚焦"}</div>
	              <div class="compare-row">{signal_pills}</div>
	              <form action="/dashboard/market" method="get" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));align-items:end;">
	                <input type="hidden" name="lang" value="{lang}" />
	                <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
	                <input type="hidden" name="heatmap_sort" value="{heatmap_sort}" />
	                <input type="hidden" name="market_filter" value="{market_filter}" />
	                <input type="hidden" name="kpi_focus" value="{kpi_focus}" />
	                <input type="hidden" name="signal_filter" value="{signal_filter}" />
	                <div>
	                  <label class="muted" style="display:block;margin-bottom:6px;">{"Execution Tag" if lang == "en" else "执行提醒标签"}</label>
	                  <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
	                </div>
	                <div>
	                  <label class="muted" style="display:block;margin-bottom:6px;">{"Exclude Tag" if lang == "en" else "排除标签"}</label>
	                  <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
	                </div>
	                <div>
	                  <label class="muted" style="display:block;margin-bottom:6px;">{"Min BUY Hits In Window" if lang == "en" else "窗口内最少 BUY 命中次数"}</label>
	                  <input type="number" name="min_buy_signal_count" min="0" step="1" value="{min_buy_signal_count}" />
	                </div>
	                <div>
	                  <label class="muted" style="display:block;margin-bottom:6px;">{"Min Strength" if lang == "en" else "最低强度"}</label>
	                  <input type="number" name="min_signal_strength" min="0" max="100" step="1" value="{min_signal_strength}" />
	                </div>
	                <div style="grid-column:1 / -1;">
	                  <div class="muted">{"This field filters homepage candidates by how many recent snapshots tagged the stock as BUY inside the current window." if lang == "en" else "这个字段会按当前快照窗口内，这只股票最近被标记为 BUY 的次数来过滤首页候选。"}</div>
	                </div>
	                <datalist id="execution-tag-options">
	                  <option value="gap-risk"></option>
	                  <option value="earnings-soon"></option>
	                  <option value="thin-liquidity"></option>
	                </datalist>
	                <button type="submit">{_concept_tr(lang, 'apply_filters')}</button>
	              </form>
	            </details>
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
    heatmap_metric: str = "model",
    market_filter: str = "CN",
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
    market_filter = str(market_filter or "CN").strip().upper()
    if market_filter not in {"ALL", "CN", "US"}:
        market_filter = "CN"
    signal_filter = signal_filter.upper()
    heatmap_metric = (heatmap_metric or "model").strip().lower()
    if heatmap_metric not in {"model", "five_day", "breadth", "buy", "flow"}:
        heatmap_metric = "model"
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    heatmap_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_MARKET_HEATMAP_WORKSPACE)
    heatmap_payload = (heatmap_snapshot or {}).get("payload") if isinstance(heatmap_snapshot, dict) else None
    heatmap_ready = isinstance(heatmap_payload, dict) and isinstance(heatmap_payload.get("sector_heatmap"), list)
    heatmap_rows = list((heatmap_payload or {}).get("sector_heatmap") or [])
    if market_filter != "ALL":
        heatmap_rows = [item for item in heatmap_rows if str(item.get("market") or "").upper() == market_filter]
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
    def _heatmap_metric_value(item: dict) -> float | None:
        if heatmap_metric == "five_day":
            value = item.get("avg_move_5d")
            return None if value is None else float(value)
        if heatmap_metric == "breadth":
            value = item.get("breadth_pct")
            return None if value is None else float(value)
        if heatmap_metric == "buy":
            return float(item.get("buy_signal_count") or 0)
        if heatmap_metric == "flow":
            value = item.get("flow_proxy_score")
            return None if value is None else float(value)
        return max(float(item.get("max_signal_strength") or 0), float(item.get("avg_score") or 0))

    def _heatmap_metric_display(item: dict) -> str:
        value = _heatmap_metric_value(item)
        if value is None:
            return "-"
        if heatmap_metric == "five_day":
            return f"{'+' if value > 0 else ''}{value:.1f}%"
        if heatmap_metric == "breadth":
            return f"{value:.0f}% {'涨' if lang == 'zh' else 'up'}"
        if heatmap_metric == "buy":
            return f"{int(value)} {'买点' if lang == 'zh' else 'buy'}"
        if heatmap_metric == "flow":
            return f"{value:.0f}"
        return f"{value:.0f}"

    def _heatmap_weight(item: dict) -> float:
        hits = max(1, int(item.get("hits") or 0))
        buy_count = max(0, int(item.get("buy_signal_count") or 0))
        strength = max(0, int(item.get("max_signal_strength") or 0))
        return float((hits ** 1.18) + buy_count * 0.85 + strength / 22)

    def _heatmap_background(item: dict) -> str:
        value = _heatmap_metric_value(item)
        label = str(item.get("label") or "")
        palette = [
            ((34, 197, 94), (22, 101, 52)),
            ((45, 212, 191), (15, 118, 110)),
            ((96, 165, 250), (30, 64, 175)),
            ((168, 85, 247), (88, 28, 135)),
            ((251, 146, 60), (154, 52, 18)),
            ((244, 114, 182), (157, 23, 77)),
            ((250, 204, 21), (133, 77, 14)),
            ((56, 189, 248), (12, 74, 110)),
            ((129, 140, 248), (67, 56, 202)),
            ((74, 222, 128), (22, 101, 52)),
        ]
        palette_index = sum(ord(char) for char in label) % len(palette)
        primary, deep = palette[palette_index]

        def _category_gradient(level: float) -> str:
            level = max(0.18, min(1.0, level))
            p_alpha = 0.42 + level * 0.54
            d_alpha = 0.70 + level * 0.28
            shadow_alpha = 0.86 + level * 0.12
            return (
                f"linear-gradient(135deg, rgba({primary[0]},{primary[1]},{primary[2]},{p_alpha:.2f}) 0%, "
                f"rgba({deep[0]},{deep[1]},{deep[2]},{d_alpha:.2f}) 58%, "
                f"rgba(2,6,23,{shadow_alpha:.2f}) 100%)"
            )

        if value is None:
            return "linear-gradient(135deg, #64748b 0%, #334155 48%, #111827 100%)"
        if heatmap_metric == "five_day":
            return _category_gradient(0.28 + min(0.72, abs(value) / 18))
        if heatmap_metric == "breadth":
            return _category_gradient(0.22 + min(0.78, max(0.0, value) / 100))
        if heatmap_metric == "buy":
            max_buy = max([int(row.get("buy_signal_count") or 0) for row in heatmap_rows] or [1])
            return _category_gradient(0.25 + min(0.75, value / max(max_buy, 1)))
        if heatmap_metric == "flow":
            flow_values = [float(row.get("flow_proxy_score") or 0.0) for row in heatmap_rows if row.get("flow_proxy_score") is not None]
            min_flow = min(flow_values or [0.0])
            max_flow = max(flow_values or [100.0])
            normalized = (float(value) - min_flow) / max(max_flow - min_flow, 1.0)
            return _category_gradient(0.22 + normalized * 0.78)
        model_values = [
            max(float(row.get("max_signal_strength") or 0), float(row.get("avg_score") or 0))
            for row in heatmap_rows
        ]
        min_model = min(model_values or [0.0])
        max_model = max(model_values or [100.0])
        normalized = (float(value) - min_model) / max(max_model - min_model, 1.0)
        return _category_gradient(0.25 + normalized * 0.75)

    def _split_treemap(items: list[dict], x: float, y: float, width: float, height: float) -> list[dict]:
        if not items:
            return []
        if len(items) == 1:
            return [{**items[0], "_x": x, "_y": y, "_w": width, "_h": height}]
        total = sum(float(item.get("_weight") or 0) for item in items) or float(len(items))
        half = total / 2
        running = 0.0
        split_index = 1
        for index, item in enumerate(items[:-1], start=1):
            next_running = running + float(item.get("_weight") or 0)
            if abs(next_running - half) <= abs(running - half) or index == 1:
                running = next_running
                split_index = index
            else:
                break
        first = items[:split_index]
        second = items[split_index:]
        first_total = sum(float(item.get("_weight") or 0) for item in first) or 1.0
        ratio = max(0.08, min(0.92, first_total / total))
        if width >= height:
            first_width = width * ratio
            return _split_treemap(first, x, y, first_width, height) + _split_treemap(second, x + first_width, y, width - first_width, height)
        first_height = height * ratio
        return _split_treemap(first, x, y, width, first_height) + _split_treemap(second, x, y + first_height, width, height - first_height)

    for item in heatmap_rows:
        avg_move = item.get("avg_move_5d")
        breadth = item.get("breadth_pct")
        item["display_label"] = (
            f"{str(item.get('market') or '').upper()} · {item.get('label') or '-'}"
            if market_filter == "ALL"
            else item.get("label") or "-"
        )
        item["avg_move_5d_display"] = "-" if avg_move is None else f"{'+' if float(avg_move) > 0 else ''}{float(avg_move):.1f}%"
        item["breadth_display"] = "-" if breadth is None else f"{float(breadth):.0f}% {'涨' if lang == 'zh' else 'up'}"
        turnover_ratio = item.get("turnover_ratio_20d")
        signed_turnover = item.get("signed_turnover_pct")
        item["flow_ratio_display"] = "-" if turnover_ratio is None else f"{float(turnover_ratio):.2f}x"
        item["flow_signed_display"] = "-" if signed_turnover is None else f"{'+' if float(signed_turnover) > 0 else ''}{float(signed_turnover):.0f}%"
        item["execution_tags_display"] = " · ".join(item.get("execution_tags") or [])
        item["metric_display"] = _heatmap_metric_display(item)
        item["background"] = _heatmap_background(item)
        item["_weight"] = _heatmap_weight(item)
    heatmap_rows_for_map = heatmap_rows[:24]
    treemap_rows = _split_treemap(heatmap_rows_for_map, 0.0, 0.0, 100.0, 100.0)
    heatmap_tiles = "".join(
        f"<a href='{('/dashboard/continuous-leaders?' + urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'continuous_market': 'US'})) if str(item.get('market') or '').upper() == 'US' else ('/dashboard/concepts/' + str(item['slug']) + '?' + urlencode({'lookback_runs': lookback_runs, 'lang': lang, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter}))}' class='heat-tile' style='left:{item['_x']:.3f}%;top:{item['_y']:.3f}%;width:{item['_w']:.3f}%;height:{item['_h']:.3f}%;background:{item['background']};'>"
        f"<div><div class='heat-label'>{html.escape(str(item['display_label']))}</div><div class='heat-meta'>{'面积=命中密度' if lang == 'zh' else 'Size = hit density'} · {'颜色=' if lang == 'zh' else 'Color = '}{html.escape(_heatmap_metric_label(lang, heatmap_metric))}</div></div>"
        f"<div><div class='heat-metric'>{html.escape(str(item['metric_display']))}</div><div class='heat-meta'>{int(item.get('hits') or 0)} {'次命中' if lang == 'zh' else 'hit(s)'} · {'买点' if lang == 'zh' else 'Buy'} {int(item.get('buy_signal_count') or 0)} · {'最强' if lang == 'zh' else 'Max'} {int(item.get('max_signal_strength') or 0)}</div></div>"
        f"<div class='heat-meta heat-extra'>{(item['flow_ratio_display'] + ' · 净方向 ' + item['flow_signed_display']) if lang == 'zh' and heatmap_metric == 'flow' else ((item['flow_ratio_display'] + ' · Net ' + item['flow_signed_display']) if heatmap_metric == 'flow' else (item['avg_move_5d_display'] + ' · ' + item['breadth_display']))}</div>"
        f"<div class='heat-tags'>{''.join('<span>' + html.escape(str(tag)) + '</span>' for tag in (item.get('execution_tags') or [])[:3]) or ('<span>无执行提醒</span>' if lang == 'zh' else '<span>No tags</span>')}</div>"
        "</a>"
        for item in treemap_rows
    ) or f"<div class='muted'>{'暂无热力图数据，请先等待后台完成对应市场的预计算。' if lang == 'zh' else 'No heatmap data yet. Wait for the market precompute job to finish.'}</div>"
    market_rows = "".join(
        f"<tr><td>{item['market']}</td><td>{item['count']}</td></tr>"
        for item in (
            row for row in ((heatmap_payload or {}).get("market_distribution") or [])
            if market_filter == "ALL" or str(row.get("market") or "").upper() == market_filter
        )
    ) or f"<tr><td colspan='2'>{'热力图仍在后台预计算' if lang == 'zh' else 'Heatmap is still being precomputed'}</td></tr>"
    heatmap_scope_cards_html = ""
    all_heatmap_rows = list((heatmap_payload or {}).get("sector_heatmap") or []) if isinstance(heatmap_payload, dict) else []
    heatmap_scope_history = _recent_market_heat_history(db, limit=6)
    heatmap_scope_help = (
        (
            "口径说明：资金流代理不是主力净流入，而是把当日成交额相对近20日均值的放大倍数、上涨成交额占比、以及板块上涨广度合成为 0-100 分。适合看轮动热度，不适合替代真实席位资金。"
            if heatmap_metric == "flow"
            else "口径说明：这里的热度统一表示 0-100 的平均信号强度。热力图页面使用当前市场下各板块 `max_signal_strength` 的均值。"
        )
        if lang == "zh"
        else (
            "Methodology: flow proxy is not true institutional net flow. It combines turnover expansion vs the prior 20 sessions, the share of turnover on advancing names, and breadth into a 0-100 score. Use it for rotation heat, not broker-level money flow."
            if heatmap_metric == "flow"
            else "Methodology: heat is a unified 0-100 average signal-strength score. On the heatmap it is the mean `max_signal_strength` across tiles in the selected market."
        )
    )
    flow_rows = [item for item in heatmap_rows if item.get("flow_proxy_score") is not None]
    flow_snapshot_ready = any(
        any(key in item for key in ("flow_proxy_score", "turnover_ratio_20d", "signed_turnover_pct"))
        for item in heatmap_rows
    )
    flow_leader = max(
        flow_rows,
        key=lambda item: (float(item.get("flow_proxy_score") or 0.0), float(item.get("signed_turnover_pct") or 0.0)),
        default=None,
    )
    flow_laggard = min(
        flow_rows,
        key=lambda item: (float(item.get("signed_turnover_pct") or 0.0), float(item.get("flow_proxy_score") or 0.0)),
        default=None,
    )
    flow_summary_html = (
        (
            f"<div style='font-size:30px;font-weight:900;margin:6px 0;'>{float(flow_leader.get('flow_proxy_score') or 0.0):.0f}</div>"
            f"<div class='muted'>{'最热资金流代理' if lang == 'zh' else 'Top flow proxy'}: <strong>{html.escape(str(flow_leader.get('display_label') or flow_leader.get('label') or '-'))}</strong></div>"
            f"<div class='muted' style='margin-top:8px;'>{'放量' if lang == 'zh' else 'Turnover'} {html.escape(str(flow_leader.get('flow_ratio_display') or '-'))} · {'净方向' if lang == 'zh' else 'Net'} {html.escape(str(flow_leader.get('flow_signed_display') or '-'))}</div>"
            f"<div class='muted' style='margin-top:8px;'>{'降温板块' if lang == 'zh' else 'Cooling'}: <strong>{html.escape(str((flow_laggard or {}).get('display_label') or (flow_laggard or {}).get('label') or '-'))}</strong></div>"
        )
        if flow_leader
        else (
            f"<div class='muted'>{'当前热力图快照还是旧版本，资金流代理会在下一次市场热力图预计算后出现。' if lang == 'zh' else 'This heatmap snapshot is still on the older schema. Flow proxy will appear after the next market heatmap precompute refresh.'}</div>"
            if not flow_snapshot_ready
            else f"<div class='muted'>{'当前没有足够的资金流代理数据。' if lang == 'zh' else 'Not enough flow-proxy data yet.'}</div>"
        )
    )
    for scope_market, scope_label in (("CN", "A股" if lang == "zh" else "A-Shares"), ("US", "美股" if lang == "zh" else "U.S. Stocks")):
        scope_rows = [row for row in all_heatmap_rows if str(row.get("market") or "").upper() == scope_market]
        scope_history = heatmap_scope_history.get(scope_market) or []
        scope_hit_total = sum(int(row.get("hits") or 0) for row in scope_rows)
        scope_buy_total = sum(int(row.get("buy_signal_count") or 0) for row in scope_rows)
        scope_heat = round(
            sum(float(row.get("max_signal_strength") or 0.0) for row in scope_rows)
            / max(len(scope_rows), 1),
            1,
        ) if scope_rows else 0.0
        scope_top_label = (scope_rows[0].get("label") if scope_rows else None) or ("暂无" if lang == "zh" else "No leader")
        scope_delta = (scope_history[-1] - scope_history[0]) if len(scope_history) >= 2 else 0
        scope_delta_tone = "up" if scope_delta > 0 else "down" if scope_delta < 0 else "flat"
        scope_delta_label = (
            (f"+{scope_delta} {'升温' if lang == 'zh' else 'warming'}" if scope_delta > 0 else f"{scope_delta} {'降温' if lang == 'zh' else 'cooling'}")
            if scope_delta != 0
            else ("持平" if lang == "zh" else "Flat")
        )
        scope_href = f"/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'heatmap_metric': heatmap_metric, 'market_filter': scope_market, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}"
        heatmap_scope_cards_html += (
            f"<a class='market-scope-card{' active' if market_filter == scope_market else ''}' href='{scope_href}'>"
            f"<div class='market-scope-head'><strong>{scope_label}</strong><span>{_compact_label(str(scope_top_label), 18)}</span></div>"
            f"<div class='market-scope-headline'><span class='market-scope-chip {scope_delta_tone}'>{scope_delta_label}</span></div>"
            f"<div class='market-scope-stats'>"
            f"<div><b>{scope_hit_total}</b><span>{'命中' if lang == 'zh' else 'Hits'}</span></div>"
            f"<div><b>{scope_buy_total}</b><span>{'买点' if lang == 'zh' else 'Buy'}</span></div>"
            f"<div><b>{scope_heat:.1f}</b><span>{'热度' if lang == 'zh' else 'Heat'}</span></div>"
            f"</div>"
            f"<div class='market-scope-trend-wrap'><span>{'近6次快照热度' if lang == 'zh' else 'Last 6 snapshots'}</span>{_mini_trend_bars(scope_history, lang=lang)}</div>"
            f"<div class='market-scope-meta'>{'板块数' if lang == 'zh' else 'Tiles'} {len(scope_rows)} · {'标签数' if lang == 'zh' else 'Tags'} {sum(1 for row in scope_rows if row.get('execution_tags'))}</div>"
            "</a>"
        )
    lookback_pills = _lookback_pills("/dashboard/market/heatmap", selected=lookback_runs, extra_params={"lang": lang, "heatmap_sort": heatmap_sort, "heatmap_metric": heatmap_metric, "market_filter": market_filter, "signal_filter": signal_filter, "min_signal_strength": min_signal_strength, "min_buy_signal_count": min_buy_signal_count, "execution_tag_filter": execution_tag_filter, "exclude_execution_tag_filter": exclude_execution_tag_filter})
    market_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'heatmap_metric': heatmap_metric, 'market_filter': market, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if market_filter == market else ''}'>{label}</a>"
        for market, label in (
            ("ALL", "All Markets" if lang == "en" else "全部市场"),
            ("CN", "A-Shares" if lang == "en" else "A股"),
            ("US", "U.S." if lang == "en" else "美股"),
        )
    )
    heatmap_sort_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': mode, 'heatmap_metric': heatmap_metric, 'market_filter': market_filter, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if heatmap_sort == mode else ''}'>{label}</a>"
        for mode, label in (
            ("hits", _dt(lang, "sort_by_hits")),
            ("five_day", _dt(lang, "sort_by_5d")),
            ("breadth", _dt(lang, "sort_by_breadth")),
            ("score", _dt(lang, "sort_by_score")),
        )
    )
    heatmap_metric_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'heatmap_metric': mode, 'market_filter': market_filter, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if heatmap_metric == mode else ''}'>{_heatmap_metric_label(lang, mode)}</a>"
        for mode in ("model", "five_day", "breadth", "buy", "flow")
    )
    signal_pills = "".join(
        f"<a href='/dashboard/market/heatmap?{urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'heatmap_sort': heatmap_sort, 'heatmap_metric': heatmap_metric, 'market_filter': market_filter, 'signal_filter': mode, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}' class='compare-pill{' active' if signal_filter == mode else ''}'>{label}</a>"
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:none; margin:0; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .toolbar,.compare-row {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .pill,.compare-pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); font-size:13px; font-weight:700; text-decoration:none; }}
          .compare-pill.active {{ background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }}
		          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }}
		          .heat-stage {{ border-radius:26px; padding:14px; background:radial-gradient(circle at top left, rgba(61,217,182,0.12), transparent 32%), rgba(5,11,18,0.64); border:1px solid rgba(34,50,70,0.72); }}
		          .heat-grid {{ position:relative; height:min(680px, calc(100vh - 330px)); min-height:520px; margin-top:12px; border-radius:20px; overflow:hidden; background:#050b12; box-shadow:inset 0 0 0 1px rgba(255,255,255,0.04); }}
		          .heat-tile {{ position:absolute; color:#fff; border:3px solid #050b12; border-radius:11px; padding:13px; display:flex; flex-direction:column; justify-content:space-between; text-decoration:none; box-shadow:inset 0 1px 0 rgba(255,255,255,0.16),0 12px 28px rgba(0,0,0,0.28); transition:transform .16s ease, filter .16s ease, box-shadow .16s ease, border-color .16s ease; overflow:hidden; }}
		          .heat-tile:hover {{ z-index:5; transform:translateY(-2px) scale(1.012); filter:saturate(1.14) brightness(1.05); border-color:rgba(226,232,240,0.32); box-shadow:inset 0 1px 0 rgba(255,255,255,0.22),0 22px 42px rgba(0,0,0,0.38); }}
	          .heat-label {{ font-weight:900; line-height:1.25; font-size:15px; text-shadow:0 1px 1px rgba(0,0,0,0.22); }}
	          .heat-metric {{ font-size:24px; font-weight:950; letter-spacing:-0.04em; }}
	          .heat-meta {{ font-size:11px; opacity:0.9; line-height:1.35; }}
	          .heat-tags {{ display:flex; gap:5px; flex-wrap:wrap; margin-top:4px; }}
	          .heat-tags span {{ display:inline-flex; padding:3px 7px; border-radius:999px; background:rgba(255,255,255,0.12); font-size:10px; font-weight:800; }}
	          .heat-legend {{ display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-top:12px; color:var(--muted); font-size:12px; }}
	          .legend-scale {{ display:inline-grid; grid-template-columns:repeat(5,32px); gap:3px; align-items:center; }}
	          .legend-scale span {{ height:8px; border-radius:999px; }}
          .market-scope-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }}
          .market-scope-card {{ display:block; min-height:188px; padding:16px; border-radius:18px; background:rgba(11,19,29,0.74); border:1px solid rgba(34,50,70,0.92); transition:transform .16s ease, border-color .16s ease, background .16s ease; }}
          .market-scope-card:hover {{ transform:translateY(-1px); border-color:rgba(61,217,182,0.36); background:rgba(15,25,37,0.92); }}
          .market-scope-card.active {{ border-color:rgba(61,217,182,0.5); box-shadow:0 12px 28px rgba(0,0,0,0.16); }}
          .market-scope-head {{ display:flex; justify-content:space-between; gap:12px; align-items:baseline; }}
          .market-scope-head strong {{ font-size:16px; }}
          .market-scope-head span {{ color:var(--muted); font-size:11px; }}
          .market-scope-headline {{ display:flex; justify-content:flex-start; margin-top:10px; }}
          .market-scope-chip {{ display:inline-flex; align-items:center; padding:5px 9px; border-radius:999px; font-size:11px; font-weight:800; letter-spacing:0.02em; }}
          .market-scope-chip.up {{ background:rgba(74,222,128,0.14); color:#8af0a6; }}
          .market-scope-chip.down {{ background:rgba(255,107,129,0.14); color:#ff93a4; }}
          .market-scope-chip.flat {{ background:rgba(82,168,255,0.14); color:#89c2ff; }}
          .market-scope-stats {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:14px; }}
          .market-scope-stats b {{ display:block; font-size:22px; line-height:1; }}
          .market-scope-stats span {{ display:block; margin-top:6px; color:var(--muted); font-size:11px; }}
          .market-scope-meta {{ margin-top:10px; color:var(--muted); font-size:12px; }}
          .market-scope-help {{ margin-top:12px; padding:11px 12px; border-radius:14px; border:1px dashed rgba(61,217,182,0.24); background:rgba(61,217,182,0.06); color:var(--muted); font-size:12px; line-height:1.55; }}
          .market-scope-trend-wrap {{ margin-top:12px; }}
          .market-scope-trend-wrap span {{ display:block; color:var(--muted); font-size:11px; margin-bottom:8px; }}
          .mini-trend {{ height:34px; display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:5px; align-items:end; }}
          .mini-trend span {{ display:block; border-radius:999px 999px 3px 3px; background:linear-gradient(180deg, rgba(61,217,182,0.96), rgba(61,217,182,0.28)); box-shadow:0 6px 12px rgba(0,0,0,0.14); }}
          .mini-trend.empty {{ grid-template-columns:1fr; align-items:center; }}
          .mini-trend.empty span {{ height:auto; border-radius:0; background:none; box-shadow:none; color:var(--muted); font-size:11px; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          .ticker-links {{ max-width:280px; line-height:1.8; }}
          .ticker-links a {{ display:inline-flex; padding:2px 7px; margin:1px 2px 1px 0; border:1px solid rgba(61,217,182,0.22); border-radius:999px; background:rgba(61,217,182,0.08); color:#bff7eb; font-size:12px; font-weight:800; }}
          input, button {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); font:inherit; }}
          button {{ width:auto; background:var(--accent); color:#041119; font-weight:800; cursor:pointer; }}
          .sidebar-foot {{ margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }}
          h1 {{ margin:0 0 8px; font-size:38px; line-height:1.04; letter-spacing:-0.03em; }}
	          @media (max-width:1100px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:20px 16px 36px; }} .heat-grid {{ height:620px; min-height:620px; }} .heat-label {{ font-size:13px; }} .heat-metric {{ font-size:20px; }} .heat-extra,.heat-tags {{ display:none; }} }}
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
	            <a href="/dashboard/market?lang={lang}&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&market_filter={market_filter}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">← {'返回市场脉冲' if lang == 'zh' else 'Back to Market Pulse'}</a>
	            <a href="{('/dashboard/continuous-leaders?' + urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'continuous_market': 'US'})) if market_filter == 'US' else ('/dashboard/market/concepts?' + urlencode({'lang': lang, 'lookback_runs': lookback_runs, 'signal_filter': signal_filter, 'min_signal_strength': min_signal_strength, 'min_buy_signal_count': min_buy_signal_count, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter}) )}" class="pill">{('连续强势跟踪' if lang == 'zh' else 'Continuous Leaders') if market_filter == 'US' else _dt(lang, 'concept_activity_tracker')}</a>
	            <a href="/dashboard/market/heatmap?lang=en&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&heatmap_metric={heatmap_metric}&market_filter={market_filter}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">English</a>
	            <a href="/dashboard/market/heatmap?lang=zh&lookback_runs={lookback_runs}&heatmap_sort={heatmap_sort}&heatmap_metric={heatmap_metric}&market_filter={market_filter}&signal_filter={signal_filter}&min_signal_strength={min_signal_strength}&min_buy_signal_count={min_buy_signal_count}&execution_tag_filter={execution_tag_filter}&exclude_execution_tag_filter={exclude_execution_tag_filter}" class="pill">中文</a>
          </div>
          <div class="compare-row">{market_pills}</div>
          <div class="card">
            <div class="eyebrow">{('美股热力图' if lang == 'zh' else 'U.S. Heatmap') if market_filter == 'US' else _dt(lang, 'sector_heatmap')}</div>
            <h1 style="margin:0 0 8px;">{('美股热力图' if lang == 'zh' else 'U.S. Heatmap') if market_filter == 'US' else ('板块热力图' if lang == 'zh' else 'Sector Heatmap')}</h1>
            <p class="muted">{('聚焦美股行业分布、模型强度和持续强势名字。' if lang == 'zh' else 'Focus on U.S. sector distribution, model strength, and persistent leaders.') if market_filter == 'US' else _dt(lang, 'sector_heatmap_help')}</p>
          </div>
          {loading_hint}
	          <section class="card">
	            <div class="eyebrow">{_dt(lang, 'snapshot_window')}</div>
	            <div class="compare-row">{lookback_pills}</div>
	            <div class="eyebrow" style="margin-top:12px;">{'市场范围' if lang == 'zh' else 'Market Scope'}</div>
	            <div class="compare-row">{market_pills}</div>
	            <div class="eyebrow" style="margin-top:12px;">{'颜色指标' if lang == 'zh' else 'Color Metric'}</div>
	            <div class="compare-row">{heatmap_metric_pills}</div>
	            <div class="eyebrow" style="margin-top:12px;">{"Signal Focus" if lang == "en" else "信号聚焦"}</div>
	            <div class="compare-row">{signal_pills}</div>
	            <form action="/dashboard/market/heatmap" method="get" style="display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));align-items:end;">
	              <input type="hidden" name="lang" value="{lang}" />
	              <input type="hidden" name="lookback_runs" value="{lookback_runs}" />
	              <input type="hidden" name="heatmap_sort" value="{heatmap_sort}" />
	              <input type="hidden" name="heatmap_metric" value="{heatmap_metric}" />
	              <input type="hidden" name="market_filter" value="{market_filter}" />
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
	            <div class="heat-stage">
	              <div class="muted">{'面积代表模型命中数量；颜色代表当前选择的强弱指标。点击任意色块进入概念详情和股票明细。' if lang == 'zh' else 'Tile size represents model-hit density; color represents the selected strength metric. Click any tile for concept detail and ticker breakdown.'}</div>
	              <div class="heat-grid">{heatmap_tiles}</div>
	              <div class="heat-legend">
	                <span>{'当前颜色指标' if lang == 'zh' else 'Current color metric'}: <strong>{_heatmap_metric_label(lang, heatmap_metric)}</strong></span>
	                <span class="legend-scale"><span style="background:#7f1d1d;"></span><span style="background:#475569;"></span><span style="background:#0f766e;"></span><span style="background:#14b8a6;"></span><span style="background:#22c55e;"></span></span>
	              </div>
	            </div>
	          </section>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{_dt(lang, 'signal_distribution')}</div>
              <div class="market-scope-grid">{heatmap_scope_cards_html}</div>
              <div class="market-scope-help">{heatmap_scope_help}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{'资金流代理' if lang == 'zh' else 'Flow Proxy'}</div>
              {flow_summary_html}
              <div class="muted" style="margin-top:10px;">{'建议这样看：先用面积看板块被命中的密度，再切到资金流代理看哪些板块在放量升温。两者同时强，才更像有持续轮动支持。' if lang == 'zh' else 'Use tile size for hit density first, then switch color to flow proxy to see where turnover is expanding. The most actionable groups usually show both.'}</div>
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:none; margin:0; }}
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
    job_repo = DataJobRepository(db)
    latest_model = summary["latest_model"] or {}
    sync_overview = summary["sync_overview"] or {}
    pipeline_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_PIPELINE_STATUS)
    pipeline_payload = (pipeline_snapshot or {}).get("payload") if isinstance(pipeline_snapshot, dict) else None
    if isinstance(pipeline_payload, dict):
        recent_jobs = pipeline_payload.get("recent_jobs") or recent_jobs
    recent_jobs = list(recent_jobs or [])
    existing_job_types = {str(item.get("job_type") or "").strip().lower() for item in recent_jobs if isinstance(item, dict)}
    for required_job_type in (
        "screener_precompute",
        "screener_precompute_core",
        "screener_precompute_combos",
        "screener_precompute_rest",
        "model_selection_guidance_snapshot",
        "model_calibration_snapshot",
    ):
        if required_job_type.lower() in existing_job_types:
            continue
        latest_required_job = job_repo.get_latest_job(required_job_type)
        if latest_required_job:
            recent_jobs.append(latest_required_job)
            existing_job_types.add(required_job_type.lower())
    acceptance_snapshot = _build_acceptance_snapshot(db, lang=lang)
    model_health_rows = pipeline_payload.get("model_health") if isinstance(pipeline_payload, dict) else None
    anomaly_rows = pipeline_payload.get("anomalies") if isinstance(pipeline_payload, dict) else None
    lake_health = summary.get("lake_health") or {}
    close_review_status = close_review_scheduler_service.get_status()
    provider_strategy = _provider_strategy_view(lang)
    notifier = PushNotificationService()
    notification_channels = notifier.available_channels()
    dashboard_redirect = "/dashboard/ops?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    pipeline_news_meta = (pipeline_payload.get("news_market_meta") or {}) if isinstance(pipeline_payload, dict) else {}
    ai_send_status = str(acceptance_snapshot.get("ai_send_status") or "").strip().lower()
    ai_send_note = str(acceptance_snapshot.get("ai_send_note") or "").strip()
    ai_send_guard_html = ""
    ai_send_force_html = ""
    if ai_send_status in {"fallback", "not_ready"}:
        default_ai_send_note = (
            "今日 A股候选未完全就绪，默认不建议直接发送日报。"
            if lang == "zh"
            else "Today's A-share candidates are not fully ready, so direct sending is not recommended."
        )
        ai_send_guard_html = (
            f"<div class='subtle' style='margin-top:10px;color:#f6c177;'>"
            f"{html.escape(ai_send_note or default_ai_send_note)}"
            f"</div>"
        )
        ai_send_force_html = f"""
        <form action="/jobs/send-ai-daily-report" method="post">
          <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
          <input type="hidden" name="force_send" value="1" />
          <button class="cta" type="submit">{'仍然发送当前降级日报' if lang == 'zh' else 'Force Send Current Fallback Report'}</button>
        </form>
        """

    anomaly_rows = list(anomaly_rows or [])
    if int(lake_health.get("issue_count") or 0) > 0:
        examples = []
        runtime_skipped = False
        for issue in (lake_health.get("issues") or [])[:2]:
            examples.extend(list(issue.get("examples") or [])[:2])
            if str(issue.get("issue") or "") == "runtime_skipped_parquet":
                runtime_skipped = True
        anomaly_rows.insert(
            0,
            {
                "title": "Lake 文件异常" if lang == "zh" else "Lake file anomaly",
                "detail": (
                    (
                        f"检测到 {int(lake_health.get('issue_count') or 0)} 个异常 parquet 文件，其中部分已在运行时被自动跳过，建议清理或补刷。"
                        if runtime_skipped
                        else f"检测到 {int(lake_health.get('issue_count') or 0)} 个异常 parquet 文件，建议清理或补刷。"
                    )
                    if lang == "zh"
                    else (
                        f"Detected {int(lake_health.get('issue_count') or 0)} anomalous parquet files, including runtime-skipped partitions; clean up or refresh them."
                        if runtime_skipped
                        else f"Detected {int(lake_health.get('issue_count') or 0)} anomalous parquet files; clean up or refresh them."
                    )
                )
                + (f" 示例: {', '.join(examples)}" if examples else ""),
            },
        )

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
    cn_concept_sync_job = next((item for item in recent_jobs if str(item.get("job_type") or "").lower() == "sync_cn_concepts"), None)
    screener_precompute_job = next((item for item in recent_jobs if str(item.get("job_type") or "").lower() == "screener_precompute"), None)
    screener_precompute_core_job = _find_latest_job_by_type(recent_jobs, "screener_precompute_core")
    screener_precompute_combo_job = _find_latest_job_by_type(recent_jobs, "screener_precompute_combos")
    screener_precompute_rest_job = _find_latest_job_by_type(recent_jobs, "screener_precompute_rest")
    us_signal_train_job = _find_latest_job_by_type(recent_jobs, US_SIGNAL_TRAIN_JOB_TYPES)
    if us_signal_train_job is None:
        us_signal_train_job = job_repo.get_latest_job(US_SIGNAL_TRAIN_JOB_TYPES)
    model_selection_guidance_job = _find_latest_job_by_type(recent_jobs, "model_selection_guidance_snapshot")
    if model_selection_guidance_job is None:
        model_selection_guidance_job = job_repo.get_latest_job("model_selection_guidance_snapshot")
    model_calibration_job = _find_latest_job_by_type(recent_jobs, "model_calibration_snapshot")
    if model_calibration_job is None:
        model_calibration_job = job_repo.get_latest_job("model_calibration_snapshot")
    latest_cn_refresh = _latest_cn_refresh_summary(db, recent_jobs, lang=lang)
    screener_stage_rows = _build_screener_precompute_stage_rows(recent_jobs, lang=lang)

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
                "label": "概念同步" if lang == "zh" else "Concept Sync",
                "detail": _display_time((cn_concept_sync_job or {}).get("finished_at") or (cn_concept_sync_job or {}).get("started_at")),
                "message": (cn_concept_sync_job or {}).get("message") or (
                    "收盘后会同步自选、重点池和模型候选的 A 股概念映射。" if lang == "zh" else "After the close, CN concepts are synced for watchlist, focus pool, and model candidates."
                ),
                "status": _step_status_label(cn_concept_sync_job, "idle"),
            },
            {
                "label": "预计算总控" if lang == "zh" else "Staged Precompute",
                "detail": _display_time((screener_precompute_job or {}).get("finished_at") or (screener_precompute_job or {}).get("started_at")),
                "message": _summarize_screener_precompute_job(screener_precompute_job, lang=lang).get("detail") or (
                    "收盘后会把常用模型先跑一遍并缓存结果。" if lang == "zh" else "Common screener models are precomputed and cached after the close."
                ),
                "status": _step_status_label(screener_precompute_job, "idle"),
            },
            {
                "label": "核心预计算" if lang == "zh" else "Core Precompute",
                "detail": _display_time((screener_precompute_core_job or {}).get("finished_at") or (screener_precompute_core_job or {}).get("started_at")),
                "message": _summarize_screener_precompute_job(screener_precompute_core_job, lang=lang).get("detail"),
                "status": _step_status_label(screener_precompute_core_job, "idle"),
            },
            {
                "label": "组合预计算" if lang == "zh" else "Combo Precompute",
                "detail": _display_time((screener_precompute_combo_job or {}).get("finished_at") or (screener_precompute_combo_job or {}).get("started_at")),
                "message": _summarize_screener_precompute_job(screener_precompute_combo_job, lang=lang).get("detail"),
                "status": _step_status_label(screener_precompute_combo_job, "idle"),
            },
            {
                "label": "补全预计算" if lang == "zh" else "Rest Precompute",
                "detail": _display_time((screener_precompute_rest_job or {}).get("finished_at") or (screener_precompute_rest_job or {}).get("started_at")),
                "message": _summarize_screener_precompute_job(screener_precompute_rest_job, lang=lang).get("detail"),
                "status": _step_status_label(screener_precompute_rest_job, "idle"),
            },
            {
                "label": "美股训练" if lang == "zh" else "US Training",
                "detail": _display_time((us_signal_train_job or {}).get("finished_at") or (us_signal_train_job or {}).get("started_at")),
                "message": (us_signal_train_job or {}).get("message") or (
                    "美股收盘后会把 U.S. lake 股票池写入统一模型结果层。" if lang == "zh" else "After the U.S. close, the U.S. lake symbol pool is written into the unified model-result layer."
                ),
                "status": _step_status_label(us_signal_train_job, "idle"),
            },
            {
                "label": "模型使用指导" if lang == "zh" else "Model Guidance",
                "detail": _display_time((model_selection_guidance_job or {}).get("finished_at") or (model_selection_guidance_job or {}).get("started_at")),
                "message": (model_selection_guidance_job or {}).get("message") or (
                    "收盘后会把优先模型、优先组合和强票反向归因写入快照。" if lang == "zh" else "After the close, priority models, priority combos, and winner traceback are saved into a snapshot."
                ),
                "status": _step_status_label(model_selection_guidance_job, "idle"),
            },
            {
                "label": "模型样本外校准" if lang == "zh" else "Model OOS Calibration",
                "detail": _display_time((model_calibration_job or {}).get("finished_at") or (model_calibration_job or {}).get("started_at")),
                "message": (model_calibration_job or {}).get("message") or (
                    "收盘后会用历史预测落地表现校准 LightGBM 预期收益/回撤。" if lang == "zh" else "After the close, realized historical predictions calibrate LightGBM expected return and drawdown."
                ),
                "status": _step_status_label(model_calibration_job, "idle"),
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
        f"<div class='row-message'>{html.escape(_display_job_message(item.get('message'), lang=lang))}</div>"
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
    news_market_rows_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{'A股 / CN' if market == 'CN' else '美股 / US'}</div>"
        f"<div class='subtle'>"
        + (
            f"命中 {meta.get('matched_ticker_count', 0)}/{meta.get('ticker_count', 0)} 只，{meta.get('headline_total', 0)} 条新闻，覆盖率 {meta.get('coverage_pct', 0)}%。"
            if lang == 'zh'
            else f"Matched {meta.get('matched_ticker_count', 0)}/{meta.get('ticker_count', 0)} names, {meta.get('headline_total', 0)} headlines, {meta.get('coverage_pct', 0)}% coverage."
        )
        + "</div>"
        + (
            f"<div class='subtle'>{('Provider' if lang == 'en' else 'Provider')}: {meta.get('primary_provider') or ('-' if lang == 'en' else '-')}</div>"
            if meta.get("primary_provider")
            else ""
        )
        + (
            f"<div class='subtle'>{('来源' if lang == 'zh' else 'Sources')}: "
            + " · ".join(
                f"{item.get('source')}({item.get('count')})"
                for item in (meta.get('top_sources') or [])[:2]
                if item.get('source')
            )
            + "</div>"
            if meta.get("top_sources")
            else ""
        )
        + "</div>"
        f"<div class='row-right'><span class='status-pill {'success' if (meta.get('matched_ticker_count') or 0) > 0 else 'idle'}'>{meta.get('matched_ticker_count') or 0}</span></div>"
        "</article>"
        for market, meta in (pipeline_news_meta.items() if isinstance(pipeline_news_meta, dict) else [])
    ) or f"<div class='empty'>{'暂无分市场新闻覆盖统计' if lang == 'zh' else 'No per-market news coverage stats yet'}</div>"

    screener_precompute_result = (screener_precompute_job or {}).get("result") if screener_precompute_job else None
    if not isinstance(screener_precompute_result, dict):
        screener_precompute_result = {}
    screener_snapshots_created = screener_precompute_result.get("snapshots_created") or []
    if not isinstance(screener_snapshots_created, list):
        screener_snapshots_created = []
    us_signal_train_result = (us_signal_train_job or {}).get("result") if us_signal_train_job else None
    if not isinstance(us_signal_train_result, dict):
        us_signal_train_result = {}

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
            "label": "概念同步" if lang == "zh" else "Concept Sync",
            "value": (
                str(((cn_concept_sync_job or {}).get("result") or {}).get("rows_written"))
                if ((cn_concept_sync_job or {}).get("result") or {}).get("rows_written") is not None
                else ("待运行" if lang == "zh" else "Pending")
            ),
            "meta": (
                (cn_concept_sync_job or {}).get("message")
                or ("收盘后同步自选 + 候选股概念" if lang == "zh" else "Sync concepts for watchlist and candidates after the close")
            ),
        },
        {
            "label": "最近回测" if lang == "zh" else "Backtest",
            "value": _compact_run_name(latest_backtest.get("name") or ("暂无回测" if lang == "zh" else "No backtest"), 24),
            "meta": latest_backtest.get("status") or "-",
        },
        {
            "label": "模型预计算" if lang == "zh" else "Precompute",
            "value": (
                f"{len(screener_snapshots_created)}/"
                f"{len(screener_snapshots_created) + int(screener_precompute_result.get('failed_count') or 0)}"
                if screener_precompute_result
                else ("待运行" if lang == "zh" else "Pending")
            ),
            "meta": (
                _summarize_screener_precompute_job(screener_precompute_job, lang=lang).get("detail")
                or ("收盘后预跑常用模型" if lang == "zh" else "Precompute common screener models after the close")
            ),
        },
        {
            "label": "美股训练" if lang == "zh" else "US Train",
            "value": (
                str(us_signal_train_result.get("predictions_written"))
                if us_signal_train_result.get("predictions_written") is not None
                else ("待运行" if lang == "zh" else "Pending")
            ),
            "meta": (
                (f"{us_signal_train_result.get('ticker_count') or 0} {'只美股' if lang == 'zh' else 'U.S. symbols'} · "
                 f"{_display_time((us_signal_train_job or {}).get('finished_at') or (us_signal_train_job or {}).get('started_at'))}")
                if us_signal_train_job
                else ("收盘后自动训练美股信号" if lang == "zh" else "Auto-train U.S. signals after the close")
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
    close_review_actionable_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'><a href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker') or '-'}</a></div><div class='subtle'>{item.get('name') or item.get('ticker') or '-'}</div><div class='subtle'>{item.get('entry_trigger') or item.get('execution_note') or '-'}</div>"
        + (f"<div class='subtle' style='font-weight:800;color:#f59e0b;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div class='row-right'><span class='status-pill success'>{'主攻' if lang == 'zh' else 'Primary'}</span><span class='mini-metric'>{item.get('target_weight') or '-'}</span></div>"
        "</article>"
        for item in (close_review_action_feed.get("actionable") or [])[:4]
    ) or f"<div class='empty'>{'暂无主攻候选' if lang == 'zh' else 'No primary action candidates yet'}</div>"
    close_review_watch_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'><a href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker') or '-'}</a></div><div class='subtle'>{item.get('name') or item.get('ticker') or '-'}</div><div class='subtle'>{item.get('execution_note') or item.get('block_reason') or '-'}</div>"
        + (f"<div class='subtle' style='font-weight:800;color:#f59e0b;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div class='row-right'><span class='status-pill partial'>{'观察' if lang == 'zh' else 'Watch'}</span><span class='mini-metric'>{item.get('target_weight') or '-'}</span></div>"
        "</article>"
        for item in (close_review_action_feed.get("blocked") or [])[:4]
    ) or f"<div class='empty'>{'暂无只观察名单' if lang == 'zh' else 'No watch-only queue yet'}</div>"
    close_review_risk_reduce_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'><a href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker') or '-'}</a></div><div class='subtle'>{item.get('name') or item.get('ticker') or '-'}</div><div class='subtle'>{item.get('invalidation_condition') or item.get('execution_note') or '-'}</div></div>"
        f"<div class='row-right'><span class='status-pill failed'>{'减仓' if lang == 'zh' else 'Reduce'}</span><span class='mini-metric'>{item.get('target_weight') or '-'}</span></div>"
        "</article>"
        for item in (close_review_action_feed.get("risk_reduction") or [])[:4]
    ) or f"<div class='empty'>{'暂无减仓处理名单' if lang == 'zh' else 'No risk-reduction queue yet'}</div>"
    close_review_action_html = f"""
      <div>
        <div class="subtle" style="font-weight:700;margin-bottom:6px;">{'明日主攻' if lang == 'zh' else 'Primary Action'}</div>
        <div class="list-stack">{close_review_actionable_html}</div>
      </div>
      <div>
        <div class="subtle" style="font-weight:700;margin:14px 0 6px;">{'只观察' if lang == 'zh' else 'Watch Only'}</div>
        <div class="list-stack">{close_review_watch_html}</div>
      </div>
      <div>
        <div class="subtle" style="font-weight:700;margin:14px 0 6px;">{'减仓处理' if lang == 'zh' else 'Reduce Risk'}</div>
        <div class="list-stack">{close_review_risk_reduce_html}</div>
      </div>
    """
    screener_stage_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{item['label']}</div><div class='subtle'>{_display_time((item['job'] or {}).get('finished_at') or (item['job'] or {}).get('started_at'))}</div><div class='subtle'>{html.escape(item['detail'])}</div></div>"
        f"<div class='row-right'><span class='mini-metric'>{html.escape(item['summary'])}</span><span class='status-pill {item['status']}'>{_job_status_text(item['status'], lang)}</span></div>"
        "</article>"
        for item in screener_stage_rows
    ) or f"<div class='empty'>{'暂无预计算阶段记录' if lang == 'zh' else 'No precompute stage records yet'}</div>"
    model_selection_guidance_job = _find_latest_job_by_type(recent_jobs, "model_selection_guidance_snapshot")
    if model_selection_guidance_job is None:
        model_selection_guidance_job = job_repo.get_latest_job("model_selection_guidance_snapshot")
    screener_stage_actions_html = _render_screener_precompute_action_forms(
        lang=lang,
        redirect_to=dashboard_redirect,
        stage_rows=screener_stage_rows,
    )
    model_selection_guidance_actions_html = _render_model_selection_guidance_action_form(
        lang=lang,
        redirect_to=dashboard_redirect,
        job=model_selection_guidance_job,
    )
    acceptance_gaps_html = "".join(
        f"<article class='list-row'><div><div class='ticker'>{html.escape(str(item))}</div></div></article>"
        for item in (acceptance_snapshot.get("remaining_gaps") or [])
    ) or f"<div class='empty'>{'当前没有额外增强项' if lang == 'zh' else 'No additional enhancement items right now'}</div>"
    lake_runtime = (lake_health.get("runtime") or {}) if isinstance(lake_health, dict) else {}
    lake_query_stats = list(lake_runtime.get("query_stats") or [])
    lake_file_cache = list(lake_runtime.get("file_cache") or [])
    lake_issue_rows = list(lake_health.get("issues") or []) if isinstance(lake_health, dict) else []
    def _lake_query_status_view(status: str | None) -> tuple[str, str]:
        normalized = str(status or "").strip().lower()
        if normalized == "success":
            return ("成功" if lang == "zh" else "Success", "success")
        if normalized == "skipped_all_bad_files":
            return ("已跳过坏文件" if lang == "zh" else "Skipped Bad Files", "partial")
        if normalized in {"error", "failed"}:
            return ("失败" if lang == "zh" else "Failed", "failed")
        return ("待观察" if lang == "zh" else "Observe", "idle")
    lake_issue_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{html.escape(str(item.get('issue') or '-'))}</div>"
        f"<div class='subtle'>{html.escape(str((item.get('market') or 'MULTI')))}</div>"
        f"<div class='subtle'>{html.escape('；'.join(str(example) for example in (item.get('examples') or [])[:2]))}</div></div>"
        f"<div class='row-right'><span class='status-pill failed'>{int(item.get('count') or 0)}</span></div>"
        "</article>"
        for item in lake_issue_rows[:4]
    ) or f"<div class='empty'>{'当前没有 parquet 文件异常' if lang == 'zh' else 'No parquet file anomalies right now'}</div>"
    lake_query_stats_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{html.escape(str(item.get('label') or '-'))}</div>"
        f"<div class='subtle'>{'最近耗时' if lang == 'zh' else 'Last duration'}: {float(item.get('last_duration_ms') or 0.0):.1f} ms · "
        f"{'平均' if lang == 'zh' else 'Avg'} {float(item.get('avg_duration_ms') or 0.0):.1f} ms</div>"
        f"<div class='subtle'>{'文件' if lang == 'zh' else 'Files'} {int(item.get('last_file_count') or 0)} · "
        f"{'结果' if lang == 'zh' else 'Rows'} {int(item.get('last_row_count') or 0)} · "
        f"{'尝试' if lang == 'zh' else 'Attempts'} {int(item.get('last_attempt_count') or 0)}</div></div>"
        f"<div class='row-right'><span class='status-pill {_lake_query_status_view(item.get('last_status'))[1]}'>{_lake_query_status_view(item.get('last_status'))[0]}</span></div>"
        "</article>"
        for item in lake_query_stats[:5]
    ) or f"<div class='empty'>{'DuckDB 查询统计尚未产生' if lang == 'zh' else 'DuckDB query stats not populated yet'}</div>"
    lake_file_cache_html = "".join(
        "<article class='list-row'>"
        f"<div><div class='ticker'>{html.escape(str(item.get('market') or '-'))}</div>"
        f"<div class='subtle'>{'缓存文件数' if lang == 'zh' else 'Cached files'}: {int(item.get('file_count') or 0)}</div></div>"
        f"<div class='row-right'><span class='mini-metric'>{float(item.get('age_seconds') or 0.0):.1f}s</span></div>"
        "</article>"
        for item in lake_file_cache
    ) or f"<div class='empty'>{'当前没有活跃文件缓存' if lang == 'zh' else 'No active file-list cache right now'}</div>"
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
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
          .inline-actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }}
          .action-with-note {{ display:grid; gap:6px; align-content:start; }}
          .inline-form {{ display:inline-flex; margin:0; }}
          .cta {{ display:inline-flex; align-items:center; justify-content:center; padding:10px 14px; border-radius:999px; border:1px solid var(--line); background:rgba(21,34,49,0.92); color:var(--ink); font-size:13px; font-weight:800; }}
          .cta.primary {{ background:linear-gradient(135deg, rgba(61,217,182,0.28), rgba(82,168,255,0.24)); border-color:rgba(61,217,182,0.3); }}
          .cta.compact {{ padding:8px 12px; font-size:12px; }}
          .precompute-note {{ margin-top:10px; }}
          .action-receipt {{ max-width:260px; }}
          .playbook {{ margin-top:12px; padding:14px; border-radius:18px; background:rgba(21,34,49,0.82); border:1px solid var(--line); }}
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
                    <div class="subtle">{'A股基本面回填' if lang == 'zh' else 'CN Fundamental Sync'}</div>
                    <div class="ticker">{('开启' if auto_analysis.get('sync_cn_fundamentals') else '关闭') if lang == 'zh' else ('On' if auto_analysis.get('sync_cn_fundamentals') else 'Off')}</div>
                  </div>
                  <div>
                    <div class="subtle">{'A股概念回填' if lang == 'zh' else 'CN Concept Sync'}</div>
                    <div class="ticker">{('开启' if auto_analysis.get('sync_cn_concepts') else '关闭') if lang == 'zh' else ('On' if auto_analysis.get('sync_cn_concepts') else 'Off')}</div>
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
                  {ai_send_force_html}
                  <a class="cta" href="/dashboard/ai-daily-report?lang={lang}">{'打开 AI 日报' if lang == 'zh' else 'Open AI Report'}</a>
                </div>
                {ai_send_guard_html}
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
                  <span class="eyebrow">{'模型预计算分层' if lang == 'zh' else 'Precompute Stages'}</span>
                  <h2 class="section-title">{'A 股预计算卡在哪一层' if lang == 'zh' else 'Which precompute stage is blocked?'}</h2>
                  <p class="section-copy">{'先看总控是否跑完，再看核心模板、组合模板、补全模板分别有没有成功。这样你能马上区分是模型没训练、快照没生成，还是组合缺前置依赖。' if lang == 'zh' else 'Check the controller first, then core, combo, and rest stages. This lets you quickly tell model/training issues from missing snapshots or combo prerequisites.'}</p>
                  <div class="list-stack">{screener_stage_html}</div>
                  <div class="inline-actions">{screener_stage_actions_html}</div>
                  <div class="inline-actions" style="margin-top:12px;">{model_selection_guidance_actions_html}</div>
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
                  <span class="eyebrow">{'数据湖健康' if lang == 'zh' else 'Lake Health'}</span>
                  <h2 class="section-title">{'DuckDB / Parquet 当前状态' if lang == 'zh' else 'Current DuckDB / Parquet status'}</h2>
                  <p class="section-copy">{'这里主要看三件事：有没有坏文件、哪类查询最慢、文件列表缓存是否工作正常。页面慢或 job 慢时，先看这里。' if lang == 'zh' else 'Watch three things here: bad files, the slowest query classes, and whether the file-list cache is working. Start here when pages or jobs feel slow.'}</p>
                  <div class="chip-row" style="margin-bottom:12px;">
                    <span class="chip">{'异常文件' if lang == 'zh' else 'File issues'}: {int(lake_health.get('issue_count') or 0)}</span>
                    <span class="chip">{'慢查询类别' if lang == 'zh' else 'Query labels'}: {len(lake_query_stats)}</span>
                    <span class="chip">{'缓存市场数' if lang == 'zh' else 'Cached markets'}: {len(lake_file_cache)}</span>
                  </div>
                  <div class="list-stack">{lake_issue_html}</div>
                  <div class="list-stack" style="margin-top:14px;">{lake_query_stats_html}</div>
                  <div class="list-stack" style="margin-top:14px;">{lake_file_cache_html}</div>
                </article>

                <article class="card">
                  <span class="eyebrow">{'剩余增强项' if lang == 'zh' else 'Remaining Enhancements'}</span>
                  <h2 class="section-title">{'离完整终验还差什么' if lang == 'zh' else 'What is left for full sign-off?'}</h2>
                  <div class="list-stack">{acceptance_gaps_html}</div>
                </article>

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
                  <span class="eyebrow">{'新闻覆盖' if lang == 'zh' else 'News Coverage'}</span>
                  <h2 class="section-title">{'A股 / 美股新闻增强是否有产出' if lang == 'zh' else 'Is news enrichment producing output for CN and US?'}</h2>
                  <p class="section-copy">{'这块把新闻增强拆成 A股 / 美股 两条线，方便你判断到底是哪边 provider 在工作、哪边仍然偏弱。' if lang == 'zh' else 'This splits news enrichment into CN and US lanes so you can see which provider path is producing real output and which is still weak.'}</p>
                  <div class="list-stack">{news_market_rows_html}</div>
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
    cn_fundamental_job = next((item for item in recent_jobs if item["job_type"] == "sync_cn_fundamentals"), None)
    cn_concept_job = next((item for item in recent_jobs if item["job_type"] == "sync_cn_concepts"), None)
    def _load_cn_sync_stats() -> dict:
        symbol_repo = SymbolRepository(db)
        fundamental_repo = FundamentalSnapshotRepository(db)
        concept_repo = ConceptSnapshotRepository(db)
        technical_snapshot_repo = TechnicalSnapshotRepository(db)
        cn_symbols = [symbol for symbol in symbol_repo.list_symbols() if (symbol.market or "").upper() == "CN"]
        cn_ticker_set = {symbol.ticker for symbol in cn_symbols}
        cn_symbol_count = len(cn_symbols)
        cn_sync_success_count = sum(
            1 for item in sync_states if item["ticker"] in cn_ticker_set and item["status"] == "success"
        )
        cn_fundamentals = fundamental_repo.list_latest_for_market("CN")
        concept_summary = concept_repo.get_latest_summary()
        cn_technical_snapshot_count = len(technical_snapshot_repo.list_latest_for_market("CN"))
        cn_progress_pct = round((cn_sync_success_count / cn_symbol_count) * 100, 1) if cn_symbol_count else 0.0
        next_cn_offset = cn_sync_success_count
        default_cn_batch_size = min(500, max(100, cn_symbol_count - cn_sync_success_count)) if cn_symbol_count > cn_sync_success_count else 0
        return {
            "cn_symbol_count": cn_symbol_count,
            "cn_sync_success_count": cn_sync_success_count,
            "cn_technical_snapshot_count": cn_technical_snapshot_count,
            "cn_fundamental_snapshot_count": len(cn_fundamentals),
            "cn_concept_symbol_count": int(concept_summary.get("symbol_count") or 0),
            "cn_concept_latest_as_of_date": concept_summary.get("latest_as_of_date"),
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
    cn_fundamental_snapshot_count = int(cn_stats.get("cn_fundamental_snapshot_count") or 0)
    cn_concept_symbol_count = int(cn_stats.get("cn_concept_symbol_count") or 0)
    cn_concept_latest_as_of_date = str(cn_stats.get("cn_concept_latest_as_of_date") or "").strip() or "-"
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:1108px; margin:0 auto; }}
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
                + (
                    f"<p class='muted'>{'即使已配置 token，A 股概念同步还需要 TuShare 账号具备 ' if lang == 'zh' else 'Even with a token configured, CN concept sync also requires TuShare access to '}"
                    + "<code>concept_detail</code>"
                    + (
                        " 接口权限；否则系统会优雅降级为未配置，而不是把收盘链路打成失败。"
                        if lang == "zh"
                        else " permission; otherwise the system degrades gracefully to not_configured instead of failing the post-close pipeline."
                    )
                    + "</p>"
                )
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
            <div class="muted">{'基本面快照' if lang == 'zh' else 'Fundamental Snapshots'}: <strong>{cn_fundamental_snapshot_count}</strong></div>
            <div class="muted">{'概念覆盖股票' if lang == 'zh' else 'Concept-covered Symbols'}: <strong>{cn_concept_symbol_count}</strong> · {('截至 ' + cn_concept_latest_as_of_date) if lang == 'zh' else ('As of ' + cn_concept_latest_as_of_date)}</div>
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
            <div class="eyebrow">{'A股增强原料状态' if lang == 'zh' else 'CN Enrichment Inputs'}</div>
            <div class="muted">{'如果这里还是 0，当前 LightGBM 本质上仍是价格量能版。先把基本面和概念原料补齐，再看增强模型效果。' if lang == 'zh' else 'If these stay at 0, current LightGBM is still effectively price-and-volume only. Fill the fundamental and concept inputs first before judging the enriched model.'}</div>
            <div class="grid" style="margin-top:14px;">
              <article class="card" style="margin:0;padding:16px;border-radius:18px;">
                <div class="eyebrow">{'基本面快照' if lang == 'zh' else 'Fundamental Snapshots'}</div>
                <div style="font-size:28px;font-weight:900;line-height:1;">{cn_fundamental_snapshot_count}</div>
                <div class="muted" style="margin-top:8px;">{(cn_fundamental_job or {}).get('message') or ('还没有最近执行记录。' if lang == 'zh' else 'No recent run recorded yet.')}</div>
              </article>
              <article class="card" style="margin:0;padding:16px;border-radius:18px;">
                <div class="eyebrow">{'概念覆盖' if lang == 'zh' else 'Concept Coverage'}</div>
                <div style="font-size:28px;font-weight:900;line-height:1;">{cn_concept_symbol_count}</div>
                <div class="muted" style="margin-top:8px;">{(('最近日期 ' + cn_concept_latest_as_of_date) if lang == 'zh' else ('Latest as-of ' + cn_concept_latest_as_of_date)) if cn_concept_latest_as_of_date != '-' else ((cn_concept_job or {}).get('message') or ('还没有最近执行记录。' if lang == 'zh' else 'No recent run recorded yet.'))}</div>
              </article>
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
              <div class="eyebrow">{'训练美股模型' if lang == 'zh' else 'Train US Signals'}</div>
              <form action="/jobs/train-us-signals" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <div class="muted">{'从本地美股 Parquet market lake 读取股票池，正式写入 predictions / prediction_details。这样美股不再只是预计算候选，而是进入统一模型结果链路。' if lang == 'zh' else 'Read the U.S. symbol pool from the local U.S. Parquet market lake and write into predictions / prediction_details so U.S. names join the unified model-result pipeline.'}</div>
                <input type="text" name="run_name" value="us_close_lightgbm" />
                <input type="number" name="lookback_days" min="1" step="1" value="3" />
                <input type="number" name="top_n" min="1" step="1" value="5" />
                <button type="submit">{'训练美股信号' if lang == 'zh' else 'Train US Signals'}</button>
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
              <div class="muted" style="margin-bottom:10px;">{(cn_fundamental_job or {}).get('message') or ('当前库里还没有 A 股基本面快照，建议先跑一次。' if lang == 'zh' else 'No CN fundamental snapshots are in the database yet. Run this once first.')}</div>
              <form action="/jobs/sync-cn-fundamentals" method="post">
                <input type="hidden" name="redirect_to" value="{dashboard_redirect}" />
                <input type="text" name="tickers" placeholder="600519.SH,000001.SZ" />
                <button type="submit">{_dt(lang, 'sync_cn_fundamentals')}</button>
              </form>
              <div style="height:10px;"></div>
              <div class="eyebrow">{_dt(lang, 'sync_cn_concepts')}</div>
              <div class="muted" style="margin-bottom:10px;">{(cn_concept_job or {}).get('message') or ('当前库里还没有 A 股概念快照，建议在 TuShare 权限就绪后再跑。' if lang == 'zh' else 'No CN concept snapshots are in the database yet. Run this after TuShare concept permissions are ready.')}</div>
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
        f"<div class='tag'>{html.escape(format_trade_gate_reason(reason, lang=lang))}: {count}</div>"
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
        .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }} {WORKSPACE_SIDEBAR_STYLE}
        .main {{ padding:20px 18px 28px; }} .wrap {{ max-width:1108px; margin:0 auto; }} .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
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
            <input type="text" name="run_name" value="lightgbm_momentum" placeholder="{_dt(lang, 'run_name')}" />
            <input type="hidden" name="model_type" value="lightgbm" />
            <select name="signal_type"><option value="momentum">Momentum</option></select>
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
    screener_stage_rows = _build_screener_precompute_stage_rows(recent_jobs, lang=lang)
    dashboard_redirect = "/dashboard/ops/jobs?" + urlencode({"lang": lang, "lookback_runs": lookback_runs})
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops", lookback_runs=lookback_runs)
    def status_badge(status: str) -> str:
        tone = {"success":("#dcfce7","#166534"),"failed":("#fee2e2","#991b1b"),"partial":("#fef3c7","#92400e"),"running":("#dbeafe","#1d4ed8")}.get(status,("#e5e7eb","#374151"))
        return f"<span style='display:inline-block;padding:4px 8px;border-radius:999px;background:{tone[0]};color:{tone[1]};font-size:12px;font-weight:700;'>{status}</span>"

    def result_summary(job: dict) -> str:
        if str(job.get("job_type") or "").lower().startswith("screener_precompute"):
            summary = _summarize_screener_precompute_job(job, lang=lang)
            return f"{summary['summary']} · {summary['detail']}"
        result = job.get("result") or {}
        if not isinstance(result, dict):
            return "-"
        if "snapshots_created" in result:
            created = list(result.get("snapshots_created") or [])
            failed_count = int(result.get("failed_count") or 0)
            total = len(created) + failed_count
            return f"{len(created)}/{total}" if total else "-"
        if str(job.get("job_type") or "").lower() == "social_us_price_sync":
            success_count = int(result.get("success_count") or 0)
            failure_count = int(result.get("failure_count") or 0)
            failed_tickers = [str(item).upper() for item in (result.get("failed_tickers") or []) if str(item).strip()]
            base = (
                f"成功 {success_count} / 失败 {failure_count}"
                if lang == "zh"
                else f"{success_count} success / {failure_count} failed"
            )
            if failed_tickers:
                return base + (
                    f"；失败代码：{', '.join(failed_tickers[:5])}"
                    if lang == "zh"
                    else f"; failed tickers: {', '.join(failed_tickers[:5])}"
                )
            return base
        return _compact_json_summary(result, 48)

    screener_stage_summary_html = "".join(
        "<article style='padding:14px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:16px;align-items:flex-start;'>"
        f"<div><div style='font-weight:800;'>{item['label']}</div><div class='job-subtle'>{html.escape(item['detail'])}</div></div>"
        f"<div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end;'><span style='display:inline-flex;padding:4px 8px;border-radius:999px;background:rgba(82,168,255,0.12);color:#9acbff;font-size:12px;font-weight:700;'>{html.escape(item['summary'])}</span>{status_badge(item['status'])}</div>"
        "</article>"
        for item in screener_stage_rows
    ) or f"<div class='job-subtle'>{'暂无预计算阶段记录' if lang == 'zh' else 'No precompute stage records yet'}</div>"
    screener_stage_actions_html = _render_screener_precompute_action_forms(
        lang=lang,
        redirect_to=dashboard_redirect,
        compact=True,
        stage_rows=screener_stage_rows,
    )

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
        .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }} {WORKSPACE_SIDEBAR_STYLE}
        .main {{ padding:20px 18px 28px; }} .wrap {{ max-width:1108px; margin:0 auto; }} .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
        .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }} .pill {{ display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); text-decoration:none; font-size:13px; font-weight:700; }}
        .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
        .muted {{ color:var(--muted); font-size:14px; }} .job-subtle {{ margin-top:6px; color:var(--muted); font-size:12px; line-height:1.4; white-space:normal; }} .table-wrap {{ width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }} table {{ width:100%; min-width:960px; border-collapse:collapse; font-size:14px; }} th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }} th {{ color:var(--muted); font-weight:600; }} code {{ white-space:pre-wrap; word-break:break-word; }}
        .inline-actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; }} .action-with-note {{ display:grid; gap:6px; align-content:start; }} .inline-form {{ display:inline-flex; margin:0; }} .cta {{ display:inline-flex; align-items:center; justify-content:center; padding:10px 14px; border-radius:999px; border:1px solid var(--line); background:rgba(21,34,49,0.92); color:var(--ink); font-size:13px; font-weight:800; }} .cta.primary {{ background:linear-gradient(135deg, rgba(61,217,182,0.28), rgba(82,168,255,0.24)); border-color:rgba(61,217,182,0.3); }} .cta.compact {{ padding:8px 12px; font-size:12px; }} .precompute-note {{ margin-top:10px; }} .action-receipt {{ max-width:260px; }}
        h1 {{ margin:0 0 8px; font-size:36px; line-height:1.05; letter-spacing:-0.03em; }}
      </style></head>
      <body><div class="app"><aside class="sidebar"><div class="brand"><span class="brand-tag">PQW</span><h1>{'任务记录' if lang == 'zh' else 'Job History'}</h1><p>{'最近一次同步、训练、回测和失败信息，都在这里回看。' if lang == 'zh' else 'Review recent sync, training, backtest, and failure details here.'}</p></div><nav class="side-nav">{nav_html}</nav><div class="sidebar-foot">{'这页适合看参数、错误和状态，不适合承载复杂分析，所以保持轻量。' if lang == 'zh' else 'This page stays light and focuses on params, errors, and status history.'}</div></aside><main class="main"><div class="wrap">
        <div class="toolbar">
          <a href="/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}" class="pill">← {'返回运维操作台' if lang == 'zh' else 'Back to Operations'}</a>
          <a href="/dashboard/ops/jobs?lang=en&lookback_runs={lookback_runs}" class="pill">English</a>
          <a href="/dashboard/ops/jobs?lang=zh&lookback_runs={lookback_runs}" class="pill">中文</a>
        </div>
        <div class="card"><div class="eyebrow">{'任务记录' if lang == 'zh' else 'Job History'}</div><h1>{'最近任务与参数' if lang == 'zh' else 'Recent Jobs and Parameters'}</h1><p class="muted">{'单独查看任务成功、失败和参数详情。' if lang == 'zh' else 'Inspect recent success, failure, and run parameters in one place.'}</p></div>
        <section class="card"><div class="eyebrow">{'A股预计算分层' if lang == 'zh' else 'CN Precompute Stages'}</div><h1 style="font-size:26px;margin:0 0 8px;">{'先看是哪一层失败' if lang == 'zh' else 'See which stage failed first'}</h1><p class="muted">{'总控、核心、组合、补全四层结果在这里集中展开，便于快速判断是前置依赖没准备好，还是某一批模板本身失败。' if lang == 'zh' else 'Controller, core, combo, and rest stages are expanded here so you can quickly tell whether prerequisites are missing or a specific batch failed.'}</p>{screener_stage_summary_html}<div class="inline-actions">{screener_stage_actions_html}</div></section>
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
    watchlist_nlp_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_WATCHLIST_NLP)
    job_status = request.query_params.get("job_status")
    job_id = request.query_params.get("job_id")
    job_message = request.query_params.get("job_message")
    banner_html = ""
    if job_status or job_message:
        display_job_message = _display_job_message(job_message or "Completed", lang=lang)
        tone = {
            "success": ("#10261b", "#8af0a6"),
            "failed": ("#2b1520", "#ff93a4"),
            "partial": ("#2b2412", "#ffd982"),
        }.get(job_status or "", ("#172534", "#d7e2ec"))
        banner_html = (
            f"<div class='banner' style='background:{tone[0]};color:{tone[1]};'>"
            f"Job {job_id or '-'} · {job_status or 'done'} · {html.escape(display_job_message)}"
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
    dashboard_nlp_payload = ((dashboard_nlp_snapshot or {}).get("payload") if isinstance(dashboard_nlp_snapshot, dict) else {}) or {}
    watchlist_nlp_payload = ((watchlist_nlp_snapshot or {}).get("payload") if isinstance(watchlist_nlp_snapshot, dict) else {}) or {}
    if isinstance(dashboard_nlp_payload, dict) and not dashboard_nlp_payload.get("meta"):
        dashboard_nlp_payload = {
            **dashboard_nlp_payload,
            "meta": (
                watchlist_nlp_payload.get("meta")
                or summarize_news_rows(watchlist_nlp_payload.get("rows") or [])
            ),
        }
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
        nlp_payload=dashboard_nlp_payload,
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
        display_job_message = _display_job_message(job_message or "Completed", lang=lang)
        tone = {
            "success": ("#dcfce7", "#166534"),
            "failed": ("#fee2e2", "#991b1b"),
            "partial": ("#fef3c7", "#92400e"),
        }.get(job_status or "", ("#e5e7eb", "#374151"))
        banner_html = (
            f"<div style='margin-bottom:18px;padding:14px 16px;border-radius:16px;"
            f"background:{tone[0]};color:{tone[1]};font-weight:600;'>"
            f"Job {job_id or '-'} · {job_status or 'done'} · {html.escape(display_job_message)}"
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
          .hero-grid {{
            display:grid;
            grid-template-columns:minmax(0,1.35fr) minmax(320px,0.85fr);
            gap:16px;
            margin-bottom:16px;
          }}
          .hero-panel {{
            background:
              radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 28%),
              radial-gradient(circle at bottom left, rgba(82,168,255,0.10) 0, transparent 26%),
              linear-gradient(180deg, rgba(17,28,40,0.98) 0%, rgba(12,21,31,0.96) 100%);
            border:1px solid var(--line);
            border-radius:22px;
            padding:22px;
            box-shadow:0 18px 40px rgba(15,23,42,0.18);
          }}
          .hero-panel h1 {{
            margin:0 0 8px;
            font-size:40px;
            line-height:1.02;
            letter-spacing:-0.03em;
            max-width:12ch;
          }}
          .hero-copy {{
            max-width:720px;
            color:var(--muted);
            font-size:15px;
            line-height:1.6;
          }}
          .hero-actions {{
            display:flex;
            flex-wrap:wrap;
            gap:10px;
            margin-top:18px;
          }}
          .hero-cta {{
            display:inline-flex;
            align-items:center;
            justify-content:center;
            padding:10px 14px;
            border-radius:999px;
            text-decoration:none;
            font-size:13px;
            font-weight:800;
            border:1px solid var(--line);
          }}
          .hero-cta.primary {{
            background:#0f766e;
            color:#fff;
            border-color:#0f766e;
          }}
          .hero-cta.secondary {{
            background:rgba(255,255,255,0.04);
            color:var(--ink);
          }}
          .hero-cta.ghost {{
            background:#eef8f5;
            color:#0f766e;
            border-color:#cde9e4;
          }}
          .hero-strip {{
            display:grid;
            grid-template-columns:repeat(3,minmax(0,1fr));
            gap:10px;
            margin-top:18px;
          }}
          .hero-strip-item {{
            border:1px solid rgba(255,255,255,0.06);
            border-radius:16px;
            background:rgba(255,255,255,0.03);
            padding:12px 13px;
          }}
          .hero-strip-label {{
            color:var(--muted);
            font-size:11px;
            font-weight:800;
            letter-spacing:0.06em;
            text-transform:uppercase;
            margin-bottom:6px;
          }}
          .hero-strip-value {{
            color:var(--ink);
            font-size:18px;
            font-weight:800;
            line-height:1.2;
          }}
          .hero-side {{
            display:grid;
            gap:12px;
          }}
          .desk-chip-row {{
            display:flex;
            flex-wrap:wrap;
            gap:8px;
            margin-top:12px;
          }}
          .desk-chip {{
            display:inline-flex;
            align-items:center;
            padding:6px 10px;
            border-radius:999px;
            background:rgba(255,255,255,0.05);
            border:1px solid rgba(255,255,255,0.06);
            color:var(--ink);
            font-size:12px;
            font-weight:700;
          }}
          .desk-metrics {{
            display:grid;
            grid-template-columns:repeat(4,minmax(0,1fr));
            gap:12px;
            margin-bottom:16px;
          }}
          .desk-metric {{
            border:1px solid var(--line);
            border-radius:18px;
            padding:14px 16px;
            background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94));
            box-shadow:0 12px 28px rgba(15,23,42,0.12);
          }}
          .desk-metric .metric {{
            font-size:26px;
            margin:4px 0 6px;
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
          @media (max-width: 1120px) {{
            .hero-grid {{ grid-template-columns:1fr; }}
            .desk-metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
          }}
          @media (max-width: 720px) {{
            .hero-panel h1 {{ font-size:32px; max-width:none; }}
            .hero-strip {{ grid-template-columns:1fr; }}
            .desk-metrics {{ grid-template-columns:1fr; }}
          }}
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
          <section class="hero-grid">
            <article class="hero-panel">
              <div class="eyebrow">{'今日判断台' if lang == 'zh' else 'Decision Desk'}</div>
              <h1>{_dt(lang, 'title')}</h1>
              <p class="hero-copy">{_dt(lang, 'lead')}</p>
              {banner_html}
              <div class="hero-actions">
                <a class="hero-cta primary" href="/watchlist?lang={lang}&mode={session_mode}">{_dt(lang, 'open_watchlist')}</a>
                <a class="hero-cta secondary" href="/screeners?lang={lang}">{_dt(lang, 'open_screener')}</a>
                <a class="hero-cta secondary" href="/dashboard/model-performance?lang={lang}&market=ALL">{'模型评测总览' if lang == 'zh' else 'Model Evaluation Overview'}</a>
                <a class="hero-cta ghost" href="/screeners/market-snapshot?lang={lang}&mode={session_mode}">{'市场快照榜单' if lang == 'zh' else 'Market Snapshot'}</a>
              </div>
              <div class="hero-strip">
                <div class="hero-strip-item">
                  <div class="hero-strip-label">{'自动分析' if lang == 'zh' else 'Auto Analysis'}</div>
                  <div class="hero-strip-value">{_dt(lang, 'on') if auto_analysis['enabled'] else _dt(lang, 'off')}</div>
                  <div class="muted">{_dt(lang, 'next_run')}: {auto_analysis['next_run_at'] or '-'}</div>
                </div>
                <div class="hero-strip-item">
                  <div class="hero-strip-label">{'最新模型' if lang == 'zh' else 'Latest Model'}</div>
                  <div class="hero-strip-value" title="{latest_model['name'] if latest_model else 'None'}">{_compact_run_name((latest_model or {}).get('name'), 20) if latest_model else 'None'}</div>
                  <div class="muted">{_dt(lang, 'status')}: {latest_model['status'] if latest_model else '-'}</div>
                </div>
                <div class="hero-strip-item">
                  <div class="hero-strip-label">{'回测状态' if lang == 'zh' else 'Backtest'}</div>
                  <div class="hero-strip-value">{latest_backtest['status'] if latest_backtest else 'None'}</div>
                  <div class="muted">{_dt(lang, 'period')}: {latest_backtest['start_date'] if latest_backtest else '-'} → {latest_backtest['end_date'] if latest_backtest else '-'}</div>
                </div>
              </div>
            </article>
            <div class="hero-side">
              <article class="hero-panel">
                <div class="eyebrow">{'会话与刷新' if lang == 'zh' else 'Mode and Refresh'}</div>
                <div class="metric">{'盘前' if session_mode == 'premarket' and lang == 'zh' else '盘中观察' if session_mode == 'monitor' and lang == 'zh' else '盘后复盘' if lang == 'zh' else 'Premarket' if session_mode == 'premarket' else 'Monitor' if session_mode == 'monitor' else 'Postmarket'}</div>
                <div class="muted">{'先定工作模式，再看市场快照、自选和持仓，能明显减少页面切换。' if lang == 'zh' else 'Set the working mode first, then move through snapshot, watchlist, and portfolio with less context switching.'}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;">{mode_switch}</div>
                <div class="desk-chip-row">
                  <span class="pill" id="refresh-label">{'每 10 秒自动刷新' if lang == 'zh' else 'Auto-refresh every 10s'}</span>
                  <label class="desk-chip" style="gap:8px;">
                    <input type="checkbox" id="auto-refresh" checked style="width:auto;" />
                    {'自动刷新' if lang == 'zh' else 'Auto refresh'}
                  </label>
                  <span class="desk-chip">{'最近更新' if lang == 'zh' else 'Last updated'}: {generated_at}</span>
                </div>
                <div class="hero-actions" style="margin-top:14px;">
                  <button id="refresh-now" type="button" style="width:auto;">{'立即刷新' if lang == 'zh' else 'Refresh Now'}</button>
                  <a class="hero-cta secondary" href="/dashboard/data-sources?lang={lang}">{_dt(lang, 'data_sources')}</a>
                  <a class="hero-cta secondary" href="/logout">{_dt(lang, 'logout')}</a>
                </div>
                <div class="desk-chip-row">{lang_switch}</div>
              </article>
              <article class="hero-panel">
                <div class="eyebrow">{_dt(lang, 'stock_insight_search')}</div>
                <div class="muted">{'直接输入股票代码，快速跳到单票分析页。' if lang == 'zh' else 'Jump straight into the single-name insight page by ticker.'}</div>
                <form action="/insights/open" method="get" style="display:grid;gap:10px;margin-top:12px;">
                  <input type="hidden" name="lang" value="{lang}" />
                  <input type="text" name="ticker" placeholder="{_dt(lang, 'search_placeholder')}" />
                  <button type="submit">{_dt(lang, 'open_insight_page')}</button>
                  <span class="muted">{_dt(lang, 'search_help')}</span>
                </form>
              </article>
            </div>
          </section>

          <section class="desk-metrics">
            <article class="desk-metric">
              <div class="eyebrow">{_dt(lang, 'auto_analysis')}</div>
              <div class="metric">{_dt(lang, 'on') if auto_analysis['enabled'] else _dt(lang, 'off')}</div>
              <div class="muted">{_dt(lang, 'every_hours', hours=auto_analysis['interval_hours'])}</div>
              <div class="muted">{_dt(lang, 'next_run')}: {auto_analysis['next_run_at'] or '-'}</div>
            </article>
            <article class="desk-metric">
              <div class="eyebrow">{_dt(lang, 'data_source')}</div>
              <div class="metric">{data_sources['primary_provider'] or 'None'}</div>
              <div class="muted">{_dt(lang, 'current_dominant_provider')}</div>
              <div class="muted">{_dt(lang, 'concept_data_note', freshness=data_sources['concept_data']['freshness'], as_of=data_sources['concept_data']['latest_as_of_date'] or '-')}</div>
            </article>
            <article class="desk-metric">
              <div class="eyebrow">{_dt(lang, 'latest_model')}</div>
              <div class="metric" title="{latest_model['name'] if latest_model else 'None'}">{_compact_run_name((latest_model or {}).get('name'), 20) if latest_model else 'None'}</div>
              <div class="muted">{_dt(lang, 'status')}: {latest_model['status'] if latest_model else '-'}</div>
              <div class="muted">{_dt(lang, 'type')}: {latest_model['model_type'] if latest_model else '-'}</div>
            </article>
            <article class="desk-metric">
              <div class="eyebrow">{_dt(lang, 'backtest')}</div>
              <div class="metric">{latest_backtest['status'] if latest_backtest else 'None'}</div>
              <div class="muted" title="{latest_backtest['name'] if latest_backtest else '-'}">{_dt(lang, 'run')}: {_compact_run_name((latest_backtest or {}).get('name'), 20) if latest_backtest else '-'}</div>
              <div class="muted">{_dt(lang, 'period')}: {latest_backtest['start_date'] if latest_backtest else '-'} → {latest_backtest['end_date'] if latest_backtest else '-'}</div>
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
              <a class="nav-card" href="/dashboard/premarket-plan?lang={lang}">
                <div class="nav-kicker">3</div>
                <div class="nav-title">{'看盘前便签' if lang == 'zh' else 'Read Premarket Plan'}</div>
                <div class="muted">{'手机端先看触发、放弃条件、仓位和风险。' if lang == 'zh' else 'Start with triggers, invalidation, sizing, and risk on mobile.'}</div>
              </a>
              <a class="nav-card" href="/dashboard/realtime-monitor?lang={lang}">
                <div class="nav-kicker">4</div>
                <div class="nav-title">{'重点监控台' if lang == 'zh' else 'Live Monitor'}</div>
                <div class="muted">{'只盯自选、持仓和 AI 候选，快速判断买入区/风险位。' if lang == 'zh' else 'Track only watchlist, portfolio, and AI candidates for buy-zone/risk changes.'}</div>
              </a>
              <a class="nav-card" href="/portfolio">
                <div class="nav-kicker">5</div>
                <div class="nav-title">{'检查持仓' if lang == 'zh' else 'Check Portfolio'}</div>
                <div class="muted">{'结合盈亏、成本和 AI 策略复核持仓。' if lang == 'zh' else 'Review live positions with PnL, cost basis, and AI posture.'}</div>
              </a>
              <a class="nav-card" href="/settings/notifications">
                <div class="nav-kicker">6</div>
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
                <a class="action-link" href="/dashboard/weekly-review?lang={lang}">{'打开每周复盘' if lang == 'zh' else 'Open Weekly Review'}</a>
                <a class="action-link" href="/dashboard/continuous-leaders?lang={lang}&lookback_runs={lookback_runs}">{'打开连续强势股' if lang == 'zh' else 'Open Continuous Leaders'}</a>
                <a class="action-link" href="/watchlist?lang={lang}&mode={session_mode}">{_dt(lang, 'open_watchlist')}</a>
                <a class="action-link" href="/screeners/market-snapshot?lang={lang}&mode={session_mode}">{'打开市场快照榜单' if lang == 'zh' else 'Open Market Snapshot'}</a>
                <a class="action-link" href="/dashboard/premarket-plan?lang={lang}">{'打开盘前便签' if lang == 'zh' else 'Open Premarket Plan'}</a>
                <a class="action-link" href="/dashboard/realtime-monitor?lang={lang}">{'打开重点监控台' if lang == 'zh' else 'Open Live Monitor'}</a>
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
    selection_guidance = load_model_selection_guidance_snapshot(db, market="CN", allow_fallback=True)
    selection_guidance_summary = summarize_model_selection_guidance(selection_guidance, lang=lang)
    return _render_dashboard_top_fragment(
        lang=lang,
        latest_signals=summary["latest_signals"],
        latest_model=summary["latest_model"],
        risk_overview=summary["market_context"].get("risk_overview", {}),
        selection_guidance_summary=selection_guidance_summary,
    )


@router.get("/weekly-review", response_class=HTMLResponse)
def dashboard_weekly_review(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/weekly-review")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    summary = _build_weekly_review_summary(db, lang=lang)
    mood_counts = summary.get("mood_counts") or {}
    mood_chips = "".join(
        f"<span class='pill'>{html.escape(str(label))} · {int(count)}</span>"
        for label, count in sorted(mood_counts.items(), key=lambda item: (-int(item[1]), str(item[0])))
    ) or f"<span class='muted'>{'本周还没有日报记录。' if lang == 'zh' else 'No AI report entries this week.'}</span>"
    repeated_rows_html = "".join(
        "<tr>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{int(item.get('count') or 0)}</td>"
        f"<td>{html.escape(str(item.get('latest_verdict') or '-'))}</td>"
        "</tr>"
        for item in (summary.get('repeated_top_tickers') or [])
    ) or f"<tr><td colspan='3'>{'本周没有重复入选的 Top 候选。' if lang == 'zh' else 'No repeated Top picks this week.'}</td></tr>"
    run_rows_html = "".join(
        "<tr>"
        f"<td><a href='/dashboard/model-performance?lang={lang}&run_id={int(item.get('id') or 0)}'>#{int(item.get('id') or 0)}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('market') or '-'))}<div class='muted'>{html.escape(str(item.get('latest_trade_date') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('window_3') or {}).get('avg_return')), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('window_3') or {}).get('hit_rate')), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('window_5') or {}).get('avg_return')), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('window_5') or {}).get('hit_rate')), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('window_10') or {}).get('avg_return')), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('window_10') or {}).get('hit_rate')), suffix='%', digits=1)}</div></td>"
        "</tr>"
        for item in (summary.get('run_rows') or [])
    ) or f"<tr><td colspan='5'>{'本周还没有成功模型 run。' if lang == 'zh' else 'No successful model runs this week.'}</td></tr>"
    weekly_jobs_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('job_type') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('status') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('started_at') or '-'))}</td>"
        f"<td>{html.escape(_display_job_message(item.get('message'), lang=lang))}</td>"
        "</tr>"
        for item in (summary.get('partial_or_failed_jobs') or [])
    ) or f"<tr><td colspan='4'>{'本周没有失败或部分完成的任务。' if lang == 'zh' else 'No failed or partial jobs this week.'}</td></tr>"
    trade_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}<div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(item.get('price'), digits=3)}</td>"
        f"<td>{_fmt_optional_float(item.get('quantity'), digits=0)}</td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl'), digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl_pct'), suffix='%', digits=2)}</td>"
        f"<td>{html.escape(trade_reason_label(item.get('reason'), lang=lang))}</td>"
        "</tr>"
        for item in (summary.get('trade_rows') or [])
    ) or f"<tr><td colspan='7'>{'本周还没有卖出记录。' if lang == 'zh' else 'No portfolio sell records this week.'}</td></tr>"
    advice_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('bucket_label') or '-'))}</td>"
        f"<td>{int(item.get('count') or 0)}</td>"
        f"<td>{int(item.get('winner_count') or 0)}</td>"
        f"<td>{_fmt_optional_float(item.get('win_rate'), suffix='%', digits=1)}</td>"
        f"<td>{_fmt_optional_float(item.get('avg_return'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl'), digits=2)}</td>"
        "</tr>"
        for item in (summary.get('advice_effectiveness_rows') or [])
    ) or f"<tr><td colspan='6'>{'本周还没有足够的卖出记录来评估建议有效性。' if lang == 'zh' else 'Not enough portfolio exits this week to evaluate advice effectiveness yet.'}</td></tr>"
    unresolved_trade_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}<div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl'), digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('realized_pnl_pct'), suffix='%', digits=2)}</td>"
        f"<td><a class='pill' href='/portfolio?lang={lang}'>{'去持仓页补录' if lang == 'zh' else 'Fix in Portfolio'}</a></td>"
        "</tr>"
        for item in (summary.get('unresolved_trade_rows') or [])
    ) or f"<tr><td colspan='5'>{'本周没有待补录原因的卖出记录。' if lang == 'zh' else 'No weekly sell records are missing a structured reason.'}</td></tr>"
    audited_trade_rows_html = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('ticker') or '-'))}<div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('action_hint_at_exit') or '-'))}<div class='muted'>{html.escape(str(item.get('action_priority_at_exit') or '-'))} · {html.escape(str(item.get('risk_tag_at_exit') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('action_reason_at_exit') or '-'))}<div class='muted'>{html.escape(str(item.get('rebalance_action_at_exit') or '-'))}</div></td>"
            f"<td>{html.escape(trade_reason_label(item.get('reason'), lang=lang))}</td>"
            f"<td>{html.escape(_audit_conclusion_for_trade(item, lang=lang)[0])}<div class='muted'>{html.escape(_audit_conclusion_for_trade(item, lang=lang)[1])}</div></td>"
            f"<td>{_fmt_optional_float(item.get('realized_pnl_pct'), suffix='%', digits=2)}<div class='muted'>"
            f"3D {_fmt_optional_float(item.get('post_sell_return_3d'), suffix='%', digits=2)} · "
            f"5D {_fmt_optional_float(item.get('post_sell_return_5d'), suffix='%', digits=2)} · "
            f"10D {_fmt_optional_float(item.get('post_sell_return_10d'), suffix='%', digits=2)}</div></td>"
            "</tr>"
        )
        for item in (summary.get('audited_trade_rows') or [])
    ) or f"<tr><td colspan='7'>{'本周还没有带建议快照的卖出记录。新卖出会自动进入审计链。' if lang == 'zh' else 'No weekly exits carry an advice snapshot yet. New exits will enter the audit chain automatically.'}</td></tr>"
    job_counts = summary.get("job_status_counts") or {}
    model_windows = summary.get("model_window_summary") or {}
    trade_summary = summary.get("trade_summary") or {}
    audit_summary = summary.get("audit_summary") or {}
    structured_reason_summary = summary.get("structured_reason_summary") or {}
    report_window_summary = summary.get("report_window_summary") or {}
    recommendation_validation_rows_html = "".join(
        "<tr>"
        f"<td><a href='{html.escape(str(item.get('href') or '#'), quote=True)}'>{html.escape(str(item.get('label') or '-'))}</a><div class='muted'>{html.escape(str(item.get('title') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('note') or '-'))}</td>"
        f"<td>{_fmt_optional_float(((item.get('windows') or {}).get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('windows') or {}).get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('windows') or {}).get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('windows') or {}).get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('windows') or {}).get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('windows') or {}).get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(((item.get('windows') or {}).get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((item.get('windows') or {}).get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        "</tr>"
        for item in (summary.get("recommendation_validation_rows") or [])
    ) or f"<tr><td colspan='6'>{'本周还没有足够的推荐验证样本。' if lang == 'zh' else 'Not enough recommendation validation samples yet this week.'}</td></tr>"
    structured_reason_note = ""
    coverage_pct = structured_reason_summary.get("coverage_pct")
    if coverage_pct is not None and float(coverage_pct) < 60.0:
        structured_reason_note = (
            "<div class='muted' style='margin-top:8px;color:#f6c177;'>"
            + (
                f"当前仅有 {float(coverage_pct):.1f}% 的本周卖出记录带结构化原因，历史旧记录会暂时落到“其他”，这会降低本模块的解释力。"
                if lang == "zh"
                else f"Only {float(coverage_pct):.1f}% of this week's exits carry a structured reason. Older records will stay in 'Other' for now, so interpret this section cautiously."
            )
            + "</div>"
        )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'每周复盘' if lang == 'zh' else 'Weekly Review'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:1108px; margin:0 auto; }}
          .toolbar,.stackline {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }}
          .pill {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }}
          .card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          .muted {{ color:var(--muted); font-size:14px; line-height:1.55; }}
          h2 {{ margin:0 0 8px; font-size:30px; }}
          .metric-grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }}
          .metric-card {{ border:1px solid var(--line); border-radius:18px; padding:16px; background:rgba(11,19,29,0.78); }}
          .metric {{ font-size:30px; font-weight:800; margin:6px 0; }}
          .table-wrap {{ width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }}
          table {{ width:100%; min-width:860px; border-collapse:collapse; font-size:14px; }}
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
              <h1>{'每周复盘' if lang == 'zh' else 'Weekly Review'}</h1>
              <p>{'把本周日报、模型表现、持仓卖出和任务质量合到一页，先形成第一版周报。' if lang == 'zh' else 'Pull the week’s reports, model performance, portfolio exits, and task quality into one first-pass weekly review.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to Dashboard'}</a>
                <a class="pill" href="/dashboard/model-performance?lang={lang}">{'模型评测总览' if lang == 'zh' else 'Model Evaluation Overview'}</a>
                <a class="pill" href="/dashboard/ai-daily-report/history?lang={lang}">{'日报历史' if lang == 'zh' else 'Report History'}</a>
              </div>
              <section class="card">
                <div class="eyebrow">{'每周复盘' if lang == 'zh' else 'Weekly Review'}</div>
                <h2>{html.escape(str(summary.get('week_start') or '-'))} ~ {html.escape(str(summary.get('week_end') or '-'))}</h2>
                <div class="muted">{'这一版周报优先回答四件事：本周模型有没有用、本周反复出现了什么股票、本周持仓卖出结果如何、本周 job 是否稳定。' if lang == 'zh' else 'This first version focuses on four questions: did the model work this week, which names kept resurfacing, how did portfolio exits do, and were jobs stable.'}</div>
              </section>
              <section class="metric-grid">
                <article class="metric-card">
                  <div class="eyebrow">{'日报数量' if lang == 'zh' else 'Reports'}</div>
                  <div class="metric">{int(summary.get('report_count') or 0)}</div>
                  <div class="muted">{'本周已留档 AI 日报数量。' if lang == 'zh' else 'Archived AI daily reports this week.'}</div>
                </article>
                <article class="metric-card">
                  <div class="eyebrow">{'任务质量' if lang == 'zh' else 'Job Quality'}</div>
                  <div class="metric">{int(job_counts.get('success', 0))}</div>
                  <div class="muted">{'成功'} {int(job_counts.get('success', 0))} · {'部分完成'} {int(job_counts.get('partial', 0))} · {'失败'} {int(job_counts.get('failed', 0))}</div>
                </article>
                <article class="metric-card">
                  <div class="eyebrow">{'本周已实现盈亏' if lang == 'zh' else 'Realized PnL'}</div>
                  <div class="metric">{_fmt_optional_float(trade_summary.get('realized_pnl'), digits=2)}</div>
                  <div class="muted">{'卖出笔数'} {int(trade_summary.get('count') or 0)} · {'盈利笔数'} {int(trade_summary.get('winner_count') or 0)}</div>
                </article>
                <article class="metric-card">
                  <div class="eyebrow">{'原因补录进度' if lang == 'zh' else 'Reason Coverage'}</div>
                  <div class="metric">{_fmt_optional_float(structured_reason_summary.get('coverage_pct'), suffix='%', digits=1)}</div>
                  <div class="muted">{'已结构化'} {int(structured_reason_summary.get('count') or 0)} · {'待补录'} {max(0, int(trade_summary.get('count') or 0) - int(structured_reason_summary.get('count') or 0))}</div>
                </article>
                <article class="metric-card">
                  <div class="eyebrow">{'建议审计覆盖率' if lang == 'zh' else 'Advice Audit Coverage'}</div>
                  <div class="metric">{_fmt_optional_float(audit_summary.get('coverage_pct'), suffix='%', digits=1)}</div>
                  <div class="muted">{'已带建议快照'} {int(audit_summary.get('count') or 0)} · {'总卖出'} {int(trade_summary.get('count') or 0)}</div>
                </article>
                <article class="metric-card">
                  <div class="eyebrow">{'模型 3D 均值' if lang == 'zh' else 'Model 3D Avg'}</div>
                  <div class="metric">{_fmt_optional_float(((model_windows.get(3) or {}).get('avg_return')), suffix='%', digits=2)}</div>
                  <div class="muted">{'样本数'} {int((model_windows.get(3) or {}).get('count') or 0)} · {'命中率'} {_fmt_optional_float(((model_windows.get(3) or {}).get('hit_rate')), suffix='%', digits=1)}</div>
                </article>
                <article class="metric-card">
                  <div class="eyebrow">{'日报 5D 均值' if lang == 'zh' else 'Report 5D Avg'}</div>
                  <div class="metric">{_fmt_optional_float(((report_window_summary.get(5) or {}).get('avg_return')), suffix='%', digits=2)}</div>
                  <div class="muted">{'可测样本'} {int(summary.get('measured_report_rows') or 0)} · {'命中率'} {_fmt_optional_float(((report_window_summary.get(5) or {}).get('hit_rate')), suffix='%', digits=1)}</div>
                </article>
              </section>
              <section class="card">
                <div class="eyebrow">{'一、市场情绪与重复候选' if lang == 'zh' else '1. Mood and Repeated Picks'}</div>
                <div class="stackline">{mood_chips}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'上榜次数' if lang == 'zh' else 'Times Picked'}</th><th>{'最近结论' if lang == 'zh' else 'Latest Verdict'}</th></tr></thead>
                  <tbody>{repeated_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'二、本周模型表现' if lang == 'zh' else '2. Weekly Model Performance'}</div>
                <div class="muted">{'主值是平均收益，下面小字是上涨命中率。' if lang == 'zh' else 'Main values are average returns, with positive hit rates below.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'Run' if lang == 'zh' else 'Run'}</th><th>{'市场 / 最近交易日' if lang == 'zh' else 'Market / Latest Trade Date'}</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{run_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'二点五、推荐验证' if lang == 'zh' else '2.5 Recommendation Validation'}</div>
                <div class="muted">{'把今天优先模型、优先组合和本周 AI 日报 Top 5 放到一张表里。主值是平均收益，小字是上涨命中率，用来回答“这周到底该更信哪套建议”。' if lang == 'zh' else 'Put the priority model, priority combo, and this week’s AI Report Top 5 onto one board. Main values are average returns and muted values are hit rates so you can judge which guidance deserved more trust this week.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'对象' if lang == 'zh' else 'Recommendation'}</th><th>{'说明' if lang == 'zh' else 'Note'}</th><th>1D</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{recommendation_validation_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'三、本周持仓卖出记录' if lang == 'zh' else '3. Weekly Portfolio Exits'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'日期' if lang == 'zh' else 'Date'}</th><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'卖出价' if lang == 'zh' else 'Sell Price'}</th><th>{'数量' if lang == 'zh' else 'Qty'}</th><th>{'已实现盈亏' if lang == 'zh' else 'Realized PnL'}</th><th>{'收益率' if lang == 'zh' else 'Return'}</th><th>{'原因' if lang == 'zh' else 'Reason'}</th></tr></thead>
                  <tbody>{trade_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'四、持仓建议有效性' if lang == 'zh' else '4. Advice Effectiveness'}</div>
                <div class="muted">{'第一版先按卖出原因归类，观察止盈、止损、调仓和复核动作最终带来的收益结果。' if lang == 'zh' else 'The first pass groups exits by reason so we can see how profit-taking, stops, rebalancing, and review-led exits actually performed.'}</div>
                {structured_reason_note}
                <div class="table-wrap"><table>
                  <thead><tr><th>{'建议类型' if lang == 'zh' else 'Advice Type'}</th><th>{'次数' if lang == 'zh' else 'Count'}</th><th>{'盈利笔数' if lang == 'zh' else 'Winners'}</th><th>{'命中率' if lang == 'zh' else 'Win Rate'}</th><th>{'平均收益率' if lang == 'zh' else 'Avg Return'}</th><th>{'累计已实现盈亏' if lang == 'zh' else 'Realized PnL'}</th></tr></thead>
                  <tbody>{advice_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'五、原因待补录清单' if lang == 'zh' else '5. Reason Review Queue'}</div>
                <div class="muted">{'这张表只列本周还没有结构化卖出原因的记录，补完后“持仓建议有效性”统计会更可信。' if lang == 'zh' else 'This table lists the weekly exits still missing a structured reason. Once reviewed, the advice-effectiveness section becomes more trustworthy.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'日期' if lang == 'zh' else 'Date'}</th><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'已实现盈亏' if lang == 'zh' else 'Realized PnL'}</th><th>{'收益率' if lang == 'zh' else 'Return'}</th><th>{'操作' if lang == 'zh' else 'Action'}</th></tr></thead>
                  <tbody>{unresolved_trade_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'六、建议审计链' if lang == 'zh' else '6. Advice Audit Trail'}</div>
                <div class="muted">{'从现在开始，新卖出会自动带上当时系统给出的动作建议、优先级、风险标签和调仓说明。这张表会进一步给出一条轻量复盘结论，回答：系统当时怎么说，你最后怎么做，结果如何。' if lang == 'zh' else 'From now on, new exits automatically carry the system advice snapshot at exit time. This table adds a lightweight review conclusion so you can see what the system said, what you did, and how it turned out.'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'日期' if lang == 'zh' else 'Date'}</th><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'当时建议' if lang == 'zh' else 'Advice at Exit'}</th><th>{'建议理由 / 调仓说明' if lang == 'zh' else 'Advice Reason / Rebalance'}</th><th>{'最终动作' if lang == 'zh' else 'Final Action'}</th><th>{'复盘结论' if lang == 'zh' else 'Review Conclusion'}</th><th>{'结果' if lang == 'zh' else 'Outcome'}</th></tr></thead>
                  <tbody>{audited_trade_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'七、本周任务异常' if lang == 'zh' else '7. Weekly Job Exceptions'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'任务类型' if lang == 'zh' else 'Job Type'}</th><th>{'状态' if lang == 'zh' else 'Status'}</th><th>{'开始时间' if lang == 'zh' else 'Started At'}</th><th>{'说明' if lang == 'zh' else 'Message'}</th></tr></thead>
                  <tbody>{weekly_jobs_html}</tbody>
                </table></div>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """


def _render_ai_report_guidance_bridge(report: dict, *, lang: str) -> str:
    guidance_summary = report.get("model_selection_guidance_summary") or {}
    attribution = report.get("market_template_attribution") or {}
    snapshot_meta = guidance_summary.get("snapshot_meta") or {}
    top_model_title = html.escape(
        str(guidance_summary.get("top_model_title") or ("样本继续沉淀" if lang == "zh" else "Still collecting samples"))
    )
    top_model_copy = html.escape(
        str(guidance_summary.get("top_model_summary") or ("当前还没有足够样本给出明确优先模型。" if lang == "zh" else "Not enough samples yet for a priority model."))
    )
    top_combo_title = html.escape(
        str(guidance_summary.get("top_combo_title") or ("组合样本继续沉淀" if lang == "zh" else "Combo samples still accumulating"))
    )
    top_combo_copy = html.escape(
        str(guidance_summary.get("top_combo_summary") or ("当前还没有足够组合样本。" if lang == "zh" else "Not enough combo samples yet."))
    )
    top_model_href = html.escape(str(guidance_summary.get("top_model_href") or f"/dashboard/model-performance?lang={lang}&market=CN"), quote=True)
    top_combo_href = html.escape(str(guidance_summary.get("top_combo_href") or f"/screeners?lang={lang}&market=CN&universe=full_market&run=1"), quote=True)
    source_time = html.escape(str(snapshot_meta.get("snapshot_date") or snapshot_meta.get("generated_at") or report.get("report_date") or "-"))
    source_kind = str(snapshot_meta.get("source") or "").strip()
    source_text = (
        f"来源：{'后台评测快照' if source_kind == 'snapshot' else '日报缓存 / 实时回退'} · {source_time}"
        if lang == "zh"
        else f"Source: {'evaluation snapshot' if source_kind == 'snapshot' else 'report cache / live fallback'} · {source_time}"
    )
    leaders = list(attribution.get("leaders") or [])
    leader_rows = "".join(
        "<div class='muted' style='margin-top:8px;'>"
        f"• {html.escape(str(item.get('label') or item.get('template') or '-'))} · {int(item.get('count') or 0)} "
        f"{'只' if lang == 'zh' else 'names'} · 1D {_fmt_optional_float((item.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)} / "
        f"{_fmt_optional_float((item.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)} · 3D "
        f"{_fmt_optional_float((item.get('stats_3d') or {}).get('avg_return'), suffix='%', digits=2)} / "
        f"{_fmt_optional_float((item.get('stats_3d') or {}).get('hit_rate'), suffix='%', digits=1)}"
        "</div>"
        for item in leaders[:4]
    ) or f"<div class='muted'>{'暂无模板归因样本。' if lang == 'zh' else 'No template attribution samples yet.'}</div>"
    return f"""
    <section class="card">
      <div class="eyebrow">{'模型来源与近期验证' if lang == 'zh' else 'Model Source & Recent Validation'}</div>
      <div class="muted">{html.escape(source_text)}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px;margin-top:14px;">
        <div class="playbook" style="margin-top:0;">
          <div style="font-weight:900;margin-bottom:6px;">{'优先模型' if lang == 'zh' else 'Priority model'}</div>
          <div style="font-weight:800;">{top_model_title}</div>
          <div class="muted" style="margin-top:6px;">{top_model_copy}</div>
          <div style="margin-top:10px;"><a class="pill" href="{top_model_href}">{'按该模型筛选' if lang == 'zh' else 'Run this model'}</a></div>
        </div>
        <div class="playbook" style="margin-top:0;">
          <div style="font-weight:900;margin-bottom:6px;">{'优先组合' if lang == 'zh' else 'Priority confluence'}</div>
          <div style="font-weight:800;">{top_combo_title}</div>
          <div class="muted" style="margin-top:6px;">{top_combo_copy}</div>
          <div style="margin-top:10px;"><a class="pill" href="{top_combo_href}">{'按组合筛选' if lang == 'zh' else 'Run confluence'}</a></div>
        </div>
        <div class="playbook" style="margin-top:0;">
          <div style="font-weight:900;margin-bottom:6px;">{'Top 5 模板表现' if lang == 'zh' else 'Top 5 template evidence'}</div>
          {leader_rows}
        </div>
      </div>
      <div class="toolbar" style="margin:14px 0 0;">
        <a class="pill" href="/dashboard/model-performance?lang={lang}&market=CN">{'模型评测总览' if lang == 'zh' else 'Model evaluation'}</a>
        <a class="pill" href="/dashboard/model-performance/winner-traceback?lang={lang}&market=CN">{'强票反向归因' if lang == 'zh' else 'Winner traceback'}</a>
        <a class="pill" href="/screeners?lang={lang}&market=CN&universe=full_market&run=1">{'回到模型选股' if lang == 'zh' else 'Back to screeners'}</a>
      </div>
    </section>
    """


def _premarket_plan_rows(report: dict) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for source, items in (
        ("actionable", report.get("market_recommendations") or report.get("rows") or []),
        ("watch", report.get("market_watch_recommendations") or []),
    ):
        for item in list(items)[:8]:
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            rows.append({**item, "_plan_source": source})
    rows.sort(
        key=lambda item: (
            0 if str(item.get("_plan_source") or "") == "actionable" else 1,
            0 if str(item.get("tradability_status") or "").upper() == "READY" else 1,
            -float(item.get("trade_readiness_score") or 0.0),
            float(abs(item.get("close_vs_buy_zone_high_pct") or 0.0)),
            -float(item.get("quant_rank") or 0.0),
            str(item.get("ticker") or ""),
        )
    )
    return rows[:8]


def _premarket_plan_action_text(item: dict, *, lang: str) -> str:
    source = str(item.get("_plan_source") or "watch")
    status = str(item.get("tradability_status") or "").upper()
    deviation = _monitor_float(item.get("close_vs_buy_zone_high_pct"))
    risk_flags = {str(flag).strip().lower() for flag in (item.get("risk_flags") or []) if str(flag).strip()}
    position_text = str(item.get("target_weight_text") or item.get("target_weight") or item.get("position_size_hint") or "").strip()
    if source == "watch" or status in {"REVIEW", "DEFER"}:
        return (
            "先观察，不抢第一笔；只有回踩买入区并重新放量时才考虑处理。"
            if lang == "zh"
            else "Watch first and avoid the first print; only act if price resets into the buy zone with renewed volume."
        )
    if deviation is not None and deviation >= 12.0:
        return (
            "已经偏离计划买点，今天只做观察，不追高。"
            if lang == "zh"
            else "Price is already extended beyond the planned entry, so treat it as watch-only and do not chase."
        )
    if "missing-model-score" in risk_flags:
        return (
            f"模型分暂不完整，按 {position_text or '小仓'} 先做验证单，开盘 15 分钟后再决定是否扩仓。"
            if lang == "zh"
            else f"Model scoring is incomplete, so start with a {position_text or 'small'} probe after the first 15 minutes before adding."
        )
    return (
        f"开盘后先等 15 分钟确认，再按 {position_text or '计划仓位'} 执行，不用抢竞价。"
        if lang == "zh"
        else f"Wait for the first 15 minutes to confirm, then execute with {position_text or 'planned size'} instead of chasing the open."
    )


def _premarket_plan_focus_text(item: dict, *, lang: str) -> str:
    trigger = str(item.get("entry_trigger") or "").strip()
    invalidation = str(item.get("invalidation_condition") or "").strip()
    if lang == "zh":
        return f"先看：{trigger or '是否守住开盘区间 / VWAP'}；不做：{invalidation or '跌破支撑或量价结构转弱'}"
    return f"Watch for: {trigger or 'VWAP / opening-range hold'}; stand down if: {invalidation or 'support fails or the tape weakens'}"


def _monitor_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _monitor_market_for_ticker(ticker: str) -> str:
    normalized = str(ticker or "").strip().upper()
    if normalized.endswith((".SS", ".SZ")) or re.fullmatch(r"\d{6}", normalized):
        return "CN"
    if normalized.endswith(".HK"):
        return "HK"
    return "US"


def _monitor_source_label(source: str, *, lang: str) -> str:
    labels = {
        "zh": {
            "portfolio": "持仓",
            "watchlist": "自选",
            "ai_actionable": "AI可执行",
            "ai_watch": "AI观察",
            "us_hotspot": "美股热点",
            "social": "社交信号",
        },
        "en": {
            "portfolio": "Portfolio",
            "watchlist": "Watchlist",
            "ai_actionable": "AI Actionable",
            "ai_watch": "AI Watch",
            "us_hotspot": "US Hotspot",
            "social": "Social Signal",
        },
    }
    return labels["zh" if lang == "zh" else "en"].get(source, source)


def _monitor_status(row: dict, latest_price: float | None, *, lang: str) -> dict[str, str]:
    buy_zone = row.get("buy_zone") if isinstance(row.get("buy_zone"), dict) else {}
    buy_low = _monitor_float(buy_zone.get("low"))
    buy_high = _monitor_float(buy_zone.get("high"))
    stop_loss = _monitor_float(row.get("stop_loss"))
    sources = set(row.get("sources") or [])
    if latest_price is None or latest_price <= 0:
        return {
            "key": "no_price",
            "label": "缺行情" if lang == "zh" else "No price",
            "detail": "本地价格湖里暂时没有最新价格。" if lang == "zh" else "No latest local price is available yet.",
        }
    if stop_loss is not None and latest_price <= stop_loss:
        return {
            "key": "risk",
            "label": "触及风险位" if lang == "zh" else "Risk trigger",
            "detail": f"{'现价已低于/接近止损位' if lang == 'zh' else 'Latest price is at or below the stop'} {_fmt_optional_float(stop_loss, digits=3)}",
        }
    if buy_low is not None and buy_high is not None and buy_low <= latest_price <= buy_high:
        return {
            "key": "in_zone",
            "label": "进入买入区" if lang == "zh" else "In buy zone",
            "detail": "价格进入计划买入区，仍需结合成交量与开盘走势确认。" if lang == "zh" else "Price is inside the planned buy zone; still confirm volume and tape.",
        }
    if buy_high is not None and latest_price > buy_high * 1.08:
        return {
            "key": "extended",
            "label": "不追高" if lang == "zh" else "Do not chase",
            "detail": "价格明显高于计划买入区，优先等回落或放弃。" if lang == "zh" else "Price is materially above the buy zone; wait for a pullback or pass.",
        }
    if "ai_actionable" in sources:
        return {
            "key": "ready",
            "label": "等待触发" if lang == "zh" else "Await trigger",
            "detail": "候选来自 AI 可执行池，等待触发条件确认。" if lang == "zh" else "Candidate came from the actionable AI pool; wait for trigger confirmation.",
        }
    if "ai_watch" in sources or "us_hotspot" in sources or "social" in sources:
        return {
            "key": "watch",
            "label": "观察确认" if lang == "zh" else "Watch",
            "detail": "目前偏观察，适合等待形态或风险条件改善。" if lang == "zh" else "This is still a watch item; wait for setup or risk improvement.",
        }
    return {
        "key": "track",
        "label": "跟踪" if lang == "zh" else "Track",
        "detail": "来自持仓或自选池，当前没有明确触发。" if lang == "zh" else "From portfolio/watchlist without a hard trigger yet.",
    }


def _build_realtime_monitor_rows(db: Session, *, lang: str, limit: int = 120) -> list[dict]:
    report = _load_cached_ai_daily_report(db) or {}
    focus: dict[str, dict] = {}

    def _add(item: dict, source: str) -> None:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            return
        row = focus.setdefault(
            ticker,
            {
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "market": item.get("market") or _monitor_market_for_ticker(ticker),
                "sources": [],
                "headline": item.get("headline") or item.get("summary") or item.get("verdict") or "",
                "entry_trigger": item.get("entry_trigger") or "",
                "invalidation_condition": item.get("invalidation_condition") or "",
                "buy_zone": item.get("buy_zone") if isinstance(item.get("buy_zone"), dict) else {},
                "stop_loss": item.get("stop_loss"),
                "risk_flags": list(item.get("risk_flags") or []),
                "tradability_status": item.get("tradability_status"),
            },
        )
        if source not in row["sources"]:
            row["sources"].append(source)
        for key in ("name", "market", "headline", "entry_trigger", "invalidation_condition", "stop_loss", "tradability_status"):
            if not row.get(key) and item.get(key):
                row[key] = item.get(key)
        if not row.get("buy_zone") and isinstance(item.get("buy_zone"), dict):
            row["buy_zone"] = item.get("buy_zone")
        if not row.get("risk_flags") and item.get("risk_flags"):
            row["risk_flags"] = list(item.get("risk_flags") or [])

    for item in load_portfolio_positions():
        _add(item, "portfolio")
    for item in _dashboard_watchlist_map(db).values():
        _add(item, "watchlist")
    for item in report.get("market_recommendations") or report.get("rows") or []:
        _add(item, "ai_actionable")
    for item in report.get("market_watch_recommendations") or []:
        _add(item, "ai_watch")
    for item in report.get("us_model_recommendations") or report.get("us_hotspot_validation") or []:
        _add(item, "us_hotspot")
    social_summary = social_signal_summary(db)
    social_items = list(social_summary.get("actionable") or []) + list(social_summary.get("hot_mentions_24h") or [])
    seen_social: set[str] = set()
    for item in social_items:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen_social:
            continue
        seen_social.add(ticker)
        _add(
            {
                **item,
                "headline": item.get("system_action") or item.get("social_view") or item.get("summary") or "",
                "risk_flags": item.get("validation_reasons") or [],
            },
            "social",
        )

    source_priority = {"portfolio": 0, "ai_actionable": 1, "ai_watch": 2, "watchlist": 3, "us_hotspot": 4, "social": 5}
    us_live_quotes = load_us_latest_trades(
        [ticker for ticker, row in focus.items() if str(row.get("market") or "").upper() == "US"]
    )
    rows: list[dict] = []
    for ticker, row in focus.items():
        live_quote = us_live_quotes.get(ticker) if str(row.get("market") or "").upper() == "US" else None
        local_close = _monitor_float(load_latest_close(ticker))
        latest_price = _monitor_float((live_quote or {}).get("price")) if live_quote else local_close
        status = _monitor_status(row, latest_price, lang=lang)
        buy_zone = row.get("buy_zone") if isinstance(row.get("buy_zone"), dict) else {}
        buy_low = _monitor_float(buy_zone.get("low"))
        buy_high = _monitor_float(buy_zone.get("high"))
        source_labels = [_monitor_source_label(source, lang=lang) for source in row.get("sources") or []]
        rows.append(
            {
                **row,
                "latest_price": latest_price,
                "local_close": local_close,
                "price_source": (live_quote or {}).get("source") or "local_latest_close",
                "price_timestamp": (live_quote or {}).get("timestamp"),
                "price_feed": (live_quote or {}).get("feed"),
                "status": status,
                "buy_low": buy_low,
                "buy_high": buy_high,
                "source_labels": source_labels,
                "source_rank": min((source_priority.get(source, 9) for source in row.get("sources") or ["watchlist"]), default=9),
            }
        )
    status_rank = {"risk": 0, "in_zone": 1, "ready": 2, "extended": 3, "watch": 4, "track": 5, "no_price": 6}
    rows.sort(key=lambda item: (status_rank.get((item.get("status") or {}).get("key"), 9), item.get("source_rank", 9), str(item.get("ticker") or "")))
    return rows[: max(20, min(int(limit or 120), 300))]


@router.get("/realtime-monitor/intraday", response_class=JSONResponse)
def dashboard_realtime_monitor_intraday(
    request: Request,
    ticker: str,
    market: str = "US",
    timeframe: str = "5Min",
) -> JSONResponse:
    if not is_authenticated(request):
        return JSONResponse({"status": "unauthorized", "bars": [], "message": "Unauthorized."}, status_code=401)
    normalized_ticker = str(ticker or "").strip().upper()
    market_code = str(market or "").strip().upper() or _monitor_market_for_ticker(normalized_ticker)
    if market_code not in {"CN", "US", "HK"}:
        market_code = _monitor_market_for_ticker(normalized_ticker)
    if market_code == "CN":
        return JSONResponse(load_cn_intraday_bars(normalized_ticker, timeframe=timeframe))
    if market_code != "US":
        return JSONResponse(
            {
                "status": "unsupported",
                "ticker": normalized_ticker,
                "market": market_code,
                "bars": [],
                "message": "当前弹窗日内 K 线支持美股和 A 股；港股分钟线后续接入后会在这里打开。",
            }
        )
    return JSONResponse(load_us_intraday_bars(normalized_ticker, timeframe=timeframe))


@router.get("/realtime-monitor", response_class=HTMLResponse)
def dashboard_realtime_monitor(request: Request, limit: int = 120, market: str = "ALL", db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/realtime-monitor")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="monitor")
    all_rows = _build_realtime_monitor_rows(db, lang=lang, limit=300)
    market_filter = str(market or "ALL").strip().upper()
    if market_filter not in {"ALL", "CN", "US", "HK"}:
        market_filter = "ALL"
    rows = [row for row in all_rows if market_filter == "ALL" or str(row.get("market") or "").upper() == market_filter]
    rows = rows[: max(20, min(int(limit or 120), 300))]
    counts = Counter((row.get("status") or {}).get("key") for row in rows)
    market_counts = Counter(str(row.get("market") or "-").upper() for row in all_rows)
    generated_at = _display_time(datetime.now(timezone.utc).isoformat(), with_tz=True)

    def _zone_text(row: dict) -> str:
        low = row.get("buy_low")
        high = row.get("buy_high")
        if low is None and high is None:
            return "-"
        return f"{_fmt_optional_float(low, digits=3)} - {_fmt_optional_float(high, digits=3)}"

    def _risk_chips(row: dict) -> str:
        flags = [str(flag).strip() for flag in (row.get("risk_flags") or []) if str(flag).strip()]
        if not flags:
            return "<span class='muted'>-</span>"
        return "".join(f"<span class='risk-chip'>{html.escape(flag)}</span>" for flag in flags[:4])

    def _price_source_text(row: dict) -> str:
        source = str(row.get("price_source") or "").strip()
        if source == "alpaca_latest_trade":
            timestamp = str(row.get("price_timestamp") or "").strip()
            feed = str(row.get("price_feed") or "").strip().upper()
            prefix = "Alpaca实时" if lang == "zh" else "Alpaca live"
            detail = f"{prefix}{(' · ' + feed) if feed else ''}"
            return f"{detail}{(' · ' + timestamp[:19]) if timestamp else ''}"
        return "本地最新收盘" if lang == "zh" else "Local latest close"

    cards = "".join(
        f"""
        <article class="monitor-card {html.escape(str((row.get('status') or {}).get('key') or 'track'))}">
          <div class="monitor-top">
            <div>
              <a class="ticker" href="/insights/{html.escape(str(row.get('ticker') or ''), quote=True)}?lang={lang}">{html.escape(str(row.get('name') or row.get('ticker') or '-'))}</a>
              <div class="name">{html.escape(str(row.get('ticker') or '-'))}</div>
            </div>
            <span class="status-chip {html.escape(str((row.get('status') or {}).get('key') or 'track'))}">{html.escape(str((row.get('status') or {}).get('label') or '-'))}</span>
          </div>
          <div class="price-row">
            <strong>{_fmt_optional_float(row.get('latest_price'), digits=3)}</strong>
            <span>{html.escape(str(row.get('market') or '-'))}</span>
            <span>{html.escape(' / '.join(row.get('source_labels') or []) or '-')}</span>
            <span>{html.escape(_price_source_text(row))}</span>
          </div>
          <div class="monitor-grid">
            <div><span>{'买入区' if lang == 'zh' else 'Buy zone'}</span><strong>{html.escape(_zone_text(row))}</strong></div>
            <div><span>{'止损' if lang == 'zh' else 'Stop'}</span><strong>{_fmt_optional_float(row.get('stop_loss'), digits=3)}</strong></div>
            <div><span>{'触发条件' if lang == 'zh' else 'Trigger'}</span><strong>{html.escape(str(row.get('entry_trigger') or '-'))}</strong></div>
            <div><span>{'放弃条件' if lang == 'zh' else 'Invalidation'}</span><strong>{html.escape(str(row.get('invalidation_condition') or '-'))}</strong></div>
          </div>
          <div class="monitor-detail">{html.escape(str((row.get('status') or {}).get('detail') or '-'))}</div>
          <div class="risk-row">{_risk_chips(row)}</div>
          <div class="card-actions">
            <button
              class="track-button"
              type="button"
              data-ticker="{html.escape(str(row.get('ticker') or ''), quote=True)}"
              data-name="{html.escape(str(row.get('name') or row.get('ticker') or ''), quote=True)}"
              data-market="{html.escape(str(row.get('market') or ''), quote=True)}"
            >{'弹出日内K线' if lang == 'zh' else 'Pop Intraday Chart'}</button>
            <a class="track-link" href="/insights/{html.escape(str(row.get('ticker') or ''), quote=True)}?lang={lang}">{'打开分析页' if lang == 'zh' else 'Open Insight'}</a>
          </div>
        </article>
        """
        for row in rows
    ) or f"""
        <article class="monitor-card">
          <div class="monitor-detail">{'暂无可监控股票。请先添加自选/持仓，或生成 AI 日报。' if lang == 'zh' else 'No monitor names yet. Add watchlist/portfolio names or generate the AI report first.'}</div>
        </article>
    """
    market_pills = "".join(
        f"<a class='pill{' active' if market_filter == code else ''}' href='/dashboard/realtime-monitor?lang={lang}&market={code}&limit={limit}'>{label}: {int(count)}</a>"
        for code, label, count in (
            ("ALL", "全部" if lang == "zh" else "All", len(all_rows)),
            ("CN", "A股" if lang == "zh" else "CN", market_counts.get("CN", 0)),
            ("US", "美股" if lang == "zh" else "US", market_counts.get("US", 0)),
            ("HK", "港股" if lang == "zh" else "HK", market_counts.get("HK", 0)),
        )
    )

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'重点监控台' if lang == 'zh' else 'Live Monitor'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --warn:#fbbf24; --danger:#fb7185; --blue:#60a5fa; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(96,165,250,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.14), transparent 26%),linear-gradient(180deg,#08111a 0%,#071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:18px; }}
          .wrap {{ max-width:1180px; margin:0 auto; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:14px; }}
          .pill {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.72); color:var(--ink); font-size:13px; font-weight:850; }}
          .pill.active {{ border-color:rgba(61,217,182,0.42); background:rgba(61,217,182,0.16); color:var(--ink); }}
          .hero,.monitor-card,.metric {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }}
          .hero {{ padding:20px; margin-bottom:14px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:950; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px; }}
          h1 {{ margin:0 0 8px; font-size:32px; line-height:1.08; letter-spacing:-0.04em; }}
          .muted,.name,.monitor-detail {{ color:var(--muted); font-size:14px; line-height:1.55; }}
          .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:14px 0; }}
          .metric {{ padding:14px; }}
          .metric span {{ color:var(--muted); font-size:12px; font-weight:850; }}
          .metric strong {{ display:block; margin-top:6px; font-size:24px; letter-spacing:-0.04em; }}
          .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; }}
          .monitor-card {{ padding:16px; position:relative; overflow:hidden; }}
          .monitor-card::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:4px; background:var(--line); }}
          .monitor-card.risk::before {{ background:var(--danger); }}
          .monitor-card.in_zone::before,.monitor-card.ready::before {{ background:var(--accent); }}
          .monitor-card.extended::before {{ background:var(--warn); }}
          .monitor-card.watch::before {{ background:var(--blue); }}
          .monitor-top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
          .ticker {{ font-size:24px; font-weight:950; letter-spacing:-0.03em; }}
          .status-chip {{ flex:0 0 auto; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:950; border:1px solid rgba(255,255,255,0.08); }}
          .status-chip.risk {{ color:#fecdd3; background:rgba(251,113,133,0.16); }}
          .status-chip.in_zone,.status-chip.ready {{ color:#bbf7d0; background:rgba(61,217,182,0.14); }}
          .status-chip.extended {{ color:#fde68a; background:rgba(251,191,36,0.14); }}
          .status-chip.watch {{ color:#bfdbfe; background:rgba(96,165,250,0.14); }}
          .status-chip.track,.status-chip.no_price {{ color:#cbd5e1; background:rgba(148,163,184,0.14); }}
          .price-row {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:12px; }}
          .price-row strong {{ font-size:22px; }}
          .price-row span {{ color:var(--muted); font-size:12px; font-weight:850; padding:5px 8px; border-radius:999px; background:rgba(255,255,255,0.05); }}
          .monitor-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:14px; }}
          .monitor-grid div {{ min-width:0; padding:10px; border-radius:16px; background:rgba(21,34,49,0.72); border:1px solid rgba(255,255,255,0.06); }}
          .monitor-grid span {{ display:block; color:var(--muted); font-size:12px; font-weight:850; margin-bottom:5px; }}
          .monitor-grid strong {{ display:block; font-size:13px; line-height:1.35; overflow-wrap:anywhere; }}
          .monitor-detail {{ margin-top:12px; }}
          .risk-row {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }}
          .risk-chip {{ display:inline-flex; padding:5px 8px; border-radius:999px; background:rgba(251,191,36,0.10); color:#fde68a; border:1px solid rgba(251,191,36,0.22); font-size:12px; font-weight:850; }}
          .card-actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }}
          .track-button,.track-link {{ appearance:none; border:1px solid rgba(61,217,182,0.28); border-radius:999px; padding:8px 12px; background:rgba(61,217,182,0.12); color:var(--ink); font-size:12px; font-weight:950; cursor:pointer; }}
          .track-link {{ border-color:rgba(148,163,184,0.20); background:rgba(148,163,184,0.10); }}
          .track-button:hover,.track-link:hover {{ transform:translateY(-1px); border-color:rgba(61,217,182,0.55); }}
          body.modal-open {{ overflow:hidden; }}
          .kline-modal[hidden] {{ display:none; }}
          .kline-modal {{ position:fixed; inset:0; z-index:50; display:grid; place-items:center; padding:18px; background:rgba(2,6,12,0.72); backdrop-filter:blur(10px); }}
          .kline-dialog {{ width:min(980px, 96vw); max-height:92vh; overflow:auto; border:1px solid rgba(148,163,184,0.22); border-radius:26px; background:linear-gradient(180deg, rgba(13,23,35,0.98), rgba(8,16,25,0.98)); box-shadow:0 30px 90px rgba(0,0,0,0.46); padding:16px; }}
          .kline-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:10px; }}
          .kline-title {{ font-size:20px; font-weight:950; letter-spacing:-0.03em; }}
          .kline-meta,.kline-message {{ color:var(--muted); font-size:13px; line-height:1.55; }}
          .kline-close {{ width:34px; height:34px; border-radius:999px; border:1px solid rgba(148,163,184,0.22); background:rgba(255,255,255,0.06); color:var(--ink); font-size:20px; cursor:pointer; }}
          .kline-chart-wrap {{ border:1px solid rgba(148,163,184,0.16); border-radius:18px; background:rgba(3,8,14,0.48); padding:10px; }}
          #kline-canvas {{ display:block; width:100%; height:420px; }}
          @media (max-width:960px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:14px; }} .metrics {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .wrap {{ max-width:100%; }} }}
          @media (max-width:560px) {{ h1 {{ font-size:27px; }} .cards,.monitor-grid,.metrics {{ grid-template-columns:1fr; }} .ticker {{ font-size:22px; }} #kline-canvas {{ height:320px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'重点监控台' if lang == 'zh' else 'Live Monitor'}</h1>
              <p>{'只盯持仓、自选、AI 日报候选，不做全市场实时流，避免把机器拖垮。' if lang == 'zh' else 'Tracks only portfolio, watchlist, and AI report candidates instead of streaming the whole market.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/dashboard?lang={lang}&mode=monitor">← {'返回首页' if lang == 'zh' else 'Back to Dashboard'}</a>
                <a class="pill" href="/dashboard/premarket-plan?lang={lang}">{'盘前便签' if lang == 'zh' else 'Premarket Plan'}</a>
                <a class="pill" href="/dashboard/ai-daily-report?lang={lang}">{'AI 日报' if lang == 'zh' else 'AI Report'}</a>
                <a class="pill" href="/portfolio?lang={lang}">{'持仓' if lang == 'zh' else 'Portfolio'}</a>
              </div>
              <section class="hero">
                <div class="eyebrow">{'60 秒自动刷新' if lang == 'zh' else 'Auto refresh every 60s'}</div>
                <h1>{'重点池准实时监控' if lang == 'zh' else 'Focused Quasi-live Monitor'}</h1>
                <div class="muted">{'当前价格使用本地最新行情缓存，适合盘中/盘后快速判断“是否进入买入区、是否不该追高、是否触及风险位”。后续可以把价格源替换为 Alpaca/Polygon 实时报价。' if lang == 'zh' else 'Prices currently use local latest-price cache. This is designed to quickly flag buy-zone, do-not-chase, and risk-trigger states; Alpaca/Polygon quotes can be plugged in later.'}</div>
                <div class="toolbar" style="margin:14px 0 0;">
                  <span class="pill">{'生成时间' if lang == 'zh' else 'Generated'}: {html.escape(generated_at)}</span>
                  <span class="pill">{'股票数' if lang == 'zh' else 'Names'}: {len(rows)}</span>
                </div>
              </section>
              <section class="toolbar" style="margin-top:0;">{market_pills}</section>
              <section class="metrics">
                <div class="metric"><span>{'进入买入区' if lang == 'zh' else 'In buy zone'}</span><strong>{int(counts.get('in_zone') or 0)}</strong></div>
                <div class="metric"><span>{'触及风险位' if lang == 'zh' else 'Risk triggers'}</span><strong>{int(counts.get('risk') or 0)}</strong></div>
                <div class="metric"><span>{'不追高' if lang == 'zh' else 'Do not chase'}</span><strong>{int(counts.get('extended') or 0)}</strong></div>
                <div class="metric"><span>{'缺行情' if lang == 'zh' else 'No price'}</span><strong>{int(counts.get('no_price') or 0)}</strong></div>
              </section>
              <section class="cards">{cards}</section>
            </div>
          </main>
        </div>
        <div class="kline-modal" id="kline-modal" hidden>
          <div class="kline-dialog" role="dialog" aria-modal="true" aria-labelledby="kline-title">
            <div class="kline-head">
              <div>
                <div class="kline-title" id="kline-title">{'日内 K 线' if lang == 'zh' else 'Intraday Chart'}</div>
                <div class="kline-meta" id="kline-meta">{'加载中...' if lang == 'zh' else 'Loading...'}</div>
              </div>
              <button class="kline-close" id="kline-close" type="button" aria-label="Close">×</button>
            </div>
            <div class="kline-chart-wrap">
              <canvas id="kline-canvas"></canvas>
            </div>
            <div class="kline-message" id="kline-message"></div>
          </div>
        </div>
        <script>
          const monitorLang = {json.dumps(lang)};
          const modal = document.getElementById('kline-modal');
          const closeBtn = document.getElementById('kline-close');
          const titleEl = document.getElementById('kline-title');
          const metaEl = document.getElementById('kline-meta');
          const messageEl = document.getElementById('kline-message');
          const canvas = document.getElementById('kline-canvas');
          const ctx = canvas.getContext('2d');

          function openModal() {{
            modal.hidden = false;
            document.body.classList.add('modal-open');
          }}

          function closeModal() {{
            modal.hidden = true;
            document.body.classList.remove('modal-open');
          }}

          function resizeCanvas() {{
            const ratio = window.devicePixelRatio || 1;
            const rect = canvas.getBoundingClientRect();
            canvas.width = Math.max(320, Math.floor(rect.width * ratio));
            canvas.height = Math.max(260, Math.floor(rect.height * ratio));
            ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
            return {{ width: rect.width, height: rect.height }};
          }}

          function drawEmpty(message) {{
            const size = resizeCanvas();
            ctx.clearRect(0, 0, size.width, size.height);
            ctx.fillStyle = 'rgba(255,255,255,0.62)';
            ctx.font = '14px ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif';
            ctx.fillText(message || (monitorLang === 'zh' ? '暂无日内K线数据' : 'No intraday bars'), 22, 44);
          }}

          function drawCandles(bars) {{
            const size = resizeCanvas();
            ctx.clearRect(0, 0, size.width, size.height);
            const padL = 48, padR = 14, padT = 18, padB = 34;
            const w = size.width - padL - padR;
            const h = size.height - padT - padB;
            if (!bars || bars.length === 0) {{
              drawEmpty(monitorLang === 'zh' ? '暂无日内K线数据' : 'No intraday bars');
              return;
            }}
            const highs = bars.map(b => Number(b.h)).filter(Number.isFinite);
            const lows = bars.map(b => Number(b.l)).filter(Number.isFinite);
            const maxP = Math.max(...highs);
            const minP = Math.min(...lows);
            const span = Math.max(0.0001, maxP - minP);
            const y = price => padT + ((maxP - price) / span) * h;
            ctx.strokeStyle = 'rgba(148,163,184,0.16)';
            ctx.lineWidth = 1;
            for (let i = 0; i <= 4; i++) {{
              const yy = padT + h * i / 4;
              ctx.beginPath();
              ctx.moveTo(padL, yy);
              ctx.lineTo(padL + w, yy);
              ctx.stroke();
              const label = (maxP - span * i / 4).toFixed(2);
              ctx.fillStyle = 'rgba(203,213,225,0.72)';
              ctx.font = '11px ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif';
              ctx.fillText(label, 6, yy + 4);
            }}
            const slot = w / bars.length;
            const bodyW = Math.max(3, Math.min(10, slot * 0.58));
            bars.forEach((bar, index) => {{
              const o = Number(bar.o), c = Number(bar.c), hi = Number(bar.h), lo = Number(bar.l);
              if (![o, c, hi, lo].every(Number.isFinite)) return;
              const x = padL + slot * index + slot / 2;
              const up = c >= o;
              const color = up ? '#34d399' : '#fb7185';
              ctx.strokeStyle = color;
              ctx.fillStyle = color;
              ctx.lineWidth = 1.25;
              ctx.beginPath();
              ctx.moveTo(x, y(hi));
              ctx.lineTo(x, y(lo));
              ctx.stroke();
              const top = y(Math.max(o, c));
              const bottom = y(Math.min(o, c));
              const bodyH = Math.max(2, bottom - top);
              ctx.fillRect(x - bodyW / 2, top, bodyW, bodyH);
            }});
            const first = bars[0];
            const last = bars[bars.length - 1];
            ctx.fillStyle = 'rgba(203,213,225,0.68)';
            ctx.font = '11px ui-sans-serif, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif';
            ctx.fillText(String(first.t || '').slice(11, 16), padL, size.height - 10);
            ctx.fillText(String(last.t || '').slice(11, 16), Math.max(padL, size.width - 68), size.height - 10);
          }}

          async function fetchIntradayBars(url, retries) {{
            let lastData = null;
            for (let attempt = 0; attempt <= retries; attempt++) {{
              const response = await fetch(url, {{ headers: {{ 'Accept': 'application/json' }} }});
              const data = await response.json();
              const bars = Array.isArray(data.bars) ? data.bars : [];
              if (bars.length > 0 || attempt >= retries) return data;
              lastData = data;
              await new Promise(resolve => window.setTimeout(resolve, 700));
            }}
            return lastData || {{ status: 'empty', bars: [] }};
          }}

          async function loadChart(button) {{
            const ticker = button.dataset.ticker || '';
            const name = button.dataset.name || ticker;
            const market = button.dataset.market || 'US';
            titleEl.textContent = `${{name}} (${{ticker}}) · ${{monitorLang === 'zh' ? '日内 K 线' : 'Intraday Chart'}}`;
            metaEl.textContent = monitorLang === 'zh' ? '正在加载 5 分钟K线...' : 'Loading 5-minute bars...';
            messageEl.textContent = '';
            openModal();
            drawEmpty(monitorLang === 'zh' ? '正在加载...' : 'Loading...');
            try {{
              const url = `/dashboard/realtime-monitor/intraday?ticker=${{encodeURIComponent(ticker)}}&market=${{encodeURIComponent(market)}}&timeframe=5Min`;
              const data = await fetchIntradayBars(url, market === 'CN' ? 2 : 0);
              const bars = Array.isArray(data.bars) ? data.bars : [];
              metaEl.textContent = `${{ticker}} · ${{data.timeframe || '5Min'}} · ${{data.feed ? String(data.feed).toUpperCase() : market}} · ${{bars.length}} bars`;
              messageEl.textContent = data.message || '';
              if (data.status !== 'success' || bars.length === 0) {{
                drawEmpty(data.message || (monitorLang === 'zh' ? '暂无日内K线数据' : 'No intraday bars'));
                return;
              }}
              drawCandles(bars);
            }} catch (error) {{
              const message = monitorLang === 'zh' ? '加载日内K线失败，请稍后重试。' : 'Failed to load intraday bars.';
              metaEl.textContent = message;
              messageEl.textContent = String(error && error.message ? error.message : error);
              drawEmpty(message);
            }}
          }}

          document.querySelectorAll('.track-button').forEach(button => {{
            button.addEventListener('click', () => loadChart(button));
          }});
          closeBtn.addEventListener('click', closeModal);
          modal.addEventListener('click', event => {{
            if (event.target === modal) closeModal();
          }});
          window.addEventListener('keydown', event => {{
            if (event.key === 'Escape' && !modal.hidden) closeModal();
          }});
          window.addEventListener('resize', () => {{
            if (!modal.hidden) drawEmpty(monitorLang === 'zh' ? '窗口大小已变化，请重新点击加载。' : 'Window resized. Click again to reload.');
          }});
          window.setInterval(() => {{
            if (modal.hidden) window.location.reload();
          }}, 60000);
        </script>
      </body>
    </html>
    """


@router.get("/premarket-plan", response_class=HTMLResponse)
def dashboard_premarket_plan(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/premarket-plan")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="ops")
    report = _load_cached_ai_daily_report(db) or {
        "mood": "-",
        "headline": "暂无可用的 A股 AI 日报，请先运行收盘复盘或手动生成。",
        "strategy": {"headline": "-", "playbook": "-", "bullets": []},
        "market_recommendations": [],
        "market_watch_recommendations": [],
    }
    plan_rows = _premarket_plan_rows(report)
    plan_date = html.escape(str(report.get("report_date") or report.get("generated_at") or "-"))
    strategy = report.get("strategy") or {}
    guidance_summary = report.get("model_selection_guidance_summary") or {}
    top_model_title = html.escape(str(guidance_summary.get("top_model_title") or "-"))
    top_combo_title = html.escape(str(guidance_summary.get("top_combo_title") or "-"))
    actionable_count = sum(1 for item in plan_rows if str(item.get("_plan_source") or "") == "actionable")
    watch_count = sum(1 for item in plan_rows if str(item.get("_plan_source") or "") != "actionable")
    do_not_chase_count = sum(
        1
        for item in plan_rows
        if (_monitor_float(item.get("close_vs_buy_zone_high_pct")) or 0.0) >= 12.0
    )

    def _source_label(value: str) -> str:
        if value == "actionable":
            return "可执行" if lang == "zh" else "Actionable"
        return "观察" if lang == "zh" else "Watch"

    def _source_class(value: str) -> str:
        return "actionable" if value == "actionable" else "watch"

    def _risk_text(item: dict) -> str:
        flags = [str(flag).strip() for flag in (item.get("risk_flags") or []) if str(flag).strip()]
        if flags:
            return " / ".join(flags[:4])
        return "无明显风险标签" if lang == "zh" else "No obvious risk tags"

    def _buy_zone_text(item: dict) -> str:
        zone = item.get("buy_zone") or {}
        low = zone.get("low")
        high = zone.get("high")
        if low is None and high is None:
            return "-"
        return f"{_fmt_optional_float(low, digits=3)} - {_fmt_optional_float(high, digits=3)}"

    card_rows = "".join(
        f"""
        <article class="plan-card">
          <div class="plan-top">
            <div>
              <a class="ticker" href="/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}">{html.escape(str(item.get('name') or item.get('ticker') or '-'))}</a>
              <div class="name">{html.escape(str(item.get('ticker') or '-'))}</div>
            </div>
            <span class="badge {_source_class(str(item.get('_plan_source') or 'watch'))}">{_source_label(str(item.get('_plan_source') or 'watch'))}</span>
          </div>
          <div class="headline">{html.escape(str(item.get('headline') or item.get('summary') or item.get('verdict') or '-'))}</div>
          <div class="risk" style="margin-top:10px;background:rgba(96,165,250,0.10);border-color:rgba(96,165,250,0.22);color:#bfdbfe;">{html.escape(_premarket_plan_action_text(item, lang=lang))}</div>
          <div class="plan-grid">
            <div><span>{'触发条件' if lang == 'zh' else 'Trigger'}</span><strong>{html.escape(str(item.get('entry_trigger') or '-'))}</strong></div>
            <div><span>{'放弃条件' if lang == 'zh' else 'Give up if'}</span><strong>{html.escape(str(item.get('invalidation_condition') or '-'))}</strong></div>
            <div><span>{'买入区' if lang == 'zh' else 'Buy zone'}</span><strong>{html.escape(_buy_zone_text(item))}</strong></div>
            <div><span>{'仓位' if lang == 'zh' else 'Size'}</span><strong>{html.escape(str(item.get('target_weight_text') or item.get('target_weight') or item.get('position_size_hint') or '-'))}</strong></div>
            <div><span>{'止损' if lang == 'zh' else 'Stop'}</span><strong>{html.escape(str(item.get('stop_loss') or '-'))}</strong></div>
            <div><span>{'状态' if lang == 'zh' else 'Status'}</span><strong>{html.escape(format_trade_status(item.get('tradability_status'), lang=lang))}</strong></div>
          </div>
          <div class="note" style="margin-top:10px;font-weight:800;color:var(--ink);">{html.escape(_premarket_plan_focus_text(item, lang=lang))}</div>
          <div class="risk">{'风险提示' if lang == 'zh' else 'Risk'}：{html.escape(_risk_text(item))}</div>
          <div class="note">{html.escape(build_trade_explain_text(item, lang=lang))}</div>
        </article>
        """
        for item in plan_rows
    ) or f"""
        <article class="plan-card empty">
          <div class="headline">{'当前没有可生成盘前计划的候选。请先运行收盘刷新、模型预计算和 AI 日报。' if lang == 'zh' else 'No candidates are available for a premarket plan. Run post-close refresh, model precompute, and the AI report first.'}</div>
        </article>
    """

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'盘前便签' if lang == 'zh' else 'Premarket Plan'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --warn:#fbbf24; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }}
          a {{ color:inherit; text-decoration:none; }}
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:18px; }}
          .wrap {{ max-width:920px; margin:0 auto; }}
          .toolbar {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:14px; }}
          .pill {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }}
          .hero,.plan-card {{ background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:18px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }}
          .hero {{ margin-bottom:14px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:900; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px; }}
          h1 {{ margin:0 0 8px; font-size:30px; line-height:1.1; }}
          .muted,.name,.note {{ color:var(--muted); font-size:14px; line-height:1.55; }}
          .cards {{ display:grid; gap:12px; }}
          .plan-top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
          .ticker {{ font-size:24px; font-weight:950; letter-spacing:-0.03em; }}
          .badge {{ flex:0 0 auto; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:900; border:1px solid transparent; }}
          .badge.actionable {{ color:#022c22; background:linear-gradient(135deg,#6ee7b7,#3dd9b6); }}
          .badge.watch {{ color:#2f1b00; background:linear-gradient(135deg,#fde68a,#fbbf24); }}
          .headline {{ margin-top:12px; font-size:16px; font-weight:850; line-height:1.45; }}
          .plan-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:14px; }}
          .plan-grid div {{ min-width:0; padding:10px; border-radius:16px; background:rgba(21,34,49,0.72); border:1px solid rgba(255,255,255,0.06); }}
          .plan-grid span {{ display:block; color:var(--muted); font-size:12px; font-weight:800; margin-bottom:5px; }}
          .plan-grid strong {{ display:block; font-size:14px; line-height:1.35; overflow-wrap:anywhere; }}
          .risk {{ margin-top:12px; padding:10px 12px; border-radius:16px; background:rgba(251,191,36,0.10); border:1px solid rgba(251,191,36,0.22); color:#fde68a; font-size:13px; font-weight:800; }}
          .note {{ margin-top:8px; }}
          .empty {{ border-style:dashed; }}
          @media (max-width: 960px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .main {{ padding:14px; }} .wrap {{ max-width:100%; }} }}
          @media (max-width: 520px) {{ .plan-grid {{ grid-template-columns:1fr; }} h1 {{ font-size:26px; }} .ticker {{ font-size:22px; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'盘前便签' if lang == 'zh' else 'Premarket'}</h1>
              <p>{'手机端 10 秒看完：先看触发、放弃、仓位和风险，不在盘前重新翻长表。' if lang == 'zh' else 'A 10-second mobile plan: triggers, invalidation, sizing, and risk without reopening long tables.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="main">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/dashboard?lang={lang}&mode=premarket">← {'返回首页' if lang == 'zh' else 'Back to Dashboard'}</a>
                <a class="pill" href="/dashboard/ai-daily-report?lang={lang}">{'AI 日报' if lang == 'zh' else 'AI Report'}</a>
                <a class="pill" href="/dashboard/realtime-monitor?lang={lang}">{'重点监控台' if lang == 'zh' else 'Live Monitor'}</a>
                <a class="pill" href="/screeners?lang={lang}&market=CN&universe=full_market&run=1">{'模型选股' if lang == 'zh' else 'Screeners'}</a>
              </div>
              <section class="hero">
                <div class="eyebrow">{'明日重点盯盘清单' if lang == 'zh' else 'Next-session watch plan'}</div>
                <h1>{html.escape(str(strategy.get('headline') or report.get('headline') or '-'))}</h1>
                <div class="muted">{html.escape(str(strategy.get('playbook') or report.get('headline') or '-'))}</div>
                <div class="toolbar" style="margin:14px 0 0;">
                  <span class="pill">{'日报日期' if lang == 'zh' else 'Report date'}: {plan_date}</span>
                  <span class="pill">{'优先模型' if lang == 'zh' else 'Model'}: {top_model_title}</span>
                  <span class="pill">{'优先组合' if lang == 'zh' else 'Combo'}: {top_combo_title}</span>
                </div>
                <div class="toolbar" style="margin:10px 0 0;">
                  <span class="pill">{'可执行' if lang == 'zh' else 'Actionable'}: {actionable_count}</span>
                  <span class="pill">{'观察' if lang == 'zh' else 'Watch'}: {watch_count}</span>
                  <span class="pill">{'不要追高' if lang == 'zh' else 'Do not chase'}: {do_not_chase_count}</span>
                </div>
              </section>
              <section class="cards">{card_rows}</section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """


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
    market_recommendation_meta = report.get("market_recommendations_meta") or {}
    market_structure = report.get("market_structure") or {}
    market_template_attribution = report.get("market_template_attribution") or {}
    us_market_recommendation_meta = report.get("us_model_recommendations_meta") or {}
    us_market_structure = report.get("us_market_structure") or {}
    lightgbm_execution_bias = report.get("lightgbm_execution_bias") or {}
    model_guidance_card_html = _render_ai_report_guidance_bridge(report, lang=lang)
    market_candidate_status = str(market_recommendation_meta.get("status") or "").strip().lower()
    send_guard_note = ""
    force_send_cta = ""
    if market_candidate_status in {"fallback", "not_ready"}:
        send_guard_note = (
            html.escape(str(market_recommendation_meta.get("note") or "今日 A股候选未完全就绪，默认不建议直接发送日报。"))
        )
        force_send_cta = f"""
        <form action="/jobs/send-ai-daily-report" method="post" style="display:inline;">
          <input type="hidden" name="redirect_to" value="/dashboard/ai-daily-report?lang={lang}" />
          <input type="hidden" name="force_send" value="1" />
          <button type="submit" style="background:linear-gradient(135deg, rgba(245,158,11,0.88), rgba(251,191,36,0.82));">{'仍然发送当前降级日报' if lang == 'zh' else 'Force send degraded report'}</button>
        </form>
        """
    def recommendation_meta_badge(meta: dict) -> str:
        status = str(meta.get("status") or "").strip().lower()
        source = str(meta.get("source") or "").strip().lower()
        note = html.escape(str(meta.get("note") or "-"))
        blocked_candidates = int(meta.get("blocked_candidates") or 0)
        label = {
            "ready": "今日候选已就绪",
            "fallback": "已降级到预测候选",
            "blocked": "推荐已暂停",
            "not_ready": "今日候选未就绪",
            "empty": "当前无可用候选",
        }.get(status, "候选状态")
        source_text = {
            "fresh_snapshot": "来源：今日 screener 快照",
            "predictions_fallback": "来源：最新模型预测",
            "snapshot_required": "来源：等待今日快照",
            "none": "来源：无",
        }.get(source, "来源：-")
        tone = {
            "ready": "rgba(61,217,182,0.12)",
            "fallback": "rgba(245,158,11,0.14)",
            "blocked": "rgba(239,68,68,0.14)",
            "not_ready": "rgba(239,68,68,0.14)",
            "empty": "rgba(148,163,184,0.14)",
        }.get(status, "rgba(148,163,184,0.14)")
        border = {
            "ready": "rgba(61,217,182,0.28)",
            "fallback": "rgba(245,158,11,0.32)",
            "blocked": "rgba(239,68,68,0.32)",
            "not_ready": "rgba(239,68,68,0.32)",
            "empty": "rgba(148,163,184,0.28)",
        }.get(status, "rgba(148,163,184,0.28)")
        return f"""
        <div class="playbook" style="margin-top:14px;background:{tone};border-color:{border};">
          <div style="font-weight:800;margin-bottom:6px;">{label}</div>
          <div class="muted">{source_text}</div>
          <div class="muted" style="margin-top:6px;">{note}</div>
          <div class="muted" style="margin-top:6px;">{'被规则拦截' if lang == 'zh' else 'Blocked by hard rules'}: {blocked_candidates}</div>
        </div>
        """
    def structure_block(title: str, structure: dict) -> str:
        source_value = str(structure.get("source") or "").strip()
        if lang == "zh":
            source_text = {
                "market_heatmap_snapshot": "来源：后台市场快照",
                "recommendation_rows": "来源：全市场模板主题汇总",
            }.get(source_value, "来源：结构化候选汇总")
        else:
            source_text = {
                "market_heatmap_snapshot": "Source: background market snapshot",
                "recommendation_rows": "Source: full-market template theme aggregation",
            }.get(source_value, "Source: structured candidate aggregation")
        strong_rows = "".join(
            f"<div class='muted'>• {html.escape(str(item.get('label') or '-'))} · {int(item.get('count') or 0)} 只 · 均强度 {html.escape(str(item.get('avg_strength') or '-'))} · {' / '.join((item.get('tickers') or [])[:3]) or '-'}</div>"
            for item in (structure.get("strong_sectors") or [])[:3]
        ) or "<div class='muted'>-</div>"
        weak_rows = "".join(
            f"<div class='muted'>• {html.escape(str(item.get('label') or '-'))} · 风险均值 {html.escape(str(item.get('avg_risk') or '-'))} · {' / '.join((item.get('tickers') or [])[:3]) or '-'}</div>"
            for item in (structure.get("weak_sectors") or [])[:3]
        ) or "<div class='muted'>-</div>"
        risk_rows = "".join(
            f"<div class='muted'>• {html.escape(str(item.get('ticker') or '-'))} · {html.escape(str(item.get('tradability_status') or '-'))} · {html.escape(', '.join(item.get('risk_flags') or []) or '-')}</div>"
            for item in (structure.get("risk_watch") or [])[:4]
        ) or "<div class='muted'>-</div>"
        return f"""
        <article class="card">
          <div class="eyebrow">{title}</div>
          <div class="muted">{html.escape(str(structure.get('headline') or '-'))}</div>
          <div class="muted" style="margin-top:6px;">{html.escape(source_text)}</div>
          <div class="playbook" style="margin-top:14px;">
            <div style="font-weight:800;margin-bottom:6px;">{'强方向' if lang == 'zh' else 'Strong sectors'}</div>
            {strong_rows}
            <div style="font-weight:800;margin:12px 0 6px;">{'弱方向 / 风险集中' if lang == 'zh' else 'Weak / risk concentration'}</div>
            {weak_rows}
            <div style="font-weight:800;margin:12px 0 6px;">{'风险清单' if lang == 'zh' else 'Risk watch'}</div>
            {risk_rows}
          </div>
        </article>
        """

    def template_attribution_block(title: str, attribution: dict) -> str:
        leaders = attribution.get("leaders") or []
        leader_rows = "".join(
            (
                f"<div class='muted'>• {html.escape(str(item.get('label') or '-'))} · {int(item.get('count') or 0)} 只 · 量化均分 {html.escape(str(item.get('avg_quant_rank') or '-'))} · {' / '.join(item.get('tickers') or []) or '-'}</div>"
                + (
                    f"<div class='muted' style='padding-left:12px;'>1D {_fmt_optional_float((item.get('stats_1d') or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float((item.get('stats_1d') or {}).get('hit_rate'), suffix='%', digits=1)}"
                    f" · 3D {_fmt_optional_float((item.get('stats_3d') or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float((item.get('stats_3d') or {}).get('hit_rate'), suffix='%', digits=1)}"
                    f" · 5D {_fmt_optional_float((item.get('stats_5d') or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float((item.get('stats_5d') or {}).get('hit_rate'), suffix='%', digits=1)}</div>"
                )
            )
            for item in leaders[:4]
        ) or "<div class='muted'>-</div>"
        return f"""
        <article class="card">
          <div class="eyebrow">{title}</div>
          <div class="muted">{html.escape(str(attribution.get('headline') or '-'))}</div>
          <div class="playbook" style="margin-top:14px;">
            <div style="font-weight:800;margin-bottom:6px;">{'今日 Top 5 主要来源模板' if lang == 'zh' else 'Template drivers behind today Top 5'}</div>
            {leader_rows}
          </div>
        </article>
        """
    def _ai_report_name_cell(item: dict, *, link: bool = False) -> str:
        ticker = html.escape(str(item.get("ticker") or "-"))
        name = html.escape(str(item.get("name") or item.get("ticker") or "-"))
        href = f"/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}"
        title_html = f"<a href='{href}' style='font-weight:800;color:var(--ink);'>{name}</a>" if link else f"<div style='font-weight:800'>{name}</div>"
        return title_html + f"<div class='muted'>{ticker}</div>"
    portfolio_rows_html = "".join(
        "<tr>"
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}</td>"
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
    market_watch_rows = report.get("market_watch_recommendations") or []
    rows_html = "".join(
        "<tr>"
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}</td>"
        f"<td>{item.get('verdict') or '-'}<div class='muted'>{html.escape(format_trade_status(item.get('tradability_status'), lang=lang))}</div></td>"
        f"<td>{item.get('confidence') or '-'}</td>"
        f"<td>{item.get('quant_rank') or '-'}<div class='muted'>验证分: {item.get('verification_score') or '-'}</div></td>"
        f"<td>{item.get('strategy') or '-'}<div class='muted'>仓位: {item.get('target_weight') or '-'}</div><div class='muted'>就绪度: {item.get('trade_readiness_score') or '-'} / {item.get('readiness_bucket') or '-'}</div><div class='muted'><a href='{html.escape(_reason_screen_href(reason=item.get('block_reason'), status=item.get('tradability_status'), market=item.get('market'), lang=lang), quote=True)}'>{html.escape(build_trade_explain_text(item, lang=lang))}</a></div><div class='muted'>{html.escape(str(item.get('report_pool_reason') or '-'))}</div><div class='muted'><a href='{html.escape(_reason_screen_href(reason=item.get('block_reason'), status=item.get('tradability_status'), market=item.get('market'), lang=lang), quote=True)}'>{'查看同类筛选' if lang == 'zh' else 'Open screener'}</a></div></td>"
        f"<td>{item.get('entry_trigger') or '-'}<div class='muted'>失效: {item.get('invalidation_condition') or '-'}</div></td>"
        f"<td>{item.get('time_horizon') or '-'}<div class='muted'>滑点: {item.get('max_slippage_bps') or '-'}bps · 流动性: {item.get('liquidity_bucket') or '-'}</div></td>"
        f"<td>{item.get('verification_note') or '-'}<div class='muted'>止损: {item.get('stop_loss', '-')} · {item.get('stop_loss_type') or '-'}</div></td>"
        f"<td>{item.get('headline') or '-'}<div class='muted'>{item.get('summary') or '-'}</div></td>"
        "</tr>"
        for item in market_recommendation_rows[:5]
    ) or f"<tr><td colspan='10'>{'当前没有满足条件的可执行买入池，今天更适合少做或只观察。' if lang == 'zh' else 'No executable buy-pool candidates right now. Today is better treated as a watch-first session.'}</td></tr>"
    watch_rows_html = "".join(
        "<tr>"
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
        f"<td>{html.escape(str(item.get('ticker') or '-'))}</td>"
        f"<td>{item.get('verdict') or '-'}<div class='muted'>{html.escape(format_trade_status(item.get('tradability_status'), lang=lang))}</div></td>"
        f"<td>{item.get('confidence') or '-'}</td>"
        f"<td>{item.get('quant_rank') or '-'}<div class='muted'>验证分: {item.get('verification_score') or '-'}</div></td>"
        f"<td>{item.get('strategy') or '-'}<div class='muted'>偏离买点: {item.get('close_vs_buy_zone_high_pct') or '-'}%</div><div class='muted'>{html.escape(str(item.get('report_pool_reason') or '-'))}</div></td>"
        f"<td>{item.get('entry_trigger') or '-'}<div class='muted'>失效: {item.get('invalidation_condition') or '-'}</div></td>"
        f"<td>{item.get('latest_price') or item.get('latest_close') or '-'}<div class='muted'>买入区: {((item.get('buy_zone') or {}).get('low') or '-')} - {((item.get('buy_zone') or {}).get('high') or '-')}</div></td>"
        f"<td>{', '.join(item.get('risk_flags') or []) or '-'}</td>"
        f"<td>{item.get('headline') or '-'}<div class='muted'>{item.get('summary') or '-'}</div></td>"
        "</tr>"
        for item in market_watch_rows[:5]
    ) or f"<tr><td colspan='10'>{'当前没有单独列出的强势观察池股票。' if lang == 'zh' else 'No separate strong-watch candidates right now.'}</td></tr>"
    social_payload = report.get("social_signal_summary") or {}
    social_signal_rows = social_payload.get("actionable") or []
    social_accounts = social_payload.get("accounts") or []
    social_signal_rows_html = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('handle') or '-'))}</td>"
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
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
        f"<td>{_ai_report_name_cell(item, link=True)}</td>"
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:1108px; margin:0 auto; }}
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
                <a class="pill" href="/dashboard/premarket-plan?lang={lang}">{'盘前便签' if lang == 'zh' else 'Premarket Plan'}</a>
                <a class="pill" href="/dashboard/realtime-monitor?lang={lang}">{'重点监控台' if lang == 'zh' else 'Live Monitor'}</a>
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
                    <div class="muted" style="margin-top:8px;font-weight:700;color:var(--ink);">{lightgbm_execution_bias.get('title') or 'LightGBM：今天先观察'}</div>
                    <div class="muted" style="margin-top:6px;">{lightgbm_execution_bias.get('summary') or '-'}</div>
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
                    {force_send_cta}
                  </div>
                  {"<div class='muted' style='margin-top:10px;color:#fbbf24;'>" + send_guard_note + "</div>" if send_guard_note else ""}
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
              {model_guidance_card_html}
              <section class="card">
                <div class="eyebrow">{'一、持仓库总结' if lang == 'zh' else '1. Portfolio Review'}</div>
                <div class="muted">{portfolio_summary.get('headline') or '-'}</div>
                <div class="table-wrap"><table>
              <thead>
                <tr><th>名称</th><th>代码</th><th>数量</th><th>成本</th><th>最新价</th><th>盈亏</th><th>AI 判断</th><th>动作桶</th><th>Note</th></tr>
              </thead>
              <tbody>{portfolio_rows_html}</tbody></table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'二、明日可执行买入池' if lang == 'zh' else '2. Executable Buy Pool'}</div>
                <div class="muted">{'这里只保留更接近计划买点、且交易状态更适合次日执行的股票。' if lang == 'zh' else 'Only keep names that are still close to the planned buy zone and structurally more executable for the next session.'}</div>
                {recommendation_meta_badge(market_recommendation_meta)}
                {structure_block('A股固定结构' if lang == 'zh' else 'A-Share structure', market_structure)}
                {template_attribution_block('A股来源归因' if lang == 'zh' else 'A-Share template attribution', market_template_attribution)}
                <div class="table-wrap"><table>
              <thead>
                <tr><th>名称</th><th>代码</th><th>结论</th><th>置信度</th><th>量化 / 验证</th><th>策略 / 仓位</th><th>触发 / 失效</th><th>周期 / 流动性</th><th>验证 / 止损</th><th>Headline / Summary</th></tr>
              </thead>
              <tbody>{rows_html}</tbody></table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'三、强势观察池' if lang == 'zh' else '3. Strong Watch Pool'}</div>
                <div class="muted">{'这里放的是值得盯盘但不适合直接追的股票：通常已经偏离买点，或者仍处于 REVIEW 状态。' if lang == 'zh' else 'These are names worth watching but not chasing directly: usually extended beyond the buy zone or still in REVIEW status.'}</div>
                <div class="table-wrap"><table>
              <thead>
                <tr><th>名称</th><th>代码</th><th>结论</th><th>置信度</th><th>量化 / 验证</th><th>观察理由</th><th>触发 / 失效</th><th>当前价 / 买入区</th><th>风险标签</th><th>Headline / Summary</th></tr>
              </thead>
              <tbody>{watch_rows_html}</tbody></table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'四、X 账户社交信号验证' if lang == 'zh' else '4. X Account Signal Validation'}</div>
                <div class="muted">{'这里不是直接照单买入，而是把社交观点和模型信号、触发条件、自选/持仓状态做交叉验证。' if lang == 'zh' else 'This does not copy trades directly; it cross-validates social views against model signals, triggers, watchlist, and portfolio state.'}</div>
                <div class="table-wrap"><table>
              <thead>
                <tr><th>账号</th><th>股票</th><th>观点</th><th>验证分</th><th>模型</th><th>系统动作</th><th>原因</th></tr>
              </thead>
              <tbody>{social_signal_rows_html}</tbody></table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'五、X 热点美股验证' if lang == 'zh' else '5. X U.S. Hotspot Validation'}</div>
                <div class="muted">{'把 X 帖子里提到的美股，与后台预计算的美股模型候选做交叉验证。没有重合时不强行推荐。' if lang == 'zh' else 'Cross-check U.S. tickers mentioned on X against precomputed U.S. model candidates. No overlap means no forced recommendation.'}</div>
                <div class="table-wrap"><table>
              <thead>
                <tr><th>账号</th><th>股票</th><th>X观点</th><th>美股模型</th><th>模型动作</th><th>验证结论</th></tr>
              </thead>
              <tbody>{us_hotspot_rows_html}</tbody></table></div>
              </section>
              {recommendation_meta_badge(us_market_recommendation_meta)}
              {structure_block('美股固定结构' if lang == 'zh' else 'U.S. structure', us_market_structure)}
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
    bias_filter = str(request.query_params.get("bias_filter") or "ALL").strip().upper()
    window_filter = str(request.query_params.get("window") or "ALL").strip().upper()
    history = list_ai_daily_report_history(limit=60, db=db)

    def _bias_bucket(payload: dict) -> str:
        bias = payload.get("lightgbm_execution_bias") or {}
        title = str(bias.get("title") or "").strip().lower()
        if "突破" in title or "breakout" in title:
            return "BREAKOUT"
        if "回踩" in title or "pullback" in title:
            return "PULLBACK"
        if "观察" in title or "watch" in title:
            return "WATCH"
        return "UNKNOWN"

    tagged_history: list[dict] = []
    today = datetime.now(timezone(timedelta(hours=8))).date()

    def _within_window(snapshot_date: str) -> bool:
        if window_filter == "ALL":
            return True
        try:
            snap_date = datetime.strptime(snapshot_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return False
        if window_filter == "7D":
            return snap_date >= (today - timedelta(days=6))
        if window_filter == "30D":
            return snap_date >= (today - timedelta(days=29))
        return True

    for item in history:
        payload = item.get("payload") or {}
        snapshot_date = str(item.get("snapshot_date") or payload.get("report_date") or "")
        tagged_history.append(
            {
                **item,
                "_bias_bucket": _bias_bucket(payload),
                "_in_window": _within_window(snapshot_date),
            }
        )

    tagged_history = [item for item in tagged_history if item.get("_in_window")]
    if bias_filter != "ALL":
        history = [item for item in tagged_history if item.get("_bias_bucket") == bias_filter]
    else:
        history = tagged_history

    counts = {
        key: sum(1 for item in tagged_history if item.get("_bias_bucket") == key)
        for key in ("BREAKOUT", "PULLBACK", "WATCH")
    }
    history_cache: dict[tuple[str, str], list[dict]] = {}

    def _bucket_stats(items: list[dict]) -> dict:
        window_values: dict[int, list[float]] = {1: [], 3: [], 5: [], 10: []}
        measured_count = 0
        for item in items:
            payload = item.get("payload") or {}
            report_date = str(item.get("snapshot_date") or payload.get("report_date") or "")
            rows = _report_market_rows(payload)
            for row in rows[:5]:
                ticker = str(row.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                market_code = str(row.get("market") or "").strip().upper() or ("CN" if ticker.endswith((".SS", ".SZ", ".SH", ".BJ")) else "US")
                cache_key = (market_code, ticker)
                if cache_key not in history_cache:
                    history_cache[cache_key] = load_lake_price_history(market=market_code, ticker=ticker, limit=260)
                history_rows = history_cache.get(cache_key) or []
                row_measured = False
                for session in (1, 3, 5, 10):
                    value = _forward_return_from_history(history_rows, trade_date=report_date, sessions=session)
                    if value is None:
                        continue
                    window_values[session].append(float(value))
                    row_measured = True
                if row_measured:
                    measured_count += 1
        return {
            "report_count": len(items),
            "measured_count": measured_count,
            "windows": {session: _aggregate_window_stats(values) for session, values in window_values.items()},
        }

    current_stats = _bucket_stats(history)
    current_windows = current_stats.get("windows") or {}
    avg_return = (current_windows.get(5) or {}).get("avg_return")
    hit_rate = (current_windows.get(5) or {}).get("hit_rate")
    measured_count = int(current_stats.get("measured_count") or 0)
    hit_count = int((current_windows.get(5) or {}).get("count") or 0)
    bucket_labels = {
        "BREAKOUT": "突破偏向" if lang == "zh" else "Breakout",
        "PULLBACK": "回踩偏向" if lang == "zh" else "Pullback",
        "WATCH": "观察偏向" if lang == "zh" else "Watch",
    }
    comparison_rows = ""
    for key in ("BREAKOUT", "PULLBACK", "WATCH"):
        bucket_items = [item for item in tagged_history if item.get("_bias_bucket") == key]
        bucket_stats = _bucket_stats(bucket_items)
        row_href = f"/dashboard/ai-daily-report/history?lang={lang}&bias_filter={key}&window={window_filter}"
        comparison_rows += (
            f"<tr style='cursor:pointer;' onclick=\"window.location.href='{row_href}'\">"
            f"<td><a href='{row_href}' style='font-weight:800;color:var(--ink);'>{bucket_labels[key]}</a></td>"
            f"<td>{int(bucket_stats.get('report_count') or 0)}</td>"
            f"<td>{int(bucket_stats.get('measured_count') or 0)}</td>"
            f"<td>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(((bucket_stats.get('windows') or {}).get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )
    if not comparison_rows:
        comparison_rows = f"<tr><td colspan='7'>{'暂无可对照的偏向统计。' if lang == 'zh' else 'No comparable bias statistics yet.'}</td></tr>"

    def _filter_pill(label: str, key: str) -> str:
        active = bias_filter == key
        href = f"/dashboard/ai-daily-report/history?lang={lang}&bias_filter={key}&window={window_filter}"
        style = (
            "border-color:rgba(61,217,182,0.55);color:var(--accent);"
            if active
            else ""
        )
        return f"<a class='pill' href='{href}' style='{style}'>{label}</a>"

    def _window_pill(label: str, key: str) -> str:
        active = window_filter == key
        href = f"/dashboard/ai-daily-report/history?lang={lang}&bias_filter={bias_filter}&window={key}"
        style = (
            "border-color:rgba(61,217,182,0.55);color:var(--accent);"
            if active
            else ""
        )
        return f"<a class='pill' href='{href}' style='{style}'>{label}</a>"

    filter_pills = "".join(
        [
            _filter_pill("全部" if lang == "zh" else "All", "ALL"),
            _filter_pill((f"突破偏向 {counts['BREAKOUT']}" if lang == "zh" else f"Breakout {counts['BREAKOUT']}"), "BREAKOUT"),
            _filter_pill((f"回踩偏向 {counts['PULLBACK']}" if lang == "zh" else f"Pullback {counts['PULLBACK']}"), "PULLBACK"),
            _filter_pill((f"观察偏向 {counts['WATCH']}" if lang == "zh" else f"Watch {counts['WATCH']}"), "WATCH"),
        ]
    )
    window_pills = "".join(
        [
            _window_pill("全部时间" if lang == "zh" else "All time", "ALL"),
            _window_pill("近 7 天" if lang == "zh" else "Last 7D", "7D"),
            _window_pill("近 30 天" if lang == "zh" else "Last 30D", "30D"),
        ]
    )
    summary_cards = f"""
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:16px;">
      <div class="card" style="padding:16px;border-radius:18px;box-shadow:none;">
        <div class="eyebrow">{'当前筛选' if lang == 'zh' else 'Current filter'}</div>
        <div style="font-size:22px;font-weight:900;">{html.escape({'ALL':'全部','BREAKOUT':'突破','PULLBACK':'回踩','WATCH':'观察'}.get(bias_filter, bias_filter) if lang == 'zh' else bias_filter.title())}</div>
        <div class="muted">{'历史日报条数' if lang == 'zh' else 'Reports'}: {len(history)} · {html.escape({'ALL':'全部时间','7D':'近 7 天','30D':'近 30 天'}.get(window_filter, window_filter) if lang == 'zh' else {'ALL':'All time','7D':'Last 7D','30D':'Last 30D'}.get(window_filter, window_filter))}</div>
      </div>
      <div class="card" style="padding:16px;border-radius:18px;box-shadow:none;">
        <div class="eyebrow">{'5日平均收益' if lang == 'zh' else '5D Avg Return'}</div>
        <div style="font-size:22px;font-weight:900;">{_fmt_optional_float(avg_return, suffix='%', digits=2) if avg_return is not None else '-'}</div>
        <div class="muted">{'基于候选池可测样本' if lang == 'zh' else 'Across measurable candidate-pool rows'}: {measured_count}</div>
      </div>
      <div class="card" style="padding:16px;border-radius:18px;box-shadow:none;">
        <div class="eyebrow">{'5日上涨命中率' if lang == 'zh' else '5D Hit Rate'}</div>
        <div style="font-size:22px;font-weight:900;">{_fmt_optional_float(hit_rate, suffix='%', digits=1) if hit_rate is not None else '-'}</div>
        <div class="muted">{'5日可测样本' if lang == 'zh' else '5D measured rows'}: {hit_count}</div>
      </div>
    </div>
    """
    rows_html = ""
    for item in history:
        payload = item.get("payload") or {}
        top5 = _report_market_rows(payload)
        lightgbm_execution_bias = payload.get("lightgbm_execution_bias") or {}
        report_stats = _bucket_stats([item])
        top5_text = ", ".join(
            (
                f"{str(row.get('name') or '').strip()}（{str(row.get('ticker') or '').strip()}）"
                if str(row.get("name") or "").strip() and str(row.get("ticker") or "").strip() and str(row.get("name") or "").strip() != str(row.get("ticker") or "").strip()
                else str(row.get("name") or row.get("ticker") or "").strip()
            )
            for row in top5[:5]
            if row.get("ticker") or row.get("name")
        ) or "-"
        actionable_count = len(payload.get("market_recommendations") or payload.get("rows") or [])
        watch_count = len(payload.get("market_watch_recommendations") or [])
        portfolio_rows = payload.get("portfolio_rows") or []
        rows_html += (
            "<tr>"
            f"<td><a href='/dashboard/ai-daily-report/history/{int(item.get('id'))}?lang={lang}'>{html.escape(str(item.get('snapshot_date') or '-'))}</a>"
            f"<div class='muted'>#{int(item.get('id'))} · {_display_time(item.get('created_at'), with_tz=True)}</div></td>"
            f"<td>{html.escape(str(payload.get('mood') or '-'))}"
            f"<div class='muted'>{html.escape(str(payload.get('headline') or '-'))}</div>"
            f"<div class='muted' style='margin-top:6px;color:var(--ink);font-weight:700;'>{html.escape(str(lightgbm_execution_bias.get('title') or 'LightGBM：未记录'))}</div>"
            f"<div class='muted'>{html.escape(str(lightgbm_execution_bias.get('summary') or '-'))}</div></td>"
            f"<td>{len(portfolio_rows)}</td>"
            f"<td>{html.escape(top5_text)}<div class='muted'>{'可执行' if lang == 'zh' else 'Executable'} {actionable_count} · {'观察' if lang == 'zh' else 'Watch'} {watch_count}</div></td>"
            f"<td>{_fmt_optional_float(((report_stats.get('windows') or {}).get(1) or {}).get('avg_return'), suffix='%', digits=2)}"
            f"<div class='muted'>3D {_fmt_optional_float(((report_stats.get('windows') or {}).get(3) or {}).get('avg_return'), suffix='%', digits=2)}</div>"
            f"<div class='muted'>5D {_fmt_optional_float(((report_stats.get('windows') or {}).get(5) or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float(((report_stats.get('windows') or {}).get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div>"
            f"<div class='muted'>10D {_fmt_optional_float(((report_stats.get('windows') or {}).get(10) or {}).get('avg_return'), suffix='%', digits=2)}</div></td>"
            f"<td><a class='cta' href='/dashboard/ai-daily-report/history/{int(item.get('id'))}?lang={lang}'>{'打开' if lang == 'zh' else 'Open'}</a></td>"
            "</tr>"
        )
    if not rows_html:
        rows_html = f"<tr><td colspan='6'>{'暂无历史日报。下一次生成或发送 AI 日报后会自动保存。' if lang == 'zh' else 'No historical reports yet. The next generated or sent AI report will be archived automatically.'}</td></tr>"

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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:1108px; margin:0 auto; }}
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
          tbody tr:hover {{ background:rgba(61,217,182,0.05); }}
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
                <div class="toolbar" style="margin-top:14px;">{filter_pills}</div>
                <div class="toolbar" style="margin-top:10px;">{window_pills}</div>
                {summary_cards}
                <div class="muted" style="margin-top:12px;">{'点击下面任一偏向行，可以直接筛出对应日报。主值是平均收益，小字是上涨命中率。' if lang == 'zh' else 'Click any bias row below to filter the archive directly. Main values are average returns and muted values are hit rates.'}</div>
                <div class="table-wrap" style="margin-top:16px;"><table>
                  <thead><tr><th>{'执行偏向' if lang == 'zh' else 'Bias'}</th><th>{'日报数' if lang == 'zh' else 'Reports'}</th><th>{'可测样本' if lang == 'zh' else 'Measured'}</th><th>1D</th><th>3D</th><th>5D</th><th>10D</th></tr></thead>
                  <tbody>{comparison_rows}</tbody>
                </table></div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'日期' if lang == 'zh' else 'Date'}</th><th>{'市场判断' if lang == 'zh' else 'Market View'}</th><th>{'持仓数' if lang == 'zh' else 'Holdings'}</th><th>{'候选池' if lang == 'zh' else 'Candidate Pool'}</th><th>{'历史验证' if lang == 'zh' else 'Validation'}</th><th>{'操作' if lang == 'zh' else 'Action'}</th></tr></thead>
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
    lightgbm_execution_bias = report.get("lightgbm_execution_bias") or {}
    model_guidance_card_html = _render_ai_report_guidance_bridge(report, lang=lang)
    report_date = str(snapshot.get("snapshot_date") or report.get("report_date") or "")
    outcome_rows = _report_outcome_rows(report, report_date=report_date)
    outcome_summary = _report_outcome_summary(outcome_rows, lang=lang)
    outcome_rows_html = "".join(
        "<tr>"
        f"<td><div style='font-weight:800'>{html.escape(str(item.get('name') or item.get('ticker') or '-'))}</div><div class='muted'>{html.escape(str(item.get('ticker') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('baseline_date') or '-'))}<div class='muted'>{_fmt_optional_float(item.get('baseline_close'), digits=3)}</div></td>"
        f"<td>{html.escape(str(item.get('latest_date') or '-'))}<div class='muted'>{_fmt_optional_float(item.get('latest_close'), digits=3)}</div></td>"
        f"<td>{_fmt_optional_float(item.get('return_pct'), suffix='%', digits=2)}</td>"
        f"<td>{_outcome_status_label(item.get('status'), lang=lang)}</td>"
        "</tr>"
        for item in outcome_rows
    ) or f"<tr><td colspan='5'>{'暂无可验证记录。' if lang == 'zh' else 'No measurable records yet.'}</td></tr>"
    actionable_rows = list(report.get("market_recommendations") or report.get("rows") or [])[:5]
    watch_rows = list(report.get("market_watch_recommendations") or [])[:5]

    def _report_pool_table_rows(rows: list[dict], *, empty_text: str) -> str:
        return "".join(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}' style='font-weight:800;color:var(--ink);'>{html.escape(str(item.get('name') or item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('ticker') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('verdict') or '-'))}<div class='muted'>{html.escape(format_trade_status(item.get('tradability_status'), lang=lang))}</div><div class='muted'>{html.escape(str(item.get('report_pool_reason') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('quant_rank') or '-'))}<div class='muted'>验证分 {html.escape(str(item.get('verification_score') or '-'))}</div><div class='muted'>{html.escape(str(item.get('trade_readiness_score') or '-'))} / {html.escape(str(item.get('readiness_bucket') or '-'))}</div></td>"
            f"<td>{html.escape(str(item.get('entry_trigger') or '-'))}<div class='muted'>失效: {html.escape(str(item.get('invalidation_condition') or '-'))}</div><div class='muted'>{'现价' if lang == 'zh' else 'Latest'}: {html.escape(_fmt_optional_float(item.get('latest_price') or item.get('latest_close'), digits=3))}</div><div class='muted'>{'偏离买点上沿' if lang == 'zh' else 'Vs buy-zone high'}: {html.escape(_fmt_optional_float(item.get('close_vs_buy_zone_high_pct'), suffix='%', digits=1))}</div><div class='muted'><a href='{html.escape(_reason_screen_href(reason=item.get('block_reason'), status=item.get('tradability_status'), market=item.get('market'), lang=lang), quote=True)}'>{html.escape(build_trade_explain_text(item, lang=lang))}</a></div><div class='muted'><a href='{html.escape(_reason_screen_href(reason=item.get('block_reason'), status=item.get('tradability_status'), market=item.get('market'), lang=lang), quote=True)}'>{'查看同类筛选' if lang == 'zh' else 'Open screener'}</a></div></td>"
            f"<td>{html.escape(str(item.get('headline') or item.get('summary') or '-'))}</td>"
            "</tr>"
            for index, item in enumerate(rows, start=1)
        ) or f"<tr><td colspan='6'>{empty_text}</td></tr>"

    actionable_rows_html = _report_pool_table_rows(
        actionable_rows,
        empty_text=('当日没有可执行买入池记录。' if lang == 'zh' else 'No executable buy-pool rows were archived for this report.'),
    )
    watch_rows_html = _report_pool_table_rows(
        watch_rows,
        empty_text=('当日没有强势观察池记录。' if lang == 'zh' else 'No strong watch-pool rows were archived for this report.'),
    )

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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:1108px; margin:0 auto; }}
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
                <div class="eyebrow">LightGBM Bias</div>
                <div class="muted" style="font-weight:800;color:var(--ink);">{html.escape(str(lightgbm_execution_bias.get('title') or ('LightGBM：未记录' if lang == 'zh' else 'LightGBM: not recorded')))}</div>
                <div class="muted" style="margin-top:6px;">{html.escape(str(lightgbm_execution_bias.get('summary') or '-'))}</div>
              </section>
              {model_guidance_card_html}
              <section class="card">
                <div class="eyebrow">{'事后表现验证' if lang == 'zh' else 'Outcome Check'}</div>
                <div class="muted">{html.escape(outcome_summary)}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'基准收盘' if lang == 'zh' else 'Baseline Close'}</th><th>{'最新收盘' if lang == 'zh' else 'Latest Close'}</th><th>{'区间收益' if lang == 'zh' else 'Return'}</th><th>{'状态' if lang == 'zh' else 'Status'}</th></tr></thead>
                  <tbody>{outcome_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'当日可执行买入池' if lang == 'zh' else 'Executable Buy Pool'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>#</th><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'结论' if lang == 'zh' else 'Verdict'}</th><th>{'量化/验证' if lang == 'zh' else 'Quant/Validation'}</th><th>{'触发/失效' if lang == 'zh' else 'Trigger/Invalidation'}</th><th>Headline</th></tr></thead>
                  <tbody>{actionable_rows_html}</tbody>
                </table></div>
              </section>
              <section class="card">
                <div class="eyebrow">{'当日强势观察池' if lang == 'zh' else 'Strong Watch Pool'}</div>
                <div class="table-wrap"><table>
                  <thead><tr><th>#</th><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'结论' if lang == 'zh' else 'Verdict'}</th><th>{'量化/验证' if lang == 'zh' else 'Quant/Validation'}</th><th>{'触发/失效' if lang == 'zh' else 'Trigger/Invalidation'}</th><th>Headline</th></tr></thead>
                  <tbody>{watch_rows_html}</tbody>
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
          .app {{ display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .main {{ padding:20px 18px 28px; }}
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
