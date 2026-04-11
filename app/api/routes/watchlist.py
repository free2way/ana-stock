from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.ai_analysis import AIAnalysisService
from app.models.schema import SymbolCreate
from app.services.analysis_fusion import safe_symbol_analysis
from app.services.auth import is_authenticated, login_redirect
from app.services.market_intelligence import build_symbol_decision_brief
from app.services.market_sync import sync_market_data
from app.services.repository import PredictionRepository, PredictionTradePlanRepository, SymbolRepository, WatchlistRepository
from app.services.model_signal_summary import build_signal_label, model_confidence, signal_strength
from app.services.runtime_cache import clear_namespace, get_or_set
from app.services.symbol_catalog import infer_symbol_record, search_symbol_catalog
from app.services.ticker_format import normalize_ticker_for_market
from app.services.watchlist_metadata import refresh_watchlist_metadata


router = APIRouter(prefix="/watchlist", tags=["watchlist"])


def _clear_watchlist_caches() -> None:
    clear_namespace("watchlist_items")
    clear_namespace("watchlist_analysis_fragment")
    clear_namespace("watchlist_table_fragment")


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


def _decision_chip(value: str) -> str:
    normalized = str(value or "HOLD").upper()
    bg = "#f3f4f6"
    fg = "#374151"
    if "BUY" in normalized:
        bg, fg = "#dcfce7", "#166534"
    elif "SELL" in normalized:
        bg, fg = "#fee2e2", "#991b1b"
    elif "HOLD" in normalized:
        bg, fg = "#fef3c7", "#92400e"
    return (
        "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;white-space:nowrap;'>{normalized}</span>"
    )


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

    return get_or_set("watchlist_items", cache_key, ttl_seconds=15.0, loader=_load)


def _render_watchlist_analysis_fragment(
    *,
    items: list[dict],
    view_mode: str,
    analysis_limit: int,
    ai_analysis_limit: int,
) -> str:
    pre_ranked_items = sorted(items, key=_watchlist_pre_rank)
    detailed_tickers = {item["ticker"] for item in pre_ranked_items[:analysis_limit]}
    ai_tickers = {item["ticker"] for item in pre_ranked_items[: min(ai_analysis_limit, analysis_limit)]}
    ai_service = AIAnalysisService()
    detailed_items: list[dict] = []
    for item in items:
        model_output = item.get("model_output")
        if item["ticker"] in detailed_tickers:
            overview = {
                "ticker": item["ticker"],
                "market": item.get("market"),
                "exchange": item.get("exchange"),
            }
            combined = safe_symbol_analysis(overview, model_output)
            decision_brief = build_symbol_decision_brief(
                ticker=item["ticker"],
                combined_analysis=combined,
                latest_signal=model_output,
            )
        else:
            combined = _lightweight_watchlist_analysis(model_output)
            decision_brief = _lightweight_watchlist_brief(item["ticker"], model_output, combined)
        enriched = dict(item)
        enriched["combined_analysis"] = combined
        enriched["decision_brief"] = decision_brief
        enriched["mode_priority"] = _watchlist_mode_priority(combined, view_mode)
        if item["ticker"] in ai_tickers:
            ai_payload = ai_service.analyze_symbol(
                overview={
                    "ticker": item["ticker"],
                    "name": item.get("name"),
                    "market": item.get("market"),
                    "exchange": item.get("exchange"),
                },
                latest_signal=model_output,
                combined_analysis=combined,
                lang="en",
            )
            enriched["ai_brief"] = ai_payload.get("headline") or ai_payload.get("summary") or "-"
        else:
            enriched["ai_brief"] = "AI brief available for top-ranked names."
        detailed_items.append(enriched)

    risk_counts: dict[str, int] = {}
    risk_examples: list[dict] = []
    for item in detailed_items:
        tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        risk_examples.append({"ticker": item["ticker"], "tags": tags[:2]})
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
    return f"""
      <section class="card" style="margin-bottom:16px;">
        <div class="eyebrow">Decision Console</div>
        <div class="muted" style="margin-bottom:10px;">View Mode</div>
        <div style="display:grid;gap:16px;grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));">
          <article class="card" style="margin:0;background:#f9f7f0;">
            <div class="eyebrow">High Priority</div>
            <div style="font-size:28px;font-weight:800;margin:6px 0;">{high_priority}</div>
            <div class="muted">Watchlist names currently rated BUY or STRONG BUY.</div>
          </article>
          <article class="card" style="margin:0;background:#f9f7f0;">
            <div class="eyebrow">Caution</div>
            <div style="font-size:28px;font-weight:800;margin:6px 0;">{caution_count}</div>
            <div class="muted">Names where the blended decision is SELL or STRONG SELL.</div>
          </article>
          <article class="card" style="margin:0;background:#f9f7f0;">
            <div class="eyebrow">Top Briefs</div>
            <div class="muted">{"<br/>".join(f"{item['ticker']}: {item.get('decision_brief', {}).get('headline')}" for item in ranked_items[:3]) or "-"}</div>
          </article>
          <article class="card" style="margin:0;background:#f9f7f0;">
            <div class="eyebrow">AI Briefs</div>
            <div class="muted">{"<br/>".join(f"{item['ticker']}: {item.get('ai_brief')}" for item in ranked_items[:3]) or "-"}</div>
          </article>
        </div>
      </section>

      <section class="card" style="margin-bottom:16px;">
        <div class="eyebrow">Risk Overview</div>
        <div style="display:grid;gap:16px;grid-template-columns:repeat(auto-fit, minmax(240px, 1fr));">
          <article class="card" style="margin:0;background:#f9f7f0;">
            <div class="eyebrow">Tagged Names</div>
            <div style="font-size:28px;font-weight:800;margin:6px 0;">{len(risk_examples)}</div>
            <div class="muted">Watchlist names that currently carry execution warnings.</div>
          </article>
          <article class="card" style="margin:0;background:#f9f7f0;">
            <div class="eyebrow">Common Risks</div>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;">
              {"".join(f"<span class='linkbtn'>{tag} · {count}</span>" for tag, count in risk_top_tags) or "<span class='muted'>No execution warnings in the current watchlist view.</span>"}
            </div>
            <div class="muted">Examples: {" · ".join(f"{item['ticker']} ({' / '.join(item['tags'])})" for item in risk_examples) or "-"}</div>
          </article>
        </div>
      </section>
    """


def _render_watchlist_table_fragment(*, items: list[dict]) -> str:
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
                f"<td colspan='12'>{_market_section_label(current_market)}</td>"
                "</tr>"
            )
            previous_market = current_market
        item_rows_list.append(
            "<tr>"
            f"<td><a href='/watchlist/open/{item['item_id']}'>{item['ticker']}</a></td>"
            f"<td>{item['name'] or item['ticker']}</td>"
            f"<td>{item['market'] or '-'}</td>"
            f"<td>{item['exchange'] or '-'}</td>"
            f"<td>{_decision_chip((item.get('combined_analysis') or {}).get('decision') or 'HOLD')}</td>"
            f"<td>{(item.get('combined_analysis') or {}).get('confidence') or '-'}</td>"
            f"<td>{item.get('decision_brief', {}).get('headline') or '-'}</td>"
            f"<td>{item.get('ai_brief') or '-'}</td>"
            f"<td>{' · '.join(item.get('execution_tags') or []) or '-'}</td>"
            f"<td>{sync_state_text(item)}</td>"
            f"<td>{item['last_synced_date'] or '-'}</td>"
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
    item_rows = "".join(item_rows_list) or "<tr><td colspan='12'>No stocks in your watchlist yet.</td></tr>"
    return f"""
      <section class="card table-card">
        <div class="eyebrow">Saved Stocks</div>
        <table>
          <thead>
            <tr><th>Ticker</th><th>Name</th><th>Market</th><th>Exchange</th><th>Decision</th><th>Confidence</th><th>Decision Brief</th><th>AI Brief</th><th>Execution Tags</th><th>Sync</th><th>Last Sync</th><th>Actions</th></tr>
          </thead>
          <tbody>{item_rows}</tbody>
        </table>
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
    analysis_limit: int = Query(12, ge=1, le=60),
    ai_analysis_limit: int = Query(3, ge=0, le=12),
    execution_tag_filter: str = Query("ALL"),
    exclude_execution_tag_filter: str = Query("ALL"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    view_mode = (mode or "monitor").strip().lower()
    if view_mode not in {"premarket", "monitor", "postmarket"}:
        view_mode = "monitor"
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
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

    mode_switch_html = "".join(
        (
            f"<a href='/watchlist?{urlencode({'mode': value})}' "
            "style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            f"border:1px solid {'#0f766e' if value == view_mode else '#cde9e4'};"
            f"background:{'#0f766e' if value == view_mode else '#fffdf7'};"
            f"color:{'#fff' if value == view_mode else '#0f766e'};text-decoration:none;font-weight:800;font-size:12px;'>{label}</a>"
        )
        for value, label in (
            ("premarket", "Premarket"),
            ("monitor", "Monitor"),
            ("postmarket", "Postmarket"),
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
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Watchlist</title>
        <style>
          :root {{
            --bg: #f5efe2;
            --panel: #fffdf7;
            --ink: #1f2937;
            --muted: #6b7280;
            --line: #d6cfc2;
            --accent: #0f766e;
            --accent-soft: #dff5ef;
            --danger: #b91c1c;
          }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background:
            radial-gradient(circle at top left, #fff6d8 0, transparent 30%),
            radial-gradient(circle at top right, #d9f3ee 0, transparent 35%),
            var(--bg); }}
          .wrap {{ max-width: 1120px; margin: 0 auto; padding: 28px 20px 56px; }}
          .topbar {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }}
          .topbar a {{ color: var(--accent); text-decoration:none; }}
          .banner {{ margin-bottom:16px; padding:14px 16px; border-radius:16px; background:#dff5ef; color:#0f766e; font-weight:700; }}
          .hero {{ display:grid; gap:16px; grid-template-columns: minmax(320px, 1.1fr) minmax(340px, 1fr); margin-bottom:16px; }}
          .card {{ background: var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow: 0 8px 24px rgba(31,41,55,0.05); }}
          .nav-grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:16px; }}
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
          .nav-card:hover {{ border-color:#0f766e; box-shadow:0 12px 28px rgba(15,118,110,0.10); }}
          .nav-head {{ display:flex; align-items:center; gap:12px; margin-bottom:10px; }}
          .nav-icon {{
            width:42px; height:42px; border-radius:14px; display:inline-flex; align-items:center; justify-content:center;
            background:#eef8f5; color:#0f766e; font-size:12px; font-weight:900; letter-spacing:0.04em; border:1px solid #cde9e4; flex:0 0 auto;
          }}
          .nav-title {{ font-size:18px; font-weight:800; color:#0f766e; }}
          .nav-kicker {{ color:var(--muted); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          h1 {{ margin:0 0 8px; font-size:38px; }}
          p {{ margin:0; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          form {{ margin:0; }}
          .stack {{ display:grid; gap:12px; }}
          .suggest-wrap {{ position:relative; }}
          .suggestions {{
            position:absolute;
            top:100%;
            left:0;
            right:0;
            z-index:10;
            background:#fffdf7;
            border:1px solid var(--line);
            border-radius:14px;
            box-shadow:0 10px 28px rgba(31,41,55,0.08);
            margin-top:6px;
            display:none;
            overflow:hidden;
          }}
          .suggestion {{
            width:100%;
            display:block;
            text-align:left;
            background:#fffdf7;
            color:var(--ink);
            border:none;
            border-bottom:1px solid var(--line);
            border-radius:0;
            padding:12px;
            cursor:pointer;
          }}
          .suggestion:last-child {{ border-bottom:none; }}
          .suggestion:hover {{ background:#eef8f5; color:#0f766e; }}
          .suggest-name {{ display:block; font-weight:700; }}
          .suggest-meta {{ display:block; color:var(--muted); font-size:12px; margin-top:4px; }}
          input, select, button {{
            border-radius:12px;
            border:1px solid var(--line);
            padding:10px 12px;
            font:inherit;
            background:#fff;
          }}
          button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
          button.danger {{ background:var(--danger); border-color:var(--danger); padding:8px 10px; }}
          .hint {{ color:var(--muted); font-size:13px; }}
          .checkline {{ display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); }}
          th {{ color:var(--muted); font-weight:600; }}
          .market-section td {{ background:#f7f4ec; color:#0f766e; font-weight:800; letter-spacing:0.03em; border-top:1px solid var(--line); }}
          .table-card a {{ color: var(--accent); text-decoration:none; }}
          .linkbtn {{ display:inline-block; padding:8px 10px; border-radius:10px; background:#eef8f5; color:#0f766e; font-weight:700; }}
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
            const analysisPanels = document.getElementById("watchlist-analysis-panels");
            const tablePanel = document.getElementById("watchlist-table-panel");
            tickerInput.addEventListener("input", loadSuggestions);
            marketSelect.addEventListener("change", loadSuggestions);
            document.addEventListener("click", (event) => {{
              if (!box.contains(event.target) && event.target !== tickerInput) {{
                box.style.display = "none";
              }}
            }});
            if (analysisPanels) {{
              fetch("/watchlist/analysis-fragment?{urlencode({'mode': view_mode, 'analysis_limit': analysis_limit, 'ai_analysis_limit': ai_analysis_limit, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}", {{ credentials: "same-origin" }})
                .then((response) => response.text())
                .then((html) => {{
                  analysisPanels.innerHTML = html;
                }})
                .catch(() => {{
                  analysisPanels.innerHTML = "<section class='card' style='margin-bottom:16px;'><div class='eyebrow'>Decision Console</div><div class='muted'>Failed to load watchlist analysis panels.</div></section>";
                }});
            }}
            if (tablePanel) {{
              fetch("/watchlist/table-fragment?{urlencode({'mode': view_mode, 'execution_tag_filter': execution_tag_filter, 'exclude_execution_tag_filter': exclude_execution_tag_filter})}", {{ credentials: "same-origin" }})
                .then((response) => response.text())
                .then((html) => {{
                  tablePanel.innerHTML = html;
                }})
                .catch(() => {{
                  tablePanel.innerHTML = "<section class='card table-card'><div class='eyebrow'>Saved Stocks</div><div class='muted'>Failed to load watchlist table.</div></section>";
                }});
            }}
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
        <main class="wrap">
          <div class="topbar">
            <a href="/dashboard">← Back to dashboard</a>
            <a href="/screeners">Open Screener</a>
          </div>
          {banner}
          <section class="nav-grid">
            <a class="nav-card" href="/dashboard">
              <div class="nav-head">
                <span class="nav-icon">HOME</span>
                <div>
                  <div class="nav-kicker">Overview</div>
                  <div class="nav-title">Dashboard</div>
                </div>
              </div>
              <div class="muted">Return to the lightweight hub and navigate to Market Pulse, Continuous Leaders, or Operations.</div>
            </a>
            <a class="nav-card" href="/screeners">
              <div class="nav-head">
                <span class="nav-icon">SCAN</span>
                <div>
                  <div class="nav-kicker">Discovery</div>
                  <div class="nav-title">Screeners</div>
                </div>
              </div>
              <div class="muted">Open rule-based stock selection and turn candidates into watchlist names.</div>
            </a>
            <a class="nav-card" href="/dashboard/data-sources">
              <div class="nav-head">
                <span class="nav-icon">DATA</span>
                <div>
                  <div class="nav-kicker">Freshness</div>
                  <div class="nav-title">Data Sources</div>
                </div>
              </div>
              <div class="muted">Check provider freshness, concept mapping, and per-symbol sync source before acting.</div>
            </a>
          </section>
          <section class="hero">
            <article class="card">
              <div class="eyebrow">My Watchlist</div>
              <h1>Follow Stocks Across Markets</h1>
              <p class="muted">Add U.S. stocks, China A-shares, or Hong Kong stocks here. Then click any ticker to jump straight into its insight page.</p>
            </article>
            <article class="card">
              <div class="eyebrow">Add A Stock</div>
              <form class="stack" action="/watchlist/add" method="post">
                <div class="suggest-wrap">
                  <input id="watchlist-ticker" type="text" name="ticker" placeholder="Ticker, e.g. ASTS or 600519.SH" autocomplete="off" required />
                  <div id="ticker-suggestions" class="suggestions"></div>
                </div>
                <input id="watchlist-name" type="text" name="name" placeholder="Stock name will auto-fill when available" />
                <select id="watchlist-market" name="market">
                  {option_html}
                </select>
                <label class="checkline">
                  <input type="checkbox" name="sync_after_add" value="true" checked />
                  Add and sync now
                </label>
                <button type="submit">Add To Watchlist</button>
              </form>
              <div class="stack" style="margin-top:12px;">
                {hint_html}
              </div>
            </article>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">Data Sync</div>
            <form class="stack" action="/watchlist/sync-enabled" method="post" style="max-width:360px;">
              <input type="text" name="provider" value="yfinance" />
              <input type="text" name="start_date" value="2025-01-01" />
              <button type="submit">Sync Enabled Stocks</button>
            </form>
            <p class="muted" style="margin-top:10px;">Only stocks with Sync Enabled = On will be pulled when you click this button.</p>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">Execution Filters</div>
            <form class="stack" action="/watchlist" method="get" style="max-width:420px;">
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">Execution Tag</label>
                <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <label class="muted" style="display:block;margin-bottom:6px;">Exclude Tag</label>
                <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if exclude_execution_tag_filter.upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
              </div>
              <div>
                <div class="muted" style="margin-bottom:6px;">Quick Tags</div>
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
              <button type="submit">Apply Filters</button>
            </form>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">Decision Console</div>
            <div class="muted" style="margin-bottom:10px;">View Mode</div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;">{mode_switch_html}</div>
          </section>

          <div id="watchlist-analysis-panels">
            <section class="card" style="margin-bottom:16px;">
              <div class="eyebrow">Decision Console</div>
              <div class="muted">Loading analysis panels…</div>
            </section>
          </div>

          <div id="watchlist-table-panel">
            <section class="card table-card">
              <div class="eyebrow">Saved Stocks</div>
              <div class="muted">Loading watchlist table…</div>
            </section>
          </div>
        </main>
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
        return _render_watchlist_table_fragment(items=items)

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
        results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="yfinance")
        _clear_watchlist_caches()
        result = results[0] if results else None
        if result and result["status"] == "success":
            return _redirect_with_message(f"Added {ticker} and synced {result['rows']} rows.")
        if result:
            return _redirect_with_message(f"Added {ticker}, but sync failed: {result.get('message', 'Unknown error')}")
        return _redirect_with_message(f"Added {ticker}, but sync did not return a result.")

    _clear_watchlist_caches()
    return _redirect_with_message(f"Added {ticker} to your watchlist.")


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
    return _redirect_with_message("Sync setting updated.")


@router.get("/open/{item_id}")
def open_watchlist_item(item_id: int, request: Request, db: Session = Depends(get_db_session)) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    watchlist_repo = WatchlistRepository(db)
    item = watchlist_repo.get_item(item_id)
    if item is None:
        return _redirect_with_message("That watchlist item no longer exists.")
    if item["sync_enabled"] and item["sync_status"] != "success":
        return _redirect_with_message("Still Sync, Please wait")
    return RedirectResponse(url=f"/insights/{item['ticker']}", status_code=303)


@router.post("/sync-enabled")
def sync_enabled_watchlist_symbols(
    request: Request,
    provider: str = Form("yfinance"),
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
