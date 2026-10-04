from __future__ import annotations

from typing import Final


QUALITY_CONFLUENCE_PROFILE: Final = "quality_confluence_v1"
QUALITY_CONFLUENCE_BLOCKED_TAGS: Final[frozenset[str]] = frozenset(
    {
        "chase-risk",
        "weak-market",
        "weak-breadth",
        "do-not-chase",
        "missing-latest-price",
    }
)
QUALITY_CONFLUENCE_PASS_REASON: Final = "双模型共振、市场状态与可成交性均通过"
QUALITY_CONFLUENCE_REJECT_REASON: Final = "未通过质量策略硬门槛"


def normalize_action_filter(value: str | None) -> str:
    """Return the canonical action-filter key used by selection policies."""
    return str(value or "").strip().lower().replace(" ", "_")


def lightgbm_confluence_fit_score(item: dict, *, confluence_action_filter: str) -> int:
    """Score how well a LightGBM tactical action fits the requested playbook."""
    action_key = str(item.get("lightgbm_tactical_action") or "").strip().lower()
    if not action_key:
        return 0
    normalized_filter = normalize_action_filter(confluence_action_filter)
    if normalized_filter == "buy_the_dip":
        return 3 if action_key == "pullback" else 1 if action_key == "breakout" else 0
    if normalized_filter == "breakout_confirmation":
        return 3 if action_key == "breakout" else 1 if action_key == "pullback" else 0
    if normalized_filter == "watchlist":
        return 3 if action_key == "watch" else 0
    if normalized_filter == "bullish_entry":
        return 3 if action_key in {"pullback", "breakout"} else 1 if action_key == "watch" else 0
    if action_key in {"breakout", "pullback"}:
        return 2
    if action_key == "watch":
        return 1
    return 0


def rerank_with_lightgbm_tactical_signal(
    results: list[dict],
    *,
    confluence_action_filter: str,
) -> list[dict]:
    """Return a ranked copy without mutating the caller's list order."""
    return sorted(
        results,
        key=lambda row: (
            int(lightgbm_confluence_fit_score(row, confluence_action_filter=confluence_action_filter)),
            int(row.get("model_hit_count") or 0),
            int(row.get("confluence_alignment_count") or 0),
            float(row.get("snapshot_score") or 0.0),
            float(row.get("trend_score") or 0.0),
            str(row.get("ticker") or ""),
        ),
        reverse=True,
    )


def quality_confluence_decision(row: dict) -> tuple[bool, str, str]:
    """Evaluate the production quality-confluence gate for one candidate."""
    tags = {
        str(item).strip().lower()
        for item in (row.get("risk_flags") or row.get("model_execution_tags") or [])
        if str(item).strip()
    }
    ready = str(row.get("tradability_status") or "").upper() == "READY"
    confluence = int(row.get("model_hit_count") or 0) >= 2
    readiness = float(row.get("trade_readiness_score") or 0.0)
    accepted = ready and confluence and readiness >= 72.0 and not tags.intersection(QUALITY_CONFLUENCE_BLOCKED_TAGS)
    if accepted:
        return True, "A", QUALITY_CONFLUENCE_PASS_REASON
    return False, "WATCH", QUALITY_CONFLUENCE_REJECT_REASON


def apply_quality_confluence_profile(results: list[dict], *, profile: str) -> list[dict]:
    """Apply the interactive screener profile while preserving its public behavior."""
    if profile != QUALITY_CONFLUENCE_PROFILE:
        return results
    approved: list[dict] = []
    for row in results:
        accepted, tier, reason = quality_confluence_decision(row)
        if not accepted:
            continue
        row["strategy_tier"] = tier
        row["strategy_gate_reason"] = reason
        approved.append(row)
    return sorted(
        approved,
        key=lambda row: (
            float(row.get("trade_readiness_score") or 0.0),
            int(row.get("model_hit_count") or 0),
        ),
        reverse=True,
    )


def apply_snapshot_strategy_profile(rows: list[dict], *, profile: str) -> list[dict]:
    """Apply the same gate to persisted snapshots and retain rejection evidence."""
    if profile != QUALITY_CONFLUENCE_PROFILE:
        return rows
    approved: list[dict] = []
    for row in rows:
        accepted, tier, reason = quality_confluence_decision(row)
        row["strategy_tier"] = tier
        row["strategy_gate_reason"] = reason
        if accepted:
            approved.append(row)
    approved.sort(
        key=lambda row: (
            float(row.get("trade_readiness_score") or 0.0),
            int(row.get("model_hit_count") or 0),
            float(row.get("trend_score") or 0.0),
        ),
        reverse=True,
    )
    return approved


def kronos_sort_value(item: dict) -> float:
    """Rank ready/supportive Kronos validations before their numeric score."""
    validation = item.get("kronos_validation") if isinstance(item.get("kronos_validation"), dict) else {}
    try:
        score = float((validation or {}).get("kronos_score"))
    except (TypeError, ValueError):
        score = -1.0
    decision = str((validation or {}).get("kronos_decision") or "").lower()
    support_bonus = 1000.0 if ("支持" in decision or "support" in decision) and "不支持" not in decision else 0.0
    ready_bonus = 100.0 if str((validation or {}).get("kronos_status") or "").upper() == "READY" else 0.0
    return support_bonus + ready_bonus + score


def focus_pool_trade_candidates(results: list[dict]) -> tuple[list[dict], int]:
    """Split actionable focus-pool candidates from blocked/low-readiness rows."""
    candidates: list[dict] = []
    skipped = 0
    for item in results:
        status = str(item.get("tradability_status") or "").strip().upper()
        bucket = str(item.get("readiness_bucket") or "").strip().upper()
        try:
            readiness = float(item.get("trade_readiness_score") or 0.0)
        except (TypeError, ValueError):
            readiness = 0.0
        hard_blocked = status in {"BLOCKED", "DO_NOT_CHASE"}
        low_readiness = bucket in {"LOW", "BLOCKED"} or (readiness > 0 and readiness < 60.0)
        if hard_blocked or low_readiness:
            skipped += 1
            continue
        candidates.append(item)
    return candidates, skipped
