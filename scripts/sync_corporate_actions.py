"""Sync corporate actions into the local store.

CN (tushare_adj_factor): fetch per-session cumulative adjustment factors for
every lake trade date (bulk, one API call per day), store the factor series and
derive factor-jump action records. Resumable via a sidecar state file.

US (polygon): bulk splits + dividends within [start, end]; Alpaca is reserved
for cross-checking, not as the primary source.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.cn_adjustment_factors import (  # noqa: E402
    derive_dividend_actions,
    derive_factor_actions,
    load_dividend_rows,
    load_factor_rows,
    summarize_factor_rows,
    write_dividend_rows,
    write_factor_series,
)
from app.services.corporate_actions import write_actions  # noqa: E402
from app.services.market_lake import market_lake_root  # noqa: E402


def _settings_root() -> Path:
    from app.core.config import get_settings

    return get_settings().data_dir / "corporate_actions"


def _cn_lake_dates() -> list[str]:
    return sorted(
        path.name.split("=", 1)[-1]
        for path in (market_lake_root() / "cn_daily").glob("date=*")
        if path.is_dir()
    )


def _load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"done": {}, "failed": {}}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def sync_cn_adj_factors(*, max_days: int, sleep_seconds: float, refresh: bool, dates: list[str] | None = None) -> dict:
    from app.services.tushare_client import TushareClient

    state_path = _settings_root() / "cn_adj_factor_dates.json"
    state = _load_state(state_path)
    if dates:
        lake_dates = _cn_lake_dates()
        unknown = [item for item in dates if item not in lake_dates]
        if unknown:
            raise ValueError(f"dates missing from the CN lake: {', '.join(unknown[:5])}")
        selected = sorted(dates)
    else:
        selected = _cn_lake_dates()
        if max_days:
            selected = selected[-max_days:]
    client = TushareClient()
    buffer: list[dict] = []
    report = {"market": "CN", "dates": len(selected), "fetched": 0, "empty": 0, "failed": 0, "rows": 0}
    for index, trade_date in enumerate(selected, start=1):
        if not refresh and str(trade_date) in state["done"]:
            continue
        rows = client.fetch_cn_adj_factor_bulk(trade_date)
        if rows:
            buffer.extend(rows)
            state["done"][trade_date] = len(rows)
            state["failed"].pop(trade_date, None)
            report["fetched"] += 1
            report["rows"] += len(rows)
        elif client.last_error:
            state["failed"][trade_date] = str(client.last_error)
            report["failed"] += 1
        else:
            state["done"][trade_date] = 0
            report["empty"] += 1
        if len(buffer) >= 80_000 or index == len(selected):
            if buffer:
                write_factor_series(buffer)
                buffer = []
            _save_state(state_path, state)
        if sleep_seconds:
            time.sleep(sleep_seconds)
    if buffer:
        write_factor_series(buffer)
    _save_state(state_path, state)

    factor_rows = load_factor_rows()
    actions = derive_factor_actions(factor_rows)
    if actions:
        write_actions("CN", actions)
    report["factor_series"] = summarize_factor_rows(factor_rows)
    report["actions_derived"] = len(actions)
    report["state_path"] = str(state_path)
    return report


def sync_cn_dividends(*, periods: list[str], sleep_seconds: float, refresh: bool) -> dict:
    from app.services.tushare_client import TushareClient

    state_path = _settings_root() / "cn_dividend_periods.json"
    state = _load_state(state_path)
    client = TushareClient()
    report = {"market": "CN", "source": "tushare_dividend", "periods": len(periods), "fetched": 0, "failed": 0, "rows": 0}
    for period in periods:
        if not refresh and str(period) in state["done"]:
            continue
        rows = client.fetch_cn_dividends(period)
        if rows:
            write_dividend_rows(rows)
            state["done"][period] = len(rows)
            state["failed"].pop(period, None)
            report["fetched"] += 1
            report["rows"] += len(rows)
        elif client.last_error:
            state["failed"][period] = str(client.last_error)
            report["failed"] += 1
        else:
            state["done"][period] = 0
        _save_state(state_path, state)
        if sleep_seconds:
            time.sleep(sleep_seconds)
    dividend_rows = load_dividend_rows()
    actions = derive_dividend_actions(dividend_rows)
    if actions:
        write_actions("CN", actions)
    report["dividend_rows"] = len(dividend_rows)
    report["actions_derived"] = len(actions)
    report["state_path"] = str(state_path)
    return report


def sync_cn_akshare_dividends(*, periods: list[str], sleep_seconds: float = 2.0, refresh: bool = False) -> dict:
    from app.services.akshare_actions import fetch_fhps_rows

    state_path = _settings_root() / "cn_akshare_fhps_periods.json"
    state = _load_state(state_path)
    report = {"market": "CN", "source": "akshare_fhps", "periods": len(periods), "fetched": 0, "failed": 0, "rows": 0}
    for period in periods:
        if not refresh and str(period) in state["done"]:
            continue
        try:
            rows = fetch_fhps_rows(period)
        except Exception as exc:
            state["failed"][period] = f"{type(exc).__name__}: {exc}"
            report["failed"] += 1
            _save_state(state_path, state)
            continue
        if rows:
            write_dividend_rows(rows)
            state["done"][period] = len(rows)
            state["failed"].pop(period, None)
            report["fetched"] += 1
            report["rows"] += len(rows)
        else:
            state["done"][period] = 0
        _save_state(state_path, state)
        if sleep_seconds:
            time.sleep(sleep_seconds)
    dividend_rows = load_dividend_rows()
    actions = derive_dividend_actions(dividend_rows)
    if actions:
        write_actions("CN", actions)
    report["dividend_rows"] = len(dividend_rows)
    report["actions_derived"] = len(actions)
    report["state_path"] = str(state_path)
    return report


def sync_us_actions_from_alpaca(*, start: str, end: str | None) -> dict:
    from app.core.config import get_settings
    from app.services.alpaca_client import AlpacaClient, normalize_corporate_action_rows
    from app.services.corporate_actions import normalize_action

    settings = get_settings()
    client = AlpacaClient(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        trading_endpoint=settings.alpaca_endpoint,
        data_endpoint=settings.alpaca_data_endpoint,
        feed=settings.alpaca_data_feed,
    )
    announcements = client.list_corporate_actions(
        start=start, end=end, ca_types=("split", "dividend", "merger", "spinoff")
    )
    rows = normalize_corporate_action_rows(announcements)
    records = [normalize_action(row, market="US") for row in rows]
    if records:
        write_actions("US", records)
    return {
        "market": "US",
        "source": "alpaca",
        "start": start,
        "end": end,
        "announcements": len(announcements),
        "records": len(records),
        "splits": sum(1 for record in records if record.action_type == "split"),
        "dividends": sum(1 for record in records if record.action_type == "cash_dividend"),
        "mergers": sum(1 for record in records if record.action_type == "merger"),
        "spinoffs": sum(1 for record in records if record.action_type == "spinoff"),
    }


def sync_us_actions(*, start: str, end: str | None, kinds: set[str] | None = None) -> dict:
    from app.services.polygon_actions import fetch_dividend_rows, fetch_split_rows
    from app.core.config import get_settings

    wanted = kinds or {"splits", "dividends"}
    settings = get_settings()
    if not settings.polygon_api_key:
        raise RuntimeError("PQW_POLYGON_API_KEY is not configured")
    split_rows = (
        fetch_split_rows(settings.polygon_api_key, start=start, end=end, page_delay=13.0)
        if "splits" in wanted
        else []
    )
    dividend_rows = (
        fetch_dividend_rows(settings.polygon_api_key, start=start, end=end, page_delay=13.0)
        if "dividends" in wanted
        else []
    )
    records = []
    from app.services.corporate_actions import normalize_action

    for row in split_rows:
        records.append(normalize_action({**row, "action_type": "split"}, market="US"))
    for row in dividend_rows:
        records.append(normalize_action({**row, "action_type": "cash_dividend"}, market="US"))
    if records:
        write_actions("US", records)
    return {
        "market": "US",
        "start": start,
        "end": end,
        "kinds": sorted(wanted),
        "splits": len(split_rows),
        "dividends": len(dividend_rows),
        "records": len(records),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["CN", "US"], required=True)
    parser.add_argument(
        "--source",
        choices=["tushare_adj_factor", "tushare_dividend", "akshare_fhps", "polygon", "alpaca"],
        default=None,
    )
    parser.add_argument("--max-days", type=int, default=0, help="CN: most recent N lake sessions (0 = all)")
    parser.add_argument("--sleep", type=float, default=0.12, help="seconds between TuShare calls")
    parser.add_argument("--refresh", action="store_true", help="CN: re-fetch dates already recorded")
    parser.add_argument("--periods", default="20241231,20250630,20251231,20260630", help="CN dividend fiscal periods")
    parser.add_argument("--dates", default=None, help="CN adj_factor: explicit comma-separated lake dates")
    parser.add_argument("--kinds", default="splits,dividends", help="US polygon: subset of splits,dividends")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    if args.market == "CN":
        periods = [item.strip() for item in args.periods.split(",") if item.strip()]
        if args.source == "tushare_dividend":
            report = sync_cn_dividends(
                periods=periods,
                sleep_seconds=max(61.0, args.sleep),
                refresh=args.refresh,
            )
        elif args.source == "akshare_fhps":
            report = sync_cn_akshare_dividends(periods=periods, sleep_seconds=max(2.0, args.sleep), refresh=args.refresh)
        else:
            report = sync_cn_adj_factors(
                max_days=args.max_days,
                sleep_seconds=args.sleep,
                refresh=args.refresh,
                dates=[item.strip() for item in args.dates.split(",") if item.strip()] if args.dates else None,
            )
    else:
        if args.source == "alpaca":
            report = sync_us_actions_from_alpaca(start=args.start, end=args.end)
        else:
            report = sync_us_actions(
                start=args.start,
                end=args.end,
                kinds={item.strip() for item in args.kinds.split(",") if item.strip()},
            )
    payload = {
        "schema_version": "corporate_action_sync_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "report": report,
    }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 1 if report.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
