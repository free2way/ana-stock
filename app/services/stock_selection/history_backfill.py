from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from typing import Callable, Mapping, Sequence

from app.services.market_lake import (
    count_lake_symbols_for_trade_date,
    list_lake_symbols,
    list_lake_trade_dates,
    write_ohlcv_rows_to_lake,
)
from app.services.tushare_client import TushareClient


@dataclass(frozen=True, slots=True)
class CNHistoryBackfillConfig:
    target_history_sessions: int = 252
    minimum_partition_coverage: float = 0.60
    max_dates_per_run: int = 5
    maximum_consecutive_failures: int = 2
    inter_date_delay_seconds: float = 4.0
    dry_run: bool = True

    def __post_init__(self) -> None:
        if self.target_history_sessions <= 0:
            raise ValueError("target_history_sessions must be positive")
        if not 0.0 < self.minimum_partition_coverage <= 1.0:
            raise ValueError("minimum_partition_coverage must be in (0, 1]")
        if self.max_dates_per_run <= 0:
            raise ValueError("max_dates_per_run must be positive")
        if self.maximum_consecutive_failures <= 0:
            raise ValueError("maximum_consecutive_failures must be positive")
        if self.inter_date_delay_seconds < 0:
            raise ValueError("inter_date_delay_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class CNHistoryBackfillPlan:
    universe_symbol_count: int
    target_history_sessions: int
    available_partition_count: int
    target_partition_count: int
    minimum_symbols_per_partition: int
    healthy_partition_count: int
    pending_dates: tuple[str, ...]
    partition_symbol_counts: Mapping[str, int]
    completion_ratio: float
    blockers: tuple[str, ...]

    @property
    def ready_for_data_readiness_audit(self) -> bool:
        return not self.blockers and not self.pending_dates


@dataclass(frozen=True, slots=True)
class CNHistoryBackfillDateResult:
    trade_date: str
    status: str
    symbol_count_before: int
    fetched_row_count: int
    fetched_symbol_count: int
    symbol_count_after: int
    parquet_file_count: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CNHistoryBackfillResult:
    status: str
    dry_run: bool
    plan_before: CNHistoryBackfillPlan
    plan_after: CNHistoryBackfillPlan
    attempted_date_count: int
    completed_date_count: int
    failed_date_count: int
    date_results: tuple[CNHistoryBackfillDateResult, ...]
    message: str


ProgressCallback = Callable[[Mapping[str, object]], None]


def summarize_cn_history_backfill_plan(
    plan: CNHistoryBackfillPlan,
    *,
    pending_date_limit: int = 20,
) -> dict:
    return {
        "universe_symbol_count": plan.universe_symbol_count,
        "target_history_sessions": plan.target_history_sessions,
        "available_partition_count": plan.available_partition_count,
        "target_partition_count": plan.target_partition_count,
        "minimum_symbols_per_partition": plan.minimum_symbols_per_partition,
        "healthy_partition_count": plan.healthy_partition_count,
        "pending_partition_count": len(plan.pending_dates),
        "next_pending_dates": list(plan.pending_dates[: max(0, pending_date_limit)]),
        "completion_ratio": plan.completion_ratio,
        "blockers": list(plan.blockers),
        "ready_for_data_readiness_audit": plan.ready_for_data_readiness_audit,
    }


def summarize_cn_history_backfill_result(result: CNHistoryBackfillResult) -> dict:
    return {
        "market": "CN",
        "status": result.status,
        "dry_run": result.dry_run,
        "message": result.message,
        "attempted_date_count": result.attempted_date_count,
        "completed_date_count": result.completed_date_count,
        "failed_date_count": result.failed_date_count,
        "plan_before": summarize_cn_history_backfill_plan(result.plan_before),
        "plan_after": summarize_cn_history_backfill_plan(result.plan_after),
        "date_results": [asdict(item) for item in result.date_results],
    }


def build_cn_history_backfill_plan(
    *,
    config: CNHistoryBackfillConfig | None = None,
    universe_symbols: Sequence[str] | None = None,
    trade_dates: Sequence[str] | None = None,
    partition_symbol_counts: Mapping[str, int] | None = None,
) -> CNHistoryBackfillPlan:
    settings = config or CNHistoryBackfillConfig()
    symbols = tuple(
        sorted(
            {
                str(item or "").strip().upper()
                for item in (
                    universe_symbols
                    if universe_symbols is not None
                    else list_lake_symbols(market="CN")
                )
                if str(item or "").strip()
            }
        )
    )
    dates = sorted(
        {
            str(item or "").strip()[:10]
            for item in (
                trade_dates
                if trade_dates is not None
                else list_lake_trade_dates(market="CN")
            )
            if str(item or "").strip()
        },
        reverse=True,
    )
    target_dates = dates[: settings.target_history_sessions]
    minimum_symbols = math.ceil(len(symbols) * settings.minimum_partition_coverage)
    provided_counts = partition_symbol_counts or {}
    counts = {
        trade_date: int(
            provided_counts.get(trade_date)
            if trade_date in provided_counts
            else count_lake_symbols_for_trade_date(market="CN", trade_date=trade_date)
        )
        for trade_date in target_dates
    }
    pending_dates = tuple(
        trade_date
        for trade_date in target_dates
        if counts.get(trade_date, 0) < minimum_symbols
    )
    healthy_count = len(target_dates) - len(pending_dates)
    blockers: list[str] = []
    if not symbols:
        blockers.append("empty_cn_lake_universe")
    if len(target_dates) < settings.target_history_sessions:
        blockers.append("insufficient_trade_date_partitions")
    if pending_dates:
        blockers.append("undercovered_trade_date_partitions")
    return CNHistoryBackfillPlan(
        universe_symbol_count=len(symbols),
        target_history_sessions=settings.target_history_sessions,
        available_partition_count=len(dates),
        target_partition_count=len(target_dates),
        minimum_symbols_per_partition=minimum_symbols,
        healthy_partition_count=healthy_count,
        pending_dates=pending_dates,
        partition_symbol_counts=counts,
        completion_ratio=(healthy_count / settings.target_history_sessions),
        blockers=tuple(blockers),
    )


def backfill_cn_stock_selection_history(
    *,
    config: CNHistoryBackfillConfig | None = None,
    progress_callback: ProgressCallback | None = None,
    client: TushareClient | None = None,
) -> CNHistoryBackfillResult:
    settings = config or CNHistoryBackfillConfig()
    symbols = tuple(sorted(list_lake_symbols(market="CN")))
    trade_dates = tuple(list_lake_trade_dates(market="CN"))
    plan_before = build_cn_history_backfill_plan(
        config=settings,
        universe_symbols=symbols,
        trade_dates=trade_dates,
    )
    selected_dates = plan_before.pending_dates[: settings.max_dates_per_run]
    if settings.dry_run or not selected_dates:
        status = "success" if plan_before.ready_for_data_readiness_audit else "partial"
        if plan_before.ready_for_data_readiness_audit:
            message = "CN history backfill is complete and ready for the strict data-readiness audit."
        elif settings.dry_run:
            message = (
                f"Dry run found {len(plan_before.pending_dates)} undercovered partition(s); "
                f"the next run would process {len(selected_dates)}."
            )
        else:
            message = "No undercovered existing partition is runnable; resolve the plan blockers first."
        return CNHistoryBackfillResult(
            status=status,
            dry_run=settings.dry_run,
            plan_before=plan_before,
            plan_after=plan_before,
            attempted_date_count=0,
            completed_date_count=0,
            failed_date_count=0,
            date_results=(),
            message=message,
        )

    provider = client or TushareClient()
    date_results: list[CNHistoryBackfillDateResult] = []
    consecutive_failures = 0
    for position, trade_date in enumerate(selected_dates, start=1):
        if position > 1 and settings.inter_date_delay_seconds > 0:
            time.sleep(settings.inter_date_delay_seconds)
        before_count = plan_before.partition_symbol_counts.get(trade_date, 0)
        try:
            rows_by_ticker = provider.fetch_cn_daily_history_bulk(
                list(symbols),
                start_date=trade_date,
                end_date=trade_date,
            )
            rows = [
                row
                for ticker_rows in rows_by_ticker.values()
                for row in ticker_rows
                if str(row.get("date") or "")[:10] == trade_date
            ]
            provider_error = str(getattr(provider, "last_error", "") or "").strip()
            if provider_error:
                raise RuntimeError(
                    "provider failed before completing the full trading-date page set: "
                    + provider_error
                )
            if not rows:
                raise RuntimeError(
                    "provider returned no rows for a known CN trading date"
                )
            parquet_paths = write_ohlcv_rows_to_lake(market="CN", rows=rows)
            after_count = count_lake_symbols_for_trade_date(
                market="CN",
                trade_date=trade_date,
            )
            fetched_symbols = len(
                {str(row.get("symbol") or "").strip().upper() for row in rows if row.get("symbol")}
            )
            completed = after_count >= plan_before.minimum_symbols_per_partition
            result = CNHistoryBackfillDateResult(
                trade_date=trade_date,
                status="success" if completed else "partial",
                symbol_count_before=before_count,
                fetched_row_count=len(rows),
                fetched_symbol_count=fetched_symbols,
                symbol_count_after=after_count,
                parquet_file_count=len(parquet_paths),
                error=None if completed else "partition remains below the configured coverage threshold",
            )
            consecutive_failures = 0 if completed else consecutive_failures + 1
        except Exception as exc:
            result = CNHistoryBackfillDateResult(
                trade_date=trade_date,
                status="failed",
                symbol_count_before=before_count,
                fetched_row_count=0,
                fetched_symbol_count=0,
                symbol_count_after=before_count,
                parquet_file_count=0,
                error=str(exc),
            )
            consecutive_failures += 1
        date_results.append(result)
        if progress_callback is not None:
            progress_callback(
                {
                    "step": "cn_history_backfill",
                    "processed_dates": position,
                    "scheduled_dates": len(selected_dates),
                    "trade_date": trade_date,
                    "date_status": result.status,
                    "remaining_pending_dates": max(
                        0,
                        len(plan_before.pending_dates) - position,
                    ),
                }
            )
        if consecutive_failures >= settings.maximum_consecutive_failures:
            break

    plan_after = build_cn_history_backfill_plan(
        config=settings,
        universe_symbols=symbols,
        trade_dates=trade_dates,
    )
    completed_count = sum(item.status == "success" for item in date_results)
    failed_count = sum(item.status != "success" for item in date_results)
    if plan_after.ready_for_data_readiness_audit:
        status = "success"
    elif completed_count:
        status = "partial"
    else:
        status = "failed"
    return CNHistoryBackfillResult(
        status=status,
        dry_run=False,
        plan_before=plan_before,
        plan_after=plan_after,
        attempted_date_count=len(date_results),
        completed_date_count=completed_count,
        failed_date_count=failed_count,
        date_results=tuple(date_results),
        message=(
            f"Processed {len(date_results)} CN history partition(s): {completed_count} reached "
            f"coverage, {failed_count} remain incomplete, and {len(plan_after.pending_dates)} "
            "target partition(s) are still pending."
        ),
    )
