from dataclasses import dataclass
from datetime import date, datetime
from io import StringIO
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.services.ticker_format import provider_ticker_candidates
from app.services.tushare_client import TushareClient


def _to_iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return text[:10] if len(text) >= 10 else text


@dataclass(slots=True)
class HistoricalPriceRequest:
    ticker: str
    start_date: str | None = None
    end_date: str | None = None
    provider: str = "yfinance"


class OpenBBClient:
    """Thin wrapper placeholder for the OpenBB SDK."""

    def __init__(self) -> None:
        self.last_source_used = "unknown"

    def _load_openbb(self):
        try:
            from openbb import obb
        except Exception:
            return None
        return obb

    def fetch_symbol_profile(self, ticker: str) -> dict:
        self.last_source_used = "unknown"
        market = "HK" if ticker.upper().endswith(".HK") else None
        last_result = {"ticker": ticker.upper(), "name": None, "exchange": None}
        for candidate in provider_ticker_candidates(ticker, market):
            result = self._fetch_single_symbol_profile(candidate)
            if result.get("name") or result.get("exchange"):
                result["ticker"] = ticker.upper()
                self.last_source_used = "openbb_or_yfinance_profile"
                return result
            last_result = result
        last_result["ticker"] = ticker.upper()
        return last_result

    def _fetch_single_symbol_profile(self, ticker: str) -> dict:
        obb = self._load_openbb()
        if obb is None:
            return self._fetch_profile_with_yfinance(ticker)

        try:
            output = obb.equity.profile(symbol=ticker)
            records = output.to_dict()
            if isinstance(records, list) and records:
                record = records[0]
            elif isinstance(records, dict):
                record = records
            else:
                return self._fetch_profile_with_yfinance(ticker)
        except Exception:
            return self._fetch_profile_with_yfinance(ticker)

        return {
            "ticker": ticker.upper(),
            "name": record.get("name") or record.get("company_name") or record.get("long_name"),
            "exchange": record.get("exchange") or record.get("exchange_name"),
        }

    def fetch_historical_prices(self, request: HistoricalPriceRequest) -> list[dict]:
        self.last_source_used = "unknown"
        market = self._infer_market(request.ticker)
        if market == "CN":
            return self._fetch_cn_history_with_fallbacks(request)
        obb = self._load_openbb()
        if obb is None:
            rows = self._fetch_with_yfinance(request)
            if market in {"HK", "CN"} and len(rows) <= 1:
                ak_rows = self._fetch_with_akshare(request)
                if len(ak_rows) > len(rows):
                    self.last_source_used = "akshare"
                    return ak_rows
                if market == "CN":
                    baostock_rows = self._fetch_with_baostock(request)
                    if len(baostock_rows) > len(rows):
                        self.last_source_used = "baostock"
                        return baostock_rows
                if market == "HK":
                    stockanalysis_rows = self._fetch_with_stockanalysis(request)
                    if len(stockanalysis_rows) > len(rows):
                        self.last_source_used = "stockanalysis"
                        return stockanalysis_rows
            self.last_source_used = "yfinance"
            return rows

        try:
            output = obb.equity.price.historical(
                symbol=request.ticker,
                start_date=request.start_date,
                end_date=request.end_date,
                provider=request.provider,
            )
            records = output.to_dict()
        except Exception:
            if request.provider == "yfinance":
                rows = self._fetch_with_yfinance(request)
                if market in {"HK", "CN"} and len(rows) <= 1:
                    ak_rows = self._fetch_with_akshare(request)
                    if len(ak_rows) > len(rows):
                        self.last_source_used = "akshare"
                        return ak_rows
                    if market == "CN":
                        baostock_rows = self._fetch_with_baostock(request)
                        if len(baostock_rows) > len(rows):
                            self.last_source_used = "baostock"
                            return baostock_rows
                    if market == "HK":
                        stockanalysis_rows = self._fetch_with_stockanalysis(request)
                        if len(stockanalysis_rows) > len(rows):
                            self.last_source_used = "stockanalysis"
                            return stockanalysis_rows
                self.last_source_used = "yfinance"
                return rows
            raise

        normalized: list[dict] = []
        for record in records:
            normalized.append(
                {
                    "date": _to_iso_date(record.get("date")),
                    "symbol": request.ticker.upper(),
                    "open": record.get("open"),
                    "high": record.get("high"),
                    "low": record.get("low"),
                    "close": record.get("close"),
                    "volume": record.get("volume"),
                    "adj_close": record.get("adj_close") or record.get("adjClose"),
                    "dividend": record.get("dividend"),
                    "split_ratio": record.get("split_ratio") or record.get("splitRatio"),
                }
            )

        normalized.sort(key=lambda row: row["date"] or "")
        self.last_source_used = "openbb"
        return normalized

    def _fetch_cn_history_with_fallbacks(self, request: HistoricalPriceRequest) -> list[dict]:
        tushare_rows = TushareClient().fetch_cn_daily_history(
            request.ticker,
            start_date=request.start_date,
            end_date=request.end_date,
        )
        if tushare_rows:
            self.last_source_used = "tushare"
            return tushare_rows

        eastmoney_rows = self._fetch_with_eastmoney_cn(request)
        if eastmoney_rows:
            self.last_source_used = "eastmoney"
            return eastmoney_rows

        ak_rows = self._fetch_with_akshare(request)
        if ak_rows:
            self.last_source_used = "akshare"
            return ak_rows

        baostock_rows = self._fetch_with_baostock(request)
        if baostock_rows:
            self.last_source_used = "baostock"
            return baostock_rows

        yfinance_rows = self._fetch_with_yfinance(request)
        if yfinance_rows:
            self.last_source_used = "yfinance"
        return yfinance_rows

    def fetch_fundamental_snapshot(self, ticker: str) -> dict | None:
        self.last_source_used = "unknown"
        market = self._infer_market(ticker)
        for candidate in provider_ticker_candidates(ticker, market):
            snapshot = self._fetch_fundamental_snapshot_with_yfinance(candidate)
            if snapshot:
                snapshot["ticker"] = ticker.upper()
                self.last_source_used = "yfinance_fundamentals"
                return snapshot
        return None

    def _fetch_profile_with_yfinance(self, ticker: str) -> dict:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "Neither OpenBB nor yfinance is available for symbol profile lookup."
            ) from exc

        symbol = yf.Ticker(ticker)
        info = {}
        try:
            info = symbol.get_info() or {}
        except Exception:
            info = {}

        name = (
            info.get("longName")
            or info.get("shortName")
            or info.get("displayName")
            or info.get("name")
        )
        exchange = (
            info.get("exchange")
            or info.get("fullExchangeName")
            or info.get("quoteType")
        )
        return {
            "ticker": ticker.upper(),
            "name": name,
            "exchange": exchange,
        }

    def _fetch_with_yfinance(self, request: HistoricalPriceRequest) -> list[dict]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "Neither OpenBB nor yfinance is available for market data sync."
            ) from exc

        data = yf.download(
            request.ticker,
            start=request.start_date,
            end=request.end_date,
            auto_adjust=False,
            progress=False,
            timeout=15,
            threads=False,
        )
        if data is None or data.empty:
            return []

        normalized: list[dict] = []
        price_columns = data.columns.get_level_values(0) if hasattr(data.columns, "nlevels") and data.columns.nlevels > 1 else None
        for index, row in data.iterrows():
            def pick(name: str):
                if price_columns is not None and name in price_columns:
                    key_upper = (name, request.ticker.upper())
                    key_raw = (name, request.ticker)
                    if key_upper in row.index:
                        return row[key_upper]
                    if key_raw in row.index:
                        return row[key_raw]
                    level_zero_matches = [col for col in row.index if isinstance(col, tuple) and col[0] == name]
                    if level_zero_matches:
                        return row[level_zero_matches[0]]
                return row.get(name)

            normalized.append(
                {
                    "date": _to_iso_date(index),
                    "symbol": request.ticker.upper(),
                    "open": float(pick("Open")) if pick("Open") is not None else None,
                    "high": float(pick("High")) if pick("High") is not None else None,
                    "low": float(pick("Low")) if pick("Low") is not None else None,
                    "close": float(pick("Close")) if pick("Close") is not None else None,
                    "volume": float(pick("Volume")) if pick("Volume") is not None else None,
                    "adj_close": float(pick("Adj Close")) if pick("Adj Close") is not None else None,
                    "dividend": None,
                    "split_ratio": None,
                }
            )

        normalized.sort(key=lambda row: row["date"] or "")
        return normalized

    def _fetch_fundamental_snapshot_with_yfinance(self, ticker: str) -> dict | None:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "Neither OpenBB nor yfinance is available for fundamental data sync."
            ) from exc

        symbol = yf.Ticker(ticker)
        try:
            info = symbol.get_info() or {}
        except Exception:
            info = {}

        try:
            history = symbol.history(period="max", auto_adjust=False)
        except Exception:
            history = None

        report_date = None
        listing_date = self._normalize_date_from_info(info.get("firstTradeDateEpochUtc"))
        if history is not None and not history.empty:
            dates = list(history.index)
            if dates:
                report_date = _to_iso_date(dates[-1])
                if not listing_date:
                    listing_date = _to_iso_date(dates[0])

        if not report_date:
            report_date = self._normalize_date_from_info(info.get("regularMarketTime")) or date.today().isoformat()

        name = (
            info.get("longName")
            or info.get("shortName")
            or info.get("displayName")
            or info.get("name")
        )
        exchange = info.get("exchange") or info.get("fullExchangeName") or info.get("quoteType")

        pe_ttm = self._to_float(info.get("trailingPE"))
        dividend_yield = self._normalize_percent(info.get("dividendYield"))
        market_cap = self._to_float(info.get("marketCap"))
        roe_avg_3y = self._normalize_percent(info.get("returnOnEquity"))
        net_profit_yoy = self._normalize_percent(info.get("earningsGrowth"))
        revenue_yoy = self._normalize_percent(info.get("revenueGrowth"))
        debt_to_assets = self._normalize_debt_to_assets(info.get("debtToEquity"))

        if not any(
            value is not None
            for value in (pe_ttm, dividend_yield, market_cap, roe_avg_3y, net_profit_yoy, revenue_yoy, debt_to_assets)
        ):
            return None

        return {
            "ticker": ticker.upper(),
            "report_date": report_date,
            "name": name,
            "exchange": exchange,
            "listing_date": listing_date,
            "pe_ttm": pe_ttm,
            "dividend_yield": dividend_yield,
            "market_cap": market_cap,
            "roe_avg_3y": roe_avg_3y,
            "net_profit_yoy": net_profit_yoy,
            "revenue_yoy": revenue_yoy,
            "debt_to_assets": debt_to_assets,
            "raw_data": {
                "provider": "yfinance",
                "info": {
                    "trailingPE": info.get("trailingPE"),
                    "dividendYield": info.get("dividendYield"),
                    "marketCap": info.get("marketCap"),
                    "returnOnEquity": info.get("returnOnEquity"),
                    "earningsGrowth": info.get("earningsGrowth"),
                    "revenueGrowth": info.get("revenueGrowth"),
                    "debtToEquity": info.get("debtToEquity"),
                    "firstTradeDateEpochUtc": info.get("firstTradeDateEpochUtc"),
                },
            },
        }

    def _fetch_with_akshare(self, request: HistoricalPriceRequest) -> list[dict]:
        try:
            import akshare as ak
        except ImportError:
            return []

        market = self._infer_market(request.ticker)
        if market not in {"HK", "CN"}:
            return []

        try:
            if market == "HK":
                symbol = request.ticker.upper().replace(".HK", "").zfill(5)
                df = ak.stock_hk_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=(request.start_date or "19700101").replace("-", ""),
                    end_date=(request.end_date or "22220101").replace("-", ""),
                    adjust="",
                )
            else:
                symbol = (
                    request.ticker.upper()
                    .replace(".SS", "")
                    .replace(".SZ", "")
                    .replace(".SH", "")
                )
                df = ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=(request.start_date or "19700101").replace("-", ""),
                    end_date=(request.end_date or "20500101").replace("-", ""),
                    adjust="",
                )
        except Exception:
            return []

        if df is None or df.empty:
            return []

        normalized: list[dict] = []
        for _, row in df.iterrows():
            normalized.append(
                {
                    "date": _to_iso_date(row.get("日期")),
                    "symbol": request.ticker.upper(),
                    "open": self._to_float(row.get("开盘")),
                    "high": self._to_float(row.get("最高")),
                    "low": self._to_float(row.get("最低")),
                    "close": self._to_float(row.get("收盘")),
                    "volume": self._to_float(row.get("成交量")),
                    "adj_close": self._to_float(row.get("收盘")),
                    "dividend": None,
                    "split_ratio": None,
                }
            )

        normalized.sort(key=lambda row: row["date"] or "")
        return normalized

    def _fetch_with_eastmoney_cn(self, request: HistoricalPriceRequest) -> list[dict]:
        market = self._infer_market(request.ticker)
        if market != "CN":
            return []

        code = request.ticker.upper().replace(".SS", "").replace(".SH", "").replace(".SZ", "")
        if not code.isdigit() or len(code) != 6:
            return []

        market_code = "1" if code.startswith("6") else "0"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101",
            "fqt": "0",
            "secid": f"{market_code}.{code}",
            "beg": (request.start_date or "1970-01-01").replace("-", ""),
            "end": (request.end_date or "2050-01-01").replace("-", ""),
        }
        url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urlencode(params)}"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://quote.eastmoney.com/",
        }
        try:
            payload = urlopen(Request(url, headers=headers), timeout=15).read().decode("utf-8")
            data_json = json.loads(payload)
        except Exception:
            return []

        klines = ((data_json or {}).get("data") or {}).get("klines") or []
        if not klines:
            return []

        normalized: list[dict] = []
        for item in klines:
            parts = str(item).split(",")
            if len(parts) < 6:
                continue
            normalized.append(
                {
                    "date": parts[0],
                    "symbol": request.ticker.upper(),
                    "open": self._to_float(parts[1]),
                    "close": self._to_float(parts[2]),
                    "high": self._to_float(parts[3]),
                    "low": self._to_float(parts[4]),
                    "volume": self._to_float(parts[5]),
                    "adj_close": self._to_float(parts[2]),
                    "dividend": None,
                    "split_ratio": None,
                }
            )

        normalized.sort(key=lambda row: row["date"] or "")
        return normalized

    def _fetch_with_baostock(self, request: HistoricalPriceRequest) -> list[dict]:
        market = self._infer_market(request.ticker)
        if market != "CN":
            return []

        try:
            import baostock as bs
        except ImportError:
            return []

        code = self._to_baostock_code(request.ticker)
        if code is None:
            return []

        try:
            login_result = bs.login()
        except Exception:
            return []
        if getattr(login_result, "error_code", "0") not in {"0", 0, None}:
            return []

        try:
            query_result = bs.query_history_k_data_plus(
                code,
                "date,open,high,low,close,volume",
                start_date=request.start_date or "1990-01-01",
                end_date=request.end_date or date.today().isoformat(),
                frequency="d",
                adjustflag="3",
            )
        except Exception:
            try:
                bs.logout()
            except Exception:
                pass
            return []

        rows: list[dict] = []
        try:
            if getattr(query_result, "error_code", "0") not in {"0", 0, None}:
                return []
            while query_result.next():
                record = query_result.get_row_data()
                if not record or len(record) < 6:
                    continue
                rows.append(
                    {
                        "date": record[0],
                        "symbol": request.ticker.upper(),
                        "open": self._to_float(record[1]),
                        "high": self._to_float(record[2]),
                        "low": self._to_float(record[3]),
                        "close": self._to_float(record[4]),
                        "volume": self._to_float(record[5]),
                        "adj_close": self._to_float(record[4]),
                        "dividend": None,
                        "split_ratio": None,
                    }
                )
        finally:
            try:
                bs.logout()
            except Exception:
                pass

        rows.sort(key=lambda row: row["date"] or "")
        return rows

    def _fetch_with_stockanalysis(self, request: HistoricalPriceRequest) -> list[dict]:
        market = self._infer_market(request.ticker)
        if market != "HK":
            return []

        try:
            import pandas as pd
        except ImportError:
            return []

        core = request.ticker.upper().replace(".HK", "")
        if not core.isdigit():
            return []
        symbol = str(int(core))
        url = f"https://stockanalysis.com/quote/hkg/{symbol}/history/"
        headers = {"User-Agent": "Mozilla/5.0"}

        try:
            html = urlopen(Request(url, headers=headers), timeout=15).read().decode("utf-8", errors="ignore")
            tables = pd.read_html(StringIO(html))
        except Exception:
            return []

        target = None
        for table in tables:
            columns = {str(column).strip().lower() for column in table.columns}
            if {"date", "open", "high", "low", "close"}.issubset(columns):
                target = table
                break
        if target is None or target.empty:
            return []

        normalized: list[dict] = []
        for _, row in target.iterrows():
            iso_date = self._normalize_stockanalysis_date(row.get("Date"))
            if iso_date is None:
                continue
            if request.start_date and iso_date < request.start_date:
                continue
            if request.end_date and iso_date > request.end_date:
                continue
            normalized.append(
                {
                    "date": iso_date,
                    "symbol": request.ticker.upper(),
                    "open": self._to_float(row.get("Open")),
                    "high": self._to_float(row.get("High")),
                    "low": self._to_float(row.get("Low")),
                    "close": self._to_float(row.get("Close")),
                    "volume": self._to_float(row.get("Volume")),
                    "adj_close": self._to_float(row.get("Close")),
                    "dividend": None,
                    "split_ratio": None,
                }
            )

        normalized.sort(key=lambda row: row["date"] or "")
        return normalized

    def _infer_market(self, ticker: str) -> str | None:
        upper = ticker.upper()
        if upper.endswith(".HK"):
            return "HK"
        if upper.endswith(".SS") or upper.endswith(".SZ") or upper.endswith(".SH") or upper.endswith(".BJ"):
            return "CN"
        return None

    def _to_baostock_code(self, ticker: str) -> str | None:
        upper = ticker.strip().upper()
        if upper.endswith(".SS") or upper.endswith(".SH"):
            return f"sh.{upper.split('.')[0]}"
        if upper.endswith(".SZ"):
            return f"sz.{upper.split('.')[0]}"
        return None

    def _normalize_percent(self, value: Any) -> float | None:
        numeric = self._to_float(value)
        if numeric is None:
            return None
        if -1.0 <= numeric <= 1.0:
            numeric *= 100.0
        return round(numeric, 2)

    def _normalize_debt_to_assets(self, debt_to_equity: Any) -> float | None:
        numeric = self._to_float(debt_to_equity)
        if numeric is None:
            return None
        ratio = numeric / 100.0 if abs(numeric) > 5 else numeric
        debt_to_assets = (ratio / (1.0 + ratio)) * 100.0 if ratio > -1 else None
        return round(debt_to_assets, 2) if debt_to_assets is not None else None

    def _normalize_date_from_info(self, value: Any) -> str | None:
        if value in (None, "", "None"):
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value).date().isoformat()
            except (OSError, OverflowError, ValueError):
                return None
        return _to_iso_date(value)

    def _normalize_stockanalysis_date(self, value: Any) -> str | None:
        if value in (None, "", "None"):
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        text = str(value).strip()
        for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
        return _to_iso_date(text)

    def _to_float(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
