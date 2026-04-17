from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.core.config import get_settings
from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient
from app.services.providers.base import BasePriceProvider
from app.services.tushare_client import TushareClient


class OpenBBPriceProvider(BasePriceProvider):
    name = "openbb"

    def __init__(self) -> None:
        super().__init__()
        self.client = OpenBBClient()

    def fetch_historical_prices(self, request: HistoricalPriceRequest) -> list[dict]:
        provider = str(request.provider or "").strip().lower()
        effective_request = HistoricalPriceRequest(
            ticker=request.ticker,
            start_date=request.start_date,
            end_date=request.end_date,
            provider="yfinance" if provider in {"", "auto"} else request.provider,
        )
        rows = self.client.fetch_historical_prices(effective_request)
        self.last_source_used = getattr(self.client, "last_source_used", self.name) or self.name
        return rows


class YFinancePriceProvider(BasePriceProvider):
    name = "yfinance"

    def __init__(self) -> None:
        super().__init__()
        self.client = OpenBBClient()

    def fetch_historical_prices(self, request: HistoricalPriceRequest) -> list[dict]:
        effective_request = HistoricalPriceRequest(
            ticker=request.ticker,
            start_date=request.start_date,
            end_date=request.end_date,
            provider="yfinance",
        )
        rows = self.client.fetch_historical_prices(effective_request)
        self.last_source_used = getattr(self.client, "last_source_used", self.name) or self.name
        return rows


class AlpacaPriceProvider(BasePriceProvider):
    name = "alpaca"

    def __init__(self) -> None:
        super().__init__()
        self.settings = get_settings()
        self.fallback = YFinancePriceProvider()

    def fetch_historical_prices(self, request: HistoricalPriceRequest) -> list[dict]:
        if not self.settings.alpaca_api_key or not self.settings.alpaca_api_secret:
            rows = self.fallback.fetch_historical_prices(request)
            self.last_source_used = f"yfinance_fallback_missing_alpaca"
            return rows
        try:
            rows = self._fetch_with_alpaca(request)
        except Exception:
            rows = []
        if rows:
            self.last_source_used = "alpaca"
            return rows
        fallback_rows = self.fallback.fetch_historical_prices(request)
        self.last_source_used = f"yfinance_fallback_after_alpaca"
        return fallback_rows

    def _fetch_with_alpaca(self, request: HistoricalPriceRequest) -> list[dict]:
        endpoint = str(self.settings.alpaca_data_endpoint or "https://data.alpaca.markets/v2").rstrip("/")
        symbol = request.ticker.upper()
        params = {
            "timeframe": "1Day",
            "adjustment": "raw",
            "feed": self.settings.alpaca_data_feed or "iex",
            "limit": 10000,
        }
        if request.start_date:
            params["start"] = request.start_date
        if request.end_date:
            params["end"] = request.end_date
        url = f"{endpoint}/stocks/{symbol}/bars?{urlencode(params)}"
        http_request = Request(
            url,
            headers={
                "APCA-API-KEY-ID": self.settings.alpaca_api_key or "",
                "APCA-API-SECRET-KEY": self.settings.alpaca_api_secret or "",
                "Accept": "application/json",
            },
        )
        with urlopen(http_request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        bars = payload.get("bars") or []
        rows: list[dict] = []
        for bar in bars:
            rows.append(
                {
                    "date": str(bar.get("t") or "")[:10],
                    "symbol": symbol,
                    "open": bar.get("o"),
                    "high": bar.get("h"),
                    "low": bar.get("l"),
                    "close": bar.get("c"),
                    "volume": bar.get("v"),
                    "adj_close": bar.get("c"),
                    "dividend": None,
                    "split_ratio": None,
                }
            )
        rows = [row for row in rows if row.get("date")]
        rows.sort(key=lambda row: row["date"])
        return rows


class TusharePriceProvider(BasePriceProvider):
    name = "tushare"

    def __init__(self) -> None:
        super().__init__()
        self.client = TushareClient()

    def fetch_historical_prices(self, request: HistoricalPriceRequest) -> list[dict]:
        rows = self.client.fetch_cn_daily_history(
            request.ticker,
            start_date=request.start_date,
            end_date=request.end_date,
        )
        self.last_source_used = self.name if rows else "tushare_unavailable"
        return rows


def resolve_price_provider(name: str | None, *, market: str | None = None) -> BasePriceProvider:
    normalized = str(name or "").strip().lower()
    market_code = str(market or "").strip().upper()
    if normalized in {"", "auto"}:
        if market_code == "CN":
            return TusharePriceProvider()
        if market_code in {"", "US"}:
            return AlpacaPriceProvider()
        return YFinancePriceProvider()
    if normalized == "alpaca":
        return AlpacaPriceProvider()
    if normalized == "openbb":
        return OpenBBPriceProvider()
    if normalized == "tushare":
        return TusharePriceProvider()
    if normalized == "yfinance":
        return YFinancePriceProvider()
    if market_code == "CN":
        return TusharePriceProvider()
    return YFinancePriceProvider()
