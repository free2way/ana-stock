from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.services.physical_storage_cutover import (  # noqa: E402
    activate_market_physical_only,
    cutover_apply_token,
    rollback_market_physical_only,
    validate_market_physical_cutover_gate,
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
        description="Validate, activate, or roll back market physical-only fact writes."
    )
    parser.add_argument("--market", choices=("CN", "HK", "US"), default="CN")
    parser.add_argument("--action", choices=("check", "activate", "rollback"), default="check")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--evidence-manifest", type=Path)
    parser.add_argument("--backup-receipt", type=Path)
    parser.add_argument("--restore-receipt", type=Path)
    parser.add_argument("--dual-write-receipt", type=Path)
    parser.add_argument("--live-audit-receipt", type=Path)
    parser.add_argument("--hot-audit-receipt", type=Path)
    parser.add_argument("--snapshot-audit-receipt", type=Path)
    parser.add_argument("--isolation-audit-receipt", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    database_connected = False
    database_mutated = False
    try:
        if args.action == "rollback":
            if not args.apply:
                raise RuntimeError("Rollback requires --apply.")
            with SessionLocal() as db:
                database_connected = True
                result = rollback_market_physical_only(
                    db,
                    market=args.market,
                    approval_token=args.approval_token,
                )
                database_mutated = True
            status = "success"
        else:
            required = {
                "evidence_manifest": args.evidence_manifest,
                "backup_receipt": args.backup_receipt,
                "restore_receipt": args.restore_receipt,
                "dual_write_receipt": args.dual_write_receipt,
                "live_audit_receipt": args.live_audit_receipt,
                "hot_audit_receipt": args.hot_audit_receipt,
                "snapshot_audit_receipt": args.snapshot_audit_receipt,
                "isolation_audit_receipt": args.isolation_audit_receipt,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise RuntimeError(
                    f"{args.market} physical cutover is missing evidence arguments: "
                    + ", ".join(missing)
                )
            gate = validate_market_physical_cutover_gate(
                market=args.market,
                approval_token=args.approval_token or cutover_apply_token(args.market),
                evidence_manifest_path=args.evidence_manifest,
                backup_receipt_path=args.backup_receipt,
                restore_receipt_path=args.restore_receipt,
                dual_write_receipt_path=args.dual_write_receipt,
                live_audit_receipt_path=args.live_audit_receipt,
                hot_audit_receipt_path=args.hot_audit_receipt,
                snapshot_audit_receipt_path=args.snapshot_audit_receipt,
                isolation_audit_receipt_path=args.isolation_audit_receipt,
            )
            result = {"gate": gate, "action": args.action}
            status = "pass"
            if args.action == "activate":
                if not args.apply:
                    raise RuntimeError("Activation requires --apply.")
                with SessionLocal() as db:
                    database_connected = True
                    result["activation"] = activate_market_physical_only(
                        db,
                        market=args.market,
                        gate=gate,
                        approval_token=args.approval_token,
                    )
                    database_mutated = True
                status = "success"
        payload = {
            "cutover_version": "market-physical-storage-cutover-v2",
            "generated_at": app_now_iso(),
            "status": status,
            **result,
            "database_connected": database_connected,
            "database_mutated": database_mutated,
        }
    except RuntimeError as exc:
        payload = {
            "cutover_version": "market-physical-storage-cutover-v2",
            "generated_at": app_now_iso(),
            "status": "blocked",
            "action": args.action,
            "reason": str(exc),
            "database_connected": database_connected,
            "database_mutated": database_mutated,
        }
    _write_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
