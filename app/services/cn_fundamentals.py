from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.repository import FundamentalSnapshotRepository, SymbolRepository
from app.services.ticker_format import normalize_ticker_for_market
from app.services.tushare_client import TushareClient


def sync_cn_fundamentals(tickers: list[str] | None = None) -> dict:
    client = TushareClient()
    if not client.is_configured():
        return {
            "status": "not_configured",
            "message": "Set PQW_TUSHARE_TOKEN to enable CN fundamental sync.",
            "rows_written": 0,
            "tickers": [],
        }

    normalized_tickers = [normalize_ticker_for_market(ticker, "CN") for ticker in (tickers or []) if ticker.strip()]
    rows = client.fetch_cn_growth_value_candidates(normalized_tickers or None)
    if not rows:
        return {
            "status": "empty",
            "message": "No CN fundamental rows returned from TuShare.",
            "rows_written": 0,
            "tickers": normalized_tickers,
        }

    written = 0
    touched: list[str] = []
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        fundamental_repo = FundamentalSnapshotRepository(db)
        for row in rows:
            ticker = normalize_ticker_for_market(row.ticker, "CN")
            symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(
                    ticker=ticker,
                    name=row.name,
                    market="CN",
                    exchange=row.exchange,
                )
            )
            fundamental_repo.upsert_snapshot(
                symbol_id=symbol.id,
                report_date=row.report_date,
                source="tushare",
                listing_date=row.listing_date,
                pe_ttm=row.pe_ttm,
                dividend_yield=row.dividend_yield,
                market_cap=row.market_cap,
                roe_avg_3y=row.roe_avg_3y,
                net_profit_yoy=row.net_profit_yoy,
                revenue_yoy=row.revenue_yoy,
                debt_to_assets=row.debt_to_assets,
                data=row.raw_data,
            )
            written += 1
            if ticker not in touched:
                touched.append(ticker)

    return {
        "status": "success",
        "message": f"Synced {written} CN fundamental row(s) for {len(touched)} stock(s).",
        "rows_written": written,
        "tickers": touched,
    }
