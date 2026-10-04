"""Calendar coverage monitor (S-9 maintenance).

Reports how far each market's calendar is covered and whether any upcoming year
still relies on the provisional CN table. Exits non-zero when action is needed
so it can be wired into a periodic check.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.market_calendar import (  # noqa: E402
    CALENDAR_SYMBOLS,
    CN_MARKET_HOLIDAYS_BY_YEAR,
    PROVISIONAL_CN_YEARS,
    _calendar,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--warn-days", type=int, default=120, help="flag when coverage ends within N days")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of)

    payload: dict = {"as_of": as_of.isoformat(), "markets": {}, "actions_required": []}
    for market, symbol in CALENDAR_SYMBOLS.items():
        calendar = _calendar(market)
        last_session = calendar.last_session.date().isoformat() if calendar is not None else None
        entry = {"symbol": symbol, "last_session": last_session}
        if market == "CN":
            covered_years = sorted(CN_MARKET_HOLIDAYS_BY_YEAR)
            entry["local_years"] = covered_years
            entry["provisional_years"] = sorted(PROVISIONAL_CN_YEARS)
            needed_years = {as_of.year, as_of.year + 1}
            missing = sorted(needed_years - set(covered_years))
            entry["missing_local_years"] = missing
            if missing:
                payload["actions_required"].append(
                    f"CN: local table misses {missing} - add the official arrangement before the year starts"
                )
            payload["markets"][market] = entry
            # The library's CN coverage is expected to be exhausted; the local
            # official table is the source for 2026+.
            continue
        payload["markets"][market] = entry
        if last_session is None:
            payload["actions_required"].append(f"{market}: calendar library unavailable")
            continue
        days_left = (date.fromisoformat(last_session) - as_of).days
        entry["days_remaining"] = days_left
        if days_left < args.warn_days:
            payload["actions_required"].append(
                f"{market}: library coverage ends {last_session} ({days_left} days) - extend or add a local table"
            )
    current_year = as_of.year
    for year in sorted(CN_MARKET_HOLIDAYS_BY_YEAR):
        if year in PROVISIONAL_CN_YEARS and year <= current_year + 1:
            payload["actions_required"].append(
                f"CN: {year} still provisional - replace with the official State Council arrangement when published"
            )
    payload["status"] = "action_required" if payload["actions_required"] else "ok"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 1 if payload["actions_required"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
