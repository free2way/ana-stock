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

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.services.storage_capacity import (  # noqa: E402
    capture_storage_capacity,
    storage_capacity_report,
)
from app.services.time_utils import app_now_iso  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture and audit PostgreSQL daily capacity growth."
    )
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    init_db()
    with SessionLocal() as db:
        captured = capture_storage_capacity(db) if args.capture else None
        result = {
            "audit_version": "storage-capacity-growth-v1",
            "generated_at": app_now_iso(),
            "capture": captured,
            **storage_capacity_report(db),
        }
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.receipt is not None:
        receipt_path = args.receipt.resolve()
        if receipt_path.exists():
            raise FileExistsError(f"Receipt already exists: {receipt_path}")
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = receipt_path.with_name(
            f".{receipt_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary_path.write_text(serialized + "\n", encoding="utf-8")
            os.replace(temporary_path, receipt_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    print(serialized)


if __name__ == "__main__":
    main()
