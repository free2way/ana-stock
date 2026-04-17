import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.models.schema import SymbolCreate, SymbolRead
from app.services.analysis_fusion import safe_symbol_analysis
from app.services.ai_analysis import AIAnalysisService
from app.services.auth import is_authenticated, login_redirect
from app.services.market_intelligence import build_symbol_decision_brief, build_symbol_news_sentiment_brief
from app.services.market_news import MarketNewsService
from app.services.model_signal_summary import build_signal_label, model_confidence
from app.services.repository import PredictionRepository, PriceSyncStateRepository, SymbolRepository
from app.services.runtime_cache import get_or_set
from app.services.symbol_details import SymbolDataService
from app.services.technical_patterns import TechnicalPatternService
from app.services.tradingview_client import TradingViewClient
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html


router = APIRouter(prefix="/symbols", tags=["symbols"])


def _signal_chip(label: str, value: str) -> str:
    normalized = str(value or "-").upper()
    bg = "#f3f4f6"
    fg = "#374151"
    if "STRONG_BUY" in normalized or normalized == "BUY" or "BULLISH" in normalized:
        bg, fg = "#dcfce7", "#166534"
    elif "STRONG_SELL" in normalized or normalized == "SELL" or "BEARISH" in normalized:
        bg, fg = "#fee2e2", "#991b1b"
    elif "NEUTRAL" in normalized or "MIXED" in normalized:
        bg, fg = "#fef3c7", "#92400e"
    return (
        "<span style='display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:700;font-size:12px;'>{label} {normalized}</span>"
    )


def _lightweight_symbol_summary(overview: dict, latest_signal: dict | None) -> dict:
    score = None if latest_signal is None else latest_signal.get("score")
    label = (latest_signal or {}).get("signal_label") or build_signal_label(score, lang="en") or "Hold"
    decision = str(label).strip().upper()
    confidence = (latest_signal or {}).get("confidence") or model_confidence(score) or 45
    if decision not in {"BUY", "SELL", "WATCH", "HOLD"}:
        decision = "HOLD"
    if score is None:
        reason = "Waiting for a fresh model update."
    elif float(score) >= 0.18:
        reason = "Model score is supportive, so this name stays on the long radar."
    elif float(score) >= 0.05:
        reason = "Model score is constructive, but the setup still needs confirmation."
    elif float(score) <= -0.05:
        reason = "Model score is defensive, so risk control matters more here."
    else:
        reason = "Current setup is mixed and still needs more evidence."
    return {
        "decision": decision,
        "confidence": int(confidence),
        "score": int(round(float(score or 0) * 10)),
        "reason": reason,
        "headline": f"{overview['ticker']} is loading detailed analysis",
        "summary": "Open the JSON analysis endpoints or wait a moment for the live page panels to finish loading.",
    }


def _symbol_page_bundle(overview: dict, latest_signal: dict | None, *, headline_limit: int = 3) -> dict:
    cache_key = json.dumps(
        {
            "ticker": overview.get("ticker"),
            "market": overview.get("market"),
            "exchange": overview.get("exchange"),
            "headline_limit": headline_limit,
            "signal": {
                "trade_date": (latest_signal or {}).get("trade_date"),
                "score": (latest_signal or {}).get("score"),
                "rank_value": (latest_signal or {}).get("rank_value"),
            },
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _load() -> dict:
        combined = safe_symbol_analysis(overview, latest_signal)
        decision_brief = build_symbol_decision_brief(
            ticker=overview["ticker"],
            combined_analysis=combined,
            latest_signal=latest_signal,
        )
        news_brief = build_symbol_news_sentiment_brief(
            ticker=overview["ticker"],
            decision_brief=decision_brief,
            combined_analysis=combined,
        )
        return {
            "combined": combined,
            "decision_brief": decision_brief,
            "news_brief": news_brief,
            "news_feed": [],
        }

    return get_or_set("symbol_page_bundle", cache_key, ttl_seconds=90.0, loader=_load)


@router.get("", response_model=list[SymbolRead])
def list_symbols(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = SymbolRepository(db)
    return repo.list_symbols()


@router.post("", response_model=SymbolRead)
def create_symbol(payload: SymbolCreate, request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = SymbolRepository(db)
    existing = repo.get_by_ticker(payload.ticker)
    if existing:
        raise HTTPException(status_code=409, detail="Ticker already exists.")
    return repo.create_symbol(payload)


@router.get("/{ticker}/overview")
def symbol_overview(ticker: str, request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
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
def symbol_history(ticker: str, request: Request, limit: int = 120, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    if symbol_repo.get_by_ticker(ticker) is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    history = SymbolDataService().get_history(ticker, limit=limit)
    return history


@router.get("/{ticker}/signals")
def symbol_signals(
    ticker: str,
    request: Request,
    limit: int = 120,
    latest_run_only: bool = True,
    db: Session = Depends(get_db_session),
) :
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    if symbol_repo.get_by_ticker(ticker) is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    repo = PredictionRepository(db)
    return repo.list_symbol_predictions(ticker, limit=limit, latest_run_only=latest_run_only)


@router.get("/{ticker}/technical-rating")
def symbol_technical_rating(
    ticker: str,
    request: Request,
    interval: str = "1d",
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    payload = TradingViewClient().get_technical_rating(
        ticker=overview["ticker"],
        market=overview.get("market"),
        exchange=overview.get("exchange"),
        interval=interval,
    )
    if payload is None:
        raise HTTPException(status_code=400, detail="Unsupported market for TradingView technical rating.")
    return payload


@router.get("/{ticker}/multi-timeframe-analysis")
def symbol_multi_timeframe_analysis(
    ticker: str,
    request: Request,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    payload = TradingViewClient().get_multi_timeframe_analysis(
        ticker=overview["ticker"],
        market=overview.get("market"),
        exchange=overview.get("exchange"),
    )
    if payload is None:
        raise HTTPException(status_code=400, detail="Unsupported market for TradingView multi-timeframe analysis.")
    return payload


@router.get("/{ticker}/bollinger-band-analysis")
def symbol_bollinger_band_analysis(
    ticker: str,
    request: Request,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    payload = TechnicalPatternService().get_bollinger_band_analysis(overview["ticker"])
    if payload is None:
        raise HTTPException(status_code=404, detail="Insufficient price history for Bollinger analysis.")
    return payload


@router.get("/{ticker}/candlestick-patterns")
def symbol_candlestick_patterns(
    ticker: str,
    request: Request,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    payload = TechnicalPatternService().get_candlestick_patterns(overview["ticker"])
    if payload is None:
        raise HTTPException(status_code=404, detail="Insufficient price history for candlestick pattern analysis.")
    return payload


@router.get("/{ticker}/combined-analysis")
def symbol_combined_analysis(
    ticker: str,
    request: Request,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    latest_signal = None
    predictions = prediction_repo.list_symbol_predictions(ticker, limit=1, latest_run_only=True)
    if predictions:
        latest_signal = predictions[0]
    return safe_symbol_analysis(overview, latest_signal)


@router.get("/{ticker}/page-bundle")
def symbol_page_bundle(
    ticker: str,
    request: Request,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    latest_signal = None
    predictions = prediction_repo.list_symbol_predictions(ticker, limit=1, latest_run_only=True)
    if predictions:
        latest_signal = predictions[0]
    payload = _symbol_page_bundle(overview, latest_signal, headline_limit=3)
    return {"ticker": overview["ticker"], "status": "success", **payload}


@router.get("/{ticker}/ai-analysis")
def symbol_ai_analysis(
    ticker: str,
    request: Request,
    lang: str = "zh",
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    latest_signal = None
    predictions = prediction_repo.list_symbol_predictions(ticker, limit=1, latest_run_only=True)
    if predictions:
        latest_signal = predictions[0]
    combined = safe_symbol_analysis(overview, latest_signal)
    return AIAnalysisService().analyze_symbol(
        overview=overview,
        latest_signal=latest_signal,
        combined_analysis=combined,
        lang=lang,
    )


@router.get("/{ticker}/decision-brief")
def symbol_decision_brief(
    ticker: str,
    request: Request,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    latest_signal = None
    predictions = prediction_repo.list_symbol_predictions(ticker, limit=1, latest_run_only=True)
    if predictions:
        latest_signal = predictions[0]
    combined = safe_symbol_analysis(overview, latest_signal)
    return build_symbol_decision_brief(
        ticker=overview["ticker"],
        combined_analysis=combined,
        latest_signal=latest_signal,
    )


@router.get("/{ticker}/news-sentiment")
def symbol_news_sentiment(
    ticker: str,
    request: Request,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    latest_signal = None
    predictions = prediction_repo.list_symbol_predictions(ticker, limit=1, latest_run_only=True)
    if predictions:
        latest_signal = predictions[0]
    combined = safe_symbol_analysis(overview, latest_signal)
    decision_brief = build_symbol_decision_brief(
        ticker=overview["ticker"],
        combined_analysis=combined,
        latest_signal=latest_signal,
    )
    return build_symbol_news_sentiment_brief(
        ticker=overview["ticker"],
        decision_brief=decision_brief,
        combined_analysis=combined,
    )


@router.get("/{ticker}/news-feed")
def symbol_news_feed(
    ticker: str,
    request: Request,
    limit: int = 5,
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}")
    symbol_repo = SymbolRepository(db)
    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    headlines = MarketNewsService().fetch_symbol_headlines(
        ticker=overview["ticker"],
        name=overview.get("name"),
        limit=limit,
    )
    return {"ticker": overview["ticker"], "status": "success", "items": headlines}


@router.get("/{ticker}", response_class=HTMLResponse)
def symbol_page(ticker: str, request: Request, db: Session = Depends(get_db_session)) -> str:
    lang = resolve_request_lang(request)
    if not is_authenticated(request):
        return login_redirect(f"/symbols/{ticker}?lang={lang}")
    symbol_repo = SymbolRepository(db)
    sync_repo = PriceSyncStateRepository(db)
    prediction_repo = PredictionRepository(db)

    overview = symbol_repo.get_overview(ticker)
    if overview is None:
        raise HTTPException(status_code=404, detail="Ticker not found.")

    history = SymbolDataService().get_history(ticker, limit=60)
    signals = prediction_repo.list_symbol_predictions(ticker, limit=12, latest_run_only=True)
    sync_state = sync_repo.get_state_for_ticker(ticker)
    latest_signal = signals[0] if signals else None
    lightweight = _lightweight_symbol_summary(overview, latest_signal)
    decision_chip = _signal_chip("决策" if lang == "zh" else "Decision", lightweight["decision"])

    price_chart = f"<div class='muted'>{'暂无价格历史' if lang == 'zh' else 'No price history available'}</div>"
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
          <rect x="0" y="0" width="{width}" height="{height}" rx="14" fill="#0f1823"></rect>
          <line x1="{left_pad}" y1="{height-bottom_pad}" x2="{width-left_pad}" y2="{height-bottom_pad}" stroke="#223246" />
          <polyline fill="none" stroke="#3dd9b6" stroke-width="3" points="{' '.join(points)}"></polyline>
          {''.join(labels)}
        </svg>
        """

    signal_rows = "".join(
        f"<tr><td>{item['trade_date']}</td><td>{item['score']:.6f}</td><td>{int(item['rank_value'])}</td><td>{item['model_run_id']}</td></tr>"
        for item in signals
    ) or f"<tr><td colspan='4'>{'暂无信号历史' if lang == 'zh' else 'No signal history yet'}</td></tr>"

    sync_text = (
        f"{sync_state['provider']} | {sync_state['status']} | {sync_state['last_synced_date'] or '-'}"
        if sync_state
        else ("暂无同步状态" if lang == "zh" else "No sync state yet")
    )
    nav_html = render_workspace_nav_html(lang=lang, active_key="watchlist")
    latest_signal_text = (
        f"{signals[0]['trade_date']} | {signals[0]['score']:.6f} | #{int(signals[0]['rank_value'])}"
        if signals
        else ("暂无最新信号" if lang == "zh" else "No latest signal yet")
    )

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{overview['ticker']} | {'量化工作台' if lang == 'zh' else 'Personal Quant Workbench'}</title>
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
          }}
          * {{ box-sizing: border-box; }}
          body {{ margin: 0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background:
            radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),
            radial-gradient(circle at bottom right, rgba(61,217,182,0.10) 0, transparent 26%),
            var(--bg); }}
          .app {{ display:grid; grid-template-columns:280px minmax(0, 1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px; }}
          .wrap {{ max-width: 1120px; margin: 0 auto; padding: 0 0 56px; }}
          .topbar {{ display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom: 16px; }}
          .topbar a {{ color:var(--accent); text-decoration:none; }}
          .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); margin-bottom: 16px; }}
          .hero {{ display:grid; gap:16px; grid-template-columns:minmax(320px, 1.2fr) minmax(300px, 0.8fr); margin-bottom:16px; }}
          .card {{ background: linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border: 1px solid var(--line); border-radius: 22px; padding: 18px; box-shadow: 0 24px 48px rgba(0,0,0,0.18); }}
          .eyebrow {{ display: inline-block; padding: 6px 10px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 12px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 12px; }}
          h1 {{ margin: 0 0 6px; font-size: 38px; }}
          .muted {{ color: var(--muted); font-size: 14px; }}
          .chip-row {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }}
          .metric {{ font-size:28px; font-weight:800; margin:4px 0 8px; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
          th, td {{ text-align: left; padding: 10px 8px; border-bottom: 1px solid var(--line); }}
          th {{ color: var(--muted); font-weight: 600; }}
          a {{ color: var(--accent); text-decoration: none; }}
          .loading {{ color: var(--muted); font-size: 14px; }}
          .summary-list {{ display:grid; gap:10px; }}
          .summary-item strong {{ display:block; font-size:16px; margin-bottom:4px; }}
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
            .hero {{ grid-template-columns:1fr; }}
          }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'个股详情' if lang == 'zh' else 'Symbol Detail'}</h1>
              <p>{'把单只股票的模型、信号、AI 解释和快速数据入口放在同一个工作台里。' if lang == 'zh' else 'Keep model outputs, signal context, AI explanation, and quick data access for one stock in the same workspace.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
            <div class="sidebar-foot">{'先看最新信号和模型结论，再决定是否进入洞察页看更细的模型解释。' if lang == 'zh' else 'Review the latest signal and model verdict first, then open the insight page when you want deeper explanation.'}</div>
          </aside>
          <main class="content">
            <div class="wrap">
          <div class="topbar">
            <a href="/dashboard?lang={lang}">← {'返回首页' if lang == 'zh' else 'Back to dashboard'}</a>
            <div class="muted">{latest_signal_text}</div>
          </div>
          <section class="hero">
            <article class="card">
              <div class="eyebrow">{'个股详情' if lang == 'zh' else 'Symbol Detail'}</div>
              <h1>{overview['ticker']}</h1>
              <p class="muted">{overview['name'] or overview['ticker']} | {'市场' if lang == 'zh' else 'Market'}: {overview['market'] or '-'} | {'同步' if lang == 'zh' else 'Sync'}: {sync_text}</p>
              <div class="chip-row" style="margin-top:14px;">{decision_chip}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{'当前摘要' if lang == 'zh' else 'Current Summary'}</div>
              <div class="summary-list">
                <div class="summary-item"><strong>{'最新信号' if lang == 'zh' else 'Latest Signal'}</strong><div class="muted">{latest_signal_text}</div></div>
                <div class="summary-item"><strong>{'交易所 / 行业' if lang == 'zh' else 'Exchange / Sector'}</strong><div class="muted">{overview['exchange'] or '-'} | {overview['sector'] or '-'}</div></div>
                <div class="summary-item"><strong>{'当前判断' if lang == 'zh' else 'Current View'}</strong><div class="muted">{lightweight['reason']}</div></div>
              </div>
            </article>
          </section>
          <section class="grid">
            <article class="card">
              <div class="eyebrow">{'基础信息' if lang == 'zh' else 'Overview'}</div>
              <div class="muted">{'交易所' if lang == 'zh' else 'Exchange'}: {overview['exchange'] or '-'}</div>
              <div class="muted">{'板块' if lang == 'zh' else 'Sector'}: {overview['sector'] or '-'}</div>
              <div class="muted">{'行业' if lang == 'zh' else 'Industry'}: {overview['industry'] or '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{'最新信号' if lang == 'zh' else 'Latest Signal'}</div>
              <div class="muted">{'日期' if lang == 'zh' else 'Date'}: {signals[0]['trade_date'] if signals else '-'}</div>
              <div class="muted">Score: {f"{signals[0]['score']:.6f}" if signals else '-'}</div>
              <div class="muted">Rank: {int(signals[0]['rank_value']) if signals else '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">{'综合分析' if lang == 'zh' else 'Combined Analysis'}</div>
              <div class="metric">{decision_chip}</div>
              <div class="muted" id="combined-meta">Confidence: {lightweight['confidence']} | Score: {lightweight['score']}</div>
              <div class="chip-row" id="combined-chips"></div>
              <div class="muted" style="margin-top:10px;" id="combined-reasons">{lightweight['reason']}</div>
            </article>
            <article class="card">
              <div class="eyebrow">AI Analysis</div>
              <div class="muted" id="ai-headline">Load AI analysis when you want a deeper decision note.</div>
              <div class="muted" style="margin-top:8px;" id="ai-summary">This panel is now on-demand so the detail page opens faster.</div>
              <div class="chip-row" id="ai-chips"></div>
              <div class="muted" style="margin-top:10px;" id="ai-levels"></div>
              <div class="muted" style="margin-top:10px;" id="ai-checklist"></div>
              <div style="margin-top:12px;">
                <button id="load-ai-analysis" type="button" style="border-radius:12px;border:1px solid var(--line);padding:10px 12px;background:#1b2a3b;color:var(--ink);font:inherit;font-weight:700;cursor:pointer;">{'加载 AI 分析' if lang == 'zh' else 'Load AI Analysis'}</button>
              </div>
            </article>
            <article class="card">
              <div class="eyebrow">{'决策摘要' if lang == 'zh' else 'Decision Brief'}</div>
              <div class="muted" id="decision-headline">{lightweight['headline']}</div>
              <div class="muted" style="margin-top:8px;" id="decision-summary">{lightweight['summary']}</div>
              <div class="chip-row" id="decision-chips"></div>
            </article>
            <article class="card">
              <div class="eyebrow">{'快速入口' if lang == 'zh' else 'Quick Links'}</div>
              <div><a href="/dashboard?lang={lang}">{'打开首页' if lang == 'zh' else 'Open dashboard'}</a></div>
              <div><a href="/insights/{overview['ticker']}?lang={lang}">{'打开洞察页' if lang == 'zh' else 'Open insight page'}</a></div>
              <div><a href="/symbols/{overview['ticker']}/history">{'JSON 历史价格' if lang == 'zh' else 'JSON history'}</a></div>
              <div><a href="/symbols/{overview['ticker']}/signals">{'JSON 信号' if lang == 'zh' else 'JSON signals'}</a></div>
              <div><a href="/symbols/{overview['ticker']}/signals?latest_run_only=false">{'全部运行信号' if lang == 'zh' else 'All run signals'}</a></div>
              <div><a href="/symbols/{overview['ticker']}/technical-rating">TradingView technical rating</a></div>
              <div><a href="/symbols/{overview['ticker']}/multi-timeframe-analysis">Multi-timeframe analysis</a></div>
              <div><a href="/symbols/{overview['ticker']}/bollinger-band-analysis">Bollinger band analysis</a></div>
              <div><a href="/symbols/{overview['ticker']}/candlestick-patterns">Candlestick patterns</a></div>
              <div><a href="/symbols/{overview['ticker']}/combined-analysis">Combined analysis</a></div>
              <div><a href="/symbols/{overview['ticker']}/decision-brief">Decision brief</a></div>
              <div><a href="/symbols/{overview['ticker']}/news-sentiment">News sentiment</a></div>
              <div><a href="/symbols/{overview['ticker']}/news-feed">News feed</a></div>
              <div><a href="/symbols/{overview['ticker']}/ai-analysis">AI analysis</a></div>
            </article>
          </section>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">TradingView Multi-Timeframe</div>
              <div class="muted" id="mtf-summary">Loading multi-timeframe analysis...</div>
              <div class="chip-row" id="mtf-chips"></div>
            </article>
            <article class="card">
              <div class="eyebrow">Bollinger Band</div>
              <div class="muted" id="bollinger-summary">Loading Bollinger band analysis...</div>
            </article>
            <article class="card">
              <div class="eyebrow">Candlestick Patterns</div>
              <div class="muted" id="candlestick-summary">Loading candlestick patterns...</div>
            </article>
            <article class="card">
              <div class="eyebrow">News Sentiment</div>
              <div class="muted" id="news-headline">Loading sentiment brief...</div>
              <div class="muted" style="margin-top:8px;" id="news-summary">External headlines will appear here when available.</div>
              <div class="chip-row" id="news-chips"></div>
              <div id="news-feed" class="loading" style="margin-top:8px;">Loading external headlines...</div>
            </article>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">{'价格历史' if lang == 'zh' else 'Price History'}</div>
            {price_chart}
          </section>

          <section class="card">
            <div class="eyebrow">{'信号历史' if lang == 'zh' else 'Signal History'}</div>
            <table>
              <thead>
                <tr><th>{'日期' if lang == 'zh' else 'Date'}</th><th>Score</th><th>Rank</th><th>{'模型运行' if lang == 'zh' else 'Model Run'}</th></tr>
              </thead>
              <tbody>{signal_rows}</tbody>
            </table>
          </section>
          <script>
            const chip = (label, value) => {{
              const normalized = String(value || "-").toUpperCase();
              let bg = "#f3f4f6";
              let fg = "#374151";
              if (normalized.includes("STRONG_BUY") || normalized === "BUY" || normalized.includes("BULLISH")) {{
                bg = "#dcfce7"; fg = "#166534";
              }} else if (normalized.includes("STRONG_SELL") || normalized === "SELL" || normalized.includes("BEARISH")) {{
                bg = "#fee2e2"; fg = "#991b1b";
              }} else if (normalized.includes("NEUTRAL") || normalized.includes("MIXED")) {{
                bg = "#fef3c7"; fg = "#92400e";
              }}
              return `<span style="display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;background:${{bg}};color:${{fg}};font-weight:700;font-size:12px;">${{label}} ${{normalized}}</span>`;
            }};

            fetch("/symbols/{overview['ticker']}/page-bundle")
              .then((response) => response.ok ? response.json() : Promise.reject(new Error("Failed to load symbol bundle")))
              .then((payload) => {{
                const combined = payload.combined || {{}};
                const decision = payload.decision_brief || {{}};
                const news = payload.news_brief || {{}};
                const feed = payload.news_feed || [];
                const technical = combined.technical_rating || {{}};
                const mtf = combined.multi_timeframe || {{}};
                const bollinger = combined.bollinger_band || {{}};
                const candles = combined.candlestick_patterns || {{}};

                document.getElementById("combined-meta").textContent = `Confidence: ${{combined.confidence ?? "-"}} | Score: ${{combined.score ?? "-"}}`;
                document.getElementById("combined-chips").innerHTML = [
                  chip("1D", technical.recommendation || "-"),
                  chip("MTF", mtf.alignment || "mixed"),
                  chip("BB", bollinger.signal || "neutral"),
                ].join("");
                document.getElementById("combined-reasons").textContent = (combined.reasons || []).join(" · ") || "No confluence reasons yet";

                document.getElementById("decision-headline").textContent = decision.headline || "-";
                document.getElementById("decision-summary").textContent = decision.summary || "-";
                document.getElementById("decision-chips").innerHTML = [
                  chip("Sentiment", decision.sentiment || "neutral"),
                  chip("Urgency", decision.urgency || "normal"),
                ].join("");

                document.getElementById("mtf-summary").textContent = `Alignment: ${{mtf.alignment || "-"}} | Bullish: ${{mtf.bullish_count ?? "-"}} | Bearish: ${{mtf.bearish_count ?? "-"}} | Neutral: ${{mtf.neutral_count ?? "-"}}`;
                document.getElementById("mtf-chips").innerHTML = Object.entries(mtf.ratings || {{}})
                  .map(([interval, value]) => chip(interval.toUpperCase(), (value || {{}}).recommendation || "-"))
                  .join("");

                document.getElementById("bollinger-summary").textContent =
                  `Rating: ${{bollinger.rating ?? "-"}} | Signal: ${{bollinger.signal || "-"}} | Bandwidth: ${{bollinger.bandwidth_pct ?? "-"}}% | Position: ${{bollinger.band_position_pct ?? "-"}}% | Squeeze: ${{bollinger.squeeze ? "Yes" : "No"}}`;
                document.getElementById("candlestick-summary").textContent =
                  (candles.patterns || []).join(", ") || "No active candlestick pattern";

                document.getElementById("news-headline").textContent =
                  (news.headlines || [])[0] || "-";
                document.getElementById("news-summary").textContent = news.summary || "-";
                document.getElementById("news-chips").innerHTML = [
                  chip("Tone", news.sentiment || "neutral"),
                  chip("Urgency", news.urgency || "normal"),
                ].join("");
                document.getElementById("news-feed").innerHTML = feed.length
                  ? feed.map((item) => `<div style="margin-top:8px;"><a href="${{item.link || '#'}}" target="_blank" rel="noreferrer">${{item.title || '-'}}</a><div class="muted" style="font-size:12px;margin-top:4px;">${{item.source || '-' }}${{item.published_at ? ' · ' + item.published_at : ''}}</div></div>`).join("")
                  : "<div class='muted' style='margin-top:8px;'>No external headlines available.</div>";
              }})
              .catch(() => {{
                document.getElementById("combined-reasons").textContent = "Detailed analysis is temporarily unavailable.";
                document.getElementById("news-feed").textContent = "External headlines are temporarily unavailable.";
              }});

            const loadAiButton = document.getElementById("load-ai-analysis");
            if (loadAiButton) {{
              loadAiButton.addEventListener("click", () => {{
                loadAiButton.disabled = true;
                loadAiButton.textContent = "{'正在加载...' if lang == 'zh' else 'Loading...'}";
                fetch("/symbols/{overview['ticker']}/ai-analysis?lang={lang}")
                  .then((response) => response.ok ? response.json() : Promise.reject(new Error("Failed to load ai analysis")))
                  .then((payload) => {{
                    document.getElementById("ai-headline").textContent = payload.headline || "-";
                    document.getElementById("ai-summary").textContent = payload.summary || "-";
                    document.getElementById("ai-chips").innerHTML = [
                      chip("Verdict", payload.verdict || "hold"),
                      chip("Confidence", String(payload.confidence ?? "-")),
                      chip("Source", payload.source || "local"),
                    ].join("");
                    const buyZone = payload.buy_zone || {{}};
                    const takeProfit = payload.take_profit || {{}};
                    const stopLoss = payload.stop_loss ?? "-";
                    document.getElementById("ai-levels").textContent =
                      `Strategy: ${{payload.strategy || "-"}} | Buy: ${{buyZone.low ?? "-"}} - ${{buyZone.high ?? "-"}} | Stop: ${{stopLoss}} | Take Profit: ${{takeProfit.low ?? "-"}} - ${{takeProfit.high ?? "-"}}`;
                    const checklist = payload.checklist || [];
                    document.getElementById("ai-checklist").innerHTML = checklist.length
                      ? checklist.map((item) => `<div style="margin-top:6px;">${{item.status === 'pass' ? 'PASS' : 'WATCH'}} · ${{item.label}}</div>`).join("")
                      : "No checklist available.";
                    loadAiButton.textContent = "{'已加载 AI 分析' if lang == 'zh' else 'AI Analysis Loaded'}";
                  }})
                  .catch(() => {{
                    document.getElementById("ai-headline").textContent = "AI analysis is temporarily unavailable.";
                    loadAiButton.disabled = false;
                    loadAiButton.textContent = "{'重试加载 AI 分析' if lang == 'zh' else 'Retry AI Analysis'}";
                  }});
              }});
            }}
          </script>
            </div>
          </main>
        </div>
      </body>
    </html>
    """
