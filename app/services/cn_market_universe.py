from datetime import date, timedelta

from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.models.tables import PriceSyncState
from app.services.market_sync import sync_market_data
from app.services.repository import SymbolRepository
from app.services.technical_snapshot_cache import rebuild_technical_snapshots
from app.services.tushare_client import TushareClient


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
    provider: str = "yfinance",
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
    provider: str = "yfinance",
    incremental: bool = False,
    overlap_days: int = 3,
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
    )
    success_count = sum(1 for item in results if item.get("status") == "success")
    failure_count = sum(1 for item in results if item.get("status") == "failed")
    status = "success" if failure_count == 0 else "partial"
    if success_count == 0 and failure_count:
        status = "failed"

    payload = {
        "status": status,
        "message": (
            f"{'Incrementally refreshed' if incremental else 'Refreshed'} CN market data for {success_count} stock(s)"
            f" over the recent ~{days_back} days"
            + (f", {failure_count} failed." if failure_count else ".")
        ),
        "days_back": days_back,
        "incremental": incremental,
        "overlap_days": overlap_days,
        "total_symbols": len(selected_tickers),
        "success_count": success_count,
        "failure_count": failure_count,
        "results": results,
    }
    if success_count:
        technical_snapshot_rebuild = rebuild_technical_snapshots(market="CN", limit=limit)
        payload["technical_snapshot_rebuild"] = technical_snapshot_rebuild
        payload["message"] = f"{payload['message']} {technical_snapshot_rebuild['message']}"
    return payload


def refresh_cn_market_data_daily(
    *,
    days_back: int = 7,
    limit: int | None = None,
    provider: str = "yfinance",
    overlap_days: int = 3,
) -> dict:
    return refresh_cn_market_data(
        days_back=days_back,
        limit=limit,
        provider=provider,
        incremental=True,
        overlap_days=overlap_days,
    )


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
