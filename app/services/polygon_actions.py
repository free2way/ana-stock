"""Polygon reference APIs for splits and dividends (bulk, paginated).

Free API tiers rate-limit aggressively (HTTP 429); pagination therefore sleeps
between pages and retries with a fixed backoff. The page delay is injectable so
unit tests never sleep.
"""
from __future__ import annotations

import json
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = "https://api.polygon.io"
PAGE_LIMIT = 1000
RATE_LIMIT_SLEEP_SECONDS = 13.0


def _fetch_json(url: str, *, timeout: int = 30, attempts: int = 5) -> dict:
    for attempt in range(attempts):
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https endpoint
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429 and attempt < attempts - 1:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = float(retry_after) if retry_after else RATE_LIMIT_SLEEP_SECONDS
                except (TypeError, ValueError):
                    delay = RATE_LIMIT_SLEEP_SECONDS
                time.sleep(max(1.0, delay))
                continue
            raise RuntimeError(f"polygon HTTP {exc.code}: {exc.reason}") from exc
    raise RuntimeError("polygon request exhausted retries")


def _paginate(
    api_key: str,
    path: str,
    params: dict,
    *,
    endpoint: str,
    page_delay: float = 0.05,
) -> list[dict]:
    query = urlencode({**params, "apiKey": api_key, "limit": PAGE_LIMIT})
    url = f"{endpoint.rstrip('/')}{path}?{query}"
    results: list[dict] = []
    while url:
        payload = _fetch_json(url)
        if payload.get("status") == "ERROR":
            raise RuntimeError(f"polygon error: {payload.get('error') or payload.get('message')}")
        results.extend(payload.get("results") or [])
        next_url = payload.get("next_url")
        if next_url:
            separator = "&" if "?" in next_url else "?"
            url = f"{next_url}{separator}apiKey={api_key}"
            if page_delay:
                time.sleep(page_delay)
        else:
            url = ""
    return results


def fetch_split_rows(
    api_key: str,
    *,
    start: str,
    end: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    page_delay: float = 0.05,
) -> list[dict]:
    params = {"execution_date.gte": start, "sort": "execution_date", "order": "asc"}
    if end:
        params["execution_date.lte"] = end
    rows: list[dict] = []
    for item in _paginate(api_key, "/v3/reference/splits", params, endpoint=endpoint, page_delay=page_delay):
        ticker = str(item.get("ticker") or "").strip().upper()
        execution_date = str(item.get("execution_date") or "")[:10]
        split_from = item.get("split_from")
        split_to = item.get("split_to")
        try:
            factor = float(split_to) / float(split_from)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
        if not ticker or not execution_date or factor <= 0:
            continue
        rows.append(
            {
                "symbol": ticker,
                "effective_date": execution_date,
                "factor": factor,
                "source": "polygon",
                "source_reference": f"polygon:splits:{item.get('id') or ticker + execution_date}",
            }
        )
    return rows


def fetch_dividend_rows(
    api_key: str,
    *,
    start: str,
    end: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
    page_delay: float = 0.05,
) -> list[dict]:
    params = {"ex_dividend_date.gte": start, "sort": "ex_dividend_date", "order": "asc"}
    if end:
        params["ex_dividend_date.lte"] = end
    rows: list[dict] = []
    for item in _paginate(api_key, "/v3/reference/dividends", params, endpoint=endpoint, page_delay=page_delay):
        ticker = str(item.get("ticker") or "").strip().upper()
        effective_date = str(item.get("ex_dividend_date") or "")[:10]
        try:
            cash_amount = float(item.get("cash_amount"))
        except (TypeError, ValueError):
            continue
        if not ticker or not effective_date or cash_amount < 0:
            continue
        rows.append(
            {
                "symbol": ticker,
                "effective_date": effective_date,
                "cash_amount": cash_amount,
                "currency": str(item.get("currency") or "USD").upper(),
                "source": "polygon",
                "source_reference": f"polygon:dividends:{item.get('id') or ticker + effective_date}",
            }
        )
    return rows
