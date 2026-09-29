from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime


PROTECTED_EVIDENCE_PREFIXES = (
    "stock_selection_shadow_daily:",
    "stock_selection_shadow_evaluation:",
    "stock_selection_final_decisions:",
)


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def select_workspace_snapshot_retention(
    rows: list[dict],
    *,
    today: date | None = None,
    minimum_latest_per_type: int = 1,
    daily_days: int = 30,
    weekly_days: int = 180,
) -> dict:
    """Select calendar-tiered retention without mutating PostgreSQL."""

    current_date = today or datetime.now().date()
    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_type[str(row.get("snapshot_type") or "unknown")].append(row)

    kept_ids: set[int] = set()
    reasons: dict[int, str] = {}
    for snapshot_type, values in by_type.items():
        if snapshot_type.startswith(PROTECTED_EVIDENCE_PREFIXES):
            for row in values:
                kept_ids.add(int(row["id"]))
                reasons[int(row["id"])] = "forward_decision_evidence_protected"
            continue
        ordered = sorted(values, key=lambda row: int(row["id"]), reverse=True)
        for row in ordered[: max(1, int(minimum_latest_per_type))]:
            kept_ids.add(int(row["id"]))
            reasons[int(row["id"])] = "latest_floor"

        buckets: dict[tuple, dict] = {}
        for row in ordered:
            snapshot_date = _parse_date(row.get("snapshot_date"))
            if snapshot_date is None:
                kept_ids.add(int(row["id"]))
                reasons[int(row["id"])] = "invalid_date_protected"
                continue
            age_days = max(0, (current_date - snapshot_date).days)
            if age_days < max(1, int(daily_days)):
                bucket = ("daily", snapshot_date.isoformat())
                reason = "daily_30d"
            elif age_days < max(int(daily_days) + 1, int(weekly_days)):
                iso_year, iso_week, _ = snapshot_date.isocalendar()
                bucket = ("weekly", iso_year, iso_week)
                reason = "weekly_30_180d"
            else:
                bucket = ("monthly", snapshot_date.year, snapshot_date.month)
                reason = "monthly_180d_plus"
            existing = buckets.get(bucket)
            if existing is None or int(row["id"]) > int(existing["id"]):
                buckets[bucket] = row
                reasons[int(row["id"])] = reason
        kept_ids.update(int(row["id"]) for row in buckets.values())

    all_ids = {int(row["id"]) for row in rows}
    delete_ids = sorted(all_ids - kept_ids)
    return {
        "status": "dry_run",
        "input_rows": len(rows),
        "snapshot_types": len(by_type),
        "keep_rows": len(kept_ids),
        "candidate_delete_rows": len(delete_ids),
        "keep_ids": sorted(kept_ids),
        "candidate_delete_ids": delete_ids,
        "keep_reasons": reasons,
        "policy": {
            "minimum_latest_per_type": max(1, int(minimum_latest_per_type)),
            "daily_days": max(1, int(daily_days)),
            "weekly_days": max(int(daily_days) + 1, int(weekly_days)),
            "older_tier": "monthly",
            "protected_evidence_prefixes": list(PROTECTED_EVIDENCE_PREFIXES),
        },
    }
