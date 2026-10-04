from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Calendar policy (S-9)
#
# Engine: exchange_calendars (XNYS / XHKG / XSHG) where it has coverage:
#   XSHG covers through 2025-12-31, XHKG through 2027-09-30, XNYS through 2027-10-01.
# CN therefore carries a local holiday table: official State Council notices for
# 2025/2026 and a provisional table for 2027 (the official 2027 notice is not
# published yet - expected around Nov 2026; update the table then, and keep the
# `is_provisional` flag until then).
# ---------------------------------------------------------------------------

CN_MARKET_HOLIDAYS_2025 = {
    "2025-01-01",
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",
    "2025-02-01", "2025-02-02", "2025-02-03", "2025-02-04",
    "2025-04-04", "2025-04-05", "2025-04-06",
    "2025-05-01", "2025-05-02", "2025-05-03", "2025-05-04", "2025-05-05",
    "2025-05-31", "2025-06-01", "2025-06-02",
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-04",
    "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",
}

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

# Provisional: statutory holiday days only (《全国年节及纪念日放假办法》).
# The State Council's 2027 arrangement (incl. 调休 blocks) is not published yet.
CN_MARKET_HOLIDAYS_2027_PROVISIONAL = {
    "2027-01-01",
    "2027-02-05", "2027-02-08",  # 除夕/初三 weekdays (初一初二 are weekend)
    "2027-04-05",
    "2027-06-09",
    "2027-09-15",
    "2027-10-01",
}

CN_MARKET_HOLIDAYS_BY_YEAR: dict[int, set[str]] = {
    2025: CN_MARKET_HOLIDAYS_2025,
    2026: CN_MARKET_HOLIDAYS_2026,
    2027: CN_MARKET_HOLIDAYS_2027_PROVISIONAL,
}

PROVISIONAL_CN_YEARS = {2027}

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

CALENDAR_VERSION = "market_calendar_2027_v2"
ADJUSTMENT_VERSION = "raw_prices_with_actions_v1"

MARKET_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
}

CALENDAR_SYMBOLS = {"CN": "XSHG", "US": "XNYS", "HK": "XHKG"}

_DEFAULT_SESSIONS = {"CN": ("09:30", "15:00"), "US": ("09:30", "16:00"), "HK": ("09:30", "16:00")}

_CALENDAR_CACHE: dict[str, object] = {}


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
    if value in {"HK", "HKG", "HONGKONG", "HONG KONG", "港股", "香港"}:
        return "HK"
    return value or "CN"


def market_timezone(market: str | None) -> ZoneInfo:
    return ZoneInfo(MARKET_TIMEZONES.get(normalize_market(market), "Asia/Shanghai"))


def _calendar(market_code: str):
    symbol = CALENDAR_SYMBOLS.get(market_code)
    if not symbol:
        return None
    if symbol in _CALENDAR_CACHE:
        return _CALENDAR_CACHE[symbol]
    try:
        import exchange_calendars as xcals  # noqa: PLC0415 - optional heavy import

        calendar = xcals.get_calendar(symbol)
    except Exception:  # pragma: no cover - library unavailable
        calendar = None
    _CALENDAR_CACHE[symbol] = calendar
    return calendar


def _in_bounds(calendar, day: date) -> bool:
    try:
        return calendar.first_session.date() <= day <= calendar.last_session.date()
    except Exception:  # pragma: no cover - defensive
        return False


def calendar_source(market: str | None, value: str | date | datetime | None) -> str:
    """Where the open/closed verdict comes from (transparency for audits)."""

    market_code = normalize_market(market)
    day = _to_date(value)
    if market_code == "CN" and day.year >= 2026 and day.year in CN_MARKET_HOLIDAYS_BY_YEAR:
        suffix = "provisional" if day.year in PROVISIONAL_CN_YEARS else "official"
        return f"local:CN_{day.year}_{suffix}"
    calendar = _calendar(market_code)
    if calendar is not None and _in_bounds(calendar, day):
        return f"exchange_calendars:{CALENDAR_SYMBOLS[market_code]}"
    if market_code == "US" and day.year == 2026:
        return "local:US_2026"
    return "weekday_fallback"


def is_provisional(market: str | None, value: str | date | datetime | None) -> bool:
    market_code = normalize_market(market)
    day = _to_date(value)
    return market_code == "CN" and day.year in PROVISIONAL_CN_YEARS


def is_market_open_date(market: str | None, value: str | date | datetime | None) -> bool:
    market_code = normalize_market(market)
    day = _to_date(value)
    day_iso = day.isoformat()
    if day.weekday() >= 5:
        return False
    if market_code == "CN":
        holidays = CN_MARKET_HOLIDAYS_BY_YEAR.get(day.year)
        if day.year >= 2026 and holidays is not None:
            return day_iso not in holidays
        calendar = _calendar("CN")
        if calendar is not None and _in_bounds(calendar, day):
            return bool(calendar.is_session(day))
        if holidays is not None:
            return day_iso not in holidays
        return True
    calendar = _calendar(market_code)
    if calendar is not None and _in_bounds(calendar, day):
        return bool(calendar.is_session(day))
    if market_code == "US" and day.year == 2026:
        return day_iso not in US_MARKET_HOLIDAYS_2026
    return True


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
    default_open, default_close = _DEFAULT_SESSIONS.get(market_code, ("09:30", "16:00"))
    regular_open = default_open
    regular_close = default_close
    early_close: str | None = None
    session_break: str | None = None
    if is_open:
        calendar = _calendar(market_code)
        if calendar is not None and _in_bounds(calendar, day):
            tz = market_timezone(market_code)
            try:
                regular_open = calendar.session_open(day).tz_convert(tz).strftime("%H:%M")
                close_local = calendar.session_close(day).tz_convert(tz)
                regular_close = close_local.strftime("%H:%M")
                if market_code in {"CN", "HK"}:
                    break_start = calendar.session_break_start(day)
                    break_end = calendar.session_break_end(day)
                    if break_start is not None and break_end is not None and break_start == break_start:
                        session_break = (
                            f"{break_start.tz_convert(tz):%H:%M}-{break_end.tz_convert(tz):%H:%M}"
                        )
            except Exception:  # pragma: no cover - defensive
                pass
        if market_code == "US" and day.year == 2026:
            local_early = US_MARKET_EARLY_CLOSES_2026.get(day_iso)
            if local_early:
                regular_close = local_early
        if regular_close != default_close:
            early_close = regular_close
        reason = "early_close" if early_close else "regular_session"
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
        "regular_open": regular_open,
        "regular_close": regular_close,
        "early_close": early_close,
        "session_break": session_break,
        "calendar_source": calendar_source(market_code, day),
        "provisional": is_provisional(market_code, day),
        "previous_open_date": previous_market_open_date(market_code, day, include_self=False),
        "next_open_date": next_market_open_date(market_code, day, include_self=False),
    }
