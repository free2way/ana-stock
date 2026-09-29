from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import uuid


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings  # noqa: E402
from app.services.prediction_artifacts import (  # noqa: E402
    PredictionArtifactWriter,
    PredictionPublicationLimitError,
    select_hot_explanation_rows,
)
from app.services.time_utils import app_now_iso  # noqa: E402


def _prediction_rows() -> list[dict]:
    return [
        {
            "symbol_id": symbol_id,
            "trade_date": "2026-08-22",
            "score": 1.0 / rank,
            "rank_value": float(rank),
        }
        for symbol_id, rank in (
            (1, 1),
            (2, 50),
            (3, 51),
            (4, 55),
            (5, 56),
            (6, 100),
        )
    ]


def audit_prediction_publication_guard() -> dict:
    settings = get_settings()
    rows = _prediction_rows()
    details = [
        {"symbol_id": 1, "trade_date": "2026-08-22", "confidence": 0.8}
    ]
    explanations = [
        {
            "symbol_id": row["symbol_id"],
            "trade_date": row["trade_date"],
            "feature_name": "momentum_20d",
            "display_order": 1,
        }
        for row in rows
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        staging_writer = PredictionArtifactWriter(
            root=root / "staging",
            staging_threshold_rows=2,
            max_rows=20,
            max_estimated_bytes=10_000_000,
        )
        staging_plan = staging_writer.plan(
            prediction_rows=rows[:2],
            detail_rows=details,
            explanation_rows=[],
        )

        blocked_plan: dict = {}
        blocked_before_staging = False
        blocked_root = root / "blocked"
        try:
            PredictionArtifactWriter(
                root=blocked_root,
                staging_threshold_rows=2,
                max_rows=2,
                max_estimated_bytes=10_000_000,
            ).write(
                model_run_id=900_001,
                market="CN",
                prediction_rows=rows[:2],
                detail_rows=details,
            )
        except PredictionPublicationLimitError as exc:
            blocked_plan = exc.plan
            blocked_before_staging = not blocked_root.exists()

        cleanup_root = root / "cleanup"
        cleanup_writer = PredictionArtifactWriter(
            root=cleanup_root,
            staging_threshold_rows=1,
            max_rows=20,
            max_estimated_bytes=10_000_000,
        )
        failure_observed = False
        with patch(
            "app.services.prediction_artifacts._write_parquet",
            side_effect=RuntimeError("acceptance fault injection"),
        ):
            try:
                cleanup_writer.write(
                    model_run_id=900_002,
                    market="CN",
                    prediction_rows=rows[:2],
                )
            except RuntimeError:
                failure_observed = True
        staging_cleaned = (
            failure_observed
            and not (cleanup_root / "model_run_id=900002").exists()
            and not list(cleanup_root.glob(".model_run_id=900002.*.tmp"))
        )

    selected = select_hot_explanation_rows(
        explanations,
        prediction_rows=rows,
        top_k=50,
        holding_symbol_ids={6},
        boundary_radius=5,
    )
    selected_symbol_ids = sorted({int(row["symbol_id"]) for row in selected})
    checks = {
        "production_threshold_is_one_million": int(
            settings.prediction_publication_staging_threshold_rows
        )
        == 1_000_000,
        "production_max_rows_exceeds_threshold": int(
            settings.prediction_publication_max_rows
        )
        >= int(settings.prediction_publication_staging_threshold_rows),
        "production_byte_limit_positive": int(
            settings.prediction_publication_max_estimated_bytes
        )
        > 0,
        "over_threshold_enters_staging": staging_plan.get("mode")
        == "staging_atomic_publish"
        and staging_plan.get("staging_required") is True,
        "plan_records_rows_and_bytes": staging_plan.get("total_rows") == 3
        and int(staging_plan.get("estimated_bytes") or 0) > 0,
        "over_limit_fails_before_staging": blocked_plan.get("status") == "blocked"
        and bool(blocked_plan.get("violations"))
        and blocked_before_staging,
        "failed_staging_is_cleaned": staging_cleaned,
        "explanations_are_bounded_to_top_boundary_holdings": selected_symbol_ids
        == [1, 2, 3, 4, 6],
        "cold_scope_retains_non_materialized_explanations": len(explanations)
        > len(selected)
        and 5 not in selected_symbol_ids,
    }
    return {
        "audit_version": "prediction-publication-guard-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if all(checks.values()) else "failed",
        "production_limits": {
            "staging_threshold_rows": int(
                settings.prediction_publication_staging_threshold_rows
            ),
            "max_rows": int(settings.prediction_publication_max_rows),
            "max_estimated_bytes": int(
                settings.prediction_publication_max_estimated_bytes
            ),
        },
        "staging_plan": staging_plan,
        "blocked_plan": blocked_plan,
        "selected_explanation_symbol_ids": selected_symbol_ids,
        "cold_explanation_row_count": len(explanations),
        "hot_explanation_row_count": len(selected),
        "checks": checks,
    }


def _write_new_json(path: Path, payload: dict) -> None:
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
        description="Audit A0-03 publication guards and A0-02 explanation selection."
    )
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    result = audit_prediction_publication_guard()
    if args.receipt is not None:
        _write_new_json(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
