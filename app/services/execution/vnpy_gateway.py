from __future__ import annotations

from app.services.execution.base import BaseExecutionGateway, ExecutionOrderRequest


class VnpyGatewayPlaceholder(BaseExecutionGateway):
    """
    Placeholder gateway for future vn.py integration.

    This makes the intended architecture explicit before we wire live
    event loops, account sessions, and broker-specific mappings.
    """

    name = "vnpy"

    def is_configured(self) -> bool:
        return False

    def submit_order(self, request: ExecutionOrderRequest) -> dict:
        return {
            "status": "not_configured",
            "gateway": self.name,
            "message": "vn.py execution gateway is not wired yet.",
            "request": {
                "ticker": request.ticker,
                "side": request.side,
                "quantity": request.quantity,
                "order_type": request.order_type,
                "limit_price": request.limit_price,
                "market": request.market,
                "strategy_tag": request.strategy_tag,
            },
        }
