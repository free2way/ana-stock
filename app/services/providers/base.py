from __future__ import annotations

from abc import ABC, abstractmethod

from app.services.openbb_client import HistoricalPriceRequest


class BasePriceProvider(ABC):
    name: str = "base"

    def __init__(self) -> None:
        self.last_source_used = self.name

    @abstractmethod
    def fetch_historical_prices(self, request: HistoricalPriceRequest) -> list[dict]:
        raise NotImplementedError
