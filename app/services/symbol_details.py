import csv
from pathlib import Path

from app.core.config import get_settings


class SymbolDataService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def get_history(self, ticker: str, limit: int = 120) -> list[dict]:
        path = self.settings.normalized_data_dir / f"{ticker.upper()}.csv"
        if not path.exists():
            return []

        with path.open("r", newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            rows = [
                {
                    "date": row.get("date"),
                    "symbol": row.get("symbol"),
                    "open": float(row["open"]) if row.get("open") else None,
                    "high": float(row["high"]) if row.get("high") else None,
                    "low": float(row["low"]) if row.get("low") else None,
                    "close": float(row["close"]) if row.get("close") else None,
                    "volume": float(row["volume"]) if row.get("volume") else None,
                    "adj_close": float(row["adj_close"]) if row.get("adj_close") else None,
                }
                for row in reader
            ]

        rows.sort(key=lambda item: item["date"] or "")
        return rows[-limit:]
