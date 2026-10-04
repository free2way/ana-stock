"""Symbol domain repositories."""

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.schema import SymbolCreate
from app.models.tables import Symbol

from app.services.repositories.shared import (
    market_sort_case,
    ticker_query_candidates,
    utc_now_iso,
)


class SymbolRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_symbols(self) -> list[Symbol]:
        stmt = select(Symbol).order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        return list(self.db.scalars(stmt).all())

    def get_by_ticker(self, ticker: str) -> Symbol | None:
        candidates = ticker_query_candidates(ticker)
        stmt = select(Symbol).where(Symbol.ticker.in_(candidates)).order_by(Symbol.ticker.asc())
        return self.db.scalar(stmt)

    def create_symbol(self, payload: SymbolCreate) -> Symbol:
        now = utc_now_iso()
        symbol = Symbol(
            ticker=payload.ticker.upper(),
            name=payload.name,
            market=payload.market,
            exchange=payload.exchange,
            sector=payload.sector,
            industry=payload.industry,
            is_active=1,
            created_at=now,
            updated_at=now,
        )
        self.db.add(symbol)
        self.db.commit()
        self.db.refresh(symbol)
        return symbol

    def get_or_create_symbol(self, payload: SymbolCreate) -> Symbol:
        existing = self.get_by_ticker(payload.ticker)
        if existing is not None:
            changed = False
            if payload.name and (not existing.name or existing.name == existing.ticker):
                existing.name = payload.name
                changed = True
            if payload.market and not existing.market:
                existing.market = payload.market
                changed = True
            if payload.exchange and not existing.exchange:
                existing.exchange = payload.exchange
                changed = True
            if payload.sector and not existing.sector:
                existing.sector = payload.sector
                changed = True
            if payload.industry and not existing.industry:
                existing.industry = payload.industry
                changed = True
            if changed:
                existing.updated_at = utc_now_iso()
                self.db.commit()
                self.db.refresh(existing)
            return existing
        return self.create_symbol(payload)

    def get_overview(self, ticker: str) -> dict | None:
        symbol = self.get_by_ticker(ticker)
        if symbol is None:
            return None
        return {
            "id": symbol.id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "market": symbol.market,
            "exchange": symbol.exchange,
            "sector": symbol.sector,
            "industry": symbol.industry,
            "is_active": symbol.is_active,
            "created_at": symbol.created_at,
            "updated_at": symbol.updated_at,
        }

    def list_overviews_for_tickers(self, tickers: list[str]) -> dict[str, dict]:
        normalized = [ticker.strip().upper() for ticker in tickers if ticker and ticker.strip()]
        if not normalized:
            return {}
        stmt = select(Symbol).where(Symbol.ticker.in_(normalized)).order_by(Symbol.ticker.asc())
        rows = self.db.scalars(stmt).all()
        return {
            symbol.ticker: {
                "id": symbol.id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "exchange": symbol.exchange,
                "sector": symbol.sector,
                "industry": symbol.industry,
                "is_active": symbol.is_active,
                "created_at": symbol.created_at,
                "updated_at": symbol.updated_at,
            }
            for symbol in rows
        }

    def update_symbol_metadata(
        self,
        symbol_id: int,
        *,
        name: str | None = None,
        market: str | None = None,
        exchange: str | None = None,
        sector: str | None = None,
        industry: str | None = None,
        overwrite_name: bool = False,
        overwrite_exchange: bool = False,
        overwrite_sector: bool = False,
        overwrite_industry: bool = False,
    ) -> Symbol | None:
        symbol = self.db.scalar(select(Symbol).where(Symbol.id == symbol_id))
        if symbol is None:
            return None

        changed = False
        if name and (overwrite_name or not symbol.name or symbol.name == symbol.ticker):
            symbol.name = name
            changed = True
        if market and not symbol.market:
            symbol.market = market
            changed = True
        if exchange and (overwrite_exchange or not symbol.exchange):
            symbol.exchange = exchange
            changed = True
        if sector and (overwrite_sector or not symbol.sector):
            symbol.sector = sector
            changed = True
        if industry and (overwrite_industry or not symbol.industry):
            symbol.industry = industry
            changed = True

        if changed:
            symbol.updated_at = utc_now_iso()
            self.db.commit()
            self.db.refresh(symbol)
        return symbol

    def list_symbols_for_metadata_refresh(
        self,
        *,
        market: str,
        limit: int = 200,
        only_missing: bool = True,
    ) -> list[Symbol]:
        stmt = select(Symbol).where(Symbol.market == market.upper())
        weak_name = or_(Symbol.name.is_(None), Symbol.name == "", func.upper(Symbol.name) == func.upper(Symbol.ticker))
        missing_exchange = or_(Symbol.exchange.is_(None), Symbol.exchange == "")
        missing_sector = or_(Symbol.sector.is_(None), Symbol.sector == "")
        missing_industry = or_(Symbol.industry.is_(None), Symbol.industry == "")
        if only_missing:
            stmt = stmt.where(or_(weak_name, missing_exchange, missing_sector, missing_industry))
        stmt = stmt.order_by(
            case((missing_sector, 0), else_=1),
            case((missing_industry, 0), else_=1),
            case((weak_name, 0), else_=1),
            case((missing_exchange, 0), else_=1),
            Symbol.updated_at.asc(),
            Symbol.ticker.asc(),
        ).limit(max(1, int(limit)))
        return list(self.db.scalars(stmt).all())
