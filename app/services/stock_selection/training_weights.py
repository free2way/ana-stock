"""Date-balanced weights independent of ticker order or universe size."""
from __future__ import annotations

from collections import Counter
from datetime import date


TRAINING_WEIGHT_POLICY = "linear_date_balanced_v1"


def date_balanced_training_weights(samples: list[dict]) -> tuple[list[float], dict]:
    if not samples:
        raise ValueError("training samples must not be empty")
    dates = [date.fromisoformat(str(row["trade_date"])).isoformat() for row in samples]
    counts = Counter(dates)
    ordered = sorted(counts)
    date_weights = {
        day: 0.65 + 0.7 * index / (len(ordered) - 1) if len(ordered) > 1 else 1.0
        for index, day in enumerate(ordered)
    }
    # Mean row weight is one; dates get equal mass before the time tilt.
    mass = len(samples) / sum(date_weights.values())
    by_date = {day: mass * date_weights[day] / counts[day] for day in ordered}
    weights = [by_date[day] for day in dates]
    audit = {
        "policy": TRAINING_WEIGHT_POLICY,
        "sample_count": len(samples),
        "date_count": len(ordered),
        "start_date": ordered[0],
        "end_date": ordered[-1],
        "effective_sample_count": sum(weights) ** 2 / sum(w * w for w in weights),
        "dates": [{"date": day, "count": counts[day], "row_weight": by_date[day],
                   "total_weight": by_date[day] * counts[day]} for day in ordered],
    }
    return weights, audit
