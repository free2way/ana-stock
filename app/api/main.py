from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
import time
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, text

from app.api.routes import ai_chat, auth, backtests, dashboard, insights, jobs, portfolio, review_journal, screener, settings as settings_routes, signals, social_signals, symbols, watchlist
from app.core.config import get_settings
from app.core.db import init_db
from app.services.auth import is_authenticated, login_redirect
from app.services.auto_analysis import auto_analysis_service
from app.services.cn_market_universe import _is_supported_cn_symbol
from app.services.cn_market_scheduler import cn_market_scheduler_service
from app.services.social_signal_scheduler import social_signal_scheduler_service
from app.services.storage_capacity_scheduler import storage_capacity_scheduler_service
from app.services.ui_lang import LANG_COOKIE_NAME
from app.services.us_market_scheduler import us_market_scheduler_service
from app.services.us_symbol_metadata_scheduler import us_symbol_metadata_scheduler_service
from app.services.market_freshness import latest_completed_market_date
from app.services.market_lake import (
    get_latest_lake_trade_date,
    list_lake_symbols_for_trade_date,
)
from app.services.storage_capacity import storage_capacity_report
from app.services.app_setting_storage import app_setting_storage_report


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
    storage_capacity_scheduler_service.start()
    yield
    storage_capacity_scheduler_service.stop()
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


# Paths reachable without a session. Everything else -- including every JSON
# and form-POST endpoint -- is redirected to /login by the middleware below,
# so a new route file can no longer silently ship an unauthenticated mutation.
PUBLIC_AUTH_PATHS = {"/login", "/health", "/health/ready"}


@app.middleware("http")
async def enforce_authentication(request: Request, call_next):
    # Registered after persist_language_preference, so this runs first
    # (outermost) and unauthenticated requests never reach a handler.
    path = request.url.path
    if path in PUBLIC_AUTH_PATHS or is_authenticated(request):
        return await call_next(request)
    next_path = path
    if request.url.query:
        next_path = f"{path}?{request.url.query}"
    return login_redirect(next_path)


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


MARKET_READINESS_ABNORMAL_LIMIT = 15


def _symbol_readiness_summary(quality: dict) -> dict:
    raw_status = str(quality.get("symbol_state_status") or "missing")
    stale_count = int(quality.get("stale_count") or 0)
    missing_count = int(quality.get("missing_count") or 0)
    fresh_count = int(quality.get("fresh_count") or 0)
    accounted_count = int(quality.get("accounted_symbol_count", fresh_count) or 0)
    raw_abnormal_count = stale_count + missing_count
    abnormal_count = int(quality.get("blocking_anomaly_count", raw_abnormal_count) or 0)
    tolerated = (
        raw_status in {"partial", "stale"}
        and accounted_count > 0
        and abnormal_count <= MARKET_READINESS_ABNORMAL_LIMIT
    )
    effective_status = (
        "fresh"
        if raw_status == "fresh"
        else "fresh_with_tolerance"
        if tolerated
        else raw_status
    )
    readiness_status = (
        "ok"
        if effective_status in {"fresh", "fresh_with_tolerance"}
        else "degraded"
        if effective_status in {"partial", "stale", "missing"}
        else effective_status
    )
    return {
        "raw_status": raw_status,
        "effective_status": effective_status,
        "readiness_status": readiness_status,
        "abnormal_count": abnormal_count,
        "raw_abnormal_count": raw_abnormal_count,
        "accounted_count": accounted_count,
        "abnormal_limit": MARKET_READINESS_ABNORMAL_LIMIT,
        "tolerated": tolerated,
    }


def _prediction_cold_storage_summary() -> dict:
    cold_root = settings.artifacts_dir / "prediction_runs"
    if not settings.prediction_cold_reads_enabled:
        return {
            "status": "degraded",
            "mode": "postgresql_only_rollback",
            "read_enabled": False,
            "artifact_root": str(cold_root),
        }
    available = cold_root.is_dir()
    return {
        "status": "ok" if available else "degraded",
        "mode": "hot_cold",
        "read_enabled": True,
        "artifact_root": str(cold_root),
        "artifact_root_available": available,
    }


def _storage_capacity_readiness_summary(
    report: dict | None,
    *,
    enabled: bool,
    today: date | None = None,
) -> dict:
    if not enabled:
        return {"status": "ok", "mode": "disabled"}
    latest = (report or {}).get("latest_sample")
    if not latest or not latest.get("sample_date"):
        return {
            "status": "degraded",
            "mode": "enabled",
            "message": "No PostgreSQL capacity sample is available.",
        }
    current_date = today or date.today()
    sample_date = date.fromisoformat(str(latest["sample_date"])[:10])
    age_days = (current_date - sample_date).days
    growth_status = str((report or {}).get("status") or "collecting")
    status = (
        "stale"
        if age_days > 1
        else "degraded"
        if growth_status == "failed"
        else "ok"
    )
    return {
        "status": status,
        "mode": "enabled",
        "latest_sample_date": sample_date.isoformat(),
        "sample_age_days": age_days,
        "growth_status": growth_status,
        "sample_count": int((report or {}).get("sample_count") or 0),
        "required_intervals": int((report or {}).get("required_intervals") or 5),
        "average_daily_growth_bytes": (report or {}).get(
            "average_daily_growth_bytes"
        ),
        "limit_bytes": (report or {}).get("limit_bytes"),
    }


def _app_setting_storage_readiness_summary(report: dict | None) -> dict:
    if report is None:
        return {
            "status": "degraded",
            "message": "App setting storage could not be inspected.",
        }
    oversized = int(report.get("oversized_inline_count") or 0)
    return {
        "status": "ok" if oversized == 0 else "degraded",
        "threshold_bytes": int(report.get("threshold_bytes") or 0),
        "setting_count": int(report.get("setting_count") or 0),
        "oversized_inline_count": oversized,
        "maximum_inline_bytes": int(report.get("maximum_inline_bytes") or 0),
    }


def _market_health_ticker_filters(
    *, cn_supported_tickers: set[str] | None = None,
) -> tuple[dict[str, set[str]] | None, dict[str, str]]:
    """Scope US readiness to the provider's latest grouped-daily cohort.

    The US symbol registry is historical and intentionally retains renamed and
    delisted identifiers.  Treating that registry as today's expected provider
    response makes a successful grouped-daily refresh look permanently broken.
    CN uses the exact exchange universe supported by its price refresh.
    Registry-only BSE symbols are not counted as missing price bars when the
    current refresh explicitly excludes BSE; they remain a separate coverage
    gap, not provider-confirmed no-trade exceptions.
    """

    filters = {"CN": cn_supported_tickers} if cn_supported_tickers is not None else {}
    scopes = {
        "CN": "supported_price_refresh_universe" if cn_supported_tickers is not None else "exchange_registry",
    }
    try:
        latest_us_date = get_latest_lake_trade_date(market="US")
        if not latest_us_date:
            return filters or None, scopes
        latest_us_symbols = list_lake_symbols_for_trade_date(
            market="US",
            trade_date=latest_us_date,
        )
        if not latest_us_symbols:
            return filters or None, scopes
        filters["US"] = latest_us_symbols
        scopes["US"] = "latest_lake_partition"
        return filters, scopes
    except Exception:
        return filters or None, scopes


def _collect_readiness_checks() -> dict[str, dict]:
    """Build the full dependency readiness report, including diagnostics.

    The detailed report intentionally carries exception text and artifact paths,
    so it may only be served from the authenticated diagnostics endpoint; the
    public ``/health/ready`` probe returns a sanitized summary instead.
    """
    checks: dict[str, dict] = {}
    market_freshness: dict = {}
    capacity_report: dict | None = None
    setting_storage_report: dict | None = None
    ticker_filters, freshness_scopes = None, {}
    try:
        from app.core.db import SessionLocal
        from app.models.tables import Symbol
        from app.services.repository import PriceSyncStateRepository

        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            cn_supported_tickers = {
                ticker for ticker, exchange in db.execute(
                    select(Symbol.ticker, Symbol.exchange).where(Symbol.market == "CN")
                ).all()
                if _is_supported_cn_symbol(ticker=ticker, exchange=exchange)
            }
            ticker_filters, freshness_scopes = _market_health_ticker_filters(
                cn_supported_tickers=cn_supported_tickers,
            )
            market_freshness = PriceSyncStateRepository(db).get_market_freshness_overview(
                tickers_by_market=ticker_filters,
            )
            capacity_report = storage_capacity_report(db)
            setting_storage_report = app_setting_storage_report(
                db,
                max_inline_bytes=settings.app_setting_inline_value_max_bytes,
            )
        checks["database"] = {"status": "ok"}
    except Exception as exc:
        checks["database"] = {"status": "failed", "message": str(exc)}

    checks["prediction_cold_storage"] = _prediction_cold_storage_summary()
    checks["storage_capacity_monitor"] = _storage_capacity_readiness_summary(
        capacity_report,
        enabled=settings.storage_capacity_monitor_enabled,
    )
    checks["app_setting_storage"] = _app_setting_storage_readiness_summary(
        setting_storage_report
    )

    for market in ("CN", "US"):
        try:
            expected = latest_completed_market_date(market)
            latest = get_latest_lake_trade_date(market=market)
            quality = (market_freshness or {}).get(market, {})
            lake_status = "ok" if latest and latest >= expected else "stale" if latest else "missing"
            symbol_readiness = _symbol_readiness_summary(quality)
            symbol_status = symbol_readiness["effective_status"]
            symbol_quality_status = symbol_readiness["readiness_status"]
            check_status = lake_status if lake_status in {"missing", "stale"} else symbol_quality_status
            checks[f"lake_{market.lower()}"] = {
                "status": check_status,
                "latest_as_of_date": latest,
                "expected_as_of_date": expected,
                "symbol_state_status": symbol_status,
                "symbol_state_scope": freshness_scopes.get(market, "symbol_registry"),
                "symbol_state_raw_status": symbol_readiness["raw_status"],
                "symbol_state_abnormal": symbol_readiness["abnormal_count"],
                "symbol_state_abnormal_limit": symbol_readiness["abnormal_limit"],
                "symbol_state_tolerated": symbol_readiness["tolerated"],
                "symbol_state_total": quality.get("total_count", 0),
                "symbol_state_fresh": quality.get("fresh_count", 0),
                "symbol_state_stale": quality.get("stale_count", 0),
                "symbol_state_missing": quality.get("missing_count", 0),
                "symbol_state_no_trade": quality.get("no_trade_count", 0),
                "symbol_state_inactive": quality.get("inactive_count", 0),
                "symbol_state_manual_approved": quality.get("manual_approved_count", 0),
                "symbol_state_raw_abnormal": symbol_readiness["raw_abnormal_count"],
                "symbol_anomaly_classification": quality.get("anomaly_classification") or {},
            }
        except Exception as exc:
            checks[f"lake_{market.lower()}"] = {"status": "failed", "message": str(exc)}

    return checks


def _readiness_overall(checks: dict[str, dict]) -> tuple[str, list[str], list[str], list[str]]:
    failed = [key for key, value in checks.items() if value.get("status") in {"failed", "missing"}]
    stale = [key for key, value in checks.items() if value.get("status") == "stale"]
    degraded = [key for key, value in checks.items() if value.get("status") == "degraded"]
    overall = "failed" if failed else "degraded" if stale or degraded else "ready"
    return overall, failed, stale, degraded


def _unsatisfiable_readiness_checks(checks: dict[str, dict]) -> list[str]:
    """Checks whose probe itself errored (a hard dependency is unavailable).

    ``missing``/``stale`` market-lake states are data-freshness signals that the
    endpoint reports but never turns into a 503; only an errored probe (the
    database is unreachable, or a check raised) means the service cannot serve.
    """
    return [key for key, value in checks.items() if value.get("status") == "failed"]


def _public_readiness_summary(
    checks: dict[str, dict],
    *,
    overall: str,
    failed: list[str],
    stale: list[str],
    degraded: list[str],
) -> dict:
    """Sanitized readiness body: statuses only, never messages or paths."""
    return {
        "status": overall,
        "checks": {key: {"status": value.get("status")} for key, value in checks.items()},
        "failed": failed,
        "stale": stale,
        "degraded": degraded,
    }


@app.get("/health/ready")
def readiness() -> JSONResponse:
    """Report dependency readiness, not merely whether the web process is alive."""
    checks = _collect_readiness_checks()
    overall, failed, stale, degraded = _readiness_overall(checks)
    unsatisfiable = _unsatisfiable_readiness_checks(checks)
    if unsatisfiable:
        # Detailed diagnostics stay in the log and behind authentication.
        logger.warning(
            "readiness probe failed for %s: %s",
            unsatisfiable,
            {key: checks[key] for key in unsatisfiable},
        )
    return JSONResponse(
        status_code=503 if unsatisfiable else 200,
        content=_public_readiness_summary(
            checks, overall=overall, failed=failed, stale=stale, degraded=degraded
        ),
    )


@app.get("/health/ready/diagnostics")
def readiness_diagnostics() -> dict:
    """Authenticated dependency diagnostics: exception text and artifact paths."""
    checks = _collect_readiness_checks()
    overall, failed, stale, degraded = _readiness_overall(checks)
    return {
        "status": overall,
        "checks": checks,
        "failed": failed,
        "stale": stale,
        "degraded": degraded,
    }
