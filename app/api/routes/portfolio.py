from __future__ import annotations

import csv
import html
import threading
from io import StringIO

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db_session
from app.models.schema import SymbolCreate
from app.services.auth import is_authenticated, login_redirect
from app.services.portfolio_intelligence import (
    build_position_management_fields,
    build_portfolio_ai_summary,
    build_portfolio_intelligence,
)
from app.services.portfolio_book import (
    load_portfolio_positions,
    load_portfolio_trades,
    remove_portfolio_position,
    sell_portfolio_position,
    upsert_portfolio_position,
)
from app.services.price_snapshot import load_latest_close, load_latest_closes
from app.services.repository import PredictionRepository, SymbolRepository, WatchlistRepository
from app.services.runtime_cache import clear_namespace, get_or_set
from app.services.symbol_catalog import infer_symbol_record, search_symbol_catalog
from app.services.ticker_format import normalize_ticker_for_market
from app.services.time_utils import app_today_iso
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html
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


def _compact_text(value: str | None, limit: int = 28) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


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
    seen = {(item["ticker"], item["market"]) for item in results}
    text = q.strip().upper()
    if not text:
        return results[:8]
    for symbol in symbol_repo.list_symbols():
        if market_value and (symbol.market or "").upper() != market_value:
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
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    lang = resolve_request_lang(request, default="zh")
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
    snapshot_payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
    intelligence = (snapshot_payload or {}).get("intelligence") if isinstance(snapshot_payload, dict) else None
    if not isinstance(intelligence, dict):
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
    if isinstance(snapshot_payload, dict) and isinstance(snapshot_payload.get("rows"), list) and isinstance(snapshot_payload.get("totals"), dict):
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
                latest_price = float(latest_prices.get(item["ticker"]) or 0.0)
                quantity = float(item.get("quantity") or 0.0)
                cost_basis = float(item.get("cost_basis") or 0.0)
                market_value = latest_price * quantity
                cost_value = cost_basis * quantity
                pnl = market_value - cost_value
                pnl_pct = ((latest_price / cost_basis) - 1.0) * 100 if cost_basis else 0.0
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
                        "ai_headline": ai_summary["ai_headline"],
                        "ai_verdict": ai_summary["ai_verdict"],
                        "ai_strategy": ai_summary["ai_strategy"],
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
        news_row = nlp_map.get(str(row.get("ticker") or "").strip().upper()) or {}
        row["news_sentiment_label"] = news_row.get("sentiment_label") or ("中性" if lang == "zh" else "neutral")
        row["news_summary"] = news_row.get("summary_text") or ""
        row["news_headline_count"] = int(news_row.get("headline_count") or 0)
    for row in intelligence.get("watch_items", []):
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
    recent_sell_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row.get('trade_date') or '-'))}</td>"
        f"<td>{html.escape(row.get('ticker') or '-')}</td>"
        f"<td title='{html.escape(row.get('name') or '-', quote=True)}'>{_compact_text(row.get('name') or row.get('ticker'), 18)}</td>"
        f"<td>{float(row.get('quantity') or 0.0):.0f}</td>"
        f"<td>{float(row.get('price') or 0.0):.2f}</td>"
        f"<td>{float(row.get('cost_basis') or 0.0):.2f}</td>"
        f"<td>{float(row.get('realized_pnl') or 0.0):.2f} ({float(row.get('realized_pnl_pct') or 0.0):.1f}%)</td>"
        f"<td>{float(row.get('remaining_quantity') or 0.0):.0f}</td>"
        f"<td title='{html.escape(row.get('reason') or '-', quote=True)}'>{_compact_text(row.get('reason') or '-', 18)}</td>"
        "</tr>"
        for row in trades[:10]
    ) or f"<tr><td colspan='9'>{'暂无卖出记录' if lang == 'zh' else 'No sell records yet.'}</td></tr>"
    realized_total = sum(float(row.get("realized_pnl") or 0.0) for row in trades)
    sector_rankings_html = "".join(
        "<article style='display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--line);'>"
        f"<div><div style='font-weight:800'>{row['sector']}</div><div class='muted'>{'市值暴露' if lang == 'zh' else 'Exposure'}</div></div>"
        f"<div style='text-align:right;'><div style='font-weight:700'>{row['weight_pct']:.1f}%</div><div class='muted'>{row['market_value']:.2f}</div></div>"
        "</article>"
        for row in intelligence["sector_rankings"]
    ) or f"<div class='muted'>{'暂无行业暴露数据' if lang == 'zh' else 'No sector exposure data yet'}</div>"
    watch_items_html = "".join(
        "<article style='display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--line);align-items:flex-start;'>"
        f"<div><div style='font-weight:800'>{row['ticker']}</div><div class='muted' style='margin-top:4px;'>{row.get('name') or row['ticker']}</div><div class='muted' style='margin-top:4px;'>{row['sector']} · {row['signal_label']} · {row['risk_tag']}</div><div class='muted' style='margin-top:6px;'>{row['action_reason']}</div><div class='muted' style='margin-top:6px;'>{row['rebalance_action']}</div><div class='muted' style='margin-top:6px;'>{row['execution_risk_summary']}</div><div class='muted' style='margin-top:6px;'>{row.get('news_text') or '-'}</div></div>"
        f"<div style='text-align:right;'><div style='font-weight:700'>{row['action_hint']}</div><div class='muted'>{'优先级' if lang == 'zh' else 'Priority'}: {row['action_priority']}</div><div class='muted'>{row['pnl_pct']:.1f}% · {row['weight_pct']:.1f}%</div><div class='muted'>{'目标仓位' if lang == 'zh' else 'Target'}: {(f"{row['target_weight_pct']:.1f}%" if row.get('target_weight_pct') is not None else '-')}</div><div class='muted'>{'偏离' if lang == 'zh' else 'Gap'}: {(f"{row['rebalance_gap_pct']:.1f}%" if row.get('rebalance_gap_pct') is not None else '-')}</div></div>"
        "</article>"
        for row in intelligence["watch_items"]
    ) or f"<div class='muted'>{'暂无持仓动作建议' if lang == 'zh' else 'No action items yet'}</div>"
    rows_html = "".join(
        "<tr>"
        f"<td>{row['ticker']}</td>"
        f"<td title='{row['name']}'>{_compact_text(row['name'], 24)}</td>"
        f"<td>{row['market']}</td>"
        f"<td>{row['quantity']:.0f}</td>"
        f"<td>{row['cost_basis']:.2f}</td>"
        f"<td>{row['latest_price']:.2f}</td>"
        f"<td>{row['market_value']:.2f}</td>"
        f"<td>{row['pnl']:.2f} ({row['pnl_pct']:.1f}%)</td>"
        f"<td>{row['ai_verdict']}</td>"
        f"<td title='{row['ai_headline']}'>{_compact_text(row['ai_headline'], 30)}</td>"
        f"<td title='{row['ai_strategy']}'>{_compact_text(row['ai_strategy'], 24)}</td>"
        f"<td title='当前仓位 {float(row.get('current_weight_pct') or 0.0):.1f}% · {row.get('target_weight_source') or '-'}'>{row['target_weight_text']}</td>"
        f"<td>{row['action_bucket']}</td>"
        f"<td title='{html.escape(row.get('news_summary') or '-', quote=True)}'>{html.escape(row.get('news_sentiment_label') or '-')} · {int(row.get('news_headline_count') or 0)}</td>"
        f"<td title='{html.escape(row['note'] or '-', quote=True)}'>{_compact_text(row['note'] or '-', 20)}</td>"
        "<td>"
        f"<form action='/portfolio/sell' method='post' style='display:grid;gap:6px;min-width:220px;'>"
        f"<input type='hidden' name='ticker' value='{row['ticker']}' />"
        f"<input type='hidden' name='lang' value='{lang}' />"
        f"<input type='date' name='trade_date' value='{app_today_iso()}' title='{'卖出日期' if lang == 'zh' else 'Sell date'}' />"
        f"<input type='number' name='quantity' min='0.0001' max='{float(row['quantity']):.6f}' step='0.0001' value='{float(row['quantity']):.0f}' title='{'卖出数量' if lang == 'zh' else 'Sell quantity'}' />"
        f"<input type='number' name='price' min='0.0001' step='0.0001' value='{float(row['latest_price']):.2f}' title='{'卖出价格' if lang == 'zh' else 'Sell price'}' />"
        f"<input type='number' name='fee' min='0' step='0.01' value='0' title='{'手续费' if lang == 'zh' else 'Fee'}' />"
        f"<input type='text' name='reason' placeholder='{'卖出原因' if lang == 'zh' else 'Sell reason'}' />"
        f"<button type='submit'>{'卖出' if lang == 'zh' else 'Sell'}</button>"
        f"</form>"
        f"<form action='/portfolio/remove' method='post' style='margin-top:6px;'><input type='hidden' name='ticker' value='{row['ticker']}' /><button type='submit'>{'删除记录' if lang == 'zh' else 'Remove Record'}</button></form>"
        "</td>"
        "</tr>"
        for row in rows
    ) or "<tr><td colspan='16'>No positions yet.</td></tr>"

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
          .app {{ display:grid; grid-template-columns:280px minmax(0, 1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px; }}
          .wrap {{ max-width: 1200px; margin:0 auto; padding:0 0 56px; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); margin-bottom:16px; }}
          .card {{ background: linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:700; margin-bottom:12px; }}
          .metric {{ font-size:28px; font-weight:800; margin:4px 0 8px; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .banner {{ margin-bottom:16px; padding:14px 16px; border-radius:16px; background:rgba(61,217,182,0.14); color:var(--accent); font-weight:700; }}
          .table-wrap {{ width:100%; max-width:100%; overflow-x:auto; overflow-y:hidden; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); padding-bottom:8px; scrollbar-gutter:stable both-edges; }}
          .table-wrap::-webkit-scrollbar {{ height:12px; }}
          .table-wrap::-webkit-scrollbar-track {{ background:#0f1823; border-radius:999px; }}
          .table-wrap::-webkit-scrollbar-thumb {{ background:#32465d; border-radius:999px; border:2px solid #0f1823; }}
          .table-wrap::-webkit-scrollbar-thumb:hover {{ background:#47627f; }}
          table {{ width:100%; min-width:1380px; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }}
          th {{ color:var(--muted); font-weight:600; }}
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
            background:#0b131d;
            box-shadow:8px 0 18px rgba(0,0,0,0.14);
          }}
          .positions-table th:first-child,
          .positions-table th:nth-child(2) {{
            z-index:5;
            background:#101b27;
          }}
          .table-wrap th:nth-child(2), .table-wrap td:nth-child(2) {{ min-width:150px; max-width:150px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(10), .table-wrap td:nth-child(10) {{ min-width:180px; max-width:180px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(11), .table-wrap td:nth-child(11) {{ min-width:150px; max-width:150px; overflow:hidden; text-overflow:ellipsis; }}
          .table-wrap th:nth-child(14), .table-wrap td:nth-child(14) {{ min-width:120px; max-width:120px; overflow:hidden; text-overflow:ellipsis; }}
          .stack {{ display:grid; gap:10px; }}
          .suggest-wrap {{ position:relative; }}
          .suggestions {{
            position:absolute;
            top:100%;
            left:0;
            right:0;
            z-index:10;
            background:#111c28;
            border:1px solid var(--line);
            border-radius:14px;
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
            padding:12px;
            cursor:pointer;
          }}
          .suggestion:last-child {{ border-bottom:none; }}
          .suggestion:hover {{ background:rgba(61,217,182,0.10); color:var(--accent); }}
          .suggest-name {{ display:block; font-weight:700; }}
          .suggest-meta {{ display:block; color:var(--muted); font-size:12px; margin-top:4px; }}
          .hint {{ color:var(--muted); font-size:13px; }}
          .quote-preview {{
            margin-top:12px;
            border:1px solid var(--line);
            border-radius:16px;
            background:rgba(11,19,29,0.72);
            padding:12px;
            display:grid;
            gap:8px;
          }}
          .quote-grid {{ display:grid; gap:8px; grid-template-columns:repeat(2, minmax(0, 1fr)); }}
          .quote-cell {{ padding:10px 12px; border-radius:12px; background:rgba(21,34,49,0.9); border:1px solid rgba(255,255,255,0.03); }}
          .quote-label {{ color:var(--muted); font-size:12px; margin-bottom:4px; }}
          .quote-value {{ font-size:16px; font-weight:800; }}
          .quote-value.positive {{ color:#4ade80; }}
          .quote-value.negative {{ color:#f87171; }}
          input, select, textarea {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); }}
          button {{ padding:10px 14px; border:none; border-radius:12px; background:var(--accent); color:#fff; font-weight:700; cursor:pointer; }}
          a {{ color:var(--accent); text-decoration:none; }}
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
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
            updatePortfolioPreview();
          }});
        </script>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>Portfolio</h1>
              <p>把持仓、盈亏和 AI 态度放进同一个执行面板。</p>
            </div>
            <nav class="side-nav">{render_workspace_nav_html(lang=lang, active_key='portfolio')}</nav>
          </aside>
          <main class="content">
        <div class="wrap">
          <div style="margin-bottom:16px;"><a href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to dashboard'}</a></div>
          {banner}
          <section class="grid">
            <article class="card">
              <div class="eyebrow">Portfolio</div>
              <div class="metric">{total_market_value:.2f}</div>
              <div class="muted">总市值 | 总盈亏 {total_pnl:.2f} ({total_pnl_pct:.1f}%)</div>
            </article>
            <article class="card">
              <div class="eyebrow">{'组合风险' if lang == 'zh' else 'Portfolio Risk'}</div>
              <div class="metric">{intelligence['concentration_pct']:.1f}%</div>
              <div class="muted">{intelligence['risk_summary']}</div>
              <div class="muted" style="margin-top:8px;">{'最大市场暴露' if lang == 'zh' else 'Largest market exposure'}: {intelligence['top_market']}</div>
            </article>
            <article class="card">
              <div class="eyebrow">AI Posture</div>
              <div class="muted">持仓页会结合当前 AI 分析，给出每只持仓的结论与策略提示。</div>
              <div class="muted" style="margin-top:8px;">{'当前持仓数' if lang == 'zh' else 'Current positions'}: {intelligence['total_positions']}</div>
              <div class="muted" style="margin-top:8px;">{'动作优先级' if lang == 'zh' else 'Action mix'}: {'高' if lang == 'zh' else 'H'} {intelligence['action_mix']['high']} / {'中' if lang == 'zh' else 'M'} {intelligence['action_mix']['medium']} / {'低' if lang == 'zh' else 'L'} {intelligence['action_mix']['low']}</div>
              <div class="muted" style="margin-top:8px;">{'显著偏离目标仓位的持仓' if lang == 'zh' else 'Positions materially away from target'}: {intelligence['rebalance_alerts']}</div>
              <div class="muted" style="margin-top:8px;">{'当前权重规则' if lang == 'zh' else 'Current weighting rule'}: {'按目标仓位为主，明显偏离时优先复核或调仓。' if lang == 'zh' else 'Target weights lead; review or rebalance when drift becomes material.'}</div>
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
                <div class="muted">{"输入代码、成本价和股数后，会根据本地最新价即时试算。" if lang == "zh" else "Enter a ticker, cost basis, and quantity to preview value and unrealized PnL from the latest local close."}</div>
                <div class="quote-grid">
                  <div class="quote-cell"><div class="quote-label">{"最新价" if lang == "zh" else "Latest Close"}</div><div id="portfolio-latest-close" class="quote-value">--</div></div>
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
            <article class="card">
              <div class="eyebrow">{'动作建议' if lang == 'zh' else 'Action Board'}</div>
              <div class="muted">{'先看高暴露持仓和模型态度变化，再决定是否加减仓。' if lang == 'zh' else 'Review the highest-exposure positions and model posture changes first.'}</div>
              <div style="margin-top:12px;">{watch_items_html}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{'行业暴露' if lang == 'zh' else 'Sector Exposure'}</div>
              <div class="muted">{'先确认组合有没有过度集中在单一行业。' if lang == 'zh' else 'Check whether the portfolio is too concentrated in one sector first.'}</div>
              <div style="margin-top:12px;">{sector_rankings_html}</div>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">Positions</div>
            <div class="table-wrap">
            <table class="positions-table">
              <thead>
                <tr><th>Ticker</th><th>Name</th><th>Market</th><th>Qty</th><th>Cost</th><th>Last</th><th>Market Value</th><th>PnL</th><th>AI Verdict</th><th>AI Headline</th><th>AI Strategy</th><th>{'目标仓位' if lang == 'zh' else 'Target Wt'}</th><th>{'动作桶' if lang == 'zh' else 'Action Bucket'}</th><th>{'新闻' if lang == 'zh' else 'News'}</th><th>{'备注' if lang == 'zh' else 'Note'}</th><th>{'操作' if lang == 'zh' else 'Actions'}</th></tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            </div>
            <div class="muted" style="margin-top:10px;">{'可拖动底部滚动条查看更多列。' if lang == 'zh' else 'Drag the horizontal scrollbar to see more columns.'}</div>
          </section>
          <section class="card">
            <div class="eyebrow">{'卖出记录' if lang == 'zh' else 'Sell Records'}</div>
            <div class="muted">{('已实现盈亏合计: ' + f'{realized_total:.2f}') if lang == 'zh' else ('Total realized PnL: ' + f'{realized_total:.2f}')}</div>
            <div class="table-wrap" style="margin-top:12px;">
              <table>
                <thead>
                  <tr><th>{'日期' if lang == 'zh' else 'Date'}</th><th>Ticker</th><th>Name</th><th>{'数量' if lang == 'zh' else 'Qty'}</th><th>{'卖出价' if lang == 'zh' else 'Sell Price'}</th><th>{'成本' if lang == 'zh' else 'Cost'}</th><th>{'已实现盈亏' if lang == 'zh' else 'Realized PnL'}</th><th>{'剩余数量' if lang == 'zh' else 'Remaining'}</th><th>{'原因' if lang == 'zh' else 'Reason'}</th></tr>
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
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    try:
        result = sell_portfolio_position(
            {
                "ticker": ticker,
                "quantity": quantity,
                "price": price,
                "trade_date": trade_date,
                "fee": fee,
                "reason": reason,
                "note": note,
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
