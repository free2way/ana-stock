from collections.abc import Callable

from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.market_lake import list_lake_symbols
from app.services.providers import resolve_fundamental_provider
from app.services.repository import (
    FundamentalSnapshotRepository,
    PointInTimeFeatureSnapshotRepository,
    SymbolRepository,
)
from app.services.stock_selection.feature_availability import adapt_fundamental_snapshots
from app.services.ticker_format import normalize_ticker_for_market
from app.services.time_utils import app_now_iso


DEFAULT_CN_FUNDAMENTAL_BATCH_SIZE = 240
DEFAULT_HITHINK_FUNDAMENTAL_BATCH_SIZE = 20
MAX_CN_FUNDAMENTAL_BATCH_SIZE = 800


def _batch_progress_payload(
    *,
    total_tickers: int,
    offset: int,
    processed_tickers: int,
    batch_size: int,
    batch_count: int,
    failed_batch_count: int,
) -> dict:
    next_offset = min(total_tickers, offset + processed_tickers)
    return {
        "total_tickers": total_tickers,
        "offset": offset,
        "batch_size": batch_size,
        "batch_count": batch_count,
        "processed_tickers": processed_tickers,
        "next_offset": next_offset,
        "remaining_tickers": max(0, total_tickers - next_offset),
        "complete": next_offset >= total_tickers,
        "failed_batch_count": failed_batch_count,
    }


def sync_cn_fundamentals(
    tickers: list[str] | None = None,
    *,
    provider_name: str = "community",
    offset: int = 0,
    batch_size: int | None = None,
    max_batches: int | None = None,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    normalized_provider = str(provider_name or "community").strip().lower()
    provider = resolve_fundamental_provider(normalized_provider, market="CN")
    is_configured = getattr(provider, "is_configured", None)
    client = getattr(provider, "client", None)
    if callable(is_configured) and not is_configured():
        return {
            "status": "not_configured",
            "message": f"CN fundamental provider {normalized_provider} is not configured.",
            "rows_written": 0,
            "tickers": [],
        }
    if client is not None and hasattr(client, "is_configured") and not client.is_configured():
        return {
            "status": "not_configured",
            "message": "Set PQW_TUSHARE_TOKEN to enable the TuShare CN fundamental provider.",
            "rows_written": 0,
            "tickers": [],
        }

    explicit_tickers = bool(tickers)
    normalized_tickers = sorted(
        {
            normalize_ticker_for_market(ticker, "CN")
            for ticker in (tickers or [])
            if str(ticker or "").strip()
        }
    )
    if not normalized_tickers:
        normalized_tickers = sorted(list_lake_symbols(market="CN"))
    total_tickers = len(normalized_tickers)
    normalized_offset = min(max(0, int(offset or 0)), total_tickers)
    effective_batch_size = min(
        MAX_CN_FUNDAMENTAL_BATCH_SIZE,
        max(
            1,
            int(
                batch_size
                or (
                    total_tickers
                    if explicit_tickers
                    else (
                        DEFAULT_HITHINK_FUNDAMENTAL_BATCH_SIZE
                        if normalized_provider in {"hithink", "hithink_finance", "tonghuashun", "ths"}
                        else DEFAULT_CN_FUNDAMENTAL_BATCH_SIZE
                    )
                )
                or 1
            ),
        ),
    )
    normalized_max_batches = None if max_batches in (None, 0) else max(1, int(max_batches))
    selected_tickers = normalized_tickers[normalized_offset:]
    if normalized_max_batches is not None:
        selected_tickers = selected_tickers[: effective_batch_size * normalized_max_batches]
    if not selected_tickers:
        progress = _batch_progress_payload(
            total_tickers=total_tickers,
            offset=normalized_offset,
            processed_tickers=0,
            batch_size=effective_batch_size,
            batch_count=0,
            failed_batch_count=0,
        )
        return {
            "status": "success",
            "message": "CN point-in-time fundamental backfill is already complete for this universe.",
            "rows_written": 0,
            "point_in_time_values_written": 0,
            "tickers": [],
            "provider": getattr(provider, "last_source_used", normalized_provider),
            **progress,
        }
    with SessionLocal() as db:
        symbol_meta_by_ticker = SymbolRepository(db).list_overviews_for_tickers(selected_tickers)

    written = 0
    point_in_time_values_written = 0
    touched: list[str] = []
    processed_tickers = 0
    batch_count = 0
    batch_results: list[dict] = []
    failed_batches: list[dict] = []
    provider_diagnostics: list[dict] = []
    for batch_start in range(0, len(selected_tickers), effective_batch_size):
        batch = selected_tickers[batch_start : batch_start + effective_batch_size]
        batch_count += 1
        batch_error = ""
        rows: list[dict] = []
        try:
            rows = provider.fetch_snapshots(
                batch,
                metadata={ticker: symbol_meta_by_ticker.get(ticker, {}) for ticker in batch},
            )
            batch_error = str(getattr(provider, "last_error", "") or "").strip()
        except Exception as exc:
            batch_error = str(exc)
        diagnostics = dict(getattr(provider, "last_diagnostics", {}) or {})
        provider_diagnostics.append(
            {
                "batch_no": batch_count,
                "first_ticker": batch[0],
                "last_ticker": batch[-1],
                **diagnostics,
            }
        )

        batch_written = 0
        batch_values_written = 0
        if rows:
            observed_at = app_now_iso()
            with SessionLocal() as db:
                symbol_repo = SymbolRepository(db)
                fundamental_repo = FundamentalSnapshotRepository(db)
                point_in_time_repo = PointInTimeFeatureSnapshotRepository(db)
                for row in rows:
                    ticker = normalize_ticker_for_market(row["ticker"], "CN")
                    source_name = getattr(provider, "last_source_used", normalized_provider) or normalized_provider
                    symbol = symbol_repo.get_or_create_symbol(
                        SymbolCreate(
                            ticker=ticker,
                            name=row.get("name"),
                            market="CN",
                            exchange=row.get("exchange"),
                        )
                    )
                    fundamental_repo.upsert_snapshot(
                        symbol_id=symbol.id,
                        report_date=row["report_date"],
                        source=source_name,
                        listing_date=row.get("listing_date"),
                        pe_ttm=row.get("pe_ttm"),
                        dividend_yield=row.get("dividend_yield"),
                        market_cap=row.get("market_cap"),
                        roe_avg_3y=row.get("roe_avg_3y"),
                        net_profit_yoy=row.get("net_profit_yoy"),
                        revenue_yoy=row.get("revenue_yoy"),
                        debt_to_assets=row.get("debt_to_assets"),
                        data=row.get("raw_data"),
                    )
                    adapted = adapt_fundamental_snapshots(
                        (
                            {
                                **row,
                                "ticker": ticker,
                                "source": source_name,
                                "created_at": observed_at,
                                "updated_at": observed_at,
                            },
                        ),
                        market="CN",
                    )
                    base_source_record_id = str(
                        row.get("source_record_id") or f"{ticker}:{row['report_date']}"
                    )
                    feature_time_names = set((row.get("feature_times") or {}).keys())
                    for record in adapted.records:
                        _, inserted = point_in_time_repo.append_snapshot(
                            symbol_id=symbol.id,
                            feature_name=record.feature_name,
                            feature_value=record.value,
                            event_time=record.event_time.isoformat(),
                            available_time=record.available_time.isoformat(),
                            ingested_time=record.ingested_time.isoformat(),
                            source=record.source,
                            source_record_id=(
                                f"{base_source_record_id}:{record.feature_name}:"
                                f"{record.event_time.date().isoformat() if record.feature_name in feature_time_names else row['report_date']}"
                            ),
                            revision_id=record.revision_id,
                            payload={
                                "report_date": row["report_date"],
                                "provider": source_name,
                                "revision_history_preserved": bool(row.get("revision_history_preserved")),
                            },
                            commit=False,
                        )
                        batch_values_written += int(inserted)
                    batch_written += 1
                    if ticker not in touched:
                        touched.append(ticker)
                db.commit()
        processed_tickers += len(batch)
        written += batch_written
        point_in_time_values_written += batch_values_written
        batch_result = {
            "batch_no": batch_count,
            "offset": normalized_offset + batch_start,
            "first_ticker": batch[0],
            "last_ticker": batch[-1],
            "requested_tickers": len(batch),
            "snapshots_written": batch_written,
            "point_in_time_values_written": batch_values_written,
            "status": "partial" if batch_error else ("success" if rows else "empty"),
            "error": batch_error or None,
        }
        batch_results.append(batch_result)
        if batch_error or not rows:
            failed_batches.append(batch_result)
        progress = _batch_progress_payload(
            total_tickers=total_tickers,
            offset=normalized_offset,
            processed_tickers=processed_tickers,
            batch_size=effective_batch_size,
            batch_count=batch_count,
            failed_batch_count=len(failed_batches),
        )
        if progress_callback is not None:
            progress_callback(
                {
                    **progress,
                    "rows_written": written,
                    "point_in_time_values_written": point_in_time_values_written,
                    "last_batch": batch_result,
                }
            )

    progress = _batch_progress_payload(
        total_tickers=total_tickers,
        offset=normalized_offset,
        processed_tickers=processed_tickers,
        batch_size=effective_batch_size,
        batch_count=batch_count,
        failed_batch_count=len(failed_batches),
    )
    progress["scanned_through_offset"] = progress["next_offset"]
    if failed_batches:
        # Resume from the earliest incomplete batch. Replaying later successful
        # batches is safe because the point-in-time store is append-only and
        # idempotent by source-record/revision identity.
        progress["next_offset"] = min(int(item["offset"]) for item in failed_batches)
        progress["remaining_tickers"] = max(0, total_tickers - progress["next_offset"])
        progress["complete"] = False
    provider_error = "; ".join(
        str(item.get("error") or "") for item in failed_batches if item.get("error")
    )
    status = "success"
    if failed_batches or not progress["complete"]:
        status = "partial"
    if written == 0 and failed_batches:
        status = "failed"
    elif written == 0:
        status = "empty"
    return {
        "status": status,
        "message": (
            f"Synced {written} CN fundamental row(s) for {len(touched)} stock(s); "
            f"processed {progress['next_offset']}/{total_tickers} ticker(s) via "
            f"{getattr(provider, 'last_source_used', normalized_provider)}."
            + (
                f" {len(failed_batches)} batch(es) require retry from offset "
                f"{progress['next_offset']}: {provider_error or 'empty provider result'}"
                if failed_batches
                else ""
            )
        ),
        "rows_written": written,
        "point_in_time_values_written": point_in_time_values_written,
        "tickers": touched,
        "provider": getattr(provider, "last_source_used", normalized_provider),
        "provider_diagnostics": provider_diagnostics,
        "batch_results": batch_results,
        "failed_batches": failed_batches,
        **progress,
    }
