from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


APP_TZ = ZoneInfo("Asia/Shanghai")


def app_now() -> datetime:
    return datetime.now(APP_TZ).replace(microsecond=0)


def app_now_iso() -> str:
    return app_now().isoformat()


def app_today_iso() -> str:
    return app_now().date().isoformat()


def parse_app_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=APP_TZ)
    return parsed.astimezone(APP_TZ)


def format_app_datetime(value: str | None, *, with_tz: bool = False) -> str:
    parsed = parse_app_datetime(value)
    if parsed is None:
        return "-"
    rendered = parsed.strftime("%Y-%m-%d %H:%M:%S")
    if with_tz:
        rendered += " Asia/Shanghai"
    return rendered
