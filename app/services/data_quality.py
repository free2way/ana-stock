from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.repository import PriceSyncStateRepository


def market_data_gate(
    db: Session,
    *,
    market: str,
    tickers: list[str] | set[str] | None = None,
) -> dict:
    """Return the input-data gate used by downstream model jobs.

    Explicit ``no_trade``/``inactive`` symbols are accepted as accounted-for;
    stale or missing eligible symbols block a model run.  ``unknown`` is kept
    non-blocking for isolated test/import flows where no sync state exists.
    """

    market_code = str(market or "").strip().upper()
    ticker_filter = None
    if tickers:
        ticker_filter = {str(ticker or "").strip().upper() for ticker in tickers if str(ticker or "").strip()}
    overview = PriceSyncStateRepository(db).get_market_freshness_overview(
        markets=(market_code,),
        tickers_by_market={market_code: ticker_filter} if ticker_filter is not None else None,
    ).get(market_code) or {}
    total = int(overview.get("total_count") or 0)
    blocking = int(overview.get("stale_count") or 0) + int(overview.get("missing_count") or 0)
    accounted = int(overview.get("fresh_count") or 0) + int(overview.get("exception_count") or 0)
    coverage = round(accounted / total, 6) if total else None
    if total == 0:
        status = "unknown"
    elif blocking:
        status = "blocked"
    else:
        status = "ready"
    return {
        "status": status,
        "market": market_code,
        "expected_as_of_date": overview.get("expected_as_of_date"),
        "latest_as_of_date": overview.get("authoritative_as_of_date"),
        "total_count": total,
        "fresh_count": int(overview.get("fresh_count") or 0),
        "no_trade_count": int(overview.get("no_trade_count") or 0),
        "inactive_count": int(overview.get("inactive_count") or 0),
        "manual_approved_count": int(overview.get("manual_approved_count") or 0),
        "stale_count": int(overview.get("stale_count") or 0),
        "missing_count": int(overview.get("missing_count") or 0),
        "coverage": coverage,
        "message": (
            f"{market_code} market input is ready ({accounted}/{total} accounted, "
            f"{int(overview.get('exception_count') or 0)} no-trade/inactive/manual-approved)."
            if status == "ready"
            else f"{market_code} market input is blocked: {blocking} stale/missing eligible symbol(s)."
            if status == "blocked"
            else f"{market_code} market input coverage is unknown because no sync state rows matched."
        ),
    }


def format_data_gate_failure(gate: dict) -> str:
    return str(gate.get("message") or "Market input data quality gate failed.")
