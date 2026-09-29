"""Research-only P1 portfolio concentration and allocation gate."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
from math import floor
import statistics
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class P1PortfolioRiskConfig:
    market: str
    max_industry_names: int = 2
    max_concept_names: int = 2
    correlation_lookback_sessions: int = 60
    minimum_correlation_observations: int = 20
    maximum_pair_correlation: float = 0.80
    max_position_weight: float = 0.10
    max_gross_exposure: float = 0.60
    max_new_gross_weight: float = 0.25
    missing_industry_policy: str = "BLOCK"
    missing_concept_policy: str = "ALLOW_WITH_AUDIT"
    insufficient_correlation_policy: str = "BLOCK"
    missing_atr_policy: str = "BLOCK"
    schema_version: str = "p1_portfolio_risk_gate_v1"

    def __post_init__(self) -> None:
        if self.market.upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        if min(self.max_industry_names, self.max_concept_names) <= 0:
            raise ValueError("concentration name limits must be positive")
        if self.correlation_lookback_sessions < 2 or self.minimum_correlation_observations < 2:
            raise ValueError("correlation windows must be at least two")
        if self.minimum_correlation_observations > self.correlation_lookback_sessions:
            raise ValueError("minimum correlation observations cannot exceed lookback")
        for name in ("maximum_pair_correlation", "max_position_weight", "max_gross_exposure", "max_new_gross_weight"):
            value = float(getattr(self, name))
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.max_position_weight > self.max_gross_exposure:
            raise ValueError("position limit cannot exceed gross exposure")
        if self.missing_industry_policy not in {"BLOCK", "ALLOW_WITH_AUDIT"}:
            raise ValueError("invalid missing_industry_policy")
        if self.missing_concept_policy not in {"BLOCK", "ALLOW_WITH_AUDIT"}:
            raise ValueError("invalid missing_concept_policy")
        if self.insufficient_correlation_policy not in {"BLOCK", "ALLOW_WITH_AUDIT"}:
            raise ValueError("invalid insufficient_correlation_policy")
        if self.missing_atr_policy not in {"BLOCK", "ALLOW_WITH_AUDIT"}:
            raise ValueError("invalid missing_atr_policy")

    def version(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return f"{self.schema_version}:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class P1PortfolioCandidate:
    ticker: str
    score: float
    entry_price: float
    data_as_of: date
    industry: str | None
    concepts: tuple[str, ...] = ()
    atr_value: float | None = None
    atr_unit: str | None = None
    atr_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if not str(self.ticker or "").strip() or not math.isfinite(self.score):
            raise ValueError("candidate ticker and finite score are required")
        if not math.isfinite(self.entry_price) or self.entry_price <= 0:
            raise ValueError("candidate entry_price must be positive")
        if self.atr_unit not in {None, "price", "relative"}:
            raise ValueError("atr_unit must be price, relative, or None")
        if self.atr_value is not None and (not math.isfinite(self.atr_value) or self.atr_value < 0):
            raise ValueError("atr_value must be finite and non-negative")
        if not math.isfinite(self.atr_multiplier) or self.atr_multiplier <= 0:
            raise ValueError("atr_multiplier must be positive")


@dataclass(frozen=True, slots=True)
class P1PortfolioPosition:
    ticker: str
    market_value: float
    data_as_of: date
    industry: str | None
    concepts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.ticker or "").strip():
            raise ValueError("position ticker is required")
        if not math.isfinite(self.market_value) or self.market_value < 0:
            raise ValueError("position market_value must be finite and non-negative")


def _correlation(left: Mapping[str, float], right: Mapping[str, float], lookback: int) -> tuple[float | None, int]:
    common = sorted(set(left) & set(right))[-lookback:]
    if len(common) < 2:
        return None, len(common)
    x = [float(left[item]) for item in common]
    y = [float(right[item]) for item in common]
    if any(not math.isfinite(item) for item in (*x, *y)):
        return None, len(common)
    x_mean, y_mean = statistics.fmean(x), statistics.fmean(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y, strict=True))
    denominator = math.sqrt(sum((item - x_mean) ** 2 for item in x)) * math.sqrt(
        sum((item - y_mean) ** 2 for item in y)
    )
    return (numerator / denominator if denominator > 1e-15 else None), len(common)


def _stop_loss(candidate: P1PortfolioCandidate) -> tuple[float | None, str | None]:
    if candidate.atr_value is None or candidate.atr_unit is None:
        return None, "missing_atr"
    if candidate.atr_unit == "price":
        value = candidate.entry_price - candidate.atr_multiplier * candidate.atr_value
    else:
        value = candidate.entry_price * (1.0 - candidate.atr_multiplier * candidate.atr_value)
    if not math.isfinite(value) or value <= 0 or value >= candidate.entry_price:
        return None, "invalid_atr_stop"
    return value, None


def apply_p1_portfolio_risk_gate(
    candidates: Sequence[P1PortfolioCandidate],
    *,
    existing_positions: Sequence[P1PortfolioPosition],
    returns_by_ticker: Mapping[str, Mapping[str, float]],
    portfolio_nav: float,
    cash: float,
    regime_position_scale: float,
    config: P1PortfolioRiskConfig,
    decision_date: date,
) -> dict:
    if not math.isfinite(portfolio_nav) or portfolio_nav <= 0 or not 0 <= cash <= portfolio_nav:
        raise ValueError("portfolio NAV/cash are invalid")
    if not math.isfinite(regime_position_scale) or not 0 <= regime_position_scale <= 1:
        raise ValueError("regime_position_scale must be in [0, 1]")
    if any(item.data_as_of > decision_date for item in (*candidates, *existing_positions)):
        raise ValueError("candidate and position data_as_of must not be after decision_date")
    for ticker, observations in returns_by_ticker.items():
        for raw_date in observations:
            try:
                return_date = date.fromisoformat(str(raw_date)[:10])
            except ValueError as exc:
                raise ValueError(f"invalid return-history date for {ticker}") from exc
            if return_date > decision_date:
                raise ValueError("return history must not contain post-decision observations")
    tickers = [item.ticker.upper() for item in candidates]
    if len(tickers) != len(set(tickers)):
        raise ValueError("candidate tickers must be unique")
    position_tickers = [item.ticker.upper() for item in existing_positions]
    if len(position_tickers) != len(set(position_tickers)):
        raise ValueError("existing position tickers must be unique")
    current_gross_weight = sum(max(0.0, item.market_value) for item in existing_positions) / portfolio_nav
    new_weight_budget = min(
        config.max_new_gross_weight * regime_position_scale,
        max(0.0, config.max_gross_exposure - current_gross_weight),
        cash / portfolio_nav,
    )
    industry_counts = Counter(
        str(item.industry).strip() for item in existing_positions if str(item.industry or "").strip()
    )
    concept_counts = Counter(
        str(concept).strip()
        for item in existing_positions
        for concept in item.concepts
        if str(concept).strip()
    )
    comparison_tickers = list(position_tickers)
    selected = []
    rejected = []
    audit_flags: list[str] = []
    remaining_weight = new_weight_budget
    lot_size = 100 if config.market.upper() == "CN" else 1
    for index, candidate in enumerate(candidates):
        ticker = candidate.ticker.upper()
        reasons: list[str] = []
        flags: list[str] = []
        industry = str(candidate.industry or "").strip()
        concepts = tuple(sorted({str(item).strip() for item in candidate.concepts if str(item).strip()}))
        if ticker in position_tickers:
            reasons.append("already_held")
        if not industry:
            if config.missing_industry_policy == "BLOCK":
                reasons.append("missing_industry")
            else:
                flags.append("missing_industry_allowed")
        elif industry_counts[industry] >= config.max_industry_names:
            reasons.append("industry_concentration")
        if not concepts:
            if config.missing_concept_policy == "BLOCK":
                reasons.append("missing_concept")
            else:
                flags.append("missing_concept_allowed")
        elif any(concept_counts[value] >= config.max_concept_names for value in concepts):
            reasons.append("concept_concentration")

        candidate_returns = returns_by_ticker.get(ticker)
        for other in comparison_tickers:
            other_returns = returns_by_ticker.get(other)
            if not candidate_returns or not other_returns:
                if config.insufficient_correlation_policy == "BLOCK":
                    reasons.append("missing_correlation_history")
                else:
                    flags.append("missing_correlation_history_allowed")
                break
            correlation, observations = _correlation(
                candidate_returns, other_returns, config.correlation_lookback_sessions,
            )
            if observations < config.minimum_correlation_observations or correlation is None:
                if config.insufficient_correlation_policy == "BLOCK":
                    reasons.append("insufficient_correlation_history")
                else:
                    flags.append("insufficient_correlation_history_allowed")
                break
            if correlation > config.maximum_pair_correlation:
                reasons.append(f"pair_correlation_exceeded:{other}")
                break

        stop_loss, stop_reason = _stop_loss(candidate)
        if stop_reason:
            if config.missing_atr_policy == "BLOCK":
                reasons.append(stop_reason)
            else:
                flags.append(f"{stop_reason}_allowed")
        if remaining_weight <= 0:
            reasons.append("new_position_budget_exhausted")
        if reasons:
            audit_flags.extend(flags)
            rejected.append({
                "ticker": ticker,
                "input_rank": index + 1,
                "reason_codes": sorted(set(reasons)),
                "audit_flags": sorted(set(flags)),
            })
            continue
        remaining_slots = max(1, len(candidates) - index)
        target_weight = min(config.max_position_weight, remaining_weight / remaining_slots)
        target_notional = min(portfolio_nav * target_weight, cash)
        quantity = floor((target_notional / candidate.entry_price) / lot_size) * lot_size
        if quantity <= 0:
            audit_flags.extend(flags)
            rejected.append({
                "ticker": ticker, "input_rank": index + 1,
                "reason_codes": ["insufficient_cash_or_lot_size"],
                "audit_flags": sorted(set(flags)),
            })
            continue
        actual_notional = quantity * candidate.entry_price
        actual_weight = actual_notional / portfolio_nav
        remaining_weight = max(0.0, remaining_weight - actual_weight)
        cash -= actual_notional
        if industry:
            industry_counts[industry] += 1
        concept_counts.update(concepts)
        comparison_tickers.append(ticker)
        audit_flags.extend(flags)
        selected.append({
            "ticker": ticker,
            "input_rank": index + 1,
            "score": candidate.score,
            "industry": industry or None,
            "concepts": list(concepts),
            "target_quantity": quantity,
            "target_notional": actual_notional,
            "target_weight": actual_weight,
            "stop_loss": stop_loss,
            "stop_loss_type": f"atr_{candidate.atr_unit}",
            "atr_multiplier": candidate.atr_multiplier,
            "audit_flags": sorted(set(flags)),
        })
    return {
        "schema_version": "p1_portfolio_risk_gate_result_v1",
        "config_version": config.version(),
        "market": config.market.upper(),
        "decision_date": decision_date.isoformat(),
        "status": "READY_RESEARCH_ONLY",
        "semantics": "research_allocation_not_trade_authorization",
        "existing_position_count": len(existing_positions),
        "input_candidate_count": len(candidates),
        "selected_count": len(selected),
        "rejected_count": len(rejected),
        "current_gross_weight": current_gross_weight,
        "regime_position_scale": regime_position_scale,
        "initial_new_weight_budget": new_weight_budget,
        "remaining_new_weight_budget": remaining_weight,
        "remaining_cash": cash,
        "selected": selected,
        "rejected": rejected,
        "audit_flags": sorted(set(audit_flags)),
    }


__all__ = [
    "P1PortfolioCandidate",
    "P1PortfolioPosition",
    "P1PortfolioRiskConfig",
    "apply_p1_portfolio_risk_gate",
]
