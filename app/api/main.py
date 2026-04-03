from fastapi import FastAPI

from app.api.routes import backtests, dashboard, jobs, signals, symbols
from app.core.config import get_settings


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Local-first personal finance analysis tool powered by OpenBB and Qlib.",
)

app.include_router(symbols.router)
app.include_router(signals.router)
app.include_router(backtests.router)
app.include_router(jobs.router)
app.include_router(dashboard.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
