from __future__ import annotations

import json
import platform
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.tables import ModelRun, Prediction, PredictionArtifact
from app.services.prediction_artifacts import read_prediction_artifact_rows
from app.services.repository import PRODUCTION_SIGNAL_MODEL_TYPES, PredictionRepository
from app.services.time_utils import app_now_iso


DEFAULT_THRESHOLDS_MS = {
    "hot_latest_cn_candidates": 500.0,
    "hot_model_run_latest_date": 500.0,
    "cold_latest_date_slice": 3000.0,
    "cold_single_symbol_slice": 1000.0,
    "cold_full_artifact_scan": 5000.0,
}


def percentile(values: list[float], percentile_value: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""

    if not values:
        raise ValueError("At least one timing sample is required.")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * min(100.0, max(0.0, float(percentile_value))) / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def measure_query(
    operation: Callable[[], Any],
    *,
    iterations: int,
    warmups: int,
    threshold_ms: float,
) -> dict:
    measured_iterations = max(1, int(iterations))
    warmup_iterations = max(0, int(warmups))
    last_result: Any = None
    for _ in range(warmup_iterations):
        last_result = operation()

    samples_ms: list[float] = []
    row_counts: list[int | None] = []
    for _ in range(measured_iterations):
        started = time.perf_counter()
        last_result = operation()
        samples_ms.append((time.perf_counter() - started) * 1000.0)
        try:
            row_counts.append(len(last_result))
        except TypeError:
            row_counts.append(None)

    p50_ms = percentile(samples_ms, 50)
    p95_ms = percentile(samples_ms, 95)
    return {
        "status": "pass" if p95_ms <= float(threshold_ms) else "fail",
        "threshold_ms": round(float(threshold_ms), 3),
        "warmups": warmup_iterations,
        "iterations": measured_iterations,
        "p50_ms": round(p50_ms, 3),
        "p95_ms": round(p95_ms, 3),
        "min_ms": round(min(samples_ms), 3),
        "max_ms": round(max(samples_ms), 3),
        "row_count_min": min((value for value in row_counts if value is not None), default=None),
        "row_count_max": max((value for value in row_counts if value is not None), default=None),
        "samples_ms": [round(value, 3) for value in samples_ms],
    }


def _latest_hot_run_id(db: Session, *, market: str) -> int:
    run_id = db.scalar(
        select(func.max(Prediction.model_run_id))
        .select_from(Prediction)
        .join(ModelRun, ModelRun.id == Prediction.model_run_id)
        .where(
            ModelRun.status == "success",
            ModelRun.market == market,
            ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
        )
    )
    if run_id is None:
        raise RuntimeError(f"No successful hot production prediction run found for {market}.")
    return int(run_id)


def _cold_artifact(db: Session, *, market: str, model_run_id: int | None) -> PredictionArtifact:
    stmt = select(PredictionArtifact).where(
        PredictionArtifact.market == market,
        PredictionArtifact.status == "verified",
    )
    if model_run_id is not None:
        stmt = stmt.where(PredictionArtifact.model_run_id == int(model_run_id))
    else:
        stmt = stmt.order_by(PredictionArtifact.prediction_count.desc(), PredictionArtifact.model_run_id.asc())
    artifact = db.scalar(stmt.limit(1))
    if artifact is None:
        suffix = f" model run {model_run_id}" if model_run_id is not None else ""
        raise RuntimeError(f"No verified {market}{suffix} prediction artifact found.")
    return artifact


def run_storage_architecture_benchmark(
    db: Session,
    *,
    market: str = "CN",
    cold_model_run_id: int | None = None,
    iterations: int = 20,
    cold_full_iterations: int = 10,
    warmups: int = 2,
) -> dict:
    """Run prewarmed, read-only hot/cold storage benchmarks."""

    normalized_market = str(market).strip().upper()
    if normalized_market != "CN":
        raise ValueError("The current acceptance benchmark is intentionally limited to CN while US is on hold.")

    repository = PredictionRepository(db)
    hot_run_id = _latest_hot_run_id(db, market=normalized_market)
    artifact = _cold_artifact(db, market=normalized_market, model_run_id=cold_model_run_id)
    manifest_path = Path(artifact.artifact_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    latest_trade_date = str(manifest.get("max_trade_date") or artifact.max_trade_date or "")
    if not latest_trade_date:
        raise RuntimeError(f"Cold artifact {artifact.model_run_id} has no maximum trade date.")
    seed_rows = read_prediction_artifact_rows(
        manifest_path,
        trade_dates=[latest_trade_date],
        include_details=False,
        limit=1,
    )
    if not seed_rows:
        raise RuntimeError(f"Cold artifact {artifact.model_run_id} has no rows on {latest_trade_date}.")
    symbol_id = int(seed_rows[0]["symbol_id"])

    results = {
        "hot_latest_cn_candidates": measure_query(
            lambda: repository.list_latest_predictions_for_market(normalized_market, limit=50),
            iterations=iterations,
            warmups=warmups,
            threshold_ms=DEFAULT_THRESHOLDS_MS["hot_latest_cn_candidates"],
        ),
        "hot_model_run_latest_date": measure_query(
            lambda: repository.list_predictions_for_run(hot_run_id, market=normalized_market, limit=100),
            iterations=iterations,
            warmups=warmups,
            threshold_ms=DEFAULT_THRESHOLDS_MS["hot_model_run_latest_date"],
        ),
        "cold_latest_date_slice": measure_query(
            lambda: read_prediction_artifact_rows(
                manifest_path,
                trade_dates=[latest_trade_date],
                include_details=True,
            ),
            iterations=iterations,
            warmups=warmups,
            threshold_ms=DEFAULT_THRESHOLDS_MS["cold_latest_date_slice"],
        ),
        "cold_single_symbol_slice": measure_query(
            lambda: read_prediction_artifact_rows(
                manifest_path,
                symbol_ids=[symbol_id],
                include_details=True,
            ),
            iterations=iterations,
            warmups=warmups,
            threshold_ms=DEFAULT_THRESHOLDS_MS["cold_single_symbol_slice"],
        ),
        "cold_full_artifact_scan": measure_query(
            lambda: read_prediction_artifact_rows(manifest_path, include_details=True),
            iterations=cold_full_iterations,
            warmups=max(1, min(warmups, 1)),
            threshold_ms=DEFAULT_THRESHOLDS_MS["cold_full_artifact_scan"],
        ),
    }
    database_version = str(db.scalar(text("select version()")) or "")
    database_name = str(db.scalar(text("select current_database()")) or "")
    return {
        "benchmark_version": "storage-architecture-read-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if all(item["status"] == "pass" for item in results.values()) else "fail",
        "mode": "read_only_prewarmed",
        "market": normalized_market,
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "database_name": database_name,
            "database_version": database_version,
        },
        "inputs": {
            "hot_model_run_id": hot_run_id,
            "cold_model_run_id": int(artifact.model_run_id),
            "cold_prediction_count": int(artifact.prediction_count),
            "cold_detail_count": int(artifact.detail_count),
            "cold_trade_date": latest_trade_date,
            "cold_symbol_id": symbol_id,
        },
        "results": results,
    }
