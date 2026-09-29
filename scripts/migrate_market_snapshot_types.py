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


TARGET_COLUMNS = {
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


def _column_types(db) -> dict[str, dict[str, str]]:
    return {
        table_name: {
            str(row["column_name"]): str(row["data_type"])
            for row in db.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :table_name"
                ),
                {"table_name": table_name},
            ).mappings()
            if str(row["column_name"]) in columns
        }
        for table_name, columns in TARGET_COLUMNS.items()
    }


def _invalid_values(db, current_types: dict[str, dict[str, str]]) -> dict[str, dict[str, int]]:
    invalid: dict[str, dict[str, int]] = {}
    for table_name, columns in TARGET_COLUMNS.items():
        invalid[table_name] = {}
        for column_name, desired_type in columns.items():
            if current_types[table_name].get(column_name) == desired_type:
                invalid[table_name][column_name] = 0
                continue
            invalid[table_name][column_name] = int(
                db.scalar(
                    text(
                        f"SELECT count(*) FROM {table_name} "
                        f"WHERE {column_name} IS NOT NULL "
                        f"AND NOT pg_input_is_valid({column_name}::text, :target_type)"
                    ),
                    {"target_type": desired_type},
                )
                or 0
            )
    return invalid


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate physical market snapshot dates to native PostgreSQL types."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    with SessionLocal() as db:
        before = _column_types(db)
        invalid = _invalid_values(db, before)
        invalid_count = sum(sum(values.values()) for values in invalid.values())
        changes = [
            {
                "table": table_name,
                "column": column_name,
                "from": before[table_name].get(column_name),
                "to": desired_type,
            }
            for table_name, columns in TARGET_COLUMNS.items()
            for column_name, desired_type in columns.items()
            if before[table_name].get(column_name) != desired_type
        ]
        if args.apply and invalid_count:
            raise RuntimeError(
                f"Refusing native-type migration: {invalid_count} values cannot be cast."
            )
        if args.apply and changes:
            try:
                db.execute(text("SET LOCAL lock_timeout = '10s'"))
                db.execute(text("SET LOCAL statement_timeout = '5min'"))
                for change in changes:
                    sql_type = (
                        "DATE"
                        if change["to"] == "date"
                        else "TIMESTAMP WITH TIME ZONE"
                    )
                    db.execute(
                        text(
                            f"ALTER TABLE {change['table']} "
                            f"ALTER COLUMN {change['column']} TYPE {sql_type} "
                            f"USING NULLIF({change['column']}::text, '')::{sql_type}"
                        )
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
        after = _column_types(db)
        exact_types = all(
            after[table_name].get(column_name) == desired_type
            for table_name, columns in TARGET_COLUMNS.items()
            for column_name, desired_type in columns.items()
        )
        result = {
            "migration_version": "market-snapshot-native-types-v1",
            "generated_at": app_now_iso(),
            "status": (
                "pass"
                if (args.apply and exact_types) or (not args.apply and invalid_count == 0)
                else "failed"
            ),
            "mode": "apply" if args.apply else "dry_run",
            "invalid_value_count": invalid_count,
            "invalid_values": invalid,
            "changes": changes,
            "before": before,
            "after": after,
            "exact_types": exact_types,
            "database_rows_deleted": 0,
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
