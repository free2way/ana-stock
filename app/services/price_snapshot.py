from __future__ import annotations

import csv
from pathlib import Path

from app.core.config import get_settings
from app.services.market_lake import load_lake_price_history
from app.services.runtime_cache import get_or_set
from app.services.ticker_format import market_ticker_candidates


def _candidate_paths(ticker: str) -> list[Path]:
    settings = get_settings()
    normalized = settings.normalized_data_dir / f"{ticker}.csv"
    raw = settings.raw_data_dir / f"{ticker}.csv"
    return [normalized, raw]


def load_latest_close(ticker: str) -> float | None:
    normalized = str(ticker or "").strip().upper()
    if not normalized:
        return None

    def _load() -> float | None:
        for path in _candidate_paths(normalized):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle))
            except Exception:
                continue
            if not rows:
                continue
            latest = rows[-1]
            for field in ("close", "adj_close", "latest_close"):
                value = latest.get(field)
                if value in {None, ""}:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        for market, symbol in _lake_candidates(normalized):
            rows = load_lake_price_history(market=market, ticker=symbol, limit=1)
            if not rows:
                continue
            latest = rows[-1]
            for field in ("close", "adj_close", "latest_close"):
                value = latest.get(field)
                if value in {None, ""}:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    return get_or_set("latest_local_close", normalized, ttl_seconds=60.0, loader=_load)


def load_latest_closes(tickers: list[str]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for ticker in tickers:
        normalized = str(ticker or "").strip().upper()
        if not normalized:
            continue
        values[normalized] = load_latest_close(normalized)
    return values


def _lake_candidates(ticker: str) -> list[tuple[str, str]]:
    upper = ticker.upper().strip()
    if not upper:
        return []
    if upper.endswith((".SS", ".SZ", ".SH", ".BJ")) or (upper.isdigit() and len(upper) == 6):
        return [("CN", candidate) for candidate in market_ticker_candidates(upper, "CN")]
    if upper.endswith(".HK"):
        return []
    return [("US", upper)]
