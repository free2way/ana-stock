import html
import threading
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db_session
from app.models.schema import SymbolCreate
from app.services.auth import is_authenticated, login_redirect
from app.services.market_sync import sync_market_data
from app.services.market_lake import load_lake_price_history
from app.services.price_snapshot import load_latest_closes
from app.services.repository import PredictionRepository, PredictionTradePlanRepository, SymbolRepository, WatchlistRepository
from app.services.model_signal_summary import build_signal_label, model_confidence, signal_strength
from app.services.nlp_snapshots import summarize_news_rows
from app.services.runtime_cache import clear_namespace, get_or_set
from app.services.symbol_details import SymbolDataService
from app.services.symbol_catalog import infer_symbol_record, search_symbol_catalog
from app.services.ticker_format import normalize_ticker_for_market
from app.services.ui_lang import resolve_request_lang
from app.services.watchlist_metadata import refresh_watchlist_metadata
from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html
from app.services.workspace_snapshots import (
    SNAPSHOT_WATCHLIST_NLP,
    SNAPSHOT_WATCHLIST_WORKSPACE,
    load_latest_workspace_snapshot,
    refresh_workspace_snapshots,
)


router = APIRouter(prefix="/watchlist", tags=["watchlist"])


def _clear_watchlist_caches() -> None:
    clear_namespace("watchlist_items")
    clear_namespace("watchlist_analysis_fragment")
    clear_namespace("watchlist_table_fragment")


def _refresh_workspace_snapshots_async() -> None:
    def _run() -> None:
        try:
            with SessionLocal() as snapshot_db:
                refresh_workspace_snapshots(snapshot_db)
        except Exception:
            return

    threading.Thread(
        target=_run,
        name="watchlist-workspace-refresh",
        daemon=True,
    ).start()


MARKET_OPTIONS = [
    ("US", "US Stocks", "Examples: ASTS, NVDA, AAPL"),
    ("CN", "China A-Shares", "Examples: 600519.SH, 000001.SZ"),
    ("HK", "Hong Kong Stocks", "Examples: 0700.HK, 9988.HK"),
]

MARKET_LABELS = {
    "CN": "A股",
    "HK": "港股",
    "US": "美股",
}


def _redirect_with_message(message: str | None = None) -> RedirectResponse:
    query = urlencode({"message": message}) if message else ""
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/watchlist{suffix}", status_code=303)


def _market_section_label(market: str | None) -> str:
    return MARKET_LABELS.get((market or "").upper(), "其他")


def _decision_chip(value: str, *, lang: str = "en") -> str:
    normalized = str(value or "HOLD").upper()
    bg = "#f3f4f6"
    fg = "#374151"
    if "BUY" in normalized:
        bg, fg = "#dcfce7", "#166534"
    elif "SELL" in normalized:
        bg, fg = "#fee2e2", "#991b1b"
    elif "HOLD" in normalized:
        bg, fg = "#fef3c7", "#92400e"
    if lang == "zh":
        if "BUY" in normalized:
            label = "偏多"
        elif "SELL" in normalized:
            label = "偏弱"
        elif "WATCH" in normalized:
            label = "观察"
        else:
            label = "中性"
    else:
        label = normalized
    return (
        "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;white-space:nowrap;'>{label}</span>"
    )


def _execution_tag_label(tag: str, *, lang: str) -> str:
    normalized = str(tag or "").strip().lower().replace("_", "-")
    if lang != "zh":
        return str(tag or "").strip()
    mapping = {
        "earnings-soon": "财报临近",
        "earnings-near": "财报临近",
        "gap-risk": "跳空风险",
        "gap-risk-high": "跳空风险",
        "breakout-only": "仅看突破",
        "wait-for-breakout": "等待突破",
        "pullback-preferred": "偏向回踩",
        "buy-the-dip": "回踩买点",
        "low-liquidity": "流动性弱",
        "crowded-sector": "板块拥挤",
        "event-pending": "事件待定",
        "trim-on-5pct": "冲高减仓",
        "time-stop-5d": "时间止损",
        "funding-risk": "融资风险",
        "regulatory-risk": "监管风险",
        "news-risk": "消息风险",
        "high-volatility": "高波动",
        "chase-risk": "追高风险",
    }
    return mapping.get(normalized, str(tag or "").strip())


def _execution_tag_chip(tag: str, *, lang: str) -> str:
    normalized = str(tag or "").strip().lower().replace("_", "-")
    bg = "rgba(148,163,184,0.14)"
    fg = "#cbd5e1"
    if any(keyword in normalized for keyword in ("risk", "earnings", "event", "low-liquidity", "crowded")):
        bg = "rgba(220,38,38,0.16)"
        fg = "#fca5a5"
    elif any(keyword in normalized for keyword in ("breakout", "buy-the-dip", "pullback")):
        bg = "rgba(14,165,233,0.16)"
        fg = "#7dd3fc"
    elif any(keyword in normalized for keyword in ("trim", "time-stop")):
        bg = "rgba(245,158,11,0.16)"
        fg = "#fcd34d"
    label = _execution_tag_label(tag, lang=lang)
    return (
        "<span style='display:inline-flex;align-items:center;padding:5px 9px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:700;font-size:11px;white-space:nowrap;'>{html.escape(label)}</span>"
    )


def _execution_tag_chips(tags: list[str] | None, *, lang: str) -> str:
    cleaned = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    if not cleaned:
        return "<span class='muted'>-</span>"
    return "".join(_execution_tag_chip(tag, lang=lang) for tag in cleaned[:3])


def _derive_watchlist_execution_tags(item: dict) -> list[str]:
    combined = item.get("combined_analysis") or {}
    decision = str(combined.get("decision") or "HOLD").upper()
    confidence = int(combined.get("confidence") or 0)
    signal_strength_value = int(combined.get("signal_strength") or 0)
    daily_change_pct = item.get("daily_change_pct")
    try:
        day_move = float(daily_change_pct) if daily_change_pct is not None else None
    except (TypeError, ValueError):
        day_move = None

    derived: list[str] = []
    if decision in {"BUY", "WATCH"} and day_move is not None and day_move >= 3.0:
        derived.append("chase-risk")
    if decision in {"BUY", "WATCH"} and signal_strength_value >= 55 and day_move is not None and day_move >= 1.5:
        derived.append("wait-for-breakout")
    if decision == "BUY" and confidence >= 70 and day_move is not None and day_move <= 0.8:
        derived.append("pullback-preferred")
    if decision == "BUY" and signal_strength_value >= 65 and day_move is not None and day_move >= 1.0:
        derived.append("breakout-only")
    if decision == "WATCH" and signal_strength_value >= 40 and day_move is not None and day_move <= 0:
        derived.append("buy-the-dip")

    seen: set[str] = set()
    ordered: list[str] = []
    for tag in derived:
        clean = str(tag).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        ordered.append(clean)
    return ordered[:3]


def _ensure_watchlist_execution_tags(items: list[dict]) -> None:
    for item in items:
        existing = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if existing:
            item["execution_tags"] = existing
            continue
        item["execution_tags"] = _derive_watchlist_execution_tags(item)


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


def _lightweight_watchlist_analysis(model_output: dict | None) -> dict:
    score = None if model_output is None else model_output.get("score")
    label = (model_output or {}).get("signal_label") or build_signal_label(score, lang="en") or "Hold"
    normalized_label = str(label).strip().lower()
    if normalized_label == "buy":
        decision = "BUY"
    elif normalized_label == "sell":
        decision = "SELL"
    elif normalized_label == "watch":
        decision = "WATCH"
    else:
        decision = "HOLD"
    confidence = (
        (model_output or {}).get("confidence")
        or model_confidence(score)
        or 45
    )
    strength = (
        (model_output or {}).get("signal_strength")
        or signal_strength(score)
        or 0
    )
    return {
        "status": "lightweight",
        "decision": decision,
        "confidence": int(confidence),
        "score": int(round(float(score or 0) * 10)),
        "signal_strength": int(strength),
    }


def _lightweight_watchlist_brief(ticker: str, model_output: dict | None, combined_analysis: dict) -> dict:
    decision = str((combined_analysis or {}).get("decision") or "HOLD").upper()
    score = model_output.get("score") if model_output else None
    if decision == "BUY":
        headline = f"{ticker} stays on the long radar"
        summary = (
            f"Model score {float(score):.3f} keeps the setup constructive."
            if score is not None
            else "Model tone stays constructive."
        )
    elif decision == "SELL":
        headline = f"{ticker} needs tighter risk control"
        summary = (
            f"Model score {float(score):.3f} reads defensive."
            if score is not None
            else "Model tone stays defensive."
        )
    elif decision == "WATCH":
        headline = f"{ticker} still needs confirmation"
        summary = "Current setup is worth monitoring, but the trigger is not clean yet."
    else:
        headline = f"{ticker} remains in monitoring mode"
        summary = "No fresh high-conviction setup yet."
    return {
        "status": "lightweight",
        "headline": headline,
        "summary": summary,
    }


def _watchlist_mode_priority(combined: dict | None, view_mode: str) -> int:
    confidence = int((combined or {}).get("confidence") or 0)
    score = int((combined or {}).get("score") or 0)
    if view_mode == "premarket":
        return confidence * 2 + score
    if view_mode == "postmarket":
        return score * 3 + confidence
    return confidence + score * 2


def _watchlist_pre_rank(item: dict) -> tuple:
    model_output = item.get("model_output") or {}
    score = float(model_output.get("score") or 0.0)
    confidence = int(model_output.get("confidence") or model_confidence(model_output.get("score")) or 0)
    percentile = float(model_output.get("percentile") or 0.0)
    return (-confidence, -score, -percentile, item.get("ticker", ""))


def _watchlist_action_hint(item: dict, *, lang: str) -> tuple[str, str]:
    combined = item.get("combined_analysis") or {}
    decision = str(combined.get("decision") or "HOLD").upper()
    confidence = int(combined.get("confidence") or 0)
    signal_strength_value = int(combined.get("signal_strength") or 0)
    score = int(combined.get("score") or 0)
    daily_change_pct = item.get("daily_change_pct")
    tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
    try:
        day_move = float(daily_change_pct) if daily_change_pct is not None else None
    except (TypeError, ValueError):
        day_move = None
    if decision in {"SELL", "STRONG SELL"}:
        action = "降低关注优先级" if lang == "zh" else "Lower priority"
        reason = "当前模型态度偏弱，除非有新的价格结构改善，否则不宜放到前排。" if lang == "zh" else "Current model posture is weak; keep it out of the front row unless price structure improves."
    elif tags:
        action = "先核对风险标签" if lang == "zh" else "Check risk tags first"
        reason = "这只股票带有执行提醒，先确认风险事件再决定是否继续跟踪。" if lang == "zh" else "This name carries execution warnings, so verify those risks before continuing."
    elif decision in {"BUY", "STRONG BUY"} and confidence >= 76 and (day_move is None or day_move <= 1.5):
        action = "优先复核入场条件" if lang == "zh" else "Review entry trigger"
        reason = "模型偏强且位置还不算过热，适合优先检查是否接近入场触发位。" if lang == "zh" else "Model posture is strong without looking overextended; review whether price is near the entry trigger."
    elif decision in {"BUY", "STRONG BUY"} and day_move is not None and day_move >= 2.5:
        action = "等待放量突破" if lang == "zh" else "Wait for breakout follow-through"
        reason = "日内已经明显走强，更适合等突破后的延续性和量能确认。" if lang == "zh" else "The name is already extended on the day, so wait for follow-through and volume confirmation after the breakout."
    elif decision in {"BUY", "STRONG BUY"}:
        action = "等待回踩确认" if lang == "zh" else "Wait for pullback confirmation"
        reason = "模型仍偏多，但更适合等价格回踩后再确认承接强度。" if lang == "zh" else "The model is still constructive, but a pullback can provide a cleaner confirmation of support."
    elif decision == "WATCH" and signal_strength_value >= 55 and day_move is not None and day_move >= 1.5:
        action = "等待放量突破" if lang == "zh" else "Wait for breakout confirmation"
        reason = "信号开始转强，若继续放量上攻，再把它前置会更合适。" if lang == "zh" else "The setup is strengthening; move it up only if price keeps pushing higher on volume."
    elif decision == "WATCH" and signal_strength_value >= 40 and day_move is not None and day_move <= 0:
        action = "等待回踩企稳" if lang == "zh" else "Wait for pullback stabilization"
        reason = "现在更像回踩过程中的观察标的，先看支撑是否站稳。" if lang == "zh" else "This looks more like a pullback setup, so watch whether support stabilizes."
    elif decision == "WATCH" and signal_strength_value >= 35:
        action = "趋势未破，继续跟踪" if lang == "zh" else "Trend intact, keep tracking"
        reason = "结构还没走坏，但触发条件也不够清晰，适合继续跟踪。" if lang == "zh" else "The structure is still intact, but the trigger is not clear enough yet, so keep tracking it."
    elif decision == "HOLD" and score >= 1:
        action = "信号偏弱，暂不前置" if lang == "zh" else "Weak signal, keep in background"
        reason = "模型没有明显转空，但强度还不足以放到前排处理。" if lang == "zh" else "The model is not bearish, but the setup is not strong enough to move to the front of the queue."
    else:
        action = "等待更多确认" if lang == "zh" else "Wait for more confirmation"
        reason = "当前还缺少足够清晰的触发条件，先观察量价与模型变化再决定是否前置。" if lang == "zh" else "The setup still lacks a clean trigger, so watch price, volume, and model changes before promoting it."
    return action, reason


def _format_watchlist_close(value: float | None) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(numeric) >= 1000:
        return f"{numeric:,.2f}"
    if abs(numeric) >= 1:
        return f"{numeric:.2f}"
    return f"{numeric:.4f}"


def _render_daily_change_chip(value: float | None) -> str:
    if value is None:
        return "<span class='muted'>-</span>"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "<span class='muted'>-</span>"
    bg = "rgba(148,163,184,0.12)"
    fg = "#cbd5e1"
    if numeric > 0:
        bg = "rgba(22,163,74,0.16)"
        fg = "#4ade80"
    elif numeric < 0:
        bg = "rgba(220,38,38,0.16)"
        fg = "#f87171"
    return (
        "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;white-space:nowrap;'>{numeric:+.2f}%</span>"
    )


def _load_watchlist_daily_change_pct(*, market: str | None, ticker: str) -> float | None:
    market_value = str(market or "").strip().upper()
    normalized_ticker = normalize_ticker_for_market(ticker, market_value)
    if market_value not in {"CN", "US"} or not normalized_ticker:
        return None
    rows = load_lake_price_history(market=market_value, ticker=normalized_ticker, limit=2)
    if len(rows) < 2:
        return None
    latest = rows[-1]
    previous = rows[-2]
    latest_close = latest.get("close") or latest.get("adj_close")
    previous_close = previous.get("close") or previous.get("adj_close")
    try:
        latest_value = float(latest_close)
        previous_value = float(previous_close)
    except (TypeError, ValueError):
        return None
    if previous_value == 0:
        return None
    return ((latest_value / previous_value) - 1.0) * 100.0


def _attach_watchlist_price_fields(items: list[dict]) -> None:
    tickers = [str(item.get("ticker") or "").strip().upper() for item in items if str(item.get("ticker") or "").strip()]
    latest_closes = load_latest_closes(tickers)
    for item in items:
        ticker = str(item.get("ticker") or "").strip().upper()
        item["latest_close"] = latest_closes.get(ticker)
        item["daily_change_pct"] = _load_watchlist_daily_change_pct(
            market=item.get("market"),
            ticker=item.get("ticker") or "",
        )


def _load_watchlist_items(
    *,
    db: Session,
    execution_tag_filter: str,
    exclude_execution_tag_filter: str,
) -> list[dict]:
    cache_key = f"include={execution_tag_filter.strip().lower() or 'all'}|exclude={exclude_execution_tag_filter.strip().lower() or 'all'}"

    def _load() -> list[dict]:
        watchlist_repo = WatchlistRepository(db)
        prediction_repo = PredictionRepository(db)
        trade_plan_repo = PredictionTradePlanRepository(db)
        watchlist = watchlist_repo.get_or_create_default()
        items = watchlist_repo.list_items(watchlist.id)
        tickers = [item["ticker"] for item in items]
        latest_outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)
        latest_trade_plans = trade_plan_repo.get_latest_for_tickers(tickers)

        enriched_items: list[dict] = []
        for item in items:
            model_output = latest_outputs.get(item["ticker"])
            item["model_output"] = model_output
            item["execution_tags"] = (
                (model_output or {}).get("trade_plan", {}).get("execution_tags")
                or []
            )
            if not item["execution_tags"]:
                trade_plan = latest_trade_plans.get(item["ticker"])
                item["execution_tags"] = (trade_plan or {}).get("execution_tags") or []
            enriched_items.append(item)
        return [
            item
            for item in enriched_items
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
            and _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]

    return get_or_set("watchlist_items", cache_key, ttl_seconds=45.0, loader=_load)


def _watchlist_render_signature(items: list[dict]) -> str:
    payload = [
        {
            "item_id": item.get("item_id"),
            "ticker": item.get("ticker"),
            "market": item.get("market"),
            "sync_enabled": item.get("sync_enabled"),
            "sync_status": item.get("sync_status"),
            "last_synced_date": item.get("last_synced_date"),
            "latest_close": item.get("latest_close"),
            "daily_change_pct": item.get("daily_change_pct"),
            "score": ((item.get("model_output") or {}).get("score")),
            "signal_strength": ((item.get("model_output") or {}).get("signal_strength")),
            "tags": item.get("execution_tags") or [],
            "news_count": item.get("news_headline_count"),
            "news_score": item.get("news_sentiment_score"),
        }
        for item in items
    ]
    return urlencode({"payload": str(payload)})[:2000]


def _is_news_risk_item(item: dict) -> bool:
    label = str(item.get("news_sentiment_label") or "").strip().lower()
    score = float(item.get("news_sentiment_score") or 0.0)
    risk_tags = item.get("news_risk_tags") or []
    return int(item.get("news_headline_count") or 0) > 0 and (
        label == "negative" or score < 0 or bool(risk_tags)
    )


def _is_news_opportunity_item(item: dict) -> bool:
    label = str(item.get("news_sentiment_label") or "").strip().lower()
    score = float(item.get("news_sentiment_score") or 0.0)
    return int(item.get("news_headline_count") or 0) > 0 and (label == "positive" or score > 0)


def _render_watchlist_news_panel(
    *,
    items: list[dict],
    news_view: str,
    lang: str,
    mode: str,
    execution_tag_filter: str,
    exclude_execution_tag_filter: str,
) -> str:
    rows = [
        {
            "ticker": item.get("ticker"),
            "name": item.get("name") or item.get("ticker"),
            "market": item.get("market"),
            "headline_count": int(item.get("news_headline_count") or 0),
            "sentiment_label": item.get("news_sentiment_label") or ("中性" if lang == "zh" else "neutral"),
            "sentiment_score": float(item.get("news_sentiment_score") or 0.0),
            "risk_tags": item.get("news_risk_tags") or [],
            "summary_text": item.get("news_summary") or "",
            "source_counts": item.get("news_source_counts") or {},
        }
        for item in items
    ]
    meta = summarize_news_rows(rows)
    market_meta = {
        market: summarize_news_rows([row for row in rows if str(row.get("market") or "").upper() == market])
        for market in ("CN", "US")
    }
    risk_rows = sorted(
        [item for item in items if _is_news_risk_item(item)],
        key=lambda item: (
            float(item.get("news_sentiment_score") or 0.0),
            -len(item.get("news_risk_tags") or []),
            -int(item.get("news_headline_count") or 0),
            str(item.get("ticker") or ""),
        ),
    )
    opportunity_rows = sorted(
        [item for item in items if _is_news_opportunity_item(item)],
        key=lambda item: (
            -float(item.get("news_sentiment_score") or 0.0),
            -int(item.get("news_headline_count") or 0),
            str(item.get("ticker") or ""),
        ),
    )
    no_news_rows = [item for item in items if int(item.get("news_headline_count") or 0) <= 0]

    def _tab(label: str, value: str, count: int | None = None) -> str:
        params = {"lang": lang, "mode": mode, "news_view": value}
        if execution_tag_filter and execution_tag_filter.upper() != "ALL":
            params["execution_tag_filter"] = execution_tag_filter
        if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
            params["exclude_execution_tag_filter"] = exclude_execution_tag_filter
        active = " active" if news_view == value else ""
        suffix = f" · {count}" if count is not None else ""
        return f"<a class='news-tab{active}' href='/watchlist?{urlencode(params)}'>{label}{suffix}</a>"

    def _row(item: dict, *, risk_mode: bool = False) -> str:
        summary = html.escape(str(item.get("news_summary") or "-"))
        risk_text = " / ".join(str(tag) for tag in (item.get("news_risk_tags") or [])) or "-"
        sentiment = html.escape(str(item.get("news_sentiment_label") or "-"))
        tone = "risk" if risk_mode else "opportunity"
        return (
            "<article class='news-row'>"
            f"<div><a class='ticker' href='/insights/{item.get('ticker')}?lang={lang}'>{item.get('ticker')}</a>"
            f"<div class='muted'>{html.escape(str(item.get('name') or item.get('ticker') or '-'))} · {html.escape(str(item.get('market') or '-'))}</div>"
            f"<div class='muted'>{summary}</div></div>"
            f"<div class='news-row-right'><span class='news-chip {tone}'>{sentiment} · {int(item.get('news_headline_count') or 0)}</span>"
            f"<div class='muted'>{html.escape(risk_text if risk_mode else str(item.get('news_source_text') or '-'))}</div></div>"
            "</article>"
        )

    if news_view == "risk":
        body = "".join(_row(item, risk_mode=True) for item in risk_rows[:12])
    elif news_view == "opportunity":
        body = "".join(_row(item) for item in opportunity_rows[:12])
    elif news_view == "coverage":
        body = "".join(_row(item) for item in no_news_rows[:12])
    else:
        body = "".join(
            [
                "<div class='news-subtitle'>" + ("新闻机会" if lang == "zh" else "News opportunities") + "</div>",
                "".join(_row(item) for item in opportunity_rows[:4]) or f"<div class='muted'>{'暂无新闻机会。' if lang == 'zh' else 'No news opportunities yet.'}</div>",
                "<div class='news-subtitle'>" + ("新闻风险" if lang == "zh" else "News risks") + "</div>",
                "".join(_row(item, risk_mode=True) for item in risk_rows[:4]) or f"<div class='muted'>{'暂无新闻风险。' if lang == 'zh' else 'No news risks yet.'}</div>",
            ]
        )
    body = body or f"<div class='muted'>{'暂无匹配新闻。' if lang == 'zh' else 'No matching news yet.'}</div>"

    cn_meta = market_meta.get("CN") or {}
    us_meta = market_meta.get("US") or {}
    return f"""
      <section class="card news-console" id="news">
        <div class="eyebrow">{'自选股新闻' if lang == 'zh' else 'Watchlist News'}</div>
        <div class="news-head">
          <div>
            <h2>{'新闻只跟踪自选股' if lang == 'zh' else 'News tracks watchlist names only'}</h2>
            <p class="muted">{'首页只保留新闻状态，具体新闻机会、风险和覆盖诊断都收在这里。' if lang == 'zh' else 'The dashboard only keeps a news status; opportunities, risks, and coverage diagnostics live here.'}</p>
          </div>
          <div class="news-score"><strong>{meta.get('coverage_pct', 0)}%</strong><span>{'覆盖率' if lang == 'zh' else 'coverage'}</span></div>
        </div>
        <div class="news-metrics">
          <div><strong>{meta.get('matched_ticker_count', 0)}/{meta.get('ticker_count', 0)}</strong><span>{'命中股票' if lang == 'zh' else 'matched names'}</span></div>
          <div><strong>{cn_meta.get('matched_ticker_count', 0)}/{cn_meta.get('ticker_count', 0)}</strong><span>A股</span></div>
          <div><strong>{us_meta.get('matched_ticker_count', 0)}/{us_meta.get('ticker_count', 0)}</strong><span>US</span></div>
          <div><strong>{meta.get('headline_total', 0)}</strong><span>{'新闻数' if lang == 'zh' else 'headlines'}</span></div>
        </div>
        <div class="news-tabs">
          {_tab('全部' if lang == 'zh' else 'All', 'all')}
          {_tab('新闻机会' if lang == 'zh' else 'Opportunities', 'opportunity', len(opportunity_rows))}
          {_tab('新闻风险' if lang == 'zh' else 'Risks', 'risk', len(risk_rows))}
          {_tab('覆盖诊断' if lang == 'zh' else 'Coverage', 'coverage', len(no_news_rows))}
        </div>
        <div class="news-list">{body}</div>
      </section>
    """


def _render_watchlist_analysis_fragment(
    *,
    items: list[dict],
    view_mode: str,
    analysis_limit: int,
    ai_analysis_limit: int,
    lang: str,
) -> str:
    pre_ranked_items = sorted(items, key=_watchlist_pre_rank)
    detailed_items: list[dict] = []
    for item in pre_ranked_items[:analysis_limit]:
        model_output = item.get("model_output")
        combined = _lightweight_watchlist_analysis(model_output)
        decision_brief = _lightweight_watchlist_brief(item["ticker"], model_output, combined)
        enriched = dict(item)
        enriched["combined_analysis"] = combined
        enriched["decision_brief"] = decision_brief
        enriched["mode_priority"] = _watchlist_mode_priority(combined, view_mode)
        action_hint, action_reason = _watchlist_action_hint(enriched, lang=lang)
        enriched["action_hint"] = action_hint
        enriched["action_reason"] = action_reason
        execution_tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if execution_tags:
            enriched["ai_brief"] = (
                "先处理执行风险标签，再决定是否推进。"
                if lang == "zh"
                else "Resolve execution risk tags before promoting this name."
            )
        elif str(combined.get("decision") or "").upper() == "BUY":
            enriched["ai_brief"] = (
                "模型偏多，可优先检查触发位是否接近。"
                if lang == "zh"
                else "Constructive model posture; check whether price is near the trigger."
            )
        elif str(combined.get("decision") or "").upper() == "SELL":
            enriched["ai_brief"] = (
                "当前偏弱，先降低优先级。"
                if lang == "zh"
                else "Current setup is weak, so lower its priority."
            )
        else:
            enriched["ai_brief"] = (
                "等待更多量价确认后再推进。"
                if lang == "zh"
                else "Wait for more price and volume confirmation."
            )
        detailed_items.append(enriched)

    risk_counts: dict[str, int] = {}
    risk_examples: list[dict] = []
    for item in detailed_items:
        tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        risk_examples.append({"ticker": item["ticker"], "name": item.get("name"), "tags": tags[:2]})
    risk_examples = risk_examples[:3]
    risk_top_tags = sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    ranked_items = sorted(
        detailed_items,
        key=lambda item: (
            -int(item.get("mode_priority") or 0),
            -int((item.get("combined_analysis") or {}).get("confidence") or 0),
            item["ticker"],
        ),
    )
    high_priority = sum(
        1
        for item in ranked_items
        if str((item.get("combined_analysis") or {}).get("decision") or "").upper() in {"BUY", "STRONG BUY"}
    )
    caution_count = sum(
        1
        for item in ranked_items
        if str((item.get("combined_analysis") or {}).get("decision") or "").upper() in {"SELL", "STRONG SELL"}
    )
    observation_rows = "".join(
        "<article class='watch-row'>"
        f"<div><div class='watch-ticker'>{item['ticker']}</div><div class='muted' style='margin-top:4px;'>{item.get('name') or item['ticker']}</div><div class='muted' style='margin-top:4px;'>{item.get('decision_brief', {}).get('headline') or '-'}</div><div class='muted' style='margin-top:4px;'>{item.get('action_reason') or '-'}</div></div>"
        f"<div style='text-align:right;'><div class='watch-priority'>{int((item.get('combined_analysis') or {}).get('confidence') or 0)}%</div><div class='muted'>{item.get('action_hint') or '-'}</div></div>"
        "</article>"
        for item in ranked_items[:5]
    ) or f"<div class='muted'>{'暂无观察池项目' if lang == 'zh' else 'No observation items yet'}</div>"
    return f"""
      <section class="card" style="margin-bottom:16px;">
        <div class="eyebrow">{'决策面板' if lang == 'zh' else 'Decision Console'}</div>
        <div class="muted" style="margin-bottom:10px;">{'基于最新模型输出和执行标签生成的轻量决策视图。' if lang == 'zh' else 'A lightweight decision view built from the latest model output and execution tags.'}</div>
        <div style="display:grid;gap:16px;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));">
          <article class="card" style="margin:0;">
            <div class="eyebrow">{'高优先级' if lang == 'zh' else 'High Priority'}</div>
            <div style="font-size:28px;font-weight:800;margin:6px 0;">{high_priority}</div>
            <div class="muted">{'当前模型判定为 BUY 的自选标的数量。' if lang == 'zh' else 'Watchlist names currently rated BUY.'}</div>
          </article>
          <article class="card" style="margin:0;">
            <div class="eyebrow">{'谨慎处理' if lang == 'zh' else 'Caution'}</div>
            <div style="font-size:28px;font-weight:800;margin:6px 0;">{caution_count}</div>
            <div class="muted">{'当前模型判定偏弱，需要降级关注的名字。' if lang == 'zh' else 'Names where the current posture is weak and needs caution.'}</div>
          </article>
          <article class="card" style="margin:0;">
            <div class="eyebrow">{'顶部摘要' if lang == 'zh' else 'Top Briefs'}</div>
            <div class="muted">{"<br/>".join(f"{item['ticker']} · {item.get('name') or item['ticker']}: {item.get('decision_brief', {}).get('headline')}" for item in ranked_items[:3]) or "-"}</div>
          </article>
          <article class="card" style="margin:0;">
            <div class="eyebrow">{'执行提示' if lang == 'zh' else 'Execution Notes'}</div>
            <div class="muted">{"<br/>".join(f"{item['ticker']} · {item.get('name') or item['ticker']}: {item.get('ai_brief')}" for item in ranked_items[:3]) or "-"}</div>
          </article>
        </div>
      </section>

      <section class="card" style="margin-bottom:16px;">
        <div class="eyebrow">{'观察池' if lang == 'zh' else 'Observation Pool'}</div>
        <div class="muted" style="margin-bottom:10px;">{'先看优先级最高、模型最明确的名字，再决定是否进入分析页。' if lang == 'zh' else 'Start with the clearest, highest-priority names before opening full analysis pages.'}</div>
        <div class="stack">{observation_rows}</div>
      </section>

      <section class="card" style="margin-bottom:16px;">
        <div class="eyebrow">{'风险概览' if lang == 'zh' else 'Risk Overview'}</div>
        <div style="display:grid;gap:16px;grid-template-columns:repeat(auto-fit, minmax(240px, 1fr));">
          <article class="card" style="margin:0;">
            <div class="eyebrow">{'带风险标签' if lang == 'zh' else 'Tagged Names'}</div>
            <div style="font-size:28px;font-weight:800;margin:6px 0;">{len(risk_examples)}</div>
            <div class="muted">{'当前携带执行风险标签的自选标的数量。' if lang == 'zh' else 'Watchlist names that currently carry execution warnings.'}</div>
          </article>
          <article class="card" style="margin:0;">
            <div class="eyebrow">{'常见风险' if lang == 'zh' else 'Common Risks'}</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;">
              {"".join(f"<span class='linkbtn'>{tag} · {count}</span>" for tag, count in risk_top_tags) or "<span class='muted'>No execution warnings in the current watchlist view.</span>"}
            </div>
            <div class="muted">{'示例：' if lang == 'zh' else 'Examples: '}{" · ".join(f"{item['ticker']} · {item.get('name') or item['ticker']} ({' / '.join(item['tags'])})" for item in risk_examples) or "-"}</div>
          </article>
        </div>
      </section>
    """


def _render_watchlist_table_fragment(*, items: list[dict], lang: str) -> str:
    def news_chip(item: dict) -> str:
        count = int(item.get("news_headline_count") or 0)
        label = str(item.get("news_sentiment_label") or ("中性" if lang == "zh" else "neutral"))
        tone = "news-neutral"
        if label.lower() == "positive" or label == "positive":
            tone = "news-positive"
        if label.lower() == "negative" or label == "negative":
            tone = "news-negative"
        summary = html.escape(str(item.get("news_summary") or "-"), quote=True)
        return (
            f"<span class='news-chip {tone}' title='{summary}'>{html.escape(label)} · {count}</span>"
            f"<div class='muted news-line' title='{summary}'>{summary}</div>"
        )

    def sync_state_text(item: dict) -> str:
        if item["sync_enabled"] and item["sync_status"] == "success":
            return "Ready"
        if item["sync_enabled"]:
            return "Waiting"
        return "Off"

    item_rows_list: list[str] = []
    previous_market = None
    for item in items:
        current_market = (item.get("market") or "").upper()
        if current_market != previous_market:
            item_rows_list.append(
                "<tr class='market-section'>"
                f"<td colspan='10'>{_market_section_label(current_market)}</td>"
                "</tr>"
            )
            previous_market = current_market
        item_rows_list.append(
            "<tr>"
            f"<td class='sticky-col sticky-col-1'><a href='/watchlist/open/{item['item_id']}'>{item['ticker']}</a></td>"
            f"<td class='sticky-col sticky-col-2'>{item['name'] or item['ticker']}</td>"
            f"<td>{item['market'] or '-'}</td>"
            f"<td>{_format_watchlist_close(item.get('latest_close'))}</td>"
            f"<td>{_render_daily_change_chip(item.get('daily_change_pct'))}</td>"
            f"<td>{_decision_chip((item.get('combined_analysis') or {}).get('decision') or 'HOLD', lang=lang)}</td>"
            f"<td>{item.get('action_hint') or '-'}</td>"
            f"<td><div style='display:flex;gap:6px;flex-wrap:wrap;'>{_execution_tag_chips(item.get('execution_tags'), lang=lang)}</div></td>"
            f"<td>{news_chip(item)}</td>"
            "<td style='white-space:nowrap;'>"
            f"<a class='linkbtn' href='/watchlist/open/{item['item_id']}'>Open Insight</a>"
            f"<form action='/watchlist/toggle-sync' method='post' style='display:inline-block;margin-left:8px;'>"
            f"<input type='hidden' name='item_id' value='{item['item_id']}' />"
            f"<input type='hidden' name='enabled' value='{'0' if item['sync_enabled'] else '1'}' />"
            f"<button type='submit'>{'Disable Sync' if item['sync_enabled'] else 'Enable Sync'}</button>"
            "</form>"
            f"<form action='/watchlist/remove' method='post' style='display:inline-block;margin-left:8px;'>"
            f"<input type='hidden' name='item_id' value='{item['item_id']}' />"
            "<button type='submit' class='danger'>Remove</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    item_rows = "".join(item_rows_list) or "<tr><td colspan='10'>No stocks in your watchlist yet.</td></tr>"
    mobile_cards = "".join(
        "<article class='mobile-stock-card'>"
        f"<div class='mobile-stock-head'><div><a class='mobile-stock-ticker' href='/watchlist/open/{item['item_id']}'>{item['ticker']}</a><div class='muted'>{item['name'] or item['ticker']} · {item.get('market') or '-'}</div></div>{_decision_chip((item.get('combined_analysis') or {}).get('decision') or 'HOLD', lang=lang)}</div>"
        f"<div class='mobile-stock-grid'>"
        f"<div><span class='muted'>{'收盘价' if lang == 'zh' else 'Close'}</span><div>{_format_watchlist_close(item.get('latest_close'))}</div></div>"
        f"<div><span class='muted'>{'涨幅' if lang == 'zh' else 'Day %'}</span><div>{_render_daily_change_chip(item.get('daily_change_pct'))}</div></div>"
        "</div>"
        f"<div class='muted' style='margin-top:8px;'>{item.get('action_hint') or '-'}</div>"
        f"<div class='muted' style='margin-top:8px;'>{'新闻' if lang == 'zh' else 'News'}: {news_chip(item)}</div>"
        f"<div style='display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;'>{_execution_tag_chips(item.get('execution_tags'), lang=lang)}</div>"
        "<div class='mobile-stock-actions'>"
        f"<a class='linkbtn' href='/watchlist/open/{item['item_id']}'>{'打开分析' if lang == 'zh' else 'Open'}</a>"
        f"<form action='/watchlist/toggle-sync' method='post' style='display:inline-block;'><input type='hidden' name='item_id' value='{item['item_id']}' /><input type='hidden' name='enabled' value='{'0' if item['sync_enabled'] else '1'}' /><button type='submit'>{'关闭同步' if item['sync_enabled'] else '开启同步'}</button></form>"
        f"<form action='/watchlist/remove' method='post' style='display:inline-block;'><input type='hidden' name='item_id' value='{item['item_id']}' /><button type='submit' class='danger'>{'删除' if lang == 'zh' else 'Remove'}</button></form>"
        "</div>"
        "</article>"
        for item in items
    ) or f"<div class='muted'>{'暂无自选股。' if lang == 'zh' else 'No watchlist names yet.'}</div>"
    return f"""
      <section class="card table-card">
        <div class="eyebrow">Saved Stocks</div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th class='sticky-col sticky-col-1'>Ticker</th><th class='sticky-col sticky-col-2'>Name</th><th>Market</th><th>{'收盘价' if lang == 'zh' else 'Close'}</th><th>{'涨幅' if lang == 'zh' else 'Day %'}</th><th>Decision</th><th>{'下一步' if lang == 'zh' else 'Next Step'}</th><th>Execution Tags</th><th>{'新闻' if lang == 'zh' else 'News'}</th><th>Actions</th></tr>
            </thead>
            <tbody>{item_rows}</tbody>
          </table>
        </div>
        <div class="mobile-stock-list">{mobile_cards}</div>
        <div class="muted" style="margin-top:10px;">{'可拖动底部滚动条查看更多列。' if lang == 'zh' else 'Drag the horizontal scrollbar to see more columns.'}</div>
      </section>
    """


@router.get("/suggest")
def suggest_symbols(
    request: Request,
    q: str = Query(""),
    market: str | None = Query(None),
    db: Session = Depends(get_db_session),
) -> list[dict]:
    if not is_authenticated(request):
        return []
    market_value = market.strip().upper() if market else None
    symbol_repo = SymbolRepository(db)
    results = search_symbol_catalog(q, market_value)
    seen = {(item["ticker"], item["market"]) for item in results}
    for symbol in symbol_repo.list_symbols():
        if market_value and (symbol.market or "").upper() != market_value:
            continue
        text = q.strip().upper()
        if not text:
            continue
        symbol_name = symbol.name or ""
        if text in symbol.ticker.upper() or text in symbol_name.upper():
            key = (symbol.ticker, symbol.market)
            if key in seen:
                continue
            results.append(
                {
                    "ticker": symbol.ticker,
                    "name": symbol_name or symbol.ticker,
                    "market": symbol.market or market_value or "",
                    "exchange": symbol.exchange or "",
                }
            )
            seen.add(key)
        if len(results) >= 8:
            break
    return results[:8]


@router.get("", response_class=HTMLResponse)
def watchlist_page(
    request: Request,
    message: str | None = None,
    mode: str = Query("monitor"),
    news_view: str = Query("all"),
    analysis_limit: int = Query(12, ge=1, le=60),
    ai_analysis_limit: int = Query(3, ge=0, le=12),
    execution_tag_filter: str = Query("ALL"),
    exclude_execution_tag_filter: str = Query("ALL"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    lang = resolve_request_lang(request)
    view_mode = (mode or "monitor").strip().lower()
    if view_mode not in {"premarket", "monitor", "postmarket"}:
        view_mode = "monitor"
    news_view = (news_view or "all").strip().lower()
    if news_view not in {"all", "opportunity", "risk", "coverage"}:
        news_view = "all"
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    force_live = bool(message)
    snapshot = None if force_live else load_latest_workspace_snapshot(db, SNAPSHOT_WATCHLIST_WORKSPACE)
    nlp_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_WATCHLIST_NLP)
    nlp_rows = ((nlp_snapshot or {}).get("payload") or {}).get("rows") if isinstance(nlp_snapshot, dict) else None
    nlp_map = {
        str(item.get("ticker") or "").strip().upper(): item
        for item in (nlp_rows or [])
        if isinstance(item, dict) and item.get("ticker")
    }
    snapshot_rows = ((snapshot or {}).get("payload") or {}).get("rows") if isinstance(snapshot, dict) else None
    use_snapshot = False
    if isinstance(snapshot_rows, list) and snapshot_rows:
        watchlist_repo = WatchlistRepository(db)
        watchlist = watchlist_repo.get_or_create_default()
        live_count = len(watchlist_repo.list_items(watchlist.id))
        use_snapshot = live_count == len(snapshot_rows)
    if use_snapshot:
        items = [
            dict(item)
            for item in snapshot_rows
            if _matches_execution_tag_filter(item.get("execution_tags"), execution_tag_filter)
            and _excludes_execution_tag_filter(item.get("execution_tags"), exclude_execution_tag_filter)
        ]
    else:
        items = _load_watchlist_items(
            db=db,
            execution_tag_filter=execution_tag_filter,
            exclude_execution_tag_filter=exclude_execution_tag_filter,
        )
        for item in items:
            model_output = item.get("model_output")
            combined = _lightweight_watchlist_analysis(model_output)
            decision_brief = _lightweight_watchlist_brief(item["ticker"], model_output, combined)
            item["combined_analysis"] = combined
            item["decision_brief"] = decision_brief
            item["ai_brief"] = "AI brief available for top-ranked names."
    _attach_watchlist_price_fields(items)
    for item in items:
        combined = item.get("combined_analysis") or _lightweight_watchlist_analysis(item.get("model_output"))
        item["combined_analysis"] = combined
        item["decision_brief"] = item.get("decision_brief") or _lightweight_watchlist_brief(item["ticker"], item.get("model_output"), combined)
        item["mode_priority"] = _watchlist_mode_priority(combined, view_mode)
        item["ai_brief"] = item.get("ai_brief") or "AI brief available for top-ranked names."
        news_row = nlp_map.get(str(item.get("ticker") or "").strip().upper()) or {}
        item["news_sentiment_label"] = news_row.get("sentiment_label") or ("中性" if lang == "zh" else "neutral")
        item["news_sentiment_score"] = float(news_row.get("sentiment_score") or 0.0)
        item["news_summary"] = news_row.get("summary_text") or ""
        item["news_headline_count"] = int(news_row.get("headline_count") or 0)
        item["news_risk_tags"] = news_row.get("risk_tags") or []
        item["news_source_counts"] = news_row.get("source_counts") or {}
        item["news_source_text"] = " · ".join(
            f"{source}({count})"
            for source, count in list((item["news_source_counts"] or {}).items())[:2]
            if source
        )
    _ensure_watchlist_execution_tags(items)
    if news_view == "risk":
        display_items = [item for item in items if _is_news_risk_item(item)]
    elif news_view == "opportunity":
        display_items = [item for item in items if _is_news_opportunity_item(item)]
    elif news_view == "coverage":
        display_items = [item for item in items if int(item.get("news_headline_count") or 0) <= 0]
    else:
        display_items = items
    render_signature = _watchlist_render_signature(display_items)
    news_panel_html = _render_watchlist_news_panel(
        items=items,
        news_view=news_view,
        lang=lang,
        mode=view_mode,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
    )
    analysis_panel_html = get_or_set(
        "watchlist_analysis_fragment",
        f"{render_signature}|mode={view_mode}|analysis={analysis_limit}|ai={ai_analysis_limit}|lang={lang}",
        ttl_seconds=60.0,
        loader=lambda: _render_watchlist_analysis_fragment(
            items=items,
            view_mode=view_mode,
            analysis_limit=analysis_limit,
            ai_analysis_limit=ai_analysis_limit,
            lang=lang,
        ),
    )
    table_panel_html = get_or_set(
        "watchlist_table_fragment",
        f"{render_signature}|lang={lang}|news={news_view}",
        ttl_seconds=60.0,
        loader=lambda: _render_watchlist_table_fragment(items=display_items, lang=lang),
    )

    mode_switch_html = "".join(
        (
            f"<a href='/watchlist?{urlencode({'mode': value, 'lang': lang, 'news_view': news_view})}' "
            "style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            f"border:1px solid {'#0f766e' if value == view_mode else '#cde9e4'};"
            f"background:{'#0f766e' if value == view_mode else '#fffdf7'};"
            f"color:{'#fff' if value == view_mode else '#0f766e'};text-decoration:none;font-weight:800;font-size:12px;'>{label}</a>"
        )
        for value, label in (
            ("premarket", "盘前" if lang == "zh" else "Premarket"),
            ("monitor", "盘中" if lang == "zh" else "Monitor"),
            ("postmarket", "盘后" if lang == "zh" else "Postmarket"),
        )
    )

    option_html = "".join(
        f"<option value='{market}'>{label}</option>"
        for market, label, _ in MARKET_OPTIONS
    )
    hint_html = "".join(
        f"<div class='hint'><strong>{label}:</strong> {hint}</div>"
        for _, label, hint in MARKET_OPTIONS
    )
    banner = (
        f"<div class='banner'>{message}</div>"
        if message
        else ""
    )

    filter_query = ""
    if execution_tag_filter and execution_tag_filter.upper() != "ALL":
        filter_query += f"&execution_tag_filter={urlencode({'v': execution_tag_filter})[2:]}"
    if exclude_execution_tag_filter and exclude_execution_tag_filter.upper() != "ALL":
        filter_query += f"&exclude_execution_tag_filter={urlencode({'v': exclude_execution_tag_filter})[2:]}"

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'自选股' if lang == 'zh' else 'Watchlist'}</title>
        <style>
          :root {{
            --bg: #071018;
            --panel: #111c28;
            --panel-2: #152231;
            --ink: #e6edf3;
            --muted: #90a3b8;
            --line: #223246;
            --accent: #3dd9b6;
            --accent-soft: rgba(61,217,182,0.12);
            --danger: #b91c1c;
          }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background:
            radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),
            radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 26%),
            var(--bg); }}
          {WORKSPACE_COMPACT_STYLE}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:20px 18px 28px; }}
          .wrap {{ max-width:none; margin:0; padding: 0 0 36px; }}
          .topbar {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }}
          .topbar a {{ color: var(--accent); text-decoration:none; }}
          .banner {{ margin-bottom:12px; padding:12px 14px; border-radius:14px; background:rgba(61,217,182,0.14); color:var(--accent); font-weight:700; }}
          .hero {{ display:grid; gap:12px; grid-template-columns: minmax(280px, 1.05fr) minmax(320px, 0.95fr); margin-bottom:12px; }}
          .nav-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:12px; }}
          .nav-card {{
            display:block;
            text-decoration:none;
            color:inherit;
            background:linear-gradient(180deg, rgba(17,28,40,0.98) 0%, rgba(21,34,49,0.98) 100%);
            border:1px solid var(--line);
            border-radius:15px;
            padding:14px;
            box-shadow:0 10px 22px rgba(0,0,0,0.12);
          }}
          .nav-card:hover {{ border-color:var(--accent); box-shadow:0 12px 28px rgba(61,217,182,0.08); }}
          .nav-head {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; }}
          .nav-icon {{
            width:38px; height:38px; border-radius:12px; display:inline-flex; align-items:center; justify-content:center;
            background:rgba(61,217,182,0.10); color:var(--accent); font-size:11px; font-weight:900; letter-spacing:0.04em; border:1px solid rgba(61,217,182,0.18); flex:0 0 auto;
          }}
          .nav-title {{ font-size:16px; font-weight:800; color:var(--ink); }}
          .nav-kicker {{ color:var(--muted); font-size:11px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; }}
          h1 {{ margin:0 0 6px; font-size:32px; }}
          p {{ margin:0; }}
          form {{ margin:0; }}
          .suggest-wrap {{ position:relative; }}
          .suggestions {{
            position:absolute;
            top:100%;
            left:0;
            right:0;
            z-index:10;
            background:#111c28;
            border:1px solid var(--line);
            border-radius:12px;
            box-shadow:0 10px 28px rgba(0,0,0,0.18);
            margin-top:6px;
            display:none;
            overflow:hidden;
          }}
          .suggestion {{
            width:100%;
            display:block;
            text-align:left;
            background:#111c28;
            color:var(--ink);
            border:none;
            border-bottom:1px solid var(--line);
            border-radius:0;
            padding:10px;
            cursor:pointer;
          }}
          .suggestion:last-child {{ border-bottom:none; }}
          .suggestion:hover {{ background:rgba(61,217,182,0.10); color:var(--accent); }}
          .suggest-name {{ display:block; font-weight:700; }}
          .suggest-meta {{ display:block; color:var(--muted); font-size:12px; margin-top:4px; }}
          button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
          button.danger {{ background:var(--danger); border-color:var(--danger); padding:8px 10px; }}
          .hint {{ color:var(--muted); font-size:13px; }}
          .checkline {{ display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }}
          .table-wrap {{ width:100%; max-width:100%; overflow-x:auto; overflow-y:hidden; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); padding-bottom:8px; scrollbar-gutter:stable both-edges; }}
          .table-wrap::-webkit-scrollbar {{ height:12px; }}
          .table-wrap::-webkit-scrollbar-track {{ background:#0f1823; border-radius:999px; }}
          .table-wrap::-webkit-scrollbar-thumb {{ background:#32465d; border-radius:999px; border:2px solid #0f1823; }}
          .table-wrap::-webkit-scrollbar-thumb:hover {{ background:#47627f; }}
          table {{ width:100%; min-width:1380px; border-collapse:collapse; font-size:14px; table-layout:auto; }}
          th, td {{ text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); white-space:nowrap; vertical-align:top; }}
          th {{ color:var(--muted); font-weight:600; }}
          .table-wrap th:nth-child(1), .table-wrap td:nth-child(1) {{ min-width:108px; }}
          .table-wrap th:nth-child(2), .table-wrap td:nth-child(2) {{ min-width:160px; max-width:160px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(4), .table-wrap td:nth-child(4) {{ min-width:108px; }}
          .table-wrap th:nth-child(5), .table-wrap td:nth-child(5) {{ min-width:108px; }}
          .table-wrap th:nth-child(6), .table-wrap td:nth-child(6) {{ min-width:110px; }}
          .table-wrap th:nth-child(7), .table-wrap td:nth-child(7) {{ min-width:160px; max-width:160px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(8), .table-wrap td:nth-child(8) {{ min-width:180px; max-width:180px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(9), .table-wrap td:nth-child(9) {{ min-width:220px; }}
          .sticky-col {{ position:sticky; background:var(--panel); z-index:2; }}
          th.sticky-col {{ z-index:4; }}
          .sticky-col-1 {{ left:0; min-width:108px; box-shadow:10px 0 14px rgba(31,41,55,0.05); }}
          .sticky-col-2 {{ left:108px; min-width:160px; max-width:160px; box-shadow:10px 0 14px rgba(31,41,55,0.04); }}
          .market-section td {{ background:#132031; color:var(--accent); font-weight:800; letter-spacing:0.03em; border-top:1px solid var(--line); }}
          .table-card a {{ color: var(--accent); text-decoration:none; }}
          .linkbtn {{ display:inline-block; padding:8px 10px; border-radius:10px; background:rgba(61,217,182,0.10); color:var(--accent); font-weight:700; }}
          .news-console {{ margin-bottom:16px; }}
          .news-head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; }}
          .news-head h2 {{ margin:0 0 6px; }}
          .news-score {{ min-width:108px; text-align:right; }}
          .news-score strong {{ display:block; font-size:28px; color:var(--accent); }}
          .news-score span {{ color:var(--muted); font-size:12px; }}
          .news-metrics {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:10px; margin:14px 0; }}
          .news-metrics div {{ border:1px solid var(--line); border-radius:14px; background:rgba(11,19,29,0.62); padding:10px; }}
          .news-metrics strong {{ display:block; color:var(--ink); }}
          .news-metrics span {{ color:var(--muted); font-size:12px; }}
          .news-tabs {{ display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 14px; }}
          .news-tab {{ display:inline-flex; padding:8px 12px; border:1px solid var(--line); border-radius:999px; color:var(--muted); text-decoration:none; font-weight:800; font-size:12px; }}
          .news-tab.active {{ color:var(--accent); border-color:rgba(61,217,182,0.55); background:rgba(61,217,182,0.10); }}
          .news-list {{ display:grid; gap:10px; }}
          .news-subtitle {{ color:var(--ink); font-weight:900; margin-top:6px; }}
          .news-row {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; border:1px solid var(--line); border-radius:14px; background:rgba(11,19,29,0.56); padding:12px; }}
          .news-row-right {{ text-align:right; min-width:130px; }}
          .news-chip {{ display:inline-flex; padding:4px 8px; border-radius:999px; font-weight:900; font-size:12px; border:1px solid var(--line); }}
          .news-chip.news-positive,.news-chip.opportunity {{ color:#8df0aa; background:rgba(74,222,128,0.12); border-color:rgba(74,222,128,0.26); }}
          .news-chip.news-negative,.news-chip.risk {{ color:#ff9aaa; background:rgba(255,107,129,0.12); border-color:rgba(255,107,129,0.26); }}
          .news-chip.news-neutral {{ color:#d7e0ea; background:rgba(148,163,184,0.12); border-color:rgba(148,163,184,0.22); }}
          .news-line {{ margin-top:4px; max-width:180px; overflow:hidden; text-overflow:ellipsis; }}
          .mobile-stock-list {{ display:none; gap:10px; }}
          .mobile-stock-card {{ border:1px solid var(--line); border-radius:14px; background:rgba(11,19,29,0.82); padding:12px; }}
          .mobile-stock-head {{ display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }}
          .mobile-stock-ticker {{ font-weight:800; color:var(--accent); text-decoration:none; }}
          .mobile-stock-grid {{ display:grid; gap:8px; grid-template-columns:repeat(2, minmax(0,1fr)); margin-top:10px; }}
          .mobile-stock-actions {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
          @media (max-width: 720px) {{
            .table-wrap {{ display:none; }}
            .mobile-stock-list {{ display:grid; }}
            .sticky-col, .sticky-col-1, .sticky-col-2 {{ position:static; box-shadow:none; min-width:auto; max-width:none; }}
          }}
        </style>
        <script>
          async function loadSuggestions() {{
            const tickerInput = document.getElementById("watchlist-ticker");
            const marketSelect = document.getElementById("watchlist-market");
            const box = document.getElementById("ticker-suggestions");
            const nameInput = document.getElementById("watchlist-name");
            const query = tickerInput.value.trim();
            if (!query) {{
              box.style.display = "none";
              box.innerHTML = "";
              return;
            }}
            const url = `/watchlist/suggest?q=${{encodeURIComponent(query)}}&market=${{encodeURIComponent(marketSelect.value)}}`;
            const response = await fetch(url, {{ credentials: "same-origin" }});
            if (!response.ok) {{
              box.style.display = "none";
              return;
            }}
            const items = await response.json();
            if (!items.length) {{
              box.style.display = "none";
              box.innerHTML = "";
              return;
            }}
            box.innerHTML = items.map((item) => `
              <button class="suggestion" type="button" data-ticker="${{item.ticker}}" data-name="${{item.name || ""}}" data-market="${{item.market || ""}}">
                <span class="suggest-name">${{item.name || item.ticker}}</span>
                <span class="suggest-meta">${{item.ticker}} · ${{item.exchange || item.market || "-"}}</span>
              </button>
            `).join("");
            box.style.display = "block";
            box.querySelectorAll(".suggestion").forEach((button) => {{
              button.addEventListener("click", () => {{
                tickerInput.value = button.dataset.ticker || "";
                if (!nameInput.value.trim()) {{
                  nameInput.value = button.dataset.name || "";
                }}
                if (button.dataset.market) {{
                  marketSelect.value = button.dataset.market;
                }}
                box.style.display = "none";
              }});
            }});
          }}

          window.addEventListener("DOMContentLoaded", () => {{
            const tickerInput = document.getElementById("watchlist-ticker");
            const marketSelect = document.getElementById("watchlist-market");
            const box = document.getElementById("ticker-suggestions");
            tickerInput.addEventListener("input", loadSuggestions);
            marketSelect.addEventListener("change", loadSuggestions);
            document.addEventListener("click", (event) => {{
              if (!box.contains(event.target) && event.target !== tickerInput) {{
                box.style.display = "none";
              }}
            }});
          }});

          function appendExecutionTag(inputName, tag) {{
            const form = document.querySelector('form[action="/watchlist"]');
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

          function clearExecutionTags() {{
            const form = document.querySelector('form[action="/watchlist"]');
            if (!form) return;
            const includeInput = form.querySelector('input[name="execution_tag_filter"]');
            const excludeInput = form.querySelector('input[name="exclude_execution_tag_filter"]');
            if (includeInput) includeInput.value = "";
            if (excludeInput) excludeInput.value = "";
            if (includeInput) includeInput.focus();
          }}
        </script>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'自选股' if lang == 'zh' else 'Watchlist'}</h1>
              <p>{'把候选股、同步状态和分析入口收进一个持续跟踪面板。' if lang == 'zh' else 'Keep candidates, sync state, and analysis entry points in one tracking workspace.'}</p>
            </div>
            <nav class="side-nav">{render_workspace_nav_html(lang=lang, active_key='watchlist')}</nav>
          </aside>
          <main class="content">
        <div class="wrap">
          <div class="topbar">
            <a href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to dashboard'}</a>
            <a href="/screeners?lang={lang}">{'打开模型选股' if lang == 'zh' else 'Open Screener'}</a>
          </div>
          {banner}
          <section class="nav-grid">
            <a class="nav-card" href="/dashboard">
              <div class="nav-head">
                <span class="nav-icon">HOME</span>
                <div>
                  <div class="nav-kicker">{'总览' if lang == 'zh' else 'Overview'}</div>
                  <div class="nav-title">{'首页' if lang == 'zh' else 'Dashboard'}</div>
                </div>
              </div>
              <div class="muted">{'返回工作台，并继续进入市场、连续强势股或任务中心。' if lang == 'zh' else 'Return to the lightweight hub and navigate to Market Pulse, Continuous Leaders, or Operations.'}</div>
            </a>
            <a class="nav-card" href="/screeners?lang={lang}">
              <div class="nav-head">
                <span class="nav-icon">SCAN</span>
                <div>
                  <div class="nav-kicker">{'发现' if lang == 'zh' else 'Discovery'}</div>
                  <div class="nav-title">{'模型选股' if lang == 'zh' else 'Screeners'}</div>
                </div>
              </div>
              <div class="muted">{'打开规则选股，把候选标的加入自选。' if lang == 'zh' else 'Open rule-based stock selection and turn candidates into watchlist names.'}</div>
            </a>
            <a class="nav-card" href="/dashboard/data-sources?lang={lang}">
              <div class="nav-head">
                <span class="nav-icon">DATA</span>
                <div>
                  <div class="nav-kicker">{'新鲜度' if lang == 'zh' else 'Freshness'}</div>
                  <div class="nav-title">{'数据来源' if lang == 'zh' else 'Data Sources'}</div>
                </div>
              </div>
              <div class="muted">{'行动前先检查数据源、概念映射和逐股同步状态。' if lang == 'zh' else 'Check provider freshness, concept mapping, and per-symbol sync source before acting.'}</div>
            </a>
          </section>
          <section class="hero">
            <article class="card">
              <div class="eyebrow">{'我的自选' if lang == 'zh' else 'My Watchlist'}</div>
              <h1>{'跨市场跟踪股票' if lang == 'zh' else 'Follow Stocks Across Markets'}</h1>
              <p class="muted">{'把美股、A 股、港股放到这里统一跟踪，点击任意股票即可进入分析页。' if lang == 'zh' else 'Add U.S. stocks, China A-shares, or Hong Kong stocks here. Then click any ticker to jump straight into its insight page.'}</p>
            </article>
            <article class="card">
              <div class="eyebrow">{'添加股票' if lang == 'zh' else 'Add A Stock'}</div>
              <form class="stack" action="/watchlist/add" method="post">
                <input type="hidden" name="lang" value="{lang}" />
                <div class="suggest-wrap">
                  <input id="watchlist-ticker" type="text" name="ticker" placeholder="{'代码，如 ASTS 或 600519.SH' if lang == 'zh' else 'Ticker, e.g. ASTS or 600519.SH'}" autocomplete="off" required />
                  <div id="ticker-suggestions" class="suggestions"></div>
                </div>
                <input id="watchlist-name" type="text" name="name" placeholder="{'有可用数据时会自动补全名称' if lang == 'zh' else 'Stock name will auto-fill when available'}" />
                <select id="watchlist-market" name="market">
                  {option_html}
                </select>
                <label class="checkline">
                  <input type="checkbox" name="sync_after_add" value="true" checked />
                  {'添加后立即同步' if lang == 'zh' else 'Add and sync now'}
                </label>
                <button type="submit">{'加入自选' if lang == 'zh' else 'Add To Watchlist'}</button>
              </form>
              <div class="stack" style="margin-top:12px;">
                {hint_html}
              </div>
            </article>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">{'数据同步' if lang == 'zh' else 'Data Sync'}</div>
            <form class="stack" action="/watchlist/sync-enabled" method="post" style="max-width:360px;">
              <input type="text" name="provider" value="auto" />
              <input type="text" name="start_date" value="2025-01-01" />
              <button type="submit">{'同步已启用股票' if lang == 'zh' else 'Sync Enabled Stocks'}</button>
            </form>
            <p class="muted" style="margin-top:10px;">{'只有开启了同步的自选股，才会在这里被批量拉取。' if lang == 'zh' else 'Only stocks with Sync Enabled = On will be pulled when you click this button.'}</p>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">{'执行过滤' if lang == 'zh' else 'Execution Filters'}</div>
            <form class="stack" action="/watchlist" method="get" style="max-width:420px;">
              <input type="hidden" name="lang" value="{lang}" />
              <input type="hidden" name="news_view" value="{news_view}" />
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{'执行标签' if lang == 'zh' else 'Execution Tag'}</label>
                <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">{'排除标签' if lang == 'zh' else 'Exclude Tag'}</label>
                <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <div class="muted" style="margin-bottom:6px;">{'快捷标签' if lang == 'zh' else 'Quick Tags'}</div>
                <div style="display:flex;flex-wrap:wrap;gap:8px;">
                  <button type="button" onclick="appendExecutionTag('execution_tag_filter', 'gap-risk')">gap-risk</button>
                  <button type="button" onclick="appendExecutionTag('execution_tag_filter', 'earnings-soon')">earnings-soon</button>
                  <button type="button" onclick="appendExecutionTag('execution_tag_filter', 'thin-liquidity')">thin-liquidity</button>
                  <button type="button" onclick="appendExecutionTag('exclude_execution_tag_filter', 'gap-risk')">exclude gap-risk</button>
                  <button type="button" onclick="clearExecutionTags()">Clear Tags</button>
                </div>
              </div>
              <datalist id="execution-tag-options">
                <option value="gap-risk"></option>
                <option value="earnings-soon"></option>
                <option value="thin-liquidity"></option>
              </datalist>
              <button type="submit">{'应用过滤' if lang == 'zh' else 'Apply Filters'}</button>
            </form>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">{'决策面板' if lang == 'zh' else 'Decision Console'}</div>
            <div class="muted" style="margin-bottom:10px;">{'查看模式' if lang == 'zh' else 'View Mode'}</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;">{mode_switch_html}</div>
          </section>

          <div id="watchlist-analysis-panels">{analysis_panel_html}</div>

          {news_panel_html}

          <div id="watchlist-table-panel">{table_panel_html}</div>
        </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/analysis-fragment", response_class=HTMLResponse)
def watchlist_analysis_fragment(
    request: Request,
    mode: str = Query("monitor"),
    analysis_limit: int = Query(12, ge=1, le=60),
    ai_analysis_limit: int = Query(3, ge=0, le=12),
    execution_tag_filter: str = Query("ALL"),
    exclude_execution_tag_filter: str = Query("ALL"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return HTMLResponse("", status_code=401)
    lang = resolve_request_lang(request)
    view_mode = (mode or "monitor").strip().lower()
    if view_mode not in {"premarket", "monitor", "postmarket"}:
        view_mode = "monitor"
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    cache_key = (
        f"mode={view_mode}|analysis_limit={analysis_limit}|ai_limit={ai_analysis_limit}"
        f"|include={execution_tag_filter.lower() or 'all'}|exclude={exclude_execution_tag_filter.lower() or 'all'}"
    )

    def _load() -> str:
        items = _load_watchlist_items(
            db=db,
            execution_tag_filter=execution_tag_filter,
            exclude_execution_tag_filter=exclude_execution_tag_filter,
        )
        return _render_watchlist_analysis_fragment(
            items=items,
            view_mode=view_mode,
            analysis_limit=analysis_limit,
            ai_analysis_limit=ai_analysis_limit,
            lang=lang,
        )

    return get_or_set("watchlist_analysis_fragment", cache_key, ttl_seconds=20.0, loader=_load)


@router.get("/table-fragment", response_class=HTMLResponse)
def watchlist_table_fragment(
    request: Request,
    mode: str = Query("monitor"),
    execution_tag_filter: str = Query("ALL"),
    exclude_execution_tag_filter: str = Query("ALL"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return HTMLResponse("", status_code=401)
    lang = resolve_request_lang(request)
    view_mode = (mode or "monitor").strip().lower()
    if view_mode not in {"premarket", "monitor", "postmarket"}:
        view_mode = "monitor"
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    cache_key = (
        f"mode={view_mode}|include={execution_tag_filter.lower() or 'all'}"
        f"|exclude={exclude_execution_tag_filter.lower() or 'all'}"
    )

    def _load() -> str:
        items = _load_watchlist_items(
            db=db,
            execution_tag_filter=execution_tag_filter,
            exclude_execution_tag_filter=exclude_execution_tag_filter,
        )
        for item in items:
            model_output = item.get("model_output")
            combined = _lightweight_watchlist_analysis(model_output)
            decision_brief = _lightweight_watchlist_brief(item["ticker"], model_output, combined)
            item["combined_analysis"] = combined
            item["decision_brief"] = decision_brief
            item["mode_priority"] = _watchlist_mode_priority(combined, view_mode)
            item["ai_brief"] = "AI brief available for top-ranked names."
        return _render_watchlist_table_fragment(items=items, lang=lang)

    return get_or_set("watchlist_table_fragment", cache_key, ttl_seconds=20.0, loader=_load)


@router.post("/add")
def add_watchlist_symbol(
    request: Request,
    ticker: str = Form(...),
    name: str | None = Form(None),
    market: str | None = Form(None),
    sync_after_add: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    market = market.strip().upper() if market else None
    ticker = normalize_ticker_for_market(ticker, market)
    if not ticker:
        return _redirect_with_message("Ticker is required.")
    sync_now = str(sync_after_add or "").lower() in {"1", "true", "yes", "on"}
    inferred = infer_symbol_record(ticker, market)
    display_name = name.strip() if name else None
    if not display_name and inferred:
        display_name = inferred["name"]
    exchange = inferred["exchange"] if inferred else None

    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    symbol = symbol_repo.get_or_create_symbol(
        SymbolCreate(
            ticker=ticker,
            name=display_name,
            market=market,
            exchange=exchange,
        )
    )
    item = watchlist_repo.add_symbol(watchlist.id, symbol.id)

    if sync_now:
        watchlist_repo.set_sync_enabled(item.id, True)
        results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="auto")
        _clear_watchlist_caches()
        _refresh_workspace_snapshots_async()
        result = results[0] if results else None
        if result and result["status"] == "success":
            return _redirect_with_message(f"Added {ticker}, synced {result['rows']} rows, and updated dashboard watchlist.")
        if result:
            return _redirect_with_message(f"Added {ticker}; dashboard updated, but sync failed: {result.get('message', 'Unknown error')}")
        return _redirect_with_message(f"Added {ticker}; dashboard updated, but sync did not return a result.")

    _clear_watchlist_caches()
    _refresh_workspace_snapshots_async()
    return _redirect_with_message(f"Added {ticker} to your watchlist and refreshed dashboard.")


@router.post("/remove")
def remove_watchlist_symbol(
    request: Request,
    item_id: int = Form(...),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    watchlist_repo = WatchlistRepository(db)
    removed = watchlist_repo.remove_item(item_id)
    if removed:
        _clear_watchlist_caches()
        _refresh_workspace_snapshots_async()
        return _redirect_with_message("Removed stock from your watchlist.")
    return _redirect_with_message("That watchlist item no longer exists.")


@router.post("/toggle-sync")
def toggle_watchlist_sync(
    request: Request,
    item_id: int = Form(...),
    enabled: str = Form(...),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    watchlist_repo = WatchlistRepository(db)
    item = watchlist_repo.set_sync_enabled(item_id, enabled == "1")
    if item is None:
        return _redirect_with_message("That watchlist item no longer exists.")
    _clear_watchlist_caches()
    _refresh_workspace_snapshots_async()
    return _redirect_with_message("Sync setting updated.")


@router.get("/open/{item_id}")
def open_watchlist_item(item_id: int, request: Request, db: Session = Depends(get_db_session)) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    watchlist_repo = WatchlistRepository(db)
    item = watchlist_repo.get_item(item_id)
    if item is None:
        return _redirect_with_message("That watchlist item no longer exists.")
    has_local_history = bool(SymbolDataService().get_history(item["ticker"], limit=1))
    if item["sync_enabled"] and item["sync_status"] != "success" and not has_local_history:
        return _redirect_with_message("Still Sync, Please wait")
    return RedirectResponse(url=f"/insights/{item['ticker']}", status_code=303)


@router.post("/sync-enabled")
def sync_enabled_watchlist_symbols(
    request: Request,
    provider: str = Form("auto"),
    start_date: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    tickers = watchlist_repo.list_enabled_tickers(watchlist.id)
    if not tickers:
        return _redirect_with_message("No sync-enabled stocks yet. Turn sync on for at least one stock first.")
    results = sync_market_data(tickers=tickers, start_date=start_date, provider=provider)
    _clear_watchlist_caches()
    success_count = sum(1 for item in results if item["status"] == "success")
    return _redirect_with_message(f"Synced {success_count}/{len(results)} enabled stocks.")


@router.post("/refresh-metadata")
def refresh_existing_watchlist_metadata(request: Request) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    result = refresh_watchlist_metadata()
    _clear_watchlist_caches()
    return _redirect_with_message(f"Updated metadata for {result['updated_count']} existing watchlist stock(s).")
