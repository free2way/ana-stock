from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.market_lake import load_lake_latest_metrics, list_lake_symbols
from app.services.repository import SymbolRepository


COMMON_STOCK_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
BAD_TICKER_SUFFIXES = (
    ".WS",
    ".WT",
    ".W",
    ".U",
    ".R",
    "WS",
    "WT",
    "W",
    "U",
    "R",
)
BAD_NAME_KEYWORDS = (
    "WARRANT",
    "RIGHT",
    "UNIT",
    "PREFERRED",
    "PREFERENCE",
    "PFD",
    "NOTE",
    "BOND",
    "ETF",
    "ETN",
    "FUND",
    "INVERSE",
    "LEVERAGED",
    "TRADING--",
    "ACQUISITION CORP",
    "ACQUISITION CORPORATION",
    "SPECIAL PURPOSE ACQUISITION",
)


def is_known_us_non_common_security(
    ticker: str,
    name: str | None = None,
    metadata_text: str | None = None,
) -> tuple[bool, str | None]:
    """Return true only when metadata clearly identifies a non-common-stock security.

    Unlike ``is_probable_us_common_stock_ticker`` this helper does not reject weak
    metadata names. It is safe to use at the ingestion layer where dropping an
    unknown ticker could accidentally remove a real common stock.
    """
    normalized = str(ticker or "").strip().upper()
    normalized_name = str(name or "").strip().upper()
    normalized_metadata = str(metadata_text or "").strip().upper()
    if not normalized:
        return True, "missing_ticker"
    if normalized.endswith(BAD_TICKER_SUFFIXES):
        return True, "non_common_ticker_suffix"
    for keyword in BAD_NAME_KEYWORDS:
        if keyword in normalized_name or keyword in normalized_metadata:
            return True, f"name_contains_{keyword.lower().replace(' ', '_')}"
    return False, None


@dataclass(frozen=True)
class USTradeUniverseConfig:
    min_price: float
    min_avg_dollar_volume: float
    min_avg_volume: float
    min_history_days: int
    lookback_days: int = 60


def default_us_trade_universe_config() -> USTradeUniverseConfig:
    settings = get_settings()
    return USTradeUniverseConfig(
        min_price=max(0.0, float(settings.us_trade_universe_min_price or 0.0)),
        min_avg_dollar_volume=max(0.0, float(settings.us_trade_universe_min_avg_dollar_volume or 0.0)),
        min_avg_volume=max(0.0, float(settings.us_trade_universe_min_avg_volume or 0.0)),
        min_history_days=max(1, int(settings.us_trade_universe_min_history_days or 30)),
    )


def is_probable_us_common_stock_ticker(ticker: str, name: str | None = None, metadata_text: str | None = None) -> tuple[bool, str | None]:
    normalized = str(ticker or "").strip().upper()
    normalized_name = str(name or "").strip().upper()
    normalized_metadata = str(metadata_text or "").strip().upper()
    if not normalized:
        return False, "missing_ticker"
    if not COMMON_STOCK_TICKER_RE.match(normalized):
        return False, "non_common_ticker_format"
    if normalized.endswith(BAD_TICKER_SUFFIXES):
        return False, "non_common_ticker_suffix"
    if not normalized_name or normalized_name == normalized:
        return False, "weak_metadata_name"
    for keyword in BAD_NAME_KEYWORDS:
        if keyword in normalized_name or keyword in normalized_metadata:
            return False, f"name_contains_{keyword.lower().replace(' ', '_')}"
    return True, None


def build_us_trade_universe(
    *,
    tickers: list[str] | set[str] | tuple[str, ...] | None = None,
    config: USTradeUniverseConfig | None = None,
    include_summary: bool = False,
) -> list[str] | tuple[list[str], dict]:
    cfg = config or default_us_trade_universe_config()
    source_tickers = sorted({str(item or "").strip().upper() for item in (tickers or list_lake_symbols(market="US")) if str(item or "").strip()})
    if not source_tickers:
        summary = {"raw_count": 0, "eligible_count": 0, "rejected_count": 0, "reasons": {}}
        return ([], summary) if include_summary else []

    with SessionLocal() as db:
        overview_map = SymbolRepository(db).list_overviews_for_tickers(source_tickers)
    metrics_map = load_lake_latest_metrics(market="US", tickers=source_tickers, lookback_days=cfg.lookback_days)

    eligible: list[str] = []
    rejected_reasons: dict[str, int] = {}
    examples: dict[str, list[str]] = {}

    def reject(ticker: str, reason: str) -> None:
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
        bucket = examples.setdefault(reason, [])
        if len(bucket) < 5:
            bucket.append(ticker)

    for ticker in source_tickers:
        overview = overview_map.get(ticker) or {}
        metadata_text = " ".join(
            str(overview.get(key) or "")
            for key in ("name", "exchange", "sector", "industry")
        )
        is_common, reason = is_probable_us_common_stock_ticker(
            ticker,
            overview.get("name"),
            metadata_text=metadata_text,
        )
        if not is_common:
            reject(ticker, reason or "not_common_stock")
            continue
        metrics = metrics_map.get(ticker) or {}
        try:
            latest_close = float(metrics.get("latest_close") or 0.0)
            avg_volume = float(metrics.get("avg_volume") or 0.0)
            avg_dollar_volume = float(metrics.get("avg_dollar_volume") or 0.0)
            history_days = int(metrics.get("history_days") or 0)
            duplicate_conflict_days = int(metrics.get("duplicate_conflict_days") or 0)
        except (TypeError, ValueError):
            reject(ticker, "invalid_lake_metrics")
            continue
        if duplicate_conflict_days > 0:
            reject(ticker, "duplicate_price_conflict")
            continue
        if history_days < cfg.min_history_days:
            reject(ticker, "insufficient_history")
            continue
        if latest_close < cfg.min_price:
            reject(ticker, "low_price")
            continue
        if avg_volume < cfg.min_avg_volume:
            reject(ticker, "low_avg_volume")
            continue
        if avg_dollar_volume < cfg.min_avg_dollar_volume:
            reject(ticker, "low_avg_dollar_volume")
            continue
        eligible.append(ticker)

    summary = {
        "raw_count": len(source_tickers),
        "eligible_count": len(eligible),
        "rejected_count": len(source_tickers) - len(eligible),
        "reasons": dict(sorted(rejected_reasons.items(), key=lambda item: (-item[1], item[0]))),
        "examples": examples,
        "thresholds": {
            "min_price": cfg.min_price,
            "min_avg_dollar_volume": cfg.min_avg_dollar_volume,
            "min_avg_volume": cfg.min_avg_volume,
            "min_history_days": cfg.min_history_days,
            "lookback_days": cfg.lookback_days,
        },
    }
    return (eligible, summary) if include_summary else eligible
