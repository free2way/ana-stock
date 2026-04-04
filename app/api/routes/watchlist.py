from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.models.schema import SymbolCreate
from app.services.auth import is_authenticated, login_redirect
from app.services.market_sync import sync_market_data
from app.services.repository import SymbolRepository, WatchlistRepository
from app.services.symbol_catalog import infer_symbol_record, search_symbol_catalog
from app.services.ticker_format import normalize_ticker_for_market
from app.services.watchlist_metadata import refresh_watchlist_metadata


router = APIRouter(prefix="/watchlist", tags=["watchlist"])


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
def watchlist_page(request: Request, message: str | None = None, db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    items = watchlist_repo.list_items(watchlist.id)

    def sync_state_text(item: dict) -> str:
        if item["sync_enabled"] and item["sync_status"] == "success":
            return "Ready"
        if item["sync_enabled"]:
            return "Waiting"
        return "Off"

    option_html = "".join(
        f"<option value='{market}'>{label}</option>"
        for market, label, _ in MARKET_OPTIONS
    )
    hint_html = "".join(
        f"<div class='hint'><strong>{label}:</strong> {hint}</div>"
        for _, label, hint in MARKET_OPTIONS
    )
    item_rows_list: list[str] = []
    previous_market = None
    for item in items:
        current_market = (item.get("market") or "").upper()
        if current_market != previous_market:
            item_rows_list.append(
                "<tr class='market-section'>"
                f"<td colspan='7'>{_market_section_label(current_market)}</td>"
                "</tr>"
            )
            previous_market = current_market
        item_rows_list.append(
            "<tr>"
            f"<td><a href='/watchlist/open/{item['item_id']}'>{item['ticker']}</a></td>"
            f"<td>{item['name'] or item['ticker']}</td>"
            f"<td>{item['market'] or '-'}</td>"
            f"<td>{item['exchange'] or '-'}</td>"
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
    item_rows = "".join(item_rows_list) or "<tr><td colspan='7'>No stocks in your watchlist yet.</td></tr>"

    banner = (
        f"<div class='banner'>{message}</div>"
        if message
        else ""
    )

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
            tickerInput.addEventListener("input", loadSuggestions);
            marketSelect.addEventListener("change", loadSuggestions);
            document.addEventListener("click", (event) => {{
              if (!box.contains(event.target) && event.target !== tickerInput) {{
                box.style.display = "none";
              }}
            }});
          }});
        </script>
      </head>
      <body>
        <main class="wrap">
          <div class="topbar">
            <a href="/dashboard">← Back to dashboard</a>
            <a href="/screeners">Open Screener</a>
          </div>
          {banner}
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

          <section class="card table-card">
            <div class="eyebrow">Saved Stocks</div>
            <table>
              <thead>
                <tr><th>Ticker</th><th>Name</th><th>Market</th><th>Exchange</th><th>Sync</th><th>Last Sync</th><th>Actions</th></tr>
              </thead>
              <tbody>{item_rows}</tbody>
            </table>
          </section>
        </main>
      </body>
    </html>
    """


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
        result = results[0] if results else None
        if result and result["status"] == "success":
            return _redirect_with_message(f"Added {ticker} and synced {result['rows']} rows.")
        if result:
            return _redirect_with_message(f"Added {ticker}, but sync failed: {result.get('message', 'Unknown error')}")
        return _redirect_with_message(f"Added {ticker}, but sync did not return a result.")

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
    success_count = sum(1 for item in results if item["status"] == "success")
    return _redirect_with_message(f"Synced {success_count}/{len(results)} enabled stocks.")


@router.post("/refresh-metadata")
def refresh_existing_watchlist_metadata(request: Request) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/watchlist")
    result = refresh_watchlist_metadata()
    return _redirect_with_message(f"Updated metadata for {result['updated_count']} existing watchlist stock(s).")
