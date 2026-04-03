import csv
from pathlib import Path


class MarketDataNormalizer:
    """Placeholder normalization service for raw market data files."""

    def normalize_symbol_file(self, source_path: Path, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with source_path.open("r", newline="", encoding="utf-8") as source_file:
            reader = csv.DictReader(source_file)
            rows = []
            for row in reader:
                rows.append(
                    {
                        "date": row.get("date"),
                        "symbol": row.get("symbol"),
                        "open": row.get("open"),
                        "high": row.get("high"),
                        "low": row.get("low"),
                        "close": row.get("close"),
                        "volume": row.get("volume"),
                        "adj_close": row.get("adj_close"),
                        "dividend": row.get("dividend"),
                        "split_ratio": row.get("split_ratio"),
                    }
                )

        rows.sort(key=lambda row: (row["symbol"] or "", row["date"] or ""))

        with target_path.open("w", newline="", encoding="utf-8") as target_file:
            writer = csv.DictWriter(
                target_file,
                fieldnames=[
                    "date",
                    "symbol",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "adj_close",
                    "dividend",
                    "split_ratio",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)

        return target_path
