from __future__ import annotations

from typing import Any

from app.services.execution_tag_filters import (
    excludes_execution_tag_filter,
    matches_execution_tag_filter,
)


def filter_market_heatmap_rows(
    rows: list[dict[str, Any]],
    *,
    market_filter: str = "CN",
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    sort_by: str = "hits",
) -> list[dict[str, Any]]:
    """Filter and sort heatmap rows without mutating snapshot payloads."""
    market = str(market_filter or "CN").strip().upper()
    signal = str(signal_filter or "ALL").strip().upper()
    include_tags = str(execution_tag_filter or "ALL").strip()
    exclude_tags = str(exclude_execution_tag_filter or "ALL").strip()
    filtered: list[dict[str, Any]] = []

    for source in rows:
        item = dict(source)
        if market != "ALL" and str(item.get("market") or "").upper() != market:
            continue
        ticker_details = item.get("ticker_details") or []
        if signal == "BUY":
            if int(item.get("buy_signal_count") or 0) <= 0:
                continue
        elif signal != "ALL" and not any(
            str(detail.get("signal_label") or "").strip().upper() == signal
            for detail in ticker_details
        ):
            continue
        if min_signal_strength > 0 and int(item.get("max_signal_strength") or 0) < min_signal_strength:
            continue
        if min_buy_signal_count > 0 and int(item.get("buy_signal_count") or 0) < min_buy_signal_count:
            continue
        tags = item.get("execution_tags") or []
        if include_tags.upper() != "ALL" and not matches_execution_tag_filter(tags, include_tags):
            continue
        if exclude_tags.upper() != "ALL" and not excludes_execution_tag_filter(tags, exclude_tags):
            continue
        filtered.append(item)

    def sort_key(item: dict[str, Any]) -> tuple[float | int, float | int]:
        if sort_by == "five_day":
            return float(item.get("avg_move_5d") or -9999.0), float(item.get("avg_score") or 0.0)
        if sort_by == "breadth":
            return float(item.get("breadth_pct") or -1.0), float(item.get("avg_score") or 0.0)
        if sort_by == "score":
            return float(item.get("avg_score") or 0.0), int(item.get("hits") or 0)
        return int(item.get("hits") or 0), float(item.get("avg_score") or 0.0)

    return sorted(filtered, key=sort_key, reverse=True)


def summarize_heatmap_execution_risks(rows: list[dict[str, Any]]) -> dict[str, Any]:
    risk_counts: dict[str, int] = {}
    examples: list[dict[str, Any]] = []
    tagged_names = 0
    for item in rows:
        tags = [str(tag).strip() for tag in item.get("execution_tags") or [] if str(tag).strip()]
        if not tags:
            continue
        tagged_names += 1
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        if len(examples) < 3:
            examples.append({"label": item.get("label"), "tags": tags[:2]})
    return {
        "tagged_names": tagged_names,
        "risk_counts": risk_counts,
        "risk_examples": examples,
        "risk_top_tags": sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3],
    }
