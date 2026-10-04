from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from app.core.config import get_settings
from app.services.cn_market_universe import _is_supported_cn_symbol
from app.services.hithink_finance_client import HithinkFinanceClient, SHANGHAI_TZ
from app.services.market_lake import write_ohlcv_rows_to_lake


HITHINK_DAILY_DUMP_KINDS = {"daily-k", "daily-k-10d"}
HITHINK_DAILY_REQUIRED_COLUMNS = {
    "thscode",
    "date_ms",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
}


def import_hithink_market_dump(
    *,
    kind: str = "daily-k-10d",
    write_lake: bool = True,
    max_rows: int | None = None,
) -> dict:
    """Download and atomically merge an official full-market daily dump.

    The upstream file is retained as an audit artifact. Presigned URLs and the
    API key are never returned or persisted.
    """

    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind not in HITHINK_DAILY_DUMP_KINDS:
        raise ValueError("Only HiThink daily-k and daily-k-10d dumps can be imported into the OHLCV lake.")

    settings = get_settings()
    client = HithinkFinanceClient()
    if not client.is_configured():
        return {
            "status": "not_configured",
            "message": "Set PQW_HITHINK_FINANCE_API_KEY before importing HiThink market dumps.",
            "provider": client.name,
            "rows_read": 0,
            "rows_written": 0,
        }

    raw_dir = settings.raw_data_dir / "hithink_finance"
    raw_path = raw_dir / f"{normalized_kind}-latest.parquet"
    download = client.download_market_dump(kind=normalized_kind, destination=raw_path)
    frame = pl.read_parquet(raw_path)
    missing = sorted(HITHINK_DAILY_REQUIRED_COLUMNS - set(frame.columns))
    if missing:
        raise RuntimeError(f"HiThink {normalized_kind} dump is missing required columns: {', '.join(missing)}")
    if max_rows not in (None, 0):
        frame = frame.head(max(1, int(max_rows)))
    duplicate_count = frame.height - frame.unique(subset=["thscode", "date_ms"]).height
    if duplicate_count:
        frame = frame.unique(subset=["thscode", "date_ms"], keep="last")

    rows: list[dict] = []
    symbols: set[str] = set()
    dates: set[str] = set()
    symbols_by_date: dict[str, set[str]] = {}
    rejected_rows = 0
    excluded_unsupported_rows = 0
    for item in frame.iter_rows(named=True):
        try:
            ticker = client.to_internal_ticker(str(item["thscode"]))
            if not _is_supported_cn_symbol(ticker=ticker, exchange=None):
                excluded_unsupported_rows += 1
                continue
            trade_date = client._ms_to_date(item["date_ms"])
            open_price = client._to_float(item.get("open_price"))
            high_price = client._to_float(item.get("high_price"))
            low_price = client._to_float(item.get("low_price"))
            close_price = client._to_float(item.get("close_price"))
            volume = client._to_float(item.get("volume"))
            if any(value is None for value in (open_price, high_price, low_price, close_price, volume)):
                rejected_rows += 1
                continue
            rows.append(
                {
                    "date": trade_date,
                    "symbol": ticker,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": volume,
                    "adj_close": close_price,
                    "dividend": None,
                    "split_ratio": None,
                }
            )
            symbols.add(ticker)
            dates.add(trade_date)
            symbols_by_date.setdefault(trade_date, set()).add(ticker)
        except (TypeError, ValueError):
            rejected_rows += 1

    lake_paths: list[Path] = []
    if write_lake and rows:
        lake_paths = write_ohlcv_rows_to_lake(
            market="CN",
            rows=rows,
            merge_existing=True,
            provenance={"provider": "hithink", "source_reference": "hithink:cn_daily"},
        )
    status = "success" if rows and rejected_rows == 0 else "partial" if rows else "failed"
    now = datetime.now(SHANGHAI_TZ).isoformat()
    return {
        "status": status,
        "message": (
            f"Imported HiThink {normalized_kind}: {len(rows)} row(s), "
            f"{len(symbols)} symbol(s), {len(dates)} trade date(s)."
        ),
        "provider": client.name,
        "kind": normalized_kind,
        "rows_read": frame.height,
        "rows_written": len(rows) if write_lake else 0,
        "symbol_count": len(symbols),
        "trade_date_count": len(dates),
        "first_trade_date": min(dates, default=None),
        "last_trade_date": max(dates, default=None),
        "latest_symbol_count": len(symbols_by_date.get(max(dates, default=""), set())),
        "duplicate_rows_removed": duplicate_count,
        "rejected_rows": rejected_rows,
        "excluded_unsupported_rows": excluded_unsupported_rows,
        "raw_path": str(raw_path),
        "raw_bytes": int(download.get("bytes") or 0),
        "lake_paths": [str(path) for path in lake_paths],
        "request_id": download.get("request_id"),
        "completed_at": now,
    }
