"""Reconcile idiosyncratic raw-price jumps against corporate actions (C2).

A candidate is a stock-day whose return deviates from the same-day
cross-sectional median by more than the board band (minus the 0.2pp tolerance).
Days where the whole market moved beyond +/-3% (crash/rally) are recorded as
systemic and excluded from attribution: no corporate action explains a market
move, and treating them as candidates only inflates the denominator.

When an adjusted view exists the same scan runs on it as a correctness check:
explained jumps must disappear from the adjusted series.

US: a 50% deviation threshold is used as a diagnostic because the market has no
price band; the US adjusted view stays blocked until the price basis is fixed.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from app.services.backtesting.market_rules import cn_price_limit_pct  # noqa: E402
from app.services.corporate_actions import explains_close_jump, load_actions  # noqa: E402
from app.services.market_lake import market_lake_root  # noqa: E402

BAND_EXCESS_MARGIN = 0.005  # rounding/ST slack on top of the nominal band
SYSTEMIC_MEDIAN_LIMIT = 0.03  # |market median| beyond this marks a systemic day


def _series_from_parquet(pattern: str, *, where: str = "close IS NOT NULL AND close > 0") -> dict[str, list[tuple[str, float]]]:
    rows = duckdb.sql(
        f"SELECT symbol, CAST(date AS VARCHAR) AS date, close FROM read_parquet(?, hive_partitioning=true) "
        f"WHERE {where} ORDER BY symbol, date",
        params=[pattern],
    ).fetchall()
    series: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for symbol, trade_date, close in rows:
        series[str(symbol).upper()].append((str(trade_date)[:10], float(close)))
    return series


def _raw_series(market: str, *, raw_glob: str | None = None) -> dict[str, list[tuple[str, float]]]:
    pattern = raw_glob or str(market_lake_root() / f"{market.lower()}_daily" / "date=*" / "*.parquet")
    return _series_from_parquet(pattern)


def _half_up_tick(value: float) -> float:
    """Exchange limit prices round half-up to 0.01 CNY; Python's round() does not."""

    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _actions_near(
    actions_by_symbol_date: dict[tuple[str, str], list],
    symbol: str,
    trade_date: str,
    *,
    window_days: int,
) -> list:
    if window_days <= 0:
        return actions_by_symbol_date.get((symbol, trade_date), [])
    try:
        center = date.fromisoformat(trade_date)
    except ValueError:
        return []
    matches: list = []
    for offset in range(-window_days, window_days + 1):
        matches.extend(actions_by_symbol_date.get((symbol, (center + timedelta(days=offset)).isoformat()), []))
    return matches


def _adjusted_series(market: str, method: str) -> dict[str, list[tuple[str, float]]] | None:
    path = market_lake_root() / "_adjusted_v2" / market.lower() / f"method={method}" / "adjusted.parquet"
    if not path.exists():
        return None
    return _series_from_parquet(str(path))


def _market_medians(series: dict[str, list[tuple[str, float]]]) -> dict[str, float]:
    returns: dict[str, list[float]] = defaultdict(list)
    for points in series.values():
        for (_, previous_close), (trade_date, close) in zip(points, points[1:]):
            if previous_close > 0:
                returns[trade_date].append(close / previous_close - 1.0)
    return {day: statistics.median(values) for day, values in returns.items() if values}


def scan_series(
    series: dict[str, list[tuple[str, float]]],
    *,
    market: str,
    us_threshold: float,
    actions_by_symbol_date: dict[tuple[str, str], list],
    medians: dict[str, float],
    market_days: list[str] | None = None,
    sample_limit: int = 25,
) -> dict:
    """Raw candidates: moves strictly beyond the exchange's rounded limit price."""

    day_index = {day: index for index, day in enumerate(market_days)} if market_days else None
    total = 0
    explained = 0
    unexplained = 0
    event_window_jumps = 0
    event_window_explained = 0
    event_window_unexplained_samples: list[dict] = []
    non_event_unexplained = 0
    systemic_days: set[str] = set()
    excluded_resumption = 0
    excluded_short_history = 0
    excluded_lake_gaps = 0
    samples: list[dict] = []
    candidate_points: list[tuple[str, str]] = []
    for symbol, points in series.items():
        for index, ((previous_date, previous_close), (trade_date, close)) in enumerate(zip(points, points[1:])):
            if previous_close <= 0:
                continue
            median = medians.get(trade_date)
            if median is None:
                continue
            if abs(median) > SYSTEMIC_MEDIAN_LIMIT:
                systemic_days.add(trade_date)
                continue
            band = cn_price_limit_pct(symbol) if market == "CN" else us_threshold
            change = (close / previous_close) - 1.0
            if market == "CN":
                # Exchange limits are rounded to a tick (0.01 CNY), so compare
                # against the rounded limit prices instead of a percentage
                # threshold: a low-priced stock's ordinary limit-up can move
                # more than 10.5% purely from rounding.
                limit_up = _half_up_tick(previous_close * (1.0 + band))
                limit_down = _half_up_tick(previous_close * (1.0 - band))
                beyond_band = close > limit_up + 1e-9 or close < limit_down - 1e-9
            else:
                beyond_band = abs(change - median) > band + BAND_EXCESS_MARGIN
            if not beyond_band:
                continue
            if index + 1 < 60:
                excluded_short_history += 1
                continue
            try:
                gap_days = (date.fromisoformat(trade_date) - date.fromisoformat(previous_date)).days
            except ValueError:
                gap_days = 99
            if gap_days > 5:
                excluded_resumption += 1
                continue
            if day_index is not None:
                previous_index = day_index.get(previous_date)
                if previous_index is None or previous_index + 1 >= len(market_days) or market_days[previous_index + 1] != trade_date:
                    # The pair spans a missing market session (lake hole); the
                    # "return" is then a multi-session move, not a band break.
                    excluded_lake_gaps += 1
                    continue
            total += 1
            candidate_points.append((symbol, trade_date))
            # OTC reverse splits often reprice a session before their
            # ex/effective date, so US splits are matched within +/-3 days.
            matches = _actions_near(
                actions_by_symbol_date,
                symbol,
                trade_date,
                window_days=0 if market == "CN" else 3,
            )
            in_event_window = bool(matches)
            if in_event_window:
                event_window_jumps += 1
            if matches and explains_close_jump(
                previous_close=previous_close,
                close=close,
                actions=matches,
                symbol=symbol,
                band=cn_price_limit_pct(symbol) if market == "CN" else us_threshold,
            ):
                explained += 1
                if in_event_window:
                    event_window_explained += 1
                continue
            unexplained += 1
            if not in_event_window:
                non_event_unexplained += 1
            elif len(event_window_unexplained_samples) < 50:
                event_window_unexplained_samples.append(
                    {
                        "symbol": symbol,
                        "date": trade_date,
                        "previous_close": previous_close,
                        "close": close,
                        "change_pct": round(change * 100.0, 3),
                        "actions": [
                            {
                                "action_type": record.action_type,
                                "effective_date": record.effective_date.isoformat(),
                                "factor": record.factor,
                                "cash_amount": record.cash_amount,
                                "source": record.source,
                            }
                            for record in matches
                        ],
                    }
                )
            if len(samples) < sample_limit:
                samples.append(
                    {
                        "symbol": symbol,
                        "date": trade_date,
                        "previous_close": previous_close,
                        "close": close,
                        "change_pct": round(change * 100.0, 3),
                        "market_median_pct": round(median * 100.0, 3),
                        "actions_on_date": [record.action_type for record in matches],
                    }
                )
    return {
        "jumps": total,
        "explained": explained,
        "unexplained": unexplained,
        "explained_rate": (explained / total) if total else None,
        "event_window_jumps": event_window_jumps,
        "event_window_explained": event_window_explained,
        "event_window_unexplained": event_window_jumps - event_window_explained,
        "event_window_unexplained_samples": event_window_unexplained_samples,
        "non_event_unexplained": non_event_unexplained,
        "systemic_days_excluded": len(systemic_days),
        "systemic_samples": sorted(systemic_days)[:10],
        "excluded_resumption_gaps": excluded_resumption,
        "excluded_short_history": excluded_short_history,
        "excluded_lake_gaps": excluded_lake_gaps,
        "unexplained_samples": samples,
        "candidate_points": candidate_points,
    }


def scan_adjusted_residuals(
    adjusted: dict[str, list[tuple[str, float]]],
    candidate_points: list[tuple[str, str]],
    *,
    market: str,
    us_threshold: float,
    sample_limit: int = 25,
) -> dict:
    """Do the raw candidates still break the band after adjustment?

    The adjusted series is not tick-quantised, so this check uses a percentage
    band plus the same margin instead of rounded limit prices.
    """

    lookup = {symbol: dict(points) for symbol, points in adjusted.items()}
    previous_lookup = {
        symbol: {date: points[index - 1][1] for index, (date, _) in enumerate(points) if index > 0}
        for symbol, points in adjusted.items()
    }
    residuals = 0
    samples: list[dict] = []
    matched = 0
    for symbol, trade_date in candidate_points:
        closes = lookup.get(symbol)
        if not closes or trade_date not in closes:
            continue
        previous_close = previous_lookup.get(symbol, {}).get(trade_date)
        close = closes[trade_date]
        if not previous_close or previous_close <= 0 or close is None:
            continue
        matched += 1
        band = cn_price_limit_pct(symbol) if market == "CN" else us_threshold
        change = (close / previous_close) - 1.0
        if abs(change) <= band + BAND_EXCESS_MARGIN:
            continue
        residuals += 1
        if len(samples) < sample_limit:
            samples.append(
                {
                    "symbol": symbol,
                    "date": trade_date,
                    "adjusted_change_pct": round(change * 100.0, 3),
                }
            )
    return {
        "candidates_checked": matched,
        "residuals": residuals,
        "residual_samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["CN", "US"], required=True)
    parser.add_argument("--method", default="qfq")
    parser.add_argument("--us-threshold", type=float, default=0.50)
    parser.add_argument("--raw-glob", default=None, help="override the raw scan pattern (e.g. Alpaca namespace)")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    actions = load_actions(args.market)
    actions_by_symbol_date: dict[tuple[str, str], list] = defaultdict(list)
    for action in actions:
        actions_by_symbol_date[(action.symbol, action.effective_date.isoformat())].append(action)

    raw = _raw_series(args.market, raw_glob=args.raw_glob)
    market_days = sorted({day for points in raw.values() for day, _ in points})
    raw_medians = _market_medians(raw)
    raw_scan = scan_series(
        raw,
        market=args.market,
        us_threshold=args.us_threshold,
        actions_by_symbol_date=actions_by_symbol_date,
        medians=raw_medians,
        market_days=market_days,
    )
    payload = {
        "schema_version": "adjusted_jump_reconciliation_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "market": args.market,
        "method": args.method,
        "actions_loaded": len(actions),
        "raw_scan": raw_scan,
    }
    adjusted = _adjusted_series(args.market, args.method)
    if adjusted is not None:
        payload["adjusted_scan"] = scan_adjusted_residuals(
            adjusted,
            raw_scan["candidate_points"],
            market=args.market,
            us_threshold=args.us_threshold,
        )
    else:
        payload["adjusted_scan"] = {"status": "view_missing"}
    payload["raw_scan"].pop("candidate_points", None)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    adjusted_summary = payload["adjusted_scan"]
    print(json.dumps({
        "market": args.market,
        "actions": len(actions),
        "raw_jumps": raw_scan["jumps"],
        "raw_explained": raw_scan["explained"],
        "raw_unexplained": raw_scan["unexplained"],
        "event_window_jumps": raw_scan["event_window_jumps"],
        "event_window_unexplained": raw_scan["event_window_unexplained"],
        "non_event_unexplained": raw_scan["non_event_unexplained"],
        "systemic_days_excluded": raw_scan["systemic_days_excluded"],
        "adjusted": adjusted_summary if adjusted_summary == {"status": "view_missing"} else {
            "candidates_checked": adjusted_summary["candidates_checked"],
            "residuals": adjusted_summary["residuals"],
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
