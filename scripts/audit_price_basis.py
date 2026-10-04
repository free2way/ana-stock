"""Read-only price-basis audit for the canonical market lake (P0B/P0C evidence).

Reports, per market: row/symbol counts, how many rows carry a distinct
``adj_close``, negative/missing prices, and adjacent-close jumps beyond the
board bands. With the v1 lake schema (no provenance columns) the provenance
section reports the fields as absent so the gap itself becomes auditable.

Never writes to the lake. Output goes to the requested JSON path.
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

import duckdb  # noqa: E402

DEFAULT_JUMP_THRESHOLD = {"CN": 0.30, "US": 0.50}


def _lake_root() -> Path:
    from app.core.config import get_settings

    return get_settings().data_dir / "lake"


def audit_market(market: str, *, lake_root: Path, jump_threshold: float) -> dict:
    market_dir = lake_root / f"{market.lower()}_daily"
    pattern = str(market_dir / "date=*" / "*.parquet")
    files = sorted(market_dir.glob("date=*/*.parquet"))
    report: dict = {
        "schema_version": "price_basis_audit_v1",
        "market": market,
        "lake_root": str(lake_root),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "partition_count": len(files),
        "jump_threshold": jump_threshold,
    }
    if not files:
        report["status"] = "empty"
        return report

    row = duckdb.sql(
        """
        SELECT count(*) AS rows,
               count(DISTINCT symbol) AS symbols,
               min(CAST(date AS DATE)) AS first_date,
               max(CAST(date AS DATE)) AS last_date,
               sum(CASE WHEN adj_close IS NULL THEN 1 ELSE 0 END) AS adj_close_missing,
               sum(CASE WHEN adj_close IS NOT NULL AND abs(adj_close - close) > 1e-9 THEN 1 ELSE 0 END) AS adj_close_differs,
               sum(CASE WHEN adj_close < 0 THEN 1 ELSE 0 END) AS adj_close_negative,
               sum(CASE WHEN close IS NULL OR close <= 0 THEN 1 ELSE 0 END) AS non_positive_close
        FROM read_parquet(?, hive_partitioning=true)
        """,
        params=[pattern],
    ).fetchone()
    columns = [item[0] for item in duckdb.sql("DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning=true) LIMIT 1", params=[pattern]).fetchall()]
    provenance_fields = [
        "provider", "provider_symbol", "price_basis", "volume_unit", "currency",
        "source_reference", "ingested_at", "revision_id", "source_batch_id",
    ]
    jump = duckdb.sql(
        """
        WITH base AS (
          SELECT symbol, CAST(date AS DATE) AS d, close
          FROM read_parquet(?, hive_partitioning=true)
          WHERE close IS NOT NULL AND close > 0
        ), lagged AS (
          SELECT symbol, d, close / NULLIF(LAG(close) OVER (PARTITION BY symbol ORDER BY d), 0) - 1 AS r
          FROM base
        )
        SELECT count(*), count(DISTINCT symbol) FROM lagged WHERE abs(r) > ?
        """,
        params=[pattern, jump_threshold],
    ).fetchone()
    report.update(
        {
            "status": "ok",
            "rows": int(row[0]),
            "symbols": int(row[1]),
            "first_date": str(row[2]),
            "last_date": str(row[3]),
            "adj_close_missing": int(row[4]),
            "adj_close_differs": int(row[5]),
            "adj_close_negative": int(row[6]),
            "non_positive_close": int(row[7]),
            "jumps_beyond_threshold": int(jump[0]),
            "jump_symbols": int(jump[1]),
            "provenance": {
                "present_fields": [name for name in provenance_fields if name in columns],
                "absent_fields": [name for name in provenance_fields if name not in columns],
                "columns": columns,
            },
            "shadow_v2": audit_shadow_v2(market, lake_root=lake_root),
        }
    )
    return report


def audit_shadow_v2(market: str, *, lake_root: Path) -> dict:
    v2_dir = lake_root / "_lake_v2" / f"{market.lower()}_daily"
    files = sorted(v2_dir.glob("date=*/*.parquet"))
    if not files:
        return {"status": "missing", "path": str(v2_dir)}
    pattern = str(v2_dir / "date=*" / "*.parquet")
    provenance_columns = (
        "market", "provider", "provider_symbol", "price_basis", "volume_unit",
        "currency", "source_reference", "source_batch_id", "ingested_at", "revision_id",
    )
    null_sums = ", ".join(
        f"sum(CASE WHEN {name} IS NULL OR {name} = '' THEN 1 ELSE 0 END) AS {name}_null"
        for name in provenance_columns
    )
    row = duckdb.sql(
        f"SELECT count(*) AS rows, count(DISTINCT symbol) AS symbols, {null_sums} "
        "FROM read_parquet(?, hive_partitioning=true)",
        params=[pattern],
    ).fetchone()
    names = ["rows", "symbols", *[f"{name}_null" for name in provenance_columns]]
    payload = dict(zip(names, row))
    coverage = {
        name: 1.0 - (int(payload.pop(f"{name}_null")) / max(1, int(payload["rows"])))
        for name in provenance_columns
    }
    return {
        "status": "ok",
        "path": str(v2_dir),
        "partition_count": len(files),
        "rows": int(payload["rows"]),
        "symbols": int(payload["symbols"]),
        "provenance_coverage": coverage,
        "minimum_coverage": min(coverage.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["CN", "US", "ALL"], default="ALL")
    parser.add_argument("--lake-root", default=None)
    parser.add_argument("--jump-threshold", type=float, default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    lake_root = Path(args.lake_root) if args.lake_root else _lake_root()
    markets = ["CN", "US"] if args.market == "ALL" else [args.market]
    reports = {
        market: audit_market(
            market,
            lake_root=lake_root,
            jump_threshold=args.jump_threshold
            if args.jump_threshold is not None
            else DEFAULT_JUMP_THRESHOLD[market],
        )
        for market in markets
    }
    payload = {"schema_version": "price_basis_audit_v1", "reports": reports}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({market: {k: report.get(k) for k in ("rows", "symbols", "adj_close_differs", "adj_close_negative", "jumps_beyond_threshold")} for market, report in reports.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
