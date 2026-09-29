from __future__ import annotations

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.models.tables import Symbol
from app.services.market_snapshot_backfill import (
    FUNDAMENTAL_COLUMNS,
    POINT_IN_TIME_COLUMNS,
    TECHNICAL_COLUMNS,
    load_market_snapshot_source,
    snapshot_semantic_summary,
)
from app.services.market_fact_constraints import inspect_market_fact_constraints
from app.services.market_storage_routing import (
    physical_fact_write_markets,
    physical_snapshot_models,
)
from app.services.repository import (
    FundamentalSnapshotRepository,
    PointInTimeFeatureSnapshotRepository,
    TechnicalSnapshotRepository,
)
from app.services.time_utils import app_now_iso


def _physical_rows(
    db: Session,
    table,
    columns: tuple[str, ...],
    *,
    market: str,
) -> list[dict]:
    return [
        dict(row)
        for row in db.execute(
            select(*(getattr(table, column) for column in columns)).where(
                table.market == market
            )
        ).mappings()
    ]


def audit_market_physical_snapshots(db: Session, *, market: str = "CN") -> dict:
    market_code = str(market or "").strip().upper()
    target_tables = physical_snapshot_models(market_code)
    market_tables = {
        item: physical_snapshot_models(item)
        for item in sorted(physical_fact_write_markets())
    }
    columns = (FUNDAMENTAL_COLUMNS, POINT_IN_TIME_COLUMNS, TECHNICAL_COLUMNS)
    names = ("fundamental_snapshots", "point_in_time_features", "technical_snapshots")
    source_rows = load_market_snapshot_source(db, market=market_code)
    physical_rows = {
        name: _physical_rows(
            db,
            table,
            table_columns,
            market=market_code,
        )
        for name, table, table_columns in zip(names, target_tables, columns, strict=True)
    }
    source_summary = snapshot_semantic_summary(source_rows)
    physical_summary = snapshot_semantic_summary(physical_rows)
    exact_match = source_summary == physical_summary

    wrong_market_rows = {
        table.__tablename__: int(
            db.scalar(select(func.count(table.id)).where(table.market != market_code)) or 0
        )
        for table in target_tables
    }
    symbol_market_mismatches = {
        table.__tablename__: int(
            db.scalar(
                select(func.count(table.id))
                .join(Symbol, Symbol.id == table.symbol_id)
                .where(Symbol.market != table.market)
            )
            or 0
        )
        for table in target_tables
    }
    market_counts = {
        item: {
            table.__tablename__: int(db.scalar(select(func.count(table.id))) or 0)
            for table in tables
        }
        for item, tables in market_tables.items()
    }

    inspector = inspect(db.get_bind())
    all_tables = tuple(
        table for tables in market_tables.values() for table in tables
    )
    table_names = set(inspector.get_table_names())
    constraints = {
        table.__tablename__: sorted(
            str(item.get("name") or "")
            for item in inspector.get_check_constraints(table.__tablename__)
        )
        for table in all_tables
    }
    indexes = {
        table.__tablename__: sorted(
            str(item.get("name") or "")
            for item in inspector.get_indexes(table.__tablename__)
        )
        for table in all_tables
    }
    table_bytes = {
        table.__tablename__: int(
            db.scalar(
                text("SELECT pg_total_relation_size(CAST(:table_name AS regclass))"),
                {"table_name": table.__tablename__},
            )
            or 0
        )
        for table in all_tables
    }

    ticker_by_table = []
    for table in target_tables:
        ticker_by_table.append(
            str(
                db.scalar(
                    select(Symbol.ticker)
                    .join(table, table.symbol_id == Symbol.id)
                    .where(table.market == market_code)
                    .limit(1)
                )
                or ""
            )
        )
    fundamental_rows = FundamentalSnapshotRepository(db).list_latest_for_market(
        market_code,
        tickers=[ticker_by_table[0]],
    )
    pit_rows = PointInTimeFeatureSnapshotRepository(db).list_history_for_market(
        market_code,
        tickers=[ticker_by_table[1]],
    )
    technical_rows = TechnicalSnapshotRepository(db).list_latest_for_market(
        market_code,
        tickers=[ticker_by_table[2]],
    )
    repository_source_layers = {
        "fundamental": str(fundamental_rows[0].get("source_layer") or "")
        if fundamental_rows
        else "",
        "point_in_time": str(pit_rows[0].get("source_layer") or "")
        if pit_rows
        else "",
        "technical": str(technical_rows[0].get("source_layer") or "")
        if technical_rows
        else "",
    }

    explain_plans = {
        table.__tablename__: "\n".join(
            str(row[0])
            for row in db.execute(
                text(
                    f"EXPLAIN SELECT * FROM {table.__tablename__} "
                    "WHERE market = :market LIMIT 5"
                ),
                {"market": market_code},
            )
        )
        for table in target_tables
    }
    fact_constraints = inspect_market_fact_constraints(db.connection())
    snapshot_fact_tables = tuple(table.__tablename__ for table in all_tables)
    expected_source_layers = {
        "fundamental": target_tables[0].__tablename__,
        "point_in_time": target_tables[1].__tablename__,
        "technical": target_tables[2].__tablename__,
    }
    repository_checks = {
        name: (
            repository_source_layers[name] == table_name
            if source_summary["counts"][logical_name] > 0
            else repository_source_layers[name] in {"", table_name}
        )
        for name, table_name, logical_name in (
            ("fundamental", expected_source_layers["fundamental"], "fundamental_snapshots"),
            ("point_in_time", expected_source_layers["point_in_time"], "point_in_time_features"),
            ("technical", expected_source_layers["technical"], "technical_snapshots"),
        )
    }
    checks = {
        "physical_tables_exist": {table.__tablename__ for table in all_tables}.issubset(
            table_names
        ),
        "source_exact_match": exact_match,
        "wrong_market_rows_zero": all(value == 0 for value in wrong_market_rows.values()),
        "symbol_market_mismatch_zero": all(
            value == 0 for value in symbol_market_mismatches.values()
        ),
        "all_market_constraints_present": all(
            f"ck_{table.__tablename__}_market" in constraints[table.__tablename__]
            for table in all_tables
        ),
        "repositories_read_market_physical_tables": all(repository_checks.values()),
        "explain_is_market_local": all(
            table_name in plan.lower()
            and " on fundamental_snapshots " not in plan.lower()
            and " on point_in_time_feature_snapshots " not in plan.lower()
            and " on technical_snapshots " not in plan.lower()
            for table_name, plan in explain_plans.items()
        ),
        "composite_symbol_market_fks_present": all(
            fact_constraints["tables"][name]["compliant"]
            for name in snapshot_fact_tables
        ),
    }
    return {
        "audit_version": "market-physical-snapshots-v2",
        "generated_at": app_now_iso(),
        "status": "pass" if all(checks.values()) else "failed",
        "market": market_code,
        "source_summary": source_summary,
        "physical_summary": physical_summary,
        "exact_match": exact_match,
        "wrong_market_rows": wrong_market_rows,
        "symbol_market_mismatches": symbol_market_mismatches,
        "market_counts": market_counts,
        "repository_source_layers": repository_source_layers,
        "constraints": constraints,
        "indexes": indexes,
        "table_bytes": table_bytes,
        "explain_plans": explain_plans,
        "fact_constraints": {
            name: fact_constraints["tables"][name]
            for name in snapshot_fact_tables
        },
        "checks": checks,
    }
