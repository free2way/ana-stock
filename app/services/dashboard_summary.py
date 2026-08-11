import json

from sqlalchemy.orm import Session

from app.services.auto_analysis import auto_analysis_service
from app.services.repository import (
    DashboardReadRepository,
)
from app.services.runtime_cache import get_or_set
from app.services.time_utils import app_now_iso


def build_data_sources(sync_states: list[dict], concept_summary: dict | None = None) -> dict:
    counts: dict[str, int] = {}
    for item in sync_states:
        provider = item.get("provider") or "unknown"
        counts[provider] = counts.get(provider, 0) + 1
    breakdown = [
        {"provider": provider, "count": count}
        for provider, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    primary_provider = breakdown[0]["provider"] if breakdown else None
    return {
        "historical_price_strategy": [
            "Try OpenBB first",
            "Fallback to yfinance if OpenBB is unavailable or fails",
            "Persist locally into raw and normalized files before analysis",
        ],
        "symbol_profile_strategy": [
            "Try OpenBB company profile first",
            "Fallback to yfinance profile if needed",
            "Fallback to local catalog only when live profile data is unavailable",
        ],
        "concept_strategy": [
            "Use TuShare concept membership data for A-share concept mapping",
            "Track Top-N model concentration by concept over recent snapshots",
            "Treat stale concept data as lower confidence for resonance analysis",
        ],
        "supplemental_source_strategy": [
            "A 股：a-stock-data 的腾讯行情仅用于指定股票的补全与交叉核验，主行情湖仍由 TuShare 写入。",
            "美股：global-stock-data 的 SEC EDGAR 仅写入官方申报基本面，主行情湖仍由 Polygon 写入。",
            "补充数据保留来源和截至日期，且不会将不同复权口径的价格直接合并。",
        ],
        "current_provider_breakdown": breakdown,
        "primary_provider": primary_provider,
        "concept_data": concept_summary
        or {
            "latest_as_of_date": None,
            "concept_count": 0,
            "symbol_count": 0,
            "freshness": "missing",
        },
    }


def load_dashboard_summary(
    db: Session,
    *,
    lookback_runs: int,
    market_context_loader,
) -> dict:
    cache_key = json.dumps({"lookback_runs": lookback_runs}, sort_keys=True)

    def _load() -> dict:
        snapshot = DashboardReadRepository(db).load_summary_snapshot()
        latest_signals = snapshot["latest_signals"]
        sync_states = snapshot["sync_states"]
        return {
            "generated_at": app_now_iso(),
            "lookback_runs": lookback_runs,
            "auto_analysis": auto_analysis_service.get_status(db=db),
            "data_sources": build_data_sources(
                sync_states,
                snapshot["concept_summary"],
            ),
            "latest_model": snapshot["latest_model"],
            "recent_model_runs": snapshot["recent_model_runs"],
            "latest_signals": latest_signals,
            "latest_backtest": snapshot["latest_backtest"],
            "latest_backtest_curve": snapshot["latest_backtest_curve"],
            "sync_states": sync_states,
            "recent_jobs": snapshot["recent_jobs"],
            "market_context": market_context_loader(latest_signals),
        }

    return get_or_set("dashboard_summary", cache_key, ttl_seconds=20.0, loader=_load)


def load_recent_jobs_summary(db: Session, *, limit: int = 20) -> list[dict]:
    cache_key = json.dumps({"limit": limit}, sort_keys=True)

    def _load() -> list[dict]:
        return DashboardReadRepository(db).load_summary_snapshot()["recent_jobs"][:limit]

    return get_or_set("dashboard_recent_jobs", cache_key, ttl_seconds=10.0, loader=_load)
