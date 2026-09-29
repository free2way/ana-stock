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
from app.services.postgres_backup_restore import (  # noqa: E402
    create_postgres_logical_backup,
    inspect_postgres_logical_backup,
)


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
    parser = argparse.ArgumentParser(description="Create a full PostgreSQL custom-format acceptance backup.")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--compression-level", type=int, default=6)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--inspect-existing",
        action="store_true",
        help="Verify an existing archive without modifying it; useful if receipt publication was interrupted.",
    )
    args = parser.parse_args()
    settings = get_settings()
    with SessionLocal() as db:
        if args.inspect_existing:
            result = inspect_postgres_logical_backup(db, destination=args.destination)
        else:
            result = create_postgres_logical_backup(
                db,
                database_url=settings.resolved_database_url,
                destination=args.destination,
                compression_level=args.compression_level,
            )
    _write_new_json(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
