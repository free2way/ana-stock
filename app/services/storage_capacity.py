from __future__ import annotations

import json
from datetime import date
from typing import Iterable, Sequence

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.tables import StorageCapacitySample
from app.services.time_utils import app_now


PHYSICAL_ONLINE_TABLES = (
    "cn_live_predictions",
    "us_live_predictions",
    "cn_predictions",
    "us_predictions",
    "cn_prediction_details",
    "us_prediction_details",
    "cn_prediction_explanations",
    "us_prediction_explanations",
    "cn_model_chart_signals",
    "us_model_chart_signals",
    "cn_prediction_trade_plans",
    "us_prediction_trade_plans",
    "cn_fundamental_snapshots",
    "us_fundamental_snapshots",
    "cn_point_in_time_features",
    "us_point_in_time_features",
    "cn_technical_snapshots",
    "us_technical_snapshots",
)
LEGACY_PREDICTION_TABLES = (
    "predictions",
    "prediction_details",
    "prediction_explanations",
    "live_predictions",
)
WORKSPACE_TABLES = ("workspace_snapshots",)
JOB_TABLES = (
    "data_jobs",
    "job_run_attempts",
    "job_run_dependencies",
)
DEFAULT_GROWTH_LIMIT_BYTES = 20 * 1024 * 1024


def _iso_or_none(value) -> str | None:
    return value.isoformat() if value is not None else None


def _sum_relation_bytes(metrics: Sequence[dict], table_names: Iterable[str]) -> int:
    wanted = set(table_names)
    return sum(
        int(row.get("total_bytes") or 0)
        for row in metrics
        if str(row.get("table_name") or "") in wanted
    )


def collect_table_metrics(db: Session) -> list[dict]:
    rows = db.execute(
        text(
            """
            SELECT
                relname AS table_name,
                n_live_tup,
                n_dead_tup,
                pg_total_relation_size(relid) AS total_bytes,
                pg_relation_size(relid) AS heap_bytes,
                pg_indexes_size(relid) AS index_bytes,
                last_vacuum,
                last_autovacuum,
                last_analyze,
                last_autoanalyze
            FROM pg_stat_user_tables
            ORDER BY pg_total_relation_size(relid) DESC, relname ASC
            """
        )
    ).mappings()
    return [
        {
            "table_name": str(row["table_name"]),
            "n_live_tup": int(row["n_live_tup"] or 0),
            "n_dead_tup": int(row["n_dead_tup"] or 0),
            "total_bytes": int(row["total_bytes"] or 0),
            "heap_bytes": int(row["heap_bytes"] or 0),
            "index_bytes": int(row["index_bytes"] or 0),
            "last_vacuum": _iso_or_none(row["last_vacuum"]),
            "last_autovacuum": _iso_or_none(row["last_autovacuum"]),
            "last_analyze": _iso_or_none(row["last_analyze"]),
            "last_autoanalyze": _iso_or_none(row["last_autoanalyze"]),
        }
        for row in rows
    ]


def capture_storage_capacity(
    db: Session,
    *,
    sample_date: date | None = None,
) -> dict:
    captured_at = app_now()
    effective_date = sample_date or captured_at.date()
    database_bytes = int(
        db.scalar(text("SELECT pg_database_size(current_database())")) or 0
    )
    table_metrics = collect_table_metrics(db)
    payload = {
        "sample_date": effective_date,
        "database_bytes": database_bytes,
        "physical_hot_bytes": _sum_relation_bytes(
            table_metrics, PHYSICAL_ONLINE_TABLES
        ),
        "legacy_prediction_bytes": _sum_relation_bytes(
            table_metrics, LEGACY_PREDICTION_TABLES
        ),
        "workspace_bytes": _sum_relation_bytes(table_metrics, WORKSPACE_TABLES),
        "job_bytes": _sum_relation_bytes(table_metrics, JOB_TABLES),
        "table_metrics_json": json.dumps(
            table_metrics,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "captured_at": captured_at,
    }
    stmt = pg_insert(StorageCapacitySample).values(payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=[StorageCapacitySample.sample_date],
        set_={
            "database_bytes": stmt.excluded.database_bytes,
            "physical_hot_bytes": stmt.excluded.physical_hot_bytes,
            "legacy_prediction_bytes": stmt.excluded.legacy_prediction_bytes,
            "workspace_bytes": stmt.excluded.workspace_bytes,
            "job_bytes": stmt.excluded.job_bytes,
            "table_metrics_json": stmt.excluded.table_metrics_json,
            "captured_at": stmt.excluded.captured_at,
        },
    ).returning(StorageCapacitySample.id)
    sample_id = int(db.scalar(stmt))
    db.commit()
    return {
        "sample_id": sample_id,
        "sample_date": effective_date.isoformat(),
        "database_bytes": database_bytes,
        "physical_hot_bytes": int(payload["physical_hot_bytes"]),
        "legacy_prediction_bytes": int(payload["legacy_prediction_bytes"]),
        "workspace_bytes": int(payload["workspace_bytes"]),
        "job_bytes": int(payload["job_bytes"]),
        "table_count": len(table_metrics),
        "captured_at": captured_at.isoformat(),
    }


def evaluate_capacity_growth(
    samples: Sequence[dict],
    *,
    required_intervals: int = 5,
    limit_bytes: int = DEFAULT_GROWTH_LIMIT_BYTES,
    not_before_date: date | str | None = None,
    maximum_database_bytes: int | None = None,
) -> dict:
    minimum_date = (
        not_before_date
        if isinstance(not_before_date, date)
        else date.fromisoformat(str(not_before_date)[:10])
        if not_before_date
        else None
    )
    rejected_before_window = 0
    rejected_above_maximum = 0
    eligible: list[dict] = []
    for item in samples:
        sample_date = (
            item["sample_date"]
            if isinstance(item["sample_date"], date)
            else date.fromisoformat(str(item["sample_date"])[:10])
        )
        database_bytes = int(item["database_bytes"])
        if minimum_date is not None and sample_date < minimum_date:
            rejected_before_window += 1
            continue
        if (
            maximum_database_bytes is not None
            and database_bytes > int(maximum_database_bytes)
        ):
            rejected_above_maximum += 1
            continue
        eligible.append(
            {"sample_date": sample_date, "database_bytes": database_bytes}
        )
    normalized = sorted(
        eligible,
        key=lambda item: item["sample_date"],
    )
    # A repeated same-day capture is an update, not another acceptance day.
    deduped = {item["sample_date"]: item for item in normalized}
    window = sorted(deduped.values(), key=lambda item: item["sample_date"])[
        -(required_intervals + 1) :
    ]
    gaps = [
        (right["sample_date"] - left["sample_date"]).days
        for left, right in zip(window, window[1:])
    ]
    deltas = [
        right["database_bytes"] - left["database_bytes"]
        for left, right in zip(window, window[1:])
    ]
    consecutive = len(gaps) == required_intervals and all(gap == 1 for gap in gaps)
    average_growth = (
        sum(deltas) / required_intervals if len(deltas) == required_intervals else None
    )
    if not consecutive:
        status = "collecting"
    elif average_growth is not None and average_growth <= int(limit_bytes):
        status = "pass"
    else:
        status = "failed"
    return {
        "status": status,
        "required_intervals": required_intervals,
        "sample_count": len(window),
        "consecutive": consecutive,
        "sample_dates": [item["sample_date"].isoformat() for item in window],
        "daily_growth_bytes": deltas,
        "average_daily_growth_bytes": average_growth,
        "limit_bytes": int(limit_bytes),
        "not_before_date": minimum_date.isoformat() if minimum_date else None,
        "maximum_database_bytes": (
            int(maximum_database_bytes)
            if maximum_database_bytes is not None
            else None
        ),
        "excluded_before_window": rejected_before_window,
        "excluded_above_maximum": rejected_above_maximum,
    }


def storage_capacity_report(
    db: Session,
    *,
    not_before_date: date | str | None = None,
    maximum_database_bytes: int | None = None,
    acceptance_scope: str = "operational",
) -> dict:
    minimum_date = (
        not_before_date
        if isinstance(not_before_date, date)
        else date.fromisoformat(str(not_before_date)[:10])
        if not_before_date
        else None
    )
    stmt = select(
        StorageCapacitySample.sample_date,
        StorageCapacitySample.database_bytes,
    )
    rows = list(
        db.execute(
            stmt.order_by(StorageCapacitySample.sample_date.desc())
        ).mappings()
    )
    result = evaluate_capacity_growth(
        rows,
        not_before_date=minimum_date,
        maximum_database_bytes=maximum_database_bytes,
    )
    result["acceptance_scope"] = str(acceptance_scope or "operational")
    latest = db.scalar(
        select(StorageCapacitySample)
        .order_by(StorageCapacitySample.sample_date.desc())
        .limit(1)
    )
    result["latest_sample"] = (
        {
            "sample_id": int(latest.id),
            "sample_date": latest.sample_date.isoformat(),
            "database_bytes": int(latest.database_bytes),
            "physical_hot_bytes": int(latest.physical_hot_bytes),
            "legacy_prediction_bytes": int(latest.legacy_prediction_bytes),
            "workspace_bytes": int(latest.workspace_bytes),
            "job_bytes": int(latest.job_bytes),
            "captured_at": latest.captured_at.isoformat(),
        }
        if latest is not None
        else None
    )
    return result
