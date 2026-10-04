"""Read the versioned adjusted view for label construction (A1).

The audit requires training labels to consume the adjusted price basis instead
of raw closes that break on ex-dates. This store reads the persisted qfq view
(`data/lake/_adjusted_v2/<market>/method=qfq/adjusted.parquet`) and exposes a
per-symbol lookup keyed by trade date.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from app.core.config import get_settings

ADJUSTED_FIELDS = ("open", "high", "low", "close")
DEFAULT_METHOD = "qfq"


def adjusted_view_path(market: str, *, method: str = DEFAULT_METHOD, root: Path | None = None) -> Path:
    base = Path(root) if root is not None else get_settings().data_dir / "lake"
    return base / "_adjusted_v2" / str(market).strip().lower() / f"method={method}" / "adjusted.parquet"


def load_adjusted_bars(
    market: str,
    *,
    method: str = DEFAULT_METHOD,
    symbols: set[str] | None = None,
    root: Path | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """`{symbol: {date: {open/high/low/close: float}}}` from the adjusted view.

    Returns an empty mapping when the view does not exist (the caller falls
    back to raw closes and the label basis stays explicit in the run metadata).
    """

    path = adjusted_view_path(market, method=method, root=root)
    if not path.exists():
        return {}
    params: list[object] = [str(path)]
    where = ""
    if symbols:
        where = "WHERE symbol IN (SELECT unnest($symbols))"
        params = {"path": str(path), "symbols": sorted(str(item).strip().upper() for item in symbols)}
        query = (
            f"SELECT symbol, CAST(date AS VARCHAR) AS d, open, high, low, close "
            f"FROM read_parquet($path) {where} ORDER BY symbol, date"
        )
        rows = duckdb.sql(query, params=params).fetchall()
    else:
        rows = duckdb.sql(
            "SELECT symbol, CAST(date AS VARCHAR) AS d, open, high, low, close "
            "FROM read_parquet(?) ORDER BY symbol, date",
            params=params,
        ).fetchall()
    output: dict[str, dict[str, dict[str, float]]] = {}
    for symbol, trade_date, open_price, high, low, close in rows:
        output.setdefault(str(symbol).strip().upper(), {})[str(trade_date)[:10]] = {
            "open": float(open_price or 0.0),
            "high": float(high or 0.0),
            "low": float(low or 0.0),
            "close": float(close or 0.0),
        }
    return output
