from __future__ import annotations

from typing import Any


def select_discouraged_recommendations(
    recommendations: list[dict[str, Any]],
    *,
    minimum_samples: int = 5,
    minimum_hit_rate: float = 45.0,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return sufficiently sampled recommendations with weak recent evidence."""
    discouraged = []
    for item in recommendations:
        stats = item.get("stats_1d") or {}
        average_return = stats.get("avg_return")
        hit_rate = stats.get("hit_rate")
        if int(item.get("sample_count") or 0) < minimum_samples:
            continue
        if (
            average_return is not None
            and float(average_return) < 0
            or hit_rate is not None
            and float(hit_rate) < minimum_hit_rate
        ):
            discouraged.append(item)
    if limit <= 0:
        return []
    return discouraged[-limit:]
