from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection


AUTOVACUUM_TABLES = (
    "workspace_snapshots",
    "data_jobs",
    "job_run_attempts",
    "live_predictions",
    "predictions",
    "prediction_details",
    "prediction_explanations",
    "cn_live_predictions",
    "us_live_predictions",
    "hk_live_predictions",
    "cn_predictions",
    "us_predictions",
    "hk_predictions",
    "cn_model_chart_signals",
    "us_model_chart_signals",
    "hk_model_chart_signals",
    "cn_prediction_trade_plans",
    "us_prediction_trade_plans",
    "hk_prediction_trade_plans",
    "fundamental_snapshots",
    "cn_fundamental_snapshots",
    "us_fundamental_snapshots",
    "hk_fundamental_snapshots",
    "technical_snapshots",
    "cn_technical_snapshots",
    "us_technical_snapshots",
    "hk_technical_snapshots",
)
MAIN_REL_OPTIONS = {
    "autovacuum_vacuum_scale_factor": "0.02",
    "autovacuum_analyze_scale_factor": "0.01",
    "autovacuum_vacuum_threshold": "50",
    "autovacuum_analyze_threshold": "50",
}
TOAST_REL_OPTIONS = {
    "autovacuum_vacuum_scale_factor": "0.01",
    "autovacuum_vacuum_threshold": "50",
}
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _parse_options(values: Iterable[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values or ():
        key, separator, value = str(item).partition("=")
        if separator:
            result[key] = value
    return result


def inspect_storage_maintenance(connection: Connection) -> dict[str, dict]:
    rows = connection.execute(
        text(
            """
            SELECT
                main.relname AS table_name,
                main.reloptions AS main_reloptions,
                toast.reloptions AS toast_reloptions
            FROM pg_class AS main
            JOIN pg_namespace AS namespace ON namespace.oid = main.relnamespace
            LEFT JOIN pg_class AS toast ON toast.oid = main.reltoastrelid
            WHERE namespace.nspname = current_schema()
              AND main.relkind IN ('r', 'p')
              AND main.relname = ANY(CAST(:table_names AS TEXT[]))
            ORDER BY main.relname
            """
        ),
        {"table_names": list(AUTOVACUUM_TABLES)},
    ).mappings()
    report: dict[str, dict] = {}
    for row in rows:
        main_options = _parse_options(row["main_reloptions"])
        toast_options = _parse_options(row["toast_reloptions"])
        main_compliant = all(
            main_options.get(key) == value for key, value in MAIN_REL_OPTIONS.items()
        )
        toast_compliant = all(
            toast_options.get(key) == value for key, value in TOAST_REL_OPTIONS.items()
        )
        report[str(row["table_name"])] = {
            "main_reloptions": main_options,
            "toast_reloptions": toast_options,
            "main_compliant": main_compliant,
            "toast_compliant": toast_compliant,
            "compliant": main_compliant and toast_compliant,
        }
    return report


def apply_storage_maintenance(connection: Connection) -> dict:
    before = inspect_storage_maintenance(connection)
    missing_tables = sorted(set(AUTOVACUUM_TABLES) - set(before))
    changed_tables: list[str] = []
    connection.execute(text("SET LOCAL lock_timeout = '10s'"))
    option_sql = ", ".join(
        [f"{key} = {value}" for key, value in MAIN_REL_OPTIONS.items()]
        + [f"toast.{key} = {value}" for key, value in TOAST_REL_OPTIONS.items()]
    )
    for table_name in AUTOVACUUM_TABLES:
        if table_name not in before or before[table_name]["compliant"]:
            continue
        if not _SAFE_IDENTIFIER.fullmatch(table_name):
            raise ValueError(f"Unsafe maintenance table identifier: {table_name!r}")
        connection.execute(text(f'ALTER TABLE "{table_name}" SET ({option_sql})'))
        changed_tables.append(table_name)
    after = inspect_storage_maintenance(connection)
    return {
        "status": (
            "pass"
            if not missing_tables and all(item["compliant"] for item in after.values())
            else "failed"
        ),
        "configured_table_count": len(after),
        "changed_tables": changed_tables,
        "missing_tables": missing_tables,
        "before": before,
        "after": after,
    }
