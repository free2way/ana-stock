import csv
from pathlib import Path

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.market_lake import load_lake_price_history, write_ohlcv_rows_to_lake
from app.services.market_freshness import is_as_of_current
from app.services.normalizer import MarketDataNormalizer
from app.services.openbb_client import HistoricalPriceRequest
from app.services.providers import resolve_price_provider
from app.services.repository import PriceSyncStateRepository, SymbolRepository
from app.services.tushare_client import TushareClient
from app.services.ticker_format import infer_market_from_ticker, normalize_ticker_for_market, provider_ticker_candidates


RAW_FIELDS = [
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


def write_raw_csv(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=RAW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_raw_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as input_file:
        return list(csv.DictReader(input_file))


def merge_market_data_rows(existing_rows: list[dict], new_rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for row in existing_rows + new_rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        trade_date = str(row.get("date") or "").strip()
        if not symbol or not trade_date:
            continue
        normalized = {
            "date": trade_date,
            "symbol": symbol,
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "close": row.get("close"),
            "volume": row.get("volume"),
            "adj_close": row.get("adj_close"),
            "dividend": row.get("dividend"),
            "split_ratio": row.get("split_ratio"),
        }
        merged[(symbol, trade_date)] = normalized
    return [merged[key] for key in sorted(merged.keys(), key=lambda item: (item[0], item[1]))]


def sync_market_data(
    *,
    tickers: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    provider: str = "auto",
    start_dates_by_ticker: dict[str, str] | None = None,
    persist_csv: bool = False,
    required_as_of_date: str | None = None,
) -> list[dict]:
    settings = get_settings()
    normalizer = MarketDataNormalizer()
    results: list[dict] = []

    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        sync_repo = PriceSyncStateRepository(db)

        symbols = []
        if tickers:
            for ticker in tickers:
                inferred_market = infer_market_from_ticker(ticker)
                normalized_ticker = normalize_ticker_for_market(ticker, inferred_market)
                if not normalized_ticker:
                    continue
                symbol = symbol_repo.get_by_ticker(normalized_ticker)
                if symbol is None:
                    symbol = symbol_repo.create_symbol(
                        SymbolCreate(
                            ticker=normalized_ticker,
                            name=normalized_ticker,
                            market=inferred_market,
                        )
                    )
                symbols.append(symbol)
        else:
            symbols = symbol_repo.list_symbols()

        if not symbols:
            raise RuntimeError("No symbols found. Add symbols first or pass tickers to sync.")

        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider in {"a_stock_data", "a_stock_data_tencent", "tencent"}:
            if not tickers:
                raise RuntimeError(
                    "a-stock-data Tencent is a supplementary source. Select explicit A-share tickers; "
                    "it is not permitted for an all-market lake refresh."
                )
            if len(symbols) > int(settings.a_stock_data_max_symbols):
                raise RuntimeError(
                    f"a-stock-data Tencent is limited to {int(settings.a_stock_data_max_symbols)} symbols per run. "
                    "Use TuShare for the A-share full-market lake."
                )
            if any(str(symbol.market or "").upper() != "CN" for symbol in symbols):
                raise RuntimeError("a-stock-data Tencent supports A-share tickers only.")

        bulk_rows_by_ticker: dict[str, list[dict]] = {}
        use_cn_tushare_bulk = (
            len(symbols) >= 100
            and normalized_provider in {"", "auto", "tushare"}
            and all(str(symbol.market or "").upper() == "CN" for symbol in symbols)
        )
        if use_cn_tushare_bulk:
            bulk_client = TushareClient()
            earliest_start_date = start_date
            if start_dates_by_ticker:
                candidate_dates = [
                    str(value).strip()
                    for value in start_dates_by_ticker.values()
                    if str(value or "").strip()
                ]
                if candidate_dates:
                    earliest_start_date = min(candidate_dates)
            bulk_rows_by_ticker = bulk_client.fetch_cn_daily_history_bulk(
                [symbol.ticker for symbol in symbols],
                start_date=earliest_start_date,
                end_date=end_date,
            )
            lake_rows = [row for ticker_rows in bulk_rows_by_ticker.values() for row in ticker_rows]
            if lake_rows:
                write_ohlcv_rows_to_lake(market="CN", rows=lake_rows)

        # A-share daily endpoints omit suspended symbols instead of returning
        # a zero-volume bar.  Keep the provider check optional and
        # conservative: an unknown suspension state must remain stale/failed.
        suspension_client = (
            TushareClient()
            if required_as_of_date
            and all(str(symbol.market or "").upper() == "CN" for symbol in symbols)
            else None
        )

        for symbol in symbols:
            try:
                symbol_start_date = start_dates_by_ticker.get(symbol.ticker, start_date) if start_dates_by_ticker else start_date
                if bulk_rows_by_ticker:
                    rows = [
                        row
                        for row in (bulk_rows_by_ticker.get(symbol.ticker) or [])
                        if not symbol_start_date or str(row.get("date") or "") >= str(symbol_start_date)
                    ]
                    selected_provider_ticker = symbol.ticker
                    provider_used = "tushare_bulk"
                else:
                    price_provider = resolve_price_provider(provider, market=symbol.market)
                    provider_ticker = normalize_ticker_for_market(symbol.ticker, symbol.market)
                    provider_candidates = provider_ticker_candidates(symbol.ticker, symbol.market)
                    rows = []
                    selected_provider_ticker = provider_ticker
                    provider_used = provider
                    for candidate in provider_candidates:
                        candidate_rows = price_provider.fetch_historical_prices(
                            HistoricalPriceRequest(
                                ticker=candidate,
                                start_date=symbol_start_date,
                                end_date=end_date,
                                provider=provider,
                            )
                        )
                        if candidate_rows and len(candidate_rows) > len(rows):
                            rows = candidate_rows
                            selected_provider_ticker = candidate
                            provider_used = getattr(price_provider, "last_source_used", provider) or provider
                market_code = str(symbol.market or infer_market_from_ticker(symbol.ticker) or "").upper()
                if not rows:
                    existing_rows = []
                    if market_code in {"CN", "US"}:
                        existing_rows = load_lake_price_history(market=market_code, ticker=symbol.ticker, limit=5)
                    if existing_rows:
                        last_synced_date = str(existing_rows[-1].get("date") or "") or None
                        is_current = is_as_of_current(last_synced_date, required_as_of_date)
                        status = "success" if is_current else "partial"
                        message = (
                            f"No new market data returned for {selected_provider_ticker}; "
                            f"retained existing lake history through {last_synced_date}."
                        )
                        no_trade = False
                        if (
                            not is_current
                            and suspension_client is not None
                            and market_code == "CN"
                            and required_as_of_date
                        ):
                            no_trade = suspension_client.is_cn_suspended_on_date(
                                symbol.ticker,
                                required_as_of_date,
                            ) is True
                        if no_trade:
                            status = "no_trade"
                            message = (
                                f"No market bar for {required_as_of_date}; TuShare reports {symbol.ticker} "
                                "as suspended/no-trade. Existing lake history was retained."
                            )
                        if not is_current and not no_trade:
                            message += f" Required as-of date is {required_as_of_date}; data remains stale."
                        sync_repo.upsert_state(
                            symbol_id=symbol.id,
                            provider=provider_used,
                            last_synced_date=last_synced_date,
                            status=status,
                            message=message,
                        )
                        results.append(
                            {
                                "ticker": symbol.ticker,
                                "status": status,
                                "rows": 0,
                                "stored_rows": 0,
                                "provider_ticker": selected_provider_ticker,
                                "last_synced_date": last_synced_date,
                                "no_trade": no_trade,
                                "no_trade_reason": "suspended" if no_trade else None,
                                "lake_paths": [],
                                "raw_path": None,
                                "normalized_path": None,
                                "persist_csv": persist_csv,
                                "message": message,
                            }
                        )
                        continue
                    raise RuntimeError(f"No market data returned for {selected_provider_ticker}")
                lake_paths = []
                if market_code in {"CN", "US"} and not bulk_rows_by_ticker:
                    lake_paths = write_ohlcv_rows_to_lake(market=market_code, rows=rows, merge_existing=True)
                raw_path = settings.raw_data_dir / f"{symbol.ticker}.csv"
                normalized_path = settings.normalized_data_dir / f"{symbol.ticker}.csv"
                merged_rows = rows
                if persist_csv:
                    merged_rows = merge_market_data_rows(read_raw_csv(raw_path), rows)
                    write_raw_csv(raw_path, merged_rows)
                    normalizer.normalize_symbol_file(raw_path, normalized_path)
                sorted_rows = sorted(rows, key=lambda row: str(row.get("date") or ""))
                last_synced_date = sorted_rows[-1]["date"] if sorted_rows else None
                is_current = is_as_of_current(last_synced_date, required_as_of_date)
                status = "success" if is_current else "partial"
                message = (
                    f"Wrote {len(rows)} fetched row(s) to Parquet lake via {selected_provider_ticker}"
                    + (f" and {len(merged_rows)} stored CSV row(s) to {raw_path.name}" if persist_csv else "")
                )
                if not is_current:
                    message += f" Required as-of date is {required_as_of_date}; fetched data is stale."
                sync_repo.upsert_state(
                    symbol_id=symbol.id,
                    provider=provider_used,
                    last_synced_date=last_synced_date,
                    status=status,
                    message=message,
                )
                results.append(
                    {
                        "ticker": symbol.ticker,
                        "status": status,
                        "rows": len(rows),
                        "stored_rows": len(merged_rows),
                        "provider_ticker": selected_provider_ticker,
                        "last_synced_date": last_synced_date,
                        "lake_paths": [str(path) for path in lake_paths],
                        "raw_path": str(raw_path) if persist_csv else None,
                        "normalized_path": str(normalized_path) if persist_csv else None,
                        "persist_csv": persist_csv,
                        "message": message,
                    }
                )
            except Exception as exc:
                sync_repo.upsert_state(
                    symbol_id=symbol.id,
                    provider=provider,
                    last_synced_date=None,
                    status="failed",
                    message=str(exc),
                )
                results.append(
                    {
                        "ticker": symbol.ticker,
                        "status": "failed",
                        "rows": 0,
                        "last_synced_date": None,
                        "message": str(exc),
                    }
                )

    return results
