import csv

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient
from app.services.repository import PriceSyncStateRepository, SymbolRepository


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
                rows = client.fetch_historical_prices(
                    HistoricalPriceRequest(
                        ticker=symbol.ticker,
                        start_date=start_date,
                        end_date=end_date,
                        provider=provider,
                    )
                )
                raw_path = settings.raw_data_dir / f"{symbol.ticker}.csv"
                write_raw_csv(raw_path, rows)
                last_synced_date = rows[-1]["date"] if rows else None
                sync_repo.upsert_state(
                    symbol_id=symbol.id,
                    provider=provider,
                    last_synced_date=last_synced_date,
                    status="success",
                    message=f"Wrote {len(rows)} rows to {raw_path.name}",
                )
                results.append(
                    {
                        "ticker": symbol.ticker,
                        "status": "success",
                        "rows": len(rows),
                        "last_synced_date": last_synced_date,
                        "raw_path": str(raw_path),
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
