from __future__ import annotations

from app.core.db import SessionLocal
from app.services.market_freshness import latest_completed_market_date
from app.services.repository import MarketRefreshBatchRepository


def record_market_refresh_result(*, source_job_id: int, result: dict) -> dict | None:
    """Attach one idempotent audit batch to a market-refresh Job result.

    Refresh can be triggered from the API, a scheduler, or a recovery script.
    Keeping this small adapter outside those entry points makes every real
    execution leave the same trace in ``market_refresh_batches``.
    """

    if not isinstance(result, dict):
        return None
    existing_id = result.get("refresh_batch_id")
    if existing_id:
        return {"id": int(existing_id)}

    market = str(result.get("market") or "").strip().upper()
    if market not in {"CN", "US"}:
        return None
    if not any(
        result.get(key)
        for key in ("trade_date", "required_as_of_date", "rows_returned", "rows_written", "provider_used")
    ):
        return None

    requested_as_of_date = str(
        result.get("requested_as_of_date")
        or result.get("required_as_of_date")
        or result.get("trade_date")
        or latest_completed_market_date(market)
    )[:10]
    provider = str(
        result.get("provider_used")
        or result.get("source")
        or result.get("provider")
        or "unknown"
    )
    with SessionLocal() as db:
        batch = MarketRefreshBatchRepository(db).record_result(
            source_job_id=source_job_id,
            market=market,
            provider=provider,
            requested_as_of_date=requested_as_of_date,
            result=result,
        )

    result["refresh_batch_id"] = batch["id"]
    result.setdefault(
        "quality_summary",
        {
            "market": market,
            "requested_as_of_date": requested_as_of_date,
            "actual_as_of_date": batch.get("actual_as_of_date"),
            "success_count": batch.get("success_count", 0),
            "no_trade_count": batch.get("no_trade_count", 0),
            "inactive_count": batch.get("inactive_count", 0),
            "partial_count": batch.get("partial_count", 0),
            "missing_count": batch.get("missing_count", 0),
            "failed_count": batch.get("failed_count", 0),
            "refresh_batch_id": batch["id"],
        },
    )
    return batch
