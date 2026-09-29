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

from app.core.db import engine, init_db  # noqa: E402
from app.services.storage_maintenance import (  # noqa: E402
    AUTOVACUUM_TABLES,
    apply_storage_maintenance,
    inspect_storage_maintenance,
)
from app.services.time_utils import app_now_iso  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit/apply table-level PostgreSQL autovacuum settings."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    init_db()
    if args.apply:
        with engine.begin() as connection:
            details = apply_storage_maintenance(connection)
    else:
        with engine.connect() as connection:
            state = inspect_storage_maintenance(connection)
        missing = sorted(set(AUTOVACUUM_TABLES) - set(state))
        details = {
            "status": (
                "pass"
                if not missing and all(item["compliant"] for item in state.values())
                else "dry_run"
            ),
            "configured_table_count": len(state),
            "changed_tables": [],
            "missing_tables": missing,
            "before": state,
            "after": state,
        }
    result = {
        "audit_version": "storage-autovacuum-settings-v1",
        "generated_at": app_now_iso(),
        "applied": bool(args.apply),
        **details,
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
    if args.apply and result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
