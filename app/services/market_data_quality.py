"""OHLCV validation and quarantine for the canonical market lake (P0B).

Validation is deliberately pure so it can run before any partition write and
be unit tested without touching the lake. Rejected rows are appended to a
per-market JSONL quarantine file; callers must not write them to canonical
storage.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROW_VALIDATION_SCHEMA_VERSION = "lake_row_validation_v1"

_PRICE_FIELDS = ("open", "high", "low", "close")


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def classify_ohlcv_row(row: dict) -> str | None:
    """Return a rejection reason code, or None when the row is canonical-safe."""

    symbol = str(row.get("symbol") or "").strip()
    trade_date = str(row.get("date") or "").strip()
    if not symbol or not trade_date:
        return "missing_identity"
    prices: dict[str, float] = {}
    for field in _PRICE_FIELDS:
        if row.get(field) in (None, ""):
            return f"missing_{field}"
        value = _as_float(row.get(field))
        if value is None:
            return f"invalid_{field}"
        if value <= 0:
            return "non_positive_price"
        prices[field] = value
    if row.get("volume") in (None, ""):
        return "missing_volume"
    volume = _as_float(row.get("volume"))
    if volume is None:
        return "invalid_volume"
    if volume < 0:
        return "negative_volume"
    tolerance = max(1e-9, abs(prices["close"]) * 1e-9)
    if prices["high"] < max(prices["open"], prices["close"], prices["low"]) - tolerance:
        return "invalid_ohlc_high"
    if prices["low"] > min(prices["open"], prices["close"], prices["high"]) + tolerance:
        return "invalid_ohlc_low"
    return None


def partition_ohlcv_rows(rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Split rows into (accepted, rejected-with-reason)."""

    accepted: list[dict] = []
    rejected: list[dict] = []
    for row in rows:
        reason = classify_ohlcv_row(row)
        if reason is None:
            accepted.append(dict(row))
        else:
            rejected.append({**dict(row), "rejection_reason": reason})
    return accepted, rejected


def quarantine_rows(*, root: Path, market: str, rows: list[dict]) -> Path | None:
    """Append rejected rows to a daily JSONL quarantine file."""

    if not rows:
        return None
    market_code = str(market or "").strip().upper() or "UNKNOWN"
    day = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    path = Path(root) / "_quarantine" / f"{market_code.lower()}_rejected_{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rejected_at = datetime.now(tz=timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            payload = {
                "rejected_at": rejected_at,
                "market": market_code,
                "schema_version": ROW_VALIDATION_SCHEMA_VERSION,
                **row,
            }
            handle.write(json.dumps(payload, default=str, sort_keys=True) + "\n")
    return path
