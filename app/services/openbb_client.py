from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


def _to_iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10] if len(text) >= 10 else text


@dataclass(slots=True)
class HistoricalPriceRequest:
    ticker: str
    start_date: str | None = None
    end_date: str | None = None
    provider: str = "yfinance"


class OpenBBClient:
    """Thin wrapper placeholder for the OpenBB SDK."""

    def fetch_historical_prices(self, request: HistoricalPriceRequest) -> list[dict]:
        try:
            from openbb import obb
        except ImportError as exc:
            raise RuntimeError(
                "OpenBB is not installed. Install project dependencies before syncing market data."
            ) from exc

        output = obb.equity.price.historical(
            symbol=request.ticker,
            start_date=request.start_date,
            end_date=request.end_date,
            provider=request.provider,
        )
        records = output.to_dict()

        normalized: list[dict] = []
        for record in records:
            normalized.append(
                {
                    "date": _to_iso_date(record.get("date")),
                    "symbol": request.ticker.upper(),
                    "open": record.get("open"),
                    "high": record.get("high"),
                    "low": record.get("low"),
                    "close": record.get("close"),
                    "volume": record.get("volume"),
                    "adj_close": record.get("adj_close") or record.get("adjClose"),
                    "dividend": record.get("dividend"),
                    "split_ratio": record.get("split_ratio") or record.get("splitRatio"),
                }
            )

        normalized.sort(key=lambda row: row["date"] or "")
        return normalized
