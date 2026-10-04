"""Backfill the provenance shadow (data/lake/_lake_v2) from the v1 lake.

Read-only with respect to v1: reads partitions and writes only the shadow
namespace through the same validated writer used by live syncs. Idempotent.

Usage:
    python scripts/backfill_lake_v2_provenance.py --market ALL --limit-partitions 20
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import polars as pl  # noqa: E402

from app.services.market_lake import (  # noqa: E402
    LAKE_V2_PROVENANCE_COLUMNS,
    market_lake_root,
    write_lake_v2_partition,
)


def _partition_files(market: str, limit: int | None) -> list[Path]:
    files = sorted((market_lake_root() / f"{market.lower()}_daily").glob("date=*/*.parquet"))
    if limit:
        files = files[-limit:]
    return files


def backfill_market(market: str, *, limit: int | None) -> dict:
    summary = {
        "market": market,
        "partitions": 0,
        "rows": 0,
        "errors": [],
    }
    batch_id = f"backfill-lake-v2-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    for path in _partition_files(market, limit):
        trade_date = path.parent.name.split("=", 1)[-1]
        try:
            rows = pl.read_parquet(path).to_dicts()
        except Exception as exc:  # pragma: no cover - diagnostic
            summary["errors"].append({"partition": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows = [
            {
                **row,
                # Truthful provenance: v1 rows did not carry a source, so the
                # v2 shadow records exactly where the data actually came from.
                "source_reference": f"legacy_v1_lake:{path.relative_to(market_lake_root())}",
                "source_batch_id": batch_id,
            }
            for row in rows
            if row.get("date") and row.get("symbol")
        ]
        if not rows:
            continue
        try:
            write_lake_v2_partition(
                market=market, trade_date=trade_date, rows=rows, merge_existing=True,
                allow_legacy_defaults=True,  # explicit legacy backfill mode (A3)
            )
        except Exception as exc:
            summary["errors"].append({"partition": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        summary["partitions"] += 1
        summary["rows"] += len(rows)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["CN", "US", "ALL"], default="ALL")
    parser.add_argument("--limit-partitions", type=int, default=0, help="0 = all partitions")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    markets = ["CN", "US"] if args.market == "ALL" else [args.market]
    limit = args.limit_partitions or None
    reports = {market: backfill_market(market, limit=limit) for market in markets}
    payload = {
        "schema_version": "lake_v2_backfill_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "provenance_columns": list(LAKE_V2_PROVENANCE_COLUMNS),
        "reports": reports,
    }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({m: {"partitions": r["partitions"], "rows": r["rows"], "error_count": len(r["errors"])} for m, r in reports.items()}, indent=2))
    return 1 if any(report["errors"] for report in reports.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
