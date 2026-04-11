from dataclasses import dataclass
from datetime import date, timedelta

from app.core.config import get_settings


@dataclass(slots=True)
class CNFundamentalRow:
    ticker: str
    report_date: str
    name: str | None = None
    exchange: str | None = None
    listing_date: str | None = None
    pe_ttm: float | None = None
    dividend_yield: float | None = None
    market_cap: float | None = None
    roe_avg_3y: float | None = None
    net_profit_yoy: float | None = None
    revenue_yoy: float | None = None
    debt_to_assets: float | None = None
    raw_data: dict | None = None


@dataclass(slots=True)
class CNConceptRow:
    ticker: str
    concept_name: str
    report_date: str
    concept_code: str | None = None
    name: str | None = None
    raw_data: dict | None = None


class TushareClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.token = self.settings.tushare_token

    def is_configured(self) -> bool:
        return bool(self.token)

    def fetch_cn_growth_value_candidates(self, tickers: list[str] | None = None) -> list[CNFundamentalRow]:
        if not self.token:
            return []

        try:
            import tushare as ts  # type: ignore
        except ImportError:
            return []

        pro = ts.pro_api(self.token)
        if pro is None:
            return []
        normalized = [self._to_ts_code(ticker) for ticker in (tickers or []) if ticker]
        if not normalized:
            return []

        rows: list[CNFundamentalRow] = []
        for ts_code in normalized:
            stock_df = pro.stock_basic(
                ts_code=ts_code,
                list_status="L",
                fields="ts_code,name,exchange,list_date",
            )
            if stock_df is None or stock_df.empty:
                continue

            price_start = (date.today() - timedelta(days=45)).strftime("%Y%m%d")
            price_end = date.today().strftime("%Y%m%d")
            daily_df = pro.daily_basic(
                ts_code=ts_code,
                start_date=price_start,
                end_date=price_end,
                fields="ts_code,trade_date,pe_ttm,dv_ttm,dv_ratio,total_mv",
            )
            fina_df = pro.fina_indicator(
                ts_code=ts_code,
                fields="ts_code,end_date,roe,roe_avg,netprofit_yoy,q_netprofit_yoy,q_dtprofit_yoy,q_sales_yoy,tr_yoy,debt_asset_ratio",
            )

            stock_row = stock_df.iloc[0].to_dict()
            latest_daily = self._latest_row_by_date(daily_df, "trade_date")
            latest_fina = self._latest_row_by_date(fina_df, "end_date")
            if latest_daily is None and latest_fina is None:
                continue

            roe_avg_3y = self._average_recent_annual_roe(fina_df)
            market_cap = self._to_float(latest_daily.get("total_mv")) if latest_daily else None
            if market_cap is not None:
                market_cap *= 10000.0

            profit_yoy = self._first_number(
                latest_fina,
                ("q_netprofit_yoy", "q_dtprofit_yoy", "netprofit_yoy"),
            )
            revenue_yoy = self._first_number(
                latest_fina,
                ("q_sales_yoy", "tr_yoy"),
            )
            debt_to_assets = self._first_number(latest_fina, ("debt_asset_ratio",))
            report_date = (
                self._normalize_date(latest_daily.get("trade_date")) if latest_daily else None
            ) or (
                self._normalize_date(latest_fina.get("end_date")) if latest_fina else None
            )
            if report_date is None:
                continue

            rows.append(
                CNFundamentalRow(
                    ticker=self._to_app_ticker(ts_code),
                    report_date=report_date,
                    name=stock_row.get("name"),
                    exchange=stock_row.get("exchange"),
                    listing_date=self._normalize_date(stock_row.get("list_date")),
                    pe_ttm=self._to_float(latest_daily.get("pe_ttm")) if latest_daily else None,
                    dividend_yield=self._first_number(latest_daily, ("dv_ttm", "dv_ratio")),
                    market_cap=market_cap,
                    roe_avg_3y=roe_avg_3y,
                    net_profit_yoy=profit_yoy,
                    revenue_yoy=revenue_yoy,
                    debt_to_assets=debt_to_assets,
                    raw_data={
                        "stock_basic": stock_row,
                        "daily_basic": latest_daily,
                        "fina_indicator": latest_fina,
                    },
                )
            )
        return rows

    def fetch_cn_symbol_universe(self) -> list[dict]:
        if not self.token:
            return []

        try:
            import tushare as ts  # type: ignore
        except ImportError:
            return []

        pro = ts.pro_api(self.token)
        if pro is None:
            return []
        stock_df = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,exchange,list_date",
        )
        if stock_df is None or stock_df.empty:
            return []

        rows: list[dict] = []
        for _, row in stock_df.iterrows():
            row_dict = row.to_dict()
            ts_code = str(row_dict.get("ts_code") or "").strip().upper()
            if not ts_code:
                continue
            rows.append(
                {
                    "ticker": self._to_app_ticker(ts_code),
                    "name": row_dict.get("name"),
                    "exchange": row_dict.get("exchange"),
                    "listing_date": self._normalize_date(row_dict.get("list_date")),
                }
            )
        return rows

    def fetch_cn_daily_history(
        self,
        ticker: str,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        if not self.token:
            return []

        try:
            import tushare as ts  # type: ignore
        except ImportError:
            return []

        pro = ts.pro_api(self.token)
        if pro is None:
            return []

        ts_code = self._to_ts_code(ticker)
        try:
            daily_df = pro.daily(
                ts_code=ts_code,
                start_date=(start_date or "19700101").replace("-", ""),
                end_date=(end_date or date.today().strftime("%Y%m%d")).replace("-", ""),
                fields="ts_code,trade_date,open,high,low,close,vol,amount",
            )
        except Exception:
            return []

        if daily_df is None or daily_df.empty:
            return []

        rows: list[dict] = []
        for _, row in daily_df.iterrows():
            row_dict = row.to_dict()
            volume = self._to_float(row_dict.get("vol"))
            if volume is not None:
                volume *= 100.0
            rows.append(
                {
                    "date": self._normalize_date(row_dict.get("trade_date")),
                    "symbol": ticker.strip().upper(),
                    "open": self._to_float(row_dict.get("open")),
                    "high": self._to_float(row_dict.get("high")),
                    "low": self._to_float(row_dict.get("low")),
                    "close": self._to_float(row_dict.get("close")),
                    "volume": volume,
                    "adj_close": self._to_float(row_dict.get("close")),
                    "dividend": None,
                    "split_ratio": None,
                }
            )

        rows.sort(key=lambda item: item["date"] or "")
        return rows

    def fetch_cn_concepts(self, tickers: list[str] | None = None) -> list[CNConceptRow]:
        if not self.token:
            return []

        try:
            import tushare as ts  # type: ignore
        except ImportError:
            return []

        pro = ts.pro_api(self.token)
        if pro is None:
            return []
        normalized = [self._to_ts_code(ticker) for ticker in (tickers or []) if ticker]
        if not normalized:
            return []

        rows: list[CNConceptRow] = []
        report_date = date.today().isoformat()
        for ts_code in normalized:
            detail_df = pro.concept_detail(
                ts_code=ts_code,
                fields="id,concept_name,ts_code,name,in_date,out_date",
            )
            if detail_df is None or detail_df.empty:
                continue
            for _, row in detail_df.iterrows():
                row_dict = row.to_dict()
                concept_name = str(row_dict.get("concept_name") or "").strip()
                if not concept_name:
                    continue
                rows.append(
                    CNConceptRow(
                        ticker=self._to_app_ticker(ts_code),
                        concept_name=concept_name,
                        concept_code=str(row_dict.get("id") or "").strip() or None,
                        report_date=report_date,
                        name=row_dict.get("name"),
                        raw_data=row_dict,
                    )
                )
        return rows

    def _to_ts_code(self, ticker: str) -> str:
        upper = ticker.strip().upper()
        if upper.endswith(".SS"):
            return f"{upper[:-3]}.SH"
        return upper

    def _to_app_ticker(self, ts_code: str) -> str:
        upper = ts_code.strip().upper()
        if upper.endswith(".SH"):
            return f"{upper[:-3]}.SS"
        return upper

    def _normalize_date(self, value: str | None) -> str | None:
        if not value:
            return None
        raw = str(value).strip()
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        return raw

    def _latest_row_by_date(self, dataframe, date_column: str) -> dict | None:
        if dataframe is None or dataframe.empty or date_column not in dataframe.columns:
            return None
        sorted_df = dataframe.sort_values(by=date_column, ascending=False)
        return sorted_df.iloc[0].to_dict()

    def _average_recent_annual_roe(self, dataframe) -> float | None:
        if dataframe is None or dataframe.empty or "end_date" not in dataframe.columns:
            return None
        annual = dataframe[dataframe["end_date"].astype(str).str.endswith("1231", na=False)]
        if annual.empty:
            annual = dataframe
        sorted_df = annual.sort_values(by="end_date", ascending=False)
        values: list[float] = []
        seen_dates: set[str] = set()
        for _, row in sorted_df.iterrows():
            end_date = str(row.get("end_date") or "")
            if end_date in seen_dates:
                continue
            seen_dates.add(end_date)
            value = self._to_float(row.get("roe_avg"))
            if value is None:
                value = self._to_float(row.get("roe"))
            if value is not None:
                values.append(value)
            if len(values) == 3:
                break
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    def _first_number(self, row: dict | None, fields: tuple[str, ...]) -> float | None:
        if row is None:
            return None
        for field in fields:
            value = self._to_float(row.get(field))
            if value is not None:
                return value
        return None

    def _to_float(self, value) -> float | None:
        if value in (None, "", "None"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
