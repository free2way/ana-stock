"""C-3 audit: non-positive prices in every lake scope, with a queryable record.

Scans raw/canonical stores and the adjusted views. Any offending symbol is
listed with the affected row count and columns so the universe rule
(`nonpositive_price_history`) and the C-3 evidence can be traced back.
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

LAKE = ROOT / "data" / "lake"

SCOPES: dict[str, str] = {
    "cn_canonical": str(LAKE / "cn_daily" / "date=*" / "*.parquet"),
    "us_alpaca_raw": str(LAKE / "_us_alpaca" / "raw" / "*.parquet"),
    "us_legacy_v1": str(LAKE / "us_daily" / "date=*" / "*.parquet"),
    "cn_adjusted_qfq": str(LAKE / "_adjusted_v2" / "cn" / "method=qfq" / "adjusted.parquet"),
    "us_adjusted_qfq": str(LAKE / "_adjusted_v2" / "us" / "method=qfq" / "adjusted.parquet"),
}

PRICE_COLUMNS = ("open", "high", "low", "close", "adj_close")


def _columns(pattern: str) -> list[str]:
    rows = duckdb.sql(
        "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning=true))",
        params=[pattern],
    ).fetchall()
    return [str(row[0]) for row in rows]


def audit_scope(name: str, pattern: str) -> dict:
    available = [column for column in PRICE_COLUMNS if column in _columns(pattern)]
    checks = " OR ".join(f"{column} <= 0" for column in available)
    per_column = ", ".join(
        f"sum(CASE WHEN {column} <= 0 THEN 1 ELSE 0 END) AS {column}_nonpositive" for column in available
    )
    total = duckdb.sql("SELECT count(*) FROM read_parquet(?, hive_partitioning=true)", params=[pattern]).fetchone()[0]
    offenders = duckdb.sql(
        f"""
        SELECT symbol, count(*) AS rows, {per_column}
        FROM read_parquet(?, hive_partitioning=true)
        WHERE {checks}
        GROUP BY symbol ORDER BY rows DESC LIMIT 50
        """,
        params=[pattern],
    ).fetchall()
    columns = ["symbol", "rows"] + [f"{column}_nonpositive" for column in available]
    return {
        "name": name,
        "pattern": pattern,
        "rows": int(total),
        "price_columns": available,
        "offending_symbols": [dict(zip(columns, row)) for row in offenders],
        "offending_rows": sum(int(row[1]) for row in offenders),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = {
        "schema_version": "negative_price_audit_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "universe_rule": "nonpositive_price_history (any non-positive price column excludes the ticker)",
        "scopes": [audit_scope(name, pattern) for name, pattern in SCOPES.items()],
    }
    canonical_clean = all(
        scope["offending_rows"] == 0
        for scope in payload["scopes"]
        if scope["name"] in {"cn_canonical", "us_alpaca_raw", "cn_adjusted_qfq", "us_adjusted_qfq"}
    )
    payload["canonical_clean"] = canonical_clean
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({
        "canonical_clean": canonical_clean,
        "scopes": {
            scope["name"]: {"rows": scope["rows"], "offending_rows": scope["offending_rows"]}
            for scope in payload["scopes"]
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
