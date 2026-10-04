"""Collect bounded Alpaca raw US bars and corporate actions without fallback."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_json(url: str, headers: dict[str, str], *, retries: int = 2) -> tuple[dict, dict]:
    error = None
    for attempt in range(retries + 1):
        requested = datetime.now(timezone.utc).isoformat()
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                body = response.read()
                payload = json.loads(body)
                receipt = {"requested_at": requested, "collected_at": datetime.now(timezone.utc).isoformat(),
                           "http_status": response.status, "response_sha256": hashlib.sha256(body).hexdigest(),
                           "request_id": response.headers.get("x-request-id"), "attempts": attempt + 1}
                return payload, receipt
        except Exception as exc:
            error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("Alpaca evidence request failed") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--start", default="2026-01-02")
    parser.add_argument("--end", default="2026-09-29")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("immutable evidence output already exists")
    tickers = [value.strip().upper() for value in args.tickers.split(",") if value.strip()]
    if not 20 <= len(tickers) <= 30 or len(set(tickers)) != len(tickers):
        raise ValueError("US pilot requires 20..30 unique tickers")
    if any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", value) for value in tickers):
        raise ValueError("invalid US ticker")
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start >= end or (end - start).days > 370:
        raise ValueError("invalid bounded collection window")
    settings = get_settings()
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise RuntimeError("Alpaca credentials are not configured")
    headers = {"APCA-API-KEY-ID": settings.alpaca_api_key,
               "APCA-API-SECRET-KEY": settings.alpaca_api_secret, "Accept": "application/json"}
    base = str(settings.alpaca_data_endpoint or "https://data.alpaca.markets/v2").rstrip("/")
    feed = str(settings.alpaca_data_feed or "iex")
    records, bars = [], []
    for ticker in tickers:
        params = {"start": args.start, "end": args.end, "timeframe": "1Day",
                  "adjustment": "raw", "feed": feed, "limit": 10000}
        endpoint = f"{base}/stocks/{ticker}/bars"
        payload, receipt = request_json(endpoint + "?" + urlencode(params), headers)
        if payload.get("next_page_token"):
            raise RuntimeError("unexpected unconsumed price pagination")
        values = payload.get("bars")
        if not isinstance(values, list) or len(values) < 120 or payload.get("symbol") != ticker:
            raise RuntimeError("insufficient or mismatched Alpaca bars")
        seen = set()
        for item in values:
            day = str(item.get("t") or "")[:10]
            if not day or day in seen or not args.start <= day <= args.end:
                raise RuntimeError("invalid or duplicate Alpaca bar")
            seen.add(day)
            bars.append({"ticker": ticker, "date": day, "open": item.get("o"), "high": item.get("h"),
                         "low": item.get("l"), "close": item.get("c"), "volume": item.get("v")})
        records.append({"provider": "alpaca", "capability": "raw_prices", "ticker": ticker,
                        "endpoint": endpoint, "params": params, **receipt, "row_count": len(values)})
        time.sleep(0.2)

    actions, token, action_records = [], None, []
    for page in range(20):
        params = {"symbols": ",".join(tickers), "start": args.start, "end": args.end, "limit": 1000}
        if token:
            params["page_token"] = token
        endpoint = "https://data.alpaca.markets/v1/corporate-actions"
        payload, receipt = request_json(endpoint + "?" + urlencode(params), headers)
        page_rows = []
        groups = payload.get("corporate_actions") or {}
        if not isinstance(groups, dict):
            raise RuntimeError("invalid Alpaca corporate-action response")
        for action_type, values in groups.items():
            if not isinstance(values, list):
                raise RuntimeError("invalid Alpaca corporate-action group")
            for item in values:
                if str(item.get("symbol") or "").upper() in tickers:
                    page_rows.append({"action_type": action_type, **item})
        actions.extend(page_rows)
        action_records.append({"provider": "alpaca", "capability": "corporate_actions",
                               "endpoint": endpoint, "params": params, **receipt,
                               "row_count": len(page_rows)})
        token = payload.get("next_page_token")
        if not token:
            break
    else:
        raise RuntimeError("corporate-action pagination exceeded safety bound")
    payload = {
        "schema": "us_alpaca_observable_evidence_v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "market": "US", "provider": "alpaca", "feed": feed, "price_basis": "raw",
        "scope": {"tickers": tickers, "start": args.start, "end": args.end},
        "requests": records + action_records, "bars": bars, "corporate_actions": actions,
        "counts": {"tickers": len(tickers), "bars": len(bars), "corporate_actions": len(actions)},
        "policy": "raw_price_volume_company_action_v2", "fallback_used": False,
        "limitations": ["IEX feed is not the consolidated exchange auction feed",
                        "corporate-action query window uses Alpaca process-date semantics"],
    }
    envelope = {"payload": payload, "sha256": digest(payload)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(envelope, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"output": str(args.output), **payload["counts"], "feed": feed,
                      "sha256": envelope["sha256"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
