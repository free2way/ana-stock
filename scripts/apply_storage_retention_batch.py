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
from app.services.storage_retention import clean_model_history  # noqa: E402
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
            "Preview one bounded market retention batch, or apply it only after all "
            "post-cutover acceptance evidence passes."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--user-authorized-override",
        action="store_true",
        help=(
            "Bypass acceptance timing/cutover gates after explicit user authorization. "
            "A registered separate-device backup and verified cold artifacts remain mandatory."
        ),
    )
    parser.add_argument("--market", choices=("CN", "US"), default="CN")
    parser.add_argument("--approval-token")
    parser.add_argument("--evidence-manifest", type=Path)
    parser.add_argument("--backup-receipt", type=Path)
    parser.add_argument("--restore-receipt", type=Path)
    parser.add_argument("--dual-write-receipt", type=Path)
    parser.add_argument("--cutover-receipt", type=Path)
    parser.add_argument("--physical-only-receipt", type=Path)
    parser.add_argument("--keep-runs", type=int, default=20)
    parser.add_argument("--keep-workspace-snapshots", type=int, default=10)
    parser.add_argument("--max-success-runs", type=int, default=5)
    parser.add_argument("--max-failed-runs", type=int, default=20)
    parser.add_argument("--include-workspace-snapshots", action="store_true")
    parser.add_argument("--max-workspace-snapshots", type=int, default=100)
    parser.add_argument(
        "--include-full-candidate-counts",
        action="store_true",
        help=(
            "Run the expensive all-candidate row scan. By default only the "
            "bounded batch is counted so runtime does not grow with history."
        ),
    )
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    database_connected = False
    database_mutated = False
    started = time.perf_counter()
    try:
        if args.apply:
            required = (
                {
                    "approval_token": args.approval_token,
                    "evidence_manifest": args.evidence_manifest,
                    "backup_receipt": args.backup_receipt,
                }
                if args.user_authorized_override
                else {
                    "approval_token": args.approval_token,
                    "evidence_manifest": args.evidence_manifest,
                    "backup_receipt": args.backup_receipt,
                    "restore_receipt": args.restore_receipt,
                    "dual_write_receipt": args.dual_write_receipt,
                    "cutover_receipt": args.cutover_receipt,
                    "physical_only_receipt": args.physical_only_receipt,
                }
            )
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise RuntimeError(
                    "Retention apply is missing required arguments: " + ", ".join(missing)
                )
        with SessionLocal() as db:
            database_connected = True
            result = clean_model_history(
                db,
                keep_model_runs_per_market=args.keep_runs,
                keep_workspace_snapshots_per_type=args.keep_workspace_snapshots,
                markets=[args.market],
                purge_failed_outputs=True,
                purge_workspace_snapshots=args.include_workspace_snapshots,
                max_verified_success_runs_per_batch=args.max_success_runs,
                max_failed_runs_per_batch=args.max_failed_runs,
                max_workspace_snapshots_per_batch=args.max_workspace_snapshots,
                include_full_candidate_row_counts=args.include_full_candidate_counts,
                apply=args.apply,
                approval_token=args.approval_token,
                evidence_manifest_path=args.evidence_manifest,
                backup_receipt_path=args.backup_receipt,
                restore_receipt_path=args.restore_receipt,
                dual_write_receipt_path=args.dual_write_receipt,
                cutover_receipt_path=args.cutover_receipt,
                physical_only_receipt_path=args.physical_only_receipt,
                user_authorized_override=args.user_authorized_override,
            )
        database_mutated = bool(
            args.apply
            and (
                int(result.get("batch_model_runs") or 0) > 0
                or int(result.get("batch_workspace_snapshots") or 0) > 0
            )
        )
        payload = {
            "retention_batch_version": "storage-retention-batch-v1",
            "generated_at": app_now_iso(),
            "status": "success",
            "action": "apply" if args.apply else "dry_run",
            "database_connected": database_connected,
            "database_mutated": database_mutated,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "result": result,
        }
    except RuntimeError as exc:
        payload = {
            "retention_batch_version": "storage-retention-batch-v1",
            "generated_at": app_now_iso(),
            "status": "blocked",
            "action": "apply" if args.apply else "dry_run",
            "reason": str(exc),
            "database_connected": database_connected,
            "database_mutated": database_mutated,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
    _write_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if payload["status"] != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
