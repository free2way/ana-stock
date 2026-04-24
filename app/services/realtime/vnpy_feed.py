from __future__ import annotations

from app.services.realtime.base import BaseRealtimeFeed


class VnpyRealtimeFeedPlaceholder(BaseRealtimeFeed):
    name = "vnpy_realtime"

    def is_configured(self) -> bool:
        return False

    def describe(self) -> dict:
        return {
            "name": self.name,
            "status": "not_configured",
            "message": "vn.py realtime feed is reserved for a later integration stage.",
        }
