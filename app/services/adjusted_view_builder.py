"""Build the versioned adjusted price view from raw bars plus corporate actions (C1).

This is the shared implementation behind ``scripts/rebuild_adjusted_view.py``
and the scheduled U.S. close pipeline stage. Prices are never adjusted in
place: the builder derives a versioned series (``adjusted_view``) from a raw
namespace plus the corporate-action store.

The U.S. raw namespace must be a single clean basis
(``data/lake/_us_alpaca/raw/*.parquet``); the legacy ``us_daily`` lake mixes
Polygon split-adjusted rows with Alpaca raw repairs, so rebuilding on top of it
would double-adjust part of the series.

Determinism: the report records per-symbol adjustment versions and a combined
digest, so callers can rebuild twice and compare.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import polars as pl

from app.services.adjusted_view import (
    adjustment_version,
    build_adjusted_series,
    raw_series_digest,
)
from app.services.adjusted_view_store import adjusted_view_path
from app.services.corporate_actions import load_actions
from app.services.market_lake import market_lake_root

ADJUSTED_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.String,
    "symbol": pl.String,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "adj_multiplier": pl.Float64,
    "adjustment_method": pl.String,
    "adj_version": pl.String,
}

# The single-basis raw namespace used to rebuild the U.S. adjusted view.
US_RAW_NAMESPACE = ("_us_alpaca", "raw", "*.parquet")


def us_adjusted_raw_glob(lake_root: Path | None = None) -> str:
    base = Path(lake_root) if lake_root is not None else market_lake_root()
    return str(base.joinpath(*US_RAW_NAMESPACE))


def load_raw_rows(
    market: str,
    *,
    limit_partitions: int = 0,
    symbols: list[str] | None = None,
    raw_glob: str | None = None,
) -> list[dict]:
    pattern = raw_glob or str(market_lake_root() / f"{market.lower()}_daily" / "date=*" / "*.parquet")
    files = sorted((market_lake_root() / f"{market.lower()}_daily").glob("date=*/*.parquet"))
    if not raw_glob and not files:
        raise RuntimeError(f"no v1 partitions for {market}")
    if limit_partitions and not raw_glob:
        keep = sorted({path.parent.name for path in files})[-limit_partitions:]
        pattern = str(market_lake_root() / f"{market.lower()}_daily" / "date=*" / "*.parquet")
        rows = duckdb.sql(
            "SELECT CAST(date AS VARCHAR) AS date, symbol, open, high, low, close, volume "
            "FROM read_parquet(?, hive_partitioning=true) "
            "WHERE CAST(date AS DATE) >= CAST(? AS DATE) ORDER BY symbol, date",
            params=[pattern, keep[0].split("=")[-1]],
        ).fetchall()
    else:
        rows = duckdb.sql(
            "SELECT CAST(date AS VARCHAR) AS date, symbol, open, high, low, close, volume "
            "FROM read_parquet(?, hive_partitioning=true) ORDER BY symbol, date",
            params=[pattern],
        ).fetchall()
    records = [
        {
            "date": row[0],
            "symbol": str(row[1]).upper(),
            "open": float(row[2] or 0.0),
            "high": float(row[3] or 0.0),
            "low": float(row[4] or 0.0),
            "close": float(row[5] or 0.0),
            "volume": float(row[6] or 0.0),
        }
        for row in rows
    ]
    if symbols:
        wanted = {item.upper() for item in symbols}
        records = [row for row in records if row["symbol"] in wanted]
    return records


def build_market_view(
    market: str,
    *,
    method: str,
    limit_partitions: int = 0,
    limit_symbols: int = 0,
    raw_glob: str | None = None,
) -> tuple[list[dict], dict]:
    rows = load_raw_rows(market, limit_partitions=limit_partitions, raw_glob=raw_glob)
    actions = load_actions(market)
    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)
    symbols = sorted(by_symbol)
    if limit_symbols:
        symbols = symbols[:limit_symbols]

    adjusted_rows: list[dict] = []
    version_digest = hashlib.sha256()
    raw_digest = hashlib.sha256()
    totals = {"events_total": 0, "events_applied": 0, "events_skipped_no_trade_date": 0, "events_skipped_unusable": 0}
    for symbol in symbols:
        bars = by_symbol[symbol]
        symbol_actions = [action for action in actions if action.symbol == symbol]
        digest = raw_series_digest(bars)
        version = adjustment_version(method=method, raw_digest=digest, actions=symbol_actions)
        adjusted, stats = build_adjusted_series(bars, symbol_actions, method=method)
        totals["events_total"] += stats.events_total
        totals["events_applied"] += stats.events_applied
        totals["events_skipped_no_trade_date"] += stats.events_skipped_no_trade_date
        totals["events_skipped_unusable"] += stats.events_skipped_unusable
        raw_digest.update(f"{symbol}:{digest}|".encode("utf-8"))
        version_digest.update(f"{symbol}:{version}|".encode("utf-8"))
        for item in adjusted:
            row = item.as_row()
            row["symbol"] = symbol
            row["adj_version"] = version
            adjusted_rows.append(row)
    report = {
        "market": market,
        "method": method,
        "symbols": len(symbols),
        "rows": len(adjusted_rows),
        "actions_loaded": len(actions),
        "action_stats": totals,
        "raw_digest": raw_digest.hexdigest()[:16],
        "version_digest": version_digest.hexdigest()[:16],
    }
    return adjusted_rows, report


def write_view(market: str, method: str, rows: list[dict], report: dict, *, output_root: Path) -> dict:
    target_dir = output_root / market.lower() / f"method={method}"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "adjusted.parquet"
    frame = pl.DataFrame(rows, schema=ADJUSTED_SCHEMA, orient="row").sort(["symbol", "date"])
    temporary = path.with_name(f".{path.name}.tmp")
    frame.write_parquet(temporary, compression="zstd")
    temporary.replace(path)
    file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        **report,
        "schema_version": "adjusted_view_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "parquet": str(path),
        "parquet_sha256": file_sha,
    }
    (target_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def adjusted_view_latest_date(market: str, *, method: str = "qfq", root: Path | None = None) -> str | None:
    """Return the newest trade date present in the persisted adjusted view."""

    path = adjusted_view_path(market, method=method, root=root)
    if not path.exists():
        return None
    value = duckdb.sql(
        "SELECT max(CAST(date AS DATE)) FROM read_parquet(?)", params=[str(path)]
    ).fetchone()[0]
    return value.isoformat() if value else None


def rebuild_adjusted_view_if_stale(
    market: str,
    *,
    method: str = "qfq",
    raw_glob: str | None = None,
    required_upper_bound: str | None = None,
    lake_root: Path | None = None,
) -> dict:
    """Rebuild ``market``'s adjusted view unless it already covers the bound.

    ``required_upper_bound`` is the newest trade date the caller needs the view
    to reach (typically the freshly refreshed raw lake date). When the persisted
    view already reaches it the expensive full rebuild is skipped, so a
    scheduled stage never recomputes the whole view on every run.
    """

    resolved_root = Path(lake_root) if lake_root is not None else market_lake_root()
    latest = adjusted_view_latest_date(market, method=method, root=resolved_root)
    required = str(required_upper_bound or "").strip() or None
    if required and latest and latest >= required:
        return {
            "status": "skipped",
            "market": market,
            "method": method,
            "latest_date": latest,
            "required_date": required,
        }
    rows, report = build_market_view(market, method=method, raw_glob=raw_glob)
    manifest = write_view(market, method, rows, report, output_root=resolved_root / "_adjusted_v2")
    return {
        "status": "rebuilt",
        "market": market,
        "method": method,
        "previous_latest_date": latest,
        "required_date": required,
        "symbols": report["symbols"],
        "rows": report["rows"],
        "manifest": {
            key: manifest[key] for key in ("parquet", "parquet_sha256", "generated_at")
        },
    }
