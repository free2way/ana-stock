from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.models.tables import (
    FundamentalSnapshot,
    PointInTimeFeatureSnapshot,
    Symbol,
    TechnicalSnapshot,
)
from app.services.market_storage_routing import (
    normalize_fact_market,
    physical_snapshot_models,
)


FUNDAMENTAL_COLUMNS = (
    "symbol_id",
    "report_date",
    "source",
    "listing_date",
    "pe_ttm",
    "dividend_yield",
    "market_cap",
    "roe_avg_3y",
    "net_profit_yoy",
    "revenue_yoy",
    "debt_to_assets",
    "data_json",
    "created_at",
    "updated_at",
)
POINT_IN_TIME_COLUMNS = (
    "symbol_id",
    "feature_name",
    "feature_value",
    "event_time",
    "available_time",
    "ingested_time",
    "source",
    "source_record_id",
    "revision_id",
    "payload_json",
    "created_at",
)
TECHNICAL_COLUMNS = (
    "symbol_id",
    "as_of_date",
    "source",
    "limit_up_yesterday",
    "volume_breakout",
    "ma_cluster",
    "bullish_ma_stack",
    "macd_underwater_cross",
    "matched_patterns_json",
    "created_at",
    "updated_at",
)
SNAPSHOT_SOURCES = (
    ("fundamental_snapshots", FundamentalSnapshot, FUNDAMENTAL_COLUMNS),
    (
        "point_in_time_features",
        PointInTimeFeatureSnapshot,
        POINT_IN_TIME_COLUMNS,
    ),
    ("technical_snapshots", TechnicalSnapshot, TECHNICAL_COLUMNS),
)
DATE_COLUMNS = {"report_date", "listing_date", "as_of_date"}
TIMESTAMP_COLUMNS = {
    "event_time",
    "available_time",
    "ingested_time",
    "created_at",
    "updated_at",
}


def _chunks(rows: list[dict], size: int = 1000) -> list[list[dict]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _load_source_rows(
    db: Session,
    source_table,
    *,
    market: str,
    columns: tuple[str, ...],
) -> list[dict]:
    return [
        dict(row)
        for row in db.execute(
            select(*(getattr(source_table, column) for column in columns))
            .join(Symbol, Symbol.id == source_table.symbol_id)
            .where(Symbol.market == market)
        ).mappings()
    ]


def _load_physical_rows(
    db: Session,
    target_table,
    *,
    market: str,
    columns: tuple[str, ...],
) -> list[dict]:
    return [
        dict(row)
        for row in db.execute(
            select(*(getattr(target_table, column) for column in columns)).where(
                target_table.market == market
            )
        ).mappings()
    ]


def _canonical_value(column: str, value: object) -> object:
    if value is None:
        return None
    if column in DATE_COLUMNS:
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return date.fromisoformat(str(value)[:10]).isoformat()
    if column in TIMESTAMP_COLUMNS:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"Timestamp {column} must be timezone-aware: {value!r}")
        return parsed.astimezone(UTC).isoformat()
    return value


def physical_snapshot_payload(
    name: str,
    row: dict,
    *,
    market: str,
) -> dict:
    columns_by_name = {item_name: item_columns for item_name, _, item_columns in SNAPSHOT_SOURCES}
    columns = columns_by_name[name]
    payload = {
        column: _canonical_value(column, row.get(column))
        for column in columns
    }
    for column in DATE_COLUMNS & set(columns):
        value = payload[column]
        payload[column] = date.fromisoformat(str(value)) if value is not None else None
    for column in TIMESTAMP_COLUMNS & set(columns):
        value = payload[column]
        payload[column] = (
            datetime.fromisoformat(str(value)) if value is not None else None
        )
    payload["market"] = market
    return payload


def _digest(rows: list[dict], columns: tuple[str, ...]) -> str:
    serialized = sorted(
        json.dumps(
            {
                column: _canonical_value(column, row.get(column))
                for column in columns
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        for row in rows
    )
    return hashlib.sha256("\n".join(serialized).encode("utf-8")).hexdigest()


def load_market_snapshot_source(db: Session, *, market: str) -> dict[str, list[dict]]:
    market_code = normalize_fact_market(market)
    return {
        name: _load_source_rows(
            db,
            source_table,
            market=market_code,
            columns=columns,
        )
        for name, source_table, columns in SNAPSHOT_SOURCES
    }


def snapshot_semantic_summary(rows_by_name: dict[str, list[dict]]) -> dict:
    columns_by_name = {name: columns for name, _, columns in SNAPSHOT_SOURCES}
    return {
        "counts": {name: len(rows) for name, rows in rows_by_name.items()},
        "digests": {
            name: _digest(rows, columns_by_name[name])
            for name, rows in rows_by_name.items()
        },
    }


def publish_market_snapshots(db: Session, *, market: str) -> dict:
    market_code = normalize_fact_market(market)
    source_rows = load_market_snapshot_source(db, market=market_code)
    source_summary = snapshot_semantic_summary(source_rows)
    target_tables = physical_snapshot_models(market_code)
    columns_by_name = {name: columns for name, _, columns in SNAPSHOT_SOURCES}

    existing_rows = {
        name: _load_physical_rows(
            db,
            target_table,
            market=market_code,
            columns=columns_by_name[name],
        )
        for (name, _, _), target_table in zip(
            SNAPSHOT_SOURCES,
            target_tables,
            strict=True,
        )
    }
    existing_summary = snapshot_semantic_summary(existing_rows)
    if existing_summary == source_summary:
        return {
            "status": "pass",
            "action": "unchanged",
            "market": market_code,
            **source_summary,
            "exact_match": True,
        }

    try:
        for (name, _, columns), target_table in zip(
            SNAPSHOT_SOURCES,
            target_tables,
            strict=True,
        ):
            db.execute(delete(target_table).where(target_table.market == market_code))
            payloads = [
                physical_snapshot_payload(name, row, market=market_code)
                for row in source_rows[name]
            ]
            for chunk in _chunks(payloads):
                db.execute(insert(target_table).values(chunk))

        published_rows = {
            name: _load_physical_rows(
                db,
                target_table,
                market=market_code,
                columns=columns,
            )
            for (name, _, columns), target_table in zip(
                SNAPSHOT_SOURCES,
                target_tables,
                strict=True,
            )
        }
        published_summary = snapshot_semantic_summary(published_rows)
        if published_summary != source_summary:
            raise RuntimeError("Physical snapshot semantic parity failed.")
        db.commit()
    except Exception:
        db.rollback()
        raise

    wrong_market_rows = {
        table.__tablename__: int(
            db.scalar(select(func.count(table.id)).where(table.market != market_code)) or 0
        )
        for table in target_tables
    }
    return {
        "status": "pass",
        "action": "replaced",
        "market": market_code,
        **published_summary,
        "source_counts": source_summary["counts"],
        "source_digests": source_summary["digests"],
        "wrong_market_rows": wrong_market_rows,
        "exact_match": True,
        "tables": [table.__tablename__ for table in target_tables],
    }
