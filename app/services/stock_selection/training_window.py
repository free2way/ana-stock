"""Auditable selection from already mature/purged samples; no implicit short fit."""
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date


@dataclass(frozen=True)
class TrainingWindowPolicy:
    mode: str = "complete_dates_v1"
    date_count: int = 252
    max_rows: int = 1_500_000
    max_estimated_fit_bytes: int = 2 * 1024**3

    def __post_init__(self):
        if self.mode not in {"complete_dates_v1", "legacy_row_budget_v1"}:
            raise ValueError("unsupported training window mode")
        for value in (self.date_count, self.max_rows, self.max_estimated_fit_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("training window budgets must be positive integers")


class TrainingWindowBlocked(RuntimeError):
    def __init__(self, audit):
        self.audit = audit
        super().__init__(f"Training window BLOCKED: {audit['block_reason']}; "
                         f"dates={audit['selected_date_count']}/{audit['requested_date_count']}; "
                         f"rows={audit['selected_sample_count']}/{audit['max_rows']}; "
                         f"estimated_fit_bytes={audit['estimated_fit_bytes']}/{audit['max_estimated_fit_bytes']}")


def estimate_training_fit_bytes(*, sample_count: int, feature_count: int) -> dict[str, int | str]:
    """Return the conservative planning envelope shared by runtime and audits."""
    for value, name in ((sample_count, "sample_count"), (feature_count, "feature_count")):
        if type(value) is not int or value < 0 or (name == "feature_count" and value == 0):
            raise ValueError(f"{name} must be a {'positive' if name == 'feature_count' else 'non-negative'} integer")
    dense_bytes = sample_count * feature_count * 8
    return {
        "dense_matrix_bytes": dense_bytes,
        "estimated_fit_bytes": (dense_bytes + sample_count * 32) * 4,
        "estimate_policy": "float64_matrix_plus_32_per_row_times4_v1",
    }


def select_training_window(samples: list[dict], *, policy: TrainingWindowPolicy,
                           feature_count: int) -> tuple[list[dict], dict]:
    if type(feature_count) is not int or feature_count <= 0:
        raise ValueError("feature_count must be a positive integer")
    groups = defaultdict(list)
    for sample in samples:
        value = sample["trade_date"]
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("canonical ISO feature date required")
        groups[value].append(sample)
    available = sorted(groups)
    if policy.mode == "complete_dates_v1":
        selected_dates = available[-policy.date_count:]
    else:
        selected_dates, used = [], 0
        for day in reversed(available):
            if selected_dates and used + len(groups[day]) > policy.max_rows:
                break
            selected_dates.append(day)
            used += len(groups[day])
        selected_dates.sort()
    count = sum(len(groups[day]) for day in selected_dates)
    # Explicit planning envelope, NOT a process RSS limit or measured peak:
    # float64 dense matrix, targets/weights/bookkeeping, four copies of allowance.
    estimate = estimate_training_fit_bytes(sample_count=count, feature_count=feature_count)
    estimated = int(estimate["estimated_fit_bytes"])
    audit = {**asdict(policy), "schema_version": "training_window_audit_v1",
             "requested_date_count": policy.date_count if policy.mode == "complete_dates_v1" else None,
             "available_date_count": len(available), "selected_date_count": len(selected_dates),
             "selected_sample_count": count, "selected_dates": selected_dates,
             "date_sample_counts": {day: len(groups[day]) for day in selected_dates},
             "start_date": selected_dates[0] if selected_dates else None,
             "end_date": selected_dates[-1] if selected_dates else None,
             "feature_count": feature_count, **estimate,
             "coverage_scope": "all_supplied_mature_purged_samples_in_selected_dates_not_verified_full_market",
             "status": "READY", "block_reason": None,
             "meets_252_date_research_window": len(selected_dates) >= 252}
    if not selected_dates:
        audit["block_reason"] = "empty_training_pool"
    elif policy.mode == "complete_dates_v1" and len(selected_dates) < policy.date_count:
        audit["block_reason"] = "insufficient_mature_feature_dates"
    elif count > policy.max_rows:
        audit["block_reason"] = "complete_window_exceeds_row_budget"
    elif estimated > policy.max_estimated_fit_bytes:
        audit["block_reason"] = "complete_window_exceeds_estimated_fit_budget"
    if audit["block_reason"]:
        audit["status"] = "BLOCKED"
        raise TrainingWindowBlocked(audit)
    return [sample for day in selected_dates for sample in sorted(groups[day],
            key=lambda row: str(row.get("ticker") or row.get("symbol") or ""))], audit
