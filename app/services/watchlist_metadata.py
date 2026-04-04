from app.core.db import SessionLocal
from app.services.openbb_client import OpenBBClient
from app.services.repository import SymbolRepository, WatchlistRepository
from app.services.symbol_catalog import infer_symbol_record


def refresh_watchlist_metadata() -> dict:
    updated = []
    client = OpenBBClient()
    with SessionLocal() as db:
        watchlist_repo = WatchlistRepository(db)
        symbol_repo = SymbolRepository(db)
        watchlist = watchlist_repo.get_or_create_default()
        for symbol in watchlist_repo.list_symbols_for_watchlist(watchlist.id):
            record = {}
            live_name_found = False
            live_exchange_found = False
            try:
                live_profile = client.fetch_symbol_profile(symbol.ticker)
                if live_profile:
                    record.update(live_profile)
                    live_name_found = bool(live_profile.get("name"))
                    live_exchange_found = bool(live_profile.get("exchange"))
            except Exception:
                pass
            fallback_record = infer_symbol_record(symbol.ticker, symbol.market)
            if fallback_record:
                if not record.get("name"):
                    record["name"] = fallback_record.get("name")
                if not record.get("exchange"):
                    record["exchange"] = fallback_record.get("exchange")
                if not record.get("market"):
                    record["market"] = fallback_record.get("market")
            if not record:
                continue
            before_name = symbol.name
            before_exchange = symbol.exchange
            updated_symbol = symbol_repo.update_symbol_metadata(
                symbol.id,
                name=record.get("name"),
                market=record.get("market"),
                exchange=record.get("exchange"),
                overwrite_name=live_name_found or (fallback_record is not None and (symbol.market or "") in {"HK", "CN"}),
                overwrite_exchange=live_exchange_found or (fallback_record is not None and (symbol.market or "") in {"HK", "CN"}),
            )
            if updated_symbol is None:
                continue
            if updated_symbol.name != before_name or updated_symbol.exchange != before_exchange:
                updated.append(
                    {
                        "ticker": updated_symbol.ticker,
                        "name": updated_symbol.name,
                        "exchange": updated_symbol.exchange,
                    }
                )
    return {
        "updated_count": len(updated),
        "updated_symbols": updated,
    }
