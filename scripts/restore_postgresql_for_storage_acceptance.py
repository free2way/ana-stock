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

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.services.postgres_backup_restore import restore_postgres_logical_backup  # noqa: E402


def _write_new_json(path: Path, payload: dict) -> None:
    target = path.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a frozen PostgreSQL backup to an isolated database.")
    parser.add_argument("--backup-receipt", type=Path, required=True)
    parser.add_argument("--restore-database", required=True)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    backup_receipt = json.loads(args.backup_receipt.resolve().read_text(encoding="utf-8"))
    if backup_receipt.get("status") != "pass":
        raise RuntimeError("Backup receipt must have pass status.")
    settings = get_settings()
    with SessionLocal() as db:
        result = restore_postgres_logical_backup(
            db,
            database_url=settings.resolved_database_url,
            backup_path=Path(backup_receipt["backup_path"]),
            backup_sha256=str(backup_receipt["backup_sha256"]),
            expected_counts=dict(backup_receipt["critical_table_counts"]),
            restore_database=args.restore_database,
            jobs=args.jobs,
        )
    _write_new_json(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
