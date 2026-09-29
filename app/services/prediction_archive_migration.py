from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import ModelRun, Prediction, PredictionArtifact, PredictionDetail, PredictionExplanation
from app.services.prediction_artifacts import (
    DETAIL_FILE,
    EXPLANATION_FILE,
    MODEL_FILE,
    PREDICTION_FILE,
    PredictionArtifactWriter,
    _manifest_digest,
    _sha256,
    verify_prediction_artifact,
)
from app.services.time_utils import app_now_iso
from app.services.repository import PredictionArtifactRepository


DETAIL_COLUMNS = (
    "confidence",
    "bullish_prob",
    "bearish_prob",
    "expected_return_5d",
    "expected_return_20d",
    "expected_drawdown_20d",
    "model_reward_risk_ratio",
    "risk_score",
    "target_horizon_days",
    "universe_size",
    "percentile",
    "regime_label",
    "conviction_bucket",
    "position_size_hint",
    "entry_style",
    "signal_label",
    "signal_strength",
    "summary_text",
)
PREDICTION_COLUMNS = ("symbol_id", "trade_date", "score", "rank_value")
EXPLANATION_COLUMNS = (
    "symbol_id",
    "trade_date",
    "feature_name",
    "feature_value",
    "contribution",
    "direction",
    "display_order",
)

_STREAM_SCHEMAS = {
    PREDICTION_FILE: pa.schema(
        [
            ("model_run_id", pa.int64()),
            ("symbol_id", pa.int64()),
            ("trade_date", pa.string()),
            ("score", pa.float64()),
            ("rank_value", pa.float64()),
        ]
    ),
    DETAIL_FILE: pa.schema(
        [
            ("symbol_id", pa.int64()),
            ("trade_date", pa.string()),
            ("confidence", pa.float64()),
            ("bullish_prob", pa.float64()),
            ("bearish_prob", pa.float64()),
            ("expected_return_5d", pa.float64()),
            ("expected_return_20d", pa.float64()),
            ("expected_drawdown_20d", pa.float64()),
            ("model_reward_risk_ratio", pa.float64()),
            ("risk_score", pa.float64()),
            ("target_horizon_days", pa.int64()),
            ("universe_size", pa.int64()),
            ("percentile", pa.float64()),
            ("regime_label", pa.string()),
            ("conviction_bucket", pa.string()),
            ("position_size_hint", pa.string()),
            ("entry_style", pa.string()),
            ("signal_label", pa.string()),
            ("signal_strength", pa.float64()),
            ("summary_text", pa.string()),
        ]
    ),
    EXPLANATION_FILE: pa.schema(
        [
            ("symbol_id", pa.int64()),
            ("trade_date", pa.string()),
            ("feature_name", pa.string()),
            ("feature_value", pa.float64()),
            ("contribution", pa.float64()),
            ("direction", pa.string()),
            ("display_order", pa.int64()),
        ]
    ),
}


def _stream_query_to_parquet(
    db: Session,
    *,
    statement,
    path: Path,
    schema: pa.Schema,
    transform,
    batch_size: int = 50_000,
) -> int:
    """Write one ordered PostgreSQL result to Parquet with bounded memory."""

    count = 0
    writer = pq.ParquetWriter(
        path,
        schema,
        compression="zstd",
        write_statistics=True,
    )
    try:
        result = db.execute(
            statement.execution_options(stream_results=True, yield_per=batch_size)
        ).mappings()
        for partition in result.partitions(batch_size):
            rows = [transform(dict(row)) for row in partition]
            if not rows:
                continue
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
            count += len(rows)
    finally:
        writer.close()
    return count


def archive_large_model_run_from_postgres(
    db: Session,
    *,
    model_run_id: int,
    artifact_root: Path | None = None,
) -> dict:
    """Archive a multi-million-row run without materializing it in Python."""

    run_id = int(model_run_id)
    run = db.get(ModelRun, run_id)
    if run is None or str(run.status or "").lower() != "success":
        raise ValueError(f"Model run {run_id} is not a successful run.")
    existing = db.scalar(
        select(PredictionArtifact).where(PredictionArtifact.model_run_id == run_id)
    )
    if existing is not None and str(existing.status or "").lower() == "verified":
        verification = verify_prediction_artifact(existing.artifact_path)
        if verification["status"] != "success":
            raise RuntimeError(f"Registered artifact verification failed for run {run_id}.")
        return {
            "status": "success",
            "mode": "apply",
            "model_run_id": run_id,
            "idempotent": True,
            "artifact_path": existing.artifact_path,
            "manifest_sha256": existing.manifest_sha256,
        }

    counts = _database_counts(db, run_id)
    settings = get_settings()
    root = (artifact_root or settings.artifacts_dir / "prediction_runs").resolve()
    final_dir = root / f"model_run_id={run_id}"
    temporary_dir = root / f".model_run_id={run_id}.{uuid.uuid4().hex}.tmp"
    root.mkdir(parents=True, exist_ok=True)
    temporary_dir.mkdir(parents=False, exist_ok=False)
    try:
        prediction_count = _stream_query_to_parquet(
            db,
            statement=(
                select(
                    Prediction.symbol_id,
                    Prediction.trade_date,
                    Prediction.score,
                    Prediction.rank_value,
                )
                .where(Prediction.model_run_id == run_id)
                .order_by(
                    Prediction.trade_date.asc(),
                    Prediction.rank_value.asc(),
                    Prediction.symbol_id.asc(),
                )
            ),
            path=temporary_dir / PREDICTION_FILE,
            schema=_STREAM_SCHEMAS[PREDICTION_FILE],
            transform=lambda row: {
                "model_run_id": run_id,
                "symbol_id": int(row["symbol_id"]),
                "trade_date": str(row["trade_date"]),
                "score": row.get("score"),
                "rank_value": row.get("rank_value"),
            },
        )
        detail_columns = [getattr(PredictionDetail, name) for name in DETAIL_COLUMNS]
        detail_count = _stream_query_to_parquet(
            db,
            statement=(
                select(Prediction.symbol_id, Prediction.trade_date, *detail_columns)
                .join(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
                .where(Prediction.model_run_id == run_id)
                .order_by(Prediction.trade_date.asc(), Prediction.symbol_id.asc())
            ),
            path=temporary_dir / DETAIL_FILE,
            schema=_STREAM_SCHEMAS[DETAIL_FILE],
            transform=lambda row: {
                **{name: row.get(name) for name in DETAIL_COLUMNS},
                "symbol_id": int(row["symbol_id"]),
                "trade_date": str(row["trade_date"]),
            },
        )
        explanation_count = _stream_query_to_parquet(
            db,
            statement=(
                select(
                    Prediction.symbol_id,
                    Prediction.trade_date,
                    PredictionExplanation.feature_name,
                    PredictionExplanation.feature_value,
                    PredictionExplanation.contribution,
                    PredictionExplanation.direction,
                    PredictionExplanation.display_order,
                )
                .join(
                    PredictionExplanation,
                    PredictionExplanation.prediction_id == Prediction.id,
                )
                .where(Prediction.model_run_id == run_id)
                .order_by(
                    Prediction.trade_date.asc(),
                    Prediction.symbol_id.asc(),
                    PredictionExplanation.display_order.asc(),
                    PredictionExplanation.feature_name.asc(),
                )
            ),
            path=temporary_dir / EXPLANATION_FILE,
            schema=_STREAM_SCHEMAS[EXPLANATION_FILE],
            transform=lambda row: {
                **{name: row.get(name) for name in EXPLANATION_COLUMNS},
                "symbol_id": int(row["symbol_id"]),
                "trade_date": str(row["trade_date"]),
            },
        )
        streamed_counts = {
            "predictions": prediction_count,
            "prediction_details": detail_count,
            "prediction_explanations": explanation_count,
        }
        if streamed_counts != counts:
            raise RuntimeError(
                f"PostgreSQL stream changed while archiving run {run_id}: "
                f"expected={counts}, streamed={streamed_counts}"
            )

        metadata = _run_metadata(run)
        metadata["streaming_archive"] = {
            "version": "prediction-streaming-archive-v1",
            "batch_rows": 50_000,
            "source_counts_verified": True,
        }
        (temporary_dir / MODEL_FILE).write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2, default=str),
            encoding="utf-8",
        )
        date_bounds = db.execute(
            select(func.min(Prediction.trade_date), func.max(Prediction.trade_date)).where(
                Prediction.model_run_id == run_id
            )
        ).one()
        files = [PREDICTION_FILE, DETAIL_FILE, EXPLANATION_FILE, MODEL_FILE]
        manifest = {
            **metadata,
            "schema_version": str(settings.prediction_artifact_schema_version),
            "model_run_id": run_id,
            "market": str(run.market or "").upper() or None,
            "row_count": prediction_count,
            "detail_row_count": detail_count,
            "explanation_row_count": explanation_count,
            "min_trade_date": str(date_bounds[0]) if date_bounds[0] is not None else None,
            "max_trade_date": str(date_bounds[1]) if date_bounds[1] is not None else None,
            "created_at": app_now_iso(),
            "files": {
                name: {
                    "bytes": (temporary_dir / name).stat().st_size,
                    "sha256": _sha256(temporary_dir / name),
                }
                for name in files
            },
        }
        stable_content = {key: value for key, value in manifest.items() if key != "created_at"}
        manifest["content_sha256"] = _manifest_digest(stable_content)
        manifest["manifest_sha256"] = _manifest_digest(manifest)
        manifest_path = temporary_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, default=str),
            encoding="utf-8",
        )
        if final_dir.exists():
            raise RuntimeError(f"Immutable prediction artifact already exists for run {run_id}.")
        os.replace(temporary_dir, final_dir)
        published_path = final_dir / "manifest.json"
        verification = verify_prediction_artifact(published_path)
        if verification["status"] != "success":
            raise RuntimeError(f"Streamed artifact verification failed for run {run_id}.")
        registered = {**manifest, "artifact_path": str(published_path)}
        PredictionArtifactRepository(db).upsert_manifest(registered, status="verified")
        run.artifact_path = str(published_path)
        db.commit()
        return {
            "status": "success",
            "mode": "apply",
            "model_run_id": run_id,
            "market": run.market,
            **counts,
            "artifact_path": str(published_path),
            "manifest_sha256": manifest["manifest_sha256"],
            "verification_status": "success",
            "streaming": True,
            "idempotent": False,
            "source_rows_deleted": 0,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)


def _canonical_rows_digest(rows: list[dict], columns: tuple[str, ...]) -> str:
    normalized = [{name: row.get(name) for name in columns} for row in rows]
    serialized = sorted(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        for row in normalized
    )
    return hashlib.sha256("\n".join(serialized).encode("utf-8")).hexdigest()


def _mismatched_columns(source_rows: list[dict], artifact_rows: list[dict], columns: tuple[str, ...]) -> list[str]:
    key_columns = tuple(name for name in ("symbol_id", "trade_date", "feature_name") if name in columns)
    mismatches: list[str] = []
    for column in columns:
        selected_columns = tuple(dict.fromkeys((*key_columns, column)))
        if _canonical_rows_digest(source_rows, selected_columns) != _canonical_rows_digest(
            artifact_rows,
            selected_columns,
        ):
            mismatches.append(column)
    return mismatches


def compare_prediction_artifact_rows(
    manifest_path: Path | str,
    *,
    prediction_rows: list[dict],
    detail_rows: list[dict],
    explanation_rows: list[dict],
) -> dict:
    """Compare PostgreSQL source rows with all three Parquet payloads."""

    artifact_dir = Path(manifest_path).resolve().parent
    comparisons: dict[str, dict] = {}
    specs = (
        ("predictions", prediction_rows, PREDICTION_FILE, PREDICTION_COLUMNS),
        ("prediction_details", detail_rows, DETAIL_FILE, ("symbol_id", "trade_date", *DETAIL_COLUMNS)),
        ("prediction_explanations", explanation_rows, EXPLANATION_FILE, EXPLANATION_COLUMNS),
    )
    for name, source_rows, filename, columns in specs:
        cold_rows = pl.read_parquet(artifact_dir / filename).select(list(columns)).to_dicts()
        source_digest = _canonical_rows_digest(source_rows, columns)
        artifact_digest = _canonical_rows_digest(cold_rows, columns)
        comparisons[name] = {
            "source_rows": len(source_rows),
            "artifact_rows": len(cold_rows),
            "source_sha256": source_digest,
            "artifact_sha256": artifact_digest,
            "exact_match": len(source_rows) == len(cold_rows) and source_digest == artifact_digest,
        }
        if not comparisons[name]["exact_match"]:
            comparisons[name]["mismatched_columns"] = _mismatched_columns(source_rows, cold_rows, columns)
    failed = [name for name, result in comparisons.items() if not result["exact_match"]]
    return {
        "status": "success" if not failed else "failed",
        "failed_tables": failed,
        "tables": comparisons,
    }


def _run_metadata(run: ModelRun) -> dict:
    try:
        config = json.loads(run.config_json or "{}")
    except (TypeError, json.JSONDecodeError):
        config = {}
    return {
        "archive_source": "postgresql",
        "name": run.name,
        "model_type": run.model_type,
        "market": run.market,
        "universe": run.universe,
        "train_start": run.train_start,
        "train_end": run.train_end,
        "test_start": run.test_start,
        "test_end": run.test_end,
        "config": config if isinstance(config, dict) else {},
        "model_run_created_at": run.created_at,
        "model_run_finished_at": run.finished_at,
    }


def _database_counts(db: Session, run_id: int) -> dict[str, int]:
    prediction_count = int(
        db.scalar(select(func.count(Prediction.id)).where(Prediction.model_run_id == run_id)) or 0
    )
    detail_count = int(
        db.scalar(
            select(func.count(PredictionDetail.id))
            .join(Prediction, Prediction.id == PredictionDetail.prediction_id)
            .where(Prediction.model_run_id == run_id)
        )
        or 0
    )
    explanation_count = int(
        db.scalar(
            select(func.count(PredictionExplanation.id))
            .join(Prediction, Prediction.id == PredictionExplanation.prediction_id)
            .where(Prediction.model_run_id == run_id)
        )
        or 0
    )
    return {
        "predictions": prediction_count,
        "prediction_details": detail_count,
        "prediction_explanations": explanation_count,
    }


def _load_prediction_rows(db: Session, run_id: int) -> list[dict]:
    rows = db.execute(
        select(
            Prediction.symbol_id,
            Prediction.trade_date,
            Prediction.score,
            Prediction.rank_value,
        )
        .where(Prediction.model_run_id == run_id)
        .order_by(Prediction.trade_date.asc(), Prediction.rank_value.asc(), Prediction.symbol_id.asc())
    ).mappings()
    return [dict(row) for row in rows]


def _load_detail_rows(db: Session, run_id: int) -> list[dict]:
    columns = [getattr(PredictionDetail, name) for name in DETAIL_COLUMNS]
    rows = db.execute(
        select(Prediction.symbol_id, Prediction.trade_date, *columns)
        .join(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
        .where(Prediction.model_run_id == run_id)
        .order_by(Prediction.trade_date.asc(), Prediction.symbol_id.asc())
    ).mappings()
    return [dict(row) for row in rows]


def _load_explanation_rows(db: Session, run_id: int) -> list[dict]:
    rows = db.execute(
        select(
            Prediction.symbol_id,
            Prediction.trade_date,
            PredictionExplanation.feature_name,
            PredictionExplanation.feature_value,
            PredictionExplanation.contribution,
            PredictionExplanation.direction,
            PredictionExplanation.display_order,
        )
        .join(PredictionExplanation, PredictionExplanation.prediction_id == Prediction.id)
        .where(Prediction.model_run_id == run_id)
        .order_by(
            Prediction.trade_date.asc(),
            Prediction.symbol_id.asc(),
            PredictionExplanation.display_order.asc(),
            PredictionExplanation.feature_name.asc(),
        )
    ).mappings()
    return [dict(row) for row in rows]


def compare_model_run_artifact_to_postgres(db: Session, *, model_run_id: int) -> dict:
    """Run a pre-retention semantic parity check for one registered artifact."""

    run_id = int(model_run_id)
    artifact = db.scalar(select(PredictionArtifact).where(PredictionArtifact.model_run_id == run_id))
    if artifact is None:
        raise ValueError(f"Model run {run_id} does not have a registered prediction artifact.")
    verification = verify_prediction_artifact(artifact.artifact_path)
    if verification["status"] != "success":
        return {
            "status": "failed",
            "model_run_id": run_id,
            "integrity": verification,
            "failed_tables": ["artifact_integrity"],
            "tables": {},
        }
    parity = compare_prediction_artifact_rows(
        artifact.artifact_path,
        prediction_rows=_load_prediction_rows(db, run_id),
        detail_rows=_load_detail_rows(db, run_id),
        explanation_rows=_load_explanation_rows(db, run_id),
    )
    return {
        **parity,
        "model_run_id": run_id,
        "artifact_path": artifact.artifact_path,
        "manifest_sha256": artifact.manifest_sha256,
        "integrity_status": verification["status"],
    }


def archive_model_run_from_postgres(
    db: Session,
    *,
    model_run_id: int,
    dry_run: bool = True,
    artifact_root: Path | None = None,
) -> dict:
    """Archive one successful model run without deleting its PostgreSQL rows."""

    run_id = int(model_run_id)
    run = db.get(ModelRun, run_id)
    if run is None:
        raise ValueError(f"Model run {run_id} does not exist.")
    if str(run.status or "").lower() != "success":
        raise ValueError(f"Model run {run_id} is not successful and cannot be archived.")
    counts = _database_counts(db, run_id)
    receipt = {
        "status": "success",
        "mode": "dry_run" if dry_run else "apply",
        "model_run_id": run_id,
        "market": run.market,
        "model_type": run.model_type,
        **counts,
    }
    if dry_run:
        receipt["message"] = "Prediction archive preview completed; no artifact or database row was changed."
        return receipt
    if counts["predictions"] > 500_000 or sum(counts.values()) > 1_000_000:
        return archive_large_model_run_from_postgres(
            db,
            model_run_id=run_id,
            artifact_root=artifact_root,
        )

    existing = db.scalar(select(PredictionArtifact).where(PredictionArtifact.model_run_id == run_id))
    if existing is not None and str(existing.status or "").lower() == "verified":
        verification = verify_prediction_artifact(existing.artifact_path)
        if verification["status"] != "success":
            raise RuntimeError(f"Registered artifact verification failed for model run {run_id}: {verification['errors']}")
        return {
            **receipt,
            "status": "success",
            "idempotent": True,
            "artifact_path": existing.artifact_path,
            "manifest_sha256": existing.manifest_sha256,
            "message": "Existing verified prediction artifact passed integrity verification.",
        }

    prediction_rows = _load_prediction_rows(db, run_id)
    detail_rows = _load_detail_rows(db, run_id)
    explanation_rows = _load_explanation_rows(db, run_id)
    loaded_counts = {
        "predictions": len(prediction_rows),
        "prediction_details": len(detail_rows),
        "prediction_explanations": len(explanation_rows),
    }
    if loaded_counts != counts:
        raise RuntimeError(
            f"PostgreSQL archive snapshot changed while reading model run {run_id}: "
            f"expected={counts}, loaded={loaded_counts}"
        )
    manifest = PredictionArtifactWriter(root=artifact_root).write(
        model_run_id=run_id,
        market=run.market,
        prediction_rows=prediction_rows,
        detail_rows=detail_rows,
        explanation_rows=explanation_rows,
        model_metadata=_run_metadata(run),
    )
    verification = verify_prediction_artifact(manifest["artifact_path"])
    if verification["status"] != "success":
        raise RuntimeError(f"Artifact verification failed for model run {run_id}: {verification['errors']}")
    artifact_counts = {
        "predictions": int(manifest.get("row_count") or 0),
        "prediction_details": int(manifest.get("detail_row_count") or 0),
        "prediction_explanations": int(manifest.get("explanation_row_count") or 0),
    }
    if artifact_counts != counts:
        raise RuntimeError(
            f"Artifact row count mismatch for model run {run_id}: expected={counts}, artifact={artifact_counts}"
        )
    parity = compare_prediction_artifact_rows(
        manifest["artifact_path"],
        prediction_rows=prediction_rows,
        detail_rows=detail_rows,
        explanation_rows=explanation_rows,
    )
    if parity["status"] != "success":
        raise RuntimeError(f"Artifact parity failed for model run {run_id}: {parity['failed_tables']}")
    PredictionArtifactRepository(db).upsert_manifest(manifest, status="verified")
    run.artifact_path = str(manifest["artifact_path"])
    db.commit()
    return {
        **receipt,
        "artifact_path": str(manifest["artifact_path"]),
        "manifest_sha256": str(manifest["manifest_sha256"]),
        "verification_status": verification["status"],
        "parity": parity,
        "idempotent": False,
        "message": "Prediction archive published and verified; PostgreSQL source rows were preserved.",
    }


def archive_model_runs_batch(
    db: Session,
    *,
    market: str,
    limit: int = 5,
    keep_latest_runs: int = 20,
    max_predictions_per_run: int = 250_000,
    max_total_rows_per_run: int = 500_000,
    dry_run: bool = True,
) -> dict:
    """Archive a bounded batch outside the online retention window."""

    normalized_market = str(market or "").strip().upper()
    if normalized_market not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    runs = list(
        db.scalars(
            select(ModelRun)
            .where(ModelRun.status == "success", ModelRun.market == normalized_market)
            .order_by(ModelRun.id.desc())
        ).all()
    )
    stale_runs = runs[max(1, int(keep_latest_runs)) :]
    registered_ids = set(
        int(run_id)
        for run_id in db.scalars(
            select(PredictionArtifact.model_run_id).where(PredictionArtifact.status == "verified")
        ).all()
    )
    receipts: list[dict] = []
    skipped_oversized: list[dict] = []
    batch_limit = max(1, int(limit))
    row_limit = max(1, int(max_predictions_per_run))
    total_row_limit = max(row_limit, int(max_total_rows_per_run))
    for run in reversed(stale_runs):
        if int(run.id) in registered_ids:
            continue
        counts = _database_counts(db, int(run.id))
        total_rows = sum(counts.values())
        if counts["predictions"] > row_limit or total_rows > total_row_limit:
            skipped_oversized.append(
                {
                    "model_run_id": int(run.id),
                    **counts,
                    "total_rows": total_rows,
                    "reason": (
                        "prediction_limit"
                        if counts["predictions"] > row_limit
                        else "total_related_rows_limit"
                    ),
                }
            )
            continue
        receipts.append(
            archive_model_run_from_postgres(
                db,
                model_run_id=int(run.id),
                dry_run=dry_run,
            )
        )
        if len(receipts) >= batch_limit:
            break
    return {
        "status": "success",
        "mode": "dry_run" if dry_run else "apply",
        "market": normalized_market,
        "keep_latest_runs": max(1, int(keep_latest_runs)),
        "max_predictions_per_run": row_limit,
        "max_total_rows_per_run": total_row_limit,
        "processed_runs": len(receipts),
        "processed_model_run_ids": [int(item["model_run_id"]) for item in receipts],
        "skipped_oversized": skipped_oversized,
        "receipts": receipts,
        "source_rows_deleted": 0,
    }
