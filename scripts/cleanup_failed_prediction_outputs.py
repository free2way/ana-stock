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
from app.services.storage_retention import clean_failed_prediction_outputs  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean one bounded failed prediction-output batch.")
    parser.add_argument("--markets", default="CN,US")
    parser.add_argument("--max-runs", type=int, default=1)
    parser.add_argument("--model-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--evidence-manifest", type=Path)
    parser.add_argument("--backup-receipt", type=Path)
    parser.add_argument("--restore-receipt", type=Path)
    parser.add_argument("--isolation-receipt", type=Path)
    parser.add_argument("--hk-archive-receipt", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    markets = [item.strip().upper() for item in args.markets.split(",") if item.strip()]
    with SessionLocal() as db:
        result = clean_failed_prediction_outputs(
            db,
            markets=markets,
            max_runs=args.max_runs,
            model_run_id=args.model_run_id,
            apply=args.apply,
            approval_token=args.approval_token,
            evidence_manifest_path=args.evidence_manifest,
            backup_receipt_path=args.backup_receipt,
            restore_receipt_path=args.restore_receipt,
            isolation_receipt_path=args.isolation_receipt,
            hk_archive_receipt_path=args.hk_archive_receipt,
        )
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    target = args.receipt.resolve()
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(serialized)


if __name__ == "__main__":
    main()
