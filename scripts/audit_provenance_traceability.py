"""B-1 traceability audit (audit finding #3, redefined criterion).

Reports, per market, how many shadow rows carry *real* provenance versus
placeholder values (`provider` in {unknown, ""} or `source_reference` in
{unspecified, ""}). Legacy backfilled rows are counted separately so the
acceptance record can cite an honest traceable-coverage number.
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

from app.services.market_lake import market_lake_root  # noqa: E402


def audit_market(market: str) -> dict:
    pattern = str(market_lake_root() / "_lake_v2" / f"{market.lower()}_daily" / "date=*" / "part.parquet")
    row = duckdb.sql(
        """
        SELECT count(*) AS rows,
               sum(CASE WHEN lower(coalesce(provider, '')) NOT IN ('', 'unknown', 'auto')
                         AND lower(coalesce(source_reference, '')) NOT IN ('', 'unspecified')
                         AND NOT starts_with(coalesce(source_reference, ''), 'legacy_v1_lake:')
                        THEN 1 ELSE 0 END) AS traceable,
               sum(CASE WHEN lower(coalesce(provider, '')) IN ('', 'unknown') THEN 1 ELSE 0 END) AS placeholder_provider,
               sum(CASE WHEN lower(coalesce(source_reference, '')) IN ('', 'unspecified') THEN 1 ELSE 0 END) AS placeholder_reference,
               sum(CASE WHEN starts_with(coalesce(source_reference, ''), 'legacy_v1_lake:') THEN 1 ELSE 0 END) AS legacy_backfill
        FROM read_parquet(?, hive_partitioning=true)
        """,
        params=[pattern],
    ).fetchone()
    by_provider = duckdb.sql(
        """
        SELECT coalesce(nullif(provider, ''), '<empty>') AS provider, count(*) AS rows
        FROM read_parquet(?, hive_partitioning=true)
        GROUP BY 1 ORDER BY rows DESC LIMIT 12
        """,
        params=[pattern],
    ).fetchall()
    total, traceable, placeholder_provider, placeholder_reference, legacy = row
    return {
        "market": market.upper(),
        "rows": int(total or 0),
        "traceable_rows": int(traceable or 0),
        "traceable_share": (float(traceable) / float(total)) if total else None,
        "placeholder_provider_rows": int(placeholder_provider or 0),
        "placeholder_reference_rows": int(placeholder_reference or 0),
        "legacy_backfill_rows": int(legacy or 0),
        "by_provider": [{"provider": name, "rows": int(count)} for name, count in by_provider],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    payload = {
        "schema_version": "provenance_traceability_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "criterion": (
            "traceable = provider not in {unknown, '', 'auto'} AND source_reference not in "
            "{unspecified, ''} AND source_reference does not start with 'legacy_v1_lake:'; "
            "legacy_v1_lake backfilled rows are reported separately and never counted as traceable"
        ),
        "markets": [audit_market(market) for market in ("CN", "US")],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({
        item["market"]: {
            "rows": item["rows"],
            "traceable_share": round(item["traceable_share"] or 0.0, 4),
            "legacy_backfill_rows": item["legacy_backfill_rows"],
            "top_providers": item["by_provider"][:4],
        }
        for item in payload["markets"]
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
