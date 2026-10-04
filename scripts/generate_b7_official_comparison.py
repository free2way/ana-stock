"""B-7 evidence: compare stored corporate actions against the upstream official data.

For a deterministic sample from the C-7 spot-check table, re-fetch the upstream
record (CN: Eastmoney fhps detail; US: Alpaca corporate-action announcements)
and compare the stored factor / cash with the official value.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import akshare as ak  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.alpaca_client import AlpacaClient  # noqa: E402
from app.services.corporate_actions import load_actions  # noqa: E402


def _cn_official(symbol: str, ex_date: str) -> dict | None:
    code = symbol.split(".", 1)[0]
    frame = ak.stock_fhps_detail_em(symbol=code)
    if frame is None or not len(frame):
        return None
    for record in frame.to_dict("records"):
        if str(record.get("除权除息日") or "")[:10] != ex_date:
            continue

        def _number(value):
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if parsed == parsed else None  # NaN guard

        return {
            "factor": (1.0 + _number(record.get("送转股份-送转总比例")) / 10.0)
            if _number(record.get("送转股份-送转总比例"))
            else None,
            "cash": (_number(record.get("现金分红-现金分红比例")) / 10.0)
            if _number(record.get("现金分红-现金分红比例"))
            else None,
            "source": "eastmoney:stock_fhps_detail_em",
        }
    return None


def _us_official(client: AlpacaClient, symbol: str, ex_date: str) -> dict | None:
    center = date.fromisoformat(ex_date)
    records = client.list_corporate_actions(
        start=(center - timedelta(days=60)).isoformat(),
        end=(center + timedelta(days=60)).isoformat(),
        ca_types=("split", "dividend"),
    )
    for record in records:
        target = str(record.get("target_symbol") or "").upper()
        recorded_date = str(record.get("ex_date") or record.get("effective_date") or "")[:10]
        if target != symbol.upper() or recorded_date != ex_date:
            continue
        ca_type = str(record.get("ca_type") or "").lower()
        if ca_type == "split":
            try:
                factor = float(record.get("new_rate")) / float(record.get("old_rate"))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            return {"factor": factor, "cash": None, "source": f"alpaca:ca:{record.get('id')}"}
        if ca_type == "dividend":
            try:
                cash = float(record.get("cash") or 0.0)
            except (TypeError, ValueError):
                continue
            return {"factor": None, "cash": cash, "source": f"alpaca:ca:{record.get('id')}"}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spotcheck", required=True)
    parser.add_argument("--symbols-per-market", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    spotcheck = json.loads(Path(args.spotcheck).read_text(encoding="utf-8"))
    rows_by_symbol: dict[str, list[dict]] = {}
    for row in spotcheck["rows"]:
        rows_by_symbol.setdefault(f"{row['market']}:{row['symbol']}", []).append(row)

    settings = get_settings()
    client = AlpacaClient(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        trading_endpoint=settings.alpaca_endpoint,
        data_endpoint=settings.alpaca_data_endpoint,
        feed=settings.alpaca_data_feed,
    )
    actions_by_key = {}
    for market in ("CN", "US"):
        for action in load_actions(market):
            actions_by_key[(market, action.symbol, action.effective_date.isoformat(), action.action_type)] = action

    results: list[dict] = []
    for market in ("CN", "US"):
        symbols = [item for item in spotcheck["markets"][market]][: args.symbols_per_market]
        for symbol in symbols:
            symbol_rows = rows_by_symbol[f"{market}:{symbol}"]
            # B-7 asks for splits/bonus as well as cash: prefer a factor event
            # when the symbol has one in the sample.
            sample = next((row for row in symbol_rows if "factor=" in str(row.get("actions") or "")), symbol_rows[0])
            ex_date = sample["ex_date"]
            official = _cn_official(symbol, ex_date) if market == "CN" else _us_official(client, symbol, ex_date)
            stored_factor = stored_cash = None
            for (mkt, sym, eff, _type), action in actions_by_key.items():
                if mkt != market or sym != symbol or eff != ex_date:
                    continue
                if action.factor is not None:
                    stored_factor = action.factor
                if action.cash_amount is not None:
                    stored_cash = action.cash_amount
            factor_match = (
                stored_factor is not None
                and official is not None
                and official.get("factor") is not None
                and abs(float(stored_factor) - float(official["factor"])) < 1e-6
            )
            cash_match = (
                stored_cash is not None
                and official is not None
                and official.get("cash") is not None
                and abs(float(stored_cash) - float(official["cash"])) < 1e-6
            )
            results.append(
                {
                    "market": market,
                    "symbol": symbol,
                    "ex_date": ex_date,
                    "stored_factor": stored_factor,
                    "official_factor": (official or {}).get("factor"),
                    "stored_cash": stored_cash,
                    "official_cash": (official or {}).get("cash"),
                    "official_source": (official or {}).get("source"),
                    "match": bool(factor_match or cash_match),
                }
            )

    payload = {
        "schema_version": "b7_official_comparison_v1",
        "spotcheck": args.spotcheck,
        "checked": len(results),
        "matched": sum(1 for row in results if row["match"]),
        "rows": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    with output.with_suffix(".csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0]) if results else ["market"])
        writer.writeheader()
        writer.writerows(results)
    print(json.dumps({"checked": payload["checked"], "matched": payload["matched"], "rows": results}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
