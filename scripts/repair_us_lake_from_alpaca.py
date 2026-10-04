"""Re-fetch US daily bars from Alpaca into a single-basis raw namespace.

The legacy US lake mixes Polygon split-adjusted rows with Alpaca raw repairs, so
it cannot be used for an adjusted-view rebuild. This script writes clean raw
bars (provider=alpaca, price_basis=raw, IEX feed) under
``data/lake/_us_alpaca/raw/<symbol>.parquet`` with a resumable state file.

Usage:
    python scripts/repair_us_lake_from_alpaca.py --symbols AAPL,MSFT --output ...
    python scripts/repair_us_lake_from_alpaca.py --from-v1-lake --limit 200

Each per-symbol file spans many trade dates, so this script rebuilds the
provenance shadow (``data/lake/_lake_v2``) one (US, trade_date) partition at a
time, tagging the manual repair as ``provider=manual_repair`` / ``price_basis=raw``.

Operational note: after this repair completes, run
``python scripts/verify_lake_manifest.py --check``; if it reports drift,
rebaseline with ``python scripts/verify_lake_manifest.py --write``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402
import polars as pl  # noqa: E402

from app.services.alpaca_client import AlpacaClient  # noqa: E402
from app.services.market_lake import market_lake_root  # noqa: E402

RAW_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.String,
    "symbol": pl.String,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "provider": pl.String,
    "provider_symbol": pl.String,
    "price_basis": pl.String,
    "source_reference": pl.String,
    "ingested_at": pl.String,
}


def _v1_us_symbols() -> list[str]:
    pattern = str(market_lake_root() / "us_daily" / "date=*" / "*.parquet")
    rows = duckdb.sql(
        "SELECT DISTINCT symbol FROM read_parquet(?, hive_partitioning=true) ORDER BY symbol",
        params=[pattern],
    ).fetchall()
    return [str(row[0]).upper() for row in rows]


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"done": {}, "failed": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=None, help="comma-separated tickers")
    parser.add_argument("--from-v1-lake", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", default="2024-12-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--sleep", type=float, default=0.3)
    parser.add_argument("--output", default=str(ROOT / "data" / "lake" / "_us_alpaca"))
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    from app.core.config import get_settings

    settings = get_settings()
    client = AlpacaClient(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        trading_endpoint=settings.alpaca_endpoint,
        data_endpoint=settings.alpaca_data_endpoint,
        feed=settings.alpaca_data_feed,
    )
    if args.symbols:
        symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    elif args.from_v1_lake:
        symbols = _v1_us_symbols()
    else:
        raise SystemExit("provide --symbols or --from-v1-lake")
    if args.limit:
        symbols = symbols[: args.limit]

    output_root = Path(args.output)
    state_path = output_root / "state.json"
    state = _load_state(state_path)
    ingested_at = datetime.now(tz=timezone.utc).isoformat()
    report = {"symbols": len(symbols), "done": 0, "empty": 0, "failed": 0, "rows": 0}

    def write_symbol(symbol: str, bars: list[dict]) -> None:
        rows = [
            {
                **bar,
                "provider": "alpaca",
                "provider_symbol": symbol,
                "price_basis": "raw",
                "source_reference": f"alpaca:bars:{settings.alpaca_data_feed}:raw",
                "ingested_at": ingested_at,
            }
            for bar in bars
        ]
        path = output_root / "raw" / f"{symbol}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(rows, schema=RAW_SCHEMA, orient="row").sort("date")
        temporary = path.with_name(f".{path.name}.tmp")
        frame.write_parquet(temporary, compression="zstd")
        temporary.replace(path)
        # NOTE (independent review R3): this script writes only the Alpaca raw
        # staging namespace (_us_alpaca/raw/<symbol>.parquet); it never rewrites
        # the canonical v1 lake, so there is deliberately no v2 shadow sync here.
        # Mirroring staging rows into _lake_v2 would create shadow rows that v1
        # does not contain (shadow ⊅ v1), i.e. drift in the opposite direction.

    pending = [symbol for symbol in symbols if symbol not in state["done"]]
    chunk_size = 50
    for chunk_start in range(0, len(pending), chunk_size):
        part = pending[chunk_start : chunk_start + chunk_size]
        batched = client.fetch_daily_bars_multi(part, start=args.start, end=args.end, adjustment="raw")
        for symbol, message in getattr(client, "multi_bars_failures", {}).items():
            state["failed"][symbol] = message
        for symbol in part:
            bars = batched.get(symbol) or []
            if bars:
                write_symbol(symbol, bars)
                state["done"][symbol] = len(bars)
                report["done"] += 1
                report["rows"] += len(bars)
            else:
                # Multi-bars omits some OTC/foreign tickers; retry them one by one.
                try:
                    bars = client.fetch_daily_bars(symbol, start=args.start, end=args.end, adjustment="raw")
                except Exception as exc:
                    state["failed"][symbol] = f"{type(exc).__name__}: {exc}"
                    report["failed"] += 1
                    continue
                if bars:
                    write_symbol(symbol, bars)
                    state["done"][symbol] = len(bars)
                    report["done"] += 1
                    report["rows"] += len(bars)
                else:
                    state["done"][symbol] = 0
                    report["empty"] += 1
        _save_state(state_path, state)
        print(json.dumps({"progress": report, "pending_left": len(pending) - chunk_start - len(part)}), flush=True)
    report["total_done"] = len(state["done"])
    report["total_failed"] = len(state["failed"])
    report["state_path"] = str(state_path)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
