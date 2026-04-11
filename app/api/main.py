from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.api.routes import auth, backtests, dashboard, insights, jobs, portfolio, screener, settings as settings_routes, signals, symbols, watchlist
from app.core.config import get_settings
from app.core.db import init_db
from app.services.auto_analysis import auto_analysis_service
from app.services.close_review_scheduler import close_review_scheduler_service


settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    auto_analysis_service.start()
    close_review_scheduler_service.start()
    yield
    auto_analysis_service.stop()
    close_review_scheduler_service.stop()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Local-first personal finance analysis tool powered by OpenBB and Qlib.",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(symbols.router)
app.include_router(insights.router)
app.include_router(watchlist.router)
app.include_router(portfolio.router)
app.include_router(settings_routes.router)
app.include_router(screener.router)
app.include_router(signals.router)
app.include_router(backtests.router)
app.include_router(jobs.router)
app.include_router(dashboard.router)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
