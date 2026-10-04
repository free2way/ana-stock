"""One-off repair: rewrite lake partitions whose `date` column is not pl.String.

Background: two historical CN partitions (2026-06-29 / 2026-06-30) were written
by a path that let polars infer `date` as pl.Date. Partitions are written
independently, so a single non-String partition breaks schema-unifying readers
(polars glob scans fail to unify Date + String), even though the app's DuckDB
queries tolerate both via CAST(date AS DATE).

The writer (`app/services/market_lake.py`) now pins `LAKE_OHLCV_SCHEMA`, so new
partitions cannot regress. This script converts existing offenders in place
(atomic tmp + replace, zstd, same as the writer) and verifies the result.

Scope note: only the `date` column dtype is repaired here. Some older US
partitions store all-null `dividend`/`split_ratio` as Null dtype, which is
harmless (Null unifies with Float64 for relaxed readers and DuckDB reads it as
NULL); rewriting hundreds of recent partitions would needlessly race with live
app writes.

Usage: .venv/bin/python scripts/repair_lake_date_dtype.py [--dry-run]

Every rewritten v1 partition is mirrored into the provenance shadow
(``data/lake/_lake_v2``) as ``provider=manual_repair`` / ``price_basis=raw`` so
the manual repair cannot silently drift from the fail-closed v2 store.

Operational note: after this repair completes, run
``python scripts/verify_lake_manifest.py --check``; if it reports drift,
rebaseline with ``python scripts/verify_lake_manifest.py --write``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import polars as pl

from app.services.market_lake import market_lake_root, write_lake_v2_partition


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report without rewriting")
    args = parser.parse_args()

    root = market_lake_root()
    repaired: list[str] = []
    checked = 0
    for market_dir in sorted(root.glob("*_daily")):
        for part_dir in sorted(p for p in market_dir.iterdir() if p.is_dir()):
            path = part_dir / "part.parquet"
            if not path.exists():
                continue
            checked += 1
            schema = pl.read_parquet_schema(path)
            if str(schema.get("date")) == "String":
                continue
            frame = pl.read_parquet(path)
            fixed = frame.with_columns(pl.col("date").cast(pl.String))
            expected_date = part_dir.name.split("=", 1)[1]
            unique_dates = fixed["date"].unique().to_list()
            ok = len(unique_dates) == 1 and str(unique_dates[0])[:10] == expected_date
            print(
                f"{part_dir}: date={schema.get('date')} -> cast; rows={fixed.height}; "
                f"date_values_match_dir={ok}"
            )
            if args.dry_run or not ok:
                continue
            tmp = path.with_name(f".{path.name}.repair-tmp")
            fixed.write_parquet(tmp, compression="zstd")
            tmp.replace(path)
            market = market_dir.name.removesuffix("_daily").upper()
            trade_date = part_dir.name.split("=", 1)[-1]
            # Rebuild the v2 provenance shadow for the rewritten partition so the
            # two stores stay in lockstep. These repairs restore raw-basis v1
            # rows, so the manual-repair provenance is recorded as raw.
            write_lake_v2_partition(
                market=market,
                trade_date=trade_date,
                rows=[
                    {
                        **row,
                        "provider": "manual_repair",
                        "source_reference": f"repair_lake_date_dtype.py:{market}:{trade_date}",
                        "price_basis": "raw",
                    }
                    for row in fixed.to_dicts()
                ],
            )
            repaired.append(str(part_dir))

    print(f"\nchecked={checked} repaired={len(repaired)} dry_run={args.dry_run}")
    for name in repaired:
        print(f"  rewrote {name}")

    # Final invariant scan: every partition must store `date` as String.
    bad = []
    for market_dir in sorted(root.glob("*_daily")):
        for path in sorted(market_dir.glob("date=*/part.parquet")):
            if str(pl.read_parquet_schema(path)["date"]) != "String":
                bad.append(str(path))
    if bad:
        print(f"FAIL: still non-String after repair: {bad}")
        return 1
    print("invariant OK: all lake partitions store date as pl.String")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
