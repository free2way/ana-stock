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

from app.services.storage_retention import (  # noqa: E402
    retention_apply_token,
    validate_retention_acceptance_gate,
)
from app.services.time_utils import app_now_iso  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only validation of the destructive retention gate.")
    parser.add_argument("--market", choices=("CN", "US"), default="CN")
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--backup-receipt", type=Path, required=True)
    parser.add_argument("--restore-receipt", type=Path, required=True)
    parser.add_argument("--dual-write-receipt", type=Path, required=True)
    parser.add_argument("--cutover-receipt", type=Path, required=True)
    parser.add_argument("--physical-only-receipt", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        gate = validate_retention_acceptance_gate(
            markets=[args.market],
            approval_token=retention_apply_token(args.market),
            evidence_manifest_path=args.evidence_manifest,
            backup_receipt_path=args.backup_receipt,
            restore_receipt_path=args.restore_receipt,
            dual_write_receipt_path=args.dual_write_receipt,
            cutover_receipt_path=args.cutover_receipt,
            physical_only_receipt_path=args.physical_only_receipt,
        )
        result = {
            "gate_version": "storage-retention-acceptance-v2",
            "generated_at": app_now_iso(),
            "status": "pass",
            "gate": gate,
            "database_connected": False,
            "database_mutated": False,
        }
    except RuntimeError as exc:
        result = {
            "gate_version": "storage-retention-acceptance-v2",
            "generated_at": app_now_iso(),
            "status": "blocked",
            "reason": str(exc),
            "database_connected": False,
            "database_mutated": False,
        }
    target = args.receipt.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
