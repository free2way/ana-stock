from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.openbb_client import OpenBBClient
from app.services.ticker_format import normalize_ticker_for_market
from app.services.tushare_client import TushareClient


class BaseFundamentalProvider(ABC):
    name: str = "base"

    def __init__(self) -> None:
        self.last_source_used = self.name

    @abstractmethod
    def fetch_snapshot(self, ticker: str) -> dict | None:
        raise NotImplementedError

    def fetch_snapshots(self, tickers: list[str], metadata: dict[str, dict] | None = None) -> list[dict]:
        rows: list[dict] = []
        for ticker in tickers:
            snapshot = self.fetch_snapshot(ticker)
            if snapshot:
                rows.append(snapshot)
        return rows


class OpenBBFundamentalProvider(BaseFundamentalProvider):
    name = "openbb_fundamentals"

    def __init__(self) -> None:
        super().__init__()
        self.client = OpenBBClient()

    def fetch_snapshot(self, ticker: str) -> dict | None:
        snapshot = self.client.fetch_fundamental_snapshot(ticker)
        self.last_source_used = getattr(self.client, "last_source_used", self.name) or self.name
        return snapshot


class TushareFundamentalProvider(BaseFundamentalProvider):
    name = "tushare"

    def __init__(self) -> None:
        super().__init__()
        self.client = TushareClient()

    def fetch_snapshot(self, ticker: str) -> dict | None:
        if not self.client.is_configured():
            self.last_source_used = "tushare_unavailable"
            return None
        rows = self.client.fetch_cn_growth_value_candidates([normalize_ticker_for_market(ticker, "CN")])
        if not rows:
            return None
        self.last_source_used = self.name
        return self._row_to_snapshot(rows[0])

    def fetch_snapshots(self, tickers: list[str], metadata: dict[str, dict] | None = None) -> list[dict]:
        if not self.client.is_configured():
            self.last_source_used = "tushare_unavailable"
            return []
        normalized = [normalize_ticker_for_market(ticker, "CN") for ticker in tickers if str(ticker or "").strip()]
        if not normalized:
            return []
        rows = self.client.fetch_cn_growth_value_candidates(
            normalized,
            stock_meta_by_ticker=metadata or None,
        )
        self.last_source_used = self.name if rows else self.last_source_used
        return [self._row_to_snapshot(row) for row in rows]

    def _row_to_snapshot(self, row) -> dict:
        return {
            "ticker": normalize_ticker_for_market(row.ticker, "CN"),
            "report_date": row.report_date,
            "name": row.name,
            "exchange": row.exchange,
            "listing_date": row.listing_date,
            "pe_ttm": row.pe_ttm,
            "dividend_yield": row.dividend_yield,
            "market_cap": row.market_cap,
            "roe_avg_3y": row.roe_avg_3y,
            "net_profit_yoy": row.net_profit_yoy,
            "revenue_yoy": row.revenue_yoy,
            "debt_to_assets": row.debt_to_assets,
            "raw_data": row.raw_data,
        }


def resolve_fundamental_provider(name: str | None, *, market: str | None = None) -> BaseFundamentalProvider:
    normalized = str(name or "").strip().lower()
    market_code = str(market or "").strip().upper()
    if normalized in {"", "auto"}:
        if market_code == "CN":
            return TushareFundamentalProvider()
        return OpenBBFundamentalProvider()
    if normalized == "tushare":
        return TushareFundamentalProvider()
    if normalized == "openbb":
        return OpenBBFundamentalProvider()
    if market_code == "CN":
        return TushareFundamentalProvider()
    return OpenBBFundamentalProvider()
