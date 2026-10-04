from __future__ import annotations

from typing import Any

from app.services.execution_tag_filters import (
    excludes_execution_tag_filter,
    matches_execution_tag_filter,
)
from app.services.model_signal_summary import build_signal_label


MARKET_PULSE_SOFT_RISK_TAGS = {
    "low-conviction",
    "drawdown-risk",
}


def filter_market_pulse_signals(
    signals: list[dict[str, Any]],
    *,
    buy_hit_counts: dict[str, int] | None = None,
    market_filter: str = "CN",
    signal_filter: str = "ALL",
    min_signal_strength: int = 0,
    min_buy_signal_count: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    lang: str = "en",
) -> list[dict[str, Any]]:
    """Apply the market-pulse candidate contract without mutating source rows."""
    hits = buy_hit_counts or {}
    selected_market = str(market_filter or "CN").strip().upper()
    selected_signal = str(signal_filter or "ALL").strip().upper()
    include_tags = str(execution_tag_filter or "ALL").strip()
    exclude_tags = str(exclude_execution_tag_filter or "ALL").strip()
    filtered: list[dict[str, Any]] = []

    for item in signals:
        row = dict(item)
        ticker = str(row.get("ticker") or "").strip().upper()
        row["snapshot_buy_hits"] = int(hits.get(ticker, 0))
        row["market"] = str(row.get("market") or "OTHER").upper()
        all_tags = [
            str(tag).strip()
            for tag in (row.get("risk_flags") or row.get("execution_tags") or [])
            if str(tag).strip()
        ]
        row["market_risk_tags"] = [tag for tag in all_tags if tag not in MARKET_PULSE_SOFT_RISK_TAGS]
        label = str(row.get("signal_label") or build_signal_label(row.get("score"), lang=lang) or "").strip().upper()
        if selected_market != "ALL" and row["market"] != selected_market:
            continue
        if selected_signal != "ALL" and label != selected_signal:
            continue
        if min_signal_strength > 0 and int(row.get("signal_strength") or 0) < min_signal_strength:
            continue
        if min_buy_signal_count > 0 and int(row.get("snapshot_buy_hits") or 0) < min_buy_signal_count:
            continue
        tags = row.get("risk_flags") or row.get("execution_tags") or []
        if include_tags.upper() != "ALL" and not matches_execution_tag_filter(tags, include_tags):
            continue
        if exclude_tags.upper() != "ALL" and not excludes_execution_tag_filter(tags, exclude_tags):
            continue
        filtered.append(row)
    return filtered


def summarize_market_pulse_signals(
    signals: list[dict[str, Any]],
    *,
    lang: str = "en",
) -> dict[str, Any]:
    market_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, Any]] = []
    tagged_names = 0
    bucket_counts = {key: 0 for key in ("BUY", "WATCH", "SELL", "HOLD")}

    for item in signals:
        market = str(item.get("market") or "OTHER").upper()
        market_counts[market] = market_counts.get(market, 0) + 1
        tags = [str(tag).strip() for tag in item.get("market_risk_tags") or [] if str(tag).strip()]
        if tags:
            tagged_names += 1
            for tag in tags:
                risk_counts[tag] = risk_counts.get(tag, 0) + 1
            if len(risk_examples) < 3:
                risk_examples.append({"label": item.get("ticker") or "-", "tags": tags[:2]})
        label = str(item.get("signal_label") or build_signal_label(item.get("score"), lang=lang) or "").strip().upper()
        if label in bucket_counts:
            bucket_counts[label] += 1

    if bucket_counts["BUY"] >= max(bucket_counts["WATCH"], bucket_counts["SELL"], 1):
        tone = "偏进攻" if lang == "zh" else "Risk-on"
    elif bucket_counts["SELL"] > bucket_counts["BUY"] or tagged_names > bucket_counts["BUY"]:
        tone = "偏防守" if lang == "zh" else "Defensive"
    else:
        tone = "观察确认" if lang == "zh" else "Watchful"

    return {
        "market_counts": market_counts,
        "tagged_names": tagged_names,
        "risk_counts": risk_counts,
        "risk_examples": risk_examples,
        "risk_top_tags": sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3],
        "signal_bucket_counts": bucket_counts,
        "market_tone": tone,
    }
