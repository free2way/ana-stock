from __future__ import annotations

import csv
from io import StringIO

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.ai_analysis import AIAnalysisService
from app.services.analysis_fusion import safe_symbol_analysis
from app.services.auth import is_authenticated, login_redirect
from app.services.portfolio_book import load_portfolio_positions, remove_portfolio_position, upsert_portfolio_position
from app.services.repository import PredictionRepository, SymbolRepository


router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def _redirect(message: str | None = None) -> RedirectResponse:
    suffix = f"?message={message}" if message else ""
    return RedirectResponse(url=f"/portfolio{suffix}", status_code=303)


@router.get("", response_class=HTMLResponse)
def portfolio_page(
    request: Request,
    message: str | None = None,
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    ai_service = AIAnalysisService()

    raw_positions = load_portfolio_positions()
    rows: list[dict] = []
    total_market_value = 0.0
    total_cost = 0.0
    for item in raw_positions:
        overview = symbol_repo.get_overview(item["ticker"]) or {
            "ticker": item["ticker"],
            "name": item.get("name"),
            "market": item.get("market"),
        }
        latest_signal = None
        predictions = prediction_repo.list_symbol_predictions(item["ticker"], limit=1, latest_run_only=True)
        if predictions:
            latest_signal = predictions[0]
        combined = safe_symbol_analysis(overview, latest_signal)
        ai_payload = ai_service.analyze_symbol(
            overview=overview,
            latest_signal=latest_signal,
            combined_analysis=combined,
            lang="zh",
        )
        latest_price = float((combined or {}).get("latest_close") or 0.0)
        quantity = float(item.get("quantity") or 0.0)
        cost_basis = float(item.get("cost_basis") or 0.0)
        market_value = latest_price * quantity
        cost_value = cost_basis * quantity
        pnl = market_value - cost_value
        pnl_pct = ((latest_price / cost_basis) - 1.0) * 100 if cost_basis else 0.0
        total_market_value += market_value
        total_cost += cost_value
        rows.append(
            {
                "ticker": item["ticker"],
                "name": overview.get("name") or item["ticker"],
                "market": overview.get("market") or item.get("market") or "-",
                "quantity": quantity,
                "cost_basis": cost_basis,
                "latest_price": latest_price,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "ai_headline": ai_payload.get("headline") or "-",
                "ai_verdict": ai_payload.get("verdict") or "-",
                "ai_strategy": ai_payload.get("strategy") or "-",
                "note": item.get("note") or "",
            }
        )

    total_pnl = total_market_value - total_cost
    total_pnl_pct = ((total_market_value / total_cost) - 1.0) * 100 if total_cost else 0.0
    banner = f"<div class='banner'>{message}</div>" if message else ""
    rows_html = "".join(
        "<tr>"
        f"<td>{row['ticker']}</td>"
        f"<td>{row['name']}</td>"
        f"<td>{row['market']}</td>"
        f"<td>{row['quantity']:.0f}</td>"
        f"<td>{row['cost_basis']:.2f}</td>"
        f"<td>{row['latest_price']:.2f}</td>"
        f"<td>{row['market_value']:.2f}</td>"
        f"<td>{row['pnl']:.2f} ({row['pnl_pct']:.1f}%)</td>"
        f"<td>{row['ai_verdict']}</td>"
        f"<td>{row['ai_headline']}</td>"
        f"<td>{row['ai_strategy']}</td>"
        f"<td>{row['note'] or '-'}</td>"
        "<td>"
        f"<form action='/portfolio/remove' method='post'><input type='hidden' name='ticker' value='{row['ticker']}' /><button type='submit'>Remove</button></form>"
        "</td>"
        "</tr>"
        for row in rows
    ) or "<tr><td colspan='13'>No positions yet.</td></tr>"

    return f"""
    <!DOCTYPE html>
    <html lang="zh">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>持仓</title>
        <style>
          body {{ margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f5efe2; color:#1f2937; }}
          .wrap {{ max-width: 1200px; margin:0 auto; padding:28px 20px 56px; }}
          .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); margin-bottom:16px; }}
          .card {{ background:#fffdf7; border:1px solid #d6cfc2; border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:#dff5ef; color:#0f766e; font-size:12px; font-weight:700; margin-bottom:12px; }}
          .metric {{ font-size:28px; font-weight:800; margin:4px 0 8px; }}
          .muted {{ color:#6b7280; font-size:14px; }}
          .banner {{ margin-bottom:16px; padding:14px 16px; border-radius:16px; background:#dff5ef; color:#0f766e; font-weight:700; }}
          table {{ width:100%; border-collapse:collapse; font-size:14px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid #d6cfc2; vertical-align:top; }}
          th {{ color:#6b7280; font-weight:600; }}
          input {{ width:100%; padding:10px 12px; border-radius:12px; border:1px solid #d6cfc2; }}
          button {{ padding:10px 14px; border:none; border-radius:12px; background:#0f766e; color:#fff; font-weight:700; cursor:pointer; }}
          a {{ color:#0f766e; text-decoration:none; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div style="margin-bottom:16px;"><a href="/dashboard?lang=zh">← 返回 dashboard</a></div>
          {banner}
          <section class="grid">
            <article class="card">
              <div class="eyebrow">Portfolio</div>
              <div class="metric">{total_market_value:.2f}</div>
              <div class="muted">总市值 | 总盈亏 {total_pnl:.2f} ({total_pnl_pct:.1f}%)</div>
            </article>
            <article class="card">
              <div class="eyebrow">AI Posture</div>
              <div class="muted">持仓页会结合当前 AI 分析，给出每只持仓的结论与策略提示。</div>
            </article>
            <article class="card">
              <div class="eyebrow">Add Position</div>
              <form action="/portfolio/add" method="post" style="display:grid;gap:10px;">
                <input type="text" name="ticker" placeholder="Ticker, e.g. ASTS or 600519.SH" required />
                <input type="text" name="name" placeholder="Name" />
                <input type="text" name="market" placeholder="Market, e.g. US/CN/HK" />
                <input type="number" step="1" min="0" name="quantity" placeholder="Quantity" required />
                <input type="number" step="0.01" min="0" name="cost_basis" placeholder="Cost Basis" required />
                <input type="text" name="note" placeholder="Note" />
                <button type="submit">Save Position</button>
              </form>
            </article>
            <article class="card">
              <div class="eyebrow">Import / Export</div>
              <div class="muted" style="margin-bottom:10px;">用 CSV 快速维护持仓，字段为 ticker,name,market,quantity,cost_basis,note</div>
              <div style="margin-bottom:10px;"><a href="/portfolio/export">下载当前持仓 CSV</a></div>
              <form action="/portfolio/import" method="post" style="display:grid;gap:10px;">
                <textarea name="csv_text" placeholder="ticker,name,market,quantity,cost_basis,note&#10;ASTS,AST SpaceMobile,US,100,18.5,swing" style="width:100%;min-height:150px;border:1px solid #d6cfc2;border-radius:12px;padding:12px;font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;"></textarea>
                <button type="submit">Import CSV</button>
              </form>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">Positions</div>
            <table>
              <thead>
                <tr><th>Ticker</th><th>Name</th><th>Market</th><th>Qty</th><th>Cost</th><th>Last</th><th>Market Value</th><th>PnL</th><th>AI Verdict</th><th>AI Headline</th><th>AI Strategy</th><th>Note</th><th>Actions</th></tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
          </section>
        </main>
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
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
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
    return _redirect("Saved position.")


@router.post("/remove")
def delete_portfolio_position(request: Request, ticker: str = Form(...)) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/portfolio")
    remove_portfolio_position(ticker)
    return _redirect("Removed position.")


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
async def import_portfolio(request: Request) -> RedirectResponse:
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
        upsert_portfolio_position(
            {
                "ticker": ticker,
                "name": row.get("name"),
                "market": row.get("market"),
                "quantity": row.get("quantity") or 0,
                "cost_basis": row.get("cost_basis") or 0,
                "note": row.get("note"),
            }
        )
        imported += 1
    return _redirect(f"Imported {imported} position(s).")
