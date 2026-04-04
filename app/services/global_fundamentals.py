from dataclasses import asdict, dataclass

from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.openbb_client import OpenBBClient
from app.services.repository import FundamentalSnapshotRepository, SymbolRepository
from app.services.ticker_format import normalize_ticker_for_market


@dataclass(slots=True)
class GlobalFundamentalRow:
    ticker: str
    market: str
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


def sync_global_fundamentals(tickers: list[str] | None = None) -> dict:
    normalized_tickers = [_normalize_any_ticker(ticker) for ticker in (tickers or []) if ticker.strip()]
    if not normalized_tickers:
        return {
            "status": "empty",
            "message": "No US/HK tickers were provided for global fundamental sync.",
            "rows_written": 0,
            "tickers": [],
        }

    client = OpenBBClient()
    rows: list[GlobalFundamentalRow] = []
    failures: list[str] = []
    for ticker in normalized_tickers:
        snapshot = client.fetch_fundamental_snapshot(ticker)
        if not snapshot:
            failures.append(ticker)
            continue
        rows.append(
            GlobalFundamentalRow(
                ticker=ticker,
                market=_infer_market(ticker),
                report_date=snapshot["report_date"],
                name=snapshot.get("name"),
                exchange=snapshot.get("exchange"),
                listing_date=snapshot.get("listing_date"),
                pe_ttm=snapshot.get("pe_ttm"),
                dividend_yield=snapshot.get("dividend_yield"),
                market_cap=snapshot.get("market_cap"),
                roe_avg_3y=snapshot.get("roe_avg_3y"),
                net_profit_yoy=snapshot.get("net_profit_yoy"),
                revenue_yoy=snapshot.get("revenue_yoy"),
                debt_to_assets=snapshot.get("debt_to_assets"),
                raw_data=snapshot.get("raw_data"),
            )
        )

    if not rows:
        return {
            "status": "empty",
            "message": "No US/HK fundamental rows returned from the live provider.",
            "rows_written": 0,
            "tickers": normalized_tickers,
            "failed_tickers": failures,
        }

    written = 0
    touched: list[str] = []
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        fundamental_repo = FundamentalSnapshotRepository(db)
        for row in rows:
            symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(
                    ticker=row.ticker,
                    name=row.name,
                    market=row.market,
                    exchange=row.exchange,
                )
            )
            fundamental_repo.upsert_snapshot(
                symbol_id=symbol.id,
                report_date=row.report_date,
                source="yfinance_fundamentals",
                listing_date=row.listing_date,
                pe_ttm=row.pe_ttm,
                dividend_yield=row.dividend_yield,
                market_cap=row.market_cap,
                roe_avg_3y=row.roe_avg_3y,
                net_profit_yoy=row.net_profit_yoy,
                revenue_yoy=row.revenue_yoy,
                debt_to_assets=row.debt_to_assets,
                data=row.raw_data or asdict(row),
            )
            written += 1
            if row.ticker not in touched:
                touched.append(row.ticker)

    status = "success" if not failures else "partial"
    failure_text = f" {len(failures)} failed." if failures else ""
    return {
        "status": status,
        "message": f"Synced {written} US/HK fundamental row(s) for {len(touched)} stock(s).{failure_text}",
        "rows_written": written,
        "tickers": touched,
        "failed_tickers": failures,
    }


def _infer_market(ticker: str) -> str:
    upper = ticker.upper()
    if upper.endswith(".HK"):
        return "HK"
    if upper.endswith(".SS") or upper.endswith(".SZ") or upper.endswith(".SH"):
        return "CN"
    return "US"


def _normalize_any_ticker(ticker: str) -> str:
    upper = ticker.strip().upper()
    if upper.endswith(".HK"):
        return normalize_ticker_for_market(upper, "HK")
    if upper.endswith(".SS") or upper.endswith(".SZ") or upper.endswith(".SH"):
        return normalize_ticker_for_market(upper, "CN")
    return upper
