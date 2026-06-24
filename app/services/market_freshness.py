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


def summarize_market_freshness(
    states: list[dict],
    *,
    market: str,
    expected_as_of_date: str | None = None,
) -> dict:
    market_code = normalize_market(market)
    target = expected_as_of_date or latest_completed_market_date(market_code)
    relevant = [row for row in states if normalize_market(row.get("market")) == market_code]
    dates = [str(row.get("last_synced_date") or "").strip()[:10] for row in relevant]
    valid_dates = [value for value in dates if value]
    fresh_count = sum(1 for value in valid_dates if value >= target)
    missing_count = len(relevant) - len(valid_dates)
    stale_count = len(relevant) - fresh_count - missing_count
    if not relevant:
        status = "missing"
    elif fresh_count == len(relevant):
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
        "status": status,
    }
