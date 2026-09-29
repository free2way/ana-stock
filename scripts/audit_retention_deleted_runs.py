from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.services.prediction_artifacts import verify_prediction_artifact  # noqa: E402
from app.services.storage_retention import _related_row_counts  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402


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
        description="Verify cold artifacts and zero PostgreSQL output rows for explicit runs."
    )
    parser.add_argument("--run-ids", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    run_ids = sorted(
        {
            int(value.strip())
            for value in args.run_ids.split(",")
            if value.strip()
        }
    )
    if not run_ids:
        raise ValueError("At least one run id is required.")
    with SessionLocal() as db:
        rows = [
            dict(row)
            for row in db.execute(
                text(
                    "SELECT mr.id, mr.market, mr.status, pa.status AS artifact_status, "
                    "pa.artifact_path FROM model_runs mr "
                    "LEFT JOIN prediction_artifacts pa ON pa.model_run_id = mr.id "
                    "WHERE mr.id = ANY(CAST(:ids AS INTEGER[])) ORDER BY mr.id"
                ),
                {"ids": run_ids},
            ).mappings()
        ]
        row_counts = _related_row_counts(db, run_ids)
    artifacts = []
    for row in rows:
        verification = (
            verify_prediction_artifact(row["artifact_path"])
            if row.get("artifact_path")
            else {"status": "missing"}
        )
        artifacts.append(
            {
                "model_run_id": int(row["id"]),
                "market": row.get("market"),
                "model_run_status": row.get("status"),
                "artifact_status": row.get("artifact_status"),
                "artifact_path": row.get("artifact_path"),
                "verification_status": verification.get("status"),
            }
        )
    all_artifacts_verified = (
        len(rows) == len(run_ids)
        and all(
            row.get("artifact_status") == "verified"
            and row.get("verification_status") == "success"
            for row in artifacts
        )
    )
    all_output_rows_zero = not any(int(value or 0) for value in row_counts.values())
    payload = {
        "audit_version": "retention-deleted-runs-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if all_artifacts_verified and all_output_rows_zero else "fail",
        "run_ids": run_ids,
        "artifacts": artifacts,
        "row_counts": row_counts,
        "all_artifacts_verified": all_artifacts_verified,
        "all_output_rows_zero": all_output_rows_zero,
        "database_mutated": False,
    }
    _write_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if payload["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
