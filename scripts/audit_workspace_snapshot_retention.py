from __future__ import annotations

import argparse
import hashlib
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
from app.services.time_utils import app_now_iso  # noqa: E402
from app.services.workspace_snapshot_retention import (  # noqa: E402
    select_workspace_snapshot_retention,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit calendar-tiered workspace snapshot retention without deleting rows."
    )
    parser.add_argument("--minimum-latest-per-type", type=int, default=10)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    with SessionLocal() as db:
        rows = [
            dict(row)
            for row in db.execute(
                text(
                    "SELECT id, snapshot_type, snapshot_date, created_at, "
                    "octet_length(payload_json) AS payload_bytes "
                    "FROM workspace_snapshots ORDER BY snapshot_type, id DESC"
                )
            ).mappings()
        ]
        result = select_workspace_snapshot_retention(
            rows,
            minimum_latest_per_type=max(1, int(args.minimum_latest_per_type)),
        )
        candidate_ids = set(result["candidate_delete_ids"])
        candidate_bytes = sum(
            int(row.get("payload_bytes") or 0)
            for row in rows
            if int(row["id"]) in candidate_ids
        )
        oversized_rows = sum(
            1 for row in rows if int(row.get("payload_bytes") or 0) > 100 * 1024
        )
        candidate_digest = hashlib.sha256(
            ",".join(str(item) for item in result["candidate_delete_ids"]).encode("utf-8")
        ).hexdigest()
        result.update(
            {
                "generated_at": app_now_iso(),
                "mode": "read_only",
                "database": str(db.scalar(text("select current_database()")) or ""),
                "total_payload_bytes": sum(int(row.get("payload_bytes") or 0) for row in rows),
                "oversized_inline_rows": oversized_rows,
                "candidate_payload_bytes": candidate_bytes,
                "candidate_ids_sha256": candidate_digest,
            }
        )
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
