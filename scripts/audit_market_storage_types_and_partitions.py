from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402


EXPECTED_TYPES = {
    "cn_live_predictions": {
        "trade_date": "date",
        "published_at": "timestamp with time zone",
    },
    "us_live_predictions": {
        "trade_date": "date",
        "published_at": "timestamp with time zone",
    },
    "cn_predictions": {
        "trade_date": "date",
        "created_at": "timestamp with time zone",
    },
    "us_predictions": {
        "trade_date": "date",
        "created_at": "timestamp with time zone",
    },
    "cn_fundamental_snapshots": {
        "report_date": "date",
        "listing_date": "date",
        "created_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
    },
    "us_fundamental_snapshots": {
        "report_date": "date",
        "listing_date": "date",
        "created_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
    },
    "cn_point_in_time_features": {
        "event_time": "timestamp with time zone",
        "available_time": "timestamp with time zone",
        "ingested_time": "timestamp with time zone",
        "created_at": "timestamp with time zone",
    },
    "us_point_in_time_features": {
        "event_time": "timestamp with time zone",
        "available_time": "timestamp with time zone",
        "ingested_time": "timestamp with time zone",
        "created_at": "timestamp with time zone",
    },
    "cn_technical_snapshots": {
        "as_of_date": "date",
        "created_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
    },
    "us_technical_snapshots": {
        "as_of_date": "date",
        "created_at": "timestamp with time zone",
        "updated_at": "timestamp with time zone",
    },
}
ONLINE_TABLES = tuple(EXPECTED_TYPES)
PARTITION_THRESHOLD_ROWS = 10_000_000


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit native date types and evidence-based partition decisions."
    )
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    with SessionLocal() as db:
        observed_types = {
            table_name: {
                str(row["column_name"]): str(row["data_type"])
                for row in db.execute(
                    text(
                        "SELECT column_name, data_type "
                        "FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = :table_name"
                    ),
                    {"table_name": table_name},
                ).mappings()
                if str(row["column_name"]) in columns
            }
            for table_name, columns in EXPECTED_TYPES.items()
        }
        type_mismatches = {
            f"{table_name}.{column_name}": {
                "expected": expected_type,
                "actual": observed_types[table_name].get(column_name),
            }
            for table_name, columns in EXPECTED_TYPES.items()
            for column_name, expected_type in columns.items()
            if observed_types[table_name].get(column_name) != expected_type
        }
        row_counts = {
            table_name: int(
                db.scalar(text(f"SELECT count(*) FROM {table_name}")) or 0
            )
            for table_name in ONLINE_TABLES
        }
        partitioned = {
            str(row["relname"]): str(row["partition_strategy"] or "")
            for row in db.execute(
                text(
                    "SELECT c.relname, pt.partstrat AS partition_strategy "
                    "FROM pg_class c "
                    "LEFT JOIN pg_partitioned_table pt ON pt.partrelid = c.oid "
                    "WHERE c.relname = ANY(CAST(:tables AS text[]))"
                ),
                {"tables": list(ONLINE_TABLES)},
            ).mappings()
        }
        partition_decisions = {
            table_name: {
                "row_count": row_count,
                "threshold_rows": PARTITION_THRESHOLD_ROWS,
                "decision": (
                    "partition_required"
                    if row_count >= PARTITION_THRESHOLD_ROWS
                    else "not_required_below_threshold"
                ),
                "currently_partitioned": bool(partitioned.get(table_name)),
            }
            for table_name, row_count in row_counts.items()
        }
        required_unpartitioned = [
            table_name
            for table_name, decision in partition_decisions.items()
            if decision["decision"] == "partition_required"
            and not decision["currently_partitioned"]
        ]
        legacy_predictions = int(
            db.scalar(text("SELECT count(*) FROM predictions")) or 0
        )
        latest_date = db.scalar(
            text(
                "SELECT max(trade_date) FROM cn_predictions "
                "WHERE model_run_id = 287"
            )
        )
        explain_plan = "\n".join(
            str(row[0])
            for row in db.execute(
                text(
                    "EXPLAIN SELECT * FROM cn_predictions "
                    "WHERE model_run_id = 287 AND trade_date = :trade_date "
                    "ORDER BY score DESC LIMIT 100"
                ),
                {"trade_date": latest_date},
            )
        )
        checks = {
            "all_online_dates_native": not type_mismatches,
            "no_required_partition_missing": not required_unpartitioned,
            "cn_latest_query_is_market_local": "cn_predictions" in explain_plan
            and "us_predictions" not in explain_plan
            and " on predictions " not in explain_plan,
            "cn_latest_query_uses_index": "Index Scan" in explain_plan
            or "Bitmap Index Scan" in explain_plan,
        }
        result = {
            "audit_version": "market-storage-type-partition-governance-v1",
            "generated_at": app_now_iso(),
            "status": "pass" if all(checks.values()) else "failed",
            "mode": "read_only",
            "expected_types": EXPECTED_TYPES,
            "observed_types": observed_types,
            "type_mismatches": type_mismatches,
            "partition_threshold_rows": PARTITION_THRESHOLD_ROWS,
            "partition_decisions": partition_decisions,
            "required_unpartitioned": required_unpartitioned,
            "legacy_shared_predictions": {
                "row_count": legacy_predictions,
                "classification": "decommission_pending_cold_history",
                "online_partition_candidate": False,
            },
            "cn_latest_explain": explain_plan,
            "checks": checks,
        }

    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.receipt is not None:
        target = args.receipt.resolve()
        if target.exists():
            raise FileExistsError(f"Receipt already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(serialized + "\n", encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    print(serialized)
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
