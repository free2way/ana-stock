from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.repository import ConceptSnapshotRepository, SymbolRepository
from app.services.ticker_format import normalize_ticker_for_market
from app.services.tushare_client import TushareClient


def sync_cn_concepts(tickers: list[str] | None = None) -> dict:
    client = TushareClient()
    if not client.is_configured():
        return {
            "status": "not_configured",
            "message": "Set PQW_TUSHARE_TOKEN to enable CN concept sync.",
            "rows_written": 0,
            "tickers": [],
        }

    normalized_tickers = [normalize_ticker_for_market(ticker, "CN") for ticker in (tickers or []) if ticker.strip()]
    rows = client.fetch_cn_concepts(normalized_tickers or None)
    if not rows:
        return {
            "status": "empty",
            "message": "No CN concept rows returned from TuShare.",
            "rows_written": 0,
            "tickers": normalized_tickers,
        }

    written = 0
    touched: list[str] = []
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        concept_repo = ConceptSnapshotRepository(db)
        for row in rows:
            ticker = normalize_ticker_for_market(row.ticker, "CN")
            symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(
                    ticker=ticker,
                    name=row.name,
                    market="CN",
                )
            )
            concept_repo.upsert_snapshot(
                symbol_id=symbol.id,
                concept_name=row.concept_name,
                concept_code=row.concept_code,
                as_of_date=row.report_date,
                source="tushare_concept",
                data=row.raw_data,
            )
            written += 1
            if ticker not in touched:
                touched.append(ticker)

    return {
        "status": "success",
        "message": f"Synced {written} CN concept row(s) for {len(touched)} stock(s).",
        "rows_written": written,
        "tickers": touched,
    }
