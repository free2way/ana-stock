from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.template_evaluation import aggregate_window_stats


SummaryLoader = Callable[[dict[str, Any]], dict[str, Any] | None]


def aggregate_model_run_performance(
    runs: list[dict[str, Any]],
    *,
    summary_loader: SummaryLoader,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Aggregate recent run summaries by model and market.

    Data access is injected by the route so this calculation remains reusable
    and deterministic outside HTTP/database infrastructure.
    """
    aggregate_by_model: dict[tuple[str, str], dict[str, Any]] = {}
    for item in runs:
        model_key = (str(item.get("name") or "-"), str(item.get("market") or "-"))
        summary = summary_loader(item) or {}
        windows = summary.get("windows") or {}
        aggregate = aggregate_by_model.setdefault(
            model_key,
            {
                "name": model_key[0],
                "market": model_key[1],
                "runs": 0,
                "latest_trade_date": None,
                "trade_dates_covered": 0,
                "sample_count": 0,
                "window_sums": {
                    3: {"weighted_return": 0.0, "count": 0, "hit_weight": 0.0},
                    5: {"weighted_return": 0.0, "count": 0, "hit_weight": 0.0},
                    10: {"weighted_return": 0.0, "count": 0, "hit_weight": 0.0},
                },
            },
        )
        aggregate["runs"] += 1
        aggregate["trade_dates_covered"] += int(summary.get("trade_dates") or 0)
        aggregate["sample_count"] += int(summary.get("pick_count") or 0)
        latest_trade_date = summary.get("latest_trade_date")
        if latest_trade_date and (
            aggregate["latest_trade_date"] is None
            or str(latest_trade_date) > str(aggregate["latest_trade_date"])
        ):
            aggregate["latest_trade_date"] = latest_trade_date
        for window in (3, 5, 10):
            window_payload = windows.get(window) or {}
            count = int(window_payload.get("count") or 0)
            if count <= 0:
                continue
            aggregate["window_sums"][window]["count"] += count
            aggregate["window_sums"][window]["weighted_return"] += (
                float(window_payload.get("avg_return") or 0.0) * count
            )
            aggregate["window_sums"][window]["hit_weight"] += (
                float(window_payload.get("hit_rate") or 0.0) * count
            )
    return aggregate_by_model


def summarize_rows_by_dimension(
    rows: list[dict[str, Any]],
    *,
    dimension: str,
    missing_label: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Summarize 3/5/10-day returns for a regime or sector dimension.

    This keeps grouping and window statistics out of the HTTP controller while
    preserving the original sample ordering rules used by the dashboard.
    """
    groups: dict[str, dict[int, list[float]]] = {}
    for row in rows:
        if dimension == "regime":
            label = str(row.get("regime_label") or missing_label)
        elif dimension == "sector":
            label = str(
                row.get("sector_group")
                or row.get("sector")
                or row.get("industry")
                or missing_label
            )
        else:
            raise ValueError(f"unsupported performance dimension: {dimension}")
        bucket = groups.setdefault(label, {3: [], 5: [], 10: []})
        for window in (3, 5, 10):
            value = row.get(f"return_{window}d")
            if value is not None:
                bucket[window].append(float(value))

    ordered = sorted(
        groups.items(),
        key=lambda pair: (-len(pair[1].get(5) or []), str(pair[0] or "")),
    )
    if limit is not None:
        ordered = ordered[: max(0, int(limit))]
    return [
        {
            "label": label,
            "sample_count": max(
                int(window_stats[3].get("count") or 0),
                int(window_stats[5].get("count") or 0),
                int(window_stats[10].get("count") or 0),
            ),
            "windows": window_stats,
        }
        for label, bucket in ordered
        for window_stats in [
            {
                window: aggregate_window_stats(bucket.get(window) or [])
                for window in (3, 5, 10)
            }
        ]
    ]
