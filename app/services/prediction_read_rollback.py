from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import PredictionArtifact
from app.services.repository import PredictionRepository
from app.services.time_utils import app_now_iso


LATEST_COLUMNS = ("model_run_id", "trade_date", "ticker", "score", "rank_value")


def _rows_digest(rows: list[dict], columns: tuple[str, ...] = LATEST_COLUMNS) -> str:
    serialized = [
        json.dumps(
            {column: row.get(column) for column in columns},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        for row in rows
    ]
    return hashlib.sha256("\n".join(serialized).encode("utf-8")).hexdigest()


def run_prediction_read_rollback_drill(db: Session, *, market: str = "CN", limit: int = 50) -> dict:
    """Simulate the cold-read kill switch without changing process configuration."""

    normalized_market = str(market).strip().upper()
    if normalized_market not in {"CN", "US"}:
        raise ValueError("prediction rollback drill market must be CN or US")

    started = time.perf_counter()
    normal_rows = PredictionRepository(db, cold_reads_enabled=True).list_latest_predictions_for_market(
        normalized_market,
        limit=max(1, int(limit)),
    )
    rollback_rows = PredictionRepository(db, cold_reads_enabled=False).list_latest_predictions_for_market(
        normalized_market,
        limit=max(1, int(limit)),
    )
    elapsed_seconds = time.perf_counter() - started
    normal_digest = _rows_digest(normal_rows)
    rollback_digest = _rows_digest(rollback_rows)
    cold_sources = [row for row in rollback_rows if row.get("source_layer") == "cold_parquet"]
    exact_match = (
        len(normal_rows) == len(rollback_rows)
        and normal_digest == rollback_digest
        and not cold_sources
        and bool(rollback_rows)
    )
    artifact_count = len(
        db.scalars(
            select(PredictionArtifact.id).where(
                PredictionArtifact.market == normalized_market,
                PredictionArtifact.status == "verified",
            )
        ).all()
    )
    return {
        "drill_version": "prediction-read-rollback-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if exact_match and elapsed_seconds <= 30 * 60 else "fail",
        "mode": "simulated_pg_only_no_configuration_write",
        "market": normalized_market,
        "elapsed_seconds": round(elapsed_seconds, 6),
        "hard_limit_seconds": 30 * 60,
        "verified_cold_artifact_count": artifact_count,
        "latest_candidates": {
            "normal_rows": len(normal_rows),
            "rollback_rows": len(rollback_rows),
            "normal_sha256": normal_digest,
            "rollback_sha256": rollback_digest,
            "exact_match": exact_match,
            "cold_rows_observed_in_rollback_mode": len(cold_sources),
            "model_run_ids": sorted({int(row["model_run_id"]) for row in rollback_rows}),
        },
        "activation": {
            "environment": "PQW_PREDICTION_COLD_READS_ENABLED=false",
            "restart_required": True,
            "health_mode": "postgresql_only_rollback",
        },
        "revert": {
            "environment": "PQW_PREDICTION_COLD_READS_ENABLED=true",
            "restart_required": True,
        },
        "expected_degradation": (
            "Archived-only history is intentionally unavailable in rollback mode; "
            "latest PostgreSQL candidates remain available and readiness reports degraded."
        ),
        "database_mutated": False,
    }
