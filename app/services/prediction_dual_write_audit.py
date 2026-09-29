from __future__ import annotations

import json
from pathlib import Path

import polars as pl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tables import ModelEvaluation, ModelEvaluationMetric, ModelRun, PredictionArtifact
from app.services.prediction_archive_migration import (
    DETAIL_COLUMNS,
    EXPLANATION_COLUMNS,
    PREDICTION_COLUMNS,
    _canonical_rows_digest,
    _load_detail_rows,
    _load_explanation_rows,
    _load_prediction_rows,
    _mismatched_columns,
)
from app.services.prediction_artifacts import (
    DETAIL_FILE,
    EXPLANATION_FILE,
    PREDICTION_FILE,
    select_hot_explanation_rows,
    select_hot_prediction_rows,
    verify_prediction_artifact,
)
from app.services.portfolio_book import load_portfolio_positions
from app.services.market_calendar import next_market_open_date
from app.services.market_hot_predictions import MarketHotPredictionRepository
from app.services.repository import PRODUCTION_SIGNAL_MODEL_TYPES, SymbolRepository
from app.services.time_utils import app_now_iso


PRODUCTION_POSTGRESQL_WRITE_LIMIT_MS = 30_000.0
PUBLICATION_TIMING_VERSION = "prediction-publication-timing-v2"
REQUIRED_PUBLICATION_TIMING_FIELDS = (
    "artifact_publish_ms",
    "legacy_predictions_ms",
    "legacy_details_ms",
    "legacy_explanations_ms",
    "physical_hot_ms",
    "live_predictions_ms",
    "postgresql_commit_ms",
    "postgresql_write_phase_ms",
    "publication_total_ms",
)
REQUIRED_PUBLICATION_MEMORY_FIELDS = (
    "process_peak_rss_bytes",
    "python_tracemalloc_peak_bytes",
    "python_publication_incremental_peak_bytes",
)


def dual_write_runtime_evidence_passed(payload: dict) -> bool:
    """Return true only when every counted run carries passing v2 evidence."""

    if payload.get("audit_version") != "prediction-dual-write-v2":
        return False
    passed_ids = {int(value) for value in payload.get("passed_runs") or []}
    if not passed_ids:
        return False
    runs = {
        int(item.get("model_run_id") or 0): item
        for item in payload.get("runs") or []
        if isinstance(item, dict)
    }
    return all(
        run_id in runs
        and (runs[run_id].get("publication_runtime") or {}).get("status") == "pass"
        for run_id in passed_ids
    )


def audit_publication_timing(config_json: str | dict | None) -> dict:
    """Validate the per-run latency and peak-memory acceptance contract."""

    if isinstance(config_json, dict):
        config = config_json
    else:
        try:
            config = json.loads(str(config_json or ""))
        except json.JSONDecodeError:
            config = {}
    timing = config.get("prediction_publication_timing") if isinstance(config, dict) else None
    if not isinstance(timing, dict) or timing.get("timing_version") != PUBLICATION_TIMING_VERSION:
        return {
            "status": "pending",
            "reason": "missing_v2_publication_timing",
            "required_version": PUBLICATION_TIMING_VERSION,
            "postgresql_write_limit_ms": PRODUCTION_POSTGRESQL_WRITE_LIMIT_MS,
        }
    missing_fields = [
        name
        for name in (*REQUIRED_PUBLICATION_TIMING_FIELDS, *REQUIRED_PUBLICATION_MEMORY_FIELDS)
        if name not in timing
    ]
    invalid_fields: list[str] = []
    for name in (*REQUIRED_PUBLICATION_TIMING_FIELDS, *REQUIRED_PUBLICATION_MEMORY_FIELDS):
        if name not in timing:
            continue
        try:
            value = float(timing[name])
        except (TypeError, ValueError):
            invalid_fields.append(name)
            continue
        if value < 0:
            invalid_fields.append(name)
    process_peak_rss = int(timing.get("process_peak_rss_bytes") or 0)
    if process_peak_rss <= 0 and "process_peak_rss_bytes" not in invalid_fields:
        invalid_fields.append("process_peak_rss_bytes")
    postgres_ms = float(timing.get("postgresql_write_phase_ms") or 0.0)
    checks = {
        "timing_version_v2": timing.get("timing_version") == PUBLICATION_TIMING_VERSION,
        "all_latency_and_memory_fields_present": not missing_fields,
        "all_latency_and_memory_fields_valid": not invalid_fields,
        "postgresql_write_phase_at_most_30_seconds": postgres_ms
        <= PRODUCTION_POSTGRESQL_WRITE_LIMIT_MS,
        "postgresql_outputs_published_atomically": timing.get(
            "postgresql_atomic_publish"
        )
        is True
        and timing.get("postgresql_transaction_version")
        == "prediction-publication-transaction-v1",
    }
    passed = all(checks.values())
    return {
        "status": "pass" if passed else "fail",
        "timing_version": timing.get("timing_version"),
        "postgresql_write_phase_ms": postgres_ms,
        "postgresql_write_limit_ms": PRODUCTION_POSTGRESQL_WRITE_LIMIT_MS,
        "publication_total_ms": float(timing.get("publication_total_ms") or 0.0),
        "process_peak_rss_bytes": process_peak_rss,
        "python_tracemalloc_peak_bytes": int(
            timing.get("python_tracemalloc_peak_bytes") or 0
        ),
        "python_publication_incremental_peak_bytes": int(
            timing.get("python_publication_incremental_peak_bytes") or 0
        ),
        "missing_fields": missing_fields,
        "invalid_fields": sorted(set(invalid_fields)),
        "checks": checks,
    }


def _compare_rows(source_rows: list[dict], target_rows: list[dict], columns: tuple[str, ...]) -> dict:
    source_digest = _canonical_rows_digest(source_rows, columns)
    target_digest = _canonical_rows_digest(target_rows, columns)
    exact_match = len(source_rows) == len(target_rows) and source_digest == target_digest
    result = {
        "source_rows": len(source_rows),
        "target_rows": len(target_rows),
        "source_sha256": source_digest,
        "target_sha256": target_digest,
        "exact_match": exact_match,
    }
    if not exact_match:
        result["mismatched_columns"] = _mismatched_columns(source_rows, target_rows, columns)
    return result


def _ordered_top_k_rows(rows: list[dict], *, top_k: int) -> list[dict]:
    limit = max(1, int(top_k))
    selected = []
    for row in rows:
        try:
            rank_value = float(row.get("rank_value"))
        except (TypeError, ValueError):
            continue
        if rank_value <= limit:
            selected.append({name: row.get(name) for name in PREDICTION_COLUMNS})
    return sorted(
        selected,
        key=lambda row: (
            str(row.get("trade_date") or ""),
            float(row.get("rank_value") or 0),
            int(row.get("symbol_id") or 0),
        ),
    )


def compare_compact_prediction_rows(
    cold_rows: list[dict],
    hot_rows: list[dict],
    *,
    full_trade_days: int,
    top_k: int,
) -> dict:
    expected_hot_rows = select_hot_prediction_rows(
        cold_rows,
        full_trade_days=full_trade_days,
        top_k=top_k,
    )
    compact = _compare_rows(expected_hot_rows, hot_rows, PREDICTION_COLUMNS)
    cold_top_k = _ordered_top_k_rows(cold_rows, top_k=top_k)
    hot_top_k = _ordered_top_k_rows(hot_rows, top_k=top_k)
    top_k_comparison = _compare_rows(cold_top_k, hot_top_k, PREDICTION_COLUMNS)
    return {
        "status": "pass" if compact["exact_match"] and top_k_comparison["exact_match"] else "fail",
        "cold_full_rows": len(cold_rows),
        "expected_hot_rows": len(expected_hot_rows),
        "actual_hot_rows": len(hot_rows),
        "reduction_rows": len(cold_rows) - len(hot_rows),
        "reduction_ratio": round(1.0 - (len(hot_rows) / max(1, len(cold_rows))), 8),
        "compact_rows": compact,
        "top_k_rows": top_k_comparison,
    }


def compare_bounded_explanation_rows(
    cold_prediction_rows: list[dict],
    cold_explanation_rows: list[dict],
    hot_explanation_rows: list[dict],
    *,
    top_k: int,
    boundary_radius: int,
    holding_symbol_ids: set[int] | None = None,
) -> dict:
    expected_hot_rows = select_hot_explanation_rows(
        cold_explanation_rows,
        prediction_rows=cold_prediction_rows,
        top_k=top_k,
        boundary_radius=boundary_radius,
        holding_symbol_ids=holding_symbol_ids,
    )
    comparison = _compare_rows(
        expected_hot_rows,
        hot_explanation_rows,
        tuple(EXPLANATION_COLUMNS),
    )
    return {
        "status": "pass" if comparison["exact_match"] else "fail",
        "cold_full_rows": len(cold_explanation_rows),
        "expected_hot_rows": len(expected_hot_rows),
        "actual_hot_rows": len(hot_explanation_rows),
        "reduction_rows": len(cold_explanation_rows) - len(hot_explanation_rows),
        "reduction_ratio": round(
            1.0
            - (
                len(hot_explanation_rows)
                / max(1, len(cold_explanation_rows))
            ),
            8,
        ),
        "selected_rows": comparison,
    }


def _current_holding_symbol_ids(db: Session, *, market: str) -> set[int]:
    """Compatibility fallback for v1 manifests written before IDs were frozen."""

    try:
        positions = load_portfolio_positions()
    except Exception:
        return set()
    market_code = str(market or "").strip().upper()
    tickers = {
        str(item.get("ticker") or "").strip().upper()
        for item in positions
        if str(item.get("ticker") or "").strip()
        and str(item.get("market") or "").strip().upper() == market_code
    }
    repository = SymbolRepository(db)
    symbol_ids: set[int] = set()
    for ticker in sorted(tickers):
        symbol = repository.get_by_ticker(ticker)
        if symbol is not None and str(symbol.market or "").strip().upper() == market_code:
            symbol_ids.add(int(symbol.id))
    return symbol_ids


def _parquet_rows(manifest_path: Path, filename: str, columns: tuple[str, ...]) -> list[dict]:
    path = manifest_path.parent / filename
    if not path.is_file():
        return []
    return pl.read_parquet(path).select(list(columns)).to_dicts()


def audit_compact_model_run(db: Session, *, artifact: PredictionArtifact, run: ModelRun) -> dict:
    manifest_path = Path(artifact.artifact_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    contract = manifest.get("prediction_storage_contract")
    if not isinstance(contract, dict) or contract.get("contract_version") != "prediction-storage-v1":
        return {
            "status": "excluded",
            "model_run_id": int(run.id),
            "reason": "missing_prediction_storage_contract",
        }
    if str(contract.get("hot_write_mode") or "") != "compact":
        return {
            "status": "excluded",
            "model_run_id": int(run.id),
            "reason": "not_compact_hot_write",
        }

    integrity = verify_prediction_artifact(manifest_path)
    if integrity["status"] != "success":
        return {
            "status": "fail",
            "model_run_id": int(run.id),
            "reason": "artifact_integrity_failed",
            "integrity": integrity,
        }

    prediction_columns = tuple(PREDICTION_COLUMNS)
    detail_columns = ("symbol_id", "trade_date", *DETAIL_COLUMNS)
    explanation_columns = tuple(EXPLANATION_COLUMNS)
    cold_predictions = _parquet_rows(manifest_path, PREDICTION_FILE, prediction_columns)
    trade_dates = sorted(
        {
            str(row.get("trade_date") or "")[:10]
            for row in cold_predictions
            if row.get("trade_date")
        }
    )
    physical_predictions, physical_details, physical_explanations = (
        MarketHotPredictionRepository(db)._load_rows(
            market=str(run.market or ""),
            model_run_id=int(run.id),
        )
    )
    hot_predictions = physical_predictions
    prediction_result = compare_compact_prediction_rows(
        cold_predictions,
        hot_predictions,
        full_trade_days=int(contract.get("hot_full_trade_days") or 5),
        top_k=int(contract.get("hot_top_k") or 100),
    )
    details = _compare_rows(
        _parquet_rows(manifest_path, DETAIL_FILE, detail_columns),
        physical_details,
        detail_columns,
    )
    frozen_holding_ids = contract.get("hot_explanation_holding_symbol_ids")
    if isinstance(frozen_holding_ids, list):
        holding_symbol_ids = {int(item) for item in frozen_holding_ids}
        holding_id_source = "manifest"
    else:
        holding_symbol_ids = _current_holding_symbol_ids(
            db,
            market=str(run.market or ""),
        )
        holding_id_source = "current_portfolio_compatibility_fallback"
    cold_explanations = _parquet_rows(
        manifest_path,
        EXPLANATION_FILE,
        explanation_columns,
    )
    explanations = compare_bounded_explanation_rows(
        cold_predictions,
        cold_explanations,
        physical_explanations,
        top_k=int(contract.get("hot_explanation_limit") or 50),
        boundary_radius=int(contract.get("hot_explanation_boundary_radius") or 0),
        holding_symbol_ids=holding_symbol_ids,
    )
    explanations["holding_symbol_ids"] = sorted(holding_symbol_ids)
    explanations["holding_id_source"] = holding_id_source
    legacy_hot_dual_write = bool(contract.get("legacy_hot_dual_write", True))
    legacy_comparisons = None
    legacy_exact_match = True
    if legacy_hot_dual_write:
        legacy_comparisons = {
            "predictions": _compare_rows(
                physical_predictions,
                _load_prediction_rows(db, int(run.id)),
                prediction_columns,
            ),
            "prediction_details": _compare_rows(
                physical_details,
                _load_detail_rows(db, int(run.id)),
                detail_columns,
            ),
            "prediction_explanations": _compare_rows(
                physical_explanations,
                _load_explanation_rows(db, int(run.id)),
                explanation_columns,
            ),
        }
        legacy_exact_match = all(
            item["exact_match"] for item in legacy_comparisons.values()
        )
    evaluation = db.scalar(
        select(ModelEvaluation)
        .where(
            ModelEvaluation.model_run_id == int(run.id),
            ModelEvaluation.status.in_(("success", "partial")),
        )
        .order_by(ModelEvaluation.id.desc())
        .limit(1)
    )
    evaluation_metric_count = 0
    if evaluation is not None:
        evaluation_metric_count = int(
            db.scalar(
                select(func.count(ModelEvaluationMetric.id)).where(
                    ModelEvaluationMetric.model_evaluation_id == int(evaluation.id)
                )
            )
            or 0
        )
    publication_runtime = audit_publication_timing(run.config_json)
    storage_passed = (
        prediction_result["status"] == "pass"
        and details["exact_match"]
        and explanations["status"] == "pass"
        and legacy_exact_match
    )
    passed = (
        storage_passed
        and evaluation is not None
        and publication_runtime["status"] == "pass"
    )
    failed = not storage_passed or publication_runtime["status"] == "fail"
    status = "pass" if passed else ("fail" if failed else "pending")
    return {
        "status": status,
        "model_run_id": int(run.id),
        "model_type": run.model_type,
        "market": run.market,
        "finished_at": run.finished_at,
        "artifact_path": str(manifest_path),
        "manifest_sha256": artifact.manifest_sha256,
        "first_trade_date": trade_dates[0] if trade_dates else None,
        "latest_trade_date": trade_dates[-1] if trade_dates else None,
        "storage_contract": contract,
        "integrity_status": integrity["status"],
        "hot_source_layer": "physical_market_tables",
        "legacy_hot_dual_write": legacy_hot_dual_write,
        "legacy_hot_comparisons": legacy_comparisons,
        "predictions": prediction_result,
        "prediction_details": details,
        "prediction_explanations": explanations,
        "publication_runtime": publication_runtime,
        "evaluation": {
            "present": evaluation is not None,
            "evaluation_id": int(evaluation.id) if evaluation is not None else None,
            "status": evaluation.status if evaluation is not None else None,
            "metric_count": evaluation_metric_count,
            "activation_status": evaluation.activation_status if evaluation is not None else None,
        },
    }


def summarize_production_sequence(
    audited: list[dict],
    *,
    required_runs: int,
    market: str = "CN",
) -> dict:
    required = max(1, int(required_runs))
    latest_by_trade_date: dict[str, dict] = {}
    duplicate_trade_date_runs: list[int] = []
    missing_trade_date_runs: list[int] = []
    # Caller supplies newest model runs first. Only the newest audited run for
    # one trading date can count toward the production sequence.
    for item in audited:
        trade_date = str(item.get("latest_trade_date") or "")[:10]
        if not trade_date:
            missing_trade_date_runs.append(int(item["model_run_id"]))
            continue
        if trade_date in latest_by_trade_date:
            duplicate_trade_date_runs.append(int(item["model_run_id"]))
            continue
        latest_by_trade_date[trade_date] = item
    selected_dates = sorted(latest_by_trade_date, reverse=True)[:required]
    selected = [latest_by_trade_date[value] for value in selected_dates]
    chronological_dates = sorted(selected_dates)
    consecutive = len(chronological_dates) == required and all(
        next_market_open_date(market, left, include_self=False) == right
        for left, right in zip(chronological_dates, chronological_dates[1:])
    )
    failed = [int(item["model_run_id"]) for item in selected if item["status"] == "fail"]
    passed = [int(item["model_run_id"]) for item in selected if item["status"] == "pass"]
    pending = [int(item["model_run_id"]) for item in selected if item["status"] == "pending"]
    status = (
        "fail"
        if failed
        else "pass"
        if len(passed) == required and consecutive
        else "pending"
    )
    return {
        "status": status,
        "passed_runs": passed,
        "failed_runs": failed,
        "pending_runs": pending,
        "sequence_runs": [int(item["model_run_id"]) for item in selected],
        "sequence_trade_dates": chronological_dates,
        "consecutive_trade_dates": consecutive,
        "duplicate_trade_date_runs": duplicate_trade_date_runs,
        "missing_trade_date_runs": missing_trade_date_runs,
        "remaining_runs": max(0, required - len(passed)),
    }


def audit_recent_compact_dual_writes(
    db: Session,
    *,
    market: str = "CN",
    required_runs: int = 5,
    scan_limit: int = 50,
) -> dict:
    normalized_market = str(market).strip().upper()
    if normalized_market not in {"CN", "HK", "US"}:
        raise ValueError(f"Unsupported dual-write acceptance market: {market!r}.")
    rows = db.execute(
        select(PredictionArtifact, ModelRun)
        .join(ModelRun, ModelRun.id == PredictionArtifact.model_run_id)
        .where(
            PredictionArtifact.status == "verified",
            PredictionArtifact.market == normalized_market,
            ModelRun.status == "success",
            ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
        )
        .order_by(ModelRun.id.desc())
        .limit(max(1, int(scan_limit)))
    ).all()

    audited: list[dict] = []
    distinct_trade_dates: set[str] = set()
    for artifact, run in rows:
        result = audit_compact_model_run(db, artifact=artifact, run=run)
        if result["status"] != "excluded":
            audited.append(result)
            if result.get("latest_trade_date"):
                distinct_trade_dates.add(str(result["latest_trade_date"])[:10])
        if len(distinct_trade_dates) >= max(1, int(required_runs)):
            break
    required = max(1, int(required_runs))
    sequence = summarize_production_sequence(
        audited,
        required_runs=required,
        market=normalized_market,
    )
    return {
        "audit_version": "prediction-dual-write-v2",
        "generated_at": app_now_iso(),
        "status": sequence["status"],
        "market": normalized_market,
        "required_runs": required,
        "audited_runs": len(audited),
        **{key: value for key, value in sequence.items() if key != "status"},
        "runs": audited,
    }
