"""Dashboard home workspace and its fragment routes."""

import html

import json

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request

from fastapi.responses import HTMLResponse

from sqlalchemy.orm import Session

from app.api.presentation.i18n import t
from app.api.presentation.dashboard_legacy import render_dashboard_legacy_page

from app.api.presentation.styles_dashboard import (
    DASHBOARD_WORKSPACE_STYLE,
)

from app.core.db import get_db_session

from app.services.ai_daily_report import (
    build_close_review_action_feed,
    format_risk_flags,
    format_trade_gate_reason,
    format_trade_status,
)

from app.services.auth import is_authenticated, login_redirect
from app.services.continuous_leaders import build_continuous_leader_view

from app.services.focus_pool import enrich_focus_pool_with_symbols, load_today_focus_pool

from app.services.market_intelligence import build_market_narrative_brief

from app.services.market_news import MarketNewsService

from app.services.model_selection_guidance import (
    load_model_selection_guidance_snapshot,
    summarize_model_selection_guidance,
)

from app.services.model_signal_summary import build_model_state, model_confidence

from app.services.nlp_snapshots import summarize_news_rows

from app.services.portfolio_book import (
    load_portfolio_positions,
)

from app.services.price_snapshot import load_latest_close

from app.services.repository import (
    PredictionRepository,
    SymbolRepository,
    WatchlistRepository,
)

from app.services.runtime_cache import get_or_set

from app.services.screener import ScreenerService

from app.services.template_evaluation import (
    build_lightgbm_prediction_evaluation,
)

from app.services.ui_lang import resolve_request_lang

from app.services.workspace_nav import render_workspace_nav_html

from app.services.workspace_snapshots import (
    SNAPSHOT_DASHBOARD_NLP,
    SNAPSHOT_HOME_PORTFOLIO,
    SNAPSHOT_HOME_WATCHLIST,
    SNAPSHOT_MODEL_CANDIDATES,
    SNAPSHOT_PIPELINE_STATUS,
    SNAPSHOT_WATCHLIST_NLP,
    load_latest_workspace_snapshot,
)


from app.api.routes.dashboard._common import _clamp_lookback_runs, _compact_job_type, _compact_label, _compact_run_name, _concept_ticker_watch_state, _dashboard_home_signal, _dashboard_model_badge, _dashboard_watchlist_map, _display_job_message, _dt, _find_latest_job_by_type, _load_cached_ai_daily_report, _load_home_summary, _load_summary, _reason_screen_link, _score_sparkline_svg, _signal_pill, _summarize_screener_precompute_job
from app.services.dashboard_insights import (
    _display_time,
    _fmt_optional_float,
)

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
    continuous_rows_source = build_continuous_leader_view(
        list(market_context.get("continuous_leaders", [])), watchlist_map,
        market=continuous_market, state=continuous_state,
        # Home has historically treated signal sorting as the default hits sort.
        sort_by="hits" if continuous_sort_by == "signal" else continuous_sort_by,
        sort_order=continuous_sort_order,
    )["rows"]
    continuous_rows_parts: list[str] = []
    for item in continuous_rows_source:
        state_label, state_bg, state_fg = _concept_ticker_watch_state(watchlist_map, item["ticker"], lang)
        continuous_rows_parts.append(
            "<article class='leader-card'>"
            f"<div class='leader-top'><a class='leader-ticker' href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a><span class='leader-market'>{item['market']}</span></div>"
            f"<div class='leader-name'>{item['name']}</div>"
            f"<div class='leader-metrics'><span class='leader-chip'>{item['hits']}/{item['runs']} {t(lang, '次', 'hits')}</span><span class='leader-chip'>{item['score']:.4f}</span></div>"
            f"<div style='margin:0 0 8px 0;'>{_dashboard_model_badge(item.get('state'), confidence=item.get('confidence'), compact=True)}</div>"
            f"<div style='margin-bottom:8px;'>{_signal_pill(item.get('score'), lang=lang, strength=int(item.get('signal_strength') or 0), compact=True)}</div>"
            f"<div class='leader-trend'>{_score_sparkline_svg(item.get('score_history', []))}</div>"
            f"<div class='leader-foot'><span>{item.get('trade_date') or '-'}</span><span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;background:{state_bg};color:{state_fg};font-size:12px;font-weight:800;white-space:nowrap;'>{state_label}</span></div>"
            "</article>"
        )
    continuous_rows = "".join(continuous_rows_parts[:3]) or f"<div class='muted'>{t(lang, '暂无连续强势股', 'No continuous leaders yet')}</div>"
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
    ) or f"<div class='muted'>{t(lang, '暂无可执行候选', 'No actionable candidates yet')}</div>"
    blocked_rows = "".join(
        "<div style='display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{item.get('ticker')}</div><div class='muted'>{item.get('name') or item.get('ticker')}</div><div class='muted'>{_reason_screen_link(format_trade_gate_reason(item.get('block_reason'), lang=lang), reason=item.get('block_reason'), status=item.get('status_label'), market=item.get('market'), lang=lang)}</div><div class='muted'>{_reason_screen_link('查看同类筛选' if lang == 'zh' else 'Open screener', reason=item.get('block_reason'), status=item.get('status_label'), market=item.get('market'), lang=lang)}</div></div>"
        f"<div style='text-align:right;'><span class='signal {item.get('status_tone')}'>{html.escape(format_trade_status(item.get('status_label'), lang=lang))}</span><div class='muted'>{html.escape(format_risk_flags(item.get('risk_flags') or [], lang=lang))}</div></div>"
        "</div>"
        for item in signal_sets["blocked"]
    ) or f"<div class='muted'>{t(lang, '暂无受阻候选', 'No blocked candidates')}</div>"
    review_rows = "".join(
        "<div style='display:flex;justify-content:space-between;gap:8px;padding:10px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{item.get('ticker')}</div><div class='muted'>{item.get('name') or item.get('ticker')}</div><div class='muted'>{item.get('execution_note') or item.get('invalidation_condition') or '-'}</div>"
        + (f"<div class='muted' style='font-weight:700;color:#f59e0b;margin-top:4px;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div style='text-align:right;'><span class='signal {item.get('status_tone')}'>{item.get('status_label')}</span><div class='muted'>{html.escape(format_risk_flags(item.get('risk_flags') or [], lang=lang))}</div></div>"
        "</div>"
        for item in signal_sets["trim_review"]
    ) or f"<div class='muted'>{t(lang, '暂无复核队列', 'No review queue yet')}</div>"
    return f"""
      <article class="card">
        <div class="eyebrow">{t(lang, '今日行动板', 'Today Action Board')}</div>
        <div class="muted">{t(lang, '把今天最该先看的快照榜首、连续强势股和重点盯盘池放在一起。', 'A compact board that combines top snapshot names, continuous leaders, and today focus items.')}</div>
        <div class="stack" style="margin-top:12px;">
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{t(lang, '市场快照', 'Market Snapshot')}</div>
            <div class="muted">{'<br/>'.join(snapshot_top_lines) or '-'}</div>
          </div>
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{t(lang, '连续强势股', 'Continuous Leaders')}</div>
            <div class="muted">{" / ".join(f"{item.get('ticker')} · {item.get('name') or item.get('ticker')}" for item in continuous_rows_source[:3]) or '-'}</div>
          </div>
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{t(lang, '今日重点盯盘池', 'Today Focus Pool')}</div>
            <div class="muted">{'<br/>'.join(focus_lines) or '-'}</div>
          </div>
          <div class="stack">
            <a class="action-link" href="/screeners/market-snapshot?lang={lang}&mode={session_mode}">{t(lang, '打开市场快照榜单', 'Open Market Snapshot')}</a>
            <a class="action-link" href="/screeners/focus/today?lang={lang}">{t(lang, '打开今日重点盯盘池', 'Open Today Focus Pool')}</a>
            <a class="action-link" href="/watchlist?lang={lang}&mode={session_mode}">{_dt(lang, 'open_watchlist')}</a>
          </div>
        </div>
      </article>
      <article class="card">
        <div class="eyebrow">{t(lang, '交易候选', 'Actionable Candidates')}</div>
        <div class="muted">{t(lang, '先看今天能做、要复核、以及不能做的票。', 'Start with what is actionable, what needs review, and what is blocked.')}</div>
        <div class="stack" style="margin-top:12px;">
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{t(lang, '可执行', 'Ready')}</div>
            {actionable_rows}
          </div>
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{t(lang, '待复核 / 减仓', 'Review / Trim')}</div>
            {review_rows}
          </div>
          <div>
            <div class="muted" style="font-weight:700;margin-bottom:6px;">{t(lang, '受阻候选', 'Blocked')}</div>
            {blocked_rows}
          </div>
        </div>
      </article>
      <article class="card">
        <div class="eyebrow">{t(lang, '市场叙事', 'Market Narrative')}</div>
        <div class="muted">{market_narrative.get('headline') or '-'}</div>
        <div class="stack" style="margin-top:12px;">
          {"".join(f"<div class='muted'>{item}</div>" for item in (market_narrative.get('bullets') or [])) or "<div class='muted'>-</div>"}
          {"".join(f"<div class='muted'><a href='{item.get('link') or '#'}' target='_blank' rel='noreferrer'>{item.get('title')}</a></div>" for item in market_headlines) if market_headlines else ""}
        </div>
      </article>
      <article class="card">
        <div class="eyebrow">{t(lang, '最近任务状态', 'Recent Job Status')}</div>
        <div class="muted">{t(lang, '直接看最近任务是否成功、部分完成，还是失败。', 'A compact view of whether the latest jobs finished successfully, partially, or failed.')}</div>
        <div class="stack" style="margin-top:12px;">
          {"".join(recent_job_lines) or f"<div class='muted'>{t(lang, '暂无任务记录', 'No recent jobs yet')}</div>"}
          <a class="action-link" href="/dashboard/ops/jobs?lang={lang}">{t(lang, '打开任务记录页', 'Open Job History')}</a>
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
    top_model_title = html.escape(str(guidance_summary.get("top_model_title") or (t(lang, "样本继续沉淀", "Still collecting samples"))))
    top_combo_title = html.escape(str(guidance_summary.get("top_combo_title") or (t(lang, "组合样本继续沉淀", "Combo samples still accumulating"))))
    top_model_copy = html.escape(str(guidance_summary.get("top_model_summary") or (t(lang, "当前还没有足够样本。", "Not enough samples yet."))))
    top_combo_copy = html.escape(str(guidance_summary.get("top_combo_summary") or (t(lang, "当前还没有足够组合样本。", "Not enough combo samples yet."))))
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
        f"<div style='margin-bottom:8px;'><span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;background:{build_model_state(item.get('score'), lang=lang, percentile=item.get('percentile'))['bg']};color:{build_model_state(item.get('score'), lang=lang, percentile=item.get('percentile'))['fg']};font-weight:800;font-size:12px;'>{build_model_state(item.get('score'), lang=lang, percentile=item.get('percentile'))['label']}</span></div>"
        f"<div class='signal-score'>{item['score']:.6f}</div>"
        f"<div style='margin-top:6px;'>{_signal_pill(item.get('score'), lang=lang, compact=True)}</div>"
        f"<div class='signal-foot' title='{latest_model['name'] if latest_model else (t(lang, '最新模型', 'Latest model'))}'>{_compact_run_name(latest_model['name'], 24) if latest_model else (t(lang, '最新模型', 'Latest model'))}"
        f"{' · ' + str(model_confidence(item.get('score'))) + '%' if model_confidence(item.get('score')) is not None else ''}</div>"
        "</article>"
        for item in latest_signals[:3]
    ) or f"<div class='muted'>{t(lang, '暂无信号', 'No signals yet')}</div>"
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
            <div class="eyebrow">{t(lang, '优先模型', 'Priority Model')}</div>
            <div style="font-size:22px;font-weight:800;line-height:1.25;margin:4px 0 8px;">{top_model_title}</div>
            <div class="muted">{top_model_copy}</div>
            <div class="muted" style="margin-top:8px;">{source_label} · {source_time}</div>
            <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
              <a class='pill' href='{html.escape(top_model_href, quote=True)}'>{t(lang, '用这套模型去筛股', 'Screen with this model')}</a>
            </div>
          </article>
          <article class="card" style="margin-bottom:0;background:#f9f7f0;">
            <div class="eyebrow">{t(lang, '优先组合', 'Priority Combo')}</div>
            <div style="font-size:22px;font-weight:800;line-height:1.25;margin:4px 0 8px;">{top_combo_title}</div>
            <div class="muted">{top_combo_copy}</div>
            <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
              <a class='pill' href='{html.escape(top_combo_href, quote=True)}'>{t(lang, '用这套组合去筛股', 'Screen with this combo')}</a>
              <a class='pill' href='/dashboard/model-performance?lang={lang}'>{t(lang, '打开模型评测', 'Open Model Performance')}</a>
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
        <div class="muted" style="margin-bottom:10px;">{t(lang, '首页只保留最新前三只信号，完整视图请去选股器或个股页。', 'Only the latest top 3 signals stay on the home page. Use Screeners or Insight pages for the full view.')}</div>
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
        confidence = model_output.get("confidence")
        label, tone = _dashboard_home_signal(score, lang)
        decision = str(label).upper()
        mode_rank = (confidence or 0) * 2 + int(round(float(score or 0.0) * 100))
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
    return ranked[:5]



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
    return rows[:5], totals



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
    db: Session | None = None,
) -> str:
    generated_at = summary["generated_at"]
    auto_analysis = summary["auto_analysis"]
    market_context = summary["market_context"]
    latest_model = summary["latest_model"] or {}
    top_signals = model_candidate_rows or (summary["latest_signals"] or [])[:5]
    risk_overview = market_context.get("risk_overview", {})
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
            "detail": latest_model.get("name") or (t(lang, "尚未训练", "No model run yet.")),
        },
        {
            "label": "核心预计算状态" if lang == "zh" else "Core Precompute",
            "value": _job_status_text(core_precompute_summary.get("status"), lang=lang),
            "status": str(core_precompute_summary.get("status") or "idle").lower(),
            "detail": core_precompute_summary.get("detail") or (t(lang, "核心快照待运行。", "Core snapshot is pending.")),
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
        f"<div class='row-right'><span class='signal {item['signal_tone']}'>{item['signal_label']}</span><div class='mini-metric'>{str(item['confidence']) + '%' if item['confidence'] is not None else '-'}</div></div>"
        "</article>"
        for item in watchlist_rows
    ) or f"<div class='empty'>{t(lang, '还没有自选股', 'No watchlist names yet')}</div>"
    portfolio_html = "".join(
        "<article class='list-row'>"
        f"<div><a class='ticker' href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a><div class='subtle'>{item['name']} · {item['market']}</div></div>"
        f"<div class='row-right'><div class='mini-metric {'neg' if item['pnl'] < 0 else 'pos'}'><span class='dashboard-portfolio-mask' aria-hidden='true'>*****</span><span class='dashboard-portfolio-actual'>{item['pnl_pct']:.1f}%</span></div><span class='signal {item['signal_tone']}'>{item['signal_label']}</span></div>"
        "</article>"
        for item in portfolio_rows
    ) or f"<div class='empty'>{t(lang, '还没有持仓', 'No positions yet')}</div>"
    top_signal_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('trade_date') or '-'}</div><div class='subtle'>{_compact_label(item.get('reason_summary'), 72) if item.get('reason_summary') else (item.get('name') or '-')}</div></div>"
        f"<div class='row-right'><span class='signal {item.get('signal_tone') or _dashboard_home_signal(item.get('score'), lang)[1]}'>{item.get('signal_label') or _dashboard_home_signal(item.get('score'), lang)[0]}</span></div>"
        "</article>"
        for item in top_signals
    ) or f"<div class='empty'>{t(lang, '暂无模型结果', 'No model output yet')}</div>"
    # Rendering must not hold request transactions open during historical replay.
    lightgbm_home_eval = build_lightgbm_prediction_evaluation(
        market="ALL", recent_runs=8, top_n=40, allow_compute=False,
    )
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
            "评测缓存未就绪或成熟样本不足；首页不触发实时评测，请在模型评测页核查。"
            if lang == "zh"
            else "Evaluation cache is not ready or mature samples are insufficient. Check model evaluation; the homepage does not compute it on demand."
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
        f"<div class='row-right'><a class='cta' href='/screeners?lang={lang}&model_template=lightgbm_top_picks&market=CN&universe=full_market&run=1'>{t(lang, '打开 LightGBM', 'Open LightGBM')}</a></div>"
        "</article>"
    )
    recent_jobs_html = "".join(
        "<article class='job-row'>"
        f"<div><div class='job-type' title='{item.get('job_type') or '-'}'>{_compact_job_type(item.get('job_type'), 20) or '-'}</div><div class='subtle'>{_display_time(item.get('started_at') or item.get('created_at'))}</div></div>"
        f"<div class='job-status {str(item.get('status') or '').lower()}'>{item.get('status') or '-'}</div>"
        "</article>"
        for item in recent_jobs[:5]
    ) or f"<div class='empty'>{t(lang, '暂无任务记录', 'No jobs yet')}</div>"
    action_focus_rows = (portfolio_meta or {}).get("watch_items") or []
    signal_sets = _dashboard_signal_action_sets(summary.get("latest_signals") or [])
    regime_view = _dashboard_trading_regime(
        latest_signals=summary.get("latest_signals") or [],
        risk_overview=risk_overview,
        lang=lang,
    )
    close_review_action_feed = (pipeline_payload or {}).get("close_review_action_feed") if isinstance(pipeline_payload, dict) else None
    if not isinstance(close_review_action_feed, dict):
        cached_report = _load_cached_ai_daily_report(db) if db is not None else None
        close_review_action_feed = build_close_review_action_feed(cached_report, lang=lang)
    nlp_meta = (nlp_payload.get("meta") or {}) if isinstance(nlp_payload, dict) else {}
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
    readiness_issue_statuses = {"failed", "partial", "stale", "error"}
    readiness_has_issue = any(str(item.get("status") or "").lower() in readiness_issue_statuses for item in readiness_items)
    system_status_summary = (
        "需要检查数据或模型状态" if readiness_has_issue else "行情、模型与日报状态正常时不打扰决策"
    ) if lang == "zh" else (
        "Data or model status needs attention" if readiness_has_issue else "Data, model, and report status stay out of the way when healthy"
    )
    if regime_view["label"] == (t(lang, "防守", "Defense")):
        decision_href = f"/portfolio?lang={lang}"
        decision_label = "先复核持仓风险" if lang == "zh" else "Review holding risk first"
    else:
        decision_href = f"/screeners?lang={lang}"
        decision_label = "开始发现候选" if lang == "zh" else "Find candidates"
    workflow_steps = [
        (
            "01",
            "市场判断" if lang == "zh" else "Market posture",
            regime_view["label"],
            regime_view["detail"],
            f"/dashboard/market?lang={lang}&lookback_runs={lookback_runs}",
        ),
        (
            "02",
            "发现候选" if lang == "zh" else "Find candidates",
            f"{action_queue_count} {t(lang, '只可执行', 'actionable')}",
            "先看通过交易条件的候选。" if lang == "zh" else "Start with candidates that passed trade gates.",
            f"/screeners?lang={lang}",
        ),
        (
            "03",
            "自选与执行" if lang == "zh" else "Watch & execute",
            f"{len(watchlist_rows)} {t(lang, '只在观察', 'in watchlist')}",
            "写清触发和失效条件，再进入执行。" if lang == "zh" else "Define the trigger and invalidation before acting.",
            f"/watchlist?lang={lang}&mode={session_mode}",
        ),
        (
            "04",
            "持仓与复盘" if lang == "zh" else "Holdings & review",
            f"{risk_reduction_count} {t(lang, '项优先复核', 'priority reviews')}",
            "先处理风险，再阅读日报和历史。" if lang == "zh" else "Handle risk before reading the report and history.",
            f"/portfolio?lang={lang}",
        ),
    ]
    workflow_steps_html = "".join(
        "<a class='workflow-step' href='{href}'>"
        "<span class='workflow-number'>{number}</span>"
        "<span class='workflow-copy'><b>{title}</b><strong>{value}</strong><small>{detail}</small></span>"
        "</a>".format(
            href=html.escape(href, quote=True),
            number=number,
            title=html.escape(title),
            value=html.escape(value),
            detail=html.escape(detail),
        )
        for number, title, value, detail, href in workflow_steps
    )
    action_focus_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('action_reason') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal sig-watch'>{item.get('action_priority') or '-'}</span><div class='mini-metric'>{item.get('action_hint') or '-'}</div></div>"
        "</article>"
        for item in action_focus_rows[:3]
    ) or f"<div class='empty'>{t(lang, '暂无动作焦点', 'No action focus yet')}</div>"
    news_monitor_html = f"""
                <article class="card compact-card">
                  <div class="panel-head compact-head">
                    <div>
                      <div class="eyebrow">{t(lang, '新闻监控', 'News Monitor')}</div>
                      <h3>{t(lang, '新闻详情已移到自选股', 'News details moved to Watchlist')}</h3>
                      <p>{nlp_meta_text}</p>
                    </div>
                    <div class="row-right">
                      <span class="signal {'sig-sell' if int(nlp_meta.get('negative_total') or 0) else 'sig-watch'}">{(t(lang, '风险 ', 'Risk ')) + str(nlp_meta.get('negative_total', 0))}</span>
                      <div class="mini-metric">{_fmt_optional_float(nlp_meta.get('coverage_pct'), suffix='%', digits=1)}</div>
                    </div>
                  </div>
                  <div class="cta-row">
                    <a class="cta primary" href="/watchlist?lang={lang}&news_view=risk#news">{t(lang, '查看新闻风险', 'Review news risks')}</a>
                    <a class="cta" href="/watchlist?lang={lang}&news_view=opportunity#news">{t(lang, '新闻机会', 'News opportunities')}</a>
                    <a class="cta" href="/dashboard/ops?lang={lang}">{t(lang, '覆盖诊断', 'Coverage diagnostics')}</a>
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
    ) or f"<div class='empty'>{t(lang, '暂无主攻候选', 'No primary action candidates yet')}</div>"
    close_review_watch_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('execution_note') or item.get('block_reason') or '-'}</div>"
        + (f"<div class='subtle' style='font-weight:800;color:#f59e0b;'>{html.escape(_dashboard_pseudo_strength_hint(item, lang=lang))}</div>" if _dashboard_pseudo_strength_hint(item, lang=lang) else "")
        + "</div>"
        f"<div class='row-right'><span class='signal sig-watch'>{item.get('tradability_status') or '-'}</span><div class='mini-metric'>{item.get('target_weight') or '-'}</div></div>"
        "</article>"
        for item in (close_review_action_feed.get("blocked") or [])[:3]
    ) or f"<div class='empty'>{t(lang, '暂无只观察名单', 'No watch-only names yet')}</div>"
    close_review_risk_reduce_html = "".join(
        "<article class='signal-row'>"
        f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a><div class='subtle'>{item.get('name') or item.get('ticker')}</div><div class='subtle'>{item.get('invalidation_condition') or item.get('execution_note') or '-'}</div></div>"
        f"<div class='row-right'><span class='signal sig-sell'>{item.get('tradability_status') or '-'}</span><div class='mini-metric'>{item.get('target_weight') or '-'}</div></div>"
        "</article>"
        for item in (close_review_action_feed.get("risk_reduction") or [])[:3]
    ) or f"<div class='empty'>{t(lang, '暂无减仓处理名单', 'No risk-reduction queue yet')}</div>"
    close_review_action_html = f"""
      <div>
        <div class="subtle" style="font-weight:700;margin-bottom:6px;">{t(lang, '明日主攻', 'Primary Action')}</div>
        <div class="list-stack">{close_review_actionable_html}</div>
      </div>
      <div>
        <div class="subtle" style="font-weight:700;margin:12px 0 6px;">{t(lang, '只观察', 'Watch Only')}</div>
        <div class="list-stack">{close_review_watch_html}</div>
      </div>
      <div>
        <div class="subtle" style="font-weight:700;margin:12px 0 6px;">{t(lang, '减仓处理', 'Reduce Risk')}</div>
        <div class="list-stack">{close_review_risk_reduce_html}</div>
      </div>
    """
    pipeline_job_map = {
        "refresh": next(
            (
                item
                for item in recent_jobs
                if str(item.get("job_type") or "").lower()
                in {"refresh_cn_market_data_lake_only", "refresh_cn_market_data_daily", "refresh_cn_market_data"}
            ),
            None,
        ),
        "analysis": next((item for item in recent_jobs if str(item.get("job_type") or "").lower() == "watchlist_auto_analysis"), None),
    }
    pipeline_rows_html = "".join(
        "<article class='job-row'>"
        f"<div><div class='job-type'>{label}</div><div class='subtle'>{_display_time((job or {}).get('finished_at') or (job or {}).get('started_at'))}</div></div>"
        f"<div class='job-status {str((job or {}).get('status') or 'unknown').lower()}'>{(job or {}).get('status') or ('unknown' if lang == 'en' else '未知')}</div>"
        "</article>"
        for label, job in (
            ((t(lang, "行情刷新与快照", "Market Refresh and Snapshots")), pipeline_job_map["refresh"]),
            ((t(lang, "自动分析与训练", "Auto Analysis and Train")), pipeline_job_map["analysis"]),
        )
    )
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
    return render_dashboard_legacy_page(
        "dashboard/legacy/home__render_dashboard_workspace.html",
        fragments=[
            f'{lang}',
            f"{t(lang, 'PQW 工作台', 'PQW Workspace')}",
            f'{DASHBOARD_WORKSPACE_STYLE}',
            f"{t(lang, '量化工作台', 'Trading Workspace')}",
            f'{lead_text}',
            f'{nav_html}',
            f"{t(lang, '自动化', 'Automation')}",
            f'{auto_status_label}',
            f'{auto_status_text}',
            f"{t(lang, '模式', 'Mode')}",
            f'{session_mode}',
            f"{t(lang, '更新', 'Updated')}",
            f'{generated_at}',
            f'{banner_html}',
            f"{t(lang, '按四步完成今天的选股与复盘。', 'Complete today’s selection and review in four steps.')}",
            f"{t(lang, '先判断市场，再发现候选；确认触发条件后进入自选，最后处理持仓风险。', 'Read the market first, then find candidates, validate triggers, and finally handle holding risk.')}",
            f'{mode_toggle}',
            f'{lang_toggle}',
            f"{t(lang, '今日市场许可', 'Today’s market permission')}",
            f"{regime_view['label']}",
            f'{decision_label}',
            f"{regime_view['detail']}",
            f"{t(lang, '模型可信度', 'Model trust')}",
            f'{trust_score}',
            f'{trust_label}',
            f'{decision_href}',
            f'{decision_label}',
            f'{workflow_steps_html}',
            f"{t(lang, '第一次使用：每天只做这四件事', 'New here? Do these four things each day')}",
            f"{t(lang, '市场判断 → 候选 → 自选与执行 → 持仓复盘', 'Market → candidates → watch & execute → holdings review')}",
            f"{t(lang, '不要把模型分数当作买入指令。先确认市场许可，再从可执行候选中选择少量股票写入观察池，并为每只股票设置触发与失效条件。', 'Do not treat a model score as a buy order. Confirm market permission, choose only a few actionable candidates, and define a trigger and invalidation for each.')}",
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '从市场判断开始', 'Start with market posture')}",
            f'{lang}',
            f"{t(lang, '查看候选', 'View candidates')}",
            f"{(' open' if readiness_has_issue else '')}",
            f"{t(lang, '数据与模型状态', 'Data and model status')}",
            f'{system_status_summary}',
            f'{readiness_html}',
            f"{t(lang, '今日首页', 'Home Board')}",
            f"{t(lang, '自选股票', 'Watchlist')}",
            f"{t(lang, '把最该看的股票直接放在第一屏。', 'Keep the most relevant names in the first screenful.')}",
            f'{lang}',
            f'{session_mode}',
            f"{t(lang, '打开完整自选', 'Open watchlist')}",
            f"{t(lang, '显示前', 'Top')}",
            f'{len(watchlist_rows)}',
            f"{t(lang, '按模型优先级排序', 'Ranked by model priority')}",
            f'{watchlist_html}',
            f"{t(lang, '持仓总览', 'Portfolio')}",
            f"{t(lang, '持仓股票', 'Positions')}",
            f"{t(lang, '把盈亏、风险态度和关注顺序放在一起。', 'Show PnL, posture, and review priority together.')}",
            f"{t(lang, '显示持仓盈亏', 'Show portfolio PnL')}",
            f"{t(lang, '隐藏持仓盈亏', 'Hide portfolio PnL')}",
            f"{t(lang, '显示持仓盈亏', 'Show portfolio PnL')}",
            f"{t(lang, '显示持仓盈亏', 'Show portfolio PnL')}",
            f"{t(lang, '打开持仓页', 'Open portfolio')}",
            f"{t(lang, '显示前', 'Top')}",
            f'{len(portfolio_rows)}',
            f"{t(lang, '总盈亏', 'PnL')}",
            f"{portfolio_totals.get('pnl_pct', 0):.1f}",
            f'{portfolio_html}',
            f"{t(lang, '组合暴露', 'Exposure')}",
            f"{t(lang, '组合层先看什么', 'Portfolio-level first look')}",
            f"{t(lang, '先确认行业集中度和动作优先级，再看单票明细。', 'Confirm concentration and action priority before drilling into single names.')}",
            f"{t(lang, '最大行业暴露', 'Top sector exposure')}",
            f'{exposure_text}',
            f"{(portfolio_meta or {}).get('top_market') or '-'}",
            f"{t(lang, '动作优先级分布', 'Action priority mix')}",
            f"{t(lang, '高优先级仓位越多，越需要人工复核。', 'More high-priority names means more manual review is needed.')}",
            f'{action_mix_text}',
            f"{t(lang, '模型机会', 'Model Opportunities')}",
            f"{t(lang, '模型选股入口', 'Model Picks')}",
            f"{t(lang, '下一步不再先看一堆参数，而是先从模板进入。', 'Lead with templates first instead of a wall of parameters.')}",
            f'{lightgbm_home_bias_html}',
            f'{top_signal_html}',
            f'{lang}',
            f"{t(lang, '进入模型选股', 'Open screeners')}",
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '连续强势股', 'Continuous Leaders')}",
            f'{lang}',
            f"{t(lang, '模型评测总览', 'Model Evaluation Overview')}",
            f"{t(lang, '减风险队列', 'Risk Reduction Queue')}",
            f"{t(lang, '优先处理哪些仓位', 'Which positions need action first')}",
            f"{t(lang, '先看该减、该复核、以及偏离目标仓位的仓位。', 'Start with trims, review names, and holdings that are far from target weight.')}",
            f"{t(lang, '打开持仓页', 'Open portfolio')}",
            f'{action_focus_html}',
            f"{t(lang, '盘后动作 Feed', 'Postmarket Action Feed')}",
            f"{close_review_action_feed.get('summary') or t(lang, '盘后动作默认收起，点击展开查看。', 'Postmarket actions are collapsed by default.')}",
            f"{t(lang, '打开 AI 日报', 'Open AI report')}",
            f'{close_review_action_html}',
            f"{t(lang, '新闻与任务状态', 'News and Jobs')}",
            f"{t(lang, '低频检查项默认收起，避免首页变成长日志。', 'Lower-frequency checks stay collapsed to keep the home cockpit short.')}",
            f'{news_monitor_html}',
            f"{t(lang, '任务中心', 'Jobs')}",
            f"{t(lang, '自动任务结果', 'Automated Results')}",
            f'{lang}',
            f'{lookback_runs}',
            f"{t(lang, '打开任务中心', 'Open jobs')}",
            f'{pipeline_rows_html}',
            f'{recent_jobs_html}',
        ],
    )



@router.get("", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    lang = resolve_request_lang(request)
    session_mode = str(request.query_params.get("mode", "monitor")).lower()
    if session_mode not in {"premarket", "monitor", "postmarket"}:
        session_mode = "monitor"
    lookback_runs = _clamp_lookback_runs(request.query_params.get("lookback_runs", 5))
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
        db=db,
    )



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
