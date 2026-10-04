"""Rebuild the adjusted price view from raw bars plus corporate actions (C1).

CN: raw TuShare bars + dividend-derived actions -> qfq/hfq series.
US: refuses to run unless explicitly forced; the legacy US lake mixes Polygon
    split-adjusted rows with Alpaca raw repairs, so rebuilding on top would
    double-adjust part of the series. Basis must be re-established first.

Determinism: the report records per-symbol adjustment versions and a combined
digest; --verify-determinism builds twice and compares both digests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import polars as pl  # noqa: E402

from app.services.adjusted_view import (  # noqa: E402
    adjustment_version,
    build_adjusted_series,
    raw_series_digest,
)
from app.services.corporate_actions import load_actions  # noqa: E402
from app.services.market_lake import market_lake_root  # noqa: E402

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["CN", "US"], required=True)
    parser.add_argument("--method", choices=["qfq", "hfq"], default="qfq")
    parser.add_argument("--limit-partitions", type=int, default=0)
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--output-root", default=str(ROOT / "data" / "lake" / "_adjusted_v2"))
    parser.add_argument("--raw-glob", default=None, help="single-basis raw namespace (required for US)")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--verify-determinism", action="store_true")
    parser.add_argument("--force-mixed-basis", action="store_true", help="US only: bypass the basis guard")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    if args.market == "US" and not (args.raw_glob or args.force_mixed_basis):
        print(json.dumps({
            "status": "blocked",
            "reason": (
                "US lake mixes Polygon adjusted rows with Alpaca raw repairs; rebuilding on top would "
                "double-adjust part of the series. Pass --raw-glob pointing at the single-basis Alpaca "
                "namespace (data/lake/_us_alpaca/raw/*.parquet) or re-establish one basis first."
            ),
        }, indent=2))
        return 2

    rows, report = build_market_view(
        args.market,
        method=args.method,
        limit_partitions=args.limit_partitions,
        limit_symbols=args.limit_symbols,
        raw_glob=args.raw_glob,
    )
    if args.verify_determinism:
        _, second = build_market_view(
            args.market,
            method=args.method,
            limit_partitions=args.limit_partitions,
            limit_symbols=args.limit_symbols,
            raw_glob=args.raw_glob,
        )
        report["determinism"] = {
            "raw_digest_match": report["raw_digest"] == second["raw_digest"],
            "version_digest_match": report["version_digest"] == second["version_digest"],
        }
        if not all(report["determinism"].values()):
            raise SystemExit("determinism check failed")
    if args.write:
        manifest = write_view(args.market, args.method, rows, report, output_root=Path(args.output_root))
        report["manifest"] = {key: manifest[key] for key in ("parquet", "parquet_sha256", "generated_at")}
    status = {"status": "ok", **report}
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(status, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(status, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
