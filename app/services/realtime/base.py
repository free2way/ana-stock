from __future__ import annotations

from abc import ABC, abstractmethod


class BaseRealtimeFeed(ABC):
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def describe(self) -> dict:
        raise NotImplementedError
