"""S-13/S-14: market sync never fakes a lake write and never hides a lost trade date."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.postgres_safety import ApplicationPostgresTestCase

from app.models.schema import SymbolCreate
from app.core.db import SessionLocal
from app.services.repository import SymbolRepository


class _FakePriceProvider:
    name = "fake"
    last_source_used = "fake"

    def __init__(self, rows_by_ticker: dict[str, list[dict]] | None = None) -> None:
        self._rows_by_ticker = rows_by_ticker or {}

    def fetch_historical_prices(self, request):
        return list(self._rows_by_ticker.get(request.ticker) or [])


def _hk_rows(ticker: str) -> list[dict]:
    return [
        {"date": "2026-04-02", "symbol": ticker, "open": 100.0, "high": 101.0, "low": 98.0, "close": 99.0, "volume": 1000.0},
    ]


class HkSyncContractTests(ApplicationPostgresTestCase):
    def _seed(self, ticker: str) -> None:
        with SessionLocal() as db:
            SymbolRepository(db).get_or_create_symbol(SymbolCreate(ticker=ticker, name=ticker, market="HK"))

    def test_hk_sync_returns_unsupported_market_not_fake_success(self) -> None:
        from app.services.market_sync import sync_market_data

        self._seed("0700.HK")
        provider = _FakePriceProvider({"0700.HK": _hk_rows("0700.HK")})
        with patch("app.services.market_sync.resolve_price_provider", return_value=provider):
            results = sync_market_data(tickers=["0700.HK"], start_date="2026-04-01", provider="yfinance")

        item = results[0]
        self.assertEqual("unsupported_market", item["status"])
        self.assertEqual([], item["lake_paths"])
        self.assertNotIn("Parquet lake via", item["message"])
        self.assertIn("not supported by the Parquet lake", item["message"])

    def test_hk_sync_without_rows_is_unsupported_not_failed(self) -> None:
        from app.services.market_sync import sync_market_data

        self._seed("9988.HK")
        provider = _FakePriceProvider({})
        with patch("app.services.market_sync.resolve_price_provider", return_value=provider):
            results = sync_market_data(tickers=["9988.HK"], start_date="2026-04-01", provider="yfinance")

        self.assertEqual("unsupported_market", results[0]["status"])


class _FakeBulkClient:
    """Mimics TushareClient bulk fetch with one failed trade date."""

    last_failed_trade_dates = ["2026-09-30"]

    def __init__(self, *args, **kwargs) -> None:
        self.last_error = None

    def fetch_cn_daily_history_bulk(self, tickers, *, start_date=None, end_date=None):
        return {
            ticker: [
                {
                    "date": "2026-10-01",
                    "symbol": ticker,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 1000.0,
                    "adj_close": 10.2,
                    "dividend": None,
                    "split_ratio": None,
                }
            ]
            for ticker in tickers
        }


class TusharePartialContractTests(ApplicationPostgresTestCase):
    def test_single_day_failure_reports_partial_with_missing_dates(self) -> None:
        from app.services.market_sync import sync_market_data

        tickers = [f"6000{index:02d}.SH" for index in range(100)]
        with SessionLocal() as db:
            repo = SymbolRepository(db)
            for ticker in tickers:
                repo.get_or_create_symbol(SymbolCreate(ticker=ticker, name=ticker, market="CN"))

        with patch("app.services.market_sync.TushareClient", _FakeBulkClient), patch(
            "app.services.market_sync.write_ohlcv_rows_to_lake", return_value=[]
        ):
            results = sync_market_data(tickers=tickers, start_date="2026-09-25", provider="auto")

        self.assertEqual(100, len(results))
        for item in results:
            self.assertEqual("partial", item["status"])
            self.assertEqual(["2026-09-30"], item["missing_trade_dates"])
            self.assertIn("Missing trade date", item["message"])


if __name__ == "__main__":
    unittest.main()
