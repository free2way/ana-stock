#!/usr/bin/env python3
"""Read-only raw-lake capacity preflight for a complete-date training window."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import duckdb


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.market_lake import list_lake_trade_dates, market_lake_root
from app.services.stock_selection.training_window import estimate_training_fit_bytes


def audit_capacity(
    *,
    market: str,
    dates: list[str],
    paths: list[Path],
    requested_date_count: int,
    feature_count: int,
    max_rows: int,
    max_estimated_fit_bytes: int,
) -> dict:
    if market not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    if len(dates) != len(paths):
        raise ValueError("dates and paths must have equal length")
    for value, name in (
        (requested_date_count, "requested_date_count"),
        (feature_count, "feature_count"),
        (max_rows, "max_rows"),
        (max_estimated_fit_bytes, "max_estimated_fit_bytes"),
    ):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    missing = [str(path) for path in paths if not path.is_file()]
    rows: list[tuple] = []
    if paths and not missing:
        with duckdb.connect(database=":memory:") as connection:
            rows = connection.execute(
                """
                SELECT CAST(date AS VARCHAR) AS trade_date,
                       COUNT(*) AS row_count,
                       COUNT(DISTINCT symbol) AS symbol_count
                FROM read_parquet(?, hive_partitioning = true)
                GROUP BY 1 ORDER BY 1
                """,
                [[str(path) for path in paths]],
            ).fetchall()
    row_count = sum(int(row[1]) for row in rows)
    estimate = estimate_training_fit_bytes(sample_count=row_count, feature_count=feature_count)
    file_inventory = [
        {"path": str(path.resolve()), "size_bytes": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        for path in paths if path.is_file()
    ]
    source_manifest_sha256 = hashlib.sha256(
        json.dumps(file_inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    reasons = []
    if len(dates) < requested_date_count:
        reasons.append("insufficient_lake_partitions")
    if missing:
        reasons.append("missing_partition_files")
    if len(rows) != len(dates):
        reasons.append("partition_date_count_mismatch")
    if row_count > max_rows:
        reasons.append("raw_rows_exceed_row_budget")
    if int(estimate["estimated_fit_bytes"]) > max_estimated_fit_bytes:
        reasons.append("raw_rows_exceed_estimated_fit_budget")
    return {
        "schema_version": "training_window_capacity_preflight_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "READY" if not reasons else "BLOCKED",
        "block_reasons": reasons,
        "market": market,
        "scope": "raw_lake_upper_bound_not_mature_purged_or_full_market_training_proof",
        "requested_date_count": requested_date_count,
        "partition_count": len(dates),
        "start_date": min(dates) if dates else None,
        "end_date": max(dates) if dates else None,
        "row_count": row_count,
        "min_rows_per_date": min((int(row[1]) for row in rows), default=0),
        "max_rows_per_date": max((int(row[1]) for row in rows), default=0),
        "min_symbols_per_date": min((int(row[2]) for row in rows), default=0),
        "max_symbols_per_date": max((int(row[2]) for row in rows), default=0),
        "feature_count": feature_count,
        **estimate,
        "max_rows": max_rows,
        "max_estimated_fit_bytes": max_estimated_fit_bytes,
        "source_manifest_sha256": source_manifest_sha256,
        "source_files": file_inventory,
        "missing_files": missing,
        "production_training_started": False,
        "postgres_accessed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=("CN", "US"), default="CN")
    parser.add_argument("--date-count", type=int, default=252)
    parser.add_argument("--feature-count", type=int, default=28)
    parser.add_argument("--max-rows", type=int, default=1_500_000)
    parser.add_argument("--max-estimated-fit-bytes", type=int, default=2 * 1024**3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("receipt already exists; choose a new path")
    dates = list_lake_trade_dates(market=args.market)[: args.date_count]
    root = market_lake_root() / f"{args.market.lower()}_daily"
    paths = [root / f"date={trade_date}" / "part.parquet" for trade_date in dates]
    report = audit_capacity(
        market=args.market,
        dates=dates,
        paths=paths,
        requested_date_count=args.date_count,
        feature_count=args.feature_count,
        max_rows=args.max_rows,
        max_estimated_fit_bytes=args.max_estimated_fit_bytes,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    print(json.dumps({key: report[key] for key in (
        "status", "market", "partition_count", "start_date", "end_date", "row_count",
        "estimated_fit_bytes", "max_rows", "max_estimated_fit_bytes", "scope",
    )}, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
