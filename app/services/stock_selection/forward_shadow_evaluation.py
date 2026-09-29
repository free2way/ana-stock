from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable, Mapping

from sqlalchemy.orm import Session

from app.services.market_calendar import next_market_open_date
from app.services.market_lake import get_latest_lake_trade_date, load_lake_rows
from app.services.repository import WorkspaceSnapshotRepository
from app.services.stock_selection.forward_shadow import CN_FORWARD_SHADOW_SNAPSHOT_TYPE


CN_FORWARD_SHADOW_EVALUATION_SNAPSHOT_TYPE = (
    "stock_selection_shadow_evaluation:CN:quality_value_shadow_v2"
)


@dataclass(frozen=True, slots=True)
class CNForwardShadowEvaluationConfig:
    top_ns: tuple[int, ...] = (5, 10, 20)
    round_trip_cost_bps: float = 40.0
    minimum_confirmation_dates: int = 60
    maximum_single_day_jump: float = 0.80
    schema_version: str = "stock_selection_forward_shadow_evaluation_v2"

    def __post_init__(self) -> None:
        if not self.top_ns or any(value <= 0 for value in self.top_ns):
            raise ValueError("top_ns must contain positive values")
        if len(set(self.top_ns)) != len(self.top_ns):
            raise ValueError("top_ns must not contain duplicates")
        if self.round_trip_cost_bps < 0:
            raise ValueError("round_trip_cost_bps must be non-negative")
        if self.minimum_confirmation_dates <= 0:
            raise ValueError("minimum_confirmation_dates must be positive")
        if not 0 < self.maximum_single_day_jump <= 1:
            raise ValueError("maximum_single_day_jump must be in (0, 1]")


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite_positive(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def build_cn_forward_shadow_evaluation(
    snapshots: Iterable[Mapping[str, object]],
    price_rows: Iterable[Mapping[str, object]],
    *,
    as_of_date: str,
    config: CNForwardShadowEvaluationConfig | None = None,
) -> dict:
    """Evaluate only forward-shadow decisions whose frozen horizon has matured."""

    resolved = config or CNForwardShadowEvaluationConfig()
    normalized_as_of = str(as_of_date or "").strip()[:10]
    if not normalized_as_of:
        raise ValueError("as_of_date is required")
    prices: dict[tuple[str, str], Mapping[str, object]] = {}
    dates_by_ticker: dict[str, list[str]] = {}
    for row in price_rows:
        ticker = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        trade_date = str(row.get("date") or row.get("trade_date") or "").strip()[:10]
        if not ticker or not trade_date:
            continue
        prices[(ticker, trade_date)] = row
        dates_by_ticker.setdefault(ticker, []).append(trade_date)
    for ticker in dates_by_ticker:
        dates_by_ticker[ticker] = sorted(set(dates_by_ticker[ticker]))

    unique_snapshots: dict[str, Mapping[str, object]] = {}
    for snapshot in snapshots:
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), Mapping) else snapshot
        effective_date = str(payload.get("effective_trade_date") or "").strip()[:10]
        if effective_date and effective_date not in unique_snapshots:
            unique_snapshots[effective_date] = payload

    daily: list[dict] = []
    pending: list[dict] = []
    excluded: list[dict] = []
    cost = resolved.round_trip_cost_bps / 10_000.0
    for effective_date, payload in sorted(unique_snapshots.items()):
        horizon_days = int(payload.get("horizon_days") or 5)
        # Entry is the frozen effective date, including decisions made long
        # after the feature date. Never shorten the holding period by accident.
        exit_date = effective_date
        for _ in range(max(0, horizon_days - 1)):
            exit_date = next_market_open_date("CN", exit_date, include_self=False)
        if exit_date > normalized_as_of:
            pending.append(
                {
                    "effective_trade_date": effective_date,
                    "exit_trade_date": exit_date,
                    "reason": "label_not_mature",
                }
            )
            continue
        observations = [
            dict(item) if isinstance(item, Mapping) else {}
            for item in (payload.get("top_observations") or [])
        ]
        outcomes: list[dict] = []
        seen_tickers: set[str] = set()
        for item in observations:
            ticker = str(item.get("ticker") or "").strip().upper()
            outcome = {"ticker": ticker, "net_return": None, "status": "missing_price"}
            outcomes.append(outcome)
            if not ticker or ticker in seen_tickers:
                outcome["status"] = "invalid_frozen_member"
                continue
            seen_tickers.add(ticker)
            entry = prices.get((ticker, effective_date))
            exit_row = prices.get((ticker, exit_date))
            entry_open = _finite_positive((entry or {}).get("open"))
            exit_close = _finite_positive((exit_row or {}).get("close"))
            if entry_open is None or exit_close is None:
                continue
            path_dates = [
                value
                for value in dates_by_ticker.get(ticker, [])
                if effective_date <= value <= exit_date
            ]
            closes = [
                _finite_positive(prices[(ticker, value)].get("close"))
                for value in path_dates
            ]
            valid_closes = [value for value in closes if value is not None]
            suspicious_jump = any(
                abs(current / previous - 1.0) >= resolved.maximum_single_day_jump
                for previous, current in zip(valid_closes, valid_closes[1:])
            )
            if suspicious_jump:
                outcome["status"] = "suspected_corporate_action"
                continue
            outcome.update(status="measured", net_return=exit_close / entry_open - 1.0 - cost)
        if not any(item["status"] == "measured" for item in outcomes):
            excluded.append(
                {
                    "effective_trade_date": effective_date,
                    "exit_trade_date": exit_date,
                    "reason": "no_complete_tradable_outcomes",
                }
            )
        top_metrics = {}
        for top_n in resolved.top_ns:
            # Slice frozen membership BEFORE considering price availability.
            selected = outcomes[:top_n]
            returns = [float(item["net_return"]) for item in selected if item["net_return"] is not None]
            complete = len(selected) == top_n and len(returns) == top_n
            top_metrics[str(top_n)] = {
                "selected_count": len(selected),
                "measured_count": len(returns),
                "status": "complete" if complete else "incomplete",
                "members": selected,
                "mean_net_return": statistics.fmean(returns) if complete else None,
                "positive_rate": sum(value > 0 for value in returns) / len(returns) if complete else None,
            }
        daily.append(
            {
                "effective_trade_date": effective_date,
                "exit_trade_date": exit_date,
                "outcome_count": sum(item["status"] == "measured" for item in outcomes),
                "complete": all(item["status"] == "complete" for item in top_metrics.values()),
                "top_n": top_metrics,
            }
        )

    aggregate: dict[str, dict] = {}
    for top_n in resolved.top_ns:
        key = str(top_n)
        complete_metrics = [item["top_n"][key] for item in daily if item["top_n"][key]["status"] == "complete"]
        values = [float(item["mean_net_return"]) for item in complete_metrics]
        signals = [member for item in complete_metrics for member in item["members"]]
        aggregate[key] = {
            "evaluated_date_count": len(values),
            "incomplete_date_count": len(daily) - len(values),
            "stock_signal_count": len(signals),
            "positive_stock_signal_count": sum(item["net_return"] > 0 for item in signals),
            "stock_signal_win_rate": sum(item["net_return"] > 0 for item in signals) / len(signals) if signals else None,
            "mean_holding_period_net_return": statistics.fmean(values) if values else None,
            # Compatibility alias; this is NOT an account daily return.
            "mean_daily_net_return": statistics.fmean(values) if values else None,
            "positive_date_rate": (
                sum(value > 0 for value in values) / len(values) if values else None
            ),
        }
    evaluated_dates = sum(item["complete"] for item in daily)
    payload = {
        "schema_version": resolved.schema_version,
        "scope": "cn_forward_shadow_evaluation_only",
        "market": "CN",
        "as_of_date": normalized_as_of,
        "configuration": asdict(resolved),
        "source_snapshot_count": len(unique_snapshots),
        "evaluated_date_count": evaluated_dates,
        "pending_date_count": len(pending),
        "excluded_date_count": len(excluded),
        "incomplete_date_count": len(daily) - evaluated_dates,
        "metric_semantics": {
            "return": "mean_per_signal_date_holding_period_net_return",
            "positive_date_rate": "fraction_of_complete_batches_with_positive_mean_return",
            "stock_signal_win_rate": "profitable_stock_date_signals_over_complete_batch_signals",
            "execution_model": "price_return_diagnostic_not_filled_portfolio",
            "missing_member_policy": "retain_frozen_membership_and_block_incomplete_batch",
        },
        "remaining_confirmation_dates": max(
            0, resolved.minimum_confirmation_dates - evaluated_dates
        ),
        "aggregate_top_n": aggregate,
        "daily_metrics": daily,
        "pending_dates": pending,
        "excluded_dates": excluded,
        "promotion_status": (
            "REVIEW_REQUIRED"
            if evaluated_dates >= resolved.minimum_confirmation_dates and all(item["complete"] for item in daily)
            else "BLOCKED"
        ),
        "promotion_blockers": [
            *([] if evaluated_dates >= resolved.minimum_confirmation_dates else [f"minimum_{resolved.minimum_confirmation_dates}_matured_untouched_dates_not_met"]),
            *([] if all(item["complete"] for item in daily) else ["incomplete_frozen_batches"]),
        ],
    }
    payload["evidence_version"] = (
        f"{resolved.schema_version}:CN:{_hash(payload)[:20]}"
    )
    return payload


def create_cn_forward_shadow_evaluation_snapshot(
    db: Session,
    *,
    as_of_date: str | date | None = None,
    source_job_id: int | None = None,
    config: CNForwardShadowEvaluationConfig | None = None,
) -> dict:
    """Persist one idempotent daily evaluation of matured forward snapshots."""

    resolved_as_of = str(
        as_of_date or get_latest_lake_trade_date(market="CN") or ""
    )[:10]
    if not resolved_as_of:
        raise ValueError("No CN lake date is available for shadow evaluation")
    repository = WorkspaceSnapshotRepository(db)
    snapshots = repository.list_snapshots(CN_FORWARD_SHADOW_SNAPSHOT_TYPE, limit=400)
    payloads = [item.get("payload") or {} for item in sorted(snapshots, key=lambda item: item["id"])]
    tickers = {
        str(row.get("ticker") or "").strip().upper()
        for payload in payloads
        for row in (payload.get("top_observations") or [])
        if isinstance(row, Mapping) and str(row.get("ticker") or "").strip()
    }
    feature_dates = sorted(
        str(payload.get("feature_date") or "")[:10]
        for payload in payloads
        if payload.get("feature_date")
    )
    price_rows = load_lake_rows(
        markets=["CN"],
        tickers=tickers,
        start_date=feature_dates[0] if feature_dates else resolved_as_of,
        end_date=resolved_as_of,
    )
    evaluation = build_cn_forward_shadow_evaluation(
        payloads,
        price_rows,
        as_of_date=resolved_as_of,
        config=config,
    )
    existing = next(
        (
            item
            for item in repository.list_snapshots(
                CN_FORWARD_SHADOW_EVALUATION_SNAPSHOT_TYPE,
                limit=30,
            )
            if str(item.get("snapshot_date") or "")[:10] == resolved_as_of
            and (item.get("payload") or {}).get("evidence_version")
            == evaluation["evidence_version"]
        ),
        None,
    )
    if existing is not None:
        return {
            "status": "success",
            "reused_existing": True,
            "snapshot_id": existing["id"],
            "payload": existing.get("payload") or evaluation,
        }
    row = repository.create_snapshot(
        snapshot_type=CN_FORWARD_SHADOW_EVALUATION_SNAPSHOT_TYPE,
        snapshot_date=resolved_as_of,
        payload=evaluation,
        source_job_id=source_job_id,
    )
    return {
        "status": "success",
        "reused_existing": False,
        "snapshot_id": row.id,
        "payload": evaluation,
    }
