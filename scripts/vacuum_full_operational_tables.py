from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

from sqlalchemy import text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import engine  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402


ALLOWED_TABLES = (
    "workspace_snapshots",
    "data_jobs",
    "job_run_attempts",
    "job_run_dependencies",
)
APPLY_TOKEN = "VACUUM_FULL_OPERATIONAL_TABLES"


def _database_size(connection) -> int:
    return int(
        connection.execute(text("SELECT pg_database_size(current_database())")).scalar_one()
    )


def _stats(connection, table_names: list[str]) -> dict[str, dict]:
    rows = connection.execute(
        text(
            "SELECT relname,n_live_tup,n_dead_tup,"
            "pg_total_relation_size(relid) AS total_bytes,last_vacuum,last_analyze "
            "FROM pg_stat_user_tables "
            "WHERE relname = ANY(CAST(:tables AS TEXT[])) ORDER BY relname"
        ),
        {"tables": table_names},
    ).mappings()
    return {str(row["relname"]): dict(row) for row in rows}


def _write_receipt(path: Path, payload: dict) -> None:
    target = path.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run explicitly authorized VACUUM FULL ANALYZE on operational tables."
    )
    parser.add_argument("--tables", default="workspace_snapshots")
    parser.add_argument("--apply-token", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    requested = [item.strip() for item in args.tables.split(",") if item.strip()]
    if args.apply_token != APPLY_TOKEN:
        raise ValueError(f"apply token must be {APPLY_TOKEN}")
    if not requested or any(item not in ALLOWED_TABLES for item in requested):
        raise ValueError(f"tables must be a subset of {ALLOWED_TABLES}")

    started = time.perf_counter()
    table_results: list[dict] = []
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("SET statement_timeout = '45min'"))
        connection.execute(text("SET lock_timeout = '10s'"))
        database_bytes_before = _database_size(connection)
        before = _stats(connection, requested)
        for table_name in requested:
            table_started = time.perf_counter()
            print(f"starting VACUUM FULL ANALYZE {table_name}", flush=True)
            connection.execute(text(f'VACUUM (FULL, ANALYZE) "{table_name}"'))
            current = _stats(connection, [table_name])[table_name]
            table_results.append(
                {
                    "table": table_name,
                    "elapsed_seconds": round(time.perf_counter() - table_started, 3),
                    "before": before[table_name],
                    "after": current,
                }
            )
            print(
                f"completed {table_name}: {before[table_name]['total_bytes']} -> "
                f"{current['total_bytes']} bytes",
                flush=True,
            )
        database_bytes_after = _database_size(connection)
        after = _stats(connection, requested)

    result = {
        "vacuum_version": "operational-table-vacuum-full-v1",
        "generated_at": app_now_iso(),
        "status": "pass",
        "mode": "apply",
        "vacuum_full": True,
        "tables": requested,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "database_bytes_before": database_bytes_before,
        "database_bytes_after": database_bytes_after,
        "database_bytes_reclaimed": database_bytes_before - database_bytes_after,
        "before": before,
        "after": after,
        "table_results": table_results,
    }
    _write_receipt(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
