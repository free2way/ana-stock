"""Research-stage regime contract; no model approval or production activation.

Preserves the existing market_risk budget ceilings. This module validates a
caller-supplied historical snapshot rather than looking up today's latest one.
"""
from datetime import date, datetime
from copy import deepcopy
import hashlib
import json
import math

from app.services.market_calendar import is_market_open_date
from app.services.stock_selection.data_contracts import validate_market_ticker


POLICY_VERSION = "regime_policy_research_v1"
CEILINGS = {
    "risk_on": 1.0, "watchful": 0.6,
    "high_dispersion": 0.35, "high_volatility": 0.35, "post_crash_rebound": 0.35,
    "rebound_failed": 0.0, "crash": 0.0, "unknown": 0.0,
}


def _aware(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return parsed


def evaluate_regime_policy(snapshot: dict | None, *, market: str, expected_market_date: str,
                           decision_cutoff_at: str, max_new_candidates: int = 5) -> dict:
    """Fail closed on bad input; REVIEW remains REVIEW, never model qualification.

    expected_market_date and cutoff are supplied by the frozen research protocol.
    The snapshot hash is an integrity reference, not an authenticated source.
    """
    reasons = []
    source_hash = None
    data = snapshot if isinstance(snapshot, dict) else {}
    if market not in {"CN", "US"}:
        reasons.append("unsupported_market")
    if data.get("market") != market:
        reasons.append("snapshot_market_mismatch")
    try:
        expected = date.fromisoformat(expected_market_date)
        if expected.isoformat() != expected_market_date or not is_market_open_date(market, expected_market_date):
            raise ValueError("invalid market date")
    except (ValueError, TypeError):
        reasons.append("invalid_expected_market_date")
    if data.get("snapshot_date") != expected_market_date:
        reasons.append("snapshot_date_mismatch")
    try:
        cutoff = _aware(decision_cutoff_at)
        generated = _aware(data.get("generated_at"))
        if generated > cutoff:
            reasons.append("snapshot_after_cutoff")
        # Validate ordering in the market's local calendar, not the host timezone.
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Asia/Shanghai" if market == "CN" else "America/New_York")
        if cutoff.astimezone(zone).date().isoformat() < expected_market_date:
            reasons.append("cutoff_before_market_date")
        if generated.astimezone(zone).date().isoformat() < expected_market_date:
            reasons.append("snapshot_generated_before_market_date")
    except (ValueError, TypeError):
        reasons.append("invalid_snapshot_or_cutoff_timestamp")
    try:
        source_hash = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                               allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError):
        reasons.append("invalid_snapshot_payload")
    regime = data.get("risk_regime")
    if not isinstance(regime, str) or regime not in CEILINGS:
        regime = "unknown"
        reasons.append("invalid_risk_regime")
    if regime in {"crash", "rebound_failed", "unknown"}:
        reasons.append("regime_blocks_new_candidates")
    source_gate = data.get("buy_gate")
    if source_gate not in ("ALLOW", "REVIEW", "BLOCK"):
        reasons.append("invalid_source_buy_gate")
    elif source_gate == "BLOCK":
        reasons.append("source_buy_gate_blocked")
    scale = data.get("max_position_scale")
    if type(scale) not in (float, int) or not 0 <= scale <= 1 or not math.isfinite(scale):
        reasons.append("invalid_position_scale")
        scale = 0.0
    if type(max_new_candidates) is not int or not 0 <= max_new_candidates <= 5:
        reasons.append("invalid_candidate_cap")
        max_new_candidates = 0
    effective_scale = min(float(scale), CEILINGS[regime])
    if effective_scale == 0 or max_new_candidates == 0:
        reasons.append("zero_new_position_budget")
    blocked = bool(reasons)
    gate = "BLOCK" if blocked else "REVIEW" if regime != "risk_on" or source_gate == "REVIEW" else "ALLOW"
    return {
        "policy_version": POLICY_VERSION, "market": market, "risk_regime": regime,
        "expected_market_date": expected_market_date, "snapshot_date": data.get("snapshot_date"),
        "generated_at": data.get("generated_at"), "decision_cutoff_at": decision_cutoff_at,
        "source_snapshot_sha256": source_hash, "source_buy_gate": source_gate,
        "buy_gate": gate, "max_new_candidates": 0 if blocked else max_new_candidates,
        "max_position_scale": 0.0 if blocked else effective_scale,
        "budget_semantics": "multiplier_of_protocol_exposure_cap_not_account_weight",
        "regime_position_hint": "暂停新开仓，候选仅作研究观察" if blocked else
            f"协议敞口上限倍率不超过 {effective_scale:g}；仍需模型资格和组合约束",
        "reasons": sorted(set(reasons)), "research_only": True, "protocol_approved": False,
    }


def prepare_regime_research_selection(candidates: list[dict], snapshot: dict | None, *, market: str,
                                     expected_market_date: str, decision_cutoff_at: str,
                                     max_new_candidates: int = 5) -> dict:
    """Apply the contract to an already-ranked research list, never publish it.

    Keep the original list and excluded suffix for denominator/audit purposes.
    This does not enforce model qualification, liquidity or portfolio execution.
    """
    if market not in {"CN", "US"}:
        raise ValueError("research regime selection supports CN/US only")
    seen = set()
    for candidate in candidates:
        ticker = validate_market_ticker(market, candidate.get("ticker"))
        if ticker != candidate.get("ticker") or ticker in seen or ticker.endswith(".BJ"):
            raise ValueError("canonical unique in-scope research candidates required")
        seen.add(ticker)
    policy = evaluate_regime_policy(snapshot, market=market, expected_market_date=expected_market_date,
        decision_cutoff_at=decision_cutoff_at, max_new_candidates=max_new_candidates)
    count = policy["max_new_candidates"]
    return {"schema_version": "regime_research_selection_v1", "policy": policy,
            "research_candidates": deepcopy(candidates[:count]),
            "watch_candidates": deepcopy(candidates),
            "excluded_candidates": [{"candidate": deepcopy(row),
                                     "reason": "regime_blocked" if policy["buy_gate"] == "BLOCK" else "candidate_cap"}
                                    for row in candidates[count:]],
            "input_count": len(candidates), "selected_count": len(candidates[:count]),
            "selection_semantics": "research_only_not_trade_authorization"}
