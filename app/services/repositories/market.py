"""Market data domain repositories (refresh batches, price sync, concept/technical snapshots)."""

import json
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.tables import (
    ConceptSnapshot,
    MarketRefreshBatch,
    PriceSyncState,
    Symbol,
    TechnicalSnapshot,
)
from app.services.market_freshness import (
    classify_market_symbol_anomalies,
    summarize_market_freshness,
)
from app.services.market_lake import (
    count_lake_symbols_for_trade_date,
    get_latest_lake_trade_date,
    list_lake_symbols_for_trade_date,
)
from app.services.time_utils import app_now_iso

from app.services.repositories.shared import (
    _is_database_locked_error,
    _legacy_snapshot_writes_enabled,
    _loads_json_object,
    _physical_date,
    _physical_datetime,
    _physical_snapshot_tables_for_market,
    _symbol_market,
    _sleep_for_lock_retry,
    market_sort_case,
    utc_now_iso,
)


class MarketRefreshBatchRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_batch(
        self,
        *,
        source_job_id: int | None,
        market: str,
        provider: str,
        requested_as_of_date: str,
        universe_count: int,
        started_at: str | None = None,
    ) -> MarketRefreshBatch:
        row = MarketRefreshBatch(
            source_job_id=source_job_id,
            market=str(market or "").strip().upper(),
            provider=str(provider or "").strip() or "unknown",
            requested_as_of_date=str(requested_as_of_date or "")[:10],
            universe_count=max(0, int(universe_count)),
            status="running",
            started_at=started_at or app_now_iso(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def complete_batch(self, batch_id: int, *, result: dict, finished_at: str | None = None) -> MarketRefreshBatch | None:
        row = self.db.get(MarketRefreshBatch, int(batch_id))
        if row is None:
            return None
        row.actual_as_of_date = str(result.get("actual_as_of_date") or result.get("trade_date") or "")[:10] or None
        row.universe_count = int(
            result.get("total_symbols")
            or result.get("universe_count")
            or result.get("rows_returned")
            or result.get("rows_written")
            or row.universe_count
            or 0
        )
        row.success_count = int(result.get("success_count") or 0)
        row.no_trade_count = int(result.get("no_trade_count") or 0)
        row.inactive_count = int(result.get("inactive_count") or 0)
        row.partial_count = int(result.get("stale_count") or result.get("partial_count") or 0)
        row.missing_count = int(result.get("missing_count") or 0)
        row.failed_count = int(result.get("failure_count") or result.get("failed_count") or 0)
        row.status = str(result.get("status") or "partial")
        row.summary_json = json.dumps(result, ensure_ascii=False)
        row.finished_at = finished_at or app_now_iso()
        self.db.commit()
        self.db.refresh(row)
        return row

    @staticmethod
    def _serialize(row: MarketRefreshBatch) -> dict:
        return {
            "id": row.id,
            "source_job_id": row.source_job_id,
            "market": row.market,
            "provider": row.provider,
            "requested_as_of_date": row.requested_as_of_date,
            "actual_as_of_date": row.actual_as_of_date,
            "universe_count": row.universe_count,
            "success_count": row.success_count,
            "no_trade_count": row.no_trade_count,
            "inactive_count": row.inactive_count,
            "partial_count": row.partial_count,
            "missing_count": row.missing_count,
            "failed_count": row.failed_count,
            "status": row.status,
            "summary": _loads_json_object(row.summary_json),
            "started_at": row.started_at,
            "finished_at": row.finished_at,
        }

    def record_result(
        self,
        *,
        source_job_id: int,
        market: str,
        provider: str,
        requested_as_of_date: str,
        result: dict,
    ) -> dict:
        """Create or update the one audit batch owned by a Job and market."""

        market_code = str(market or "").strip().upper()
        row = self.db.scalar(
            select(MarketRefreshBatch)
            .where(MarketRefreshBatch.source_job_id == int(source_job_id))
            .where(MarketRefreshBatch.market == market_code)
            .order_by(MarketRefreshBatch.id.desc())
            .limit(1)
        )
        if row is None:
            row = self.create_batch(
                source_job_id=source_job_id,
                market=market_code,
                provider=provider,
                requested_as_of_date=requested_as_of_date,
                universe_count=int(
                    result.get("total_symbols")
                    or result.get("universe_count")
                    or result.get("rows_returned")
                    or result.get("rows_written")
                    or 0
                ),
            )
        else:
            row.provider = str(provider or "").strip() or row.provider
            row.requested_as_of_date = str(requested_as_of_date or row.requested_as_of_date)[:10]
            self.db.commit()
        completed = self.complete_batch(row.id, result=result)
        return self._serialize(completed or row)

class PriceSyncStateRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

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
                "market": symbol.market,
                "provider": state.provider,
                "last_synced_date": state.last_synced_date,
                "status": state.status,
                "message": state.message,
                "updated_at": state.updated_at,
            }
            for state, symbol in rows
        ]

    def get_market_freshness_overview(
        self,
        markets: tuple[str, ...] = ("CN", "US"),
        *,
        tickers_by_market: dict[str, set[str]] | None = None,
    ) -> dict[str, dict]:
        normalized_markets = tuple(str(market or "").strip().upper() for market in markets)
        rows = self.db.execute(
            select(Symbol.ticker, Symbol.market, Symbol.is_active, PriceSyncState.last_synced_date, PriceSyncState.status)
            .outerjoin(PriceSyncState, PriceSyncState.symbol_id == Symbol.id)
            .where(Symbol.market.in_(normalized_markets))
        ).all()
        states = [
            {
                "ticker": ticker,
                "market": market,
                "last_synced_date": last_synced_date,
                "status": status or ("inactive" if not is_active else None),
                "is_active": bool(is_active),
            }
            for ticker, market, is_active, last_synced_date, status in rows
            if not tickers_by_market
            or str(market or "").strip().upper() not in tickers_by_market
            or str(ticker or "").strip().upper() in tickers_by_market.get(str(market or "").strip().upper(), set())
        ]
        overview: dict[str, dict] = {}
        for market in normalized_markets:
            summary = summarize_market_freshness(states, market=market)
            try:
                lake_latest = get_latest_lake_trade_date(market=market)
            except Exception:
                lake_latest = None
            lake_expected = summary.get("expected_as_of_date")
            lake_status = (
                "fresh" if lake_latest and lake_expected and lake_latest >= lake_expected
                else "stale" if lake_latest
                else "missing"
            )
            lake_symbol_count = 0
            lake_symbols: set[str] = set()
            if lake_latest:
                try:
                    lake_symbols = list_lake_symbols_for_trade_date(
                        market=market,
                        trade_date=lake_latest,
                    )
                    lake_symbol_count = len(lake_symbols)
                except Exception:
                    try:
                        lake_symbol_count = count_lake_symbols_for_trade_date(
                            market=market,
                            trade_date=lake_latest,
                        )
                    except Exception:
                        lake_symbol_count = 0
            classification = classify_market_symbol_anomalies(
                states,
                market=market,
                expected_as_of_date=str(summary.get("expected_as_of_date") or ""),
                lake_symbols=lake_symbols,
            )
            # Keep per-symbol state diagnostics intact, but expose the lake's
            # authoritative as-of date separately. A bulk lake refresh may be
            # current even when an old per-symbol sync row has not been touched.
            overview[market] = {
                **summary,
                "symbol_state_status": summary.get("status"),
                "lake_status": lake_status,
                "lake_latest_as_of_date": lake_latest,
                "lake_symbol_count": lake_symbol_count,
                "authoritative_as_of_date": lake_latest or summary.get("latest_as_of_date"),
                "anomaly_classification": classification,
                "blocking_anomaly_count": classification["blocking_anomaly_count"],
                "accounted_symbol_count": classification["accounted_count"],
            }
        return overview

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
        attempts = 4
        for attempt in range(1, attempts + 1):
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
            try:
                self.db.commit()
                self.db.refresh(existing)
                return existing
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Price sync state upsert exhausted retries.")

class ConceptSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_snapshot(
        self,
        *,
        symbol_id: int,
        concept_name: str,
        as_of_date: str,
        source: str,
        concept_code: str | None = None,
        strength: float | None = None,
        data: dict | None = None,
    ) -> ConceptSnapshot:
        stmt = select(ConceptSnapshot).where(
            ConceptSnapshot.symbol_id == symbol_id,
            ConceptSnapshot.concept_name == concept_name,
            ConceptSnapshot.as_of_date == as_of_date,
            ConceptSnapshot.source == source,
        )
        existing = self.db.scalar(stmt)
        now = utc_now_iso()
        payload = {
            "concept_code": concept_code,
            "strength": strength,
            "data_json": json.dumps(data, ensure_ascii=False) if data is not None else None,
            "updated_at": now,
        }
        if existing is None:
            existing = ConceptSnapshot(
                symbol_id=symbol_id,
                concept_name=concept_name,
                as_of_date=as_of_date,
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

    def list_latest_for_tickers(self, tickers: list[str]) -> list[dict]:
        normalized = [ticker.strip().upper() for ticker in tickers if ticker.strip()]
        if not normalized:
            return []
        stmt = (
            select(ConceptSnapshot, Symbol)
            .join(Symbol, Symbol.id == ConceptSnapshot.symbol_id)
            .where(Symbol.ticker.in_(normalized))
            .order_by(Symbol.ticker.asc(), ConceptSnapshot.as_of_date.desc(), ConceptSnapshot.concept_name.asc())
        )
        rows = self.db.execute(stmt).all()
        seen: set[tuple[str, str]] = set()
        payload: list[dict] = []
        for snapshot, symbol in rows:
            key = (symbol.ticker, snapshot.concept_name)
            if key in seen:
                continue
            seen.add(key)
            payload.append(
                {
                    "ticker": symbol.ticker,
                    "name": symbol.name,
                    "market": symbol.market,
                    "concept_name": snapshot.concept_name,
                    "concept_code": snapshot.concept_code,
                    "as_of_date": snapshot.as_of_date,
                    "source": snapshot.source,
                    "strength": snapshot.strength,
                }
            )
        return payload

    def list_history_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        stmt = (
            select(ConceptSnapshot, Symbol)
            .join(Symbol, Symbol.id == ConceptSnapshot.symbol_id)
            .order_by(
                market_sort_case(Symbol.market),
                Symbol.ticker.asc(),
                ConceptSnapshot.as_of_date.asc(),
                ConceptSnapshot.concept_name.asc(),
                ConceptSnapshot.id.asc(),
            )
        )
        if market and market != "ALL":
            stmt = stmt.where(Symbol.market == market)
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_([ticker.upper() for ticker in tickers]))
        rows = self.db.execute(stmt).all()
        payload: list[dict] = []
        for snapshot, symbol in rows:
            payload.append(
                {
                    "ticker": symbol.ticker,
                    "name": symbol.name,
                    "market": symbol.market,
                    "concept_name": snapshot.concept_name,
                    "concept_code": snapshot.concept_code,
                    "as_of_date": snapshot.as_of_date,
                    "source": snapshot.source,
                    "strength": snapshot.strength,
                }
            )
        return payload

    def get_latest_summary(self) -> dict:
        latest_date = self.db.scalar(select(func.max(ConceptSnapshot.as_of_date)))
        concept_count = self.db.scalar(select(func.count(func.distinct(ConceptSnapshot.concept_name)))) or 0
        symbol_count = self.db.scalar(select(func.count(func.distinct(ConceptSnapshot.symbol_id)))) or 0
        freshness = "missing"
        if latest_date:
            try:
                days_old = (date.today() - date.fromisoformat(str(latest_date))).days
                if days_old <= 1:
                    freshness = "fresh"
                elif days_old <= 5:
                    freshness = "stale"
                else:
                    freshness = "old"
            except ValueError:
                freshness = "unknown"
        return {
            "latest_as_of_date": latest_date,
            "concept_count": int(concept_count),
            "symbol_count": int(symbol_count),
            "freshness": freshness,
        }

class TechnicalSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def upsert_snapshot(
        self,
        *,
        symbol_id: int,
        as_of_date: str | None,
        source: str,
        limit_up_yesterday: bool,
        volume_breakout: bool,
        ma_cluster: bool,
        bullish_ma_stack: bool,
        macd_underwater_cross: bool,
        matched_patterns: list[str] | None = None,
    ) -> TechnicalSnapshot:
        symbol_market = _symbol_market(self.db, symbol_id)
        physical_tables = _physical_snapshot_tables_for_market(symbol_market)
        physical_table = physical_tables[2] if physical_tables is not None else None
        write_legacy = _legacy_snapshot_writes_enabled(
            self.db,
            physical_tables,
            market=symbol_market,
        )
        existing = (
            self.db.scalar(
                select(TechnicalSnapshot).where(
                    TechnicalSnapshot.symbol_id == symbol_id
                )
            )
            if write_legacy
            else None
        )
        now = utc_now_iso()
        payload = {
            "as_of_date": as_of_date,
            "source": source,
            "limit_up_yesterday": 1 if limit_up_yesterday else 0,
            "volume_breakout": 1 if volume_breakout else 0,
            "ma_cluster": 1 if ma_cluster else 0,
            "bullish_ma_stack": 1 if bullish_ma_stack else 0,
            "macd_underwater_cross": 1 if macd_underwater_cross else 0,
            "matched_patterns_json": json.dumps(matched_patterns or [], ensure_ascii=False),
            "updated_at": now,
        }
        if write_legacy and existing is None:
            existing = TechnicalSnapshot(
                symbol_id=symbol_id,
                created_at=now,
                **payload,
            )
            self.db.add(existing)
        elif write_legacy:
            for key, value in payload.items():
                setattr(existing, key, value)
        physical_existing = None
        if physical_table is not None:
            physical_existing = self.db.scalar(
                select(physical_table).where(physical_table.symbol_id == symbol_id)
            )
            if physical_existing is None:
                physical_payload = {
                    **payload,
                    "as_of_date": _physical_date(as_of_date, nullable=True),
                    "updated_at": _physical_datetime(now),
                }
                physical_existing = physical_table(
                    symbol_id=symbol_id,
                    market=symbol_market,
                    created_at=_physical_datetime(now),
                    **physical_payload,
                )
                self.db.add(physical_existing)
            else:
                physical_payload = {
                    **payload,
                    "as_of_date": _physical_date(as_of_date, nullable=True),
                    "updated_at": _physical_datetime(now),
                }
                for key, value in physical_payload.items():
                    setattr(physical_existing, key, value)
        self.db.commit()
        result = existing if write_legacy else physical_existing
        if result is None:
            raise RuntimeError("Technical write did not produce a legacy or physical row.")
        self.db.refresh(result)
        return result

    def list_latest_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        physical_tables = _physical_snapshot_tables_for_market(market)
        snapshot_table = (
            physical_tables[2] if physical_tables is not None else TechnicalSnapshot
        )
        stmt = (
            select(snapshot_table, Symbol)
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc())
        )
        if market and market != "ALL":
            stmt = stmt.where(Symbol.market == market)
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_([ticker.upper() for ticker in tickers]))
        rows = self.db.execute(stmt).all()
        return [self._to_dict(snapshot, symbol) for snapshot, symbol in rows]

    def _to_dict(self, snapshot: TechnicalSnapshot, symbol: Symbol) -> dict:
        matched_patterns = []
        if snapshot.matched_patterns_json:
            try:
                matched_patterns = json.loads(snapshot.matched_patterns_json)
            except json.JSONDecodeError:
                matched_patterns = []
        return {
            "symbol_id": snapshot.symbol_id,
            "ticker": symbol.ticker,
            "name": symbol.name,
            "market": symbol.market,
            "exchange": symbol.exchange,
            "as_of_date": snapshot.as_of_date,
            "source": snapshot.source,
            "limit_up_yesterday": bool(snapshot.limit_up_yesterday),
            "volume_breakout": bool(snapshot.volume_breakout),
            "ma_cluster": bool(snapshot.ma_cluster),
            "bullish_ma_stack": bool(snapshot.bullish_ma_stack),
            "macd_underwater_cross": bool(snapshot.macd_underwater_cross),
            "matched_patterns": matched_patterns,
            "source_layer": snapshot.__class__.__tablename__,
        }
