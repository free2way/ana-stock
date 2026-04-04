import csv

from app.core.config import get_settings
from app.core.db import SessionLocal, init_db
from app.models.schema import SymbolCreate
from app.services.repository import PriceSyncStateRepository, SymbolRepository


SAMPLE_DATA = {
    "AAPL": [
        {"date": "2026-03-30", "symbol": "AAPL", "open": 210, "high": 214, "low": 209, "close": 213, "volume": 1000000, "adj_close": 213, "dividend": "", "split_ratio": ""},
        {"date": "2026-03-31", "symbol": "AAPL", "open": 213, "high": 216, "low": 212, "close": 215, "volume": 980000, "adj_close": 215, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-01", "symbol": "AAPL", "open": 215, "high": 217, "low": 214, "close": 216, "volume": 1020000, "adj_close": 216, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-02", "symbol": "AAPL", "open": 216, "high": 219, "low": 215, "close": 218, "volume": 1050000, "adj_close": 218, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-03", "symbol": "AAPL", "open": 218, "high": 220, "low": 217, "close": 219, "volume": 990000, "adj_close": 219, "dividend": "", "split_ratio": ""},
    ],
    "MSFT": [
        {"date": "2026-03-30", "symbol": "MSFT", "open": 100, "high": 101, "low": 98, "close": 99, "volume": 800000, "adj_close": 99, "dividend": "", "split_ratio": ""},
        {"date": "2026-03-31", "symbol": "MSFT", "open": 99, "high": 100, "low": 97, "close": 98, "volume": 810000, "adj_close": 98, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-01", "symbol": "MSFT", "open": 98, "high": 100, "low": 97, "close": 99, "volume": 830000, "adj_close": 99, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-02", "symbol": "MSFT", "open": 99, "high": 100, "low": 96, "close": 97, "volume": 850000, "adj_close": 97, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-03", "symbol": "MSFT", "open": 97, "high": 98, "low": 95, "close": 96, "volume": 870000, "adj_close": 96, "dividend": "", "split_ratio": ""},
    ],
    "ASTS": [
        {"date": "2026-03-23", "symbol": "ASTS", "open": 20.2, "high": 20.9, "low": 19.9, "close": 20.7, "volume": 1600000, "adj_close": 20.7, "dividend": "", "split_ratio": ""},
        {"date": "2026-03-24", "symbol": "ASTS", "open": 20.8, "high": 21.4, "low": 20.5, "close": 21.1, "volume": 1720000, "adj_close": 21.1, "dividend": "", "split_ratio": ""},
        {"date": "2026-03-25", "symbol": "ASTS", "open": 21.1, "high": 21.9, "low": 20.9, "close": 21.8, "volume": 1800000, "adj_close": 21.8, "dividend": "", "split_ratio": ""},
        {"date": "2026-03-26", "symbol": "ASTS", "open": 21.7, "high": 22.4, "low": 21.4, "close": 22.1, "volume": 1940000, "adj_close": 22.1, "dividend": "", "split_ratio": ""},
        {"date": "2026-03-27", "symbol": "ASTS", "open": 22.2, "high": 22.9, "low": 21.8, "close": 22.6, "volume": 2050000, "adj_close": 22.6, "dividend": "", "split_ratio": ""},
        {"date": "2026-03-30", "symbol": "ASTS", "open": 22.5, "high": 23.2, "low": 22.3, "close": 23.0, "volume": 2100000, "adj_close": 23.0, "dividend": "", "split_ratio": ""},
        {"date": "2026-03-31", "symbol": "ASTS", "open": 22.9, "high": 23.5, "low": 22.4, "close": 22.8, "volume": 1980000, "adj_close": 22.8, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-01", "symbol": "ASTS", "open": 22.7, "high": 23.6, "low": 22.2, "close": 23.4, "volume": 2260000, "adj_close": 23.4, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-02", "symbol": "ASTS", "open": 23.3, "high": 24.1, "low": 23.0, "close": 23.9, "volume": 2410000, "adj_close": 23.9, "dividend": "", "split_ratio": ""},
        {"date": "2026-04-03", "symbol": "ASTS", "open": 23.8, "high": 24.5, "low": 23.4, "close": 24.2, "volume": 2520000, "adj_close": 24.2, "dividend": "", "split_ratio": ""},
    ],
}


def write_csv(path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(
            output_file,
            fieldnames=["date", "symbol", "open", "high", "low", "close", "volume", "adj_close", "dividend", "split_ratio"],
        )
        writer.writeheader()
        writer.writerows(rows)


def seed_sample_data() -> list[dict]:
    settings = get_settings()
    init_db()
    results: list[dict] = []

    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        sync_repo = PriceSyncStateRepository(db)

        for ticker, rows in SAMPLE_DATA.items():
            symbol = symbol_repo.get_by_ticker(ticker)
            if symbol is None:
                symbol = symbol_repo.create_symbol(SymbolCreate(ticker=ticker, name=ticker, market="US"))

            raw_path = settings.raw_data_dir / f"{ticker}.csv"
            normalized_path = settings.normalized_data_dir / f"{ticker}.csv"
            write_csv(raw_path, rows)
            write_csv(normalized_path, rows)

            sync_repo.upsert_state(
                symbol_id=symbol.id,
                provider="sample",
                last_synced_date=rows[-1]["date"],
                status="success",
                message=f"Seeded {len(rows)} rows",
            )
            results.append({"ticker": ticker, "rows": len(rows), "raw_path": str(raw_path), "normalized_path": str(normalized_path)})

    return results
