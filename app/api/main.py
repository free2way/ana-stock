from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text

from app.api.routes import ai_chat, auth, backtests, dashboard, insights, jobs, portfolio, review_journal, screener, settings as settings_routes, signals, social_signals, symbols, watchlist
from app.core.config import get_settings
from app.core.db import init_db
from app.services.auto_analysis import auto_analysis_service
from app.services.cn_market_scheduler import cn_market_scheduler_service
from app.services.social_signal_scheduler import social_signal_scheduler_service
from app.services.ui_lang import LANG_COOKIE_NAME
from app.services.us_market_scheduler import us_market_scheduler_service
from app.services.us_symbol_metadata_scheduler import us_symbol_metadata_scheduler_service
from app.services.market_freshness import latest_completed_market_date
from app.services.market_lake import get_latest_lake_trade_date


settings = get_settings()
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    auto_analysis_service.start()
    cn_market_scheduler_service.start()
    us_market_scheduler_service.start()
    us_symbol_metadata_scheduler_service.start()
    social_signal_scheduler_service.start()
    yield
    social_signal_scheduler_service.stop()
    us_symbol_metadata_scheduler_service.stop()
    us_market_scheduler_service.stop()
    cn_market_scheduler_service.stop()
    auto_analysis_service.stop()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Local-first personal finance analysis tool powered by OpenBB and Qlib.",
    lifespan=lifespan,
)


@app.middleware("http")
async def persist_language_preference(request: Request, call_next):
    started_at = time.perf_counter()
    response = await call_next(request)
    lang = request.query_params.get("lang")
    if lang in {"en", "zh"}:
        response.set_cookie(
            LANG_COOKIE_NAME,
            lang,
            httponly=False,
            samesite="lax",
            max_age=60 * 60 * 24 * 365,
        )
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    line = f"REQ {request.method} {path} status={getattr(response, 'status_code', '-')} duration_ms={elapsed_ms:.1f}"
    logger.info(line)
    print(line, flush=True)
    return response

app.include_router(auth.router)
app.include_router(symbols.router)
app.include_router(insights.router)
app.include_router(watchlist.router)
app.include_router(portfolio.router)
app.include_router(settings_routes.router)
app.include_router(ai_chat.router)
app.include_router(review_journal.router)
app.include_router(screener.router)
app.include_router(signals.router)
app.include_router(social_signals.router)
app.include_router(backtests.router)
app.include_router(jobs.router)
app.include_router(dashboard.router)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict:
    """Report dependency readiness, not merely whether the web process is alive."""
    checks: dict[str, dict] = {}
    market_freshness: dict = {}
    try:
        from app.core.db import SessionLocal
        from app.services.repository import PriceSyncStateRepository

        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            market_freshness = PriceSyncStateRepository(db).get_market_freshness_overview()
        checks["database"] = {"status": "ok"}
    except Exception as exc:
        checks["database"] = {"status": "failed", "message": str(exc)}

    for market in ("CN", "US"):
        try:
            expected = latest_completed_market_date(market)
            latest = get_latest_lake_trade_date(market=market)
            quality = (market_freshness or {}).get(market, {})
            lake_status = "ok" if latest and latest >= expected else "stale" if latest else "missing"
            symbol_status = str(quality.get("symbol_state_status") or "missing")
            symbol_quality_status = (
                "ok"
                if symbol_status == "fresh"
                else "degraded"
                if symbol_status in {"partial", "stale", "missing"}
                else symbol_status
            )
            check_status = lake_status if lake_status in {"missing", "stale"} else symbol_quality_status
            checks[f"lake_{market.lower()}"] = {
                "status": check_status,
                "latest_as_of_date": latest,
                "expected_as_of_date": expected,
                "symbol_state_status": symbol_status,
                "symbol_state_total": quality.get("total_count", 0),
                "symbol_state_fresh": quality.get("fresh_count", 0),
                "symbol_state_stale": quality.get("stale_count", 0),
                "symbol_state_missing": quality.get("missing_count", 0),
                "symbol_state_no_trade": quality.get("no_trade_count", 0),
                "symbol_state_inactive": quality.get("inactive_count", 0),
                "symbol_state_manual_approved": quality.get("manual_approved_count", 0),
            }
        except Exception as exc:
            checks[f"lake_{market.lower()}"] = {"status": "failed", "message": str(exc)}

    failed = [key for key, value in checks.items() if value.get("status") in {"failed", "missing"}]
    stale = [key for key, value in checks.items() if value.get("status") == "stale"]
    degraded = [key for key, value in checks.items() if value.get("status") == "degraded"]
    overall = "failed" if failed else "degraded" if stale or degraded else "ready"
    return {"status": overall, "checks": checks, "failed": failed, "stale": stale, "degraded": degraded}
