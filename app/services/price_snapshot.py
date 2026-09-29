from __future__ import annotations

from app.services.market_lake import load_lake_latest_closes, load_lake_latest_open_gaps, load_lake_price_history
from app.services.runtime_cache import get_or_set
from app.services.ticker_format import lake_ticker_candidates as _lake_candidates, normalize_ticker_for_market


def load_latest_close(ticker: str) -> float | None:
    normalized = str(ticker or "").strip().upper()
    if not normalized:
        return None

    def _load() -> float | None:
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
    us_tickers: list[str] = []
    cn_tickers: list[str] = []
    for ticker in tickers:
        normalized = str(ticker or "").strip().upper()
        if not normalized or normalized in values:
            continue
        values[normalized] = None
        if normalized.endswith((".SS", ".SZ", ".SH", ".BJ")):
            cn_tickers.append(normalized)
        elif not normalized.endswith(".HK"):
            us_tickers.append(normalized)

    for symbol, latest_value in load_lake_latest_closes(market="US", tickers=us_tickers).items():
        if symbol in values:
            values[symbol] = latest_value
    for symbol, latest_value in load_lake_latest_closes(market="CN", tickers=cn_tickers).items():
        if symbol in values:
            values[symbol] = latest_value

    for normalized in list(values.keys()):
        if values[normalized] is None:
            values[normalized] = load_latest_close(normalized)
    return values


def load_latest_open_gaps(tickers: list[str]) -> dict[str, float | None]:
    """Latest signal-day open gap per ticker, in percent (5.2 == +5.2%).

    The lake stores raw fractions, but every ``*_pct`` consumer — including the
    tradability gap-chase ceiling (percent) — compares like units, so this
    delivery boundary is the single decimal→percent conversion point.  Lake
    batch only: no per-ticker fallback, because the gap-chase rule must treat
    a missing reading as "unknown" rather than guessing from stale sources.
    """
    values: dict[str, float | None] = {}
    us_tickers: list[str] = []
    cn_tickers: list[str] = []
    for ticker in tickers:
        normalized = str(ticker or "").strip().upper()
        if not normalized or normalized in values:
            continue
        values[normalized] = None
        if normalized.endswith((".SS", ".SZ", ".SH", ".BJ")):
            cn_tickers.append(normalized)
        elif not normalized.endswith(".HK"):
            us_tickers.append(normalized)

    for source_market, chunk in (("US", us_tickers), ("CN", cn_tickers)):
        for symbol, gap in load_lake_latest_open_gaps(market=source_market, tickers=chunk).items():
            if symbol in values and gap is not None:
                values[symbol] = round(gap * 100.0, 4)
    return values


def load_daily_change_pct(*, market: str | None, ticker: str) -> float | None:
    market_value = str(market or "").strip().upper()
    normalized_ticker = normalize_ticker_for_market(ticker, market_value)
    if market_value not in {"CN", "US"} or not normalized_ticker:
        return None
    rows = load_lake_price_history(market=market_value, ticker=normalized_ticker, limit=2)
    if len(rows) < 2:
        return None
    latest = rows[-1]
    previous = rows[-2]
    latest_close = latest.get("close") or latest.get("adj_close")
    previous_close = previous.get("close") or previous.get("adj_close")
    try:
        latest_value = float(latest_close)
        previous_value = float(previous_close)
    except (TypeError, ValueError):
        return None
    if previous_value == 0:
        return None
    return ((latest_value / previous_value) - 1.0) * 100.0
