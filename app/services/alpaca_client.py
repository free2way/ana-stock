"""Alpaca US data source: raw daily bars, corporate actions, assets, calendar.

Bars come from the data API (``adjustment=raw``, IEX feed by default) and are
therefore a single price basis. Corporate actions come from the trading API and
cover forward/reverse splits and cash dividends with old/new rates and ex-dates.
Keys are read from settings and never echoed in errors or payloads.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BARS_PAGE_LIMIT = 10_000
ACTIONS_PAGE_LIMIT = 1_000
DEFAULT_RETRY_SLEEP = 2.0


class AlpacaClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        api_secret: str | None,
        trading_endpoint: str = "https://paper-api.alpaca.markets/v2",
        data_endpoint: str = "https://data.alpaca.markets/v2",
        feed: str = "iex",
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.api_secret = str(api_secret or "").strip()
        self.trading_endpoint = trading_endpoint.rstrip("/")
        self.data_endpoint = data_endpoint.rstrip("/")
        self.feed = str(feed or "iex").strip().lower() or "iex"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _get(self, url: str, *, attempts: int = 4) -> dict | list:
        if not self.configured:
            raise RuntimeError("alpaca credentials are not configured")
        for attempt in range(attempts):
            request = Request(
                url,
                headers={
                    "APCA-API-KEY-ID": self.api_key,
                    "APCA-API-SECRET-KEY": self.api_secret,
                    "Accept": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https endpoints
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 429 and attempt < attempts - 1:
                    time.sleep(DEFAULT_RETRY_SLEEP * (attempt + 1))
                    continue
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:  # noqa: BLE001 - diagnostics only
                    detail = ""
                raise RuntimeError(f"alpaca HTTP {exc.code}: {exc.reason} :: {detail}".strip()) from exc
        raise RuntimeError("alpaca request exhausted retries")

    def get_account(self) -> dict:
        return self._get(f"{self.trading_endpoint}/account")

    def list_active_assets(self) -> list[dict]:
        payload = self._get(f"{self.trading_endpoint}/assets?status=active&asset_class=us_equity")
        return payload if isinstance(payload, list) else []

    def get_calendar(self, *, start: str, end: str) -> list[dict]:
        payload = self._get(f"{self.trading_endpoint}/calendar?start={start}&end={end}")
        return payload if isinstance(payload, list) else []

    def list_corporate_actions(
        self,
        *,
        start: str,
        end: str | None = None,
        ca_types: tuple[str, ...] = ("split", "dividend"),
    ) -> list[dict]:
        records: list[dict] = []
        for window_start, window_end in _date_windows(start, end, max_days=90):
            params = {"ca_types": ",".join(ca_types), "since": window_start, "limit": ACTIONS_PAGE_LIMIT}
            if window_end:
                params["until"] = window_end
            url = f"{self.trading_endpoint}/corporate_actions/announcements?{urlencode(params)}"
            while url:
                payload = self._get(url)
                if isinstance(payload, list):
                    # The endpoint returns a bare array for a <=90-day window
                    # and does not paginate within it.
                    records.extend(item for item in payload if isinstance(item, dict))
                    break
                if isinstance(payload, dict) and "announcements" in payload:
                    records.extend(payload.get("announcements") or [])
                    token = payload.get("next_page_token")
                    if token:
                        separator = "&" if "?" in url else "?"
                        url = f"{url}{separator}page_token={token}"
                    else:
                        url = ""
                    continue
                raise RuntimeError("unexpected alpaca corporate-actions response shape")
        return records

    def fetch_daily_bars(
        self,
        symbol: str,
        *,
        start: str | None = None,
        end: str | None = None,
        adjustment: str = "raw",
    ) -> list[dict]:
        ticker = str(symbol or "").strip().upper()
        if not ticker:
            return []
        params = {
            "timeframe": "1Day",
            "adjustment": adjustment,
            "feed": self.feed,
            "limit": BARS_PAGE_LIMIT,
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        url = f"{self.data_endpoint}/stocks/{ticker}/bars?{urlencode(params)}"
        rows: list[dict] = []
        while url:
            payload = self._get(url)
            if not isinstance(payload, dict):
                break
            bars = payload.get("bars") or []
            for bar in bars:
                trade_day = str(bar.get("t") or "")[:10]
                try:
                    open_price = float(bar.get("o"))
                    high = float(bar.get("h"))
                    low = float(bar.get("l"))
                    close = float(bar.get("c"))
                    volume = float(bar.get("v") or 0.0)
                except (TypeError, ValueError):
                    continue
                if not trade_day or min(open_price, high, low, close) <= 0 or volume < 0:
                    continue
                rows.append(
                    {
                        "date": trade_day,
                        "symbol": ticker,
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                    }
                )
            token = payload.get("next_page_token")
            if token:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}page_token={token}"
            else:
                url = ""
        return _dedupe_rows(rows)

    def fetch_daily_bars_multi(
        self,
        symbols: list[str],
        *,
        start: str | None = None,
        end: str | None = None,
        adjustment: str = "raw",
        chunk_size: int = 50,
    ) -> dict[str, list[dict]]:
        """Batch daily bars for many symbols (the API caps symbols per request)."""

        tickers = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
        output: dict[str, list[dict]] = {ticker: [] for ticker in tickers}
        self.multi_bars_failures = {}

        def fetch_chunk(chunk: list[str]) -> None:
            params = {
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "adjustment": adjustment,
                "feed": self.feed,
                "limit": BARS_PAGE_LIMIT,
            }
            if start:
                params["start"] = start
            if end:
                params["end"] = end
            url = f"{self.data_endpoint}/stocks/bars?{urlencode(params)}"
            while url:
                try:
                    payload = self._get(url)
                except RuntimeError as exc:
                    message = str(exc)
                    if "HTTP 400" not in message or len(chunk) == 1:
                        if len(chunk) == 1:
                            self.multi_bars_failures[chunk[0]] = message
                            return
                        raise
                    middle = len(chunk) // 2
                    fetch_chunk(chunk[:middle])
                    fetch_chunk(chunk[middle:])
                    return
                if not isinstance(payload, dict):
                    raise RuntimeError("unexpected alpaca multi-bars response shape")
                bars_by_symbol = payload.get("bars") or {}
                if isinstance(bars_by_symbol, dict):
                    for symbol, bars in bars_by_symbol.items():
                        ticker = str(symbol).upper()
                        output.setdefault(ticker, [])
                        for bar in bars or []:
                            trade_day = str(bar.get("t") or "")[:10]
                            try:
                                row = {
                                    "date": trade_day,
                                    "symbol": ticker,
                                    "open": float(bar.get("o")),
                                    "high": float(bar.get("h")),
                                    "low": float(bar.get("l")),
                                    "close": float(bar.get("c")),
                                    "volume": float(bar.get("v") or 0.0),
                                }
                            except (TypeError, ValueError):
                                continue
                            if not trade_day or min(row["open"], row["high"], row["low"], row["close"]) <= 0:
                                continue
                            output[ticker].append(row)
                token = payload.get("next_page_token")
                if token:
                    separator = "&" if "?" in url else "?"
                    url = f"{url}{separator}page_token={token}"
                else:
                    url = ""

        for start_index in range(0, len(tickers), chunk_size):
            fetch_chunk(tickers[start_index : start_index + chunk_size])
        for ticker in output:
            output[ticker] = _dedupe_rows(output[ticker])
        return output


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    """Keep the first bar per trade date; paginated responses may repeat boundaries."""

    rows.sort(key=lambda item: item["date"])
    deduped: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if row["date"] in seen:
            continue
        seen.add(row["date"])
        deduped.append(row)
    return deduped


def _date_windows(start: str, end: str | None, *, max_days: int = 90) -> list[tuple[str, str | None]]:
    """Split [start, end] into windows the API accepts (max 90 days apart)."""

    first = date.fromisoformat(str(start)[:10])
    last = date.fromisoformat(str(end)[:10]) if end else first
    if last < first:
        first, last = last, first
    windows: list[tuple[str, str | None]] = []
    cursor = first
    while cursor <= last:
        window_end = min(cursor + timedelta(days=max_days - 1), last)
        windows.append((cursor.isoformat(), window_end.isoformat() if end else None))
        cursor = window_end + timedelta(days=1)
    return windows


def normalize_corporate_action_rows(
    announcements: list[dict],
    *,
    market: str = "US",
) -> list[dict]:
    """Map Alpaca announcements into the actions store row schema."""

    rows: list[dict] = []
    for item in announcements:
        ca_type = str(item.get("ca_type") or "").strip().lower()
        symbol = str(item.get("target_symbol") or item.get("initiating_symbol") or "").strip().upper()
        ex_date = str(item.get("ex_date") or item.get("effective_date") or "")[:10]
        if not symbol or not ex_date:
            continue
        try:
            date.fromisoformat(ex_date)
        except ValueError:
            continue
        reference = f"alpaca:ca:{item.get('id') or item.get('corporate_action_id') or symbol + ex_date}"
        if ca_type == "split":
            try:
                old_rate = float(item.get("old_rate"))
                new_rate = float(item.get("new_rate"))
            except (TypeError, ValueError):
                continue
            if old_rate <= 0 or new_rate <= 0:
                continue
            factor = new_rate / old_rate
            if abs(factor - 1.0) <= 1e-9:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "action_type": "split",
                    "effective_date": ex_date,
                    "factor": factor,
                    "source": "alpaca",
                    "source_reference": reference,
                }
            )
        elif ca_type == "dividend":
            try:
                cash = float(item.get("cash") or 0.0)
            except (TypeError, ValueError):
                continue
            if cash <= 0:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "action_type": "cash_dividend",
                    "effective_date": ex_date,
                    "cash_amount": cash,
                    "currency": "USD",
                    "source": "alpaca",
                    "source_reference": reference,
                }
            )
        elif ca_type == "merger":
            # merger_completion: the target stops trading; consideration is
            # cash and/or acquirer shares (old_rate/new_rate).
            try:
                cash = float(item.get("cash") or 0.0)
            except (TypeError, ValueError):
                cash = 0.0
            rows.append(
                {
                    "symbol": symbol,
                    "action_type": "merger",
                    "effective_date": ex_date,
                    "cash_amount": cash if cash > 0 else None,
                    "currency": "USD",
                    "source": "alpaca",
                    "source_reference": reference,
                }
            )
        elif ca_type == "spinoff":
            # The parent (target_symbol) re-prices on the ex-date as the
            # spun-off entity separates; keep it as an event-day explanation.
            rows.append(
                {
                    "symbol": symbol,
                    "action_type": "spinoff",
                    "effective_date": ex_date,
                    "source": "alpaca",
                    "source_reference": reference,
                }
            )
    return rows
