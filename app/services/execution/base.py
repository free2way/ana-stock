from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class ExecutionOrderRequest:
    ticker: str
    side: str
    quantity: float
    order_type: str = "market"
    limit_price: float | None = None
    market: str | None = None
    strategy_tag: str | None = None


class BaseExecutionGateway(ABC):
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, request: ExecutionOrderRequest) -> dict:
        raise NotImplementedError
