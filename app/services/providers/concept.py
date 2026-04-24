from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.ticker_format import normalize_ticker_for_market
from app.services.tushare_client import TushareClient


class BaseConceptProvider(ABC):
    name: str = "base"

    def __init__(self) -> None:
        self.last_source_used = self.name

    @abstractmethod
    def fetch_memberships(self, ticker: str) -> list[dict]:
        raise NotImplementedError


class TushareConceptProvider(BaseConceptProvider):
    name = "tushare_concept"

    def __init__(self) -> None:
        super().__init__()
        self.client = TushareClient()

    def fetch_memberships(self, ticker: str) -> list[dict]:
        if not self.client.is_configured():
            self.last_source_used = "tushare_unavailable"
            return []
        rows = self.client.fetch_cn_concepts([normalize_ticker_for_market(ticker, "CN")])
        self.last_source_used = self.name if rows else "tushare_concept_empty"
        return [
            {
                "ticker": normalize_ticker_for_market(row.ticker, "CN"),
                "concept_name": row.concept_name,
                "concept_code": row.concept_code,
                "report_date": row.report_date,
                "name": row.name,
                "raw_data": row.raw_data,
            }
            for row in rows
        ]


def resolve_concept_provider(name: str | None, *, market: str | None = None) -> BaseConceptProvider:
    normalized = str(name or "").strip().lower()
    market_code = str(market or "").strip().upper()
    if normalized in {"tushare", "tushare_concept", "", "auto"} or market_code == "CN":
        return TushareConceptProvider()
    return TushareConceptProvider()
