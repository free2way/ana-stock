"""Pure continuous-leader selection shared by pages and exports."""

from app.services.execution_tag_filters import (
    excludes_execution_tag_filter,
    matches_execution_tag_filter,
)


def continuous_leader_sort_key(item: dict, sort_by: str) -> tuple:
    if sort_by == "ticker":
        return (item["ticker"],)
    if sort_by == "score":
        return (float(item.get("score") or 0.0), item["ticker"])
    if sort_by == "signal":
        return (float(item.get("signal_strength") or 0.0), item["ticker"])
    if sort_by == "trend":
        history = item.get("score_history") or []
        last_delta = (history[-1] - history[0]) if len(history) >= 2 else 0.0
        return (float(last_delta), item["ticker"])
    return (int(item.get("hits") or 0), float(item.get("score") or 0.0), item["ticker"])


def build_continuous_leader_view(
    rows: list[dict], watchlist_map: dict[str, dict], *,
    market: str = "ALL", state: str = "ALL", signal: str = "ALL",
    min_signal_strength: int = 0, include_tags: str = "ALL", exclude_tags: str = "ALL",
    sort_by: str = "hits", sort_order: str = "desc",
) -> dict:
    """Select copies, leaving cached snapshot and watchlist records untouched.

    Risk examples retain snapshot order; only the result table is sorted.
    Ticker ties reverse together with the primary key, matching existing URLs.
    """
    selected = []
    for source in rows:
        item = dict(source)
        existing = watchlist_map.get(item["ticker"])
        if existing is None:
            item["continuous_state_key"] = "OFF"
        elif existing.get("sync_enabled") and existing.get("sync_status") == "success":
            item["continuous_state_key"] = "READY"
        elif existing.get("sync_enabled"):
            item["continuous_state_key"] = "WAITING"
        else:
            item["continuous_state_key"] = "IN"
        if market != "ALL" and item.get("market") != market:
            continue
        if state != "ALL" and item["continuous_state_key"] != state:
            continue
        if signal != "ALL" and str(item.get("signal_label") or "").strip().upper() != signal:
            continue
        if min_signal_strength > 0 and int(item.get("signal_strength") or 0) < min_signal_strength:
            continue
        if not matches_execution_tag_filter(item.get("execution_tags"), include_tags):
            continue
        if not excludes_execution_tag_filter(item.get("execution_tags"), exclude_tags):
            continue
        selected.append(item)
    risk_counts: dict[str, int] = {}
    examples = []
    tagged_names = 0
    for item in selected:
        tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        tagged_names += 1
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        examples.append({"ticker": item.get("ticker"), "tags": tags[:2]})
    selected.sort(key=lambda item: continuous_leader_sort_key(item, sort_by), reverse=sort_order != "asc")
    return {"rows": selected, "tagged_names": tagged_names, "risk_examples": examples[:3],
            "risk_top_tags": sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]}
