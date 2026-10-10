"""Research snapshot domain repositories (fundamentals, point-in-time features)."""

import json
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.tables import (
    FundamentalSnapshot,
    PointInTimeFeatureConflict,
    PointInTimeFeatureSnapshot,
    Symbol,
)

from app.services.repositories.shared import (
    _legacy_snapshot_writes_enabled,
    _physical_date,
    _physical_datetime,
    _physical_snapshot_tables_for_market,
    _safe_parse_iso,
    _symbol_market,
    market_sort_case,
    ticker_query_candidates,
    utc_now_iso,
)


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
        symbol_market = _symbol_market(self.db, symbol_id)
        physical_tables = _physical_snapshot_tables_for_market(symbol_market)
        physical_table = physical_tables[0] if physical_tables is not None else None
        write_legacy = _legacy_snapshot_writes_enabled(
            self.db,
            physical_tables,
            market=symbol_market,
        )
        stmt = select(FundamentalSnapshot).where(
            FundamentalSnapshot.symbol_id == symbol_id,
            FundamentalSnapshot.report_date == report_date,
            FundamentalSnapshot.source == source,
        )
        existing = self.db.scalar(stmt) if write_legacy else None
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
        if write_legacy and existing is None:
            existing = FundamentalSnapshot(
                symbol_id=symbol_id,
                report_date=report_date,
                source=source,
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
                select(physical_table).where(
                    physical_table.symbol_id == symbol_id,
                    physical_table.report_date == _physical_date(report_date),
                    physical_table.source == source,
                )
            )
            if physical_existing is None:
                physical_payload = {
                    **payload,
                    "listing_date": _physical_date(listing_date, nullable=True),
                    "updated_at": _physical_datetime(now),
                }
                physical_existing = physical_table(
                    symbol_id=symbol_id,
                    market=symbol_market,
                    report_date=_physical_date(report_date),
                    source=source,
                    created_at=_physical_datetime(now),
                    **physical_payload,
                )
                self.db.add(physical_existing)
            else:
                physical_payload = {
                    **payload,
                    "listing_date": _physical_date(listing_date, nullable=True),
                    "updated_at": _physical_datetime(now),
                }
                for key, value in physical_payload.items():
                    setattr(physical_existing, key, value)
        self.db.commit()
        result = existing if write_legacy else physical_existing
        if result is None:
            raise RuntimeError("Snapshot write did not produce a legacy or physical row.")
        self.db.refresh(result)
        return result

    def get_latest_for_ticker(self, ticker: str) -> dict | None:
        symbol = self.db.scalar(
            select(Symbol).where(Symbol.ticker.in_(ticker_query_candidates(ticker)))
        )
        if symbol is None:
            return None
        physical_tables = _physical_snapshot_tables_for_market(symbol.market)
        snapshot_table = (
            physical_tables[0] if physical_tables is not None else FundamentalSnapshot
        )
        stmt = (
            select(snapshot_table, Symbol)
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .where(Symbol.id == symbol.id)
            .order_by(snapshot_table.report_date.desc(), snapshot_table.id.desc())
            .limit(1)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        snapshot, symbol = row
        return self._to_dict(snapshot, symbol)

    def list_latest_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        physical_tables = _physical_snapshot_tables_for_market(market)
        snapshot_table = (
            physical_tables[0] if physical_tables is not None else FundamentalSnapshot
        )
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
                snapshot_table.symbol_id,
                func.max(snapshot_table.report_date).label("max_report_date"),
            )
            .where(snapshot_table.symbol_id.in_(list(symbol_map)))
            .group_by(snapshot_table.symbol_id)
            .subquery()
        )
        stmt = (
            select(snapshot_table)
            .join(
                subquery,
                (snapshot_table.symbol_id == subquery.c.symbol_id)
                & (snapshot_table.report_date == subquery.c.max_report_date),
            )
            .order_by(snapshot_table.symbol_id.asc(), snapshot_table.id.desc())
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

    def list_history_for_market(self, market: str | None, tickers: list[str] | None = None) -> list[dict]:
        physical_tables = _physical_snapshot_tables_for_market(market)
        snapshot_table = (
            physical_tables[0] if physical_tables is not None else FundamentalSnapshot
        )
        stmt = (
            select(snapshot_table, Symbol)
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .order_by(market_sort_case(Symbol.market), Symbol.ticker.asc(), snapshot_table.report_date.asc(), snapshot_table.id.asc())
        )
        if market and market != "ALL":
            stmt = stmt.where(Symbol.market == market)
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_([ticker.upper() for ticker in tickers]))
        rows = self.db.execute(stmt).all()
        return [self._to_dict(snapshot, symbol) for snapshot, symbol in rows]

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
            "created_at": snapshot.created_at,
            "updated_at": snapshot.updated_at,
            "source_layer": snapshot.__class__.__tablename__,
        }

class PointInTimeFeatureSnapshotRepository:
    """Append-only repository; an existing revision is never updated in place."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def append_snapshot(
        self,
        *,
        symbol_id: int,
        feature_name: str,
        feature_value: float,
        event_time: str,
        available_time: str,
        ingested_time: str,
        source: str,
        source_record_id: str,
        revision_id: str,
        payload: dict | None = None,
        commit: bool = True,
    ) -> tuple[PointInTimeFeatureSnapshot, bool]:
        symbol_market = _symbol_market(self.db, symbol_id)
        physical_tables = _physical_snapshot_tables_for_market(symbol_market)
        physical_table = physical_tables[1] if physical_tables is not None else None
        write_legacy = _legacy_snapshot_writes_enabled(
            self.db,
            physical_tables,
            market=symbol_market,
        )
        for name, value in (
            ("feature_name", feature_name),
            ("source", source),
            ("source_record_id", source_record_id),
            ("revision_id", revision_id),
        ):
            if not str(value or "").strip():
                raise ValueError(f"{name} must not be empty")
        numeric_value = float(feature_value)
        if not math.isfinite(numeric_value):
            raise ValueError("feature_value must be finite")
        parsed_event = _safe_parse_iso(event_time)
        parsed_available = _safe_parse_iso(available_time)
        parsed_ingested = _safe_parse_iso(ingested_time)
        if any(item is None or item.tzinfo is None for item in (parsed_event, parsed_available, parsed_ingested)):
            raise ValueError("feature timestamps must be valid and timezone-aware")
        if parsed_available < parsed_event:
            raise ValueError("available_time must not precede event_time")
        normalized_event_time = parsed_event.astimezone(UTC).isoformat()
        normalized_available_time = parsed_available.astimezone(UTC).isoformat()
        normalized_ingested_time = parsed_ingested.astimezone(UTC).isoformat()
        stmt = select(PointInTimeFeatureSnapshot).where(
            PointInTimeFeatureSnapshot.symbol_id == symbol_id,
            PointInTimeFeatureSnapshot.feature_name == feature_name,
            PointInTimeFeatureSnapshot.source == source,
            PointInTimeFeatureSnapshot.source_record_id == source_record_id,
            PointInTimeFeatureSnapshot.revision_id == revision_id,
        )
        existing = self.db.scalar(stmt) if write_legacy else None
        physical_existing = None
        if physical_table is not None:
            physical_existing = self.db.scalar(
                select(physical_table).where(
                    physical_table.symbol_id == symbol_id,
                    physical_table.feature_name == feature_name,
                    physical_table.source == source,
                    physical_table.source_record_id == source_record_id,
                    physical_table.revision_id == revision_id,
                )
            )
        # A revision identity that already exists with a different value is a
        # record-level data conflict, not a batch-fatal error: isolate it in the
        # conflict ledger so the remaining records keep landing. History stays
        # append-only (never overwritten) and the conflict is never silently
        # dropped -- it is persisted and returned as (existing, False).
        legacy_conflict = existing is not None and (
            float(existing.feature_value) != numeric_value
            or existing.event_time != normalized_event_time
        )
        physical_conflict = physical_existing is not None and (
            float(physical_existing.feature_value) != numeric_value
            or _physical_datetime(physical_existing.event_time).astimezone(UTC)
            != _physical_datetime(normalized_event_time).astimezone(UTC)
        )
        if legacy_conflict or physical_conflict:
            store_kind = "legacy" if legacy_conflict else "physical"
            conflicted = existing if legacy_conflict else physical_existing
            if conflicted is None:  # defensive; the flags above imply a row
                raise RuntimeError("Feature conflict detected without a colliding row.")
            self._record_conflict(
                symbol_id=symbol_id,
                market=symbol_market,
                feature_name=feature_name,
                source=source,
                source_record_id=source_record_id,
                store_kind=store_kind,
                conflicted=conflicted,
                incoming_revision_id=revision_id,
                incoming_feature_value=numeric_value,
                incoming_event_time=normalized_event_time,
                payload=payload,
            )
            if commit:
                self.db.commit()
                self.db.refresh(conflicted)
            else:
                self.db.flush()
            return conflicted, False
        if existing is not None and physical_table is None:
            return existing, False
        created_at = utc_now_iso()
        snapshot = existing
        inserted = False
        if write_legacy and existing is None:
            snapshot = PointInTimeFeatureSnapshot(
                symbol_id=symbol_id,
                feature_name=feature_name,
                feature_value=numeric_value,
                event_time=normalized_event_time,
                available_time=normalized_available_time,
                ingested_time=normalized_ingested_time,
                source=source,
                source_record_id=source_record_id,
                revision_id=revision_id,
                payload_json=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if payload is not None
                else None,
                created_at=created_at,
            )
            inserted = True
            self.db.add(snapshot)
        physical_inserted = False
        if physical_table is not None and physical_existing is None:
            physical_existing = physical_table(
                symbol_id=symbol_id,
                market=symbol_market,
                feature_name=feature_name,
                feature_value=numeric_value,
                event_time=_physical_datetime(normalized_event_time),
                available_time=_physical_datetime(normalized_available_time),
                ingested_time=_physical_datetime(normalized_ingested_time),
                source=source,
                source_record_id=source_record_id,
                revision_id=revision_id,
                payload_json=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
                if payload is not None
                else None,
                created_at=_physical_datetime(created_at),
            )
            physical_inserted = True
            self.db.add(physical_existing)
        result = snapshot if write_legacy else physical_existing
        if result is None:
            raise RuntimeError("Feature write did not produce a legacy or physical row.")
        result_inserted = inserted if write_legacy else physical_inserted
        if commit:
            self.db.commit()
            self.db.refresh(result)
        else:
            self.db.flush()
        return result, result_inserted

    def _record_conflict(
        self,
        *,
        symbol_id: int,
        market: str | None,
        feature_name: str,
        source: str,
        source_record_id: str,
        store_kind: str,
        conflicted: PointInTimeFeatureSnapshot,
        incoming_revision_id: str,
        incoming_feature_value: float,
        incoming_event_time: str,
        payload: dict | None,
    ) -> PointInTimeFeatureConflict:
        """Persist one revision-identity collision (append-only audit row)."""
        conflict = PointInTimeFeatureConflict(
            symbol_id=symbol_id,
            market=market,
            feature_name=feature_name,
            source=source,
            source_record_id=source_record_id,
            store_kind=store_kind,
            existing_revision_id=str(conflicted.revision_id),
            incoming_revision_id=incoming_revision_id,
            existing_feature_value=float(conflicted.feature_value),
            incoming_feature_value=incoming_feature_value,
            existing_event_time=_physical_datetime(conflicted.event_time).astimezone(UTC).isoformat(),
            incoming_event_time=incoming_event_time,
            existing_snapshot_id=int(conflicted.id),
            payload_json=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if payload is not None
            else None,
            detected_at=utc_now_iso(),
        )
        self.db.add(conflict)
        return conflict

    def list_conflicts(
        self,
        *,
        symbol_id: int | None = None,
        feature_name: str | None = None,
        source: str | None = None,
        limit: int = 200,
    ) -> list[PointInTimeFeatureConflict]:
        """Conflict-ledger query entry point (newest first, auditable)."""
        stmt = select(PointInTimeFeatureConflict)
        if symbol_id is not None:
            stmt = stmt.where(PointInTimeFeatureConflict.symbol_id == symbol_id)
        if feature_name:
            stmt = stmt.where(PointInTimeFeatureConflict.feature_name == feature_name)
        if source:
            stmt = stmt.where(PointInTimeFeatureConflict.source == source)
        stmt = stmt.order_by(PointInTimeFeatureConflict.id.desc()).limit(max(1, int(limit)))
        return list(self.db.scalars(stmt))

    def count_conflicts(
        self,
        *,
        symbol_id: int | None = None,
        feature_name: str | None = None,
        source: str | None = None,
    ) -> int:
        """Count persisted revision-identity conflicts."""
        stmt = select(func.count()).select_from(PointInTimeFeatureConflict)
        if symbol_id is not None:
            stmt = stmt.where(PointInTimeFeatureConflict.symbol_id == symbol_id)
        if feature_name:
            stmt = stmt.where(PointInTimeFeatureConflict.feature_name == feature_name)
        if source:
            stmt = stmt.where(PointInTimeFeatureConflict.source == source)
        return int(self.db.scalar(stmt) or 0)

    def list_history_for_market(
        self,
        market: str | None,
        *,
        tickers: list[str] | None = None,
        feature_names: list[str] | None = None,
    ) -> list[dict]:
        market_code = str(market or "").strip().upper()
        physical_tables = _physical_snapshot_tables_for_market(market_code)
        snapshot_table = (
            physical_tables[1]
            if physical_tables is not None
            else PointInTimeFeatureSnapshot
        )
        stmt = (
            select(snapshot_table, Symbol)
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .order_by(
                Symbol.ticker.asc(),
                snapshot_table.feature_name.asc(),
                snapshot_table.available_time.asc(),
                snapshot_table.id.asc(),
            )
        )
        if market_code and market_code != "ALL":
            stmt = stmt.where(Symbol.market == market_code)
        if tickers:
            stmt = stmt.where(Symbol.ticker.in_([item.upper() for item in tickers]))
        if feature_names:
            stmt = stmt.where(snapshot_table.feature_name.in_(feature_names))
        return [self._to_dict(snapshot, symbol) for snapshot, symbol in self.db.execute(stmt)]

    def summarize_market_coverage(
        self,
        market: str,
        *,
        required_features: tuple[str, ...] | list[str],
        minimum_cross_section_coverage: float = 0.60,
    ) -> dict:
        """Return a cheap operational snapshot; formal date coverage stays in the audit job."""

        market_code = str(market or "").strip().upper()
        physical_tables = _physical_snapshot_tables_for_market(market_code)
        snapshot_table = (
            physical_tables[1]
            if physical_tables is not None
            else PointInTimeFeatureSnapshot
        )
        feature_names = tuple(dict.fromkeys(str(item).strip() for item in required_features if str(item).strip()))
        total_symbols = int(
            self.db.scalar(
                select(func.count(Symbol.id)).where(
                    Symbol.market == market_code,
                    Symbol.is_active == 1,
                )
            )
            or 0
        )
        rows = self.db.execute(
            select(
                snapshot_table.feature_name,
                func.count(func.distinct(snapshot_table.symbol_id)),
                func.count(snapshot_table.id),
                func.max(snapshot_table.available_time),
            )
            .join(Symbol, Symbol.id == snapshot_table.symbol_id)
            .where(
                Symbol.market == market_code,
                Symbol.is_active == 1,
                snapshot_table.feature_name.in_(feature_names),
            )
            .group_by(snapshot_table.feature_name)
        ).all()
        by_name = {
            str(feature_name): {
                "symbol_count": int(symbol_count or 0),
                "record_count": int(record_count or 0),
                "latest_available_time": latest_available_time,
            }
            for feature_name, symbol_count, record_count, latest_available_time in rows
        }
        feature_coverage = []
        for feature_name in feature_names:
            values = by_name.get(feature_name, {})
            symbol_count = int(values.get("symbol_count") or 0)
            coverage = (symbol_count / total_symbols) if total_symbols else 0.0
            feature_coverage.append(
                {
                    "feature_name": feature_name,
                    "symbol_count": symbol_count,
                    "record_count": int(values.get("record_count") or 0),
                    "coverage": coverage,
                    "coverage_pct": round(coverage * 100.0, 2),
                    "latest_available_time": values.get("latest_available_time"),
                    "cross_section_gate": "PASS"
                    if coverage >= minimum_cross_section_coverage
                    else "COLLECTING",
                }
            )

        ready_symbol_count = 0
        if feature_names:
            ready_subquery = (
                select(snapshot_table.symbol_id)
                .join(Symbol, Symbol.id == snapshot_table.symbol_id)
                .where(
                    Symbol.market == market_code,
                    Symbol.is_active == 1,
                    snapshot_table.feature_name.in_(feature_names),
                )
                .group_by(snapshot_table.symbol_id)
                .having(
                    func.count(func.distinct(snapshot_table.feature_name))
                    == len(feature_names)
                )
                .subquery()
            )
            ready_symbol_count = int(
                self.db.scalar(select(func.count()).select_from(ready_subquery)) or 0
            )
        minimum_coverage = min(
            (float(item["coverage"]) for item in feature_coverage),
            default=0.0,
        )
        return {
            "market": market_code,
            "required_features": list(feature_names),
            "total_symbols": total_symbols,
            "ready_symbol_count": ready_symbol_count,
            "ready_symbol_pct": round(
                (ready_symbol_count / total_symbols) * 100.0 if total_symbols else 0.0,
                2,
            ),
            "minimum_feature_coverage": minimum_coverage,
            "minimum_feature_coverage_pct": round(minimum_coverage * 100.0, 2),
            "minimum_cross_section_coverage": float(minimum_cross_section_coverage),
            "cross_section_gate": "PASS"
            if feature_coverage and minimum_coverage >= minimum_cross_section_coverage
            else "COLLECTING",
            "formal_date_gate": "PENDING_AUDIT",
            "feature_coverage": feature_coverage,
        }

    def summarize_market_coverage_as_of(
        self,
        market: str,
        *,
        cutoff: datetime,
        required_features: tuple[str, ...] | list[str],
        max_age_days: dict[str, int],
        minimum_cross_section_coverage: float = 0.60,
    ) -> dict:
        """Return freshness-aware coverage at one historical post-close cutoff."""

        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("cutoff must be timezone-aware")
        if not 0.0 < minimum_cross_section_coverage <= 1.0:
            raise ValueError("minimum_cross_section_coverage must be in (0, 1]")
        market_code = str(market or "").strip().upper()
        physical_tables = _physical_snapshot_tables_for_market(market_code)
        snapshot_table = (
            physical_tables[1]
            if physical_tables is not None
            else PointInTimeFeatureSnapshot
        )
        feature_names = tuple(
            dict.fromkeys(str(item).strip() for item in required_features if str(item).strip())
        )
        missing_ages = set(feature_names) - set(max_age_days)
        if missing_ages:
            raise ValueError("max_age_days is missing features: " + ", ".join(sorted(missing_ages)))
        if any(int(max_age_days[name]) <= 0 for name in feature_names):
            raise ValueError("max_age_days values must be positive")

        cutoff_utc = cutoff.astimezone(UTC)
        cutoff_text = cutoff_utc.isoformat()
        cutoff_value = cutoff_utc if physical_tables is not None else cutoff_text
        total_symbols = int(
            self.db.scalar(
                select(func.count(Symbol.id)).where(
                    Symbol.market == market_code,
                    Symbol.is_active == 1,
                )
            )
            or 0
        )
        feature_coverage: list[dict] = []
        freshness_filters = []
        for feature_name in feature_names:
            minimum_datetime = cutoff_utc - timedelta(
                days=int(max_age_days[feature_name])
            )
            minimum_time = (
                minimum_datetime
                if physical_tables is not None
                else minimum_datetime.isoformat()
            )
            freshness = or_(
                snapshot_table.available_time >= minimum_time,
                snapshot_table.ingested_time >= minimum_time,
            )
            feature_filter = (
                (snapshot_table.feature_name == feature_name)
                & freshness
            )
            freshness_filters.append(feature_filter)
            symbol_count = int(
                self.db.scalar(
                    select(func.count(func.distinct(snapshot_table.symbol_id)))
                    .join(Symbol, Symbol.id == snapshot_table.symbol_id)
                    .where(
                        Symbol.market == market_code,
                        Symbol.is_active == 1,
                        snapshot_table.feature_name == feature_name,
                        snapshot_table.event_time <= cutoff_value,
                        snapshot_table.available_time <= cutoff_value,
                        snapshot_table.ingested_time <= cutoff_value,
                        freshness,
                    )
                )
                or 0
            )
            coverage = symbol_count / total_symbols if total_symbols else 0.0
            feature_coverage.append(
                {
                    "feature_name": feature_name,
                    "symbol_count": symbol_count,
                    "coverage": coverage,
                    "coverage_pct": round(coverage * 100.0, 2),
                    "max_age_days": int(max_age_days[feature_name]),
                    "cross_section_gate": (
                        "PASS"
                        if coverage >= minimum_cross_section_coverage
                        else "COLLECTING"
                    ),
                }
            )

        ready_symbol_count = 0
        if feature_names:
            ready_subquery = (
                select(snapshot_table.symbol_id)
                .join(Symbol, Symbol.id == snapshot_table.symbol_id)
                .where(
                    Symbol.market == market_code,
                    Symbol.is_active == 1,
                    snapshot_table.event_time <= cutoff_value,
                    snapshot_table.available_time <= cutoff_value,
                    snapshot_table.ingested_time <= cutoff_value,
                    or_(*freshness_filters),
                )
                .group_by(snapshot_table.symbol_id)
                .having(
                    func.count(func.distinct(snapshot_table.feature_name))
                    == len(feature_names)
                )
                .subquery()
            )
            ready_symbol_count = int(
                self.db.scalar(select(func.count()).select_from(ready_subquery)) or 0
            )
        minimum_coverage = min(
            (float(item["coverage"]) for item in feature_coverage),
            default=0.0,
        )
        return {
            "market": market_code,
            "cutoff": cutoff_text,
            "required_features": list(feature_names),
            "total_symbols": total_symbols,
            "ready_symbol_count": ready_symbol_count,
            "ready_symbol_pct": round(
                (ready_symbol_count / total_symbols) * 100.0 if total_symbols else 0.0,
                2,
            ),
            "minimum_feature_coverage": minimum_coverage,
            "minimum_feature_coverage_pct": round(minimum_coverage * 100.0, 2),
            "minimum_cross_section_coverage": float(minimum_cross_section_coverage),
            "as_of_gate": (
                "PASS"
                if feature_coverage and minimum_coverage >= minimum_cross_section_coverage
                else "COLLECTING"
            ),
            "feature_coverage": feature_coverage,
        }

    @staticmethod
    def _to_dict(snapshot: PointInTimeFeatureSnapshot, symbol: Symbol) -> dict:
        return {
            "id": snapshot.id,
            "symbol_id": snapshot.symbol_id,
            "ticker": symbol.ticker,
            "market": symbol.market,
            "feature_name": snapshot.feature_name,
            "feature_value": snapshot.feature_value,
            "event_time": snapshot.event_time,
            "available_time": snapshot.available_time,
            "ingested_time": snapshot.ingested_time,
            "source": snapshot.source,
            "source_record_id": snapshot.source_record_id,
            "revision_id": snapshot.revision_id,
            "payload_json": snapshot.payload_json,
            "created_at": snapshot.created_at,
            "source_layer": snapshot.__class__.__tablename__,
        }
