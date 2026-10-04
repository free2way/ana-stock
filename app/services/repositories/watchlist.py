"""Watchlist domain repositories."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import PriceSyncState, Symbol, Watchlist, WatchlistItem

from app.services.repositories.shared import (
    market_sort_case,
    utc_now_iso,
)


class WatchlistRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_or_create_default(self, name: str = "My Watchlist") -> Watchlist:
        stmt = select(Watchlist).where(Watchlist.name == name)
        watchlist = self.db.scalar(stmt)
        if watchlist is not None:
            return watchlist
        now = utc_now_iso()
        watchlist = Watchlist(name=name, created_at=now, updated_at=now)
        self.db.add(watchlist)
        self.db.commit()
        self.db.refresh(watchlist)
        return watchlist

    def add_symbol(self, watchlist_id: int, symbol_id: int) -> WatchlistItem:
        stmt = select(WatchlistItem).where(
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.symbol_id == symbol_id,
        )
        existing = self.db.scalar(stmt)
        if existing is not None:
            return existing
        item = WatchlistItem(
            watchlist_id=watchlist_id,
            symbol_id=symbol_id,
            sync_enabled=0,
            created_at=utc_now_iso(),
        )
        self.db.add(item)
        watchlist = self.db.scalar(select(Watchlist).where(Watchlist.id == watchlist_id))
        if watchlist is not None:
            watchlist.updated_at = utc_now_iso()
        self.db.commit()
        self.db.refresh(item)
        return item

    def remove_item(self, item_id: int) -> bool:
        item = self.db.scalar(select(WatchlistItem).where(WatchlistItem.id == item_id))
        if item is None:
            return False
        watchlist = self.db.scalar(select(Watchlist).where(Watchlist.id == item.watchlist_id))
        self.db.delete(item)
        if watchlist is not None:
            watchlist.updated_at = utc_now_iso()
        self.db.commit()
        return True

    def set_sync_enabled(self, item_id: int, enabled: bool) -> WatchlistItem | None:
        item = self.db.scalar(select(WatchlistItem).where(WatchlistItem.id == item_id))
        if item is None:
            return None
        item.sync_enabled = 1 if enabled else 0
        watchlist = self.db.scalar(select(Watchlist).where(Watchlist.id == item.watchlist_id))
        if watchlist is not None:
            watchlist.updated_at = utc_now_iso()
        self.db.commit()
        self.db.refresh(item)
        return item

    def list_enabled_tickers(self, watchlist_id: int) -> list[str]:
        stmt = (
            select(Symbol.ticker)
            .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .where(WatchlistItem.sync_enabled == 1)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        return list(self.db.scalars(stmt).all())

    def get_item(self, item_id: int) -> dict | None:
        stmt = (
            select(WatchlistItem, Symbol, PriceSyncState)
            .join(Symbol, Symbol.id == WatchlistItem.symbol_id)
            .join(PriceSyncState, PriceSyncState.symbol_id == Symbol.id, isouter=True)
            .where(WatchlistItem.id == item_id)
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        item, symbol, state = row
        return {
            "item_id": item.id,
            "symbol_id": symbol.id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "market": symbol.market,
            "exchange": symbol.exchange,
            "sync_enabled": item.sync_enabled,
            "last_synced_date": state.last_synced_date if state is not None else None,
            "sync_status": state.status if state is not None else None,
        }

    def list_items(self, watchlist_id: int) -> list[dict]:
        stmt = (
            select(WatchlistItem, Symbol, PriceSyncState)
            .join(Symbol, Symbol.id == WatchlistItem.symbol_id)
            .join(PriceSyncState, PriceSyncState.symbol_id == Symbol.id, isouter=True)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        rows = self.db.execute(stmt).all()
        return [
            {
                "item_id": item.id,
                "symbol_id": symbol.id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "exchange": symbol.exchange,
                "sync_enabled": item.sync_enabled,
                "last_synced_date": state.last_synced_date if state is not None else None,
                "sync_status": state.status if state is not None else None,
                "created_at": item.created_at,
            }
            for item, symbol, state in rows
        ]

    def list_symbols_for_watchlist(self, watchlist_id: int) -> list[Symbol]:
        stmt = (
            select(Symbol)
            .join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        return list(self.db.scalars(stmt).all())

    def list_ticker_map(self, watchlist_id: int) -> dict[str, dict]:
        stmt = (
            select(WatchlistItem, Symbol, PriceSyncState)
            .join(Symbol, Symbol.id == WatchlistItem.symbol_id)
            .join(PriceSyncState, PriceSyncState.symbol_id == Symbol.id, isouter=True)
            .where(WatchlistItem.watchlist_id == watchlist_id)
        )
        rows = self.db.execute(stmt).all()
        return {
            symbol.ticker: {
                "item_id": item.id,
                "symbol_id": symbol.id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "exchange": symbol.exchange,
                "sync_enabled": item.sync_enabled,
                "last_synced_date": state.last_synced_date if state is not None else None,
                "sync_status": state.status if state is not None else None,
            }
            for item, symbol, state in rows
        }
