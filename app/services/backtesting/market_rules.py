from __future__ import annotations

from app.services.backtesting.schemas import DailyBar


def cn_price_limit_pct(ticker: str, *, is_st: bool = False) -> float:
    code = str(ticker or "").split(".", 1)[0]
    if str(ticker or "").upper().endswith(".BJ"):
        return 0.30
    if code.startswith(("300", "301", "688")):
        return 0.20
    if is_st:
        return 0.05
    return 0.10


def entry_reject_reason(
    bar: DailyBar | None,
    *,
    market: str,
    min_adv: float,
    max_gap_pct: float,
) -> str | None:
    if bar is None:
        return "missing_market_bar"
    if bar.open <= 0:
        return "missing_open"
    if bar.volume <= 0:
        return "suspended_or_no_volume"
    if bar.adv20 < max(0.0, min_adv):
        return "adv_below_min"
    if bar.previous_close and bar.previous_close > 0:
        gap_pct = (bar.open / bar.previous_close) - 1.0
        if abs(gap_pct) > max(0.0, max_gap_pct):
            return "gap_exceeded"
        if str(market or "").upper() == "CN":
            limit_price = bar.previous_close * (
                1.0 + cn_price_limit_pct(bar.ticker, is_st=bar.is_st)
            )
            if bar.open >= limit_price * (1.0 - 1e-4):
                return "cn_limit_up_buy_blocked"
    return None


def exit_reject_reason(bar: DailyBar | None, *, market: str) -> str | None:
    if bar is None:
        return "missing_market_bar"
    if bar.close <= 0:
        return "missing_close"
    if bar.volume <= 0:
        return "suspended_or_no_volume"
    if str(market or "").upper() == "CN" and bar.previous_close and bar.previous_close > 0:
        limit_price = bar.previous_close * (
            1.0 - cn_price_limit_pct(bar.ticker, is_st=bar.is_st)
        )
        one_price = abs(bar.high - bar.low) <= max(1e-8, abs(bar.close) * 1e-8)
        if one_price and bar.close <= limit_price * (1.0 + 1e-4):
            return "cn_limit_down_sell_blocked"
    return None
