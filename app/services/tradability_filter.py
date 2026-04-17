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


def _infer_time_horizon(candidate: dict[str, Any], status: str) -> str:
    entry_style = str(candidate.get("entry_style") or "").strip().lower()
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
    entry_style = str(candidate.get("entry_style") or "").strip().lower()
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
    entry_style = str(candidate.get("entry_style") or "").strip().lower()
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
) -> str:
    if status == "BLOCKED":
        return "Do not enter until risk condition clears"
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
    del market_snapshot, portfolio_state

    rules = config or TradabilityRuleConfig()
    score = _safe_float(candidate.get("score"))
    signal_strength = _safe_float(candidate.get("signal_strength"))
    expected_drawdown_20d = _safe_float(candidate.get("expected_drawdown_20d"))
    signal_label = str(candidate.get("signal_label") or "").upper()
    entry_style = candidate.get("entry_style")

    risk_flags: list[str] = []
    block_reason: str | None = None
    status = "READY"

    if score is None:
        status = "REVIEW"
        risk_flags.append("missing-score")
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
        risk_flags=sorted(set(risk_flags)),
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
        ),
        diagnostics={
            "score": score,
            "signal_strength": signal_strength,
            "expected_drawdown_20d": expected_drawdown_20d,
            "time_horizon": time_horizon,
            "max_slippage_bps": max_slippage_bps,
        },
    )