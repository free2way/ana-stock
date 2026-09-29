import html

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.auth import is_authenticated, login_redirect
from app.services.repository import BacktestRepository


router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("")
def list_backtests(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = BacktestRepository(db)
    return repo.list_backtests()


@router.get("/latest/curve")
def latest_backtest_curve(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = BacktestRepository(db)
    return repo.get_latest_backtest_curve()


@router.get("/{strategy_run_id}")
def get_backtest(strategy_run_id: int, request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    payload = BacktestRepository(db).get_backtest(strategy_run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return payload


@router.get("/{strategy_run_id}/curve")
def get_backtest_curve(strategy_run_id: int, request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = BacktestRepository(db)
    if repo.get_backtest(strategy_run_id) is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return repo.get_daily_metrics(strategy_run_id)


@router.get("/{strategy_run_id}/trades")
def get_backtest_trades(
    strategy_run_id: int,
    request: Request,
    side: str | None = None,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = BacktestRepository(db)
    if repo.get_backtest(strategy_run_id) is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return repo.list_execution_audit(
        strategy_run_id,
        side=side,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/{strategy_run_id}/portfolio-states")
def get_backtest_portfolio_states(
    strategy_run_id: int,
    request: Request,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = BacktestRepository(db)
    if repo.get_backtest(strategy_run_id) is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return repo.get_portfolio_states(strategy_run_id)


@router.get("/{strategy_run_id}/view", response_class=HTMLResponse)
def backtest_detail_page(
    strategy_run_id: int,
    request: Request,
    lang: str = "zh",
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect(f"/backtests/{strategy_run_id}/view")
    lang = "en" if lang == "en" else "zh"
    repo = BacktestRepository(db)
    payload = repo.get_backtest(strategy_run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    summary = payload.get("summary") or {}
    counts = payload.get("audit_counts") or {}
    trades = repo.list_execution_audit(strategy_run_id, limit=500)
    states = repo.get_portfolio_states(strategy_run_id)

    def pct(value) -> str:
        return f"{float(value or 0.0) * 100:.2f}%"

    metrics = [
        ("引擎" if lang == "zh" else "Engine", summary.get("engine_version") or "legacy/unknown"),
        ("总收益" if lang == "zh" else "Total Return", pct(summary.get("total_return"))),
        ("基准收益" if lang == "zh" else "Benchmark", pct(summary.get("benchmark_total_return"))),
        ("最大回撤" if lang == "zh" else "Max Drawdown", pct(summary.get("max_drawdown"))),
        ("累计佣金" if lang == "zh" else "Fees", f"{float(summary.get('cumulative_fees') or 0.0):,.2f}"),
        ("累计滑点" if lang == "zh" else "Slippage", f"{float(summary.get('cumulative_slippage') or 0.0):,.2f}"),
    ]
    metric_html = "".join(
        f"<article><span>{html.escape(str(label))}</span><strong>{html.escape(str(value))}</strong></article>"
        for label, value in metrics
    )
    trade_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row.get('effective_date') or '-'))}</td>"
        f"<td>{html.escape(str(row.get('ticker') or '-'))}</td>"
        f"<td>{html.escape(str(row.get('side') or '-'))}</td>"
        f"<td><span class='status {html.escape(str(row.get('status') or ''))}'>{html.escape(str(row.get('status') or '-'))}</span></td>"
        f"<td>{float(row.get('quantity') or 0.0):,.0f}</td>"
        f"<td>{float(row.get('fill_price') or 0.0):,.4f}</td>"
        f"<td>{float(row.get('fee') or 0.0):,.2f}</td>"
        f"<td>{html.escape(str(row.get('reject_reason') or row.get('exit_reason') or '-'))}</td>"
        "</tr>"
        for row in trades
    ) or "<tr><td colspan='8'>No normalized trade audit for this legacy run.</td></tr>"
    state_rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(row.get('trade_date') or '-'))}</td>"
        f"<td>{float(row.get('nav') or 0.0):,.2f}</td>"
        f"<td>{float(row.get('cash') or 0.0):,.2f}</td>"
        f"<td>{float(row.get('position_market_value') or 0.0):,.2f}</td>"
        f"<td>{float(row.get('gross_exposure') or 0.0) * 100:.1f}%</td>"
        f"<td>{int(row.get('open_lots') or 0)}</td>"
        "</tr>"
        for row in states
    ) or "<tr><td colspan='6'>No normalized portfolio states for this legacy run.</td></tr>"
    title = "回测执行审计" if lang == "zh" else "Backtest Execution Audit"
    return f"""
    <!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{title} #{strategy_run_id}</title><style>
    :root{{--bg:#071018;--panel:#111c28;--line:#26384c;--ink:#e6edf3;--muted:#90a3b8;--accent:#3dd9b6;--danger:#ff7b87}}
    *{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at top left,#12304b 0,transparent 32%),var(--bg);color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
    main{{max-width:1180px;margin:auto;padding:28px 18px 60px}}a{{color:var(--accent)}}h1{{font-size:38px;margin:14px 0 6px}}.muted{{color:var(--muted)}}.card{{margin-top:18px;background:rgba(17,28,40,.95);border:1px solid var(--line);border-radius:22px;padding:20px;overflow:hidden}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin-top:20px}}article{{background:#0b1621;border:1px solid var(--line);border-radius:16px;padding:15px}}article span{{display:block;color:var(--muted);font-size:12px}}article strong{{display:block;font-size:21px;margin-top:8px}}.table{{overflow:auto;border:1px solid var(--line);border-radius:14px}}table{{width:100%;border-collapse:collapse;min-width:850px}}th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);font-size:13px;white-space:nowrap}}th{{color:var(--muted)}}.status{{padding:4px 8px;border-radius:999px;background:#1a3040}}.status.filled{{color:var(--accent)}}.status.rejected{{color:var(--danger)}}code{{color:var(--accent)}}
    </style></head><body><main>
    <a href="/dashboard/ops/models?lang={lang}">← {'返回模型页' if lang == 'zh' else 'Back to Models'}</a>
    <h1>{title} <code>#{strategy_run_id}</code></h1><div class="muted">{html.escape(str(payload.get('name') or '-'))} · {html.escape(str(payload.get('start_date') or '-'))} → {html.escape(str(payload.get('end_date') or '-'))}</div>
    <section class="metrics">{metric_html}</section>
    <section class="card"><h2>{'执行流水' if lang == 'zh' else 'Execution Ledger'}</h2><p class="muted">Orders {counts.get('orders',0)} · Fills {counts.get('fills',0)} · Rejects {counts.get('rejects',0)}</p><div class="table"><table><thead><tr><th>Date</th><th>Ticker</th><th>Side</th><th>Status</th><th>Qty</th><th>Fill</th><th>Fee</th><th>Reason</th></tr></thead><tbody>{trade_rows}</tbody></table></div></section>
    <section class="card"><h2>{'逐日账户' if lang == 'zh' else 'Daily Portfolio'}</h2><p class="muted">{counts.get('portfolio_states',0)} states · NAV = Cash + Position Value</p><div class="table"><table><thead><tr><th>Date</th><th>NAV</th><th>Cash</th><th>Positions</th><th>Gross</th><th>Lots</th></tr></thead><tbody>{state_rows}</tbody></table></div></section>
    </main></body></html>"""
