from __future__ import annotations

from datetime import datetime, timedelta

from app.services.market_calendar import (
    is_market_open_date,
    market_session_status,
    market_timezone,
    normalize_market,
    previous_market_open_date,
)


def latest_completed_market_date(
    market: str | None,
    *,
    now: datetime | None = None,
    provider_delay_minutes: int = 60,
) -> str:
    """Return the latest trading date whose end-of-day data should be available.

    This deliberately measures the *market data date*, not the wall-clock time
    at which a sync job happened.  The small post-close delay avoids declaring
    a same-day EOD refresh stale before upstream providers publish their bars.
    """

    market_code = normalize_market(market)
    timezone = market_timezone(market_code)
    current = (now or datetime.now(timezone)).astimezone(timezone)
    today = current.date()
    if not is_market_open_date(market_code, today):
        return previous_market_open_date(market_code, today, include_self=False)

    session = market_session_status(market_code, today)
    close_hour, close_minute = (int(part) for part in str(session["regular_close"]).split(":", 1))
    available_after = current.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0) + timedelta(
        minutes=max(0, int(provider_delay_minutes))
    )
    if current >= available_after:
        return today.isoformat()
    return previous_market_open_date(market_code, today, include_self=False)


def is_as_of_current(last_synced_date: str | None, expected_as_of_date: str | None) -> bool:
    actual = str(last_synced_date or "").strip()[:10]
    expected = str(expected_as_of_date or "").strip()[:10]
    return bool(actual and (not expected or actual >= expected))


def is_snapshot_as_of_current(snapshot_date: str | None, market: str | None) -> bool:
    """Return whether a derived snapshot represents the latest completed session."""

    market_code = normalize_market(market)
    if market_code not in {"CN", "US"}:
        return True
    return is_as_of_current(snapshot_date, latest_completed_market_date(market_code))


def summarize_market_freshness(
    states: list[dict],
    *,
    market: str,
    expected_as_of_date: str | None = None,
) -> dict:
    market_code = normalize_market(market)
    target = expected_as_of_date or latest_completed_market_date(market_code)
    relevant = [row for row in states if normalize_market(row.get("market")) == market_code]
    # A manual approval is deliberately distinct from provider-confirmed
    # no-trade. It is only used when an operator has reviewed a data-source
    # gap and explicitly accepts the symbol as an excluded exception.
    exception_statuses = {"no_trade", "suspended", "inactive", "manual_approved"}
    dates = [str(row.get("last_synced_date") or "").strip()[:10] for row in relevant]
    statuses = [str(row.get("status") or "").strip().lower() for row in relevant]
    no_trade_count = sum(status in {"no_trade", "suspended"} for status in statuses)
    inactive_count = sum(status == "inactive" for status in statuses)
    manual_approved_count = sum(status == "manual_approved" for status in statuses)
    exception_count = no_trade_count + inactive_count + manual_approved_count
    eligible_indexes = [index for index, status in enumerate(statuses) if status not in exception_statuses]
    valid_dates = [dates[index] for index in eligible_indexes if dates[index]]
    fresh_count = sum(1 for value in valid_dates if value >= target)
    missing_count = sum(1 for index in eligible_indexes if not dates[index])
    stale_count = len(eligible_indexes) - fresh_count - missing_count
    if not relevant:
        status = "missing"
    elif fresh_count == len(eligible_indexes) and missing_count == 0 and stale_count == 0:
        status = "fresh"
    elif fresh_count:
        status = "partial"
    else:
        status = "stale"
    return {
        "market": market_code,
        "expected_as_of_date": target,
        "latest_as_of_date": max(valid_dates) if valid_dates else None,
        "oldest_as_of_date": min(valid_dates) if valid_dates else None,
        "total_count": len(relevant),
        "fresh_count": fresh_count,
        "stale_count": stale_count,
        "missing_count": missing_count,
        "eligible_count": len(eligible_indexes),
        "exception_count": exception_count,
        "no_trade_count": no_trade_count,
        "inactive_count": inactive_count,
        "manual_approved_count": manual_approved_count,
        "status": status,
    }
