from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from pathlib import Path

import polars as pl

from app.core.config import get_settings
from app.services.time_utils import app_now_iso


PREDICTION_FILE = "predictions.parquet"
DETAIL_FILE = "prediction_details.parquet"
EXPLANATION_FILE = "prediction_explanations.parquet"
MODEL_FILE = "model.json"
MANIFEST_FILE = "manifest.json"
PUBLICATION_PLAN_VERSION = "prediction-publication-plan-v1"
PUBLICATION_ESTIMATE_SAMPLE_ROWS = 256
PUBLICATION_ESTIMATE_SAFETY_FACTOR = 1.25


class PredictionPublicationLimitError(RuntimeError):
    """Raised before staging when a publication exceeds a configured limit."""

    def __init__(self, plan: dict) -> None:
        self.plan = dict(plan)
        violations = ", ".join(str(item) for item in plan.get("violations") or [])
        super().__init__(f"Prediction publication blocked: {violations or 'configured limit exceeded'}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_parquet(path: Path, rows: list[dict], *, schema: dict[str, pl.DataType], sort_by: list[str]) -> None:
    if rows:
        frame = pl.DataFrame(rows, infer_schema_length=None)
        for name, dtype in schema.items():
            if name not in frame.columns:
                frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(name))
        frame = frame.select(list(schema)).cast(schema, strict=False)
        available_sort = [name for name in sort_by if name in frame.columns]
        if available_sort:
            frame = frame.sort(available_sort)
    else:
        frame = pl.DataFrame(schema=schema)
    frame.write_parquet(path, compression="zstd", statistics=True)


def _manifest_digest(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _estimate_row_collection_bytes(rows: list[dict]) -> int:
    """Return a deterministic, conservative JSON-size proxy for Parquet staging.

    The estimate deliberately avoids serializing every row for very large jobs.
    Sampling the head and tail captures both sparse and enriched rows while the
    safety factor covers JSON delimiters, Parquet metadata and estimation error.
    """

    count = len(rows)
    if count <= 0:
        return 0
    sample_limit = min(count, PUBLICATION_ESTIMATE_SAMPLE_ROWS)
    head_count = (sample_limit + 1) // 2
    tail_count = sample_limit - head_count
    sample = rows[:head_count]
    if tail_count:
        sample = [*sample, *rows[-tail_count:]]
    encoded_bytes = sum(
        len(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        + 1
        for row in sample
    )
    average = encoded_bytes / max(len(sample), 1)
    return int(math.ceil(average * count * PUBLICATION_ESTIMATE_SAFETY_FACTOR))


def build_prediction_publication_plan(
    *,
    prediction_rows: list[dict],
    detail_rows: list[dict] | None = None,
    explanation_rows: list[dict] | None = None,
    staging_threshold_rows: int,
    max_rows: int,
    max_estimated_bytes: int,
) -> dict:
    """Plan one immutable publication and fail closed before staging begins."""

    thresholds = {
        "staging_threshold_rows": int(staging_threshold_rows),
        "max_rows": int(max_rows),
        "max_estimated_bytes": int(max_estimated_bytes),
    }
    if thresholds["staging_threshold_rows"] < 1:
        raise ValueError("prediction publication staging threshold must be positive")
    if thresholds["max_rows"] < thresholds["staging_threshold_rows"]:
        raise ValueError("prediction publication max rows must be >= staging threshold")
    if thresholds["max_estimated_bytes"] < 1:
        raise ValueError("prediction publication byte limit must be positive")

    collections = {
        "predictions": prediction_rows,
        "details": detail_rows or [],
        "explanations": explanation_rows or [],
    }
    row_counts = {name: len(rows) for name, rows in collections.items()}
    estimated_bytes_by_collection = {
        name: _estimate_row_collection_bytes(rows)
        for name, rows in collections.items()
    }
    total_rows = sum(row_counts.values())
    estimated_bytes = sum(estimated_bytes_by_collection.values())
    staging_required = total_rows > thresholds["staging_threshold_rows"]
    violations: list[str] = []
    if total_rows > thresholds["max_rows"]:
        violations.append(
            f"total_rows={total_rows}>max_rows={thresholds['max_rows']}"
        )
    if estimated_bytes > thresholds["max_estimated_bytes"]:
        violations.append(
            "estimated_bytes="
            f"{estimated_bytes}>max_estimated_bytes={thresholds['max_estimated_bytes']}"
        )
    plan = {
        "plan_version": PUBLICATION_PLAN_VERSION,
        "status": "blocked" if violations else "allowed",
        "mode": "staging_atomic_publish" if staging_required else "atomic_temp_publish",
        "staging_required": staging_required,
        "row_counts": row_counts,
        "total_rows": total_rows,
        "estimated_bytes_by_collection": estimated_bytes_by_collection,
        "estimated_bytes": estimated_bytes,
        "estimate_safety_factor": PUBLICATION_ESTIMATE_SAFETY_FACTOR,
        "thresholds": thresholds,
        "violations": violations,
        "cleanup_policy": "remove_unpublished_staging_on_failure_or_cancel",
    }
    if violations:
        raise PredictionPublicationLimitError(plan)
    return plan


def select_hot_prediction_rows(
    rows: list[dict],
    *,
    full_trade_days: int = 5,
    top_k: int = 100,
) -> list[dict]:
    """Keep recent complete cross-sections plus historical Top-K rows.

    This selector is deterministic and does not mutate source rows. It is
    feature-gated until analytical readers can consume cold artifacts.
    """

    if not rows:
        return []
    dates = sorted({str(row.get("trade_date") or "") for row in rows if row.get("trade_date")})
    full_dates = set(dates[-max(1, int(full_trade_days)) :])
    limit = max(1, int(top_k))
    selected: dict[tuple[int, str], dict] = {}
    for row in rows:
        trade_date = str(row.get("trade_date") or "")
        try:
            rank_value = float(row.get("rank_value"))
        except (TypeError, ValueError):
            rank_value = float("inf")
        if trade_date in full_dates or rank_value <= limit:
            key = (int(row["symbol_id"]), trade_date)
            selected[key] = dict(row)
    return sorted(selected.values(), key=lambda row: (str(row["trade_date"]), float(row.get("rank_value") or 0), int(row["symbol_id"])))


def select_hot_explanation_rows(
    explanation_rows: list[dict],
    *,
    prediction_rows: list[dict],
    top_k: int = 50,
    holding_symbol_ids: set[int] | None = None,
    boundary_radius: int = 5,
) -> list[dict]:
    """Materialize only the latest Top-K, holdings and promotion boundary.

    The complete latest cross-section remains in the immutable Parquet
    artifact. This selector defines the intentionally bounded PostgreSQL
    explanation layer.
    """

    if not explanation_rows or not prediction_rows:
        return []
    latest_trade_date = max(
        (str(row.get("trade_date") or "") for row in prediction_rows),
        default="",
    )
    if not latest_trade_date:
        return []
    candidate_limit = max(1, int(top_k))
    radius = max(0, int(boundary_radius))
    holdings = {int(item) for item in (holding_symbol_ids or set())}
    selected_keys: set[tuple[int, str]] = set()
    for row in prediction_rows:
        trade_date = str(row.get("trade_date") or "")
        if trade_date != latest_trade_date:
            continue
        symbol_id = int(row["symbol_id"])
        try:
            rank_value = float(row.get("rank_value"))
        except (TypeError, ValueError):
            rank_value = float("inf")
        is_top_candidate = rank_value <= candidate_limit
        is_boundary_sample = (
            radius > 0
            and candidate_limit < rank_value <= candidate_limit + radius
        )
        if is_top_candidate or is_boundary_sample or symbol_id in holdings:
            selected_keys.add((symbol_id, trade_date))
    return [
        dict(row)
        for row in explanation_rows
        if (int(row["symbol_id"]), str(row.get("trade_date") or ""))
        in selected_keys
    ]


class PredictionArtifactWriter:
    def __init__(
        self,
        root: Path | None = None,
        *,
        staging_threshold_rows: int | None = None,
        max_rows: int | None = None,
        max_estimated_bytes: int | None = None,
    ) -> None:
        settings = get_settings()
        self.root = (root or settings.artifacts_dir / "prediction_runs").resolve()
        self.schema_version = str(settings.prediction_artifact_schema_version)
        self.staging_threshold_rows = int(
            staging_threshold_rows
            if staging_threshold_rows is not None
            else settings.prediction_publication_staging_threshold_rows
        )
        self.max_rows = int(
            max_rows if max_rows is not None else settings.prediction_publication_max_rows
        )
        self.max_estimated_bytes = int(
            max_estimated_bytes
            if max_estimated_bytes is not None
            else settings.prediction_publication_max_estimated_bytes
        )

    def plan(
        self,
        *,
        prediction_rows: list[dict],
        detail_rows: list[dict] | None = None,
        explanation_rows: list[dict] | None = None,
    ) -> dict:
        return build_prediction_publication_plan(
            prediction_rows=prediction_rows,
            detail_rows=detail_rows,
            explanation_rows=explanation_rows,
            staging_threshold_rows=self.staging_threshold_rows,
            max_rows=self.max_rows,
            max_estimated_bytes=self.max_estimated_bytes,
        )

    def write(
        self,
        *,
        model_run_id: int,
        market: str | None,
        prediction_rows: list[dict],
        detail_rows: list[dict] | None = None,
        explanation_rows: list[dict] | None = None,
        model_metadata: dict | None = None,
        publication_plan: dict | None = None,
    ) -> dict:
        run_id = int(model_run_id)
        if run_id <= 0:
            raise ValueError("model_run_id must be positive")
        predictions = [dict(row) for row in prediction_rows]
        details = [dict(row) for row in (detail_rows or [])]
        explanations = [dict(row) for row in (explanation_rows or [])]
        effective_plan = self.plan(
            prediction_rows=predictions,
            detail_rows=details,
            explanation_rows=explanations,
        )
        if publication_plan is not None and dict(publication_plan) != effective_plan:
            raise RuntimeError("Prediction publication plan changed before staging.")
        final_dir = self.root / f"model_run_id={run_id}"
        temporary_dir = self.root / f".model_run_id={run_id}.{uuid.uuid4().hex}.tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        temporary_dir.mkdir(parents=False, exist_ok=False)
        try:
            prediction_payload = [
                {
                    "model_run_id": run_id,
                    "symbol_id": int(row["symbol_id"]),
                    "trade_date": str(row["trade_date"]),
                    "score": row.get("score"),
                    "rank_value": row.get("rank_value"),
                }
                for row in predictions
            ]
            _write_parquet(
                temporary_dir / PREDICTION_FILE,
                prediction_payload,
                schema={
                    "model_run_id": pl.Int64,
                    "symbol_id": pl.Int64,
                    "trade_date": pl.String,
                    "score": pl.Float64,
                    "rank_value": pl.Float64,
                },
                sort_by=["trade_date", "rank_value", "symbol_id"],
            )
            detail_schema = {
                "symbol_id": pl.Int64,
                "trade_date": pl.String,
                "confidence": pl.Float64,
                "bullish_prob": pl.Float64,
                "bearish_prob": pl.Float64,
                "expected_return_5d": pl.Float64,
                "expected_return_20d": pl.Float64,
                "expected_drawdown_20d": pl.Float64,
                "model_reward_risk_ratio": pl.Float64,
                "risk_score": pl.Float64,
                "target_horizon_days": pl.Int64,
                "universe_size": pl.Int64,
                "percentile": pl.Float64,
                "regime_label": pl.String,
                "conviction_bucket": pl.String,
                "position_size_hint": pl.String,
                "entry_style": pl.String,
                "signal_label": pl.String,
                "signal_strength": pl.Float64,
                "summary_text": pl.String,
            }
            _write_parquet(
                temporary_dir / DETAIL_FILE,
                details,
                schema=detail_schema,
                sort_by=["trade_date", "symbol_id"],
            )
            _write_parquet(
                temporary_dir / EXPLANATION_FILE,
                explanations,
                schema={
                    "symbol_id": pl.Int64,
                    "trade_date": pl.String,
                    "feature_name": pl.String,
                    "feature_value": pl.Float64,
                    "contribution": pl.Float64,
                    "direction": pl.String,
                    "display_order": pl.Int64,
                },
                sort_by=["trade_date", "symbol_id", "display_order", "feature_name"],
            )
            metadata = dict(model_metadata or {})
            metadata["prediction_publication_plan"] = effective_plan
            (temporary_dir / MODEL_FILE).write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2, default=str),
                encoding="utf-8",
            )
            trade_dates = sorted({str(row["trade_date"]) for row in prediction_payload})
            files = [PREDICTION_FILE, DETAIL_FILE, EXPLANATION_FILE, MODEL_FILE]
            manifest = {
                **metadata,
                "schema_version": self.schema_version,
                "model_run_id": run_id,
                "market": str(market or "").upper() or None,
                "row_count": len(prediction_payload),
                "detail_row_count": len(details),
                "explanation_row_count": len(explanations),
                "min_trade_date": min(trade_dates, default=None),
                "max_trade_date": max(trade_dates, default=None),
                "created_at": app_now_iso(),
                "files": {
                    name: {"bytes": (temporary_dir / name).stat().st_size, "sha256": _sha256(temporary_dir / name)}
                    for name in files
                },
            }
            stable_content = {key: value for key, value in manifest.items() if key != "created_at"}
            manifest["content_sha256"] = _manifest_digest(stable_content)
            manifest_sha256 = _manifest_digest(manifest)
            manifest["manifest_sha256"] = manifest_sha256
            (temporary_dir / MANIFEST_FILE).write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, default=str),
                encoding="utf-8",
            )
            if final_dir.exists():
                existing_path = final_dir / MANIFEST_FILE
                existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else {}
                if existing.get("content_sha256") != manifest.get("content_sha256"):
                    raise RuntimeError(f"Immutable prediction artifact already exists for model run {run_id}.")
                return {**existing, "artifact_path": str(existing_path)}
            os.replace(temporary_dir, final_dir)
            return {**manifest, "artifact_path": str(final_dir / MANIFEST_FILE)}
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir)


def verify_prediction_artifact(manifest_path: Path | str) -> dict:
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected_manifest_hash = str(manifest.get("manifest_sha256") or "")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    errors: list[str] = []
    if _manifest_digest(unsigned) != expected_manifest_hash:
        errors.append("manifest_sha256_mismatch")
    for name, metadata in (manifest.get("files") or {}).items():
        file_path = path.parent / str(name)
        if not file_path.is_file():
            errors.append(f"missing:{name}")
            continue
        if _sha256(file_path) != str((metadata or {}).get("sha256") or ""):
            errors.append(f"sha256_mismatch:{name}")
    return {"status": "success" if not errors else "failed", "errors": errors, "manifest": manifest}


def read_prediction_artifact_rows(
    manifest_path: Path | str,
    *,
    symbol_ids: list[int] | None = None,
    trade_dates: list[str] | None = None,
    include_details: bool = True,
    limit: int | None = None,
) -> list[dict]:
    """Read a filtered prediction slice from an immutable run artifact.

    Filtering is pushed into Polars' lazy Parquet scan so callers do not have
    to load a full-market run merely to recover one symbol or trading day.
    Artifact integrity is verified when it is registered; callers that need a
    fresh byte-level audit should call ``verify_prediction_artifact`` first.
    """

    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if str(manifest.get("schema_version") or "") != str(get_settings().prediction_artifact_schema_version):
        raise RuntimeError(f"Unsupported prediction artifact schema: {manifest.get('schema_version')}")
    prediction_path = path.parent / PREDICTION_FILE
    if not prediction_path.is_file():
        raise FileNotFoundError(prediction_path)
    frame = pl.scan_parquet(prediction_path)
    normalized_symbol_ids = sorted({int(item) for item in (symbol_ids or [])})
    normalized_trade_dates = sorted({str(item) for item in (trade_dates or []) if str(item)})
    if normalized_symbol_ids:
        frame = frame.filter(pl.col("symbol_id").is_in(normalized_symbol_ids))
    if normalized_trade_dates:
        frame = frame.filter(pl.col("trade_date").is_in(normalized_trade_dates))
    if include_details:
        detail_path = path.parent / DETAIL_FILE
        if detail_path.is_file():
            details = pl.scan_parquet(detail_path)
            if normalized_symbol_ids:
                details = details.filter(pl.col("symbol_id").is_in(normalized_symbol_ids))
            if normalized_trade_dates:
                details = details.filter(pl.col("trade_date").is_in(normalized_trade_dates))
            frame = frame.join(details, on=["symbol_id", "trade_date"], how="left")
    frame = frame.sort(["trade_date", "rank_value", "symbol_id"], descending=[True, False, False])
    if limit is not None and int(limit) > 0:
        frame = frame.limit(int(limit))
    return frame.collect(engine="streaming").to_dicts()


def read_prediction_explanation_artifact_rows(
    manifest_path: Path | str,
    *,
    symbol_ids: list[int] | None = None,
    trade_dates: list[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read explanation rows with predicate pushdown from a verified artifact."""

    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if str(manifest.get("schema_version") or "") != str(
        get_settings().prediction_artifact_schema_version
    ):
        raise RuntimeError(
            f"Unsupported prediction artifact schema: {manifest.get('schema_version')}"
        )
    explanation_path = path.parent / EXPLANATION_FILE
    if not explanation_path.is_file():
        return []
    frame = pl.scan_parquet(explanation_path)
    normalized_symbol_ids = sorted({int(item) for item in (symbol_ids or [])})
    normalized_trade_dates = sorted(
        {str(item) for item in (trade_dates or []) if str(item)}
    )
    if normalized_symbol_ids:
        frame = frame.filter(pl.col("symbol_id").is_in(normalized_symbol_ids))
    if normalized_trade_dates:
        frame = frame.filter(pl.col("trade_date").is_in(normalized_trade_dates))
    frame = frame.sort(
        ["trade_date", "display_order", "feature_name"],
        descending=[True, False, False],
    )
    if limit is not None and int(limit) > 0:
        frame = frame.limit(int(limit))
    return frame.collect(engine="streaming").to_dicts()
