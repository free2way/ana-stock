from __future__ import annotations

import json
import csv
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.market_sync import RAW_FIELDS, merge_market_data_rows, read_raw_csv, write_raw_csv
from app.services.market_lake import write_daily_ohlcv_parquet
from app.services.normalizer import MarketDataNormalizer
from app.services.repository import PriceSyncStateRepository, SymbolRepository
from app.services.time_utils import app_now, app_now_iso


def refresh_us_grouped_daily(
    *,
    trade_date: str | None = None,
    adjusted: bool = True,
    limit: int | None = None,
    normalize: bool = False,
    persist_per_symbol: bool = False,
    write_lake: bool = True,
    write_snapshot: bool = False,
) -> dict:
    settings = get_settings()
    if not settings.polygon_api_key:
        return {
            "status": "not_configured",
            "message": "PQW_POLYGON_API_KEY is not configured. Add a Polygon API key before running US grouped daily refresh.",
            "trade_date": trade_date or _default_us_grouped_trade_date(),
            "normalize": normalize,
            "persist_per_symbol": persist_per_symbol,
            "write_lake": write_lake,
            "write_snapshot": write_snapshot,
            "success_count": 0,
            "failure_count": 0,
        }

    effective_trade_date = trade_date or _default_us_grouped_trade_date()
    started_at = time.perf_counter()
    try:
        rows = _fetch_polygon_grouped_daily(effective_trade_date, adjusted=adjusted)
    except Exception as exc:
        fetch_seconds = round(time.perf_counter() - started_at, 3)
        error_message = str(exc)
        if "today's data before end of day" in error_message.lower():
            return {
                "status": "empty",
                "message": (
                    f"Polygon grouped daily {effective_trade_date} is not available yet. "
                    "The provider reported that today's data was requested before end of day."
                ),
                "trade_date": effective_trade_date,
                "source": "polygon_grouped_daily",
                "normalize": normalize,
                "persist_per_symbol": persist_per_symbol,
                "write_lake": write_lake,
                "write_snapshot": write_snapshot,
                "success_count": 0,
                "failure_count": 0,
                "fetch_seconds": fetch_seconds,
                "error": error_message,
            }
        return {
            "status": "failed",
            "message": f"Polygon grouped daily {effective_trade_date} failed: {error_message}",
            "trade_date": effective_trade_date,
            "source": "polygon_grouped_daily",
            "normalize": normalize,
            "persist_per_symbol": persist_per_symbol,
            "write_lake": write_lake,
            "write_snapshot": write_snapshot,
            "success_count": 0,
            "failure_count": 1,
            "fetch_seconds": fetch_seconds,
            "error": error_message,
        }
    fetch_seconds = round(time.perf_counter() - started_at, 3)
    if limit is not None and limit > 0:
        rows = rows[:limit]
    if not rows:
        return {
            "status": "empty",
            "message": f"No Polygon grouped daily rows returned for {effective_trade_date}.",
            "trade_date": effective_trade_date,
            "normalize": normalize,
            "persist_per_symbol": persist_per_symbol,
            "write_lake": write_lake,
            "write_snapshot": write_snapshot,
            "success_count": 0,
            "failure_count": 0,
        }

    snapshot_path = None
    snapshot_write_seconds = 0.0
    if write_snapshot:
        snapshot_started_at = time.perf_counter()
        snapshot_path = _write_grouped_daily_snapshot(rows, trade_date=effective_trade_date)
        snapshot_write_seconds = round(time.perf_counter() - snapshot_started_at, 3)
    lake_path = None
    lake_write_seconds = 0.0
    if write_lake:
        lake_started_at = time.perf_counter()
        lake_path = write_daily_ohlcv_parquet(market="US", trade_date=effective_trade_date, rows=rows)
        lake_write_seconds = round(time.perf_counter() - lake_started_at, 3)
    if not persist_per_symbol:
        upsert_started_at = time.perf_counter()
        result = _bulk_upsert_grouped_daily_state(rows, trade_date=effective_trade_date)
        state_upsert_seconds = round(time.perf_counter() - upsert_started_at, 3)
        success_count = int(result.get("success_count") or 0)
        failure_count = int(result.get("failure_count") or 0)
        status = "success" if success_count and failure_count == 0 else "partial" if success_count else "failed"
        return {
            "status": status,
            "message": (
                f"Polygon grouped daily {effective_trade_date}: {success_count} state row(s) upserted, "
                f"{failure_count} failed."
                + (f" Snapshot written to {snapshot_path.name}." if snapshot_path else "")
            ),
            "trade_date": effective_trade_date,
            "source": "polygon_grouped_daily",
            "normalize": normalize,
            "persist_per_symbol": persist_per_symbol,
            "write_lake": write_lake,
            "write_snapshot": write_snapshot,
            "snapshot_path": str(snapshot_path) if snapshot_path else None,
            "lake_path": str(lake_path) if lake_path else None,
            "rows_returned": len(rows),
            "success_count": success_count,
            "failure_count": failure_count,
            "fetch_seconds": fetch_seconds,
            "snapshot_write_seconds": snapshot_write_seconds,
            "lake_write_seconds": lake_write_seconds,
            "state_upsert_seconds": state_upsert_seconds,
            "examples": _examples(rows),
        }

    normalizer = MarketDataNormalizer() if normalize else None
    success_count = 0
    failure_count = 0
    examples: list[dict] = []
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        sync_repo = PriceSyncStateRepository(db)
        for row in rows:
            ticker = str(row.get("symbol") or "").strip().upper()
            if not ticker or "." in ticker and ticker.endswith((".WS", ".U", ".R")):
                continue
            try:
                symbol = symbol_repo.get_or_create_symbol(SymbolCreate(ticker=ticker, name=ticker, market="US", exchange=None))
                raw_path = settings.raw_data_dir / f"{symbol.ticker}.csv"
                merged_rows = merge_market_data_rows(read_raw_csv(raw_path), [row])
                write_raw_csv(raw_path, merged_rows)
                if normalizer is not None:
                    normalized_path = settings.normalized_data_dir / f"{symbol.ticker}.csv"
                    normalizer.normalize_symbol_file(raw_path, normalized_path)
                sync_repo.upsert_state(
                    symbol_id=symbol.id,
                    provider="polygon_grouped_daily",
                    last_synced_date=effective_trade_date,
                    status="success",
                    message=(
                        f"Wrote grouped daily row for {effective_trade_date} to {raw_path.name}"
                        + (" and normalized file" if normalize else "")
                    ),
                )
                success_count += 1
                if len(examples) < 8:
                    examples.append({"ticker": symbol.ticker, "close": row.get("close"), "volume": row.get("volume")})
            except Exception as exc:
                failure_count += 1
                if len(examples) < 8:
                    examples.append({"ticker": ticker, "status": "failed", "message": str(exc)})

    status = "success" if success_count and failure_count == 0 else "partial" if success_count else "failed"
    return {
        "status": status,
        "message": f"Polygon grouped daily {effective_trade_date}: {success_count} synced, {failure_count} failed.",
        "trade_date": effective_trade_date,
        "source": "polygon_grouped_daily",
        "normalize": normalize,
        "persist_per_symbol": persist_per_symbol,
        "write_lake": write_lake,
        "write_snapshot": write_snapshot,
        "snapshot_path": str(snapshot_path) if snapshot_path else None,
        "lake_path": str(lake_path) if lake_path else None,
        "rows_returned": len(rows),
        "success_count": success_count,
        "failure_count": failure_count,
        "fetch_seconds": fetch_seconds,
        "snapshot_write_seconds": snapshot_write_seconds,
        "lake_write_seconds": lake_write_seconds,
        "examples": examples,
    }


def refresh_us_grouped_daily_range(
    *,
    start_date: str,
    end_date: str,
    adjusted: bool = True,
    limit: int | None = None,
) -> dict:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if end < start:
        start, end = end, start
    results: list[dict] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            day = current.isoformat()
            results.append(
                refresh_us_grouped_daily(
                    trade_date=day,
                    adjusted=adjusted,
                    limit=limit,
                    normalize=False,
                    persist_per_symbol=False,
                    write_lake=True,
                    write_snapshot=False,
                )
            )
        current += timedelta(days=1)
    success_count = sum(int(item.get("success_count") or 0) for item in results)
    failure_count = sum(int(item.get("failure_count") or 0) for item in results)
    successful_days = sum(1 for item in results if str(item.get("status")) == "success")
    empty_days = sum(1 for item in results if str(item.get("status")) == "empty")
    failed_days = sum(1 for item in results if str(item.get("status")) == "failed")
    status = "success" if results and failed_days == 0 else "partial" if success_count else "failed"
    return {
        "status": status,
        "message": (
            f"US lake-only grouped daily range refreshed {len(results)} trading day(s): "
            f"{successful_days} success, {empty_days} empty, {failed_days} failed, {success_count} row(s) synced."
        ),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days": len(results),
        "successful_days": successful_days,
        "empty_days": empty_days,
        "failed_days": failed_days,
        "success_count": success_count,
        "failure_count": failure_count,
        "results": results,
    }


def _write_grouped_daily_snapshot(rows: list[dict], *, trade_date: str) -> Path:
    settings = get_settings()
    path = settings.raw_data_dir / "us_grouped_daily" / f"{trade_date}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _bulk_upsert_grouped_daily_state(rows: list[dict], *, trade_date: str) -> dict:
    now = app_now_iso()
    tickers = sorted({str(row.get("symbol") or "").strip().upper() for row in rows if row.get("symbol")})
    if not tickers:
        return {"success_count": 0, "failure_count": 0}
    with SessionLocal() as db:
        db.execute(
            text(
                """
                INSERT INTO symbols (ticker, name, market, exchange, sector, industry, is_active, created_at, updated_at)
                SELECT ticker, ticker, 'US', NULL, NULL, NULL, 1, :now, :now
                FROM unnest(CAST(:tickers AS TEXT[])) AS ticker
                ON CONFLICT (ticker) DO UPDATE SET
                    market = COALESCE(symbols.market, EXCLUDED.market),
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {"tickers": tickers, "now": now},
        )
        db.execute(
            text(
                """
                INSERT INTO price_sync_state (symbol_id, provider, last_synced_date, status, message, updated_at)
                SELECT id, 'polygon_grouped_daily', :trade_date, 'success', :message, :now
                FROM symbols
                WHERE ticker = ANY(CAST(:tickers AS TEXT[]))
                ON CONFLICT (symbol_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    last_synced_date = EXCLUDED.last_synced_date,
                    status = EXCLUDED.status,
                    message = EXCLUDED.message,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "tickers": tickers,
                "trade_date": trade_date,
                "message": f"Polygon grouped daily snapshot available for {trade_date}.",
                "now": now,
            },
        )
        db.commit()
    return {"success_count": len(tickers), "failure_count": 0}


def _examples(rows: list[dict], limit: int = 8) -> list[dict]:
    return [
        {"ticker": row.get("symbol"), "close": row.get("close"), "volume": row.get("volume")}
        for row in rows[:limit]
    ]


def _fetch_polygon_grouped_daily(trade_date: str, *, adjusted: bool) -> list[dict]:
    settings = get_settings()
    endpoint = str(settings.polygon_endpoint or "https://api.polygon.io").rstrip("/")
    params = {
        "adjusted": "true" if adjusted else "false",
        "apiKey": settings.polygon_api_key or "",
    }
    url = f"{endpoint}/v2/aggs/grouped/locale/us/market/stocks/{trade_date}?{urlencode(params)}"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.reason
        try:
            body = exc.read().decode("utf-8")
            if body:
                payload = json.loads(body)
                detail = payload.get("error") or payload.get("message") or detail
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code} {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    results = payload.get("results") or []
    rows: list[dict] = []
    for item in results:
        ticker = str(item.get("T") or "").strip().upper()
        if not ticker:
            continue
        rows.append(
            {
                "date": trade_date,
                "symbol": ticker,
                "open": item.get("o"),
                "high": item.get("h"),
                "low": item.get("l"),
                "close": item.get("c"),
                "volume": item.get("v"),
                "adj_close": item.get("c"),
                "dividend": None,
                "split_ratio": None,
            }
        )
    rows.sort(key=lambda row: row["symbol"])
    return rows


def _default_us_grouped_trade_date() -> str:
    # In Asia/Shanghai, after the U.S. close the latest grouped daily date is usually the previous U.S. weekday.
    candidate = app_now().date() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate.isoformat()
