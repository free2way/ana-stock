"""Build a bounded, immutable HiThink CN execution-fact gap receipt.

This script performs read-only API calls and reads existing official Parquet
dumps.  It never writes the database or starts model training.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, time as datetime_time, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl

from app.core.config import get_settings
from app.services.hithink_finance_client import HithinkFinanceClient
from app.services.stock_selection.hithink_execution_evidence import (
    PRICE_FIELDS,
    build_gap_rows,
    canonical_digest,
    compare_action_sources,
    compare_price_sources,
    date_from_ms,
    normalize_action_items,
    normalize_price_items,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_TICKERS = ["000001.SZ", "600519.SH"]


def _ms(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), datetime_time.min, tzinfo=SHANGHAI_TZ).timestamp() * 1000)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Probe:
    def __init__(self):
        settings = get_settings()
        self.api_key = str(settings.hithink_finance_api_key or "").strip()
        self.base_url = str(settings.hithink_finance_base_url).rstrip("/") + "/"
        self.timeout = float(settings.hithink_finance_timeout_seconds)
        self.records: list[dict] = []
        if not self.api_key:
            raise RuntimeError("HiThink API key is not configured")

    def get(self, capability: str, path: str, params: dict) -> dict:
        requested_at = datetime.now(timezone.utc).isoformat()
        url = urljoin(self.base_url, path.lstrip("/")) + "?" + urlencode(params)
        request = Request(url, headers={"Accept": "application/json", "X-api-key": self.api_key,
                                        "User-Agent": "Personal-Quant-Workbench/evidence-audit"})
        response_status = None
        body = None
        attempts = 0
        last_error = None
        for attempt in range(3):
            attempts = attempt + 1
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    response_status = response.status
                    body = response.read().decode("utf-8")
                break
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
        collected_at = datetime.now(timezone.utc).isoformat()
        if body is None:
            self.records.append({
                "provider": "hithink_finance", "capability": capability, "endpoint": path,
                "params": params, "requested_at": requested_at, "collected_at": collected_at,
                "transport_attempts": attempts, "error_type": type(last_error).__name__,
            })
            raise RuntimeError(f"HiThink capability probe transport failed: {capability}") from last_error
        body = body.replace(self.api_key, "[REDACTED]")
        record = {
            "provider": "hithink_finance", "capability": capability, "endpoint": path,
            "params": params, "requested_at": requested_at, "collected_at": collected_at,
            "transport_attempts": attempts, "http_status": response_status,
            "response_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            record["error"] = "invalid_json"
            self.records.append(record)
            raise RuntimeError("HiThink returned invalid JSON") from exc
        record["response"] = payload
        record["request_id"] = payload.get("request_id") if isinstance(payload, dict) else None
        self.records.append(record)
        explicit_provider_empty = (
            capability == "corporate_actions"
            and isinstance(payload, dict)
            and payload.get("code") == 3002
            and str(payload.get("message") or "").startswith("No adjustment events for thscode=")
        )
        if explicit_provider_empty:
            record["normalized_empty_response"] = "no_adjustment_events"
            return {"item": []}
        if response_status != 200 or not isinstance(payload, dict) or payload.get("code") != 0:
            raise RuntimeError(f"HiThink capability probe failed: {capability}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"HiThink capability probe returned no object data: {capability}")
        time.sleep(0.20)
        return data

    def paginated_pool(self, *, name: str, path: str, trade_date: str) -> list[dict]:
        rows: list[dict] = []
        page = 1
        while True:
            data = self.get(name, path, {"date_ms": _ms(trade_date), "page": page, "size": 200})
            page_rows = data.get("item") or []
            if not isinstance(page_rows, list):
                raise RuntimeError("HiThink pool returned an invalid item list")
            rows.extend(item for item in page_rows if isinstance(item, dict))
            pagination = data.get("pagination") or {}
            pages = int(pagination.get("pages") or 0)
            total = int(pagination.get("total") or 0)
            if pages > 30 or total > 6000:
                raise RuntimeError("HiThink pool exceeded bounded audit limits")
            if page >= max(1, pages):
                if len(rows) != total:
                    raise RuntimeError("HiThink pool pagination count mismatch")
                break
            page += 1
        return rows


def _dump_price_rows(frame: pl.DataFrame, *, ticker: str, dates: set[str]) -> dict[str, dict]:
    rows = {}
    for item in frame.filter(pl.col("thscode") == ticker).to_dicts():
        day = date_from_ms(item["date_ms"])
        if day not in dates:
            continue
        rows[day] = {"ticker": ticker, "date": day, **{field: float(item[field]) for field in PRICE_FIELDS}}
    return rows


def _dump_action_rows(frame: pl.DataFrame, *, ticker: str, start: str, end: str) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    for item in frame.filter(pl.col("thscode") == ticker).to_dicts():
        day = date_from_ms(item["ex_date_ms"])
        if not start <= day <= end:
            continue
        rows.setdefault(day, []).append({
            "ticker": ticker, "ex_date": day,
            "dividend_per_share": float(item.get("dividend_per_share") or 0),
            "per_share_bonus": float(item.get("per_share_bonus") or 0),
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", default="2026-09-15")
    parser.add_argument("--end", default="2026-09-29")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--price-dump", type=Path, default=Path("data/raw/hithink_finance/daily-k-10d-latest.parquet"))
    parser.add_argument("--action-dump", type=Path, default=Path("data/raw/hithink_finance/adjustment-factors-latest.parquet"))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("evidence output already exists; use a new filename")
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start > end or (end - start).days > 31:
        parser.error("audit window must be 0..31 calendar days")
    tickers = [HithinkFinanceClient.to_thscode(value) for value in args.tickers]
    if len(tickers) > 5 or len(set(tickers)) != len(tickers):
        parser.error("use 1..5 unique A-share tickers")
    if not args.price_dump.is_file() or not args.action_dump.is_file():
        parser.error("both official HiThink dump files are required")

    price_frame = pl.read_parquet(args.price_dump)
    action_frame = pl.read_parquet(args.action_dump)
    available_dates = sorted({
        date_from_ms(value) for value in price_frame.get_column("date_ms").unique().to_list()
        if args.start <= date_from_ms(value) <= args.end
    })
    if not available_dates:
        parser.error("price dump has no trading dates in the requested window")
    date_set = set(available_dates)

    probe = Probe()
    price_checks, action_checks, actions_by_ticker = {}, {}, {}
    for ticker in tickers:
        prices = probe.get("raw_prices", "/api/a-share/prices/historical", {
            "thscode": ticker, "interval": "1d", "start": _ms(args.start), "end": _ms(args.end), "adjust": "none",
        })
        api_prices = normalize_price_items(ticker=ticker, items=prices.get("item") or [])
        api_prices = {day: row for day, row in api_prices.items() if day in date_set}
        dump_prices = _dump_price_rows(price_frame, ticker=ticker, dates=date_set)
        price_checks[ticker] = compare_price_sources(api_rows=api_prices, dump_rows=dump_prices)

        actions = probe.get("corporate_actions", "/api/a-share/corporate-actions/adjustment-factors", {
            "thscode": ticker, "from": args.start, "to": args.end,
        })
        api_actions = normalize_action_items(ticker=ticker, items=actions.get("item") or [])
        dump_actions = _dump_action_rows(action_frame, ticker=ticker, start=args.start, end=args.end)
        action_checks[ticker] = compare_action_sources(api_rows=api_actions, dump_rows=dump_actions)
        actions_by_ticker[ticker] = api_actions

    pool_paths = {
        "limit_up": "/api/a-share/special-data/limit-up-pool",
        "limit_down": "/api/a-share/special-data/limit-down-pool",
        "limit_break": "/api/a-share/special-data/limit-break-pool",
    }
    pool_memberships: dict[tuple[str, str], list[str]] = {}
    pool_daily_counts: dict[str, dict[str, int]] = {}
    for day in available_dates:
        pool_daily_counts[day] = {}
        for name, path in pool_paths.items():
            rows = probe.paginated_pool(name=name, path=path, trade_date=day)
            pool_daily_counts[day][name] = len(rows)
            for item in rows:
                ticker = str(item.get("thscode") or "")
                if ticker in tickers:
                    pool_memberships.setdefault((ticker, day), []).append(name)

    price_dump_ref = f"sha256:{_file_sha256(args.price_dump)}"
    action_dump_ref = f"sha256:{_file_sha256(args.action_dump)}"
    gap = build_gap_rows(
        tickers=tickers, trading_dates=available_dates, price_checks=price_checks,
        action_checks=action_checks, actions_by_ticker=actions_by_ticker,
        pool_memberships=pool_memberships,
        source_references={
            "raw_price": price_dump_ref,
            "corporate_actions": action_dump_ref,
            "limit_pools": "query-receipts:" + canonical_digest([
                record["response_sha256"] for record in probe.records if record["capability"].startswith("limit_")
            ]),
        },
        required_components=("raw_price", "corporate_action"),
    )
    payload = {
        "schema": "hithink_cn_execution_fact_gap_v2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": {"market": "CN", "tickers": tickers, "start": args.start, "end": args.end,
                  "trading_dates": available_dates, "bj_excluded": True},
        "dump_sources": [
            {"path": str(args.price_dump), "sha256": price_dump_ref.removeprefix("sha256:"), "rows": price_frame.height},
            {"path": str(args.action_dump), "sha256": action_dump_ref.removeprefix("sha256:"), "rows": action_frame.height},
        ],
        "api_records": probe.records,
        "price_crosscheck": price_checks,
        "corporate_action_crosscheck": action_checks,
        "pool_daily_counts": pool_daily_counts,
        "fact_gap_matrix": gap,
        "coverage_conclusion": {
            "raw_prices": "PASS_FOR_BOUNDED_SAMPLE" if all(v["status"] == "PASS" for v in price_checks.values()) else "BLOCKED",
            "corporate_actions": "PASS_FOR_BOUNDED_SAMPLE" if all(v["status"] == "PASS" for v in action_checks.values()) else "BLOCKED",
            "historical_suspensions": "MISSING_FROM_PUBLIC_HITHINK_API",
            "explicit_daily_upper_lower_limits": "MISSING_FROM_PUBLIC_HITHINK_API",
            "limit_pools": "OBSERVATION_ONLY_NOT_DAILY_BOUNDS",
        },
        "execution_evidence_policy": {
            "version": "raw_price_volume_company_action_v2",
            "required": ["raw_price", "daily_provenance", "volume", "corporate_action_state"],
            "optional_non_blocking": ["historical_suspension_state", "explicit_daily_upper_lower_limits"],
            "limitation": "Research replay does not certify exchange fillability when optional facts are absent.",
        },
        "cn_small_scope_status": "READY" if gap["evidence_complete"] else "BLOCKED",
        "training_authorized": bool(gap["evidence_complete"]),
        "training_run_id": None,
        "note": "No database writes, no old-result relabeling, and no model training in this audit.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output), "tickers": len(tickers), "trading_dates": len(available_dates),
        "api_requests": len(probe.records), "gap_counts": gap["counts"],
        "cn_small_scope_status": payload["cn_small_scope_status"],
        "training_authorized": payload["training_authorized"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
