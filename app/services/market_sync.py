import csv

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.normalizer import MarketDataNormalizer
from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient
from app.services.repository import PriceSyncStateRepository, SymbolRepository
from app.services.ticker_format import normalize_ticker_for_market, provider_ticker_candidates


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


def sync_market_data(
    *,
    tickers: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    provider: str = "yfinance",
) -> list[dict]:
    settings = get_settings()
    client = OpenBBClient()
    normalizer = MarketDataNormalizer()
    results: list[dict] = []

    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        sync_repo = PriceSyncStateRepository(db)

        symbols = []
        if tickers:
            for ticker in tickers:
                normalized_ticker = ticker.strip().upper()
                if not normalized_ticker:
                    continue
                symbol = symbol_repo.get_by_ticker(normalized_ticker)
                if symbol is None:
                    symbol = symbol_repo.create_symbol(SymbolCreate(ticker=normalized_ticker, name=normalized_ticker, market="US"))
                symbols.append(symbol)
        else:
            symbols = symbol_repo.list_symbols()

        if not symbols:
            raise RuntimeError("No symbols found. Add symbols first or pass tickers to sync.")

        for symbol in symbols:
            try:
                provider_ticker = normalize_ticker_for_market(symbol.ticker, symbol.market)
                provider_candidates = provider_ticker_candidates(symbol.ticker, symbol.market)
                rows: list[dict] = []
                selected_provider_ticker = provider_ticker
                for candidate in provider_candidates:
                    candidate_rows = client.fetch_historical_prices(
                        HistoricalPriceRequest(
                            ticker=candidate,
                            start_date=start_date,
                            end_date=end_date,
                            provider=provider,
                        )
                    )
                    if candidate_rows and len(candidate_rows) > len(rows):
                        rows = candidate_rows
                        selected_provider_ticker = candidate
                if not rows:
                    raise RuntimeError(f"No market data returned for {provider_ticker}")
                raw_path = settings.raw_data_dir / f"{symbol.ticker}.csv"
                write_raw_csv(raw_path, rows)
                normalized_path = settings.normalized_data_dir / f"{symbol.ticker}.csv"
                normalizer.normalize_symbol_file(raw_path, normalized_path)
                last_synced_date = rows[-1]["date"] if rows else None
                sync_repo.upsert_state(
                    symbol_id=symbol.id,
                    provider=getattr(client, "last_source_used", provider) or provider,
                    last_synced_date=last_synced_date,
                    status="success",
                    message=f"Wrote {len(rows)} rows to {raw_path.name} via {selected_provider_ticker}",
                )
                results.append(
                    {
                        "ticker": symbol.ticker,
                        "status": "success",
                        "rows": len(rows),
                        "provider_ticker": selected_provider_ticker,
                        "last_synced_date": last_synced_date,
                        "raw_path": str(raw_path),
                        "normalized_path": str(normalized_path),
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
