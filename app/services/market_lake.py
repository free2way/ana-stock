from __future__ import annotations

import threading
import time
from pathlib import Path
import re
from datetime import datetime

import duckdb
import polars as pl

from app.core.config import get_settings


LAKE_OHLCV_COLUMNS = [
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adj_close",
    "dividend",
    "split_ratio",
]

LAKE_PARQUET_CHUNK_SIZE = 48
LAKE_DUCKDB_MAX_CONCURRENT_READS = 2
LAKE_DUCKDB_MAX_RETRIES = 3
LAKE_MIN_PARQUET_BYTES = 64
LAKE_FILE_LIST_CACHE_TTL_SECONDS = 15.0
_LAKE_DUCKDB_READ_SEMAPHORE = threading.BoundedSemaphore(LAKE_DUCKDB_MAX_CONCURRENT_READS)
_LAKE_BAD_FILE_REGISTRY_LOCK = threading.Lock()
_LAKE_BAD_FILE_REGISTRY: dict[str, dict] = {}
_LAKE_FILE_LIST_CACHE_LOCK = threading.Lock()
_LAKE_FILE_LIST_CACHE: dict[str, dict] = {}
_LAKE_QUERY_STATS_LOCK = threading.Lock()
_LAKE_QUERY_STATS: dict[str, dict] = {}


def market_lake_root() -> Path:
    return get_settings().data_dir / "lake"


def _invalidate_lake_file_cache(market: str | None = None) -> None:
    normalized = str(market or "").strip().lower()
    with _LAKE_FILE_LIST_CACHE_LOCK:
        if normalized:
            _LAKE_FILE_LIST_CACHE.pop(normalized, None)
        else:
            _LAKE_FILE_LIST_CACHE.clear()


def write_daily_ohlcv_parquet(*, market: str, trade_date: str, rows: list[dict], merge_existing: bool = False) -> Path:
    market_code = str(market or "").strip().lower() or "unknown"
    path = market_lake_root() / f"{market_code}_daily" / f"date={trade_date}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_rows = [_normalize_ohlcv_row(row, trade_date=trade_date) for row in rows]
    frame = pl.DataFrame(normalized_rows, schema=LAKE_OHLCV_COLUMNS, orient="row")
    if merge_existing and path.exists():
        try:
            existing = pl.read_parquet(path)
            frame = pl.concat([existing, frame], how="vertical_relaxed")
            frame = frame.unique(subset=["date", "symbol"], keep="last").sort(["date", "symbol"])
        except Exception:
            pass
    frame.write_parquet(path, compression="zstd")
    _invalidate_lake_file_cache(market_code)
    return path


def us_daily_parquet_glob() -> str:
    return str(market_lake_root() / "us_daily" / "date=*" / "*.parquet")


def daily_parquet_glob(market: str) -> str:
    market_code = str(market or "").strip().lower() or "unknown"
    return str(market_lake_root() / f"{market_code}_daily" / "date=*" / "*.parquet")


def query_us_daily_summary(*, trade_date: str | None = None, limit: int = 10) -> dict:
    parquet_files = _all_parquet_files("US")
    if not parquet_files:
        return {"status": "empty", "message": "No U.S. daily Parquet lake files found.", "rows": []}
    where_clause = "WHERE date = ?" if trade_date else ""
    params = [trade_date] if trade_date else []
    sql = f"""
        SELECT
            date,
            symbol,
            close,
            volume,
            close * volume AS dollar_volume
        FROM read_parquet(?, hive_partitioning = true)
        {where_clause}
        ORDER BY dollar_volume DESC NULLS LAST
        LIMIT {max(1, int(limit))}
    """
    rows, columns = _duckdb_fetchall(sql, [parquet_files, *params], label="lake_us_daily_summary")
    return {
        "status": "success",
        "trade_date": trade_date,
        "rows": [_json_ready_row(dict(zip(columns, row, strict=False))) for row in rows],
        "count": len(rows),
    }


def query_lake_daily_movers(
    *,
    market: str,
    lookback_dates: int = 12,
    top_n_per_date: int = 20,
    min_return_pct: float = 3.0,
    min_dollar_volume: float = 0.0,
) -> list[dict]:
    market_code = str(market or "").strip().upper()
    parquet_files = _recent_parquet_files(market_code, limit=max(40, int(lookback_dates) + 8))
    if market_code not in {"CN", "US"} or not parquet_files:
        return []
    sql = f"""
        WITH base AS (
            SELECT
                CAST(date AS DATE) AS trade_date,
                symbol,
                close,
                volume,
                close * volume AS dollar_volume,
                LAG(close) OVER (PARTITION BY symbol ORDER BY CAST(date AS DATE)) AS prev_close
            FROM read_parquet(?, hive_partitioning = true)
            WHERE close IS NOT NULL
        ),
        enriched AS (
            SELECT
                trade_date,
                symbol,
                close,
                volume,
                dollar_volume,
                ((close / NULLIF(prev_close, 0)) - 1.0) * 100.0 AS return_pct
            FROM base
            WHERE prev_close IS NOT NULL
              AND close IS NOT NULL
              AND (? <= 0 OR COALESCE(dollar_volume, 0) >= ?)
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY trade_date
                    ORDER BY return_pct DESC NULLS LAST, dollar_volume DESC NULLS LAST, symbol ASC
                ) AS rn
            FROM enriched
            WHERE return_pct >= ?
        )
        SELECT
            CAST(trade_date AS VARCHAR) AS trade_date,
            symbol,
            close,
            volume,
            dollar_volume,
            return_pct
        FROM ranked
        WHERE rn <= {max(1, int(top_n_per_date))}
        ORDER BY trade_date DESC, return_pct DESC NULLS LAST, dollar_volume DESC NULLS LAST
        LIMIT {max(1, int(lookback_dates) * max(1, int(top_n_per_date)))}
    """
    rows, columns = _duckdb_fetchall(
        sql,
        [parquet_files, float(min_dollar_volume), float(min_dollar_volume), float(min_return_pct)],
        label="lake_daily_movers",
    )
    return [_json_ready_row(dict(zip(columns, row, strict=False))) for row in rows]


def load_lake_price_history(*, market: str, ticker: str, limit: int = 120) -> list[dict]:
    market_code = str(market or "").strip().upper()
    symbol = str(ticker or "").strip().upper()
    parquet_files = _recent_parquet_files(market_code, limit=max(20, int(limit) * 2))
    if market_code not in {"CN", "US"} or not symbol or not parquet_files:
        return []
    sql = f"""
        SELECT
            CAST(date AS VARCHAR) AS date,
            symbol,
            open,
            high,
            low,
            close,
            volume,
            adj_close
        FROM read_parquet(?, hive_partitioning = true)
        WHERE symbol = ?
        ORDER BY CAST(date AS DATE) DESC
    """
    history: list[dict] = []
    columns: list[str] = []
    for file_chunk in _chunked_paths(parquet_files):
        rows, chunk_columns = _duckdb_fetchall(sql, [file_chunk, symbol], label="lake_price_history")
        if not columns:
            columns = chunk_columns
        history.extend(_json_ready_row(dict(zip(columns, row, strict=False))) for row in rows)
    history.sort(key=lambda item: item.get("date") or "", reverse=True)
    history = history[: max(1, int(limit))]
    history.sort(key=lambda item: item.get("date") or "")
    return history


def load_lake_latest_closes(*, market: str, tickers: list[str]) -> dict[str, float | None]:
    market_code = str(market or "").strip().upper()
    parquet_files = _recent_parquet_files(market_code, limit=180)
    normalized = []
    for ticker in tickers:
        symbol = str(ticker or "").strip().upper()
        if symbol and symbol not in normalized:
            normalized.append(symbol)
    if market_code not in {"CN", "US"} or not normalized or not parquet_files:
        return {}

    payload: dict[str, float | None] = {}
    for start in range(0, len(normalized), 500):
        ticker_chunk = normalized[start : start + 500]
        placeholders = ", ".join("?" for _ in ticker_chunk)
        sql = f"""
            WITH ranked AS (
                SELECT
                    symbol,
                    close,
                    adj_close,
                    ROW_NUMBER() OVER (
                        PARTITION BY symbol
                        ORDER BY CAST(date AS DATE) DESC
                    ) AS row_num
                FROM read_parquet(?, hive_partitioning = true)
                WHERE symbol IN ({placeholders})
            )
            SELECT symbol, close, adj_close
            FROM ranked
            WHERE row_num = 1
        """
        rows, _columns = _duckdb_fetchall(
            sql,
            [parquet_files, *ticker_chunk],
            label="lake_latest_closes",
        )
        for symbol, close_value, adj_close_value in rows:
            latest_value = adj_close_value if adj_close_value not in (None, "") else close_value
            try:
                payload[str(symbol or "").strip().upper()] = (
                    None if latest_value in (None, "") else float(latest_value)
                )
            except (TypeError, ValueError):
                payload[str(symbol or "").strip().upper()] = None
    return payload


def get_latest_lake_trade_date(*, market: str, ticker: str | None = None) -> str | None:
    market_code = str(market or "").strip().upper()
    parquet_files = _recent_parquet_files(market_code, limit=180)
    if market_code not in {"CN", "US"} or not parquet_files:
        return None
    normalized_ticker = str(ticker or "").strip().upper()
    where_clause = "WHERE symbol = ?" if normalized_ticker else ""
    params: list[object] = [parquet_files]
    if normalized_ticker:
        params.append(normalized_ticker)
    sql = f"""
        SELECT CAST(MAX(CAST(date AS DATE)) AS VARCHAR) AS trade_date
        FROM read_parquet(?, hive_partitioning = true)
        {where_clause}
    """
    rows, _columns = _duckdb_fetchall(sql, params, label="lake_latest_trade_date")
    if not rows:
        return None
    value = rows[0][0] if rows[0] else None
    return str(value or "").strip() or None


def count_lake_symbols_for_trade_date(*, market: str, trade_date: str) -> int:
    market_code = str(market or "").strip().upper()
    normalized_trade_date = str(trade_date or "").strip()
    if market_code not in {"CN", "US"} or not normalized_trade_date:
        return 0
    path = market_lake_root() / f"{market_code.lower()}_daily" / f"date={normalized_trade_date}" / "part.parquet"
    if not path.exists():
        return 0
    sql = """
        SELECT COUNT(DISTINCT symbol) AS symbol_count
        FROM read_parquet(?, hive_partitioning = true)
    """
    rows, _columns = _duckdb_fetchall(sql, [[str(path)]], label="lake_trade_date_symbol_count")
    if not rows:
        return 0
    value = rows[0][0] if rows[0] else 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _recent_parquet_files(market: str, *, limit: int) -> list[str]:
    return _all_parquet_files(market)[: max(1, int(limit))]


def _all_parquet_files(market: str) -> list[str]:
    market_code = str(market or "").strip().lower()
    now = time.monotonic()
    with _LAKE_FILE_LIST_CACHE_LOCK:
        cached = _LAKE_FILE_LIST_CACHE.get(market_code)
        if cached and (now - float(cached.get("fetched_at") or 0.0)) < LAKE_FILE_LIST_CACHE_TTL_SECONDS:
            return list(cached.get("files") or [])
    market_dir = market_lake_root() / f"{market_code}_daily"
    files = sorted(
        (
            path
            for path in market_dir.glob("date=*/*.parquet")
            if path.is_file() and path.stat().st_size >= LAKE_MIN_PARQUET_BYTES
        ),
        key=lambda path: path.parent.name,
        reverse=True,
    )
    normalized_files = [str(path) for path in files]
    with _LAKE_FILE_LIST_CACHE_LOCK:
        _LAKE_FILE_LIST_CACHE[market_code] = {
            "files": normalized_files,
            "fetched_at": now,
        }
    return normalized_files


def _chunked_paths(paths: list[str], *, chunk_size: int = LAKE_PARQUET_CHUNK_SIZE) -> list[list[str]]:
    size = max(1, int(chunk_size))
    return [paths[index : index + size] for index in range(0, len(paths), size)]


def _is_too_many_open_files_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "too many open files" in message or "errno 24" in message


def _extract_invalid_parquet_path(exc: Exception) -> str | None:
    message = str(exc)
    lowered = message.lower()
    if "parquet" in lowered:
        match = re.search(r"File '([^']+\.parquet)'", message)
        if match:
            return str(match.group(1) or "").strip() or None
        match = re.search(r"'([^']+\.parquet)'", message)
        if match:
            return str(match.group(1) or "").strip() or None
    match = re.search(r"file:\s*([^,\s]+\.parquet)", message, re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip() or None
    return None


def _record_bad_parquet_path(path: str, exc: Exception) -> None:
    normalized = str(path or "").strip()
    if not normalized:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with _LAKE_BAD_FILE_REGISTRY_LOCK:
        existing = _LAKE_BAD_FILE_REGISTRY.get(normalized) or {}
        _LAKE_BAD_FILE_REGISTRY[normalized] = {
            "path": normalized,
            "count": int(existing.get("count") or 0) + 1,
            "first_seen_at": str(existing.get("first_seen_at") or now),
            "last_seen_at": now,
            "last_error": str(exc).splitlines()[0][:240],
        }


def _bad_file_registry_snapshot() -> list[dict]:
    with _LAKE_BAD_FILE_REGISTRY_LOCK:
        rows = list(_LAKE_BAD_FILE_REGISTRY.values())
    rows.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("path") or "")))
    return rows


def _record_lake_query_stat(
    *,
    label: str,
    duration_ms: float,
    row_count: int,
    file_count: int,
    status: str,
    attempt_count: int,
    error: Exception | None = None,
) -> None:
    normalized_label = str(label or "duckdb_query").strip() or "duckdb_query"
    now = datetime.now().isoformat(timespec="seconds")
    with _LAKE_QUERY_STATS_LOCK:
        existing = _LAKE_QUERY_STATS.get(normalized_label) or {
            "label": normalized_label,
            "count": 0,
            "error_count": 0,
            "last_error": None,
            "last_status": None,
            "last_duration_ms": None,
            "last_row_count": None,
            "last_file_count": None,
            "last_attempt_count": None,
            "avg_duration_ms": 0.0,
            "max_duration_ms": 0.0,
            "last_run_at": None,
        }
        count = int(existing.get("count") or 0) + 1
        prior_avg = float(existing.get("avg_duration_ms") or 0.0)
        avg_duration_ms = ((prior_avg * (count - 1)) + float(duration_ms)) / count
        error_count = int(existing.get("error_count") or 0) + (1 if status != "success" else 0)
        _LAKE_QUERY_STATS[normalized_label] = {
            **existing,
            "count": count,
            "error_count": error_count,
            "last_error": str(error).splitlines()[0][:240] if error else None,
            "last_status": status,
            "last_duration_ms": round(float(duration_ms), 2),
            "last_row_count": int(row_count),
            "last_file_count": int(file_count),
            "last_attempt_count": int(attempt_count),
            "avg_duration_ms": round(avg_duration_ms, 2),
            "max_duration_ms": round(max(float(existing.get("max_duration_ms") or 0.0), float(duration_ms)), 2),
            "last_run_at": now,
        }


def _lake_query_stats_snapshot() -> list[dict]:
    with _LAKE_QUERY_STATS_LOCK:
        rows = list(_LAKE_QUERY_STATS.values())
    rows.sort(
        key=lambda item: (
            -float(item.get("last_duration_ms") or 0.0),
            -int(item.get("error_count") or 0),
            str(item.get("label") or ""),
        )
    )
    return rows


def _lake_file_cache_snapshot() -> list[dict]:
    now = time.monotonic()
    with _LAKE_FILE_LIST_CACHE_LOCK:
        rows = [
            {
                "market": market_code.upper(),
                "file_count": len(list(payload.get("files") or [])),
                "age_seconds": round(max(0.0, now - float(payload.get("fetched_at") or 0.0)), 2),
            }
            for market_code, payload in _LAKE_FILE_LIST_CACHE.items()
        ]
    rows.sort(key=lambda item: str(item.get("market") or ""))
    return rows


def _duckdb_fetchall(sql: str, params: list | tuple, *, label: str = "duckdb_query") -> tuple[list[tuple], list[str]]:
    last_error: Exception | None = None
    normalized_params = list(params)
    initial_file_count = len(normalized_params[0]) if normalized_params and isinstance(normalized_params[0], list) else 0
    started_at = time.perf_counter()
    for attempt in range(LAKE_DUCKDB_MAX_RETRIES):
        try:
            with _LAKE_DUCKDB_READ_SEMAPHORE:
                with duckdb.connect(database=":memory:") as connection:
                    rows = connection.execute(sql, normalized_params).fetchall()
                    columns = [item[0] for item in connection.description]
            _record_lake_query_stat(
                label=label,
                duration_ms=(time.perf_counter() - started_at) * 1000.0,
                row_count=len(rows),
                file_count=len(normalized_params[0]) if normalized_params and isinstance(normalized_params[0], list) else initial_file_count,
                status="success",
                attempt_count=attempt + 1,
            )
            return rows, columns
        except Exception as exc:
            last_error = exc
            bad_path = _extract_invalid_parquet_path(exc)
            if bad_path and normalized_params and isinstance(normalized_params[0], list):
                _record_bad_parquet_path(bad_path, exc)
                filtered_paths = [path for path in normalized_params[0] if str(path) != bad_path]
                if filtered_paths and len(filtered_paths) < len(normalized_params[0]):
                    normalized_params[0] = filtered_paths
                    continue
                if not filtered_paths:
                    _record_lake_query_stat(
                        label=label,
                        duration_ms=(time.perf_counter() - started_at) * 1000.0,
                        row_count=0,
                        file_count=0,
                        status="skipped_all_bad_files",
                        attempt_count=attempt + 1,
                        error=exc,
                    )
                    return [], []
            if not _is_too_many_open_files_error(exc) or attempt >= LAKE_DUCKDB_MAX_RETRIES - 1:
                _record_lake_query_stat(
                    label=label,
                    duration_ms=(time.perf_counter() - started_at) * 1000.0,
                    row_count=0,
                    file_count=len(normalized_params[0]) if normalized_params and isinstance(normalized_params[0], list) else initial_file_count,
                    status="error",
                    attempt_count=attempt + 1,
                    error=exc,
                )
                raise
            time.sleep(0.2 * (attempt + 1))
    if last_error is not None:
        _record_lake_query_stat(
            label=label,
            duration_ms=(time.perf_counter() - started_at) * 1000.0,
            row_count=0,
            file_count=len(normalized_params[0]) if normalized_params and isinstance(normalized_params[0], list) else initial_file_count,
            status="error",
            attempt_count=LAKE_DUCKDB_MAX_RETRIES,
            error=last_error,
        )
        raise last_error
    return [], []


def load_lake_rows(*, markets: list[str] | None = None, tickers: set[str] | None = None, limit_per_symbol: int | None = None) -> list[dict]:
    market_codes = [
        str(market or "").strip().upper()
        for market in (markets or ["CN", "US"])
        if str(market or "").strip().upper() in {"CN", "US"}
    ] or ["CN", "US"]
    normalized_tickers = {str(ticker or "").strip().upper() for ticker in (tickers or set()) if str(ticker or "").strip()}
    all_rows: list[dict] = []
    for market_code in market_codes:
        parquet_files = _all_parquet_files(market_code)
        if not parquet_files:
            continue
        ticker_filter = "WHERE symbol = ANY(?)" if normalized_tickers else ""
        sql = f"""
            SELECT
                CAST(date AS VARCHAR) AS date,
                symbol,
                open,
                high,
                low,
                close,
                volume,
                adj_close
            FROM read_parquet(?, hive_partitioning = true)
            {ticker_filter}
            ORDER BY symbol, CAST(date AS DATE) DESC
        """
        columns: list[str] = []
        for file_chunk in _chunked_paths(parquet_files):
            params = [file_chunk]
            if normalized_tickers:
                params.append(sorted(normalized_tickers))
            rows, chunk_columns = _duckdb_fetchall(sql, params, label="lake_load_rows")
            if not columns:
                columns = chunk_columns
            all_rows.extend(_json_ready_row(dict(zip(columns, row, strict=False))) for row in rows)
    if limit_per_symbol is not None and int(limit_per_symbol) > 0:
        limited_rows: list[dict] = []
        counts: dict[str, int] = {}
        for row in all_rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            used = counts.get(symbol, 0)
            if used >= int(limit_per_symbol):
                continue
            limited_rows.append(row)
            counts[symbol] = used + 1
        all_rows = limited_rows
    all_rows.sort(key=lambda row: (row.get("symbol") or "", row.get("date") or ""))
    return all_rows


def list_lake_symbols(*, market: str) -> set[str]:
    market_code = str(market or "").strip().upper()
    parquet_files = _all_parquet_files(market_code)
    if market_code not in {"CN", "US"} or not parquet_files:
        return set()
    sql = """
        SELECT DISTINCT symbol
        FROM read_parquet(?, hive_partitioning = true)
        WHERE symbol IS NOT NULL
    """
    symbols: set[str] = set()
    for file_chunk in _chunked_paths(parquet_files):
        rows, _ = _duckdb_fetchall(sql, [file_chunk], label="lake_list_symbols")
        symbols.update(str(row[0] or "").strip().upper() for row in rows if row and row[0])
    return symbols


def write_ohlcv_rows_to_lake(*, market: str, rows: list[dict], merge_existing: bool = False) -> list[Path]:
    rows_by_date: dict[str, list[dict]] = {}
    for row in rows:
        trade_date = str(row.get("date") or "").strip()
        if not trade_date:
            continue
        rows_by_date.setdefault(trade_date, []).append(row)
    return [
        write_daily_ohlcv_parquet(market=market, trade_date=trade_date, rows=date_rows, merge_existing=merge_existing)
        for trade_date, date_rows in sorted(rows_by_date.items())
    ]


def screen_us_lake_momentum(*, trade_date: str | None = None, limit: int = 160, min_dollar_volume: float = 1_000_000.0) -> list[dict]:
    return screen_lake_momentum(market="US", trade_date=trade_date, limit=limit, min_dollar_volume=min_dollar_volume)


def screen_cn_lake_momentum(*, trade_date: str | None = None, limit: int = 160, min_dollar_volume: float = 10_000_000.0) -> list[dict]:
    return screen_lake_momentum(market="CN", trade_date=trade_date, limit=limit, min_dollar_volume=min_dollar_volume)


def screen_lake_momentum(*, market: str, trade_date: str | None = None, limit: int = 160, min_dollar_volume: float = 1_000_000.0) -> list[dict]:
    market_code = str(market or "").strip().upper() or "US"
    parquet_files = _recent_parquet_files(market_code, limit=260)
    if not parquet_files:
        return []
    date_filter = "WHERE date <= ?" if trade_date else ""
    params = [trade_date] if trade_date else []
    sql = f"""
        WITH base AS (
            SELECT
                CAST(date AS DATE) AS trade_date,
                symbol,
                high,
                low,
                close,
                volume,
                close * volume AS dollar_volume
            FROM read_parquet(?, hive_partitioning = true)
            {date_filter}
        ),
        enriched AS (
            SELECT
                trade_date,
                symbol,
                high,
                low,
                close,
                volume,
                dollar_volume,
                close / NULLIF(LAG(close, 5) OVER (PARTITION BY symbol ORDER BY trade_date), 0) - 1 AS momentum_5,
                close / NULLIF(LAG(close, 20) OVER (PARTITION BY symbol ORDER BY trade_date), 0) - 1 AS momentum_20,
                AVG(volume) OVER (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                ) AS avg_volume_20,
                AVG(close) OVER (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                ) AS ma20,
                AVG(close) OVER (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                ) AS ma60,
                MAX(high) OVER (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN 9 PRECEDING AND CURRENT ROW
                ) AS recent_high_10,
                MAX(high) OVER (
                    PARTITION BY symbol ORDER BY trade_date
                    ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                ) AS recent_high_20,
                ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
            FROM base
        )
        SELECT
            trade_date AS date,
            symbol,
            close,
            volume,
            dollar_volume,
            momentum_5,
            momentum_20,
            CASE WHEN avg_volume_20 > 0 THEN volume / avg_volume_20 ELSE NULL END AS volume_ratio,
            ma20,
            ma60,
            CASE
                WHEN recent_high_10 > 0 THEN ((recent_high_10 - close) / recent_high_10) * 100.0
                ELSE NULL
            END AS pullback_depth_pct,
            CASE
                WHEN recent_high_20 > 0 THEN ((recent_high_20 - close) / recent_high_20) * 100.0
                ELSE NULL
            END AS distance_to_breakout_pct
        FROM enriched
        WHERE rn = 1
          AND close IS NOT NULL
          AND volume IS NOT NULL
          AND dollar_volume >= ?
        ORDER BY
          COALESCE(momentum_20, 0) DESC,
          COALESCE(momentum_5, 0) DESC,
          dollar_volume DESC
        LIMIT {max(1, int(limit))}
    """
    rows, columns = _duckdb_fetchall(
        sql,
        [parquet_files, *params, float(min_dollar_volume)],
        label="lake_screen_momentum",
    )
    results: list[dict] = []
    for index, row in enumerate(rows, start=1):
        item = _json_ready_row(dict(zip(columns, row, strict=False)))
        trend_score = _lake_trend_score(item)
        item.update(
            {
                "ticker": item.get("symbol"),
                "name": item.get("symbol"),
                "market": market_code,
                "latest_close": item.get("close"),
                "trend_score": trend_score,
                "action_label": _lake_action_label(trend_score, item),
                "action_summary": _lake_action_summary(item, trend_score),
                "selection_reason": _lake_selection_reason(item),
                "risk_flags": _lake_risk_flags(item),
                "rank_value": index,
                "snapshot_score": round(
                    trend_score
                    + max(0.0, min(25.0, float(item.get("momentum_20") or 0.0) * 100.0))
                    + max(0.0, min(12.0, float(item.get("volume_ratio") or 1.0) * 2.0)),
                    2,
                ),
            }
        )
        results.append(item)
    return results


def _normalize_ohlcv_row(row: dict, *, trade_date: str) -> dict:
    return {
        "date": str(row.get("date") or trade_date),
        "symbol": str(row.get("symbol") or "").strip().upper(),
        "open": _as_float(row.get("open")),
        "high": _as_float(row.get("high")),
        "low": _as_float(row.get("low")),
        "close": _as_float(row.get("close")),
        "volume": _as_float(row.get("volume")),
        "adj_close": _as_float(row.get("adj_close")),
        "dividend": _as_float(row.get("dividend")),
        "split_ratio": _as_float(row.get("split_ratio")),
    }


def _as_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_ready_row(row: dict) -> dict:
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}


def _lake_trend_score(row: dict) -> int:
    score = 50.0
    momentum_5 = float(row.get("momentum_5") or 0.0)
    momentum_20 = float(row.get("momentum_20") or 0.0)
    volume_ratio = float(row.get("volume_ratio") or 1.0)
    pullback_depth_pct = float(row.get("pullback_depth_pct") or 0.0)
    close = row.get("close")
    ma20 = row.get("ma20")
    ma60 = row.get("ma60")
    score += max(-12.0, min(12.0, momentum_5 * 120.0))
    score += max(-18.0, min(18.0, momentum_20 * 90.0))
    score += max(-6.0, min(8.0, (volume_ratio - 1.0) * 8.0))
    score -= max(0.0, min(18.0, (pullback_depth_pct - 3.0) * 1.25))
    if pullback_depth_pct >= 6.0 and momentum_5 <= 0.02:
        score -= 6.0
    if pullback_depth_pct >= 8.0 and momentum_20 >= 0.18 and volume_ratio < 1.05:
        # Strong 20d momentum alone is not enough if the stock already rolled over
        # from a recent high and participation is fading.
        score -= 8.0
    if close is not None and ma20 is not None and float(close) > float(ma20):
        score += 8.0
    if close is not None and ma60 is not None and float(close) > float(ma60):
        score += 8.0
    return int(max(1, min(99, round(score))))


def _lake_action_label(score: int, row: dict | None = None) -> str:
    pullback_depth_pct = float((row or {}).get("pullback_depth_pct") or 0.0)
    momentum_5 = float((row or {}).get("momentum_5") or 0.0)
    volume_ratio = float((row or {}).get("volume_ratio") or 1.0)
    if score >= 75 and (pullback_depth_pct >= 8.0 or (pullback_depth_pct >= 6.0 and volume_ratio < 1.0 and momentum_5 <= 0.02)):
        return "WATCH"
    if score >= 75:
        return "BUY"
    if score >= 60 and pullback_depth_pct >= 12.0 and momentum_5 <= 0.02:
        return "HOLD"
    if score >= 60:
        return "WATCH"
    if score <= 35:
        return "AVOID"
    return "HOLD"


def _lake_action_summary(row: dict, score: int) -> str:
    summary = (
        f"Lake momentum score {score}; "
        f"20d momentum {_fmt_pct(row.get('momentum_20'))}, "
        f"5d momentum {_fmt_pct(row.get('momentum_5'))}, "
        f"volume ratio {float(row.get('volume_ratio') or 0.0):.2f}, "
        f"pullback {float(row.get('pullback_depth_pct') or 0.0):.1f}%."
    )
    risk_flags = _lake_risk_flags(row)
    if "rolled-over-after-spike" in risk_flags:
        summary += " Recent momentum has faded after a sharp spike, so this should stay watch-only."
    elif "do-not-chase" in risk_flags:
        summary += " Price is extended versus the recent setup; avoid chasing the close."
    return summary


def _lake_selection_reason(row: dict) -> str:
    reason = (
        f"DuckDB Parquet scan: dollar volume {float(row.get('dollar_volume') or 0.0):.0f}, "
        f"20d momentum {_fmt_pct(row.get('momentum_20'))}, "
        f"5d momentum {_fmt_pct(row.get('momentum_5'))}, "
        f"pullback {float(row.get('pullback_depth_pct') or 0.0):.1f}% from the 10d high."
    )
    risk_flags = _lake_risk_flags(row)
    if "rolled-over-after-spike" in risk_flags:
        reason += " Treat it as a faded momentum setup instead of a fresh breakout."
    elif "do-not-chase" in risk_flags:
        reason += " The setup is still extended, so wait for a cleaner reset."
    return reason


def _lake_risk_flags(row: dict) -> list[str]:
    flags: list[str] = []
    pullback_depth_pct = float(row.get("pullback_depth_pct") or 0.0)
    momentum_5 = float(row.get("momentum_5") or 0.0)
    momentum_20 = float(row.get("momentum_20") or 0.0)
    volume_ratio = float(row.get("volume_ratio") or 1.0)
    if pullback_depth_pct >= 8.0:
        flags.append("drawdown-risk")
    if pullback_depth_pct >= 12.0 and momentum_20 >= 0.18 and momentum_5 <= 0.03:
        flags.append("rolled-over-after-spike")
    if pullback_depth_pct >= 6.0 and volume_ratio < 1.0:
        flags.append("do-not-chase")
    return flags


def _fmt_pct(value) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def lake_file_health_summary() -> dict:
    issues: list[dict] = []
    root = market_lake_root()
    for market_code in ("cn", "us"):
        market_dir = root / f"{market_code}_daily"
        small_files = [
            path
            for path in market_dir.glob("date=*/*.parquet")
            if path.is_file() and path.stat().st_size < LAKE_MIN_PARQUET_BYTES
        ]
        if small_files:
            issues.append(
                {
                    "market": market_code.upper(),
                    "issue": "small_parquet",
                    "count": len(small_files),
                    "examples": [str(path) for path in small_files[:5]],
                }
            )
    skipped_runtime = _bad_file_registry_snapshot()
    if skipped_runtime:
        issues.append(
            {
                "market": "MULTI",
                "issue": "runtime_skipped_parquet",
                "count": len(skipped_runtime),
                "examples": [str(item.get("path") or "") for item in skipped_runtime[:5]],
                "rows": skipped_runtime[:20],
            }
        )
    return {
        "status": "healthy" if not issues else "warning",
        "issues": issues,
        "issue_count": sum(int(item.get("count") or 0) for item in issues),
        "runtime": {
            "query_stats": _lake_query_stats_snapshot()[:20],
            "file_cache": _lake_file_cache_snapshot(),
        },
    }
