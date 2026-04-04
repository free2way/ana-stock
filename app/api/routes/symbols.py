from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.models.schema import SymbolCreate, SymbolRead
from app.services.repository import PredictionRepository, PriceSyncStateRepository, SymbolRepository
from app.services.symbol_details import SymbolDataService


router = APIRouter(prefix="/symbols", tags=["symbols"])


@router.get("", response_model=list[SymbolRead])
def list_symbols(db: Session = Depends(get_db_session)) -> list[SymbolRead]:
    repo = SymbolRepository(db)
    return repo.list_symbols()


@router.post("", response_model=SymbolRead)
def create_symbol(payload: SymbolCreate, db: Session = Depends(get_db_session)) -> SymbolRead:
    repo = SymbolRepository(db)
    existing = repo.get_by_ticker(payload.ticker)
    if existing:
        raise HTTPException(status_code=409, detail="Ticker already exists.")
    return repo.create_symbol(payload)


@router.get("/{ticker}/overview")
def symbol_overview(ticker: str, db: Session = Depends(get_db_session)) -> dict:
    symbol_repo = SymbolRepository(db)
    sync_repo = PriceSyncStateRepository(db)
    prediction_repo = PredictionRepository(db)

    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    predictions = prediction_repo.list_symbol_predictions(ticker, limit=10, latest_run_only=True)
    sync_state = sync_repo.get_state_for_ticker(ticker)
    latest_signal = predictions[0] if predictions else None

    return {
        "overview": overview,
        "latest_signal": latest_signal,
        "sync_state": sync_state,
    }


@router.get("/{ticker}/history")
def symbol_history(ticker: str, limit: int = 120, db: Session = Depends(get_db_session)) -> list[dict]:
    symbol_repo = SymbolRepository(db)
    if symbol_repo.get_by_ticker(ticker) is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    history = SymbolDataService().get_history(ticker, limit=limit)
    return history


@router.get("/{ticker}/signals")
def symbol_signals(
    ticker: str,
    limit: int = 120,
    latest_run_only: bool = True,
    db: Session = Depends(get_db_session),
) -> list[dict]:
    symbol_repo = SymbolRepository(db)
    if symbol_repo.get_by_ticker(ticker) is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    repo = PredictionRepository(db)
    return repo.list_symbol_predictions(ticker, limit=limit, latest_run_only=latest_run_only)


@router.get("/{ticker}", response_class=HTMLResponse)
def symbol_page(ticker: str, db: Session = Depends(get_db_session)) -> str:
    symbol_repo = SymbolRepository(db)
    sync_repo = PriceSyncStateRepository(db)
    prediction_repo = PredictionRepository(db)

    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    history = SymbolDataService().get_history(ticker, limit=60)
    signals = prediction_repo.list_symbol_predictions(ticker, limit=12, latest_run_only=True)
    sync_state = sync_repo.get_state_for_ticker(ticker)

    price_chart = "<div class='muted'>No price history available</div>"
    if history:
        width = 520
        height = 220
        left_pad = 18
        bottom_pad = 18
        top_pad = 12
        closes = [float(item["close"]) for item in history if item["close"] is not None]
        min_close = min(closes)
        max_close = max(closes)
        close_span = max(max_close - min_close, 0.000001)
        step_x = (width - left_pad * 2) / max(len(history) - 1, 1)
        points = []
        labels = []
        for index, item in enumerate(history):
            close = float(item["close"])
            x = left_pad + index * step_x
            y = top_pad + (height - top_pad - bottom_pad) * (1 - ((close - min_close) / close_span))
            points.append(f"{x:.2f},{y:.2f}")
            if index in (0, len(history) - 1):
                labels.append(
                    f"<text x='{x:.2f}' y='{height - 2}' font-size='10' fill='#6b7280' text-anchor='middle'>{item['date'][5:]}</text>"
                )
        price_chart = f"""
        <svg viewBox="0 0 {width} {height}" width="100%" height="220" role="img" aria-label="Price history curve">
          <rect x="0" y="0" width="{width}" height="{height}" rx="14" fill="#f8faf7"></rect>
          <line x1="{left_pad}" y1="{height-bottom_pad}" x2="{width-left_pad}" y2="{height-bottom_pad}" stroke="#d6cfc2" />
          <polyline fill="none" stroke="#1d4ed8" stroke-width="3" points="{' '.join(points)}"></polyline>
          {''.join(labels)}
        </svg>
        """

    signal_rows = "".join(
        f"<tr><td>{item['trade_date']}</td><td>{item['score']:.6f}</td><td>{int(item['rank_value'])}</td><td>{item['model_run_id']}</td></tr>"
        for item in signals
    ) or "<tr><td colspan='4'>No signal history yet</td></tr>"

    sync_text = (
        f"{sync_state['provider']} | {sync_state['status']} | {sync_state['last_synced_date'] or '-'}"
        if sync_state
        else "No sync state yet"
    )

    return f"""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{overview['ticker']} | Personal Quant Workbench</title>
        <style>
          :root {{
            --bg: #eef3f7;
            --panel: #ffffff;
            --ink: #17202a;
            --muted: #667085;
            --line: #d9e2ec;
            --accent: #1d4ed8;
            --accent-soft: #e8f0ff;
          }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--ink); }}
          .wrap {{ max-width: 1080px; margin: 0 auto; padding: 28px 20px 56px; }}
          .topbar {{ margin-bottom: 16px; }}
          .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); margin-bottom: 16px; }}
          .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 18px; box-shadow: 0 8px 24px rgba(23, 32, 42, 0.05); }}
          .eyebrow {{ display: inline-block; padding: 6px 10px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 12px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 12px; }}
          h1 {{ margin: 0 0 6px; font-size: 38px; }}
          .muted {{ color: var(--muted); font-size: 14px; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
          th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); }}
          th {{ color: var(--muted); font-weight: 600; }}
          a {{ color: var(--accent); text-decoration: none; }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div class="topbar"><a href="/dashboard">← Back to dashboard</a></div>
          <div class="eyebrow">Symbol Detail</div>
          <h1>{overview['ticker']}</h1>
          <p class="muted">{overview['name'] or overview['ticker']} | Market: {overview['market'] or '-'} | Sync: {sync_text}</p>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">Overview</div>
              <div class="muted">Exchange: {overview['exchange'] or '-'}</div>
              <div class="muted">Sector: {overview['sector'] or '-'}</div>
              <div class="muted">Industry: {overview['industry'] or '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">Latest Signal</div>
              <div class="muted">Date: {signals[0]['trade_date'] if signals else '-'}</div>
              <div class="muted">Score: {f"{signals[0]['score']:.6f}" if signals else '-'}</div>
              <div class="muted">Rank: {int(signals[0]['rank_value']) if signals else '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">Quick Links</div>
              <div><a href="/dashboard">Open dashboard</a></div>
              <div><a href="/insights/{overview['ticker']}">Open insight page</a></div>
              <div><a href="/symbols/{overview['ticker']}/history">JSON history</a></div>
              <div><a href="/symbols/{overview['ticker']}/signals">JSON signals</a></div>
              <div><a href="/symbols/{overview['ticker']}/signals?latest_run_only=false">All run signals</a></div>
            </article>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">Price History</div>
            {price_chart}
          </section>

          <section class="card">
            <div class="eyebrow">Signal History</div>
            <table>
              <thead>
                <tr><th>Date</th><th>Score</th><th>Rank</th><th>Model Run</th></tr>
              </thead>
              <tbody>{signal_rows}</tbody>
            </table>
          </section>
        </main>
      </body>
    </html>
    """
