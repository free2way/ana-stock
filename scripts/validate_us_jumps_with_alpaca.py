"""Classify US reconciliation candidates against Alpaca single-basis raw bars.

For every unexplained candidate (symbol, date) from a US reconciliation report,
re-fetch that symbol's raw daily bars from Alpaca and compare the return:

* ``single_basis_move``  - Alpaca shows a similar (> threshold/2) move: the jump
  is a real market move or an action not yet modelled, not a lake artifact.
* ``lake_artifact``      - Alpaca shows a small move while the lake shows a big
  one: the legacy lake's mixed price basis produced the jump.
* ``unverifiable``       - Alpaca has no bar on that date.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.alpaca_client import AlpacaClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconcile-report", required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    from app.core.config import get_settings

    payload = json.loads(Path(args.reconcile_report).read_text(encoding="utf-8"))
    samples = payload["raw_scan"]["unexplained_samples"][: args.limit]
    settings = get_settings()
    client = AlpacaClient(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        trading_endpoint=settings.alpaca_endpoint,
        data_endpoint=settings.alpaca_data_endpoint,
        feed=settings.alpaca_data_feed,
    )

    cache: dict[str, list[dict]] = {}
    results = []
    for sample in samples:
        symbol = sample["symbol"]
        if symbol not in cache:
            try:
                cache[symbol] = client.fetch_daily_bars(symbol, start="2024-12-01", adjustment="raw")
            except Exception as exc:
                cache[symbol] = []
                sample = {**sample, "alpaca_error": f"{type(exc).__name__}: {exc}"}
        bars = cache[symbol]
        closes = {bar["date"]: bar["close"] for bar in bars}
        dates = sorted(closes)
        trade_date = sample["date"]
        classification = "unverifiable"
        alpaca_change = None
        if trade_date in closes:
            index = dates.index(trade_date)
            if index > 0:
                previous = closes[dates[index - 1]]
                close = closes[trade_date]
                if previous > 0:
                    alpaca_change = close / previous - 1.0
                    if abs(alpaca_change) >= args.threshold / 2:
                        classification = "single_basis_move"
                    else:
                        classification = "lake_artifact"
        results.append(
            {
                **sample,
                "alpaca_previous_close": closes.get(dates[dates.index(trade_date) - 1]) if trade_date in closes and dates.index(trade_date) > 0 else None,
                "alpaca_close": closes.get(trade_date),
                "alpaca_change_pct": round(alpaca_change * 100.0, 3) if alpaca_change is not None else None,
                "classification": classification,
            }
        )
    counts: dict[str, int] = {}
    for item in results:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
    output = {
        "schema_version": "us_alpaca_jump_validation_v1",
        "reconcile_report": args.reconcile_report,
        "threshold": args.threshold,
        "checked": len(results),
        "counts": counts,
        "results": results,
    }
    Path(args.output).write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"checked": len(results), "counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
