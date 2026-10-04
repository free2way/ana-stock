"""C-7 evidence: deterministic manual spot-check table (20 symbols x 5 events).

For each sampled (symbol, ex-date) the script prints the raw bars, the action
(factor / cash), the expected multiplier step and the observed multiplier step,
plus an automatic pass/fail. The human reviewer verifies the sample and signs
the CSV/JSON; the auto-check keeps the reviewer's effort bounded.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from app.services.corporate_actions import load_actions  # noqa: E402

RAW_PATTERNS = {
    "CN": str(ROOT / "data" / "lake" / "cn_daily" / "date=*" / "*.parquet"),
    "US": str(ROOT / "data" / "lake" / "_us_alpaca" / "raw" / "*.parquet"),
}


def _adjusted_pattern(market: str) -> str:
    return str(ROOT / "data" / "lake" / "_adjusted_v2" / market.lower() / "method=qfq" / "adjusted.parquet")


def _sample_symbols(market: str, count: int, events_per_symbol: int) -> list[str]:
    actions = load_actions(market)
    by_symbol: dict[str, set[str]] = {}
    for action in actions:
        if action.action_type not in {"split", "stock_dividend", "cash_dividend", "adjustment_factor"}:
            continue
        if action.effective_date.year < 2025:
            continue
        by_symbol.setdefault(action.symbol, set()).add(action.effective_date.isoformat())
    eligible = sorted(symbol for symbol, dates in by_symbol.items() if len(dates) >= events_per_symbol)
    def weight(symbol: str) -> str:
        return hashlib.sha256(f"c7:{market}:{symbol}".encode("utf-8")).hexdigest()
    eligible.sort(key=weight)
    return eligible[:count]


def build_rows(market: str, symbol: str) -> list[dict]:
    actions = [a for a in load_actions(market) if a.symbol == symbol]
    ex_dates = sorted({a.effective_date.isoformat() for a in actions if a.effective_date.year >= 2025})
    raw = duckdb.sql(
        """
        SELECT CAST(date AS VARCHAR) AS d, close FROM read_parquet(?) WHERE symbol = ? ORDER BY date
        """,
        params=[RAW_PATTERNS[market], symbol],
    ).fetchall()
    adjusted = duckdb.sql(
        """
        SELECT CAST(date AS VARCHAR) AS d, close FROM read_parquet(?) WHERE symbol = ? ORDER BY date
        """,
        params=[_adjusted_pattern(market), symbol],
    ).fetchall()
    raw_map = {row[0]: float(row[1]) for row in raw}
    adj_map = {row[0]: float(row[1]) for row in adjusted}
    raw_dates = sorted(raw_map)
    adj_dates = sorted(adj_map)
    rows: list[dict] = []
    for ex_date in ex_dates:
        if ex_date not in raw_map or ex_date not in adj_map:
            continue
        raw_index = raw_dates.index(ex_date)
        adj_index = adj_dates.index(ex_date)
        if raw_index == 0 or adj_index == 0:
            continue
        raw_prev_date = raw_dates[raw_index - 1]
        adj_prev_date = adj_dates[adj_index - 1]
        raw_prev, raw_close = raw_map[raw_prev_date], raw_map[ex_date]
        adj_prev, adj_close = adj_map[adj_prev_date], adj_map[ex_date]
        day_actions = [a for a in actions if a.effective_date.isoformat() == ex_date]
        expected = 1.0
        summary = []
        for action in day_actions:
            if action.action_type in {"split", "stock_dividend", "rights", "adjustment_factor"} and action.factor:
                expected *= float(action.factor)
                summary.append(f"{action.action_type}:factor={action.factor:g}")
            elif action.action_type == "cash_dividend" and action.cash_amount is not None:
                cash = float(action.cash_amount)
                if 0 < cash < raw_prev:
                    expected *= raw_prev / (raw_prev - cash)
                    summary.append(f"cash={cash:g}")
        observed = (adj_close / raw_close) / (adj_prev / raw_prev)
        deviation = abs(observed / expected - 1.0) if expected > 0 else float("inf")
        rows.append(
            {
                "market": market,
                "symbol": symbol,
                "ex_date": ex_date,
                "actions": ";".join(summary),
                "raw_prev": round(raw_prev, 6),
                "raw_close": round(raw_close, 6),
                "adj_prev": round(adj_prev, 6),
                "adjusted_close": round(adj_close, 6),
                "expected_multiplier_step": round(expected, 8),
                "observed_multiplier_step": round(observed, 8),
                "deviation": deviation,
                "auto_check": "pass" if deviation < 1e-3 else "FAIL",
                "reviewer_note": "",
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols-per-market", type=int, default=10)
    parser.add_argument("--events-per-symbol", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows: list[dict] = []
    sources: dict[str, list[str]] = {}
    for market in ("CN", "US"):
        candidates = _sample_symbols(market, args.symbols_per_market * 8, args.events_per_symbol)
        chosen: list[str] = []
        for symbol in candidates:
            symbol_rows = build_rows(market, symbol)[: args.events_per_symbol]
            if len(symbol_rows) < args.events_per_symbol:
                continue
            chosen.append(symbol)
            rows.extend(symbol_rows)
            if len(chosen) >= args.symbols_per_market:
                break
        sources[market] = chosen
    passed = sum(1 for row in rows if row["auto_check"] == "pass")
    payload = {
        "schema_version": "manual_spotcheck_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "markets": sources,
        "symbols": len(sources.get("CN", [])) + len(sources.get("US", [])),
        "events": len(rows),
        "auto_pass": passed,
        "auto_fail": len(rows) - passed,
        "rows": rows,
        "signoff": {"reviewer": "", "date": "", "conclusion": ""},
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    csv_path = output.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["market"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({
        "symbols": payload["symbols"],
        "events": payload["events"],
        "auto_pass": passed,
        "auto_fail": payload["auto_fail"],
        "output": str(output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
