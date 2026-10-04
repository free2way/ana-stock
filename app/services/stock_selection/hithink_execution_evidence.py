"""Conservative HiThink execution-fact reconciliation for bounded CN samples.

Provider event pools are observations, not substitutes for per-security daily
price-limit bounds. Missing optional facts remain visible even when a caller
chooses the observable-OHLCV policy that does not block on them.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PRICE_FIELDS = ("open_price", "high_price", "low_price", "close_price", "volume", "turnover")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def date_from_ms(value: object) -> str:
    return datetime.fromtimestamp(int(value) / 1000, tz=SHANGHAI_TZ).date().isoformat()


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_price_items(*, ticker: str, items: list[dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("date_ms") is None:
            raise ValueError("invalid HiThink price item")
        day = date_from_ms(item["date_ms"])
        if day in result:
            raise ValueError("duplicate HiThink price date")
        normalized = {field: _finite_number(item.get(field)) for field in PRICE_FIELDS}
        if any(value is None for value in normalized.values()):
            raise ValueError("missing or nonfinite HiThink price field")
        if normalized["high_price"] < max(
            normalized["open_price"], normalized["close_price"], normalized["low_price"]
        ) or normalized["low_price"] > min(normalized["open_price"], normalized["close_price"]):
            raise ValueError("inconsistent HiThink OHLC")
        result[day] = {"ticker": ticker, "date": day, **normalized}
    return result


def normalize_action_items(*, ticker: str, items: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    seen: set[tuple] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("ex_date_ms") is None:
            raise ValueError("invalid HiThink corporate-action item")
        day = date_from_ms(item["ex_date_ms"])
        values = {
            "dividend_per_share": _finite_number(item.get("dividend_per_share")),
            "per_share_bonus": _finite_number(item.get("per_share_bonus")),
        }
        if any(value is None or value < 0 for value in values.values()):
            raise ValueError("invalid HiThink corporate-action values")
        key = (ticker, day, values["dividend_per_share"], values["per_share_bonus"])
        if key in seen:
            raise ValueError("duplicate HiThink corporate-action item")
        seen.add(key)
        result.setdefault(day, []).append({"ticker": ticker, "ex_date": day, **values})
    return result


def compare_price_sources(*, api_rows: dict[str, dict], dump_rows: dict[str, dict]) -> dict:
    dates = sorted(set(api_rows) | set(dump_rows))
    mismatches = []
    matched = 0
    for day in dates:
        api_row, dump_row = api_rows.get(day), dump_rows.get(day)
        if api_row is None or dump_row is None:
            mismatches.append({"date": day, "reason": "missing_from_api" if api_row is None else "missing_from_dump"})
            continue
        differing = [
            field for field in PRICE_FIELDS
            if not math.isclose(float(api_row[field]), float(dump_row[field]), rel_tol=1e-10, abs_tol=1e-8)
        ]
        if differing:
            mismatches.append({"date": day, "reason": "field_mismatch", "fields": differing})
        else:
            matched += 1
    return {"matched_dates": matched, "compared_dates": len(dates), "mismatches": mismatches,
            "status": "PASS" if dates and not mismatches else "BLOCKED"}


def compare_action_sources(*, api_rows: dict[str, list[dict]], dump_rows: dict[str, list[dict]]) -> dict:
    def flattened(rows: dict[str, list[dict]]) -> list[tuple]:
        return sorted(
            (row["ticker"], row["ex_date"], row["dividend_per_share"], row["per_share_bonus"])
            for values in rows.values() for row in values
        )

    api_values, dump_values = flattened(api_rows), flattened(dump_rows)
    return {
        "api_event_count": len(api_values),
        "dump_event_count": len(dump_values),
        "status": "PASS" if api_values == dump_values else "BLOCKED",
        "api_only": [list(value) for value in sorted(set(api_values) - set(dump_values))],
        "dump_only": [list(value) for value in sorted(set(dump_values) - set(api_values))],
    }


def build_gap_rows(
    *, tickers: list[str], trading_dates: list[str], price_checks: dict[str, dict],
    action_checks: dict[str, dict], actions_by_ticker: dict[str, dict[str, list[dict]]],
    pool_memberships: dict[tuple[str, str], list[str]], source_references: dict[str, str],
    required_components: tuple[str, ...] = ("raw_price", "suspension", "trading_limit", "corporate_action"),
) -> dict:
    allowed = {"raw_price", "suspension", "trading_limit", "corporate_action"}
    if not required_components or len(set(required_components)) != len(required_components) or not set(required_components) <= allowed:
        raise ValueError("invalid required evidence components")
    rows, counts = [], Counter()
    for ticker in sorted(tickers):
        price_pass = price_checks[ticker]["status"] == "PASS"
        action_pass = action_checks[ticker]["status"] == "PASS"
        for day in trading_dates:
            pools = sorted(set(pool_memberships.get((ticker, day), [])))
            action_events = actions_by_ticker.get(ticker, {}).get(day, [])
            facts = {
                "raw_price": {
                    "status": "SATISFIED" if price_pass else "MISSING_OR_CONFLICTING",
                    "source": source_references.get("raw_price"),
                },
                "suspension": {
                    "status": "MISSING",
                    "reason": "no_historical_suspension_endpoint_in_public_hithink_contract",
                    "source": None,
                },
                "trading_limit": {
                    "status": "POOL_OBSERVATION_ONLY" if pools else "MISSING_DAILY_BOUND",
                    "observed_pools": pools,
                    "reason": "event pools do not provide every security's explicit daily upper/lower bounds",
                    "source": source_references.get("limit_pools") if pools else None,
                },
                "corporate_action": {
                    "status": "SATISFIED" if action_pass else "MISSING_OR_CONFLICTING",
                    "state": "action" if action_events else "none" if action_pass else "unknown",
                    "events": action_events,
                    "source": source_references.get("corporate_actions"),
                },
            }
            missing = [name for name in required_components if facts[name]["status"] != "SATISFIED"]
            optional_gaps = [name for name, fact in facts.items()
                             if name not in required_components and fact["status"] != "SATISFIED"]
            status = "EVIDENCE_COMPLETE" if not missing else "BLOCKED"
            counts[status] += 1
            for name in missing:
                counts[f"missing:{name}"] += 1
            rows.append({"ticker": ticker, "date": day, "status": status,
                         "missing_components": missing, "optional_evidence_gaps": optional_gaps,
                         "facts": facts})
    return {"rows": rows, "counts": dict(counts),
            "required_components": list(required_components),
            "evidence_complete": bool(rows) and all(row["status"] == "EVIDENCE_COMPLETE" for row in rows)}
