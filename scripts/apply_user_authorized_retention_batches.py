from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.services.storage_retention import (  # noqa: E402
    clean_model_history,
    validate_user_authorized_retention_override_gate,
)
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
        description=(
            "Apply multiple independently committed user-authorized retention batches. "
            "Each selected successful run must still have a verified cold artifact."
        )
    )
    parser.add_argument("--market", choices=("CN", "US"), required=True)
    parser.add_argument("--approval-token", required=True)
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--backup-receipt", type=Path, required=True)
    parser.add_argument("--keep-runs", type=int, default=20)
    parser.add_argument("--runs-per-batch", type=int, default=10)
    parser.add_argument("--max-batches", type=int, default=20)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    database_connected = False
    completed_batches: list[dict] = []
    try:
        gate = validate_user_authorized_retention_override_gate(
            markets=[args.market],
            approval_token=args.approval_token,
            evidence_manifest_path=args.evidence_manifest,
            backup_receipt_path=args.backup_receipt,
        )
        with SessionLocal() as db:
            database_connected = True
            for batch_number in range(1, max(1, int(args.max_batches)) + 1):
                batch_started = time.perf_counter()
                result = clean_model_history(
                    db,
                    keep_model_runs_per_market=max(1, int(args.keep_runs)),
                    markets=[args.market],
                    purge_failed_outputs=True,
                    purge_workspace_snapshots=False,
                    max_verified_success_runs_per_batch=max(
                        1, int(args.runs_per_batch)
                    ),
                    max_failed_runs_per_batch=20,
                    apply=True,
                    approval_token=args.approval_token,
                    evidence_manifest_path=args.evidence_manifest,
                    backup_receipt_path=args.backup_receipt,
                    user_authorized_override=True,
                    _prevalidated_acceptance_gate=gate,
                )
                completed_batches.append(
                    {
                        "batch_number": batch_number,
                        "duration_ms": round(
                            (time.perf_counter() - batch_started) * 1000.0, 3
                        ),
                        "result": result,
                    }
                )
                print(
                    json.dumps(
                        {
                            "batch": batch_number,
                            "market": args.market,
                            "deleted_runs": result["batch_verified_success_run_ids"],
                            "remaining_runs": result[
                                "remaining_verified_success_runs_after_batch"
                            ],
                            "row_counts": result["batch_row_counts"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                if int(result["remaining_verified_success_runs_after_batch"]) == 0:
                    break
        deleted_run_ids = [
            int(run_id)
            for batch in completed_batches
            for run_id in batch["result"]["batch_verified_success_run_ids"]
        ]
        payload = {
            "retention_version": "user-authorized-storage-retention-batches-v1",
            "generated_at": app_now_iso(),
            "status": "pass",
            "market": args.market,
            "database_connected": database_connected,
            "database_mutated": bool(deleted_run_ids),
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "gate": gate,
            "keep_runs": max(1, int(args.keep_runs)),
            "runs_per_batch": max(1, int(args.runs_per_batch)),
            "completed_batch_count": len(completed_batches),
            "deleted_run_count": len(deleted_run_ids),
            "deleted_run_ids": deleted_run_ids,
            "remaining_verified_success_runs": (
                int(
                    completed_batches[-1]["result"][
                        "remaining_verified_success_runs_after_batch"
                    ]
                )
                if completed_batches
                else 0
            ),
            "batches": completed_batches,
        }
    except Exception as exc:
        payload = {
            "retention_version": "user-authorized-storage-retention-batches-v1",
            "generated_at": app_now_iso(),
            "status": "partial" if completed_batches else "blocked",
            "market": args.market,
            "reason": str(exc),
            "database_connected": database_connected,
            "database_mutated": bool(completed_batches),
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "completed_batch_count": len(completed_batches),
            "batches": completed_batches,
        }
    _write_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if payload["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
