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
    "predictions",
    "prediction_details",
    "prediction_explanations",
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded non-FULL VACUUM ANALYZE on prediction tables.")
    parser.add_argument("--tables", default=",".join(ALLOWED_TABLES))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    requested = [item.strip() for item in args.tables.split(",") if item.strip()]
    if not requested or any(item not in ALLOWED_TABLES for item in requested):
        raise ValueError(f"tables must be a subset of {ALLOWED_TABLES}")
    started = time.perf_counter()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        # The application engine uses a deliberately short statement timeout for
        # request traffic.  Maintenance is bounded separately so a large-table
        # scan can finish without inheriting that request-level limit.
        connection.execute(text("SET statement_timeout = '15min'"))
        connection.execute(text("SET lock_timeout = '5s'"))
        before = _stats(connection, requested)
        if args.apply:
            for table_name in requested:
                connection.execute(text(f'VACUUM (ANALYZE) "{table_name}"'))
        after = _stats(connection, requested)
    result = {
        "vacuum_version": "prediction-table-vacuum-v1",
        "generated_at": app_now_iso(),
        "status": "pass",
        "mode": "apply" if args.apply else "dry_run",
        "vacuum_full": False,
        "tables": requested,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "before": before,
        "after": after,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    target = args.receipt.resolve()
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(serialized)


if __name__ == "__main__":
    main()
