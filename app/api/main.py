from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from app.api.routes import ai_chat, auth, backtests, dashboard, insights, jobs, portfolio, review_journal, screener, settings as settings_routes, signals, social_signals, symbols, watchlist
from app.core.config import get_settings
from app.core.db import init_db
from app.services.auto_analysis import auto_analysis_service
from app.services.close_review_scheduler import close_review_scheduler_service
from app.services.social_signal_scheduler import social_signal_scheduler_service
from app.services.ui_lang import LANG_COOKIE_NAME
from app.services.us_market_scheduler import us_market_scheduler_service
from app.services.us_symbol_metadata_scheduler import us_symbol_metadata_scheduler_service


settings = get_settings()
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    auto_analysis_service.start()
    close_review_scheduler_service.start()
    us_market_scheduler_service.start()
    us_symbol_metadata_scheduler_service.start()
    social_signal_scheduler_service.start()
    yield
    social_signal_scheduler_service.stop()
    us_symbol_metadata_scheduler_service.stop()
    us_market_scheduler_service.stop()
    auto_analysis_service.stop()
    close_review_scheduler_service.stop()

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
