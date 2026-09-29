"""Common opt-in trainer/evaluator adapter for confirmed next-open outcomes.

Execution eligibility is an explicit input from the reality model, never an
assumption derived from a positive future return. This is label evidence, not
an account simulator or a claim that a real trade was filled.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Sequence
import math

from app.services.stock_selection.labels import PriceBar, ExecutableLabel, build_executable_label
from app.services.execution_costs import FillCostModel


OUTCOME_VERSION = "confirmed_next_open_fixed_exit_v1"
FILL_COST_OUTCOME_VERSION = "confirmed_next_open_fixed_exit_fill_cost_v2"


@dataclass(frozen=True)
class ExecutionEligibility:
    entry_allowed: bool | None
    exit_allowed: bool | None
    reason: str | None = None


def limit_up_at_open(
    open_price: float, previous_close: float, threshold: float | None = None
) -> bool:
    """Reality-model gate: a limit-up open cannot be bought at the T+1 auction.

    Under the CN price-limit regime, an open already at (or within the quote
    tick of) the limit-up price means the buy queue is sealed; assuming a fill
    at that open systematically overstates backtested entry prices. ``threshold``
    is the entry band as a fraction (e.g. 0.098 for a 10% band with tolerance);
    ``None`` falls back to a conservative 9.8% band. Invalid or non-positive
    prices never classify as a limit-up open (they fail elsewhere).
    """
    if threshold is not None and (not math.isfinite(threshold) or threshold < 0):
        raise ValueError("threshold must be finite and non-negative")
    if (
        not math.isfinite(open_price)
        or not math.isfinite(previous_close)
        or previous_close <= 0
        or open_price <= 0
    ):
        return False
    band = 0.098 if threshold is None else threshold
    return open_price >= previous_close * (1.0 + band)


def confirmed_outcome(
    bars: Sequence[PriceBar], *, signal_date: date, trading_dates: Sequence[date],
    horizon_days: int, market: str, cost_bps: float | None = None,
    eligibility: ExecutionEligibility,
    industry_return: float = 0.0, market_return: float = 0.0,
    cost_model: FillCostModel | None = None,
) -> ExecutableLabel | None:
    if (cost_model is None) == (cost_bps is None):
        raise ValueError("choose exactly one cost model: flat bps or per-fill")
    if cost_bps is not None and (not math.isfinite(cost_bps) or cost_bps < 0):
        raise ValueError("cost_bps must be finite and non-negative")
    if market not in {"CN", "US", "HK"}:
        raise ValueError("explicit market required")
    if horizon_days < 1 or (market == "CN" and horizon_days == 1):
        raise ValueError("holding horizon incompatible with market execution contract")
    dates = list(trading_dates)
    if dates != sorted(set(dates)):
        raise ValueError("trading calendar must be unique and ascending")
    if signal_date not in dates:
        raise ValueError("signal date missing from trading calendar")
    start = dates.index(signal_date)
    if start + horizon_days >= len(dates):
        return None
    wanted = dates[start:start + horizon_days + 1]
    by_date = {bar.trade_date: bar for bar in bars}
    if len(by_date) != len(bars):
        raise ValueError("duplicate price date")
    if any(day not in by_date for day in wanted):
        raise ValueError("missing_price_path")
    reason = None
    if eligibility.entry_allowed is not True:
        reason = eligibility.reason or ("entry_unknown" if eligibility.entry_allowed is None else "entry_not_executable")
    elif eligibility.exit_allowed is not True:
        reason = eligibility.reason or ("exit_unknown" if eligibility.exit_allowed is None else "exit_deferred")
    result = build_executable_label(
        [by_date[day] for day in wanted], signal_index=0, horizon_days=horizon_days,
        round_trip_cost_bps=cost_bps if cost_model is None else 0.0, market_return=market_return,
        industry_return=industry_return, drawdown_penalty=0.0,
        entry_is_executable=reason is None, exclusion_reason=reason,
    )
    if cost_model is None:
        return replace(result, label_version=OUTCOME_VERSION)
    result = replace(result, label_version=FILL_COST_OUTCOME_VERSION,
                     cost_model_version=cost_model.version, cost_model_hash=cost_model.model_hash,
                     round_trip_cost_bps=cost_model.nominal_round_trip_bps)
    if not result.tradable:
        return result
    # Labels keep raw reference prices and raw gross return. Net uses actual
    # per-fill notional fees and entry cash outlay, identical to the engine.
    net = cost_model.round_trip(result.entry_price, result.exit_price)["net_return"]
    return replace(result, net_return=net, market_excess_return=net - market_return,
                   industry_excess_return=net - industry_return,
                   risk_adjusted_return=net - industry_return, is_profitable=net > 0)
