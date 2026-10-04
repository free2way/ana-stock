import math
from datetime import date as _date, timedelta as _timedelta

from app.core.db import SessionLocal, init_db
from app.models.schema import SymbolCreate
from app.services.market_lake import write_ohlcv_rows_to_lake
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


def _trading_days_before(end_date: _date, count: int, *, market: str = "US") -> list[_date]:
    from app.services.market_calendar import is_market_open_date

    days: list[_date] = []
    cursor = end_date
    while len(days) < count:
        cursor -= _timedelta(days=1)
        if is_market_open_date(market, cursor):
            days.append(cursor)
    days.reverse()
    return days


def extend_sample_rows(ticker: str, *, days: int) -> list[dict]:
    """Return the sample rows grown to ``days`` trading days.

    History is synthesized *before* the curated window so the existing dates
    stay the most recent ones: tests that assert on the curated tail keep
    seeing the same recent sessions while the trainer gains enough warmup to
    pass its minimum-history gate. The synthetic series is a deterministic,
    low-volatility walk (daily moves well under the limit bands) built from
    the first curated close, which keeps label eligibility meaningful.

    Curated rows that fall on a closed session (e.g. ``2026-04-03`` is a US
    holiday) are dropped here only: the backtest engine refuses to emit
    signals on dates outside the explicit market calendar, and the curated
    tail would otherwise leak such a date into the prediction window. The
    default ``seed_sample_data()`` path is unaffected.
    """

    from app.services.market_calendar import is_market_open_date

    base_rows = [
        row for row in SAMPLE_DATA[ticker] if is_market_open_date("US", str(row["date"]))
    ]
    missing = int(days) - len(base_rows)
    if missing <= 0:
        return base_rows

    first_date = _date.fromisoformat(str(base_rows[0]["date"]))
    anchor = float(base_rows[0]["close"])
    dates = _trading_days_before(first_date, missing, market="US")
    total = len(dates)
    synthetic: list[dict] = []
    previous_close = anchor * 0.8
    for index, day in enumerate(dates):
        progress = (index + 1) / (total + 1)
        close = anchor * (0.8 + 0.2 * progress + 0.01 * math.sin(index / 6.0))
        close = round(close, 4)
        open_price = round(previous_close, 4)
        high = round(max(open_price, close) * 1.005, 4)
        low = round(min(open_price, close) * 0.995, 4)
        volume = 1_000_000 + (index % 5) * 25_000
        synthetic.append(
            {
                "date": day.isoformat(),
                "symbol": ticker,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "adj_close": close,
                "dividend": "",
                "split_ratio": "",
            }
        )
        previous_close = close
    return synthetic + base_rows


def seed_sample_data(*, days: int | None = None) -> list[dict]:
    """Seed the sample universe.

    ``days`` optionally stretches each ticker's history to that many trading
    days (synthesizing the extra leading sessions). It defaults to ``None`` so
    existing callers keep the original 5-10 session fixture.
    """

    init_db()
    results: list[dict] = []

    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        sync_repo = PriceSyncStateRepository(db)

        for ticker, rows in SAMPLE_DATA.items():
            if days is not None:
                rows = extend_sample_rows(ticker, days=days)
            symbol = symbol_repo.get_by_ticker(ticker)
            if symbol is None:
                symbol = symbol_repo.create_symbol(SymbolCreate(ticker=ticker, name=ticker, market="US"))

            lake_paths = write_ohlcv_rows_to_lake(
                market="US",
                rows=rows,
                provenance={"provider": "sample", "source_reference": "sample_data:v1"},
            )

            sync_repo.upsert_state(
                symbol_id=symbol.id,
                provider="sample",
                last_synced_date=rows[-1]["date"],
                status="success",
                message=f"Seeded {len(rows)} rows",
            )
            results.append({"ticker": ticker, "rows": len(rows), "lake_paths": [str(path) for path in lake_paths]})

    return results
