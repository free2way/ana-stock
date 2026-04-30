from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


CN_MARKET_HOLIDAYS_2026 = {
    "2026-01-01",
    "2026-01-02",
    "2026-01-03",
    "2026-02-15",
    "2026-02-16",
    "2026-02-17",
    "2026-02-18",
    "2026-02-19",
    "2026-02-20",
    "2026-02-21",
    "2026-02-22",
    "2026-02-23",
    "2026-04-04",
    "2026-04-05",
    "2026-04-06",
    "2026-05-01",
    "2026-05-02",
    "2026-05-03",
    "2026-05-04",
    "2026-05-05",
    "2026-06-19",
    "2026-06-20",
    "2026-06-21",
    "2026-09-25",
    "2026-09-26",
    "2026-09-27",
    "2026-10-01",
    "2026-10-02",
    "2026-10-03",
    "2026-10-04",
    "2026-10-05",
    "2026-10-06",
    "2026-10-07",
}

US_MARKET_HOLIDAYS_2026 = {
    "2026-01-01",
    "2026-01-19",
    "2026-02-16",
    "2026-04-03",
    "2026-05-25",
    "2026-06-19",
    "2026-07-03",
    "2026-09-07",
    "2026-11-26",
    "2026-12-25",
}

US_MARKET_EARLY_CLOSES_2026 = {
    "2026-11-27": "13:00",
    "2026-12-24": "13:00",
}

MARKET_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "US": "America/New_York",
}


def _to_date(value: str | date | datetime | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def normalize_market(market: str | None) -> str:
    value = str(market or "").strip().upper()
    if value in {"A", "A股", "CHINA", "CN", "SH", "SZ", "SS"}:
        return "CN"
    if value in {"AMERICA", "NASDAQ", "NYSE", "USA", "US"}:
        return "US"
    return value or "CN"


def market_timezone(market: str | None) -> ZoneInfo:
    return ZoneInfo(MARKET_TIMEZONES.get(normalize_market(market), "Asia/Shanghai"))


def is_market_open_date(market: str | None, value: str | date | datetime | None) -> bool:
    market_code = normalize_market(market)
    day = _to_date(value)
    day_iso = day.isoformat()
    if day.weekday() >= 5:
        return False
    if market_code == "CN":
        return day_iso not in CN_MARKET_HOLIDAYS_2026
    if market_code == "US":
        return day_iso not in US_MARKET_HOLIDAYS_2026
    return day.weekday() < 5


def next_market_open_date(market: str | None, value: str | date | datetime | None, *, include_self: bool = True) -> str:
    day = _to_date(value)
    if not include_self:
        day += timedelta(days=1)
    for _ in range(370):
        if is_market_open_date(market, day):
            return day.isoformat()
        day += timedelta(days=1)
    return day.isoformat()


def previous_market_open_date(market: str | None, value: str | date | datetime | None, *, include_self: bool = True) -> str:
    day = _to_date(value)
    if not include_self:
        day -= timedelta(days=1)
    for _ in range(370):
        if is_market_open_date(market, day):
            return day.isoformat()
        day -= timedelta(days=1)
    return day.isoformat()


def market_session_status(market: str | None, value: str | date | datetime | None) -> dict:
    market_code = normalize_market(market)
    day = _to_date(value)
    day_iso = day.isoformat()
    is_open = is_market_open_date(market_code, day)
    early_close = US_MARKET_EARLY_CLOSES_2026.get(day_iso) if market_code == "US" and is_open else None
    if is_open:
        reason = "regular_session"
        if early_close:
            reason = "early_close"
    elif day.weekday() >= 5:
        reason = "weekend"
    else:
        reason = "holiday"
    return {
        "market": market_code,
        "date": day_iso,
        "is_open": is_open,
        "reason": reason,
        "timezone": str(market_timezone(market_code)),
        "regular_open": "09:30",
        "regular_close": early_close or ("15:00" if market_code == "CN" else "16:00"),
        "early_close": early_close,
        "previous_open_date": previous_market_open_date(market_code, day, include_self=False),
        "next_open_date": next_market_open_date(market_code, day, include_self=False),
    }
