from app.core.db import SessionLocal
from app.services.repository import SymbolRepository, TechnicalSnapshotRepository
from app.services.technical_patterns import TechnicalPatternService


def rebuild_technical_snapshots(*, market: str = "CN", limit: int | None = None) -> dict:
    normalized_market = (market or "CN").upper()
    limit = None if limit in (None, 0) else max(1, int(limit))

    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        snapshot_repo = TechnicalSnapshotRepository(db)
        symbols = [
            symbol
            for symbol in symbol_repo.list_symbols()
            if normalized_market == "ALL" or (symbol.market or "").upper() == normalized_market
        ]
        if limit:
            symbols = symbols[:limit]

        service = TechnicalPatternService()
        written = 0
        skipped = 0
        touched: list[str] = []
        for symbol in symbols:
            snapshot = service.evaluate_ticker(symbol.ticker)
            if snapshot is None:
                skipped += 1
                continue
            snapshot_repo.upsert_snapshot(
                symbol_id=symbol.id,
                as_of_date=snapshot.as_of_date,
                source="technical_patterns",
                limit_up_yesterday=snapshot.limit_up_yesterday,
                volume_breakout=snapshot.volume_breakout,
                ma_cluster=snapshot.ma_cluster,
                bullish_ma_stack=snapshot.bullish_ma_stack,
                macd_underwater_cross=snapshot.macd_underwater_cross,
                matched_patterns=snapshot.matched_patterns,
            )
            written += 1
            touched.append(symbol.ticker)

    status = "success" if written else "empty"
    return {
        "status": status,
        "message": f"Rebuilt {written} technical snapshot(s) for {normalized_market}." + (f" Skipped {skipped}." if skipped else ""),
        "market": normalized_market,
        "rows_written": written,
        "skipped": skipped,
        "tickers": touched,
    }
