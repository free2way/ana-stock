from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TradabilityRuleConfig:
    min_score: float = 0.0
    strong_score: float = 0.75
    review_score: float = 0.55
    max_expected_drawdown_20d: float = 0.15


@dataclass(slots=True)
class TradabilityDecision:
    is_tradable: bool
    tradability_status: str
    block_reason: str | None = None
    trade_readiness_score: float | None = None
    readiness_bucket: str | None = None
    readiness_reason: str | None = None
    preferred_entry_style: str | None = None
    suggested_watch_action: str | None = None
    risk_flags: list[str] = field(default_factory=list)
    liquidity_bucket: str | None = None
    suggested_participation_rate: float | None = None
    entry_trigger: str | None = None
    invalidation_condition: str | None = None
    time_horizon: str | None = None
    max_slippage_bps: int | None = None
    stop_loss_type: str | None = None
    execution_note: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [item.strip() for item in text.replace(";", ",").split(",") if item.strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_score(value: float | None) -> float | None:
    if value is None:
        return None
    return value / 100.0 if value > 1.5 else value


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _infer_liquidity_bucket(candidate: dict[str, Any]) -> str | None:
    market = str(candidate.get("market") or "").upper()
    ticker = str(candidate.get("ticker") or "").upper()
    if market == "US":
        return "A"
    if market in {"HK", "CN"}:
        return "B"
    if ticker:
        return "B"
    return None


def _infer_entry_style(candidate: dict[str, Any]) -> str | None:
    for key in ("entry_style", "model_entry_style", "action_label", "setup_bucket"):
        value = str(candidate.get(key) or "").strip().lower()
        if value:
            return value
    return None


def _readiness_bucket(score: float, status: str) -> str:
    if status == "BLOCKED" or score < 35:
        return "BLOCKED"
    if score >= 72:
        return "HIGH"
    if score >= 55:
        return "MEDIUM"
    return "LOW"


def _suggested_watch_action(bucket: str, status: str, entry_style: str | None) -> str:
    if bucket == "BLOCKED" or status == "BLOCKED":
        return "avoid"
    if bucket == "HIGH":
        return "prioritize"
    if entry_style in {"pullback", "buy_the_dip", "pullback_reentry", "support_hold"}:
        return "watch_pullback"
    if entry_style in {"breakout", "momentum", "breakout_ready", "wait_for_breakout"}:
        return "wait_confirmation"
    return "continue_to_watch"


def _market_context_summary(market_snapshot: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    market_code = str(candidate.get("market") or "").strip().upper()
    if not market_snapshot or market_code != "CN":
        return {}
    if any(key in market_snapshot for key in ("regime", "breadth_pct", "crowded_theme", "breakout_tailwind")):
        if str(market_snapshot.get("market") or market_code).strip().upper() in {"", market_code}:
            return dict(market_snapshot)
    return {}


def _calculate_readiness_score(
    candidate: dict[str, Any],
    *,
    score: float | None,
    signal_strength: float | None,
    expected_drawdown_20d: float | None,
    status: str,
    risk_flags: list[str],
    entry_style: str | None,
) -> tuple[float, str]:
    trend_score = _safe_float(candidate.get("trend_score"))
    percentile = _safe_float(candidate.get("model_percentile") or candidate.get("percentile"))
    confidence = _safe_float(candidate.get("model_confidence") or candidate.get("confidence"))
    reward_risk = _safe_float(candidate.get("model_reward_risk_ratio") or candidate.get("reward_risk_ratio"))
    volume_ratio = _safe_float(candidate.get("volume_ratio"))
    momentum_5 = _safe_float(candidate.get("momentum_5"))
    distance_to_breakout = _safe_float(candidate.get("distance_to_breakout_pct"))
    snapshot_hits = _safe_float(candidate.get("snapshot_hits"))
    model_hits = _safe_float(candidate.get("model_hit_count") or candidate.get("confluence_alignment_count"))
    latest_close = _safe_float(candidate.get("latest_close"))
    market_regime = str(candidate.get("market_regime") or "").strip().lower()
    market_breadth_pct = _safe_float(candidate.get("market_breadth_pct"))
    crowded_theme = bool(candidate.get("market_crowded_theme"))

    readiness = 38.0
    reasons: list[str] = []
    if score is not None:
        readiness += _clamp(score * 100.0, 0, 100) * 0.22
        reasons.append("model_score")
    if signal_strength is not None:
        readiness += _clamp(signal_strength, 0, 100) * 0.20
        reasons.append("signal_strength")
    if trend_score is not None:
        readiness += _clamp(trend_score, 0, 100) * 0.18
        reasons.append("trend")
    if percentile is not None:
        readiness += _clamp(percentile, 0, 100) * 0.06
    if confidence is not None:
        readiness += _clamp(confidence, 0, 100) * 0.05
    if reward_risk is not None:
        readiness += min(4.0, max(0.0, reward_risk)) * 4.0
        reasons.append("reward_risk")
    if volume_ratio is not None:
        readiness += min(8.0, max(0.0, volume_ratio)) * 1.4
    if snapshot_hits is not None:
        readiness += min(4.0, max(0.0, snapshot_hits)) * 2.0
    if model_hits is not None:
        readiness += min(5.0, max(0.0, model_hits)) * 3.0
        reasons.append("model_confluence")
    if market_regime == "risk_on":
        readiness += 6.0
        reasons.append("market_tailwind")
    elif market_regime == "defensive":
        readiness -= 10.0
        reasons.append("weak_market")
    if market_breadth_pct is not None and market_breadth_pct < 45.0:
        readiness -= 8.0
        reasons.append("weak_breadth")
    elif market_breadth_pct is not None and market_breadth_pct >= 60.0:
        readiness += 3.0
        reasons.append("broad_participation")
    if crowded_theme:
        readiness -= 5.0
        reasons.append("crowded_theme")

    if latest_close is None:
        readiness -= 8.0
        reasons.append("missing_price")
    if expected_drawdown_20d is not None:
        drawdown_pct = expected_drawdown_20d * 100.0 if expected_drawdown_20d <= 1.5 else expected_drawdown_20d
        if drawdown_pct >= 12:
            readiness -= min(28.0, (drawdown_pct - 10.0) * 1.7)
            reasons.append("drawdown_risk")
    if momentum_5 is not None and momentum_5 >= 18 and entry_style not in {"pullback", "buy_the_dip", "support_hold"}:
        readiness -= min(18.0, (momentum_5 - 16.0) * 1.2)
        reasons.append("chase_risk")
    if distance_to_breakout is not None and distance_to_breakout > 10:
        readiness -= min(18.0, (distance_to_breakout - 10.0) * 1.1)
        reasons.append("far_from_trigger")
    readiness -= min(28.0, len(set(risk_flags)) * 5.0)

    if status == "READY":
        readiness += 8.0
    elif status == "DEFER":
        readiness -= 4.0
    elif status == "REVIEW":
        readiness -= 10.0
    elif status == "BLOCKED":
        readiness -= 45.0

    readable_reasons = [item for item in reasons if item]
    reason = ", ".join(readable_reasons[:4]) if readable_reasons else "balanced_setup"
    return round(_clamp(readiness), 1), reason


def _infer_time_horizon(candidate: dict[str, Any], status: str) -> str:
    entry_style = _infer_entry_style(candidate) or ""
    if status == "BLOCKED":
        return "no-trade"
    if entry_style in {"breakout", "momentum"}:
        return "3-10d"
    if entry_style in {"pullback", "buy_the_dip"}:
        return "2-8d"
    score = _safe_float(candidate.get("score"))
    if score is not None and score >= 0.85:
        return "5-15d"
    if status == "DEFER":
        return "1-5d"
    return "3-12d"


def _infer_max_slippage_bps(candidate: dict[str, Any], liquidity_bucket: str | None) -> int:
    market = str(candidate.get("market") or "").upper()
    if liquidity_bucket == "A":
        return 15 if market == "US" else 20
    if liquidity_bucket == "B":
        return 25
    return 35


def _build_entry_trigger(candidate: dict[str, Any], status: str) -> str:
    if status == "BLOCKED":
        return "No entry until signal requalifies"
    entry_style = _infer_entry_style(candidate) or ""
    signal_strength = _safe_float(candidate.get("signal_strength"))
    if entry_style == "breakout":
        return "Enter only on confirmed breakout with volume support"
    if entry_style in {"pullback", "buy_the_dip"}:
        return "Scale in only on orderly pullback into support / buy zone"
    if signal_strength is not None and signal_strength >= 85:
        return "Open starter after first 15 minutes if price holds VWAP / opening range"
    return "Wait for liquid open and enter with passive orders near planned level"


def _build_invalidation_condition(candidate: dict[str, Any], status: str) -> str:
    if status == "BLOCKED":
        return "Signal remains non-actionable or risk condition worsens"
    expected_drawdown_20d = _safe_float(candidate.get("expected_drawdown_20d"))
    signal_label = str(candidate.get("signal_label") or "").upper()
    if signal_label in {"SELL", "STRONG_SELL"}:
        return "Model stays in sell regime"
    if expected_drawdown_20d is not None and expected_drawdown_20d >= 0.12:
        return "Abort if drawdown profile expands or support fails"
    return "Abort if setup loses support or closing strength deteriorates"


def _build_stop_loss_type(candidate: dict[str, Any], status: str) -> str:
    if status == "BLOCKED":
        return "none"
    entry_style = _infer_entry_style(candidate) or ""
    if entry_style in {"breakout", "momentum"}:
        return "breakout-failure"
    if entry_style in {"pullback", "buy_the_dip"}:
        return "support-break"
    return "technical-close"


def _build_execution_note(
    status: str,
    signal_strength: float | None,
    expected_drawdown_20d: float | None,
    entry_style: str | None,
    liquidity_bucket: str | None,
    max_slippage_bps: int | None,
    market_regime: str | None,
) -> str:
    if status == "BLOCKED":
        return "Do not enter until risk condition clears"
    if str(market_regime or "").lower() == "defensive":
        return "Tape is defensive; reduce size, prefer pullbacks, and avoid aggressive breakout chasing"
    if entry_style:
        liquidity_note = f"; keep slippage within {max_slippage_bps} bps" if max_slippage_bps is not None else ""
        return f"Prefer {entry_style.lower()} execution and reassess at the open{liquidity_note}"
    if expected_drawdown_20d is not None and expected_drawdown_20d >= 0.1:
        return "Use smaller size and stagger entry to control drawdown risk"
    if signal_strength is not None and signal_strength >= 80:
        return "High conviction candidate; prefer passive entry after first 15 minutes"
    if liquidity_bucket == "B":
        return "Respect liquidity limits and avoid chasing through thin prints"
    return "Review liquidity at the open and avoid aggressive market orders"


def evaluate_candidate_tradability(
    candidate: dict[str, Any],
    *,
    market_snapshot: dict[str, Any] | None = None,
    portfolio_state: dict[str, Any] | None = None,
    config: TradabilityRuleConfig | None = None,
) -> TradabilityDecision:
    rules = config or TradabilityRuleConfig()
    score = _normalize_score(_safe_float(candidate.get("score") or candidate.get("model_score")))
    signal_strength = _safe_float(candidate.get("signal_strength"))
    if signal_strength is None:
        signal_strength = _safe_float(candidate.get("model_signal_strength") or candidate.get("trend_score"))
    expected_drawdown_20d = _safe_float(candidate.get("expected_drawdown_20d"))
    if expected_drawdown_20d is None:
        expected_drawdown_20d = _safe_float(candidate.get("model_expected_drawdown_20d"))
    signal_label = str(candidate.get("signal_label") or "").upper()
    if not signal_label:
        signal_label = str(candidate.get("model_signal_label") or "").upper()
    entry_style = _infer_entry_style(candidate)
    market_context = _market_context_summary(market_snapshot, candidate)
    market_regime = str(market_context.get("regime") or "").strip().lower()
    market_breadth_pct = _safe_float(market_context.get("breadth_pct"))
    market_crowded_theme = bool(market_context.get("crowded_theme"))
    breakout_tailwind = bool(market_context.get("breakout_tailwind"))

    risk_flags: list[str] = _safe_list(candidate.get("risk_flags")) + _safe_list(candidate.get("model_execution_tags"))
    block_reason: str | None = None
    status = "READY"

    if score is None:
        risk_flags.append("missing-model-score")
        if signal_strength is None or signal_strength < 70:
            status = "REVIEW"
    elif score < rules.min_score or signal_label in {"SELL", "STRONG_SELL"}:
        status = "BLOCKED"
        block_reason = "signal_not_actionable"
    elif score < rules.review_score:
        status = "REVIEW"
        risk_flags.append("low-conviction")
    elif score < rules.strong_score:
        status = "DEFER"
        risk_flags.append("needs-better-entry")

    if expected_drawdown_20d is not None and expected_drawdown_20d >= rules.max_expected_drawdown_20d:
        if status == "READY":
            status = "REVIEW"
        risk_flags.append("drawdown-risk")

    if signal_strength is not None and signal_strength < 50:
        if status == "READY":
            status = "REVIEW"
        risk_flags.append("weak-signal-strength")

    latest_close = _safe_float(candidate.get("latest_close"))
    if latest_close is None:
        if status == "READY":
            status = "REVIEW"
        risk_flags.append("missing-latest-price")

    momentum_5 = _safe_float(candidate.get("momentum_5"))
    chase_threshold = 30.0 if breakout_tailwind else 22.0 if market_regime == "defensive" else 25.0
    if momentum_5 is not None and momentum_5 >= chase_threshold and entry_style not in {"pullback", "buy_the_dip", "support_hold"}:
        if status in {"READY", "DEFER"}:
            status = "REVIEW"
        risk_flags.append("chase-risk")

    if market_regime == "defensive":
        risk_flags.append("weak-market")
        if entry_style in {"breakout", "momentum", "wait_for_breakout", "breakout_ready"}:
            if status in {"READY", "DEFER"}:
                status = "BLOCKED"
                block_reason = block_reason or "weak_market_breakout"
        elif status == "READY":
            status = "REVIEW"
    elif market_regime == "watchful" and entry_style in {"breakout", "momentum", "wait_for_breakout", "breakout_ready"}:
        if (signal_strength or 0.0) < 70.0 and status == "READY":
            status = "REVIEW"
            risk_flags.append("confirmation-needed")

    if market_breadth_pct is not None and market_breadth_pct < 45.0:
        risk_flags.append("weak-breadth")
        if entry_style in {"breakout", "momentum", "wait_for_breakout", "breakout_ready"} and status == "READY":
            status = "REVIEW"

    if market_crowded_theme and entry_style in {"breakout", "momentum", "wait_for_breakout", "breakout_ready"}:
        risk_flags.append("crowded-theme")
        if momentum_5 is not None and momentum_5 >= 18.0 and status == "READY":
            status = "DEFER"

    portfolio_risk_count = _safe_float((portfolio_state or {}).get("risk_count"))
    if portfolio_risk_count is not None and portfolio_risk_count >= 3:
        risk_flags.append("portfolio-risk-budget")
        if status == "READY":
            status = "REVIEW"

    risk_flags = sorted(set(risk_flags))
    readiness_score, readiness_reason = _calculate_readiness_score(
        {
            **candidate,
            "market_regime": market_regime,
            "market_breadth_pct": market_breadth_pct,
            "market_crowded_theme": market_crowded_theme,
        },
        score=score,
        signal_strength=signal_strength,
        expected_drawdown_20d=expected_drawdown_20d,
        status=status,
        risk_flags=risk_flags,
        entry_style=entry_style,
    )
    bucket = _readiness_bucket(readiness_score, status)
    suggested_watch_action = _suggested_watch_action(bucket, status, entry_style)

    liquidity_bucket = _infer_liquidity_bucket(candidate)
    participation_rate = 0.03 if liquidity_bucket == "A" else 0.02 if liquidity_bucket == "B" else 0.01
    time_horizon = _infer_time_horizon(candidate, status)
    max_slippage_bps = _infer_max_slippage_bps(candidate, liquidity_bucket)
    entry_trigger = _build_entry_trigger(candidate, status)
    invalidation_condition = _build_invalidation_condition(candidate, status)
    stop_loss_type = _build_stop_loss_type(candidate, status)

    return TradabilityDecision(
        is_tradable=status in {"READY", "REVIEW", "DEFER"},
        tradability_status=status,
        block_reason=block_reason,
        trade_readiness_score=readiness_score,
        readiness_bucket=bucket,
        readiness_reason=readiness_reason,
        preferred_entry_style=entry_style,
        suggested_watch_action=suggested_watch_action,
        risk_flags=risk_flags,
        liquidity_bucket=liquidity_bucket,
        suggested_participation_rate=participation_rate,
        entry_trigger=entry_trigger,
        invalidation_condition=invalidation_condition,
        time_horizon=time_horizon,
        max_slippage_bps=max_slippage_bps,
        stop_loss_type=stop_loss_type,
        execution_note=_build_execution_note(
            status,
            signal_strength,
            expected_drawdown_20d,
            entry_style,
            liquidity_bucket,
            max_slippage_bps,
            market_regime,
        ),
        diagnostics={
            "score": score,
            "signal_strength": signal_strength,
            "expected_drawdown_20d": expected_drawdown_20d,
            "trade_readiness_score": readiness_score,
            "readiness_bucket": bucket,
            "time_horizon": time_horizon,
            "max_slippage_bps": max_slippage_bps,
            "market_regime": market_regime or None,
            "market_breadth_pct": market_breadth_pct,
            "market_crowded_theme": market_crowded_theme,
        },
    )
