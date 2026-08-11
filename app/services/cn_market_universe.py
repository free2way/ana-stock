import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from urllib.parse import urlencode

from sqlalchemy import text

from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.models.tables import PriceSyncState
from app.services.market_lake import write_ohlcv_rows_to_lake
from app.services.market_sync import sync_market_data
from app.services.market_freshness import latest_completed_market_date
from app.services.repository import MarketRefreshBatchRepository, SymbolRepository
from app.services.technical_snapshot_cache import rebuild_technical_snapshots
from app.services.time_utils import app_now_iso
from app.services.tushare_client import TushareClient


_EASTMONEY_CN_SPOT_URL = "http://82.push2.eastmoney.com/api/qt/clist/get"
_EASTMONEY_CN_SPOT_FIELDS = "f2,f5,f12,f15,f16,f17"
_TENCENT_CN_QUOTE_URL = "https://qt.gtimg.cn/q="


def _is_supported_cn_symbol(*, ticker: str | None, exchange: str | None) -> bool:
    normalized_ticker = str(ticker or "").strip().upper()
    normalized_exchange = str(exchange or "").strip().upper()
    if not normalized_ticker:
        return False
    if normalized_ticker.endswith(".BJ"):
        return False
    if normalized_exchange in {"BSE", "BJ"}:
        return False
    return True


def _to_float(value) -> float | None:
    try:
        if value in (None, "-", ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _eastmoney_cn_spot_rows(*, tickers: list[str], trade_date: str) -> list[dict]:
    """Fetch current-day A-share OHLCV rows from Eastmoney push2.

    This endpoint is intentionally used only as a same-day fallback. It returns
    the latest market snapshot, not historical bars, so callers must pass the
    completed trading date that the snapshot should represent.
    """

    supported = {str(ticker or "").strip().upper() for ticker in tickers if str(ticker or "").strip()}
    if not supported:
        return []

    rows: list[dict] = []
    page = 1
    page_size = 100
    fetched = 0
    total: int | None = None
    while True:
        params = {
            "pn": page,
            "pz": page_size,
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
            "fields": _EASTMONEY_CN_SPOT_FIELDS,
        }
        url = f"{_EASTMONEY_CN_SPOT_URL}?{urlencode(params)}"
        response = subprocess.run(
            ["curl", "-sL", "--retry", "4", "--retry-all-errors", "--retry-delay", "1", "--max-time", "30", url],
            capture_output=True,
            check=True,
            text=True,
            timeout=35,
        )
        payload = json.loads(response.stdout)
        data = payload.get("data") or {}
        if total is None:
            total = int(data.get("total") or 0)
        batch = list(data.get("diff") or [])
        if not batch:
            break
        fetched += len(batch)
        for raw in batch:
            code = str(raw.get("f12") or "").strip()
            ticker = f"{code}.SZ" if f"{code}.SZ" in supported else f"{code}.SS" if f"{code}.SS" in supported else ""
            close = _to_float(raw.get("f2"))
            if not ticker or close is None:
                continue
            volume = _to_float(raw.get("f5"))
            if volume is not None:
                volume *= 100.0
            rows.append(
                {
                    "date": trade_date,
                    "symbol": ticker,
                    "open": _to_float(raw.get("f17")),
                    "high": _to_float(raw.get("f15")),
                    "low": _to_float(raw.get("f16")),
                    "close": close,
                    "volume": volume,
                    "adj_close": close,
                    "dividend": None,
                    "split_ratio": None,
                }
            )
        if len(rows) >= len(supported) or (total is not None and fetched >= total):
            break
        page += 1
    return rows


def _tencent_cn_spot_rows(*, tickers: list[str], trade_date: str) -> list[dict]:
    query_to_ticker: dict[str, str] = {}
    for ticker in tickers:
        normalized = str(ticker or "").strip().upper()
        code = normalized.split(".", 1)[0]
        if normalized.endswith(".SS"):
            query_to_ticker[f"sh{code}"] = normalized
        elif normalized.endswith(".SZ"):
            query_to_ticker[f"sz{code}"] = normalized
    if not query_to_ticker:
        return []

    rows: list[dict] = []
    queries = sorted(query_to_ticker)
    batch_size = 20
    batches = [queries[index : index + batch_size] for index in range(0, len(queries), batch_size)]

    def fetch_batch(batch: list[str]) -> str:
        if not batch:
            return ""
        response = subprocess.run(
            ["curl", "-sL", "--max-time", "8", _TENCENT_CN_QUOTE_URL + ",".join(batch)],
            capture_output=True,
            check=True,
            timeout=10,
        )
        return response.stdout.decode("gbk", errors="ignore")

    payloads: list[str] = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = [executor.submit(fetch_batch, batch) for batch in batches]
        for future in as_completed(futures):
            try:
                payload = future.result()
            except Exception:
                continue
            if payload:
                payloads.append(payload)

    for payload in payloads:
        for line in payload.splitlines():
            if not line.startswith("v_") or '="' not in line:
                continue
            query = line.split("=", 1)[0].removeprefix("v_").strip()
            ticker = query_to_ticker.get(query)
            if not ticker:
                continue
            raw = line.split('="', 1)[1].rsplit('";', 1)[0]
            parts = raw.split("~")
            if len(parts) < 39:
                continue
            close = _to_float(parts[3])
            open_price = _to_float(parts[5])
            high = _to_float(parts[33])
            low = _to_float(parts[34])
            volume = _to_float(parts[6])
            if close is None or open_price is None or high is None or low is None:
                continue
            if volume is not None:
                volume *= 100.0
            rows.append(
                {
                    "date": trade_date,
                    "symbol": ticker,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "adj_close": close,
                    "dividend": None,
                    "split_ratio": None,
                }
            )
    return rows


def sync_cn_symbol_universe() -> dict:
    client = TushareClient()
    rows: list[dict] = []
    source = "akshare"
    fallback_reason = None
    if client.is_configured():
        try:
            rows = client.fetch_cn_symbol_universe()
            source = "tushare"
        except Exception as exc:
            fallback_reason = str(exc)
    else:
        fallback_reason = "PQW_TUSHARE_TOKEN is not configured."

    if not rows:
        akshare_rows = _fetch_cn_symbol_universe_from_akshare()
        if akshare_rows:
            rows = akshare_rows
            source = "akshare"

    if not rows:
        return {
            "status": "empty" if client.is_configured() else "not_configured",
            "message": (
                "No CN stock universe rows returned from TuShare or AKShare."
                if client.is_configured()
                else "Set PQW_TUSHARE_TOKEN or install AKShare data support to enable CN market universe sync."
            )
            + (f" Last error: {fallback_reason}" if fallback_reason else ""),
            "symbols_written": 0,
            "tickers": [],
        }

    written = 0
    touched: list[str] = []
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            exchange = str(row.get("exchange") or "").strip().upper() or None
            if not _is_supported_cn_symbol(ticker=ticker, exchange=exchange):
                continue
            symbol_repo.get_or_create_symbol(
                SymbolCreate(
                    ticker=ticker,
                    name=row.get("name"),
                    market="CN",
                    exchange=exchange,
                    sector=row.get("sector"),
                    industry=row.get("industry"),
                )
            )
            written += 1
            if ticker not in touched:
                touched.append(ticker)

    return {
        "status": "success",
        "message": f"Synced {written} CN universe row(s) for {len(touched)} stock(s) via {source}."
        + (f" Fallback reason: {fallback_reason}" if fallback_reason and source == "akshare" else ""),
        "symbols_written": written,
        "tickers": touched,
        "source": source,
    }


def init_cn_market_data(
    *,
    days_back: int = 180,
    offset: int = 0,
    batch_size: int | None = None,
    limit: int | None = None,
    pending_only: bool = False,
    retry_failed: bool = False,
    provider: str = "auto",
) -> dict:
    days_back = max(30, int(days_back))
    offset = max(0, int(offset))
    batch_size = None if batch_size in (None, 0) else max(1, int(batch_size))
    limit = None if limit in (None, 0) else max(1, int(limit))

    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        symbols = [
            symbol
            for symbol in symbol_repo.list_symbols()
            if (symbol.market or "").upper() == "CN"
            and _is_supported_cn_symbol(ticker=symbol.ticker, exchange=symbol.exchange)
        ]
        if pending_only or retry_failed:
            sync_state_by_symbol_id = {
                state.symbol_id: state
                for state in db.query(PriceSyncState).all()
            }
            selected_symbols = []
            for symbol in symbols:
                sync_state = sync_state_by_symbol_id.get(symbol.id)
                if pending_only and sync_state is None:
                    selected_symbols.append(symbol)
                    continue
                if retry_failed and sync_state is not None and (sync_state.status or "").lower() == "failed":
                    selected_symbols.append(symbol)
            cn_symbols = [symbol.ticker for symbol in selected_symbols]
        else:
            cn_symbols = [symbol.ticker for symbol in symbols]

    universe_result = None
    if not cn_symbols:
        universe_result = sync_cn_symbol_universe()
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            cn_symbols = [
                symbol.ticker
                for symbol in symbol_repo.list_symbols()
                if (symbol.market or "").upper() == "CN"
                and _is_supported_cn_symbol(ticker=symbol.ticker, exchange=symbol.exchange)
            ]

    if not cn_symbols:
        message = "No CN symbols available to initialize market data."
        if universe_result and universe_result.get("message"):
            message = f"{message} {universe_result['message']}"
        return {
            "status": universe_result.get("status", "empty") if universe_result else "empty",
            "message": message,
            "days_back": days_back,
            "total_symbols": 0,
            "success_count": 0,
            "failure_count": 0,
            "results": [],
        }

    selected_tickers = cn_symbols[offset:]
    if batch_size:
        selected_tickers = selected_tickers[:batch_size]
    elif limit:
        selected_tickers = selected_tickers[:limit]
    start_date = (date.today() - timedelta(days=days_back + 14)).isoformat()
    results = sync_market_data(
        tickers=selected_tickers,
        start_date=start_date,
        provider=provider,
    )
    success_count = sum(1 for item in results if item.get("status") == "success")
    failure_count = sum(1 for item in results if item.get("status") == "failed")
    status = "success" if failure_count == 0 else "partial"
    if success_count == 0 and failure_count:
        status = "failed"

    message = (
        f"Initialized CN market data for {success_count} stock(s)"
        f" over ~{days_back} days"
        + (f", {failure_count} failed." if failure_count else ".")
    )

    payload = {
        "status": status,
        "message": message,
        "days_back": days_back,
        "offset": offset,
        "batch_size": batch_size,
        "pending_only": pending_only,
        "retry_failed": retry_failed,
        "total_symbols": len(selected_tickers),
        "success_count": success_count,
        "failure_count": failure_count,
        "results": results,
        "remaining_symbols": max(0, len(cn_symbols) - offset - len(selected_tickers)),
    }
    if success_count:
        technical_snapshot_rebuild = rebuild_technical_snapshots(
            market="CN",
            limit=len(selected_tickers),
        )
        payload["technical_snapshot_rebuild"] = technical_snapshot_rebuild
        payload["message"] = f"{payload['message']} {technical_snapshot_rebuild['message']}"
    if universe_result:
        payload["universe_sync"] = universe_result
    return payload


def refresh_cn_market_data(
    *,
    days_back: int = 7,
    limit: int | None = None,
    provider: str = "auto",
    incremental: bool = False,
    overlap_days: int = 3,
    rebuild_snapshots: bool = True,
    source_job_id: int | None = None,
) -> dict:
    days_back = max(2, int(days_back))
    limit = None if limit in (None, 0) else max(1, int(limit))
    overlap_days = max(0, int(overlap_days))

    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        symbols = [
            symbol
            for symbol in symbol_repo.list_symbols()
            if (symbol.market or "").upper() == "CN"
            and _is_supported_cn_symbol(ticker=symbol.ticker, exchange=symbol.exchange)
        ]
        sync_state_by_symbol_id = {
            state.symbol_id: state
            for state in db.query(PriceSyncState).all()
        }

    cn_symbols = [symbol.ticker for symbol in symbols]

    if not cn_symbols:
        return {
            "status": "empty",
            "message": "No CN symbols available. Sync the CN market universe first.",
            "days_back": days_back,
            "total_symbols": 0,
            "success_count": 0,
            "failure_count": 0,
            "results": [],
        }

    selected_symbols = symbols[:limit] if limit else symbols
    selected_tickers = [symbol.ticker for symbol in selected_symbols]
    required_as_of_date = latest_completed_market_date("CN")
    refresh_batch_id = None
    if source_job_id is not None:
        with SessionLocal() as db:
            batch = MarketRefreshBatchRepository(db).create_batch(
                source_job_id=source_job_id,
                market="CN",
                provider=provider,
                requested_as_of_date=required_as_of_date,
                universe_count=len(selected_tickers),
            )
            refresh_batch_id = batch.id
    start_date = (date.today() - timedelta(days=days_back + overlap_days)).isoformat()
    start_dates_by_ticker = None
    if incremental:
        start_dates_by_ticker = {}
        for symbol in selected_symbols:
            sync_state = sync_state_by_symbol_id.get(symbol.id)
            start_dates_by_ticker[symbol.ticker] = _incremental_refresh_start_date(
                sync_state.last_synced_date if sync_state is not None else None,
                days_back=days_back,
                overlap_days=overlap_days,
            )
    results = sync_market_data(
        tickers=selected_tickers,
        start_date=start_date,
        provider=provider,
        start_dates_by_ticker=start_dates_by_ticker,
        required_as_of_date=required_as_of_date,
    )
    success_count = sum(1 for item in results if item.get("status") == "success")
    failure_count = sum(1 for item in results if item.get("status") == "failed")
    stale_count = sum(1 for item in results if item.get("status") == "partial")
    no_trade_count = sum(1 for item in results if item.get("status") in {"no_trade", "suspended"})
    accounted_count = success_count + no_trade_count
    status = "success" if accounted_count == len(selected_tickers) and failure_count == 0 and stale_count == 0 else "partial"
    if accounted_count == 0 and (failure_count or stale_count):
        status = "failed"

    payload = {
        "status": status,
        "message": (
            f"{'Incrementally refreshed' if incremental else 'Refreshed'} CN market data for {success_count} stock(s)"
            f" over the recent ~{days_back} days"
            + (f", {failure_count} failed." if failure_count else ".")
            + (f" {stale_count} remain behind the required market date." if stale_count else "")
            + (f" {no_trade_count} explicitly reported suspended/no-trade." if no_trade_count else "")
        ),
        "days_back": days_back,
        "incremental": incremental,
        "overlap_days": overlap_days,
        "total_symbols": len(selected_tickers),
        "success_count": success_count,
        "failure_count": failure_count,
        "stale_count": stale_count,
        "no_trade_count": no_trade_count,
        "required_as_of_date": required_as_of_date,
        "actual_as_of_date": max(
            (str(item.get("last_synced_date") or "")[:10] for item in results if item.get("last_synced_date")),
            default=None,
        ),
        "refresh_batch_id": refresh_batch_id,
        "results": results,
    }
    payload["quality_summary"] = {
        "market": "CN",
        "requested_as_of_date": required_as_of_date,
        "actual_as_of_date": payload.get("actual_as_of_date"),
        "success_count": success_count,
        "no_trade_count": no_trade_count,
        "partial_count": stale_count,
        "missing_count": 0,
        "failed_count": failure_count,
        "refresh_batch_id": refresh_batch_id,
    }
    if refresh_batch_id is not None:
        with SessionLocal() as db:
            MarketRefreshBatchRepository(db).complete_batch(refresh_batch_id, result=payload)
    if success_count and rebuild_snapshots:
        technical_snapshot_rebuild = rebuild_technical_snapshots(market="CN", limit=limit)
        payload["technical_snapshot_rebuild"] = technical_snapshot_rebuild
        payload["message"] = f"{payload['message']} {technical_snapshot_rebuild['message']}"
    return payload


def refresh_cn_market_data_daily(
    *,
    days_back: int = 7,
    limit: int | None = None,
    provider: str = "auto",
    overlap_days: int = 3,
    rebuild_snapshots: bool = True,
    source_job_id: int | None = None,
) -> dict:
    return refresh_cn_market_data(
        days_back=days_back,
        limit=limit,
        provider=provider,
        incremental=True,
        overlap_days=overlap_days,
        rebuild_snapshots=rebuild_snapshots,
        source_job_id=source_job_id,
    )


def refresh_cn_market_data_lake_only(
    *,
    start_date: str,
    end_date: str | None = None,
    limit: int | None = None,
) -> dict:
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        tickers = [
            symbol.ticker
            for symbol in symbol_repo.list_symbols()
            if (symbol.market or "").upper() == "CN"
            and _is_supported_cn_symbol(ticker=symbol.ticker, exchange=symbol.exchange)
        ]
    if limit:
        tickers = tickers[: max(1, int(limit))]
    if not tickers:
        return {
            "status": "empty",
            "message": "No CN symbols available. Sync the CN market universe first.",
            "start_date": start_date,
            "end_date": end_date,
            "success_count": 0,
            "failure_count": 0,
        }
    rows_by_ticker = TushareClient().fetch_cn_daily_history_bulk(
        tickers,
        start_date=start_date,
        end_date=end_date,
    )
    rows = [row for ticker_rows in rows_by_ticker.values() for row in ticker_rows]
    required_as_of_date = end_date or latest_completed_market_date("CN")
    provider_used = "tushare_lake"
    providers_attempted = ["tushare_lake"]
    fallback_errors: list[str] = []
    if not rows and start_date == required_as_of_date and required_as_of_date == latest_completed_market_date("CN"):
        providers_attempted.append("eastmoney_push2")
        try:
            rows = _eastmoney_cn_spot_rows(tickers=tickers, trade_date=required_as_of_date)
            if rows:
                provider_used = "eastmoney_push2"
        except Exception as exc:
            fallback_errors.append(f"eastmoney_push2: {exc}")
        if not rows:
            providers_attempted.append("tencent_gtimg")
            try:
                rows = _tencent_cn_spot_rows(tickers=tickers, trade_date=required_as_of_date)
                if rows:
                    provider_used = "tencent_gtimg"
            except Exception as exc:
                fallback_errors.append(f"tencent_gtimg: {exc}")
    parquet_paths = write_ohlcv_rows_to_lake(market="CN", rows=rows) if rows else []
    touched = {str(row.get("symbol") or "").upper() for row in rows if row.get("symbol")}
    state_summary = _upsert_lake_only_sync_state(rows, required_as_of_date=required_as_of_date)
    fresh_count = int(state_summary.get("fresh_count") or 0)
    stale_count = max(0, len(tickers) - fresh_count)
    status = "success" if fresh_count == len(tickers) else "partial" if fresh_count else "failed"
    return {
        "market": "CN",
        "status": status if rows else "empty",
        "message": (
            f"CN lake-only refresh wrote {len(rows)} row(s) for {len(touched)} stock(s) into {len(parquet_paths)} parquet partition(s)."
            + (f" {stale_count} symbol(s) remain behind required as-of {required_as_of_date}." if stale_count else "")
        ),
        "start_date": start_date,
        "end_date": end_date,
        "total_symbols": len(tickers),
        "success_count": fresh_count,
        "failure_count": 0,
        "stale_count": stale_count,
        "required_as_of_date": required_as_of_date,
        "actual_as_of_date": max((str(row.get("date") or "")[:10] for row in rows), default=None),
        "provider_used": provider_used,
        "providers_attempted": providers_attempted,
        "fallback_error": "; ".join(fallback_errors) if fallback_errors else None,
        "rows_written": len(rows),
        "parquet_files": [str(path) for path in parquet_paths[:12]],
    }


def _upsert_lake_only_sync_state(rows: list[dict], *, required_as_of_date: str) -> dict:
    latest_by_ticker: dict[str, str] = {}
    for row in rows:
        ticker = str(row.get("symbol") or "").strip().upper()
        trade_date = str(row.get("date") or "").strip()
        if not ticker or not trade_date:
            continue
        if trade_date > latest_by_ticker.get(ticker, ""):
            latest_by_ticker[ticker] = trade_date
    if not latest_by_ticker:
        return {"fresh_count": 0, "stale_count": 0}
    tickers = sorted(latest_by_ticker)
    dates = [latest_by_ticker[ticker] for ticker in tickers]
    with SessionLocal() as db:
        db.execute(
            text(
                """
                WITH payload AS (
                    SELECT *
                    FROM unnest(CAST(:tickers AS TEXT[]), CAST(:dates AS TEXT[])) AS p(ticker, last_synced_date)
                )
                INSERT INTO price_sync_state (symbol_id, provider, last_synced_date, status, message, updated_at)
                SELECT
                    symbols.id,
                    'tushare_lake',
                    payload.last_synced_date,
                    CASE WHEN payload.last_synced_date >= :required_as_of_date THEN 'success' ELSE 'partial' END,
                    CASE
                        WHEN payload.last_synced_date >= :required_as_of_date THEN 'Wrote current fetched rows to Parquet lake.'
                        ELSE 'Wrote fetched rows to Parquet lake, but the market date is stale.'
                    END,
                    :updated_at
                FROM payload
                JOIN symbols ON symbols.ticker = payload.ticker
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
                "dates": dates,
                "required_as_of_date": required_as_of_date,
                "updated_at": app_now_iso(),
            },
        )
        db.commit()
    fresh_count = sum(1 for value in dates if value >= required_as_of_date)
    return {"fresh_count": fresh_count, "stale_count": len(dates) - fresh_count}


def _fetch_cn_symbol_universe_from_akshare() -> list[dict]:
    try:
        import akshare as ak
    except ImportError:
        return []

    try:
        df = ak.stock_info_a_code_name()
    except Exception:
        return []

    if df is None or df.empty:
        return []

    rows: list[dict] = []
    for _, row in df.iterrows():
        code = str(row.get("code") or row.get("证券代码") or "").strip()
        name = str(row.get("name") or row.get("证券简称") or "").strip() or None
        if len(code) != 6 or not code.isdigit():
            continue
        ticker, exchange = _akshare_cn_code_to_app_ticker(code)
        if ticker is None:
            continue
        rows.append(
            {
                "ticker": ticker,
                "name": name,
                "exchange": exchange,
                "listing_date": None,
            }
        )
    return rows


def _akshare_cn_code_to_app_ticker(code: str) -> tuple[str | None, str | None]:
    if code.startswith(("6", "5", "9")):
        return f"{code}.SS", "SSE"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ", "SZSE"
    return None, None


def _incremental_refresh_start_date(
    last_synced_date: str | None,
    *,
    days_back: int,
    overlap_days: int,
) -> str:
    fallback_start = date.today() - timedelta(days=days_back + overlap_days)
    if not last_synced_date:
        return fallback_start.isoformat()
    try:
        last_date = date.fromisoformat(str(last_synced_date)[:10])
    except ValueError:
        return fallback_start.isoformat()
    return max(fallback_start, last_date - timedelta(days=overlap_days)).isoformat()
