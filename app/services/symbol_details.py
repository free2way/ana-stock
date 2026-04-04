import csv
from pathlib import Path

from app.core.config import get_settings
from app.services.ticker_format import market_ticker_candidates


class SymbolDataService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def get_history(self, ticker: str, limit: int = 120) -> list[dict]:
        for path in self._candidate_paths(ticker):
            if not path.exists():
                continue
            rows = self._read_rows(path, ticker)
            if rows:
                rows.sort(key=lambda item: item["date"] or "")
                return rows[-limit:]
        return []

    def _candidate_paths(self, ticker: str) -> list[Path]:
        upper = ticker.upper()
        market = "HK" if upper.endswith(".HK") else "CN" if upper.endswith((".SS", ".SZ", ".SH")) else None
        candidates = market_ticker_candidates(upper, market) if market else [upper]

        paths: list[Path] = []
        for candidate in candidates:
            paths.append(self.settings.normalized_data_dir / f"{candidate}.csv")
            paths.append(self.settings.raw_data_dir / f"{candidate}.csv")
            if candidate.endswith(".HK"):
                paths.append(self.settings.normalized_data_dir / f"{candidate[:-3]}.csv")
                paths.append(self.settings.raw_data_dir / f"{candidate[:-3]}.csv")
        return paths

    def _read_rows(self, path: Path, requested_ticker: str) -> list[dict]:
        acceptable_symbols = {candidate.upper() for candidate in market_ticker_candidates(requested_ticker.upper(), "HK" if requested_ticker.upper().endswith(".HK") else None)}
        acceptable_symbols.add(requested_ticker.upper())

        with path.open("r", newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            rows = []
            for row in reader:
                symbol_value = (row.get("symbol") or requested_ticker).upper()
                if acceptable_symbols and symbol_value not in acceptable_symbols:
                    if requested_ticker.upper().endswith(".HK"):
                        raw_symbol = symbol_value.replace(".HK", "")
                        requested_raw = requested_ticker.upper().replace(".HK", "")
                        if raw_symbol.lstrip("0") != requested_raw.lstrip("0"):
                            continue
                    else:
                        continue
                rows.append(
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
                )
        return rows
