from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time as datetime_time, timedelta
import gzip
import hashlib
import json
import math
import threading
import time

from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.services.hithink_finance_client import HithinkFinanceClient, SHANGHAI_TZ
from app.services.openbb_client import OpenBBClient
from app.services.ticker_format import normalize_ticker_for_market
from app.services.tushare_client import TushareClient


class BaseFundamentalProvider(ABC):
    name: str = "base"

    def __init__(self) -> None:
        self.last_source_used = self.name

    @abstractmethod
    def fetch_snapshot(self, ticker: str) -> dict | None:
        raise NotImplementedError

    def fetch_snapshots(self, tickers: list[str], metadata: dict[str, dict] | None = None) -> list[dict]:
        rows: list[dict] = []
        for ticker in tickers:
            snapshot = self.fetch_snapshot(ticker)
            if snapshot:
                rows.append(snapshot)
        return rows


class OpenBBFundamentalProvider(BaseFundamentalProvider):
    name = "openbb_fundamentals"

    def __init__(self) -> None:
        super().__init__()
        self.client = OpenBBClient()

    def fetch_snapshot(self, ticker: str) -> dict | None:
        snapshot = self.client.fetch_fundamental_snapshot(ticker)
        self.last_source_used = getattr(self.client, "last_source_used", self.name) or self.name
        return snapshot


class TushareFundamentalProvider(BaseFundamentalProvider):
    name = "tushare"

    def __init__(self) -> None:
        super().__init__()
        self.client = TushareClient()

    def fetch_snapshot(self, ticker: str) -> dict | None:
        if not self.client.is_configured():
            self.last_source_used = "tushare_unavailable"
            return None
        rows = self.client.fetch_cn_growth_value_candidates([normalize_ticker_for_market(ticker, "CN")])
        if not rows:
            return None
        self.last_source_used = self.name
        return self._row_to_snapshot(rows[0])

    def fetch_snapshots(self, tickers: list[str], metadata: dict[str, dict] | None = None) -> list[dict]:
        if not self.client.is_configured():
            self.last_source_used = "tushare_unavailable"
            return []
        normalized = [normalize_ticker_for_market(ticker, "CN") for ticker in tickers if str(ticker or "").strip()]
        if not normalized:
            return []
        from app.services.market_lake import get_latest_lake_trade_date

        rows = self.client.fetch_cn_growth_value_candidates(
            normalized,
            stock_meta_by_ticker=metadata or None,
            as_of_date=get_latest_lake_trade_date(market="CN"),
        )
        self.last_source_used = self.name if rows else self.last_source_used
        return [self._row_to_snapshot(row) for row in rows]

    def _row_to_snapshot(self, row) -> dict:
        return {
            "ticker": normalize_ticker_for_market(row.ticker, "CN"),
            "report_date": row.report_date,
            "name": row.name,
            "exchange": row.exchange,
            "listing_date": row.listing_date,
            "pe_ttm": row.pe_ttm,
            "dividend_yield": row.dividend_yield,
            "market_cap": row.market_cap,
            "roe_avg_3y": row.roe_avg_3y,
            "net_profit_yoy": row.net_profit_yoy,
            "revenue_yoy": row.revenue_yoy,
            "debt_to_assets": row.debt_to_assets,
            "raw_data": row.raw_data,
        }


class HithinkFinanceFundamentalProvider(BaseFundamentalProvider):
    """Official A-share statements, server-computed indicators and valuation.

    Current valuation carries its own timestamp so it cannot leak into old
    point-in-time samples when combined with an older financial report.
    """

    name = "hithink_finance"

    def __init__(self) -> None:
        super().__init__()
        self.client = HithinkFinanceClient()
        self.last_error: str | None = None
        self.last_diagnostics: dict[str, object] = {}

    def is_configured(self) -> bool:
        return self.client.is_configured()

    def fetch_snapshot(self, ticker: str) -> dict | None:
        rows = self.fetch_snapshots([ticker])
        return rows[0] if rows else None

    def fetch_snapshots(self, tickers: list[str], metadata: dict[str, dict] | None = None) -> list[dict]:
        normalized = sorted(
            {
                normalize_ticker_for_market(ticker, "CN")
                for ticker in tickers
                if str(ticker or "").strip()
            }
        )
        if not normalized or not self.is_configured():
            self.last_source_used = "hithink_finance_unavailable"
            return []

        valuation_rows, valuation_timestamp = self.client.fetch_valuation_snapshots(normalized)
        valuations = {
            self.client.to_internal_ticker(str(row.get("thscode") or "")): row
            for row in valuation_rows
            if row.get("thscode")
        }
        snapshots: list[dict] = []
        failures: list[str] = []
        tickers_with_snapshots = 0
        for ticker in normalized:
            try:
                ticker_snapshots = self._fetch_ticker_snapshots(
                    ticker,
                    valuation=valuations.get(ticker) or {},
                    valuation_timestamp=valuation_timestamp,
                    metadata=(metadata or {}).get(ticker) or {},
                )
                if ticker_snapshots:
                    snapshots.extend(ticker_snapshots)
                    tickers_with_snapshots += 1
            except Exception as exc:
                failures.append(f"{ticker}: {exc}")

        self.last_error = "; ".join(failures) or None
        self.last_diagnostics = {
            "requested_tickers": len(normalized),
            "valuation_rows": len(valuation_rows),
            "snapshots_returned": len(snapshots),
            "tickers_with_snapshots": tickers_with_snapshots,
            "historical_periods_returned": max(0, len(snapshots) - tickers_with_snapshots),
            "failed_tickers": len(failures),
            "request_id": self.client.last_request_id,
        }
        self.last_source_used = self.name if snapshots else "hithink_finance_empty"
        return snapshots

    def _fetch_ticker_snapshots(
        self,
        ticker: str,
        *,
        valuation: dict,
        valuation_timestamp: int | None,
        metadata: dict,
    ) -> list[dict]:
        income_rows, _ = self.client.fetch_financial_statements(
            ticker, statement="income", period="quarterly", limit=20
        )
        balance_rows, _ = self.client.fetch_financial_statements(
            ticker, statement="balance", period="quarterly", limit=20
        )
        indicator_values: dict[str, float | None] = {}
        indicator_raw: dict = {}
        latest_report_code = self.client.report_code(income_rows[0]) if income_rows else None
        if latest_report_code:
            try:
                indicator_values, indicator_raw = self.client.fetch_financial_indicators(
                    ticker,
                    report=latest_report_code,
                )
            except Exception:
                indicator_values = {}

        period_rows = income_rows or ([{}] if balance_rows or valuation else [])
        snapshots: list[dict] = []
        for index, income_row in enumerate(period_rows):
            snapshot = self._build_period_snapshot(
                ticker,
                income_row=income_row,
                income_rows=income_rows,
                balance_rows=balance_rows,
                valuation=valuation if index == 0 else {},
                valuation_timestamp=valuation_timestamp if index == 0 else None,
                metadata=metadata,
                indicator_values=indicator_values if index == 0 else {},
                indicator_raw=indicator_raw if index == 0 else {},
            )
            if snapshot:
                snapshots.append(snapshot)
        return snapshots

    def _build_period_snapshot(
        self,
        ticker: str,
        *,
        income_row: dict,
        income_rows: list[dict],
        balance_rows: list[dict],
        valuation: dict,
        valuation_timestamp: int | None,
        metadata: dict,
        indicator_values: dict[str, float | None],
        indicator_raw: dict,
    ) -> dict | None:
        latest_income = income_row
        latest_balance = self._matching_balance(latest_income, balance_rows)
        report_code = self.client.report_code(latest_income)

        period_end_ms = self._first_int(latest_income.get("period_end_ms"), latest_balance.get("period_end_ms"))
        report_date_ms = self._max_int(latest_income.get("report_date_ms"), latest_balance.get("report_date_ms"))
        fallback_ms = valuation_timestamp or int(datetime.now(SHANGHAI_TZ).timestamp() * 1000)
        event_ms = period_end_ms or fallback_ms
        available_ms = max(event_ms, report_date_ms or fallback_ms)
        event_dt = datetime.fromtimestamp(event_ms / 1000.0, tz=SHANGHAI_TZ)
        available_date = datetime.fromtimestamp(available_ms / 1000.0, tz=SHANGHAI_TZ).date()
        available_dt = datetime.combine(available_date, datetime_time.max, tzinfo=SHANGHAI_TZ)
        valuation_dt = (
            datetime.fromtimestamp(valuation_timestamp / 1000.0, tz=SHANGHAI_TZ)
            if valuation_timestamp is not None
            else datetime.now(SHANGHAI_TZ)
        )
        observed_at = datetime.now(SHANGHAI_TZ)

        pe_ttm = self.client._to_float(valuation.get("pe_ttm"))
        revenue_yoy = indicator_values.get("calculate_operating_income_yoy_growth_ratio")
        net_profit_yoy = indicator_values.get("calculate_parent_holder_net_profit_yoy_growth_ratio")
        debt_to_assets = indicator_values.get("assets_debt_ratio")
        if revenue_yoy is None:
            revenue_yoy = self._statement_yoy(latest_income, income_rows, "operating_income")
        if net_profit_yoy is None:
            net_profit_yoy = self._statement_yoy(latest_income, income_rows, "parent_holder_net_profit")
        if debt_to_assets is None:
            assets = self.client._to_float(latest_balance.get("assets_total"))
            debt = self.client._to_float(latest_balance.get("total_debt"))
            if assets not in (None, 0) and debt is not None:
                debt_to_assets = debt / assets * 100.0

        financial_values = {
            "net_profit_yoy": net_profit_yoy,
            "revenue_yoy": revenue_yoy,
            "debt_to_assets": debt_to_assets,
        }
        financial_revision = hashlib.sha256(
            json.dumps(
                {"ticker": ticker, "report": report_code, "values": financial_values},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:20]
        valuation_revision = hashlib.sha256(
            json.dumps(
                {"ticker": ticker, "timestamp": valuation_timestamp, "pe_ttm": pe_ttm},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:20]
        feature_times = {
            name: {
                "event_time": event_dt.isoformat(),
                "available_time": available_dt.isoformat(),
                "ingested_time": observed_at.isoformat(),
                "revision_id": financial_revision,
            }
            for name, value in financial_values.items()
            if value is not None
        }
        if pe_ttm is not None:
            feature_times["pe_ttm"] = {
                "event_time": valuation_dt.isoformat(),
                "available_time": valuation_dt.isoformat(),
                "ingested_time": observed_at.isoformat(),
                "revision_id": valuation_revision,
            }
        if not latest_income and not latest_balance and pe_ttm is None:
            return None

        report_date = event_dt.date().isoformat()
        exchange = metadata.get("exchange")
        if not exchange:
            exchange = "SSE" if ticker.endswith(".SS") else "BSE" if ticker.endswith(".BJ") else "SZSE"
        return {
            "ticker": ticker,
            "report_date": report_date,
            "available_time": available_dt.isoformat(),
            "ingested_time": observed_at.isoformat(),
            "revision_id": financial_revision,
            "source_record_id": f"{ticker}:{report_date}:{report_code or 'valuation'}",
            "revision_history_preserved": False,
            "feature_times": feature_times,
            "name": metadata.get("name") or valuation.get("name"),
            "exchange": exchange,
            "listing_date": metadata.get("listing_date"),
            "pe_ttm": pe_ttm,
            "dividend_yield": None,
            "market_cap": None,
            "roe_avg_3y": None,
            "net_profit_yoy": net_profit_yoy,
            "revenue_yoy": revenue_yoy,
            "debt_to_assets": debt_to_assets,
            "raw_data": {
                "provider": self.name,
                "report_code": report_code,
                "income": latest_income,
                "balance": latest_balance,
                "indicators": indicator_raw,
                "valuation": valuation,
                "valuation_timestamp": valuation_timestamp,
                "point_in_time_note": "Current valuation is timestamped separately and must not be backfilled into historical samples.",
            },
        }

    @staticmethod
    def _matching_balance(income: dict, balances: list[dict]) -> dict:
        period_end = income.get("period_end_ms")
        if period_end is None:
            return balances[0] if balances else {}
        return next((row for row in balances if row.get("period_end_ms") == period_end), {})

    def _statement_yoy(self, latest: dict, rows: list[dict], field: str) -> float | None:
        current = self.client._to_float(latest.get(field))
        current_year = self.client._optional_int(latest.get("fiscal_year"))
        period = str(latest.get("fiscal_period") or "")
        if current is None or current_year is None:
            return None
        previous = next(
            (
                row
                for row in rows
                if self.client._optional_int(row.get("fiscal_year")) == current_year - 1
                and str(row.get("fiscal_period") or "") == period
            ),
            None,
        )
        prior = self.client._to_float((previous or {}).get(field))
        if prior in (None, 0):
            return None
        return (current / prior - 1.0) * 100.0

    @staticmethod
    def _first_int(*values) -> int | None:
        for value in values:
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _max_int(*values) -> int | None:
        parsed: list[int] = []
        for value in values:
            try:
                if value is not None:
                    parsed.append(int(value))
            except (TypeError, ValueError):
                continue
        return max(parsed, default=None)


class CommunityCNFundamentalProvider(BaseFundamentalProvider):
    """Credential-free A-share fundamentals with source publication dates.

    Eastmoney's public financial-analysis endpoint is also wrapped by AKShare,
    which is already an optional project dependency.  Calling the underlying
    batch endpoint here avoids one HTTP request per ticker and retains the raw
    NOTICE_DATE / UPDATE_DATE fields needed by point-in-time research.
    """

    name = "community_eastmoney"
    _financial_endpoint = "https://datacenter.eastmoney.com/securities/api/data/get"
    _valuation_endpoint = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    _quote_endpoint = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    _quote_fallback_endpoint = "https://82.push2.eastmoney.com/api/qt/ulist.np/get"
    _timezone = ZoneInfo("Asia/Shanghai")
    _batch_size = 80

    def __init__(self) -> None:
        super().__init__()
        self.last_error: str | None = None
        self.last_diagnostics: dict[str, object] = {}

    def is_configured(self) -> bool:
        return True

    def fetch_snapshot(self, ticker: str) -> dict | None:
        rows = self.fetch_snapshots([ticker])
        return rows[0] if rows else None

    def fetch_snapshots(self, tickers: list[str], metadata: dict[str, dict] | None = None) -> list[dict]:
        normalized = sorted(
            {
                normalize_ticker_for_market(ticker, "CN")
                for ticker in tickers
                if str(ticker or "").strip()
            }
        )
        if not normalized:
            return []

        as_of_date = self._resolve_as_of_date()
        financial_rows: list[dict] = []
        failed_batches: list[str] = []
        for batch in self._batches(normalized, self._batch_size):
            try:
                financial_rows.extend(self._fetch_financial_rows(batch, as_of_date=as_of_date))
            except Exception as exc:
                failed_batches.append(f"{batch[0]}..{batch[-1]}: {exc}")

        try:
            valuation_by_ticker = self._fetch_valuation_rows(normalized, as_of_date=as_of_date)
        except Exception as exc:
            valuation_by_ticker = {}
            failed_batches.append(f"valuation: {exc}")
        try:
            dividends_by_ticker = self._fetch_dividend_rows(normalized, as_of_date=as_of_date)
        except Exception as exc:
            dividends_by_ticker = {}
            failed_batches.append(f"dividends: {exc}")

        result = self._build_snapshots(
            normalized,
            financial_rows=financial_rows,
            valuation_by_ticker=valuation_by_ticker,
            dividends_by_ticker=dividends_by_ticker,
            metadata=metadata or {},
            as_of_date=as_of_date,
        )
        self.last_error = "; ".join(failed_batches) or None
        self.last_diagnostics = {
            "requested_tickers": len(normalized),
            "financial_source_rows": len(financial_rows),
            "valuation_rows": len(valuation_by_ticker),
            "dividend_tickers": len(dividends_by_ticker),
            "snapshots_returned": len(result),
            "failed_batches": failed_batches,
            "as_of_date": as_of_date.isoformat(),
        }
        self.last_source_used = self.name if result else "community_eastmoney_empty"
        return result

    @staticmethod
    def _batches(values: list[str], size: int):
        for offset in range(0, len(values), size):
            yield values[offset : offset + size]

    @staticmethod
    def _to_eastmoney_code(ticker: str) -> str | None:
        normalized = normalize_ticker_for_market(ticker, "CN")
        if normalized.endswith(".SS"):
            return f"{normalized[:-3]}.SH"
        if normalized.endswith((".SZ", ".BJ")):
            return normalized
        return None

    @staticmethod
    def _to_secid(ticker: str) -> str | None:
        normalized = normalize_ticker_for_market(ticker, "CN")
        code = normalized.split(".", 1)[0]
        if len(code) != 6 or not code.isdigit():
            return None
        market_code = "1" if normalized.endswith(".SS") else "0"
        return f"{market_code}.{code}"

    def _resolve_as_of_date(self) -> date:
        try:
            from app.services.market_lake import get_latest_lake_trade_date

            value = get_latest_lake_trade_date(market="CN")
            if value:
                return date.fromisoformat(str(value)[:10])
        except Exception:
            pass
        return datetime.now(self._timezone).date()

    def _get_json(self, endpoint: str, params: dict[str, str]) -> dict:
        url = f"{endpoint}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://data.eastmoney.com/",
                "User-Agent": "Mozilla/5.0",
            },
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                with urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"Community data request failed after retries: {last_error}")

    def _fetch_financial_rows(self, tickers: list[str], *, as_of_date: date) -> list[dict]:
        provider_codes = [self._to_eastmoney_code(ticker) for ticker in tickers]
        provider_codes = [code for code in provider_codes if code]
        if not provider_codes:
            return []
        cutoff = date(as_of_date.year - 4, 1, 1).isoformat()
        quoted_codes = ",".join(f'"{code}"' for code in provider_codes)
        rows: list[dict] = []
        page = 1
        while True:
            payload = self._get_json(
                self._financial_endpoint,
                {
                    "type": "RPT_F10_FINANCE_MAINFINADATA",
                    "sty": "APP_F10_MAINFINADATA",
                    "quoteColumns": "",
                    "filter": f"(SECUCODE in ({quoted_codes}))(REPORT_DATE>='{cutoff}')",
                    "p": str(page),
                    "ps": "5000",
                    "sr": "-1",
                    "st": "REPORT_DATE",
                    "source": "HSF10",
                    "client": "PC",
                },
            )
            result = payload.get("result") or {}
            page_rows = result.get("data") or []
            rows.extend(item for item in page_rows if isinstance(item, dict))
            total_pages = int(result.get("pages") or 1)
            if page >= total_pages:
                break
            page += 1
        return rows

    def _fetch_valuation_rows(self, tickers: list[str], *, as_of_date: date) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for batch in self._batches(tickers, 80):
            code_to_ticker = {ticker.split(".", 1)[0]: ticker for ticker in batch}
            quoted_codes = ",".join(f'"{code}"' for code in code_to_ticker)
            try:
                payload = self._get_json(
                    self._valuation_endpoint,
                    {
                        "sortColumns": "TRADE_DATE",
                        "sortTypes": "-1",
                        "pageSize": "5000",
                        "pageNumber": "1",
                        "reportName": "RPT_VALUEANALYSIS_DET",
                        "columns": "ALL",
                        "quoteColumns": "",
                        "source": "WEB",
                        "client": "WEB",
                        "filter": (
                            f"(SECURITY_CODE in ({quoted_codes}))"
                            f"(TRADE_DATE='{as_of_date.isoformat()}')"
                        ),
                    },
                )
            except Exception:
                continue
            for row in ((payload.get("result") or {}).get("data") or []):
                code = str(row.get("SECURITY_CODE") or "").zfill(6)
                ticker = code_to_ticker.get(code)
                if ticker:
                    result[ticker] = {
                        "f12": code,
                        "f14": row.get("SECURITY_NAME_ABBR"),
                        "f20": row.get("TOTAL_MARKET_CAP"),
                        "f115": row.get("PE_TTM"),
                        "close": row.get("CLOSE_PRICE"),
                        "trade_date": row.get("TRADE_DATE"),
                    }
        if len(result) == len(tickers):
            return result

        # The quote service is a current-data fallback when the historical
        # valuation dataset is temporarily unavailable or not yet populated.
        missing_tickers = [ticker for ticker in tickers if ticker not in result]
        for batch in self._batches(missing_tickers, 120):
            secid_to_ticker = {
                secid: ticker
                for ticker in batch
                if (secid := self._to_secid(ticker)) is not None
            }
            if not secid_to_ticker:
                continue
            params = {
                "fltt": "2",
                "secids": ",".join(secid_to_ticker),
                "fields": "f12,f14,f20,f115",
            }
            try:
                payload = self._get_json(self._quote_endpoint, params)
            except Exception:
                try:
                    payload = self._get_json(self._quote_fallback_endpoint, params)
                except Exception:
                    continue
            for row in ((payload.get("data") or {}).get("diff") or []):
                code = str(row.get("f12") or "").zfill(6)
                ticker = next(
                    (item for item in batch if item.split(".", 1)[0] == code),
                    None,
                )
                if ticker:
                    result[ticker] = row
        return result

    def _fetch_dividend_rows(self, tickers: list[str], *, as_of_date: date) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        cutoff = (as_of_date - timedelta(days=370)).isoformat()
        for batch in self._batches(tickers, 80):
            code_to_ticker = {ticker.split(".", 1)[0]: ticker for ticker in batch}
            result.update({ticker: [] for ticker in batch})
            quoted_codes = ",".join(f'"{code}"' for code in code_to_ticker)
            payload = self._get_json(
                self._valuation_endpoint,
                {
                    "sortColumns": "EX_DIVIDEND_DATE",
                    "sortTypes": "-1",
                    "pageSize": "5000",
                    "pageNumber": "1",
                    "reportName": "RPT_SHAREBONUS_DET",
                    "columns": "ALL",
                    "quoteColumns": "",
                    "source": "WEB",
                    "client": "WEB",
                    "filter": (
                        f"(SECURITY_CODE in ({quoted_codes}))"
                        f"(EX_DIVIDEND_DATE>='{cutoff}')"
                        f"(EX_DIVIDEND_DATE<='{as_of_date.isoformat()}')"
                    ),
                },
            )
            for row in ((payload.get("result") or {}).get("data") or []):
                code = str(row.get("SECURITY_CODE") or "").zfill(6)
                ticker = code_to_ticker.get(code)
                ex_date = self._safe_date(row.get("EX_DIVIDEND_DATE"))
                notice_date = self._safe_date(row.get("NOTICE_DATE") or row.get("PLAN_NOTICE_DATE"))
                cash_per_ten = self._to_float(row.get("PRETAX_BONUS_RMB"))
                if (
                    ticker
                    and ex_date is not None
                    and cutoff <= ex_date.isoformat() <= as_of_date.isoformat()
                    and notice_date is not None
                    and notice_date <= as_of_date
                    and cash_per_ten is not None
                    and cash_per_ten >= 0
                ):
                    result[ticker].append(
                        {
                            "report_date": row.get("REPORT_DATE"),
                            "notice_date": notice_date.isoformat(),
                            "ex_dividend_date": ex_date.isoformat(),
                            "cash_per_ten": cash_per_ten,
                        }
                    )
        return result

    def _build_snapshots(
        self,
        tickers: list[str],
        *,
        financial_rows: list[dict],
        valuation_by_ticker: dict[str, dict],
        dividends_by_ticker: dict[str, list[dict]],
        metadata: dict[str, dict],
        as_of_date: date,
    ) -> list[dict]:
        by_ticker: dict[str, list[dict]] = {ticker: [] for ticker in tickers}
        for row in financial_rows:
            provider_code = str(row.get("SECUCODE") or "").strip().upper()
            ticker = normalize_ticker_for_market(provider_code, "CN")
            if ticker not in by_ticker:
                continue
            report_date = self._safe_date(row.get("REPORT_DATE"))
            notice_date = self._safe_date(row.get("NOTICE_DATE"))
            update_date = self._safe_date(row.get("UPDATE_DATE"))
            available_date = max(item for item in (notice_date, update_date) if item is not None) if any(
                item is not None for item in (notice_date, update_date)
            ) else None
            if report_date is None or report_date > as_of_date:
                continue
            if available_date is None or available_date > as_of_date:
                continue
            row_copy = dict(row)
            row_copy["_report_date"] = report_date
            row_copy["_available_date"] = available_date
            by_ticker[ticker].append(row_copy)

        snapshots: list[dict] = []
        valuation_available = datetime.combine(
            as_of_date,
            datetime_time(hour=16, minute=30),
            tzinfo=self._timezone,
        )
        valuation_event = datetime.combine(as_of_date, datetime_time.min, tzinfo=self._timezone)
        observed_at = datetime.now(self._timezone)
        for ticker in tickers:
            history = sorted(
                by_ticker.get(ticker) or [],
                key=lambda item: (item["_report_date"], item["_available_date"]),
                reverse=True,
            )
            if not history and ticker not in valuation_by_ticker:
                continue
            latest = history[0] if history else {}
            report_date = latest.get("_report_date") or as_of_date
            available_date = latest.get("_available_date") or as_of_date
            financial_available = datetime.combine(
                available_date,
                datetime_time.max,
                tzinfo=self._timezone,
            )
            annual_roe: list[float] = []
            seen_years: set[int] = set()
            for item in history:
                item_report_date = item["_report_date"]
                if item_report_date.month != 12 or item_report_date.day != 31:
                    continue
                if item_report_date.year in seen_years:
                    continue
                value = self._to_float(item.get("ROEJQ"))
                if value is None:
                    continue
                seen_years.add(item_report_date.year)
                annual_roe.append(value)
                if len(annual_roe) == 3:
                    break
            valuation = valuation_by_ticker.get(ticker) or {}
            pe_ttm = self._to_float(valuation.get("f115"))
            market_cap = self._to_float(valuation.get("f20"))
            close_price = self._to_float(valuation.get("close"))
            dividend_rows = dividends_by_ticker.get(ticker)
            dividend_yield = None
            if dividend_rows is not None and close_price not in (None, 0):
                cash_per_share = sum(
                    float(item.get("cash_per_ten") or 0.0) / 10.0
                    for item in dividend_rows
                )
                dividend_yield = round((cash_per_share / float(close_price)) * 100.0, 8)
            valuation_revision_id = hashlib.sha256(
                json.dumps(
                    {
                        "ticker": ticker,
                        "valuation_date": as_of_date.isoformat(),
                        "pe_ttm": pe_ttm,
                        "market_cap": market_cap,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:20]
            feature_times = {
                name: {
                    "event_time": valuation_event.isoformat(),
                    "available_time": valuation_available.isoformat(),
                    "ingested_time": observed_at.isoformat(),
                    "revision_id": valuation_revision_id,
                }
                for name, value in (("pe_ttm", pe_ttm), ("market_cap", market_cap))
                if value is not None
            }
            if dividend_yield is not None:
                dividend_revision_id = hashlib.sha256(
                    json.dumps(
                        {
                            "ticker": ticker,
                            "valuation_date": as_of_date.isoformat(),
                            "close": close_price,
                            "dividends": dividend_rows,
                        },
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()[:20]
                feature_times["dividend_yield"] = {
                    "event_time": valuation_event.isoformat(),
                    "available_time": valuation_available.isoformat(),
                    "ingested_time": observed_at.isoformat(),
                    "revision_id": dividend_revision_id,
                }
            raw_values = {
                "SECUCODE": latest.get("SECUCODE"),
                "REPORT_DATE": latest.get("REPORT_DATE"),
                "NOTICE_DATE": latest.get("NOTICE_DATE"),
                "UPDATE_DATE": latest.get("UPDATE_DATE"),
                "ROEJQ": latest.get("ROEJQ"),
                "TOTALOPERATEREVETZ": latest.get("TOTALOPERATEREVETZ"),
                "PARENTNETPROFITTZ": latest.get("PARENTNETPROFITTZ"),
                "ZCFZL": latest.get("ZCFZL"),
            }
            revision_id = hashlib.sha256(
                json.dumps(
                    {"ticker": ticker, "financial": raw_values},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:20]
            meta = metadata.get(ticker) or {}
            snapshots.append(
                {
                    "ticker": ticker,
                    "report_date": report_date.isoformat(),
                    "available_time": financial_available.isoformat(),
                    # Provider publication time and local observation time are
                    # separate. Backfills must never pretend they were ingested
                    # on the historical publication date.
                    "ingested_time": observed_at.isoformat(),
                    "revision_id": revision_id,
                    "source_record_id": f"{ticker}:{report_date.isoformat()}",
                    "revision_history_preserved": False,
                    "feature_times": feature_times,
                    "name": meta.get("name") or latest.get("SECURITY_NAME_ABBR") or valuation.get("f14"),
                    "exchange": meta.get("exchange"),
                    "listing_date": meta.get("listing_date"),
                    "pe_ttm": pe_ttm,
                    "dividend_yield": dividend_yield,
                    "market_cap": market_cap,
                    "roe_avg_3y": round(sum(annual_roe) / len(annual_roe), 2) if annual_roe else None,
                    "net_profit_yoy": self._to_float(latest.get("PARENTNETPROFITTZ")),
                    "revenue_yoy": self._to_float(latest.get("TOTALOPERATEREVETZ")),
                    "debt_to_assets": self._to_float(latest.get("ZCFZL")),
                    "raw_data": {
                        "provider": self.name,
                        "financial": raw_values,
                        "valuation": valuation,
                        "dividends_ttm": dividend_rows,
                        "annual_roe_observations": annual_roe,
                        "as_of_date": as_of_date.isoformat(),
                    },
                }
            )
        return snapshots

    @staticmethod
    def _safe_date(value: object) -> date | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"none", "nan", "nat"}:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value in (None, "", "-"):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None


class GlobalStockDataSECFundamentalProvider(BaseFundamentalProvider):
    """Official SEC EDGAR company-facts adapter documented by global-stock-data.

    It intentionally supplies filing-derived fundamentals only.  Price and
    valuation fields remain owned by their licensed market-data providers.
    """

    name = "global_stock_data_sec_edgar"

    _ticker_to_cik: dict[str, tuple[int, str]] | None = None
    _request_lock = threading.Lock()
    _last_request_at = 0.0
    _min_request_interval_seconds = 0.125  # Stay below SEC's 10 req/s ceiling.
    # EDGAR dates (``end`` / ``filed``) are plain calendar dates in U.S.
    # Eastern time; U.S. equities trade on the same clock.
    _filed_timezone = ZoneInfo("America/New_York")

    def __init__(self) -> None:
        super().__init__()
        self.settings = get_settings()

    def fetch_snapshot(self, ticker: str) -> dict | None:
        if not self.settings.sec_user_agent:
            self.last_source_used = "sec_edgar_not_configured"
            return None
        normalized = str(ticker or "").strip().upper().replace(".", "-")
        ticker_map = self._load_ticker_map()
        listing = ticker_map.get(normalized)
        if listing is None:
            self.last_source_used = "sec_edgar_ticker_not_found"
            return None
        cik, company_name = listing
        try:
            facts = self._get_json(
                f"{str(self.settings.sec_data_endpoint).rstrip('/')}/api/xbrl/companyfacts/CIK{cik:010d}.json"
            )
        except Exception:
            self.last_source_used = "sec_edgar_unavailable"
            return None
        snapshot = self._facts_to_snapshot(ticker=str(ticker or "").strip().upper(), cik=cik, company_name=company_name, facts=facts)
        self.last_source_used = self.name if snapshot else "sec_edgar_no_annual_facts"
        return snapshot

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": str(self.settings.sec_user_agent),
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }

    def _get_json(self, url: str) -> dict:
        with self.__class__._request_lock:
            elapsed = time.monotonic() - self.__class__._last_request_at
            wait_seconds = self.__class__._min_request_interval_seconds - elapsed
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            self.__class__._last_request_at = time.monotonic()
        request = Request(url, headers=self._headers())
        with urlopen(request, timeout=float(self.settings.sec_timeout_seconds)) as response:
            body = response.read()
            if str(response.headers.get("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body.decode("utf-8"))

    def _load_ticker_map(self) -> dict[str, tuple[int, str]]:
        if self.__class__._ticker_to_cik is not None:
            return self.__class__._ticker_to_cik
        payload = self._get_json(str(self.settings.sec_company_tickers_endpoint))
        mapping: dict[str, tuple[int, str]] = {}
        for row in payload.values() if isinstance(payload, dict) else []:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "").strip().upper()
            try:
                cik = int(row.get("cik_str"))
            except (TypeError, ValueError):
                continue
            if ticker:
                mapping[ticker] = (cik, str(row.get("title") or ticker))
        self.__class__._ticker_to_cik = mapping
        return mapping

    @staticmethod
    def _annual_values(facts: dict, tags: tuple[str, ...]) -> list[dict]:
        us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
        for tag in tags:
            units = (us_gaap.get(tag) or {}).get("units") or {}
            values = units.get("USD") or []
            annual = [
                item for item in values
                if str(item.get("form") or "") in {"10-K", "20-F"}
                and str(item.get("fp") or "").upper() == "FY"
                and str(item.get("end") or "")
            ]
            if annual:
                deduped: dict[str, dict] = {}
                for item in annual:
                    end = str(item.get("end"))
                    # Prefer an amended/most recently filed value for the same end date.
                    if end not in deduped or str(item.get("filed") or "") >= str(deduped[end].get("filed") or ""):
                        deduped[end] = item
                return [deduped[key] for key in sorted(deduped, reverse=True)]
        return []

    @staticmethod
    def _parse_filed_date(value: object) -> date | None:
        """Parse an EDGAR calendar date (``end`` / ``filed``), tolerating a timestamp."""

        text = str(value or "").strip()[:10]
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None

    @staticmethod
    def _value(values: list[dict], index: int = 0) -> float | None:
        try:
            return float(values[index].get("val"))
        except (IndexError, TypeError, ValueError):
            return None

    @staticmethod
    def _yoy(values: list[dict]) -> float | None:
        current = GlobalStockDataSECFundamentalProvider._value(values, 0)
        prior = GlobalStockDataSECFundamentalProvider._value(values, 1)
        if current is None or prior in (None, 0):
            return None
        return (current / prior - 1.0) * 100.0

    def _facts_to_snapshot(self, *, ticker: str, cik: int, company_name: str, facts: dict) -> dict | None:
        revenue = self._annual_values(facts, ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"))
        net_income = self._annual_values(facts, ("NetIncomeLoss",))
        assets = self._annual_values(facts, ("Assets",))
        liabilities = self._annual_values(facts, ("Liabilities",))
        anchor = revenue or net_income or assets
        if not anchor:
            return None
        asset_value = self._value(assets)
        liability_value = self._value(liabilities)
        report_date = str(anchor[0].get("end"))
        debt_to_assets = (
            (liability_value / asset_value) * 100.0
            if asset_value and liability_value is not None
            else None
        )
        net_profit_yoy = self._yoy(net_income)
        revenue_yoy = self._yoy(revenue)
        # EDGAR's ``filed`` is the date the 10-K/20-F was accepted, in U.S.
        # Eastern time.  A report is only safe to consume once that whole day
        # has elapsed on the exchange clock, so availability is the *end* of
        # the filed day in America/New_York (an absolute instant once the
        # tz-aware datetime is stored; the repository normalizes it to UTC).
        # Using the filing date instead of the local sync time recovers the
        # historical window between filing and ingestion without leaking a
        # filing that had not happened yet.
        #
        # When EDGAR omits ``filed`` we leave the timestamps unset on purpose:
        # the consumer then falls back to the local ingestion time, keeping the
        # previous conservative behaviour rather than inventing a date.
        filed_date = self._parse_filed_date(anchor[0].get("filed"))
        event_date = self._parse_filed_date(report_date)
        feature_times: dict[str, dict] = {}
        available_time: str | None = None
        if filed_date is not None and event_date is not None:
            available_dt = datetime.combine(
                filed_date, datetime_time.max, tzinfo=self._filed_timezone
            )
            event_dt = datetime.combine(
                event_date, datetime_time.min, tzinfo=self._filed_timezone
            )
            feature_values = {
                "net_profit_yoy": net_profit_yoy,
                "revenue_yoy": revenue_yoy,
                "debt_to_assets": debt_to_assets,
            }
            revision_id = hashlib.sha256(
                json.dumps(
                    {
                        "ticker": ticker,
                        "report_date": report_date,
                        "filed": filed_date.isoformat(),
                        "values": {
                            name: value
                            for name, value in feature_values.items()
                            if value is not None
                        },
                    },
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()[:20]
            available_time = available_dt.isoformat()
            # ``ingested_time`` is intentionally left unset: it is stamped by
            # the caller (the sync run), so a backfill never pretends the
            # filing was observed on its historical filing date.
            feature_times = {
                name: {
                    "event_time": event_dt.isoformat(),
                    "available_time": available_dt.isoformat(),
                    "revision_id": revision_id,
                }
                for name, value in feature_values.items()
                if value is not None
            }
        return {
            "ticker": ticker,
            "report_date": report_date,
            "available_time": available_time,
            "feature_times": feature_times,
            "name": company_name,
            "exchange": None,
            "listing_date": None,
            "pe_ttm": None,
            "dividend_yield": None,
            "market_cap": None,
            "roe_avg_3y": None,
            "net_profit_yoy": net_profit_yoy,
            "revenue_yoy": revenue_yoy,
            "debt_to_assets": debt_to_assets,
            "raw_data": {
                "provider": self.name,
                "source_url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                "cik": f"{cik:010d}",
                "as_of_date": report_date,
                "annual_revenue_usd": self._value(revenue),
                "annual_net_income_usd": self._value(net_income),
                "annual_assets_usd": asset_value,
                "annual_liabilities_usd": liability_value,
                "fetched_on": date.today().isoformat(),
            },
        }


def resolve_fundamental_provider(name: str | None, *, market: str | None = None) -> BaseFundamentalProvider:
    normalized = str(name or "").strip().lower()
    market_code = str(market or "").strip().upper()
    if normalized in {"", "auto"}:
        if market_code == "CN":
            return CommunityCNFundamentalProvider()
        return OpenBBFundamentalProvider()
    if normalized in {"community", "community_cn", "eastmoney", "akshare"} and market_code == "CN":
        return CommunityCNFundamentalProvider()
    if normalized in {"hithink", "hithink_finance", "tonghuashun", "ths"} and market_code == "CN":
        return HithinkFinanceFundamentalProvider()
    if normalized == "tushare":
        return TushareFundamentalProvider()
    if normalized == "openbb":
        return OpenBBFundamentalProvider()
    if normalized in {"global_stock_data", "global_stock_data_sec", "sec", "sec_edgar"} and market_code == "US":
        return GlobalStockDataSECFundamentalProvider()
    if market_code == "CN":
        return TushareFundamentalProvider()
    return OpenBBFundamentalProvider()
