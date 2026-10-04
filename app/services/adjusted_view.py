"""Derive adjusted price views from raw bars plus corporate actions.

Prices are never adjusted in place: this module returns a derived series whose
version pins the raw digest, the action revision ids and the method. The event
model is factor-based so TuShare ``adj_factor`` jumps (splits plus dividends)
and explicit split/cash-dividend records flow through the same arithmetic.

qfq: multiplier(t) = product over actions with effective_date > t of 1/factor
hfq: multiplier(t) = product over actions with effective_date <= t of factor
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from typing import Iterable, Mapping, Sequence

from app.services.corporate_actions import CorporateActionRecord

ADJUSTED_METHODS = ("qfq", "hfq")
_FACTOR_ACTIONS = {"split", "stock_dividend", "rights", "adjustment_factor"}


class AdjustmentError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdjustedBar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    multiplier: float
    method: str

    def as_row(self) -> dict:
        return {
            "date": self.trade_date.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "adj_multiplier": self.multiplier,
            "adjustment_method": self.method,
        }


@dataclass(frozen=True, slots=True)
class AdjustmentStats:
    events_total: int
    events_applied: int
    events_skipped_no_trade_date: int
    events_skipped_unusable: int


def _parse_bar_date(value: object) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise AdjustmentError(f"invalid bar date `{value}`") from exc


def _event_factor(action: CorporateActionRecord, *, previous_close: float | None) -> float | None:
    if action.action_type in _FACTOR_ACTIONS:
        return float(action.factor) if action.factor else None
    if action.action_type == "cash_dividend" and action.cash_amount is not None:
        if previous_close and previous_close > float(action.cash_amount) > 0:
            return previous_close / (previous_close - float(action.cash_amount))
        return None
    return None


def build_adjusted_series(
    bars: Sequence[Mapping | AdjustedBar],
    actions: Iterable[CorporateActionRecord],
    *,
    method: str = "qfq",
) -> tuple[list[AdjustedBar], AdjustmentStats]:
    method_code = str(method or "").strip().lower()
    if method_code not in ADJUSTED_METHODS:
        raise AdjustmentError(f"method must be one of {ADJUSTED_METHODS}, got `{method}`")
    normalized = [
        {
            "date": _parse_bar_date(
                (bar.get("trade_date") or bar.get("date")) if isinstance(bar, Mapping) else bar.trade_date
            ),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar["volume"]),
        }
        for bar in bars
    ]
    if not normalized:
        return [], AdjustmentStats(0, 0, 0, 0)
    dates = [bar["date"] for bar in normalized]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise AdjustmentError("bars must have unique ascending trade dates")
    date_index = {value: index for index, value in enumerate(dates)}

    actions_by_date: dict[date, list[CorporateActionRecord]] = {}
    for action in actions:
        actions_by_date.setdefault(action.effective_date, []).append(action)

    events_total = sum(len(items) for items in actions_by_date.values())
    factors_by_date: dict[date, list[float]] = {}
    skipped_no_date = 0
    skipped_unusable = 0
    for event_date, items in actions_by_date.items():
        factors: list[float] = []
        index = date_index.get(event_date)
        if index is None:
            skipped_no_date += len(items)
            continue
        previous_close = normalized[index - 1]["close"] if index > 0 else None
        for action in items:
            factor = _event_factor(action, previous_close=previous_close)
            if factor is None or factor <= 0:
                skipped_unusable += 1
                continue
            factors.append(factor)
        if factors:
            factors_by_date[event_date] = factors

    adjusted: list[AdjustedBar] = []
    if method_code == "qfq":
        multiplier = 1.0
        for bar in reversed(normalized):
            adjusted.append(
                AdjustedBar(
                    trade_date=bar["date"],
                    open=bar["open"] * multiplier,
                    high=bar["high"] * multiplier,
                    low=bar["low"] * multiplier,
                    close=bar["close"] * multiplier,
                    volume=bar["volume"],
                    multiplier=multiplier,
                    method=method_code,
                )
            )
            for factor in factors_by_date.get(bar["date"], ()):
                multiplier /= factor
        adjusted.reverse()
    else:  # hfq: events on the effective date already scale that session
        multiplier = 1.0
        for bar in normalized:
            for factor in factors_by_date.get(bar["date"], ()):
                multiplier *= factor
            adjusted.append(
                AdjustedBar(
                    trade_date=bar["date"],
                    open=bar["open"] * multiplier,
                    high=bar["high"] * multiplier,
                    low=bar["low"] * multiplier,
                    close=bar["close"] * multiplier,
                    volume=bar["volume"],
                    multiplier=multiplier,
                    method=method_code,
                )
            )

    applied = sum(len(items) for items in factors_by_date.values())
    return adjusted, AdjustmentStats(
        events_total=events_total,
        events_applied=applied,
        events_skipped_no_trade_date=skipped_no_date,
        events_skipped_unusable=skipped_unusable,
    )


def raw_series_digest(bars: Sequence[Mapping]) -> str:
    payload = [
        [
            str(bar.get("date") or bar.get("trade_date") or "")[:10],
            str(bar.get("symbol") or ""),
            float(bar.get("open") or 0.0),
            float(bar.get("high") or 0.0),
            float(bar.get("low") or 0.0),
            float(bar.get("close") or 0.0),
            float(bar.get("volume") or 0.0),
        ]
        for bar in bars
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def adjustment_version(
    *,
    method: str,
    raw_digest: str,
    actions: Sequence[CorporateActionRecord],
) -> str:
    payload = json.dumps(
        {
            "method": method,
            "raw_digest": raw_digest,
            "actions": sorted(action.revision_id for action in actions),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"adj_v1:{method}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"
