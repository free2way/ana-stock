from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from typing import Sequence


@dataclass(frozen=True, slots=True)
class PriceBar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.open, self.high, self.low, self.close, self.volume)):
            raise ValueError("OHLCV must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be greater than or equal to OHLC values")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must be less than or equal to OHLC values")
        if self.volume < 0:
            raise ValueError("volume must not be negative")


@dataclass(frozen=True, slots=True)
class ExecutableLabel:
    signal_date: date
    entry_date: date
    exit_date: date
    label_available_date: date
    horizon_days: int
    entry_price: float | None
    exit_price: float | None
    gross_return: float | None
    net_return: float | None
    market_excess_return: float | None
    industry_excess_return: float | None
    path_drawdown: float | None
    risk_adjusted_return: float | None
    round_trip_cost_bps: float
    tradable: bool
    is_profitable: bool | None
    exclusion_reason: str | None = None
    label_version: str = "next_open_industry_excess_dd_v1"
    cost_model_version: str = "flat_round_trip_bps_v1"
    cost_model_hash: str | None = None


def _validate_bars(bars: Sequence[PriceBar]) -> None:
    if not bars:
        raise ValueError("bars must not be empty")
    dates = [bar.trade_date for bar in bars]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("bars must have unique ascending trade dates")


def build_executable_label(
    bars: Sequence[PriceBar],
    *,
    signal_index: int,
    horizon_days: int,
    round_trip_cost_bps: float = 20.0,
    market_return: float = 0.0,
    industry_return: float = 0.0,
    drawdown_penalty: float = 0.25,
    entry_is_executable: bool = True,
    exclusion_reason: str | None = None,
    corporate_action_jump_threshold: float = 0.80,
) -> ExecutableLabel:
    """Build a D-close signal label using D+1 open through D+h close.

    `horizon_days=1` enters at the next session open and exits at that same
    session close. The label only becomes available at the exit close.
    """

    _validate_bars(bars)
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if signal_index < 0 or signal_index >= len(bars):
        raise IndexError("signal_index is outside bars")
    exit_index = signal_index + horizon_days
    entry_index = signal_index + 1
    if exit_index >= len(bars):
        raise ValueError("insufficient future bars for requested horizon")
    if not all(math.isfinite(value) for value in (
        round_trip_cost_bps, drawdown_penalty, market_return, industry_return,
        corporate_action_jump_threshold,
    )):
        raise ValueError("label parameters must be finite")
    if corporate_action_jump_threshold <= 0:
        raise ValueError("corporate_action_jump_threshold must be positive")
    if round_trip_cost_bps < 0 or drawdown_penalty < 0:
        raise ValueError("cost and drawdown penalty must not be negative")
    if not entry_is_executable and not exclusion_reason:
        raise ValueError("non-executable entry must provide exclusion_reason")
    if entry_is_executable and exclusion_reason:
        raise ValueError("executable entry cannot provide exclusion_reason")

    entry_bar = bars[entry_index]
    exit_bar = bars[exit_index]
    path = bars[entry_index : exit_index + 1]

    if not entry_is_executable:
        return ExecutableLabel(
            signal_date=bars[signal_index].trade_date,
            entry_date=entry_bar.trade_date,
            exit_date=exit_bar.trade_date,
            label_available_date=exit_bar.trade_date,
            horizon_days=horizon_days,
            entry_price=None,
            exit_price=None,
            gross_return=None,
            net_return=None,
            market_excess_return=None,
            industry_excess_return=None,
            path_drawdown=None,
            risk_adjusted_return=None,
            round_trip_cost_bps=round_trip_cost_bps,
            tradable=False,
            is_profitable=None,
            exclusion_reason=exclusion_reason,
        )

    for previous, current in zip(path, path[1:], strict=False):
        close_change = (current.close / previous.close) - 1.0
        if abs(close_change) >= corporate_action_jump_threshold:
            return ExecutableLabel(
                signal_date=bars[signal_index].trade_date,
                entry_date=entry_bar.trade_date,
                exit_date=exit_bar.trade_date,
                label_available_date=exit_bar.trade_date,
                horizon_days=horizon_days,
                entry_price=None,
                exit_price=None,
                gross_return=None,
                net_return=None,
                market_excess_return=None,
                industry_excess_return=None,
                path_drawdown=None,
                risk_adjusted_return=None,
                round_trip_cost_bps=round_trip_cost_bps,
                tradable=False,
                is_profitable=None,
                exclusion_reason="suspected_corporate_action_discontinuity",
            )

    gross_return = (exit_bar.close / entry_bar.open) - 1.0
    net_return = gross_return - (round_trip_cost_bps / 10_000.0)
    market_excess_return = net_return - market_return
    industry_excess_return = net_return - industry_return
    path_drawdown = min((bar.low / entry_bar.open) - 1.0 for bar in path)
    risk_adjusted_return = industry_excess_return - drawdown_penalty * abs(min(path_drawdown, 0.0))

    return ExecutableLabel(
        signal_date=bars[signal_index].trade_date,
        entry_date=entry_bar.trade_date,
        exit_date=exit_bar.trade_date,
        label_available_date=exit_bar.trade_date,
        horizon_days=horizon_days,
        entry_price=entry_bar.open,
        exit_price=exit_bar.close,
        gross_return=gross_return,
        net_return=net_return,
        market_excess_return=market_excess_return,
        industry_excess_return=industry_excess_return,
        path_drawdown=path_drawdown,
        risk_adjusted_return=risk_adjusted_return,
        round_trip_cost_bps=round_trip_cost_bps,
        tradable=entry_is_executable,
        is_profitable=net_return > 0.0,
        exclusion_reason=exclusion_reason,
    )
