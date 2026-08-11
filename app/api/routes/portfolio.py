from __future__ import annotations

import csv
import html
import threading
from io import StringIO
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db_session
from app.models.schema import SymbolCreate
from app.services.auth import is_authenticated, login_redirect
from app.services.market_lake import load_lake_price_history
from app.services.portfolio_intelligence import (
    build_position_management_fields,
    build_portfolio_ai_summary,
    build_portfolio_intelligence,
)
from app.services.portfolio_book import (
    SELL_REASON_OPTIONS,
    apply_suggested_trade_reasons,
    backfill_trade_audit_snapshot,
    backfill_trade_audit_snapshots,
    load_portfolio_positions,
    load_portfolio_trades,
    remove_portfolio_position,
    sell_portfolio_position,
    suggest_trade_reason,
    trade_reason_label,
    update_portfolio_trade_reason,
    upsert_portfolio_position,
)
from app.services.price_snapshot import load_latest_close, load_latest_closes
from app.services.repository import PredictionRepository, SymbolRepository, WatchlistRepository
from app.services.runtime_cache import clear_namespace, get_or_set
from app.services.symbol_catalog import infer_symbol_record, search_symbol_catalog, search_symbol_records
from app.services.ticker_format import normalize_ticker_for_market
from app.services.time_utils import app_today_iso
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html
from app.services.workspace_snapshots import (
    SNAPSHOT_PORTFOLIO_NLP,
    SNAPSHOT_PORTFOLIO_WORKSPACE,
    load_latest_workspace_snapshot,
    refresh_workspace_snapshots,
)


router = APIRouter(prefix="/portfolio", tags=["portfolio"])


MARKET_OPTIONS = [
    ("US", "US Stocks", "Examples: ASTS, NVDA, AAPL"),
    ("CN", "China A-Shares", "Examples: 600519.SH, 000001.SZ"),
    ("HK", "Hong Kong Stocks", "Examples: 0700.HK, 9988.HK"),
]


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


def _load_portfolio_daily_change_pct(*, market: str | None, ticker: str) -> float | None:
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

def _compact_text(value: str | None, limit: int = 28) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _portfolio_action_chip(value: str | None) -> str:
    text = str(value or "-").strip() or "-"
    lowered = text.lower()
    bg = "#1a2430"
    fg = "#d7e3ef"
    if any(token in lowered for token in ("减", "trim", "exit", "sell", "降")):
        bg, fg = "#34161d", "#ffb4c0"
    elif any(token in lowered for token in ("加", "buy", "entry", "增")):
        bg, fg = "#123328", "#91f0c5"
    elif any(token in lowered for token in ("观察", "review", "watch", "复核")):
        bg, fg = "#1a2940", "#9fcaff"
    return (
        "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;line-height:1.2;white-space:nowrap;'>{html.escape(text)}</span>"
    )


def _portfolio_risk_chip(value: str | None) -> str:
    text = str(value or "-").strip() or "-"
    lowered = text.lower()
    bg = "#1a2430"
    fg = "#d7e3ef"
    if any(token in lowered for token in ("高", "high", "earnings", "event", "gap", "risk")):
        bg, fg = "#35220f", "#ffd58a"
    elif any(token in lowered for token in ("低", "low", "ok", "normal")):
        bg, fg = "#163021", "#8ce8b6"
    return (
        "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;line-height:1.2;white-space:nowrap;'>{html.escape(text)}</span>"
    )


def _portfolio_priority_rank(value: object) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        pass
    lowered = raw.lower()
    if raw in {"高", "high", "H", "A"} or lowered == "high":
        return 3
    if raw in {"中", "medium", "med", "M", "B"} or lowered in {"medium", "med"}:
        return 2
    if raw in {"低", "low", "L", "C"} or lowered == "low":
        return 1
    return 0


def _portfolio_sort_value(row: dict, sort_by: str) -> tuple[float, str]:
    normalized = str(sort_by or "daily_change").strip().lower()
    if normalized == "daily_change":
        value = row.get("daily_change_pct")
    elif normalized == "pnl":
        value = row.get("pnl_pct")
    else:
        value = row.get("daily_change_pct")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = -999999.0
    return numeric, str(row.get("ticker") or "")


def _portfolio_sort_link(*, lang: str, sort_by: str, sort_order: str, target: str) -> str:
    normalized_target = target if target in {"daily_change", "pnl"} else "daily_change"
    next_order = "asc" if sort_by == normalized_target and sort_order == "desc" else "desc"
    arrow = ""
    if sort_by == normalized_target:
        arrow = " ↓" if sort_order == "desc" else " ↑"
    label = "涨幅" if normalized_target == "daily_change" and lang == "zh" else "Day %"
    if normalized_target == "pnl":
        label = "盈亏%" if lang == "zh" else "PnL %"
    query = urlencode({"lang": lang, "sort_by": normalized_target, "sort_order": next_order})
    return f"<a href='/portfolio?{query}' style='color:inherit;text-decoration:none;'>{label}{arrow}</a>"


def _portfolio_market_label(market: str | None, *, lang: str) -> str:
    normalized = str(market or "").strip().upper()
    if lang == "zh":
        return {
            "CN": "A 股持仓",
            "US": "美股持仓",
            "HK": "港股持仓",
        }.get(normalized, f"{normalized or '-'} 持仓")
    return {
        "CN": "China A-Share Positions",
        "US": "U.S. Positions",
        "HK": "Hong Kong Positions",
    }.get(normalized, f"{normalized or '-'} Positions")


def _portfolio_currency_symbol(market: str | None) -> str:
    normalized = str(market or "").strip().upper()
    return {
        "CN": "¥",
        "US": "$",
        "HK": "HK$",
    }.get(normalized, "")


def _portfolio_currency_label(market: str | None, *, lang: str) -> str:
    normalized = str(market or "").strip().upper()
    if lang == "zh":
        return {
            "CN": "人民币",
            "US": "美元",
            "HK": "港币",
        }.get(normalized, normalized or "-")
    return {
        "CN": "CNY",
        "US": "USD",
        "HK": "HKD",
    }.get(normalized, normalized or "-")


def _format_portfolio_money(value: float | None, *, market: str | None, digits: int = 2) -> str:
    try:
        numeric = float(value or 0.0)
    except (TypeError, ValueError):
        numeric = 0.0
    return f"{_portfolio_currency_symbol(market)}{numeric:,.{digits}f}"


def _redirect(message: str | None = None) -> RedirectResponse:
    suffix = f"?message={message}" if message else ""
    return RedirectResponse(url=f"/portfolio{suffix}", status_code=303)


def _clear_watchlist_caches() -> None:
    clear_namespace("watchlist_items")
    clear_namespace("watchlist_analysis_fragment")
    clear_namespace("watchlist_table_fragment")
    clear_namespace("portfolio_rows")


def _refresh_workspace_snapshots_async() -> None:
    def _run() -> None:
        try:
            with SessionLocal() as snapshot_db:
                refresh_workspace_snapshots(snapshot_db)
        except Exception:
            return

    threading.Thread(
        target=_run,
        name="portfolio-workspace-refresh",
        daemon=True,
    ).start()


def _ensure_watchlist_membership(
    db: Session,
    *,
    ticker: str,
    name: str | None,
    market: str | None,
) -> str:
    normalized_market = market.strip().upper() if market else None
    normalized_ticker = normalize_ticker_for_market(ticker, normalized_market)
    if not normalized_ticker:
        return ""
    inferred = infer_symbol_record(normalized_ticker, normalized_market)
    display_name = str(name or "").strip() or (inferred["name"] if inferred else None)
    exchange = inferred["exchange"] if inferred else None
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    symbol = symbol_repo.get_or_create_symbol(
        SymbolCreate(
            ticker=normalized_ticker,
            name=display_name,
            market=normalized_market,
            exchange=exchange,
        )
    )
    watchlist_repo.add_symbol(watchlist.id, symbol.id)
    return normalized_ticker


@router.get("/suggest")
def suggest_portfolio_symbols(
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
    return search_symbol_records(symbol_repo.list_symbols(), q, market_value, initial=results, limit=8)


@router.get("/quote")
def portfolio_quote_preview(
    request: Request,
    ticker: str = Query(""),
) -> dict:
    if not is_authenticated(request):
        return {"ticker": "", "latest_close": None}
    normalized = str(ticker or "").strip().upper()
    if not normalized:
        return {"ticker": "", "latest_close": None}
    latest_close = load_latest_close(normalized)
    return {"ticker": normalized, "latest_close": latest_close}


@router.get("", response_class=HTMLResponse)
def portfolio_page(
    request: Request,
    message: str | None = None,
    sort_by: str = Query("daily_change"),
    sort_order: str = Query("desc"),
    action_focus: str = Query("all"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    lang = resolve_request_lang(request, default="zh")
    sort_by = (sort_by or "daily_change").strip().lower()
    if sort_by not in {"daily_change", "pnl"}:
        sort_by = "daily_change"
    sort_order = (sort_order or "desc").strip().lower()
    if sort_order not in {"asc", "desc"}:
        sort_order = "desc"
    action_focus = (action_focus or "all").strip().lower()
    if action_focus not in {"all", "high_priority", "exit_trim", "underwater", "risk", "review"}:
        action_focus = "all"
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    force_live = bool(message)
    snapshot = None if force_live else load_latest_workspace_snapshot(db, SNAPSHOT_PORTFOLIO_WORKSPACE)
    nlp_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_PORTFOLIO_NLP)
    nlp_rows = ((nlp_snapshot or {}).get("payload") or {}).get("rows") if isinstance(nlp_snapshot, dict) else None
    nlp_map = {
        str(item.get("ticker") or "").strip().upper(): item
        for item in (nlp_rows or [])
        if isinstance(item, dict) and item.get("ticker")
    }
    sell_reason_options_html = "".join(
        f"<option value='{html.escape(value, quote=True)}'>{html.escape(value if lang == 'zh' else en_label)}</option>"
        for value, en_label in SELL_REASON_OPTIONS
    )
    snapshot_payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
    intelligence = (snapshot_payload or {}).get("intelligence") if isinstance(snapshot_payload, dict) else None
    required_intelligence_keys = {
        "market_rankings",
        "top_position",
        "risk_posture",
        "posture_summary",
        "trim_candidates",
        "exit_candidates",
        "review_candidates",
    }
    if not isinstance(intelligence, dict) or not required_intelligence_keys.issubset(set(intelligence.keys())):
        intelligence = build_portfolio_intelligence(db, lang=lang)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_picker_options = "".join(
        "<option "
        f"value='{html.escape(item['ticker'], quote=True)}' "
        f"data-name='{html.escape(item.get('name') or '', quote=True)}' "
        f"data-market='{html.escape(item.get('market') or '', quote=True)}'>"
        f"{html.escape(item['ticker'])} · {html.escape(item.get('name') or item['ticker'])} · {html.escape(item.get('market') or '-')}"
        "</option>"
        for item in watchlist_repo.list_items(watchlist.id)
    )

    raw_positions = load_portfolio_positions()
    snapshot_rows = snapshot_payload.get("rows") if isinstance(snapshot_payload, dict) else None
    snapshot_rows_ready = (
        isinstance(snapshot_rows, list)
        and all("latest_price_missing" in row for row in snapshot_rows if isinstance(row, dict))
    )
    if isinstance(snapshot_payload, dict) and snapshot_rows_ready and isinstance(snapshot_payload.get("totals"), dict):
        rows = list(snapshot_payload.get("rows") or [])
        totals = snapshot_payload.get("totals") or {}
        total_market_value = float(totals.get("market_value") or 0.0)
        total_cost = float(totals.get("cost") or 0.0)
    else:
        tickers = [item["ticker"] for item in raw_positions]
        portfolio_cache_key = f"{lang}|{[(item.get('ticker'), item.get('quantity'), item.get('cost_basis'), item.get('note')) for item in raw_positions]}"

        def _load_portfolio_rows() -> tuple[list[dict], float, float]:
            overviews = symbol_repo.list_overviews_for_tickers(tickers)
            latest_outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)
            latest_prices = load_latest_closes(tickers)
            rows: list[dict] = []
            total_market_value = 0.0
            total_cost = 0.0
            position_drafts: list[dict] = []
            for item in raw_positions:
                overview = overviews.get(item["ticker"]) or {
                    "ticker": item["ticker"],
                    "name": item.get("name"),
                    "market": item.get("market"),
                }
                latest_signal = latest_outputs.get(item["ticker"])
                latest_price_raw = latest_prices.get(item["ticker"])
                latest_price_missing = latest_price_raw is None or float(latest_price_raw or 0.0) <= 0.0
                latest_price = 0.0 if latest_price_missing else float(latest_price_raw or 0.0)
                quantity = float(item.get("quantity") or 0.0)
                cost_basis = float(item.get("cost_basis") or 0.0)
                market_value = latest_price * quantity
                cost_value = cost_basis * quantity
                pnl = 0.0 if latest_price_missing else market_value - cost_value
                pnl_pct = ((latest_price / cost_basis) - 1.0) * 100 if cost_basis and not latest_price_missing else 0.0
                total_market_value += market_value
                total_cost += cost_value
                position_drafts.append(
                    {
                        "item": item,
                        "overview": overview,
                        "latest_signal": latest_signal,
                        "latest_price": latest_price,
                        "quantity": quantity,
                        "cost_basis": cost_basis,
                        "market_value": market_value,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "latest_price_missing": latest_price_missing,
                    }
                )
            for draft in position_drafts:
                item = draft["item"]
                overview = draft["overview"]
                latest_signal = draft["latest_signal"]
                ai_summary = build_portfolio_ai_summary(
                    latest_signal=latest_signal,
                    pnl_pct=draft["pnl_pct"],
                    cost_basis=draft["cost_basis"],
                    lang=lang,
                )
                management = build_position_management_fields(
                    latest_signal=latest_signal,
                    pnl_pct=draft["pnl_pct"],
                    market_value=draft["market_value"],
                    total_market_value=total_market_value,
                    cost_basis=draft["cost_basis"],
                    lang=lang,
                )
                rows.append(
                    {
                        "ticker": item["ticker"],
                        "name": overview.get("name") or item["ticker"],
                        "market": overview.get("market") or item.get("market") or "-",
                        "quantity": draft["quantity"],
                        "cost_basis": draft["cost_basis"],
                        "latest_price": draft["latest_price"],
                        "market_value": draft["market_value"],
                        "pnl": draft["pnl"],
                        "pnl_pct": draft["pnl_pct"],
                        "latest_price_missing": draft.get("latest_price_missing", False),
                        "ai_headline": ai_summary["ai_headline"],
                        "ai_verdict": ai_summary["ai_verdict"],
                        "ai_strategy": ai_summary["ai_strategy"],
                        "key_hint": ai_summary["key_hint"],
                        "target_weight_pct": management["target_weight_pct"],
                        "target_weight_text": management["target_weight_text"],
                        "target_weight_source": management["target_weight_source"],
                        "current_weight_pct": management["current_weight_pct"],
                        "action_bucket": management["action_bucket"],
                        "action_bucket_key": management["action_bucket_key"],
                        "note": item.get("note") or "",
                    }
                )
            return rows, total_market_value, total_cost

        rows, total_market_value, total_cost = get_or_set(
            "portfolio_rows",
            portfolio_cache_key,
            ttl_seconds=30.0,
            loader=_load_portfolio_rows,
        )
    for row in rows:
        row["daily_change_pct"] = _load_portfolio_daily_change_pct(
            market=row.get("market"),
            ticker=row.get("ticker") or "",
        )
    for row in rows:
        news_row = nlp_map.get(str(row.get("ticker") or "").strip().upper()) or {}
        row["news_sentiment_label"] = news_row.get("sentiment_label") or ("中性" if lang == "zh" else "neutral")
        row["news_summary"] = news_row.get("summary_text") or ""
        row["news_headline_count"] = int(news_row.get("headline_count") or 0)
    rows = sorted(
        rows,
        key=lambda row: _portfolio_sort_value(row, sort_by),
        reverse=(sort_order == "desc"),
    )
    market_value_totals: dict[str, float] = {}
    market_cost_totals: dict[str, float] = {}
    market_pnl_totals: dict[str, float] = {}
    for row in rows:
        market_key = str(row.get("market") or "-").strip().upper()
        market_value_totals[market_key] = market_value_totals.get(market_key, 0.0) + float(row.get("market_value") or 0.0)
        cost_value = float(row.get("cost_basis") or 0.0) * float(row.get("quantity") or 0.0)
        market_cost_totals[market_key] = market_cost_totals.get(market_key, 0.0) + cost_value
        market_pnl_totals[market_key] = market_pnl_totals.get(market_key, 0.0) + float(row.get("pnl") or 0.0)
    watch_items = sorted(
        list(intelligence.get("watch_items", [])),
        key=lambda row: (
            -_portfolio_priority_rank(row.get("action_priority")),
            -abs(float(row.get("rebalance_gap_pct") or 0.0)),
            -abs(float(row.get("weight_pct") or 0.0)),
            str(row.get("ticker") or ""),
        ),
    )
    for row in watch_items:
        news_row = nlp_map.get(str(row.get("ticker") or "").strip().upper()) or {}
        row["news_text"] = news_row.get("summary_text") or ("暂无相关新闻摘要" if lang == "zh" else "No relevant news summary yet")
    total_pnl = total_market_value - total_cost
    total_pnl_pct = ((total_market_value / total_cost) - 1.0) * 100 if total_cost else 0.0
    option_html = "".join(
        f"<option value='{market}'>{label}</option>"
        for market, label, _ in MARKET_OPTIONS
    )
    hint_html = "".join(
        f"<div class='hint'><strong>{label}:</strong> {hint}</div>"
        for _, label, hint in MARKET_OPTIONS
    )
    banner = f"<div class='banner'>{message}</div>" if message else ""
    trades = sorted(load_portfolio_trades(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
    unresolved_trade_count = sum(1 for row in trades if str(row.get("reason") or "").strip() == "其他")
    resolved_trade_count = max(0, len(trades) - unresolved_trade_count)
    suggested_trade_count = sum(
        1
        for row in trades
        if str(row.get("reason") or "").strip() == "其他" and suggest_trade_reason(row) != "其他"
    )
    missing_audit_count = sum(
        1
        for row in trades
        if not str(row.get("audit_snapshot_at") or "").strip()
        and not str(row.get("action_hint_at_exit") or "").strip()
        and not str(row.get("action_reason_at_exit") or "").strip()
    )
    sell_reason_progress_html = (
        f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;'>"
        f"<span style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;background:rgba(61,217,182,0.12);color:var(--accent);font-weight:800;font-size:12px;'>"
        f"{'已结构化' if lang == 'zh' else 'Structured'} {resolved_trade_count}</span>"
        f"<span style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;background:rgba(246,193,119,0.14);color:#f6c177;font-weight:800;font-size:12px;'>"
        f"{'待补录' if lang == 'zh' else 'Needs review'} {unresolved_trade_count}</span>"
        f"<span style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;background:rgba(159,202,255,0.14);color:#9fcaff;font-weight:800;font-size:12px;'>"
        f"{'可按建议补录' if lang == 'zh' else 'Can autofill'} {suggested_trade_count}</span>"
        f"<span style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;background:rgba(255,180,192,0.12);color:#ffb4c0;font-weight:800;font-size:12px;'>"
        f"{'待补录建议快照' if lang == 'zh' else 'Missing audit snapshot'} {missing_audit_count}</span>"
        f"<a href='/dashboard/weekly-review?lang={lang}' style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;border:1px solid var(--line);background:rgba(17,28,40,0.7);font-weight:800;font-size:12px;'>"
        f"{'打开每周复盘' if lang == 'zh' else 'Open Weekly Review'}</a>"
        f"<form method='post' action='/portfolio/trade-reason/suggest-all' style='margin:0;'>"
        f"<input type='hidden' name='lang' value='{lang}' />"
        f"<button type='submit' class='ghost-btn' style='padding:7px 11px;' {'disabled' if suggested_trade_count <= 0 else ''}>{'一键按建议补录' if lang == 'zh' else 'Autofill suggestions'}</button>"
        "</form>"
        f"<form method='post' action='/portfolio/trade-audit/backfill-all' style='margin:0;'>"
        f"<input type='hidden' name='lang' value='{lang}' />"
        f"<button type='submit' class='ghost-btn' style='padding:7px 11px;' {'disabled' if missing_audit_count <= 0 else ''}>{'一键补录建议快照' if lang == 'zh' else 'Backfill audit snapshots'}</button>"
        "</form>"
        "</div>"
    ) if trades else ""
    def _render_trade_reason_cell(row: dict) -> str:
        current_reason = str(row.get("reason") or "").strip()
        base_form = (
            f"<form method='post' action='/portfolio/trade-reason' style='display:flex;gap:8px;align-items:center;min-width:220px;'>"
            f"<input type='hidden' name='trade_id' value='{int(row.get('id') or 0)}' />"
            f"<input type='hidden' name='lang' value='{lang}' />"
            f"<select name='reason' style='min-width:138px;'>"
            + "".join(
                f"<option value='{html.escape(value, quote=True)}' {'selected' if value == current_reason else ''}>{html.escape(value if lang == 'zh' else en_label)}</option>"
                for value, en_label in SELL_REASON_OPTIONS
            )
            + "</select>"
            + f"<button type='submit' class='ghost-btn' style='padding:7px 10px;'>{'保存' if lang == 'zh' else 'Save'}</button>"
            + "</form>"
        )
        suggested_form = ""
        if current_reason == "其他":
            suggested_form = (
                f"<form method='post' action='/portfolio/trade-reason/suggest' style='display:flex;gap:8px;align-items:center;margin-top:6px;'>"
                f"<input type='hidden' name='trade_id' value='{int(row.get('id') or 0)}' />"
                f"<input type='hidden' name='lang' value='{lang}' />"
                f"<button type='submit' class='ghost-btn' style='padding:6px 10px;'>{'按建议补录' if lang == 'zh' else 'Use suggestion'}</button>"
                f"<span class='muted'>{html.escape(suggest_trade_reason(row))}</span>"
                "</form>"
            )
        reason_label = trade_reason_label(row.get("reason"), lang=lang)
        audit_form = ""
        has_audit = bool(str(row.get("audit_snapshot_at") or "").strip()) or bool(
            str(row.get("action_hint_at_exit") or "").strip() or str(row.get("action_reason_at_exit") or "").strip()
        )
        if not has_audit:
            audit_form = (
                f"<form method='post' action='/portfolio/trade-audit/backfill' style='display:flex;gap:8px;align-items:center;margin-top:6px;'>"
                f"<input type='hidden' name='trade_id' value='{int(row.get('id') or 0)}' />"
                f"<input type='hidden' name='lang' value='{lang}' />"
                f"<button type='submit' class='ghost-btn' style='padding:6px 10px;'>{'补录建议快照' if lang == 'zh' else 'Backfill audit'}</button>"
                f"<span class='muted'>{'历史补录' if lang == 'zh' else 'Historical backfill'}</span>"
                "</form>"
            )
        return (
            base_form
            + suggested_form
            + audit_form
            + f"<div class='muted' style='margin-top:6px;' title='{html.escape(reason_label, quote=True)}'>{_compact_text(reason_label, 18)}</div>"
        )
    recent_sell_rows = "".join(
        f"<tr style='background:{'rgba(246,193,119,0.06)' if str(row.get('reason') or '').strip() == '其他' else 'transparent'};'>"
        f"<td>{html.escape(str(row.get('trade_date') or '-'))}</td>"
        f"<td>{html.escape(row.get('ticker') or '-')}</td>"
        f"<td title='{html.escape(row.get('name') or '-', quote=True)}'>{_compact_text(row.get('name') or row.get('ticker'), 18)}</td>"
        f"<td>{float(row.get('quantity') or 0.0):.0f}</td>"
        f"<td>{float(row.get('price') or 0.0):.2f}</td>"
        f"<td>{float(row.get('cost_basis') or 0.0):.2f}</td>"
        f"<td>{float(row.get('realized_pnl') or 0.0):.2f} ({float(row.get('realized_pnl_pct') or 0.0):.1f}%)</td>"
        f"<td>{float(row.get('remaining_quantity') or 0.0):.0f}</td>"
        f"<td>{_render_trade_reason_cell(row)}</td>"
        "</tr>"
        for row in trades[:10]
    ) or f"<tr><td colspan='9'>{'暂无卖出记录' if lang == 'zh' else 'No sell records yet.'}</td></tr>"
    realized_total = sum(float(row.get("realized_pnl") or 0.0) for row in trades)
    top_position = intelligence.get("top_position") or {}
    top_position_market = str(top_position.get("market") or "").strip().upper()
    total_market_value = float(intelligence.get("total_market_value") or total_market_value)
    market_rankings_html = "".join(
        "<article style='display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{row['market']}</div><div class='muted'>{'市场暴露' if lang == 'zh' else 'Market exposure'}</div></div>"
        f"<div style='text-align:right;'><div style='font-weight:700'>{row['weight_pct']:.1f}%</div><div class='muted'>{_format_portfolio_money(row.get('market_value'), market=row.get('market'))}</div></div>"
        "</article>"
        for row in intelligence.get("market_rankings", [])
    ) or f"<div class='muted'>{'暂无市场暴露数据' if lang == 'zh' else 'No market exposure data yet'}</div>"
    all_watch_items = sorted(
        list(intelligence.get("all_items") or watch_items),
        key=lambda row: (
            -_portfolio_priority_rank(row.get("action_priority")),
            -abs(float(row.get("rebalance_gap_pct") or 0.0)),
            -abs(float(row.get("weight_pct") or 0.0)),
            str(row.get("ticker") or ""),
        ),
    )
    for row in all_watch_items:
        news_row = nlp_map.get(str(row.get("ticker") or "").strip().upper()) or {}
        row["news_text"] = news_row.get("summary_text") or ("暂无相关新闻摘要" if lang == "zh" else "No relevant news summary yet")

    def _matches_action_focus(row: dict, focus: str) -> bool:
        if focus == "high_priority":
            return _priority_rank(row.get("action_priority")) <= 2
        if focus == "exit_trim":
            return (
                float(row.get("pnl_pct") or 0.0) <= -8.0
                or str(row.get("signal_label") or "").strip().lower() in {"卖点", "sell"}
                or float(row.get("weight_pct") or 0.0) >= 15.0
                or float(row.get("pnl_pct") or 0.0) >= 30.0
            )
        if focus == "underwater":
            return float(row.get("pnl_pct") or 0.0) < 0.0
        if focus == "risk":
            return str(row.get("risk_tag") or "").strip() not in {"", "LOW"}
        if focus == "review":
            return str(row.get("action_priority") or "").strip().lower() in {"高", "high"}
        return True

    def _action_focus_href(focus: str) -> str:
        return f"/portfolio?{urlencode({'lang': lang, 'sort_by': sort_by, 'sort_order': sort_order, 'action_focus': focus})}#action-queue-detail"

    def _action_preview_links(focus: str) -> str:
        preview_rows = [row for row in all_watch_items if _matches_action_focus(row, focus)][:4]
        if not preview_rows:
            return f"<div class='muted' style='margin-top:8px;'>{'暂无' if lang == 'zh' else 'None yet'}</div>"
        return (
            "<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;'>"
            + "".join(
                f"<a class='pill' href='/insights/{html.escape(str(row.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(row.get('ticker') or '-'))}</a>"
                for row in preview_rows
            )
            + "</div>"
        )
    risk_posture_tone = str(intelligence.get("risk_posture") or "")
    if risk_posture_tone in {"防守", "Defensive"}:
        posture_style = "background:rgba(255,180,192,0.12);color:#ffb4c0;"
    elif risk_posture_tone in {"均衡偏防守", "Balanced / defensive"}:
        posture_style = "background:rgba(246,193,119,0.14);color:#f6c177;"
    else:
        posture_style = "background:rgba(61,217,182,0.12);color:var(--accent);"
    risk_queue_html = (
        f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;'>"
        f"<span style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;{posture_style}font-weight:800;font-size:12px;'>{html.escape(str(intelligence.get('risk_posture') or ('均衡' if lang == 'zh' else 'Balanced')))}</span>"
        f"<a href='{_action_focus_href('exit_trim')}' style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;background:rgba(255,180,192,0.12);color:#ffb4c0;font-weight:800;font-size:12px;text-decoration:none;'>{'退出候选' if lang == 'zh' else 'Exit'} {int(intelligence.get('exit_candidates') or 0)}</a>"
        f"<a href='{_action_focus_href('exit_trim')}' style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;background:rgba(246,193,119,0.14);color:#f6c177;font-weight:800;font-size:12px;text-decoration:none;'>{'减仓候选' if lang == 'zh' else 'Trim'} {int(intelligence.get('trim_candidates') or 0)}</a>"
        f"<a href='{_action_focus_href('review')}' style='display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;background:rgba(159,202,255,0.14);color:#9fcaff;font-weight:800;font-size:12px;text-decoration:none;'>{'优先复核' if lang == 'zh' else 'Review'} {int(intelligence.get('review_candidates') or 0)}</a>"
        f"</div>"
    )
    sector_rankings_html = "".join(
        "<article style='display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{row['sector']}</div><div class='muted'>{'市值暴露' if lang == 'zh' else 'Exposure'}</div></div>"
        f"<div style='text-align:right;'><div style='font-weight:700'>{row['weight_pct']:.1f}%</div><div class='muted'>{row['market_value']:.2f}</div></div>"
        "</article>"
        for row in intelligence["sector_rankings"]
    ) or f"<div class='muted'>{'暂无行业暴露数据' if lang == 'zh' else 'No sector exposure data yet'}</div>"
    top_position_label = (
        f"{html.escape(str(top_position.get('ticker') or '-'))} · {html.escape(str(top_position.get('name') or '-'))}"
    )
    queue_summary_text = (
        f"{'退出' if lang == 'zh' else 'Exit'} {int(intelligence.get('exit_candidates') or 0)}"
        f" / {'减仓' if lang == 'zh' else 'Trim'} {int(intelligence.get('trim_candidates') or 0)}"
        f" / {'复核' if lang == 'zh' else 'Review'} {int(intelligence.get('review_candidates') or 0)}"
    )
    action_mix = intelligence.get("action_mix") or {}
    action_mix_summary = (
        f"HOLD {int(action_mix.get('hold') or 0)} · "
        f"REVIEW {int(action_mix.get('review') or 0)} · "
        f"TRIM {int(action_mix.get('trim') or 0)} · "
        f"EXIT {int(action_mix.get('exit') or 0)}"
    )
    market_rankings = intelligence.get("market_rankings") or []
    market_mix_summary = " / ".join(
        f"{html.escape(str(row.get('market') or '-'))} {float(row.get('weight_pct') or 0.0):.1f}%"
        for row in market_rankings[:2]
    ) or ("暂无市场暴露" if lang == "zh" else "No market exposure yet")
    market_position_context_html = "".join(
        (
            "<a class='pill' href='#portfolio-market-" + html.escape(market, quote=True) + "'>"
            + html.escape(_portfolio_market_label(market, lang=lang))
            + f" · {len([row for row in rows if str(row.get('market') or '').upper() == market])} "
            + ("只 · 行情可用 " if lang == "zh" else " positions · prices available ")
            + str(sum(1 for row in rows if str(row.get("market") or "").upper() == market and not bool(row.get("latest_price_missing"))))
            + "</a>"
        )
        for market in ("CN", "US")
        if any(str(row.get("market") or "").upper() == market for row in rows)
    )
    market_value_summary_html = "".join(
        (
            "<article style='border:1px solid var(--line);border-radius:16px;padding:12px 14px;background:rgba(15,24,35,0.58);'>"
            f"<div class='eyebrow'>{html.escape(_portfolio_market_label(market, lang=lang))}</div>"
            f"<div style='font-size:22px;font-weight:900;margin-top:4px;'>{_format_portfolio_money(market_value_totals.get(market, 0.0), market=market)}</div>"
            f"<div class='muted'>{'浮盈亏' if lang == 'zh' else 'PnL'} { _format_portfolio_money(market_pnl_totals.get(market, 0.0), market=market)}"
            f" · { _portfolio_currency_label(market, lang=lang)}</div>"
            "</article>"
        )
        for market in [m for m in ["CN", "US", "HK"] if market_value_totals.get(m) is not None and (market_value_totals.get(m) or market_cost_totals.get(m))]
    )
    def _priority_rank(value: object) -> int:
        raw = str(value or "").strip()
        mapping = {
            "高": 1,
            "high": 1,
            "中": 2,
            "medium": 2,
            "低": 3,
            "low": 3,
        }
        if not raw:
            return 999
        lowered = raw.lower()
        if lowered in mapping:
            return mapping[lowered]
        if raw in mapping:
            return mapping[raw]
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 999

    watch_items = [row for row in all_watch_items if _matches_action_focus(row, action_focus)]

    high_priority_count = sum(1 for row in all_watch_items if _priority_rank(row.get("action_priority")) <= 2)
    negative_pnl_count = sum(1 for row in all_watch_items if float(row.get("pnl_pct") or 0.0) < 0)
    risk_tagged_count = sum(1 for row in all_watch_items if str(row.get("risk_tag") or "").strip() and str(row.get("risk_tag") or "").strip() != "LOW")
    action_queue_cards_html = "".join(
        [
            (
                "<article style='border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:rgba(15,24,35,0.58);'>"
                f"<div class='eyebrow'>{'高优先级' if lang == 'zh' else 'High Priority'}</div>"
                f"<div style='font-size:22px;font-weight:900;margin-top:4px;'>{high_priority_count}</div>"
                f"<div class='muted'>{'优先级 1-2 的持仓动作' if lang == 'zh' else 'Priority 1-2 actions'}</div>"
                f"<div style='margin-top:10px;'><a class='pill' href='{_action_focus_href('high_priority')}'>{'查看明细' if lang == 'zh' else 'View details'}</a></div>"
                f"{_action_preview_links('high_priority')}"
                "</article>"
            ),
            (
                "<article style='border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:rgba(15,24,35,0.58);'>"
                f"<div class='eyebrow'>{'退出 / 减仓' if lang == 'zh' else 'Exit / Trim'}</div>"
                f"<div style='font-size:22px;font-weight:900;margin-top:4px;'>{int(intelligence.get('exit_candidates') or 0) + int(intelligence.get('trim_candidates') or 0)}</div>"
                f"<div class='muted'>{'需要先动手处理的仓位' if lang == 'zh' else 'Positions needing action first'}</div>"
                f"<div style='margin-top:10px;'><a class='pill' href='{_action_focus_href('exit_trim')}'>{'查看明细' if lang == 'zh' else 'View details'}</a></div>"
                f"{_action_preview_links('exit_trim')}"
                "</article>"
            ),
            (
                "<article style='border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:rgba(15,24,35,0.58);'>"
                f"<div class='eyebrow'>{'浮亏持仓' if lang == 'zh' else 'Underwater'}</div>"
                f"<div style='font-size:22px;font-weight:900;margin-top:4px;'>{negative_pnl_count}</div>"
                f"<div class='muted'>{'当前 PnL 为负的持仓' if lang == 'zh' else 'Positions with negative PnL'}</div>"
                f"<div style='margin-top:10px;'><a class='pill' href='{_action_focus_href('underwater')}'>{'查看明细' if lang == 'zh' else 'View details'}</a></div>"
                f"{_action_preview_links('underwater')}"
                "</article>"
            ),
            (
                "<article style='border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:rgba(15,24,35,0.58);'>"
                f"<div class='eyebrow'>{'风险标记' if lang == 'zh' else 'Risk Tags'}</div>"
                f"<div style='font-size:22px;font-weight:900;margin-top:4px;'>{risk_tagged_count}</div>"
                f"<div class='muted'>{'带有中高风险标签的持仓' if lang == 'zh' else 'Positions carrying medium/high risk tags'}</div>"
                f"<div style='margin-top:10px;'><a class='pill' href='{_action_focus_href('risk')}'>{'查看明细' if lang == 'zh' else 'View details'}</a></div>"
                f"{_action_preview_links('risk')}"
                "</article>"
            ),
        ]
    )
    watch_action_rows_html = "".join(
        "<tr>"
        f"<td><a href='/insights/{html.escape(str(row.get('ticker') or ''), quote=True)}?lang={lang}'>{row['ticker']}</a></td>"
        f"<td title='{html.escape(str(row.get('name') or row['ticker']), quote=True)}'>{_compact_text(row.get('name') or row['ticker'], 20)}</td>"
        f"<td>{_portfolio_action_chip(row['action_hint'])}</td>"
        f"<td>{row['action_priority']}</td>"
        f"<td>{row['sector']}</td>"
        f"<td>{row['signal_label']}</td>"
        f"<td>{_portfolio_risk_chip(row['risk_tag'])}</td>"
        f"<td>{row['pnl_pct']:.1f}%</td>"
        f"<td>{row['weight_pct']:.1f}%</td>"
        f"<td>{(format(row['target_weight_pct'], '.1f') + '%' if row.get('target_weight_pct') is not None else '-')}</td>"
        f"<td>{(format(row['rebalance_gap_pct'], '.1f') + '%' if row.get('rebalance_gap_pct') is not None else '-')}</td>"
        f"<td title='{html.escape(row['action_reason'], quote=True)}'>{_compact_text(row['action_reason'], 28)}</td>"
        f"<td title='{html.escape(row['rebalance_action'], quote=True)}'>{_compact_text(row['rebalance_action'], 24)}</td>"
        f"<td title='{html.escape(row['execution_risk_summary'], quote=True)}'>{_compact_text(row['execution_risk_summary'], 24)}</td>"
        f"<td title='{html.escape(row.get('news_text') or '-', quote=True)}'>{_compact_text(row.get('news_text') or '-', 28)}</td>"
        "</tr>"
        for row in watch_items
    ) or f"<tr><td colspan='15'>{'当前筛选下暂无持仓动作建议' if lang == 'zh' else 'No action items for the current filter'}</td></tr>"
    watch_action_cards_html = "".join(
        "<article class='mobile-action-card'>"
        f"<div class='mobile-position-head'><div><div class='mobile-position-ticker'><a href='/insights/{html.escape(str(row.get('ticker') or ''), quote=True)}?lang={lang}' style='color:inherit;text-decoration:none;'>{row['ticker']}</a></div><div class='muted'>{_compact_text(row.get('name') or row['ticker'], 24)} · {row['sector']}</div></div><div style='text-align:right;'><div>{_portfolio_action_chip(row['action_hint'])}</div><div class='muted' style='margin-top:6px;'>{'优先级' if lang == 'zh' else 'Priority'} {row['action_priority']}</div></div></div>"
        f"<div class='mobile-position-grid'>"
        f"<div><span class='muted'>{'信号' if lang == 'zh' else 'Signal'}</span><div>{row['signal_label']}</div></div>"
        f"<div><span class='muted'>{'风险' if lang == 'zh' else 'Risk'}</span><div>{_portfolio_risk_chip(row['risk_tag'])}</div></div>"
        f"<div><span class='muted'>PnL</span><div>{row['pnl_pct']:.1f}%</div></div>"
        f"<div><span class='muted'>{'仓位' if lang == 'zh' else 'Weight'}</span><div>{row['weight_pct']:.1f}%</div></div>"
        f"<div><span class='muted'>{'目标' if lang == 'zh' else 'Target'}</span><div>{(format(row['target_weight_pct'], '.1f') + '%' if row.get('target_weight_pct') is not None else '-')}</div></div>"
        f"<div><span class='muted'>{'偏离' if lang == 'zh' else 'Gap'}</span><div>{(format(row['rebalance_gap_pct'], '.1f') + '%' if row.get('rebalance_gap_pct') is not None else '-')}</div></div>"
        "</div>"
        f"<div class='muted' style='margin-top:8px;'>{_compact_text(row['action_reason'], 88)}</div>"
        f"<div class='muted' style='margin-top:6px;'>{_compact_text(row['rebalance_action'], 88)}</div>"
        f"<div class='muted' style='margin-top:6px;'>{_compact_text(row['execution_risk_summary'], 88)}</div>"
        f"<div class='muted' style='margin-top:6px;'>{_compact_text(row.get('news_text') or '-', 88)}</div>"
        "</article>"
        for row in watch_items
    ) or f"<div class='muted'>{'当前筛选下暂无持仓动作建议' if lang == 'zh' else 'No action items for the current filter'}</div>"
    def _render_position_row(row: dict) -> str:
        price_missing = bool(row.get("latest_price_missing"))
        latest_price_text = "缺行情" if lang == "zh" and price_missing else ("Missing" if price_missing else f"{float(row.get('latest_price') or 0.0):.2f}")
        pnl_text = "缺行情" if lang == "zh" and price_missing else ("Missing price" if price_missing else f"{_format_portfolio_money(row['pnl'], market=row.get('market'))} ({row['pnl_pct']:.1f}%)")
        sell_price_text = "" if price_missing else f"{float(row.get('latest_price') or 0.0):.2f}"
        return (
            "<tr>"
            f"<td>{row['ticker']}</td>"
            f"<td title='{row['name']}'>{_compact_text(row['name'], 24)}</td>"
            f"<td>{row['market']}</td>"
            f"<td>{row['quantity']:.0f}</td>"
            f"<td>{row['cost_basis']:.2f}</td>"
            f"<td>{latest_price_text}</td>"
            f"<td>{_render_daily_change_chip(row.get('daily_change_pct'))}</td>"
            f"<td>{_format_portfolio_money(row['market_value'], market=row.get('market'))}</td>"
            f"<td>{pnl_text}</td>"
            f"<td>{row['ai_verdict']}</td>"
            f"<td title='{row['ai_headline']}'>{_compact_text(row['ai_headline'], 30)}</td>"
            f"<td title='{row['ai_strategy']}'>{_compact_text(row['ai_strategy'], 24)}</td>"
            f"<td title='{row.get('key_hint') or '-'}'>{_compact_text(row.get('key_hint') or '-', 18)}</td>"
            f"<td title='当前仓位 {float(row.get('current_weight_pct') or 0.0):.1f}% · {row.get('target_weight_source') or '-'}'>{row['target_weight_text']}</td>"
            f"<td>{row['action_bucket']}</td>"
            f"<td title='{html.escape(row.get('news_summary') or '-', quote=True)}'>{html.escape(row.get('news_sentiment_label') or '-')} · {int(row.get('news_headline_count') or 0)}</td>"
            f"<td title='{html.escape(row['note'] or '-', quote=True)}'>{_compact_text(row['note'] or '-', 20)}</td>"
            "<td>"
            "<div class='position-actions'>"
            f"<button type='button' class='ghost-btn' onclick=\"openSellModal('{html.escape(row['ticker'], quote=True)}','{html.escape(row['name'], quote=True)}','{float(row['quantity']):.6f}','{sell_price_text}')\">{'卖出' if lang == 'zh' else 'Sell'}</button>"
            f"<form action='/portfolio/remove' method='post' style='margin:0;'><input type='hidden' name='ticker' value='{row['ticker']}' /><button type='submit' class='ghost-btn danger-btn'>{'删除' if lang == 'zh' else 'Delete'}</button></form>"
            "</div>"
            "</td>"
            "</tr>"
        )

    def _render_mobile_position_card(row: dict) -> str:
        price_missing = bool(row.get("latest_price_missing"))
        latest_price_text = "缺行情" if lang == "zh" and price_missing else ("Missing" if price_missing else f"{float(row.get('latest_price') or 0.0):.2f}")
        pnl_value_text = "缺行情" if lang == "zh" and price_missing else ("Missing price" if price_missing else f"{_format_portfolio_money(row['pnl'], market=row.get('market'))}")
        pnl_pct_text = "-" if price_missing else f"{row['pnl_pct']:.1f}%"
        sell_price_text = "" if price_missing else f"{float(row.get('latest_price') or 0.0):.2f}"
        return (
            "<article class='mobile-position-card'>"
            f"<div class='mobile-position-head'><div><div class='mobile-position-ticker'>{row['ticker']}</div><div class='muted'>{_compact_text(row['name'], 24)} · {row['market']}</div></div><div style='text-align:right;'><div style='font-weight:800;'>{pnl_value_text}</div><div class='muted'>{pnl_pct_text}</div></div></div>"
            f"<div class='mobile-position-grid'>"
            f"<div><span class='muted'>{'数量' if lang == 'zh' else 'Qty'}</span><div>{row['quantity']:.0f}</div></div>"
            f"<div><span class='muted'>{'成本' if lang == 'zh' else 'Cost'}</span><div>{row['cost_basis']:.2f}</div></div>"
            f"<div><span class='muted'>{'收盘价' if lang == 'zh' else 'Close'}</span><div>{latest_price_text}</div></div>"
            f"<div><span class='muted'>{'涨幅' if lang == 'zh' else 'Day %'}</span><div>{_render_daily_change_chip(row.get('daily_change_pct'))}</div></div>"
            f"<div><span class='muted'>{'市值' if lang == 'zh' else 'Value'}</span><div>{_format_portfolio_money(row['market_value'], market=row.get('market'))}</div></div>"
            f"<div><span class='muted'>AI</span><div>{row['ai_verdict']}</div></div>"
            f"<div><span class='muted'>{'动作桶' if lang == 'zh' else 'Bucket'}</span><div>{row['action_bucket']}</div></div>"
            "</div>"
            f"<div class='muted' style='margin-top:8px;'>{_compact_text(row['ai_headline'], 60)}</div>"
            f"<div class='muted' style='margin-top:6px;'>{_compact_text(row['ai_strategy'], 56)}</div>"
            f"<div class='mobile-action-bucket'>{'关键提示' if lang == 'zh' else 'Key Hint'}：{html.escape(str(row.get('key_hint') or '-'))}</div>"
            f"<div class='mobile-action-bucket'>{'动作桶' if lang == 'zh' else 'Action Bucket'}：{html.escape(str(row['action_bucket'] or '-'))}</div>"
            f"<div class='muted' style='margin-top:6px;'>{'目标仓位' if lang == 'zh' else 'Target'}: {row['target_weight_text']} · {'新闻' if lang == 'zh' else 'News'}: {html.escape(row.get('news_sentiment_label') or '-')} · {int(row.get('news_headline_count') or 0)}</div>"
            f"<div class='muted' style='margin-top:6px;'>{_compact_text(row['note'] or '-', 72)}</div>"
            "<div class='mobile-position-actions'>"
            f"<button type='button' class='ghost-btn' onclick=\"openSellModal('{html.escape(row['ticker'], quote=True)}','{html.escape(row['name'], quote=True)}','{float(row['quantity']):.6f}','{sell_price_text}')\">{'卖出' if lang == 'zh' else 'Sell'}</button>"
            f"<form action='/portfolio/remove' method='post' style='margin:0;'><input type='hidden' name='ticker' value='{row['ticker']}' /><button type='submit' class='ghost-btn danger-btn'>{'删除' if lang == 'zh' else 'Delete'}</button></form>"
            "</div>"
            "</article>"
        )

    market_order = ["CN", "US", "HK"]
    grouped_rows: dict[str, list[dict]] = {}
    for row in rows:
        grouped_rows.setdefault(str(row.get("market") or "-").strip().upper(), []).append(row)
    ordered_markets = [market for market in market_order if grouped_rows.get(market)]
    ordered_markets.extend(
        market for market in sorted(grouped_rows.keys()) if market not in ordered_markets
    )
    if rows:
        grouped_positions_html = "".join(
            (
                f"<section id='portfolio-market-{html.escape(market, quote=True)}' class='market-position-section'>"
                f"<div class='market-position-head'>"
                f"<div><div class='eyebrow'>{_portfolio_market_label(market, lang=lang)}</div>"
                f"<div class='muted'>{len(grouped_rows.get(market, []))} {'只持仓' if lang == 'zh' else 'positions'} · "
                f"{_format_portfolio_money(sum(float(item.get('market_value') or 0.0) for item in grouped_rows.get(market, [])), market=market)} "
                f"{'市值' if lang == 'zh' else 'value'} · {_portfolio_currency_label(market, lang=lang)}</div></div>"
                f"</div>"
                f"<div class='table-wrap positions-table-wrap'>"
                f"<table class='positions-table'>"
                f"<thead>"
                f"<tr><th>Ticker</th><th>Name</th><th>Market</th><th>Qty</th><th>Cost</th><th>{'收盘价' if lang == 'zh' else 'Close'}</th><th>{_portfolio_sort_link(lang=lang, sort_by=sort_by, sort_order=sort_order, target='daily_change')}</th><th>Market Value</th><th>{_portfolio_sort_link(lang=lang, sort_by=sort_by, sort_order=sort_order, target='pnl')}</th><th>AI Verdict</th><th>AI Headline</th><th>AI Strategy</th><th>{'关键提示' if lang == 'zh' else 'Key Hint'}</th><th>{'目标仓位' if lang == 'zh' else 'Target Wt'}</th><th>{'动作桶' if lang == 'zh' else 'Action Bucket'}</th><th>{'新闻' if lang == 'zh' else 'News'}</th><th>{'备注' if lang == 'zh' else 'Note'}</th><th>{'操作' if lang == 'zh' else 'Actions'}</th></tr>"
                f"</thead>"
                f"<tbody>{''.join(_render_position_row(row) for row in grouped_rows.get(market, []))}</tbody>"
                f"</table>"
                f"</div>"
                f"<div class='mobile-position-list'>{''.join(_render_mobile_position_card(row) for row in grouped_rows.get(market, []))}</div>"
                f"</section>"
            )
            for market in ordered_markets
        )
    else:
        grouped_positions_html = f"<div class='muted'>{'暂无持仓。' if lang == 'zh' else 'No positions yet.'}</div>"

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>持仓</title>
        <style>
          :root {{
            --bg:#071018;
            --panel:#111c28;
            --panel-2:#152231;
            --ink:#e6edf3;
            --muted:#90a3b8;
            --line:#223246;
            --accent:#3dd9b6;
          }}
          body {{ margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:
            radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),
            radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 26%),
            var(--bg); color:var(--ink); }}
          {WORKSPACE_COMPACT_STYLE}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:16px 14px 24px; }}
          .wrap {{ max-width:none; margin:0; padding:0 0 36px; }}
          .grid {{ display:grid; gap:8px; grid-template-columns:repeat(auto-fit, minmax(190px, 1fr)); margin-bottom:10px; }}
          .metric {{ font-size:20px; font-weight:800; margin:0 0 4px; }}
          .banner {{ margin-bottom:12px; padding:12px 14px; border-radius:14px; background:rgba(61,217,182,0.14); color:var(--accent); font-weight:700; }}
          .table-wrap {{ width:100%; max-width:100%; overflow-x:auto; overflow-y:hidden; border-radius:12px; border:1px solid var(--line); background:rgba(11,19,29,0.82); padding-bottom:8px; scrollbar-gutter:stable both-edges; }}
          .table-wrap::-webkit-scrollbar {{ height:12px; }}
          .table-wrap::-webkit-scrollbar-track {{ background:#0f1823; border-radius:999px; }}
          .table-wrap::-webkit-scrollbar-thumb {{ background:#32465d; border-radius:999px; border:2px solid #0f1823; }}
          .table-wrap::-webkit-scrollbar-thumb:hover {{ background:#47627f; }}
          table {{ width:100%; min-width:1460px; border-collapse:collapse; font-size:13px; }}
          th, td {{ text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }}
          th {{ color:var(--muted); font-weight:600; }}
          .mobile-position-list {{ display:none; gap:10px; margin-top:2px; }}
          .mobile-position-card {{
            border:1px solid var(--line);
            border-radius:10px;
            background:rgba(11,19,29,0.82);
            padding:10px;
          }}
          .mobile-position-head {{
            display:flex;
            justify-content:space-between;
            gap:10px;
            align-items:flex-start;
          }}
          .mobile-position-ticker {{ font-size:15px; font-weight:800; color:var(--accent); }}
          .mobile-position-grid {{
            display:grid;
            gap:6px;
            grid-template-columns:repeat(2, minmax(0, 1fr));
            margin-top:8px;
          }}
          .mobile-position-grid > div {{
            border:1px solid rgba(255,255,255,0.04);
            border-radius:10px;
            background:rgba(21,34,49,0.9);
            padding:8px 10px;
          }}
          .mobile-position-actions {{ display:grid; gap:8px; margin-top:10px; }}
          .position-actions {{ display:grid; gap:6px; min-width:120px; }}
          .ghost-btn {{
            width:100%;
            padding:10px 12px;
            border-radius:12px;
            border:1px solid var(--line);
            background:#0f1823;
            color:var(--ink);
            font-weight:700;
            cursor:pointer;
          }}
          .danger-btn {{ color:#fda4af; border-color:#4b1d28; background:#191018; }}
          .sell-modal {{
            position:fixed;
            inset:0;
            display:none;
            align-items:center;
            justify-content:center;
            padding:20px;
            background:rgba(3, 8, 14, 0.72);
            z-index:40;
          }}
          .sell-modal.open {{ display:flex; }}
          .sell-modal-card {{
            width:min(100%, 440px);
            border:1px solid var(--line);
            border-radius:18px;
            background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98));
            box-shadow:0 28px 60px rgba(0,0,0,0.32);
            padding:18px;
          }}
          .sell-modal-head {{
            display:flex;
            justify-content:space-between;
            gap:12px;
            align-items:flex-start;
            margin-bottom:12px;
          }}
          .sell-modal-form {{ display:grid; gap:10px; }}
          .sell-modal-actions {{ display:flex; gap:10px; justify-content:flex-end; margin-top:6px; }}
          .secondary-btn {{
            padding:10px 14px;
            border-radius:12px;
            border:1px solid var(--line);
            background:#0f1823;
            color:var(--ink);
            font-weight:700;
            cursor:pointer;
          }}
          .action-table-wrap table {{ min-width:1560px; }}
          .action-table th:nth-child(1),
          .action-table td:nth-child(1) {{
            position:sticky;
            left:0;
            z-index:4;
            min-width:104px;
            background:#0b131d;
            box-shadow:8px 0 18px rgba(0,0,0,0.22);
          }}
          .action-table th:nth-child(2),
          .action-table td:nth-child(2) {{
            position:sticky;
            left:104px;
            z-index:4;
            min-width:150px;
            max-width:150px;
            overflow:hidden;
            text-overflow:ellipsis;
            background:#0b131d;
            box-shadow:8px 0 18px rgba(0,0,0,0.16);
          }}
          .action-table th:nth-child(3),
          .action-table td:nth-child(3) {{
            position:sticky;
            left:254px;
            z-index:4;
            min-width:150px;
            max-width:150px;
            overflow:hidden;
            text-overflow:ellipsis;
            background:#0b131d;
            box-shadow:8px 0 18px rgba(0,0,0,0.10);
          }}
          .action-table th:nth-child(1),
          .action-table th:nth-child(2),
          .action-table th:nth-child(3) {{
            z-index:5;
            background:#101b27;
          }}
          .mobile-action-list {{ display:none; gap:10px; margin-top:2px; }}
          .mobile-action-card {{
            border:1px solid var(--line);
            border-radius:14px;
            background:rgba(11,19,29,0.82);
            padding:12px;
          }}
          .positions-table th:first-child,
          .positions-table td:first-child {{
            position:sticky;
            left:0;
            z-index:4;
            min-width:112px;
            background:#0b131d;
            box-shadow:8px 0 18px rgba(0,0,0,0.22);
          }}
          .positions-table th:nth-child(2),
          .positions-table td:nth-child(2) {{
            position:sticky;
            left:112px;
            z-index:4;
            min-width:150px;
            max-width:150px;
            overflow:hidden;
            text-overflow:ellipsis;
            background:#0b131d;
            box-shadow:8px 0 18px rgba(0,0,0,0.14);
          }}
          .positions-table th:nth-child(15),
          .positions-table td:nth-child(15) {{
            position:sticky;
            left:262px;
            z-index:4;
            min-width:130px;
            max-width:130px;
            overflow:hidden;
            text-overflow:ellipsis;
            background:#0b131d;
            box-shadow:8px 0 18px rgba(0,0,0,0.10);
          }}
          .positions-table th:first-child,
          .positions-table th:nth-child(2),
          .positions-table th:nth-child(15) {{
            z-index:5;
            background:#101b27;
          }}
          .table-wrap th:nth-child(2), .table-wrap td:nth-child(2) {{ min-width:150px; max-width:150px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(10), .table-wrap td:nth-child(10) {{ min-width:180px; max-width:180px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(11), .table-wrap td:nth-child(11) {{ min-width:150px; max-width:150px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(12), .table-wrap td:nth-child(12) {{ min-width:160px; max-width:160px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(13), .table-wrap td:nth-child(13) {{ min-width:150px; max-width:150px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(15), .table-wrap td:nth-child(15) {{ min-width:120px; max-width:120px; overflow:hidden; text-overflow:ellipsis; }}
          .stack {{ display:grid; gap:10px; }}
          .market-position-section {{ display:grid; gap:10px; margin-top:14px; }}
          .market-position-section:first-of-type {{ margin-top:10px; }}
          .market-position-head {{
            display:flex;
            justify-content:space-between;
            gap:12px;
            align-items:flex-end;
            padding:4px 2px 0;
          }}
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
          .hint {{ color:var(--muted); font-size:13px; }}
          .quote-preview {{
            margin-top:8px;
            border:1px solid var(--line);
            border-radius:12px;
            background:rgba(11,19,29,0.72);
            padding:10px;
            display:grid;
            gap:6px;
          }}
          .quote-grid {{ display:grid; gap:6px; grid-template-columns:repeat(2, minmax(0, 1fr)); }}
          .quote-cell {{ padding:8px 10px; border-radius:10px; background:rgba(21,34,49,0.9); border:1px solid rgba(255,255,255,0.03); }}
          .quote-label {{ color:var(--muted); font-size:11px; margin-bottom:3px; }}
          .quote-value {{ font-size:14px; font-weight:800; }}
          .quote-value.positive {{ color:#4ade80; }}
          .quote-value.negative {{ color:#f87171; }}
          .mobile-action-bucket {{ margin-top:10px; padding:10px 12px; border-radius:12px; background:rgba(61,217,182,0.10); border:1px solid rgba(61,217,182,0.18); color:var(--ink); font-weight:800; }}
          input, select, textarea {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); }}
          button {{ padding:10px 14px; border:none; border-radius:12px; background:var(--accent); color:#fff; font-weight:700; cursor:pointer; }}
          a {{ color:var(--accent); text-decoration:none; }}
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
          }}
          @media (max-width: 720px) {{
            .positions-table-wrap {{ display:none; }}
            .action-table-wrap {{ display:none; }}
            .mobile-position-list {{ display:grid; }}
            .mobile-action-list {{ display:grid; }}
            .quote-grid {{ grid-template-columns:1fr; }}
          }}
        </style>
        <script>
          let latestClose = null;

          function formatPreviewNumber(value, digits = 2) {{
            if (value === null || value === undefined || Number.isNaN(value)) {{
              return "--";
            }}
            return Number(value).toFixed(digits);
          }}

          function updatePortfolioPreview() {{
            const latestEl = document.getElementById("portfolio-latest-close");
            const valueEl = document.getElementById("portfolio-est-value");
            const pnlEl = document.getElementById("portfolio-est-pnl");
            const pnlPctEl = document.getElementById("portfolio-est-pnl-pct");
            const qty = Number(document.getElementById("portfolio-quantity").value || 0);
            const cost = Number(document.getElementById("portfolio-cost-basis").value || 0);

            latestEl.textContent = formatPreviewNumber(latestClose);
            latestEl.className = "quote-value";
            if (latestClose === null || Number.isNaN(latestClose)) {{
              valueEl.textContent = "--";
              pnlEl.textContent = "--";
              pnlPctEl.textContent = "--";
              pnlEl.className = "quote-value";
              pnlPctEl.className = "quote-value";
              return;
            }}

            const marketValue = latestClose * qty;
            const pnl = (latestClose - cost) * qty;
            const pnlPct = cost > 0 ? ((latestClose / cost) - 1) * 100 : null;
            valueEl.textContent = formatPreviewNumber(marketValue);
            pnlEl.textContent = formatPreviewNumber(pnl);
            pnlPctEl.textContent = pnlPct === null ? "--" : `${{formatPreviewNumber(pnlPct)}}%`;

            const toneClass = pnl > 0 ? "quote-value positive" : (pnl < 0 ? "quote-value negative" : "quote-value");
            const tonePctClass = pnlPct !== null && pnlPct > 0 ? "quote-value positive" : (pnlPct !== null && pnlPct < 0 ? "quote-value negative" : "quote-value");
            pnlEl.className = toneClass;
            pnlPctEl.className = tonePctClass;
          }}

          function openSellModal(ticker, name, quantity, price) {{
            const modal = document.getElementById("sell-modal");
            document.getElementById("sell-modal-title").textContent = ticker + (name ? " · " + name : "");
            document.getElementById("sell-ticker").value = ticker || "";
            document.getElementById("sell-quantity").value = Number(quantity || 0).toFixed(0);
            document.getElementById("sell-quantity").max = String(quantity || "");
            document.getElementById("sell-price").value = Number(price || 0).toFixed(2);
            document.getElementById("sell-reason").value = "止盈/保护利润";
            document.getElementById("sell-fee").value = "0";
            document.getElementById("sell-date").value = "{app_today_iso()}";
            modal.classList.add("open");
          }}

          function closeSellModal() {{
            const modal = document.getElementById("sell-modal");
            modal.classList.remove("open");
          }}

          async function loadPortfolioQuote() {{
            const tickerInput = document.getElementById("portfolio-ticker");
            const query = tickerInput.value.trim();
            latestClose = null;
            if (!query) {{
              updatePortfolioPreview();
              return;
            }}
            const response = await fetch(`/portfolio/quote?ticker=${{encodeURIComponent(query)}}`, {{ credentials: "same-origin" }});
            if (!response.ok) {{
              updatePortfolioPreview();
              return;
            }}
            const payload = await response.json();
            latestClose = payload.latest_close === null || payload.latest_close === undefined ? null : Number(payload.latest_close);
            updatePortfolioPreview();
          }}

          async function loadPortfolioSuggestions() {{
            const tickerInput = document.getElementById("portfolio-ticker");
            const marketSelect = document.getElementById("portfolio-market");
            const box = document.getElementById("portfolio-ticker-suggestions");
            const nameInput = document.getElementById("portfolio-name");
            const query = tickerInput.value.trim();
            if (!query) {{
              box.style.display = "none";
              box.innerHTML = "";
              return;
            }}
            const url = `/portfolio/suggest?q=${{encodeURIComponent(query)}}&market=${{encodeURIComponent(marketSelect.value)}}`;
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
                loadPortfolioQuote();
              }});
            }});
          }}

          window.addEventListener("DOMContentLoaded", () => {{
            const tickerInput = document.getElementById("portfolio-ticker");
            const marketSelect = document.getElementById("portfolio-market");
            const quantityInput = document.getElementById("portfolio-quantity");
            const costInput = document.getElementById("portfolio-cost-basis");
            const watchlistSelect = document.getElementById("portfolio-watchlist-picker");
            const box = document.getElementById("portfolio-ticker-suggestions");
            tickerInput.addEventListener("input", loadPortfolioSuggestions);
            tickerInput.addEventListener("change", loadPortfolioQuote);
            marketSelect.addEventListener("change", () => {{
              loadPortfolioSuggestions();
              loadPortfolioQuote();
            }});
            quantityInput.addEventListener("input", updatePortfolioPreview);
            costInput.addEventListener("input", updatePortfolioPreview);
            document.addEventListener("click", (event) => {{
              if (!box.contains(event.target) && event.target !== tickerInput) {{
                box.style.display = "none";
              }}
            }});
            if (watchlistSelect) {{
              watchlistSelect.addEventListener("change", () => {{
                const selected = watchlistSelect.options[watchlistSelect.selectedIndex];
                if (!selected || !selected.value) return;
                tickerInput.value = selected.value || "";
                const selectedName = selected.dataset.name || "";
                const selectedMarket = selected.dataset.market || "";
                const nameInput = document.getElementById("portfolio-name");
                if (selectedName) nameInput.value = selectedName;
                if (selectedMarket) marketSelect.value = selectedMarket;
                loadPortfolioQuote();
              }});
            }}
            document.addEventListener("keydown", (event) => {{
              if (event.key === "Escape") {{
                closeSellModal();
              }}
            }});
            updatePortfolioPreview();
          }});
        </script>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'持仓与复盘' if lang == 'zh' else 'Holdings & Review'}</h1>
              <p>{'先处理需要减风险或复核的仓位，再查看组合与历史复盘。' if lang == 'zh' else 'Handle trim and review positions first, then inspect the portfolio and history.'}</p>
            </div>
            <nav class="side-nav">{render_workspace_nav_html(lang=lang, active_key='portfolio')}</nav>
          </aside>
          <main class="content">
        <div class="wrap">
          <div id="sell-modal" class="sell-modal" onclick="if (event.target === this) closeSellModal();">
            <div class="sell-modal-card">
              <div class="sell-modal-head">
                <div>
                  <div class="eyebrow">{'卖出持仓' if lang == 'zh' else 'Sell Position'}</div>
                  <div id="sell-modal-title" style="font-size:18px;font-weight:800;"></div>
                </div>
                <button type="button" class="secondary-btn" onclick="closeSellModal()">{'关闭' if lang == 'zh' else 'Close'}</button>
              </div>
              <form action="/portfolio/sell" method="post" class="sell-modal-form">
                <input id="sell-ticker" type="hidden" name="ticker" />
                <input type="hidden" name="lang" value="{lang}" />
                <label class="muted">{'卖出日期' if lang == 'zh' else 'Sell Date'}</label>
                <input id="sell-date" type="date" name="trade_date" value="{app_today_iso()}" required />
                <label class="muted">{'卖出数量' if lang == 'zh' else 'Sell Quantity'}</label>
                <input id="sell-quantity" type="number" name="quantity" min="0.0001" step="0.0001" required />
                <label class="muted">{'卖出价格' if lang == 'zh' else 'Sell Price'}</label>
                <input id="sell-price" type="number" name="price" min="0.0001" step="0.0001" required />
                <label class="muted">{'手续费' if lang == 'zh' else 'Fee'}</label>
                <input id="sell-fee" type="number" name="fee" min="0" step="0.01" value="0" />
                <label class="muted">{'卖出原因' if lang == 'zh' else 'Sell Reason'}</label>
                <select id="sell-reason" name="reason">
                  {sell_reason_options_html}
                </select>
                <div class="sell-modal-actions">
                  <button type="button" class="secondary-btn" onclick="closeSellModal()">{'取消' if lang == 'zh' else 'Cancel'}</button>
                  <button type="submit">{'确认卖出' if lang == 'zh' else 'Confirm Sell'}</button>
                </div>
              </form>
            </div>
          </div>
          <div style="margin-bottom:16px;"><a href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to dashboard'}</a></div>
          {banner}
          <section class="card" style="margin-bottom:16px;padding:18px 18px 14px;">
            <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;">
              <div>
                <div class="eyebrow">{'组合总览' if lang == 'zh' else 'Portfolio Overview'}</div>
                <div style="font-size:28px;font-weight:900;letter-spacing:-0.03em;">{'分市场统计' if lang == 'zh' else 'Market-separated totals'}</div>
                <div class="muted">{'人民币和美元分别统计，不再混合成一个总市值。' if lang == 'zh' else 'CNY and USD values are shown separately instead of as one blended total.'}</div>
                <div class="muted" style="margin-top:8px;">{'持仓' if lang == 'zh' else 'Positions'} {intelligence['total_positions']}</div>
              </div>
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;flex:1;min-width:min(100%,760px);">
                {market_value_summary_html}
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{'风险姿态' if lang == 'zh' else 'Risk Posture'}</div>
                  <div style="display:flex;align-items:center;gap:10px;margin-top:6px;flex-wrap:wrap;">
                    <span style="display:inline-flex;align-items:center;padding:7px 11px;border-radius:999px;{posture_style}font-weight:800;font-size:12px;">{html.escape(str(intelligence.get('risk_posture') or ('均衡' if lang == 'zh' else 'Balanced')))}</span>
                    <span style="font-weight:800;">{intelligence['concentration_pct']:.1f}%</span>
                  </div>
                  <div class="muted" style="margin-top:8px;">{html.escape(str(intelligence.get('posture_summary') or intelligence['risk_summary']))}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{'最大单票' if lang == 'zh' else 'Largest Position'}</div>
                  <div style="margin-top:6px;font-size:20px;font-weight:900;">{float(top_position.get('weight_pct') or 0.0):.1f}%</div>
                  <div class="muted">{top_position_label}</div>
                  <div class="muted" style="margin-top:8px;">{'单票市值' if lang == 'zh' else 'Position value'} {_format_portfolio_money(float(top_position.get('market_value') or 0.0), market=top_position_market)}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{'市场暴露' if lang == 'zh' else 'Market Exposure'}</div>
                  <div style="margin-top:6px;font-size:20px;font-weight:900;">{html.escape(str(intelligence.get('top_market') or '-'))}</div>
                  <div class="muted">{market_mix_summary}</div>
                  <div class="muted" style="margin-top:8px;">{action_mix_summary}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{'处理队列' if lang == 'zh' else 'Action Queue'}</div>
                  <div style="margin-top:6px;font-size:20px;font-weight:900;">{int(intelligence.get('exit_candidates') or 0) + int(intelligence.get('trim_candidates') or 0) + int(intelligence.get('review_candidates') or 0)}</div>
                  <div class="muted">{queue_summary_text}</div>
                  <div style="margin-top:10px;">{risk_queue_html}</div>
                </article>
              </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px;"><span class="muted">{'按市场查看持仓' if lang == 'zh' else 'View positions by market'}:</span>{market_position_context_html}</div>
          </section>
          <section class="card" style="margin-bottom:16px;border-color:rgba(255,180,192,0.26);background:linear-gradient(115deg,rgba(255,180,192,0.08),rgba(17,28,40,0.94) 48%);">
            <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;">
              <div>
                <div class="eyebrow">{'今日处理优先' if lang == 'zh' else 'Today’s priority actions'}</div>
                <h2 style="margin:0 0 6px;font-size:21px;">{'先处理退出、减仓与复核，再查看完整持仓' if lang == 'zh' else 'Handle exits, trims, and reviews before the full positions list'}</h2>
                <div class="muted">{'这些队列使用现有动作建议、风险标签、仓位偏离与盈亏计算生成；不构成自动交易指令。' if lang == 'zh' else 'These queues use existing action guidance, risk tags, weight drift, and PnL; they are not automated trade instructions.'}</div>
                <div style="margin-top:10px;">{risk_queue_html}</div>
              </div>
              <a class="pill" href="#action-queue-detail">{'打开完整处理队列' if lang == 'zh' else 'Open full action queue'}</a>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:14px;">
              {action_queue_cards_html}
            </div>
          </section>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{'市场暴露' if lang == 'zh' else 'Market Exposure'}</div>
              <div class="muted">{'先看 A 股 / 美股暴露，再判断组合是不是过度偏单一市场。' if lang == 'zh' else 'Review CN/US exposure first before deciding whether the book is overly tilted to one market.'}</div>
              <div style="margin-top:12px;">{market_rankings_html}</div>
              <div class="muted" style="margin-top:10px;">{'当前最大单票' if lang == 'zh' else 'Largest position'}: {top_position_label} · {float(top_position.get('weight_pct') or 0.0):.1f}%</div>
              <div class="muted" style="margin-top:6px;">{'再平衡提醒' if lang == 'zh' else 'Rebalance alerts'}: {intelligence['rebalance_alerts']}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{'行业暴露' if lang == 'zh' else 'Sector Exposure'}</div>
              <div class="muted">{'先确认组合有没有过度集中在单一行业。' if lang == 'zh' else 'Check whether the portfolio is too concentrated in one sector first.'}</div>
              <div style="margin-top:12px;">{sector_rankings_html}</div>
            </article>
            <article class="card">
              <div class="eyebrow">Add Position</div>
              <form action="/portfolio/add" method="post" class="stack">
                <select id="portfolio-watchlist-picker">
                  <option value="">{'从自选股直接选择' if lang == 'zh' else 'Pick from watchlist'}</option>
                  {watchlist_picker_options}
                </select>
                <div class="suggest-wrap">
                  <input id="portfolio-ticker" type="text" name="ticker" placeholder="Ticker, e.g. ASTS or 600519.SH" autocomplete="off" required />
                  <div id="portfolio-ticker-suggestions" class="suggestions"></div>
                </div>
                <input id="portfolio-name" type="text" name="name" placeholder="Name auto-fills when available" />
                <select id="portfolio-market" name="market">
                  {option_html}
                </select>
                <input id="portfolio-quantity" type="number" step="1" min="0" name="quantity" placeholder="Quantity" required />
                <input id="portfolio-cost-basis" type="number" step="0.01" min="0" name="cost_basis" placeholder="Cost Basis" required />
                <input type="text" name="note" placeholder="Note" />
                <button type="submit">Save Position</button>
              </form>
              <div class="quote-preview">
                <div class="muted">{"输入代码、成本价和股数后，会根据本地最新收盘价即时试算。" if lang == "zh" else "Enter a ticker, cost basis, and quantity to preview value and unrealized PnL from the latest local close."}</div>
                <div class="quote-grid">
                  <div class="quote-cell"><div class="quote-label">{"收盘价" if lang == "zh" else "Close"}</div><div id="portfolio-latest-close" class="quote-value">--</div></div>
                  <div class="quote-cell"><div class="quote-label">{"预估市值" if lang == "zh" else "Estimated Value"}</div><div id="portfolio-est-value" class="quote-value">--</div></div>
                  <div class="quote-cell"><div class="quote-label">{"浮盈亏" if lang == "zh" else "Unrealized PnL"}</div><div id="portfolio-est-pnl" class="quote-value">--</div></div>
                  <div class="quote-cell"><div class="quote-label">{"浮盈亏%" if lang == "zh" else "Unrealized PnL %"}</div><div id="portfolio-est-pnl-pct" class="quote-value">--</div></div>
                </div>
              </div>
              <div class="stack" style="margin-top:12px;">
                {hint_html}
              </div>
            </article>
            <article class="card">
              <div class="eyebrow">Import / Export</div>
              <div class="muted" style="margin-bottom:10px;">用 CSV 快速维护持仓，字段为 ticker,name,market,quantity,cost_basis,note</div>
              <div style="margin-bottom:10px;"><a href="/portfolio/export">下载当前持仓 CSV</a></div>
              <form action="/portfolio/import" method="post" style="display:grid;gap:10px;">
                <textarea name="csv_text" placeholder="ticker,name,market,quantity,cost_basis,note&#10;ASTS,AST SpaceMobile,US,100,18.5,swing" style="width:100%;min-height:150px;border:1px solid var(--line);border-radius:12px;padding:12px;font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;background:#0f1823;color:var(--ink);"></textarea>
                <button type="submit">Import CSV</button>
              </form>
            </article>
          </section>
          <section class="card" id="action-queue-detail">
            <div class="eyebrow">{'动作建议' if lang == 'zh' else 'Action Board'}</div>
            <div class="muted">{'先看今天必须处理的持仓数量，再下钻到宽表。上面的处理队列数字和标签都可以点开，这里会显示对应股票。' if lang == 'zh' else 'Start with how many positions need action today, then drill into the wide table. The queue cards above jump here and show the matching names.'}</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;">
              <a class="pill" href="{_action_focus_href('all')}">{'全部' if lang == 'zh' else 'All'}</a>
              <a class="pill" href="{_action_focus_href('high_priority')}">{'高优先级' if lang == 'zh' else 'High Priority'}</a>
              <a class="pill" href="{_action_focus_href('exit_trim')}">{'退出 / 减仓' if lang == 'zh' else 'Exit / Trim'}</a>
              <a class="pill" href="{_action_focus_href('underwater')}">{'浮亏' if lang == 'zh' else 'Underwater'}</a>
              <a class="pill" href="{_action_focus_href('risk')}">{'风险标记' if lang == 'zh' else 'Risk'}</a>
              <a class="pill" href="{_action_focus_href('review')}">{'优先复核' if lang == 'zh' else 'Review'}</a>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:12px;">
              {action_queue_cards_html}
            </div>
            <div class="table-wrap action-table-wrap" style="margin-top:12px;">
              <table class="action-table">
                <thead>
                  <tr><th>Ticker</th><th>{'名称' if lang == 'zh' else 'Name'}</th><th>{'动作建议' if lang == 'zh' else 'Action'}</th><th>{'优先级' if lang == 'zh' else 'Priority'}</th><th>{'行业' if lang == 'zh' else 'Sector'}</th><th>{'信号' if lang == 'zh' else 'Signal'}</th><th>{'风险标签' if lang == 'zh' else 'Risk Tag'}</th><th>PnL %</th><th>{'当前权重' if lang == 'zh' else 'Weight'}</th><th>{'目标权重' if lang == 'zh' else 'Target'}</th><th>{'偏离' if lang == 'zh' else 'Gap'}</th><th>{'原因' if lang == 'zh' else 'Reason'}</th><th>{'调仓说明' if lang == 'zh' else 'Rebalance'}</th><th>{'执行风险' if lang == 'zh' else 'Execution Risk'}</th><th>{'新闻' if lang == 'zh' else 'News'}</th></tr>
                </thead>
                <tbody>{watch_action_rows_html}</tbody>
              </table>
            </div>
            <div class="mobile-action-list">{watch_action_cards_html}</div>
            <div class="muted" style="margin-top:10px;">{'桌面端可拖动底部滚动条查看更多列。' if lang == 'zh' else 'On desktop you can drag the horizontal scrollbar to inspect more columns.'}</div>
          </section>
          <section class="card">
            <div class="eyebrow">Positions</div>
            <div class="muted" style="margin-bottom:10px;">{'当前按涨幅排序，点击表头可切换升降序；A 股和美股会分开显示。' if lang == 'zh' else 'Sorted by day change; click the header to toggle direction. CN and US positions are shown in separate sections.'}</div>
            {grouped_positions_html}
            <div class="muted" style="margin-top:10px;">{'可拖动底部滚动条查看更多列。' if lang == 'zh' else 'Drag the horizontal scrollbar to see more columns.'}</div>
          </section>
          <section class="card">
            <div class="eyebrow">{'卖出记录' if lang == 'zh' else 'Sell Records'}</div>
            <div class="muted">{('已实现盈亏合计: ' + f'{realized_total:.2f}') if lang == 'zh' else ('Total realized PnL: ' + f'{realized_total:.2f}')}</div>
            <div class="muted" style="margin-top:8px;">{'下方可直接修正历史卖出原因；标黄的记录仍待补录，会影响每周复盘里的建议有效性统计。' if lang == 'zh' else 'You can correct historical sell reasons below. Highlighted rows still need review and will weaken the advice-effectiveness section in weekly review.'}</div>
            {sell_reason_progress_html}
            <div class="table-wrap" style="margin-top:12px;">
              <table>
                <thead>
                  <tr><th>{'日期' if lang == 'zh' else 'Date'}</th><th>Ticker</th><th>Name</th><th>{'数量' if lang == 'zh' else 'Qty'}</th><th>{'卖出价' if lang == 'zh' else 'Sell Price'}</th><th>{'成本' if lang == 'zh' else 'Cost'}</th><th>{'已实现盈亏' if lang == 'zh' else 'Realized PnL'}</th><th>{'剩余数量' if lang == 'zh' else 'Remaining'}</th><th>{'原因 / 修正' if lang == 'zh' else 'Reason / Edit'}</th></tr>
                </thead>
                <tbody>{recent_sell_rows}</tbody>
              </table>
            </div>
          </section>
        </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.post("/add")
def add_portfolio_position(
    request: Request,
    ticker: str = Form(...),
    name: str | None = Form(None),
    market: str | None = Form(None),
    quantity: float = Form(...),
    cost_basis: float = Form(...),
    note: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    market = market.strip().upper() if market else None
    ticker = normalize_ticker_for_market(ticker, market)
    if not ticker:
        return _redirect("Ticker is required.")
    upsert_portfolio_position(
        {
            "ticker": ticker,
            "name": name,
            "market": market,
            "quantity": quantity,
            "cost_basis": cost_basis,
            "note": note,
        }
    )
    _ensure_watchlist_membership(db, ticker=ticker, name=name, market=market)
    _clear_watchlist_caches()
    _refresh_workspace_snapshots_async()
    return _redirect("Saved position and added it to watchlist.")


@router.post("/remove")
def delete_portfolio_position(
    request: Request,
    ticker: str = Form(...),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    remove_portfolio_position(ticker)
    _refresh_workspace_snapshots_async()
    return _redirect("Removed position.")


@router.post("/sell")
def sell_portfolio(
    request: Request,
    ticker: str = Form(...),
    quantity: float = Form(...),
    price: float = Form(...),
    trade_date: str | None = Form(None),
    fee: float = Form(0.0),
    reason: str | None = Form(None),
    note: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    try:
        normalized_reason = str(reason or "").strip() or "其他"
        intelligence = build_portfolio_intelligence(db, lang="zh")
        audit_row = next(
            (
                item for item in (intelligence.get("all_items") or [])
                if str(item.get("ticker") or "").strip().upper() == str(ticker or "").strip().upper()
            ),
            {},
        )
        result = sell_portfolio_position(
            {
                "ticker": ticker,
                "quantity": quantity,
                "price": price,
                "trade_date": trade_date,
                "fee": fee,
                "reason": normalized_reason,
                "note": note,
                "action_hint_at_exit": audit_row.get("action_hint"),
                "action_priority_at_exit": audit_row.get("action_priority"),
                "action_reason_at_exit": audit_row.get("action_reason"),
                "rebalance_action_at_exit": audit_row.get("rebalance_action"),
                "risk_tag_at_exit": audit_row.get("risk_tag"),
            }
        )
    except ValueError as exc:
        return _redirect(str(exc))
    trade = result["trade"]
    status = "已清仓" if result.get("closed") else f"剩余 {trade['remaining_quantity']:.0f} 股"
    _clear_watchlist_caches()
    _refresh_workspace_snapshots_async()
    return _redirect(
        f"卖出 {trade['ticker']} {trade['quantity']:.0f} 股，已实现盈亏 {trade['realized_pnl']:.2f}，{status}。"
    )


@router.post("/trade-reason")
def portfolio_trade_reason(
    request: Request,
    trade_id: int = Form(...),
    reason: str = Form("其他"),
    lang: str = Form("zh"),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    update_portfolio_trade_reason(trade_id, reason)
    _refresh_workspace_snapshots_async()
    return _redirect("卖出原因已更新" if lang == "zh" else "Trade reason updated")


@router.post("/trade-reason/suggest")
def portfolio_trade_reason_suggest(
    request: Request,
    trade_id: int = Form(...),
    lang: str = Form("zh"),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    row = next((item for item in load_portfolio_trades() if int(item.get("id") or 0) == int(trade_id)), None)
    if row is None:
        return _redirect("未找到卖出记录" if lang == "zh" else "Trade record not found")
    suggested = suggest_trade_reason(row)
    update_portfolio_trade_reason(trade_id, suggested)
    _refresh_workspace_snapshots_async()
    return _redirect(
        f"{'已按建议补录为' if lang == 'zh' else 'Suggested reason applied:'} {suggested}"
    )


@router.post("/trade-reason/suggest-all")
def portfolio_trade_reason_suggest_all(
    request: Request,
    lang: str = Form("zh"),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    result = apply_suggested_trade_reasons(only_missing=True)
    _refresh_workspace_snapshots_async()
    return _redirect(
        (
            f"已按建议补录 {int(result.get('changed') or 0)} 条卖出记录"
            if lang == "zh"
            else f"Applied suggestions to {int(result.get('changed') or 0)} sell records"
        )
    )


@router.post("/trade-audit/backfill")
def portfolio_trade_audit_backfill(
    request: Request,
    trade_id: int = Form(...),
    lang: str = Form("zh"),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    backfill_trade_audit_snapshot(trade_id)
    _refresh_workspace_snapshots_async()
    return _redirect("已补录历史建议快照" if lang == "zh" else "Historical audit snapshot backfilled")


@router.post("/trade-audit/backfill-all")
def portfolio_trade_audit_backfill_all(
    request: Request,
    lang: str = Form("zh"),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    result = backfill_trade_audit_snapshots(only_missing=True)
    _refresh_workspace_snapshots_async()
    return _redirect(
        (
            f"已补录 {int(result.get('changed') or 0)} 条历史建议快照"
            if lang == "zh"
            else f"Backfilled {int(result.get('changed') or 0)} historical audit snapshots"
        )
    )


@router.get("/export")
def export_portfolio(request: Request) -> Response:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["ticker", "name", "market", "quantity", "cost_basis", "note"])
    writer.writeheader()
    for row in load_portfolio_positions():
        writer.writerow(
            {
                "ticker": row.get("ticker"),
                "name": row.get("name"),
                "market": row.get("market"),
                "quantity": row.get("quantity"),
                "cost_basis": row.get("cost_basis"),
                "note": row.get("note"),
            }
        )
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="portfolio_book.csv"'},
    )


@router.post("/import")
async def import_portfolio(
    request: Request,
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    form = await request.form()
    csv_text = str(form.get("csv_text") or "").strip()
    if not csv_text:
        return _redirect("No CSV content provided.")
    reader = csv.DictReader(StringIO(csv_text))
    imported = 0
    for row in reader:
        ticker = str(row.get("ticker") or "").strip()
        if not ticker:
            continue
        market = str(row.get("market") or "").strip().upper() or None
        normalized_ticker = normalize_ticker_for_market(ticker, market)
        upsert_portfolio_position(
            {
                "ticker": normalized_ticker,
                "name": row.get("name"),
                "market": market,
                "quantity": row.get("quantity") or 0,
                "cost_basis": row.get("cost_basis") or 0,
                "note": row.get("note"),
            }
        )
        _ensure_watchlist_membership(
            db,
            ticker=normalized_ticker,
            name=row.get("name"),
            market=market,
        )
        imported += 1
    _clear_watchlist_caches()
    _refresh_workspace_snapshots_async()
    return _redirect(f"Imported {imported} position(s) and synced them into watchlist.")
