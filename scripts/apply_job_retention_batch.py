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
from app.services.job_retention import select_job_retention  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402


APPLY_TOKEN = "DELETE_UNREFERENCED_EXPIRED_DATA_JOBS"
ADVISORY_LOCK_KEY = "data_job_retention_v1"
EXTERNAL_REFERENCE_TABLES = (
    ("market_refresh_batches", "source_job_id"),
    ("model_evaluations", "source_job_id"),
    ("workspace_snapshots", "source_job_id"),
)


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


def _digest(values: list[int]) -> str:
    return hashlib.sha256(
        ",".join(str(value) for value in values).encode("utf-8")
    ).hexdigest()


def _dependency_protection_closure(
    *,
    all_job_ids: set[int],
    raw_candidate_ids: set[int],
    external_reference_ids: set[int],
    dependency_pairs: list[tuple[int, int]],
) -> set[int]:
    """Protect every upstream ancestor of a retained or externally referenced job."""

    retained = (all_job_ids - raw_candidate_ids) | external_reference_ids
    changed = True
    while changed:
        changed = False
        for job_id, upstream_job_id in dependency_pairs:
            if job_id in retained and upstream_job_id not in retained:
                retained.add(upstream_job_id)
                changed = True
    return retained & raw_candidate_ids


def _load_rows(db) -> list[dict]:
    return [
        dict(row)
        for row in db.execute(
            text(
                "SELECT id, job_type, status, started_at, finished_at, "
                "octet_length(params_json) AS params_bytes "
                "FROM data_jobs ORDER BY id"
            )
        ).mappings()
    ]


def _external_reference_ids(db) -> tuple[set[int], dict[str, int]]:
    values: set[int] = set()
    counts: dict[str, int] = {}
    for table_name, column_name in EXTERNAL_REFERENCE_TABLES:
        ids = {
            int(value)
            for value in db.execute(
                text(
                    f"SELECT DISTINCT {column_name} FROM {table_name} "
                    f"WHERE {column_name} IS NOT NULL"
                )
            ).scalars()
        }
        values.update(ids)
        counts[table_name] = len(ids)
    return values, counts


def _dependency_pairs(db) -> list[tuple[int, int]]:
    return [
        (int(row["job_id"]), int(row["upstream_job_id"]))
        for row in db.execute(
            text("SELECT job_id, upstream_job_id FROM job_run_dependencies")
        ).mappings()
    ]


def _child_counts(db, job_ids: list[int]) -> dict[str, int]:
    if not job_ids:
        return {"job_run_attempts": 0, "job_run_dependencies": 0}
    params = {"ids": job_ids}
    return {
        "job_run_attempts": int(
            db.execute(
                text(
                    "SELECT COUNT(*) FROM job_run_attempts "
                    "WHERE job_id = ANY(CAST(:ids AS INTEGER[]))"
                ),
                params,
            ).scalar()
            or 0
        ),
        "job_run_dependencies": int(
            db.execute(
                text(
                    "SELECT COUNT(*) FROM job_run_dependencies "
                    "WHERE job_id = ANY(CAST(:ids AS INTEGER[])) "
                    "OR upstream_job_id = ANY(CAST(:ids AS INTEGER[]))"
                ),
                params,
            ).scalar()
            or 0
        ),
    }


def _plan(
    db,
    *,
    successful_days: int,
    failed_days: int,
    max_rows: int,
) -> dict:
    rows = _load_rows(db)
    base = select_job_retention(
        rows,
        successful_days=max(1, int(successful_days)),
        failed_days=max(1, int(failed_days)),
    )
    all_ids = {int(row["id"]) for row in rows}
    raw_candidates = {int(value) for value in base["candidate_delete_ids"]}
    external_ids, external_counts = _external_reference_ids(db)
    dependency_pairs = _dependency_pairs(db)
    dependency_protected = _dependency_protection_closure(
        all_job_ids=all_ids,
        raw_candidate_ids=raw_candidates,
        external_reference_ids=external_ids,
        dependency_pairs=dependency_pairs,
    )
    candidates = sorted(raw_candidates - dependency_protected - external_ids)
    batch_ids = candidates[: max(1, int(max_rows))]
    candidate_set = set(candidates)
    batch_set = set(batch_ids)
    external_candidate_ids = sorted(raw_candidates & external_ids)
    return {
        "input_rows": len(rows),
        "raw_policy_candidate_rows": len(raw_candidates),
        "raw_policy_candidate_ids_sha256": _digest(sorted(raw_candidates)),
        "protected_external_reference_rows": len(external_candidate_ids),
        "protected_external_reference_ids_sha256": _digest(external_candidate_ids),
        "protected_dependency_ancestor_rows": len(dependency_protected),
        "protected_dependency_ancestor_ids_sha256": _digest(sorted(dependency_protected)),
        "candidate_delete_rows": len(candidates),
        "candidate_params_bytes": sum(
            int(row.get("params_bytes") or 0)
            for row in rows
            if int(row["id"]) in candidate_set
        ),
        "candidate_ids_sha256": _digest(candidates),
        "batch_rows": len(batch_ids),
        "batch_params_bytes": sum(
            int(row.get("params_bytes") or 0)
            for row in rows
            if int(row["id"]) in batch_set
        ),
        "batch_ids": batch_ids,
        "batch_ids_sha256": _digest(batch_ids),
        "batch_child_rows": _child_counts(db, batch_ids),
        "remaining_candidate_rows_after_batch": max(0, len(candidates) - len(batch_ids)),
        "reference_counts": external_counts,
        "policy": base["policy"],
    }


def _database_sizes(db) -> dict[str, int]:
    row = db.execute(
        text(
            "SELECT pg_database_size(current_database()) AS database_bytes, "
            "pg_total_relation_size('data_jobs') AS data_jobs_bytes, "
            "pg_total_relation_size('job_run_attempts') AS attempts_bytes, "
            "pg_total_relation_size('job_run_dependencies') AS dependencies_bytes"
        )
    ).mappings().one()
    return {key: int(value or 0) for key, value in dict(row).items()}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or delete one bounded batch of unreferenced expired data jobs. "
            "Business lineage and dependency ancestors are always protected."
        )
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approval-token")
    parser.add_argument("--successful-days", type=int, default=90)
    parser.add_argument("--failed-days", type=int, default=180)
    parser.add_argument("--max-rows", type=int, default=500)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    started = time.perf_counter()
    database_connected = False
    database_mutated = False
    db = None
    try:
        if args.apply and args.approval_token != APPLY_TOKEN:
            raise RuntimeError(f"Data job deletion requires approval token {APPLY_TOKEN}.")
        db = SessionLocal()
        database_connected = True
        if args.apply:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": ADVISORY_LOCK_KEY})
        before_sizes = _database_sizes(db)
        plan = _plan(
            db,
            successful_days=args.successful_days,
            failed_days=args.failed_days,
            max_rows=args.max_rows,
        )
        deleted_ids: list[int] = []
        if args.apply and plan["batch_ids"]:
            params = {"ids": plan["batch_ids"]}
            residual_external = 0
            for table_name, column_name in EXTERNAL_REFERENCE_TABLES:
                residual_external += int(
                    db.execute(
                        text(
                            f"SELECT COUNT(*) FROM {table_name} "
                            f"WHERE {column_name} = ANY(CAST(:ids AS INTEGER[]))"
                        ),
                        params,
                    ).scalar()
                    or 0
                )
            retained_dependency_refs = int(
                db.execute(
                    text(
                        "SELECT COUNT(*) FROM job_run_dependencies "
                        "WHERE upstream_job_id = ANY(CAST(:ids AS INTEGER[])) "
                        "AND NOT (job_id = ANY(CAST(:ids AS INTEGER[])))"
                    ),
                    params,
                ).scalar()
                or 0
            )
            if residual_external or retained_dependency_refs:
                db.rollback()
                raise RuntimeError(
                    "A selected data job gained a retained reference; transaction rolled back."
                )
            db.execute(
                text(
                    "DELETE FROM job_run_dependencies "
                    "WHERE job_id = ANY(CAST(:ids AS INTEGER[])) "
                    "OR upstream_job_id = ANY(CAST(:ids AS INTEGER[]))"
                ),
                params,
            )
            db.execute(
                text(
                    "DELETE FROM job_run_attempts "
                    "WHERE job_id = ANY(CAST(:ids AS INTEGER[]))"
                ),
                params,
            )
            deleted_ids = sorted(
                int(value)
                for value in db.execute(
                    text(
                        "DELETE FROM data_jobs "
                        "WHERE id = ANY(CAST(:ids AS INTEGER[])) RETURNING id"
                    ),
                    params,
                ).scalars()
            )
            if deleted_ids != plan["batch_ids"]:
                db.rollback()
                raise RuntimeError("Data job batch changed during deletion; transaction rolled back.")
            db.commit()
            database_mutated = True
        elif args.apply:
            db.rollback()

        after_sizes = _database_sizes(db)
        remaining = _plan(
            db,
            successful_days=args.successful_days,
            failed_days=args.failed_days,
            max_rows=args.max_rows,
        )
        payload = {
            "retention_version": "data-job-retention-apply-v1",
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
            "deleted_ids_sha256": _digest(deleted_ids),
            "deleted_params_bytes": plan["batch_params_bytes"] if deleted_ids else 0,
            "remaining": remaining,
            "safety_checks": {
                "exact_delete_match": deleted_ids == plan["batch_ids"] if args.apply else True,
                "business_references_protected": True,
                "dependency_ancestor_closure_protected": True,
                "remaining_rows_match": remaining["input_rows"]
                == plan["input_rows"] - len(deleted_ids),
            },
            "message": (
                "One bounded unreferenced data-job batch was deleted and verified."
                if deleted_ids
                else "Data job retention preview completed; no rows were deleted."
            ),
        }
    except Exception as exc:
        if db is not None:
            db.rollback()
        payload = {
            "retention_version": "data-job-retention-apply-v1",
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
