from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
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


APPLY_TOKEN = "DELETE_REDUNDANT_WORKSPACE_SNAPSHOTS"
ADVISORY_LOCK_KEY = "workspace_snapshot_retention_v1"


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


def _load_rows(db) -> list[dict]:
    return [
        dict(row)
        for row in db.execute(
            text(
                "SELECT id, snapshot_type, snapshot_date, created_at, "
                "octet_length(payload_json) AS payload_bytes "
                "FROM workspace_snapshots ORDER BY snapshot_type, id DESC"
            )
        ).mappings()
    ]


def _database_sizes(db) -> dict[str, int]:
    row = db.execute(
        text(
            "SELECT pg_database_size(current_database()) AS database_bytes, "
            "pg_total_relation_size('workspace_snapshots') AS relation_bytes"
        )
    ).mappings().one()
    return {key: int(value or 0) for key, value in dict(row).items()}


def _candidate_digest(candidate_ids: list[int]) -> str:
    return hashlib.sha256(
        ",".join(str(item) for item in candidate_ids).encode("utf-8")
    ).hexdigest()


def _plan(rows: list[dict], *, minimum_latest_per_type: int, max_rows: int) -> dict:
    selection = select_workspace_snapshot_retention(
        rows,
        minimum_latest_per_type=max(1, int(minimum_latest_per_type)),
    )
    candidates = [int(value) for value in selection["candidate_delete_ids"]]
    batch_ids = sorted(candidates)[: max(1, int(max_rows))]
    candidate_set = set(candidates)
    batch_set = set(batch_ids)
    return {
        "input_rows": len(rows),
        "snapshot_types": int(selection["snapshot_types"]),
        "keep_rows": int(selection["keep_rows"]),
        "candidate_delete_rows": len(candidates),
        "candidate_payload_bytes": sum(
            int(row.get("payload_bytes") or 0)
            for row in rows
            if int(row["id"]) in candidate_set
        ),
        "candidate_ids_sha256": _candidate_digest(candidates),
        "batch_rows": len(batch_ids),
        "batch_payload_bytes": sum(
            int(row.get("payload_bytes") or 0)
            for row in rows
            if int(row["id"]) in batch_set
        ),
        "batch_ids": batch_ids,
        "batch_ids_sha256": _candidate_digest(batch_ids),
        "remaining_candidate_rows_after_batch": max(0, len(candidates) - len(batch_ids)),
        "policy": selection["policy"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or delete one bounded batch of redundant workspace snapshots. "
            "The latest floor and daily/weekly/monthly representatives are always retained."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--minimum-latest-per-type", type=int, default=10)
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    database_connected = False
    database_mutated = False
    db = None
    try:
        if args.apply and args.approval_token != APPLY_TOKEN:
            raise RuntimeError(
                f"Workspace snapshot deletion requires approval token {APPLY_TOKEN}."
            )
        db = SessionLocal()
        database_connected = True
        if args.apply:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": ADVISORY_LOCK_KEY})
        before_sizes = _database_sizes(db)
        rows = _load_rows(db)
        plan = _plan(
            rows,
            minimum_latest_per_type=args.minimum_latest_per_type,
            max_rows=args.max_rows,
        )
        deleted_ids: list[int] = []
        if args.apply and plan["batch_ids"]:
            deleted_ids = sorted(
                int(value)
                for value in db.execute(
                    text(
                        "DELETE FROM workspace_snapshots "
                        "WHERE id = ANY(CAST(:ids AS INTEGER[])) RETURNING id"
                    ),
                    {"ids": plan["batch_ids"]},
                ).scalars()
            )
            if deleted_ids != plan["batch_ids"]:
                db.rollback()
                raise RuntimeError(
                    "Workspace snapshot batch changed during deletion; transaction rolled back."
                )
            residual = int(
                db.execute(
                    text(
                        "SELECT COUNT(*) FROM workspace_snapshots "
                        "WHERE id = ANY(CAST(:ids AS INTEGER[]))"
                    ),
                    {"ids": plan["batch_ids"]},
                ).scalar()
                or 0
            )
            if residual:
                db.rollback()
                raise RuntimeError(
                    "Workspace snapshot deletion left residual rows; transaction rolled back."
                )
            db.commit()
            database_mutated = True
        elif args.apply:
            db.rollback()

        after_sizes = _database_sizes(db)
        remaining_rows = _load_rows(db)
        remaining_plan = _plan(
            remaining_rows,
            minimum_latest_per_type=args.minimum_latest_per_type,
            max_rows=args.max_rows,
        )
        payload = {
            "retention_version": "workspace-snapshot-retention-apply-v1",
            "generated_at": app_now_iso(),
            "status": "pass",
            "mode": "apply" if args.apply else "dry_run",
            "database_connected": database_connected,
            "database_mutated": database_mutated,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "before_sizes": before_sizes,
            "after_sizes": after_sizes,
            "plan": plan,
            "deleted_rows": len(deleted_ids),
            "deleted_ids": deleted_ids,
            "deleted_ids_sha256": _candidate_digest(deleted_ids),
            "deleted_payload_bytes": plan["batch_payload_bytes"] if deleted_ids else 0,
            "remaining": remaining_plan,
            "safety_checks": {
                "exact_delete_match": deleted_ids == plan["batch_ids"] if args.apply else True,
                "latest_floor_and_calendar_tiers_recomputed": True,
                "remaining_rows_match": len(remaining_rows)
                == len(rows) - len(deleted_ids),
            },
            "message": (
                "One bounded redundant snapshot batch was deleted and verified."
                if deleted_ids
                else "Workspace snapshot retention preview completed; no rows were deleted."
            ),
        }
    except Exception as exc:
        if db is not None:
            db.rollback()
        payload = {
            "retention_version": "workspace-snapshot-retention-apply-v1",
            "generated_at": app_now_iso(),
            "status": "blocked",
            "mode": "apply" if args.apply else "dry_run",
            "reason": str(exc),
            "database_connected": database_connected,
            "database_mutated": database_mutated,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
    finally:
        if db is not None:
            db.close()

    _write_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if payload["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
