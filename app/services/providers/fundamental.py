from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
import gzip
import json
import threading
import time

from urllib.request import Request, urlopen

from app.core.config import get_settings
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


class GlobalStockDataSECFundamentalProvider(BaseFundamentalProvider):
    """Official SEC EDGAR company-facts adapter documented by global-stock-data.

    It intentionally supplies filing-derived fundamentals only.  Price and
    valuation fields remain owned by their licensed market-data providers.
    """

    name = "global_stock_data_sec_edgar"

    _ticker_to_cik: dict[str, tuple[int, str]] | None = None
    _request_lock = threading.Lock()
    _last_request_at = 0.0
    _min_request_interval_seconds = 0.125  # Stay below SEC's 10 req/s ceiling.

    def __init__(self) -> None:
        super().__init__()
        self.settings = get_settings()

    def fetch_snapshot(self, ticker: str) -> dict | None:
        if not self.settings.sec_user_agent:
            self.last_source_used = "sec_edgar_not_configured"
            return None
        normalized = str(ticker or "").strip().upper().replace(".", "-")
        ticker_map = self._load_ticker_map()
        listing = ticker_map.get(normalized)
        if listing is None:
            self.last_source_used = "sec_edgar_ticker_not_found"
            return None
        cik, company_name = listing
        try:
            facts = self._get_json(
                f"{str(self.settings.sec_data_endpoint).rstrip('/')}/api/xbrl/companyfacts/CIK{cik:010d}.json"
            )
        except Exception:
            self.last_source_used = "sec_edgar_unavailable"
            return None
        snapshot = self._facts_to_snapshot(ticker=str(ticker or "").strip().upper(), cik=cik, company_name=company_name, facts=facts)
        self.last_source_used = self.name if snapshot else "sec_edgar_no_annual_facts"
        return snapshot

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": str(self.settings.sec_user_agent),
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }

    def _get_json(self, url: str) -> dict:
        with self.__class__._request_lock:
            elapsed = time.monotonic() - self.__class__._last_request_at
            wait_seconds = self.__class__._min_request_interval_seconds - elapsed
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self.__class__._last_request_at = time.monotonic()
        request = Request(url, headers=self._headers())
        with urlopen(request, timeout=float(self.settings.sec_timeout_seconds)) as response:
            body = response.read()
            if str(response.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))

    def _load_ticker_map(self) -> dict[str, tuple[int, str]]:
        if self.__class__._ticker_to_cik is not None:
            return self.__class__._ticker_to_cik
        payload = self._get_json(str(self.settings.sec_company_tickers_endpoint))
        mapping: dict[str, tuple[int, str]] = {}
        for row in payload.values() if isinstance(payload, dict) else []:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            try:
                cik = int(row.get("cik_str"))
            except (TypeError, ValueError):
                continue
            if ticker:
                mapping[ticker] = (cik, str(row.get("title") or ticker))
        self.__class__._ticker_to_cik = mapping
        return mapping

    @staticmethod
    def _annual_values(facts: dict, tags: tuple[str, ...]) -> list[dict]:
        us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        for tag in tags:
            units = (us_gaap.get(tag) or {}).get("units") or {}
            values = units.get("USD") or []
            annual = [
                item for item in values
                if str(item.get("form") or "") in {"10-K", "20-F"}
                and str(item.get("fp") or "").upper() == "FY"
                and str(item.get("end") or "")
            ]
            if annual:
                deduped: dict[str, dict] = {}
                for item in annual:
                    end = str(item.get("end"))
                    # Prefer an amended/most recently filed value for the same end date.
                    if end not in deduped or str(item.get("filed") or "") >= str(deduped[end].get("filed") or ""):
                        deduped[end] = item
                return [deduped[key] for key in sorted(deduped, reverse=True)]
        return []

    @staticmethod
    def _value(values: list[dict], index: int = 0) -> float | None:
        try:
            return float(values[index].get("val"))
        except (IndexError, TypeError, ValueError):
            return None

    @staticmethod
    def _yoy(values: list[dict]) -> float | None:
        current = GlobalStockDataSECFundamentalProvider._value(values, 0)
        prior = GlobalStockDataSECFundamentalProvider._value(values, 1)
        if current is None or prior in (None, 0):
            return None
        return (current / prior - 1.0) * 100.0

    def _facts_to_snapshot(self, *, ticker: str, cik: int, company_name: str, facts: dict) -> dict | None:
        revenue = self._annual_values(facts, ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"))
        net_income = self._annual_values(facts, ("NetIncomeLoss",))
        assets = self._annual_values(facts, ("Assets",))
        liabilities = self._annual_values(facts, ("Liabilities",))
        anchor = revenue or net_income or assets
        if not anchor:
            return None
        asset_value = self._value(assets)
        liability_value = self._value(liabilities)
        report_date = str(anchor[0].get("end"))
        return {
            "ticker": ticker,
            "report_date": report_date,
            "name": company_name,
            "exchange": None,
            "listing_date": None,
            "pe_ttm": None,
            "dividend_yield": None,
            "market_cap": None,
            "roe_avg_3y": None,
            "net_profit_yoy": self._yoy(net_income),
            "revenue_yoy": self._yoy(revenue),
            "debt_to_assets": ((liability_value / asset_value) * 100.0 if asset_value and liability_value is not None else None),
            "raw_data": {
                "provider": self.name,
                "source_url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                "cik": f"{cik:010d}",
                "as_of_date": report_date,
                "annual_revenue_usd": self._value(revenue),
                "annual_net_income_usd": self._value(net_income),
                "annual_assets_usd": asset_value,
                "annual_liabilities_usd": liability_value,
                "fetched_on": date.today().isoformat(),
            },
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
    if normalized in {"global_stock_data", "global_stock_data_sec", "sec", "sec_edgar"} and market_code == "US":
        return GlobalStockDataSECFundamentalProvider()
    if market_code == "CN":
        return TushareFundamentalProvider()
    return OpenBBFundamentalProvider()
