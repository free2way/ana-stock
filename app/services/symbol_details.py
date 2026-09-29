from app.core.config import get_settings
from app.services.market_lake import load_lake_price_history
from app.services.ticker_format import lake_ticker_candidates


class SymbolDataService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def get_history(self, ticker: str, limit: int = 120) -> list[dict]:
        for market, symbol in self._lake_candidates(ticker):
            rows = load_lake_price_history(market=market, ticker=symbol, limit=limit)
            if rows:
                return rows
        return []

    def _lake_candidates(self, ticker: str) -> list[tuple[str, str]]:
        return lake_ticker_candidates(ticker)
