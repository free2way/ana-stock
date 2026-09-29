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
from app.services.job_retention import select_job_retention  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit status-aware job retention without deleting rows."
    )
    parser.add_argument("--successful-days", type=int, default=90)
    parser.add_argument("--failed-days", type=int, default=180)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    with SessionLocal() as db:
        rows = [
            dict(row)
            for row in db.execute(
                text(
                    "SELECT id, job_type, status, started_at, finished_at, "
                    "octet_length(params_json) AS params_bytes "
                    "FROM data_jobs ORDER BY id"
                )
            ).mappings()
        ]
        protected_dependency_ids = {
            int(value)
            for value in db.execute(
                text(
                    "SELECT DISTINCT d.upstream_job_id "
                    "FROM job_run_dependencies d "
                    "JOIN data_jobs j ON j.id = d.job_id "
                    "WHERE lower(j.status) IN ('running','queued','pending','waiting')"
                )
            ).scalars()
        }
        result = select_job_retention(
            rows,
            protected_dependency_ids=protected_dependency_ids,
            successful_days=max(1, int(args.successful_days)),
            failed_days=max(1, int(args.failed_days)),
        )
        candidate_ids = set(result["candidate_delete_ids"])
        candidate_bytes = sum(
            int(row.get("params_bytes") or 0)
            for row in rows
            if int(row["id"]) in candidate_ids
        )
        candidate_digest = hashlib.sha256(
            ",".join(str(item) for item in result["candidate_delete_ids"]).encode(
                "utf-8"
            )
        ).hexdigest()
        active_candidates = [
            int(row["id"])
            for row in rows
            if int(row["id"]) in candidate_ids
            and str(row.get("status") or "").lower()
            in {"running", "queued", "pending", "waiting"}
        ]
        dependency_candidates = sorted(candidate_ids & protected_dependency_ids)
        result.update(
            {
                "generated_at": app_now_iso(),
                "mode": "read_only",
                "database": str(db.scalar(text("select current_database()")) or ""),
                "total_params_bytes": sum(
                    int(row.get("params_bytes") or 0) for row in rows
                ),
                "oversized_inline_rows": sum(
                    1 for row in rows if int(row.get("params_bytes") or 0) > 64 * 1024
                ),
                "candidate_params_bytes": candidate_bytes,
                "candidate_ids_sha256": candidate_digest,
                "protected_dependency_ids": sorted(protected_dependency_ids),
                "active_candidate_ids": active_candidates,
                "protected_dependency_candidate_ids": dependency_candidates,
                "safety_checks": {
                    "active_jobs_not_candidates": not active_candidates,
                    "active_dependencies_not_candidates": not dependency_candidates,
                },
            }
        )
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.receipt is not None:
        target = args.receipt.resolve()
        if target.exists():
            raise FileExistsError(f"Receipt already exists: {target}")
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
