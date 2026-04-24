from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.market_lake import list_lake_symbols
from app.services.providers import resolve_fundamental_provider
from app.services.repository import FundamentalSnapshotRepository, SymbolRepository
from app.services.ticker_format import normalize_ticker_for_market


def sync_cn_fundamentals(tickers: list[str] | None = None) -> dict:
    provider = resolve_fundamental_provider("tushare", market="CN")
    if getattr(provider, "client", None) is None or not provider.client.is_configured():
        return {
            "status": "not_configured",
            "message": "Set PQW_TUSHARE_TOKEN to enable CN fundamental sync.",
            "rows_written": 0,
            "tickers": [],
        }

    normalized_tickers = [normalize_ticker_for_market(ticker, "CN") for ticker in (tickers or []) if ticker.strip()]
    if not normalized_tickers:
        normalized_tickers = sorted(list_lake_symbols(market="CN"))
    with SessionLocal() as db:
        symbol_meta_by_ticker = SymbolRepository(db).list_overviews_for_tickers(normalized_tickers)
    try:
        rows = provider.fetch_snapshots(normalized_tickers, metadata=symbol_meta_by_ticker)
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        if "daily_basic" in lowered and ("权限" in message or "access" in lowered):
            return {
                "status": "not_configured",
                "message": (
                    "Current TuShare plan does not have daily_basic permission, so CN fundamental sync "
                    "cannot build valuation snapshots yet."
                ),
                "rows_written": 0,
                "tickers": normalized_tickers,
            }
        if "频率超限" in message or "rate limit" in lowered:
            return {
                "status": "partial",
                "message": f"CN fundamental sync is being rate limited by TuShare: {message}",
                "rows_written": 0,
                "tickers": normalized_tickers,
            }
        return {
            "status": "failed",
            "message": f"CN fundamental sync failed: {message}",
            "rows_written": 0,
            "tickers": normalized_tickers,
        }
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
            ticker = normalize_ticker_for_market(row["ticker"], "CN")
            symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(
                    ticker=ticker,
                    name=row.get("name"),
                    market="CN",
                    exchange=row.get("exchange"),
                )
            )
            fundamental_repo.upsert_snapshot(
                symbol_id=symbol.id,
                report_date=row["report_date"],
                source=getattr(provider, "last_source_used", "tushare"),
                listing_date=row.get("listing_date"),
                pe_ttm=row.get("pe_ttm"),
                dividend_yield=row.get("dividend_yield"),
                market_cap=row.get("market_cap"),
                roe_avg_3y=row.get("roe_avg_3y"),
                net_profit_yoy=row.get("net_profit_yoy"),
                revenue_yoy=row.get("revenue_yoy"),
                debt_to_assets=row.get("debt_to_assets"),
                data=row.get("raw_data"),
            )
            written += 1
            if ticker not in touched:
                touched.append(ticker)

    return {
        "status": "success",
        "message": (
            f"Synced {written} CN fundamental row(s) for {len(touched)} stock(s) via "
            f"{getattr(provider, 'last_source_used', 'tushare')}."
        ),
        "rows_written": written,
        "tickers": touched,
    }
