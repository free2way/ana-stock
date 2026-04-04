import json
from datetime import datetime, timezone

from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from app.models.schema import SymbolCreate
from app.models.tables import (
    AppSetting,
    DataJob,
    FundamentalSnapshot,
    ModelRun,
    Prediction,
    PredictionExplanation,
    PriceSyncState,
    StrategyDailyMetric,
    StrategyRun,
    Symbol,
    Watchlist,
    WatchlistItem,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ticker_query_candidates(ticker: str) -> list[str]:
    normalized = ticker.strip().upper()
    candidates = [normalized]
    if normalized.endswith(".HK"):
        core = normalized[:-3]
        if core.isdigit():
            raw = core.lstrip("0") or "0"
            for width in (4, 5):
                candidate = f"{raw.zfill(width)}.HK"
                if candidate not in candidates:
                    candidates.append(candidate)
    return candidates


def market_sort_case(column):
    return case(
        (column == "CN", 0),
        (column == "HK", 1),
        (column == "US", 2),
        else_=9,
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

    def update_symbol_metadata(
        self,
        symbol_id: int,
        *,
        name: str | None = None,
        market: str | None = None,
        exchange: str | None = None,
        overwrite_name: bool = False,
        overwrite_exchange: bool = False,
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

        if changed:
            symbol.updated_at = utc_now_iso()
            self.db.commit()
            self.db.refresh(symbol)
        return symbol


class PredictionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_latest_predictions(self, limit: int = 20) -> list[dict]:
        latest_model_run_id = self.db.scalar(select(func.max(Prediction.model_run_id)))
        if latest_model_run_id is None:
            return []

        latest_date = self.db.scalar(
            select(func.max(Prediction.trade_date)).where(Prediction.model_run_id == latest_model_run_id)
        )
        if latest_date is None:
            return []

        stmt = (
            select(Prediction, Symbol)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Prediction.model_run_id == latest_model_run_id)
            .where(Prediction.trade_date == latest_date)
            .order_by(desc(Prediction.score))
            .limit(limit)
        )
        rows = self.db.execute(stmt).all()
        return [
            {
                "trade_date": prediction.trade_date,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "score": prediction.score,
                "rank_value": prediction.rank_value,
            }
            for prediction, symbol in rows
        ]

    def list_symbol_predictions(self, ticker: str, limit: int = 120, latest_run_only: bool = False) -> list[dict]:
        latest_model_run_id = None
        if latest_run_only:
            latest_model_run_id = self.db.scalar(select(func.max(Prediction.model_run_id)))

        stmt = select(Prediction, Symbol).join(Symbol, Symbol.id == Prediction.symbol_id).where(
            Symbol.ticker == ticker.upper()
        )
        if latest_model_run_id is not None:
            stmt = stmt.where(Prediction.model_run_id == latest_model_run_id)

        stmt = stmt.order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc()).limit(limit)
        rows = self.db.execute(stmt).all()
        return [
            {
                "model_run_id": prediction.model_run_id,
                "trade_date": prediction.trade_date,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "score": prediction.score,
                "rank_value": prediction.rank_value,
            }
            for prediction, symbol in rows
        ]

    def get_latest_model_output_for_ticker(self, ticker: str) -> dict | None:
        stmt = (
            select(Prediction, Symbol, ModelRun)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .join(ModelRun, ModelRun.id == Prediction.model_run_id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None

        prediction, symbol, model_run = row
        peer_count = self.db.scalar(
            select(func.count(Prediction.id))
            .where(Prediction.model_run_id == prediction.model_run_id)
            .where(Prediction.trade_date == prediction.trade_date)
        ) or 0

        rank_value = prediction.rank_value
        percentile = None
        if rank_value is not None and peer_count:
            percentile = round(max(0.0, min(100.0, (1 - ((rank_value - 1) / max(peer_count, 1))) * 100.0)), 1)

        return {
            "prediction_id": prediction.id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "trade_date": prediction.trade_date,
            "score": prediction.score,
            "rank_value": prediction.rank_value,
            "universe_size": peer_count,
            "percentile": percentile,
            "model_run": {
                "id": model_run.id,
                "name": model_run.name,
                "model_type": model_run.model_type,
                "market": model_run.market,
                "universe": model_run.universe,
                "created_at": model_run.created_at,
                "status": model_run.status,
            },
        }


class PredictionExplanationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_model_run(self, model_run_id: int, rows: list[dict]) -> int:
        prediction_stmt = select(Prediction).where(Prediction.model_run_id == model_run_id)
        predictions = list(self.db.scalars(prediction_stmt).all())
        prediction_ids = [prediction.id for prediction in predictions]
        if prediction_ids:
            explanation_stmt = select(PredictionExplanation).where(PredictionExplanation.prediction_id.in_(prediction_ids))
            for explanation in self.db.scalars(explanation_stmt).all():
                self.db.delete(explanation)
            self.db.flush()

        if not rows:
            self.db.commit()
            return 0

        prediction_map = {(prediction.symbol_id, prediction.trade_date): prediction.id for prediction in predictions}
        now = utc_now_iso()
        inserted = 0
        for row in rows:
            prediction_id = prediction_map.get((row["symbol_id"], row["trade_date"]))
            if prediction_id is None:
                continue
            explanation = PredictionExplanation(
                prediction_id=prediction_id,
                feature_name=row["feature_name"],
                feature_value=row.get("feature_value"),
                contribution=row.get("contribution"),
                direction=row.get("direction"),
                display_order=row.get("display_order"),
                created_at=now,
            )
            self.db.add(explanation)
            inserted += 1

        self.db.commit()
        return inserted

    def get_for_prediction(self, prediction_id: int) -> list[dict]:
        stmt = (
            select(PredictionExplanation)
            .where(PredictionExplanation.prediction_id == prediction_id)
            .order_by(PredictionExplanation.display_order.asc(), desc(func.abs(PredictionExplanation.contribution)))
        )
        rows = self.db.scalars(stmt).all()
        return [
            {
                "feature_name": row.feature_name,
                "feature_value": row.feature_value,
                "contribution": row.contribution,
                "direction": row.direction,
                "display_order": row.display_order,
            }
            for row in rows
        ]

    def get_latest_for_ticker(self, ticker: str) -> list[dict]:
        stmt = (
            select(Prediction.id)
            .join(Symbol, Symbol.id == Prediction.symbol_id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(Prediction.trade_date.desc(), Prediction.model_run_id.desc())
            .limit(1)
        )
        prediction_id = self.db.scalar(stmt)
        if prediction_id is None:
            return []
        return self.get_for_prediction(prediction_id)


class BacktestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_backtests(self) -> list[dict]:
        stmt = select(StrategyRun).order_by(StrategyRun.created_at.desc())
        rows = self.db.scalars(stmt).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "strategy_type": row.strategy_type,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "status": row.status,
                "summary_json": row.summary_json,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
            for row in rows
        ]

    def get_latest_backtest(self) -> StrategyRun | None:
        stmt = select(StrategyRun).order_by(StrategyRun.id.desc()).limit(1)
        return self.db.scalar(stmt)

    def get_latest_backtest_summary(self) -> dict | None:
        backtest = self.get_latest_backtest()
        if backtest is None:
            return None
        return {
            "id": backtest.id,
            "name": backtest.name,
            "strategy_type": backtest.strategy_type,
            "start_date": backtest.start_date,
            "end_date": backtest.end_date,
            "status": backtest.status,
            "summary_json": backtest.summary_json,
            "created_at": backtest.created_at,
            "finished_at": backtest.finished_at,
        }

    def get_daily_metrics(self, strategy_run_id: int) -> list[dict]:
        stmt = (
            select(StrategyDailyMetric)
            .where(StrategyDailyMetric.strategy_run_id == strategy_run_id)
            .order_by(StrategyDailyMetric.trade_date.asc())
        )
        rows = self.db.scalars(stmt).all()
        return [
            {
                "trade_date": row.trade_date,
                "nav": row.nav,
                "daily_return": row.daily_return,
                "benchmark_return": row.benchmark_return,
                "drawdown": row.drawdown,
                "turnover": row.turnover,
            }
            for row in rows
        ]

    def get_latest_backtest_curve(self) -> list[dict]:
        latest = self.get_latest_backtest()
        if latest is None:
            return []
        return self.get_daily_metrics(latest.id)


class PriceSyncStateRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_states(self) -> list[PriceSyncState]:
        stmt = (
            select(PriceSyncState)
            .join(Symbol, Symbol.id == PriceSyncState.symbol_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        return list(self.db.scalars(stmt).all())

    def list_states_with_symbols(self) -> list[dict]:
        stmt = (
            select(PriceSyncState, Symbol)
            .join(Symbol, Symbol.id == PriceSyncState.symbol_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        rows = self.db.execute(stmt).all()
        return [
            {
                "symbol_id": state.symbol_id,
                "ticker": symbol.ticker,
                "name": symbol.name,
                "provider": state.provider,
                "last_synced_date": state.last_synced_date,
                "status": state.status,
                "message": state.message,
                "updated_at": state.updated_at,
            }
            for state, symbol in rows
        ]

    def get_state_for_ticker(self, ticker: str) -> dict | None:
        stmt = (
            select(PriceSyncState, Symbol)
            .join(Symbol, Symbol.id == PriceSyncState.symbol_id)
            .where(Symbol.ticker == ticker.upper())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        state, symbol = row
        return {
            "symbol_id": state.symbol_id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "provider": state.provider,
            "last_synced_date": state.last_synced_date,
            "status": state.status,
            "message": state.message,
            "updated_at": state.updated_at,
        }

    def upsert_state(
        self,
        *,
        symbol_id: int,
        provider: str,
        last_synced_date: str | None,
        status: str,
        message: str | None = None,
    ) -> PriceSyncState:
        stmt = select(PriceSyncState).where(PriceSyncState.symbol_id == symbol_id)
        existing = self.db.scalar(stmt)
        now = utc_now_iso()

        if existing is None:
            existing = PriceSyncState(
                symbol_id=symbol_id,
                provider=provider,
                last_synced_date=last_synced_date,
                status=status,
                message=message,
                updated_at=now,
            )
            self.db.add(existing)
        else:
            existing.provider = provider
            existing.last_synced_date = last_synced_date
            existing.status = status
            existing.message = message
            existing.updated_at = now

        self.db.commit()
        self.db.refresh(existing)
        return existing


class DataJobRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_job(self, *, job_type: str, status: str, params: dict | None = None, message: str | None = None) -> DataJob:
        job = DataJob(
            job_type=job_type,
            status=status,
            started_at=utc_now_iso(),
            finished_at=None,
            message=message,
            params_json=json.dumps(params) if params is not None else None,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def complete_job(self, job_id: int, *, status: str, message: str | None = None) -> DataJob | None:
        stmt = select(DataJob).where(DataJob.id == job_id)
        job = self.db.scalar(stmt)
        if job is None:
            return None
        job.status = status
        job.finished_at = utc_now_iso()
        job.message = message
        self.db.commit()
        self.db.refresh(job)
        return job

    def list_recent_jobs(self, limit: int = 20) -> list[dict]:
        stmt = select(DataJob).order_by(DataJob.id.desc()).limit(limit)
        rows = self.db.scalars(stmt).all()
        return [
            {
                "id": row.id,
                "job_type": row.job_type,
                "status": row.status,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "message": row.message,
                "params_json": row.params_json,
                "params": json.loads(row.params_json) if row.params_json else None,
            }
            for row in rows
        ]

    def has_running_job(self, job_type: str) -> bool:
        stmt = (
            select(DataJob.id)
            .where(DataJob.job_type == job_type)
            .where(DataJob.status == "running")
            .limit(1)
        )
        return self.db.scalar(stmt) is not None


class AppSettingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, key: str) -> str | None:
        setting = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
        return setting.value if setting is not None else None

    def set(self, key: str, value: str) -> AppSetting:
        setting = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
        now = utc_now_iso()
        if setting is None:
            setting = AppSetting(key=key, value=value, updated_at=now)
            self.db.add(setting)
        else:
            setting.value = value
            setting.updated_at = now
        self.db.commit()
        self.db.refresh(setting)
        return setting


class FundamentalSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_snapshot(
        self,
        *,
        symbol_id: int,
        report_date: str,
        source: str,
        listing_date: str | None = None,
        pe_ttm: float | None = None,
        dividend_yield: float | None = None,
        market_cap: float | None = None,
        roe_avg_3y: float | None = None,
        net_profit_yoy: float | None = None,
        revenue_yoy: float | None = None,
        debt_to_assets: float | None = None,
        data: dict | None = None,
    ) -> FundamentalSnapshot:
        stmt = select(FundamentalSnapshot).where(
            FundamentalSnapshot.symbol_id == symbol_id,
            FundamentalSnapshot.report_date == report_date,
            FundamentalSnapshot.source == source,
        )
        existing = self.db.scalar(stmt)
        now = utc_now_iso()
        payload = {
            "listing_date": listing_date,
            "pe_ttm": pe_ttm,
            "dividend_yield": dividend_yield,
            "market_cap": market_cap,
            "roe_avg_3y": roe_avg_3y,
            "net_profit_yoy": net_profit_yoy,
            "revenue_yoy": revenue_yoy,
            "debt_to_assets": debt_to_assets,
            "data_json": json.dumps(data) if data is not None else None,
            "updated_at": now,
        }
        if existing is None:
            existing = FundamentalSnapshot(
                symbol_id=symbol_id,
                report_date=report_date,
                source=source,
                created_at=now,
                **payload,
            )
            self.db.add(existing)
        else:
            for key, value in payload.items():
                setattr(existing, key, value)
        self.db.commit()
        self.db.refresh(existing)
        return existing

    def get_latest_for_ticker(self, ticker: str) -> dict | None:
        stmt = (
            select(FundamentalSnapshot, Symbol)
            .join(Symbol, Symbol.id == FundamentalSnapshot.symbol_id)
            .where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
            .order_by(FundamentalSnapshot.report_date.desc(), FundamentalSnapshot.id.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        snapshot, symbol = row
        return self._to_dict(snapshot, symbol)

    def list_latest_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        symbol_stmt = select(Symbol.id, Symbol.ticker, Symbol.name, Symbol.market)
        if market and market != "ALL":
            symbol_stmt = symbol_stmt.where(Symbol.market == market)
        if tickers:
            symbol_stmt = symbol_stmt.where(Symbol.ticker.in_([ticker.upper() for ticker in tickers]))
        symbol_rows = self.db.execute(symbol_stmt).all()
        if not symbol_rows:
            return []

        symbol_map = {
            row.id: {
                "ticker": row.ticker,
                "name": row.name,
                "market": row.market,
            }
            for row in symbol_rows
        }

        subquery = (
            select(
                FundamentalSnapshot.symbol_id,
                func.max(FundamentalSnapshot.report_date).label("max_report_date"),
            )
            .where(FundamentalSnapshot.symbol_id.in_(list(symbol_map)))
            .group_by(FundamentalSnapshot.symbol_id)
            .subquery()
        )
        stmt = (
            select(FundamentalSnapshot)
            .join(
                subquery,
                (FundamentalSnapshot.symbol_id == subquery.c.symbol_id)
                & (FundamentalSnapshot.report_date == subquery.c.max_report_date),
            )
            .order_by(FundamentalSnapshot.symbol_id.asc(), FundamentalSnapshot.id.desc())
        )
        rows = self.db.scalars(stmt).all()
        deduped: dict[int, dict] = {}
        for snapshot in rows:
            if snapshot.symbol_id in deduped:
                continue
            symbol = symbol_map.get(snapshot.symbol_id)
            if symbol is None:
                continue
            deduped[snapshot.symbol_id] = self._to_dict(snapshot, symbol)
        return list(deduped.values())

    def _to_dict(self, snapshot: FundamentalSnapshot, symbol: Symbol | dict) -> dict:
        ticker = symbol.ticker if hasattr(symbol, "ticker") else symbol["ticker"]
        name = symbol.name if hasattr(symbol, "name") else symbol.get("name")
        market = symbol.market if hasattr(symbol, "market") else symbol.get("market")
        return {
            "symbol_id": snapshot.symbol_id,
            "ticker": ticker,
            "name": name,
            "market": market,
            "report_date": snapshot.report_date,
            "source": snapshot.source,
            "listing_date": snapshot.listing_date,
            "pe_ttm": snapshot.pe_ttm,
            "dividend_yield": snapshot.dividend_yield,
            "market_cap": snapshot.market_cap,
            "roe_avg_3y": snapshot.roe_avg_3y,
            "net_profit_yoy": snapshot.net_profit_yoy,
            "revenue_yoy": snapshot.revenue_yoy,
            "debt_to_assets": snapshot.debt_to_assets,
            "data_json": snapshot.data_json,
        }


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


class ModelRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_run(
        self,
        *,
        name: str,
        model_type: str,
        market: str | None,
        universe: str | None,
        train_start: str | None,
        train_end: str | None,
        test_start: str | None,
        test_end: str | None,
        config: dict | None,
        artifact_path: str | None,
        status: str,
    ) -> ModelRun:
        run = ModelRun(
            name=name,
            model_type=model_type,
            market=market,
            universe=universe,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
            config_json=json.dumps(config) if config is not None else None,
            artifact_path=artifact_path,
            status=status,
            created_at=utc_now_iso(),
            finished_at=None,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def complete_run(self, run_id: int, status: str, artifact_path: str | None = None) -> ModelRun | None:
        stmt = select(ModelRun).where(ModelRun.id == run_id)
        run = self.db.scalar(stmt)
        if run is None:
            return None
        run.status = status
        run.finished_at = utc_now_iso()
        if artifact_path is not None:
            run.artifact_path = artifact_path
        self.db.commit()
        self.db.refresh(run)
        return run

    def get_latest_run(self) -> ModelRun | None:
        stmt = select(ModelRun).order_by(ModelRun.id.desc()).limit(1)
        return self.db.scalar(stmt)

    def get_run_by_id(self, run_id: int) -> ModelRun | None:
        stmt = select(ModelRun).where(ModelRun.id == run_id)
        return self.db.scalar(stmt)

    def get_latest_run_summary(self) -> dict | None:
        run = self.get_latest_run()
        if run is None:
            return None
        return {
            "id": run.id,
            "name": run.name,
            "model_type": run.model_type,
            "market": run.market,
            "universe": run.universe,
            "train_start": run.train_start,
            "train_end": run.train_end,
            "test_start": run.test_start,
            "test_end": run.test_end,
            "status": run.status,
            "artifact_path": run.artifact_path,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
        }

    def list_recent_runs(self, limit: int = 10) -> list[dict]:
        stmt = select(ModelRun).order_by(ModelRun.id.desc()).limit(limit)
        rows = self.db.scalars(stmt).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "model_type": row.model_type,
                "market": row.market,
                "universe": row.universe,
                "config_json": row.config_json,
                "status": row.status,
                "artifact_path": row.artifact_path,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
            for row in rows
        ]


class PredictionWriteRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def replace_for_model_run(self, model_run_id: int, rows: list[dict]) -> int:
        existing_stmt = select(Prediction).where(Prediction.model_run_id == model_run_id)
        for prediction in self.db.scalars(existing_stmt).all():
            self.db.delete(prediction)
        self.db.flush()

        now = utc_now_iso()
        for row in rows:
            prediction = Prediction(
                model_run_id=model_run_id,
                symbol_id=row["symbol_id"],
                trade_date=row["trade_date"],
                score=row.get("score"),
                rank_value=row.get("rank_value"),
                created_at=now,
            )
            self.db.add(prediction)

        self.db.commit()
        return len(rows)

    def list_for_model_run(self, model_run_id: int) -> list[Prediction]:
        stmt = (
            select(Prediction)
            .where(Prediction.model_run_id == model_run_id)
            .order_by(Prediction.trade_date.asc(), Prediction.rank_value.asc())
        )
        return list(self.db.scalars(stmt).all())


class StrategyRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_run(
        self,
        *,
        model_run_id: int | None,
        name: str,
        strategy_type: str,
        start_date: str | None,
        end_date: str | None,
        config: dict | None,
        status: str,
    ) -> StrategyRun:
        run = StrategyRun(
            model_run_id=model_run_id,
            name=name,
            strategy_type=strategy_type,
            start_date=start_date,
            end_date=end_date,
            config_json=json.dumps(config) if config is not None else None,
            summary_json=None,
            status=status,
            created_at=utc_now_iso(),
            finished_at=None,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def replace_daily_metrics(self, strategy_run_id: int, rows: list[dict]) -> int:
        existing_stmt = select(StrategyDailyMetric).where(StrategyDailyMetric.strategy_run_id == strategy_run_id)
        for metric in self.db.scalars(existing_stmt).all():
            self.db.delete(metric)
        self.db.flush()

        now = utc_now_iso()
        for row in rows:
            metric = StrategyDailyMetric(
                strategy_run_id=strategy_run_id,
                trade_date=row["trade_date"],
                nav=row.get("nav"),
                daily_return=row.get("daily_return"),
                benchmark_return=row.get("benchmark_return"),
                drawdown=row.get("drawdown"),
                turnover=row.get("turnover"),
                created_at=now,
            )
            self.db.add(metric)

        self.db.commit()
        return len(rows)

    def complete_run(self, strategy_run_id: int, status: str, summary: dict | None) -> StrategyRun | None:
        stmt = select(StrategyRun).where(StrategyRun.id == strategy_run_id)
        run = self.db.scalar(stmt)
        if run is None:
            return None
        run.status = status
        run.summary_json = json.dumps(summary) if summary is not None else None
        run.finished_at = utc_now_iso()
        self.db.commit()
        self.db.refresh(run)
        return run
