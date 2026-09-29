from __future__ import annotations

from datetime import datetime, timedelta

from app.services.time_utils import app_now, parse_app_datetime


SUCCESS_STATUSES = {"success", "partial", "empty", "not_configured"}
FAILURE_STATUSES = {"failed", "failed_timeout", "error", "cancelled"}
ACTIVE_STATUSES = {"running", "queued", "pending", "waiting"}


def select_job_retention(
    rows: list[dict],
    *,
    protected_dependency_ids: set[int] | None = None,
    now: datetime | None = None,
    successful_days: int = 90,
    failed_days: int = 180,
) -> dict:
    current_time = now or app_now()
    protected = {int(item) for item in (protected_dependency_ids or set())}
    candidate_ids: list[int] = []
    retained_reasons: dict[int, str] = {}
    for row in rows:
        job_id = int(row["id"])
        status = str(row.get("status") or "").strip().lower()
        if status in ACTIVE_STATUSES:
            retained_reasons[job_id] = "active"
            continue
        if job_id in protected:
            retained_reasons[job_id] = "active_dependency"
            continue
        completed_at = parse_app_datetime(
            str(row.get("finished_at") or row.get("started_at") or "")
        )
        if completed_at is None:
            retained_reasons[job_id] = "invalid_timestamp_protected"
            continue
        retention_days = successful_days if status in SUCCESS_STATUSES else failed_days
        cutoff = current_time - timedelta(days=max(1, int(retention_days)))
        if completed_at < cutoff:
            candidate_ids.append(job_id)
        else:
            retained_reasons[job_id] = (
                "success_90d" if status in SUCCESS_STATUSES else "failure_or_other_180d"
            )
    candidate_ids.sort()
    return {
        "status": "dry_run",
        "input_rows": len(rows),
        "candidate_delete_rows": len(candidate_ids),
        "candidate_delete_ids": candidate_ids,
        "retained_rows": len(rows) - len(candidate_ids),
        "retained_reasons": retained_reasons,
        "policy": {
            "successful_days": max(1, int(successful_days)),
            "failed_days": max(1, int(failed_days)),
            "active_jobs": "always_keep",
            "active_dependencies": "always_keep",
        },
    }
