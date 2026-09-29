from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.services.market_freshness import latest_completed_market_date
from app.services.ticker_format import normalize_ticker_for_market


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class HithinkFinanceAPIError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class HithinkFinanceClient:
    """Small REST client for the official HiThink/Tonghuashun Financial API.

    Authentication is read only from application settings. The API key is
    never included in exceptions, returned diagnostics, URLs, or persisted
    market data.
    """

    name = "hithink_finance"
    _request_lock = threading.Lock()
    _last_request_at = 0.0

    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = str(self.settings.hithink_finance_base_url).rstrip("/")
        self.last_request_id: str | None = None

    def _wait_for_request_slot(self) -> None:
        minimum_interval = max(
            0.0,
            float(getattr(self.settings, "hithink_finance_min_request_interval_seconds", 0.20)),
        )
        if minimum_interval <= 0:
            return
        with self.__class__._request_lock:
            elapsed = time.monotonic() - self.__class__._last_request_at
            wait_seconds = minimum_interval - elapsed
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self.__class__._last_request_at = time.monotonic()

    def is_configured(self) -> bool:
        return bool(str(self.settings.hithink_finance_api_key or "").strip())

    @staticmethod
    def to_thscode(ticker: str) -> str:
        normalized = normalize_ticker_for_market(ticker, "CN")
        if normalized.endswith(".SS"):
            return f"{normalized[:-3]}.SH"
        if normalized.endswith((".SZ", ".BJ")):
            return normalized
        raise ValueError(f"Unsupported A-share ticker: {ticker}")

    @staticmethod
    def to_internal_ticker(thscode: str) -> str:
        return normalize_ticker_for_market(thscode, "CN")

    @staticmethod
    def _date_to_ms(value: str | date) -> int:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
        return int(datetime.combine(parsed, datetime_time.min, tzinfo=SHANGHAI_TZ).timestamp() * 1000)

    @staticmethod
    def _ms_to_date(value: int | float | str) -> str:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=SHANGHAI_TZ).date().isoformat()

    def _request_json(self, path: str, params: dict[str, object] | None = None) -> dict:
        if not self.is_configured():
            raise HithinkFinanceAPIError("PQW_HITHINK_FINANCE_API_KEY is not configured.")
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "Personal-Quant-Workbench/1.0",
                "X-api-key": str(self.settings.hithink_finance_api_key),
            },
        )
        retry_count = max(0, int(self.settings.hithink_finance_max_retries))
        last_error: Exception | None = None
        for attempt in range(retry_count + 1):
            try:
                self._wait_for_request_slot()
                with urlopen(request, timeout=float(self.settings.hithink_finance_timeout_seconds)) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise HithinkFinanceAPIError("HiThink returned a non-object response.")
                self.last_request_id = str(payload.get("request_id") or "").strip() or None
                code = int(payload.get("code") or 0)
                if code == 0:
                    data = payload.get("data")
                    return data if isinstance(data, dict) else {}
                message = str(payload.get("message") or f"HiThink business error {code}")
                error = HithinkFinanceAPIError(message, code=code, request_id=self.last_request_id)
                if code != 4001 and code < 5000:
                    raise error
                last_error = error
            except HithinkFinanceAPIError:
                raise
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < retry_count:
                time.sleep(min(4.0, 0.5 * (2**attempt)))
        raise HithinkFinanceAPIError(
            f"HiThink request failed after {retry_count + 1} attempt(s): {type(last_error).__name__}",
            request_id=self.last_request_id,
        ) from last_error

    def fetch_historical_prices(
        self,
        ticker: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        adjust: str = "none",
    ) -> list[dict]:
        end = date.fromisoformat(str(end_date)[:10]) if end_date else date.fromisoformat(latest_completed_market_date("CN"))
        start = date.fromisoformat(str(start_date)[:10]) if start_date else end - timedelta(days=3650)
        if start > end:
            raise ValueError("start_date must not be after end_date")

        rows_by_date: dict[str, dict] = {}
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(end, chunk_start + timedelta(days=3650))
            data = self._request_json(
                "/api/a-share/prices/historical",
                {
                    "thscode": self.to_thscode(ticker),
                    "interval": "1d",
                    "start": self._date_to_ms(chunk_start),
                    "end": self._date_to_ms(chunk_end),
                    "adjust": adjust,
                },
            )
            for item in data.get("item") or []:
                if not isinstance(item, dict) or item.get("date_ms") is None:
                    continue
                trade_date = self._ms_to_date(item["date_ms"])
                close = self._to_float(item.get("close_price"))
                row = {
                    "date": trade_date,
                    "symbol": normalize_ticker_for_market(ticker, "CN"),
                    "open": self._to_float(item.get("open_price")),
                    "high": self._to_float(item.get("high_price")),
                    "low": self._to_float(item.get("low_price")),
                    "close": close,
                    "volume": self._to_float(item.get("volume")),
                    "adj_close": close,
                    "dividend": None,
                    "split_ratio": None,
                }
                if all(row[key] is not None for key in ("open", "high", "low", "close", "volume")):
                    rows_by_date[trade_date] = row
            chunk_start = chunk_end + timedelta(days=1)
        return [rows_by_date[key] for key in sorted(rows_by_date)]

    def fetch_valuation_snapshots(self, tickers: list[str]) -> tuple[list[dict], int | None]:
        normalized = [self.to_thscode(ticker) for ticker in tickers]
        rows: list[dict] = []
        timestamps: list[int] = []
        for offset in range(0, len(normalized), 100):
            data = self._request_json(
                "/api/a-share/valuations/snapshot",
                {"thscodes": ",".join(normalized[offset : offset + 100])},
            )
            if data.get("timestamp") is not None:
                timestamps.append(int(data["timestamp"]))
            rows.extend(item for item in (data.get("item") or []) if isinstance(item, dict))
        return rows, max(timestamps, default=None)

    def fetch_financial_statements(self, ticker: str, *, statement: str, period: str = "quarterly", limit: int = 8) -> tuple[list[dict], int | None]:
        paths = {
            "income": "/api/a-share/financials/income-statements",
            "balance": "/api/a-share/financials/balance-sheets",
            "cash_flow": "/api/a-share/financials/cash-flow-statements",
        }
        if statement not in paths:
            raise ValueError(f"Unsupported statement: {statement}")
        data = self._request_json(
            paths[statement],
            {"thscode": self.to_thscode(ticker), "period": period, "limit": max(1, min(20, int(limit)))},
        )
        return [item for item in (data.get("item") or []) if isinstance(item, dict)], self._optional_int(data.get("timestamp"))

    def fetch_financial_indicators(self, ticker: str, *, report: str) -> tuple[dict[str, float | None], dict]:
        data = self._request_json(
            "/api/a-share/financials/indicators",
            {"thscode": self.to_thscode(ticker), "report": report},
        )
        values: dict[str, float | None] = {}
        for block in data.get("abilities") or []:
            if not isinstance(block, dict):
                continue
            for item in block.get("indicators") or []:
                if isinstance(item, dict) and item.get("index_id"):
                    values[str(item["index_id"])] = self._to_float(item.get("value"))
        return values, data

    def get_market_dump_download_url(self, kind: str = "daily-k-10d") -> tuple[str, str | None]:
        normalized = str(kind or "").strip().lower()
        if normalized not in {"daily-k", "daily-k-10d", "adjustment-factors"}:
            raise ValueError(f"Unsupported HiThink market dump kind: {kind}")
        data = self._request_json(f"/api/dump/market-dumps/{normalized}/download-url")
        url = str(data.get("presigned_url") or "").strip()
        if not url:
            raise HithinkFinanceAPIError("HiThink did not return a market dump download URL.", request_id=self.last_request_id)
        return url, str(data.get("presigned_url_expires_at") or "").strip() or None

    def download_market_dump(self, *, kind: str, destination: Path) -> dict:
        url, expires_at = self.get_market_dump_download_url(kind)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.download")
        request = Request(url, headers={"User-Agent": "Personal-Quant-Workbench/1.0"})
        retry_count = max(0, int(self.settings.hithink_finance_max_retries))
        last_error: Exception | None = None
        try:
            for attempt in range(retry_count + 1):
                try:
                    self._wait_for_request_slot()
                    with urlopen(
                        request,
                        timeout=max(
                            60.0,
                            float(self.settings.hithink_finance_timeout_seconds),
                        ),
                    ) as response:
                        content_length = response.headers.get("Content-Length")
                        with temporary.open("wb") as output_file:
                            shutil.copyfileobj(
                                response,
                                output_file,
                                length=1024 * 1024,
                            )
                    downloaded_bytes = temporary.stat().st_size
                    if content_length and downloaded_bytes != int(content_length):
                        raise HithinkFinanceAPIError(
                            "HiThink market dump download was truncated."
                        )
                    with temporary.open("rb") as downloaded_file:
                        header = downloaded_file.read(4)
                        downloaded_file.seek(-4, 2)
                        footer = downloaded_file.read(4)
                    if header != b"PAR1" or footer != b"PAR1":
                        raise HithinkFinanceAPIError(
                            "HiThink market dump download is not a complete Parquet file."
                        )
                    temporary.replace(destination)
                    break
                except (
                    HTTPError,
                    URLError,
                    TimeoutError,
                    OSError,
                    ValueError,
                    HithinkFinanceAPIError,
                ) as exc:
                    last_error = exc
                    temporary.unlink(missing_ok=True)
                    if attempt >= retry_count:
                        raise HithinkFinanceAPIError(
                            f"HiThink market dump download failed after {retry_count + 1} attempt(s): "
                            f"{type(last_error).__name__}",
                            request_id=self.last_request_id,
                        ) from last_error
                    time.sleep(min(4.0, 0.5 * (2**attempt)))
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "presigned_url_expires_at": expires_at,
            "request_id": self.last_request_id,
        }

    @staticmethod
    def report_code(row: dict) -> str | None:
        year = HithinkFinanceClient._optional_int(row.get("fiscal_year"))
        if year is None:
            return None
        raw = str(row.get("fiscal_period") or "").strip().upper()
        mapping = {"Q1": 1, "1Q": 1, "H1": 2, "HY": 2, "Q2": 2, "Q3": 3, "3Q": 3, "FY": 4, "Q4": 4, "ANNUAL": 4}
        quarter = mapping.get(raw)
        if quarter is None and row.get("period_end_ms") is not None:
            month = datetime.fromtimestamp(float(row["period_end_ms"]) / 1000.0, tz=SHANGHAI_TZ).month
            quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(month)
        return f"{year}-{quarter}" if quarter else None

    @staticmethod
    def _optional_int(value) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(value) -> float | None:
        if value is None:
            return None
        try:
            text = str(value).strip().replace(",", "")
            if text.endswith("%"):
                text = text[:-1]
            return float(text) if text else None
        except (TypeError, ValueError):
            return None
