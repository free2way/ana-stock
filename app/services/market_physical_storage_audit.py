from __future__ import annotations

import hashlib
import json
from datetime import date

from sqlalchemy import func, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import (
    LivePrediction,
    ModelRun,
    Symbol,
)
from app.services.market_hot_predictions import MarketHotPredictionRepository, _prepare_rows
from app.services.market_fact_constraints import inspect_market_fact_constraints
from app.services.market_storage_routing import (
    enabled_physical_markets,
    physical_fact_table_contract,
    physical_fact_write_markets,
    physical_hot_prediction_models,
    physical_live_prediction_model,
)
from app.services.prediction_archive_migration import (
    _load_detail_rows,
    _load_explanation_rows,
    _load_prediction_rows,
)
from app.services.repository import PredictionRepository
from app.services.time_utils import app_now, app_now_iso


PARITY_COLUMNS = (
    "symbol_id",
    "trade_date",
    "score",
    "rank_value",
    "confidence",
    "signal_label",
    "signal_strength",
    "expected_return_20d",
    "expected_drawdown_20d",
    "model_reward_risk_ratio",
    "conviction_bucket",
    "position_size_hint",
    "entry_style",
    "percentile",
    "summary_text",
)


def audit_market_table_isolation_contract(db: Session) -> dict:
    """Audit the structural CN/HK/US table-isolation contract without mutations."""

    contract = physical_fact_table_contract()
    inspector = inspect(db.get_bind())
    database_tables = set(inspector.get_table_names())
    market_tables = {
        market: set(mapping.values()) for market, mapping in contract.items()
    }
    all_tables = set().union(*market_tables.values())
    missing_tables = sorted(all_tables - database_tables)
    parent_tables = {
        market: {
            name
            for logical_name, name in mapping.items()
            if logical_name
            not in {
                "prediction_details",
                "prediction_explanations",
                "prediction_trade_plans",
            }
        }
        for market, mapping in contract.items()
    }
    child_parent = {
        mapping[child_name]: mapping["predictions"]
        for mapping in contract.values()
        for child_name in (
            "prediction_details",
            "prediction_explanations",
            "prediction_trade_plans",
        )
    }
    row_counts: dict[str, int] = {}
    wrong_market_rows: dict[str, int] = {}
    check_constraints: dict[str, list[str]] = {}
    for market, tables in parent_tables.items():
        for table_name in sorted(tables):
            if table_name not in database_tables:
                continue
            row_counts[table_name] = int(
                db.scalar(text(f'SELECT count(*) FROM "{table_name}"')) or 0
            )
            wrong_market_rows[table_name] = int(
                db.scalar(
                    text(
                        f'SELECT count(*) FROM "{table_name}" '
                        "WHERE market IS DISTINCT FROM :market"
                    ),
                    {"market": market},
                )
                or 0
            )
            check_constraints[table_name] = sorted(
                str(item.get("name") or "")
                for item in inspector.get_check_constraints(table_name)
            )
    child_foreign_keys: dict[str, list[dict]] = {}
    child_contract_ok = True
    for child_name, expected_parent in child_parent.items():
        if child_name not in database_tables:
            child_contract_ok = False
            continue
        row_counts[child_name] = int(
            db.scalar(text(f'SELECT count(*) FROM "{child_name}"')) or 0
        )
        foreign_keys = [
            {
                "referred_table": str(item.get("referred_table") or ""),
                "constrained_columns": list(item.get("constrained_columns") or []),
                "referred_columns": list(item.get("referred_columns") or []),
                "ondelete": str((item.get("options") or {}).get("ondelete") or "").upper(),
            }
            for item in inspector.get_foreign_keys(child_name)
        ]
        child_foreign_keys[child_name] = foreign_keys
        child_contract_ok = child_contract_ok and any(
            item["referred_table"] == expected_parent
            and item["constrained_columns"] == ["prediction_id"]
            and item["referred_columns"] == ["id"]
            and item["ondelete"] == "CASCADE"
            for item in foreign_keys
        )

    settings = get_settings()
    configured_markets = {
        "live": sorted(enabled_physical_markets(settings.market_physical_live_markets)),
        "hot": sorted(enabled_physical_markets(settings.market_physical_hot_markets)),
        "snapshots": sorted(
            enabled_physical_markets(settings.market_physical_snapshot_markets)
        ),
    }
    fact_constraints = inspect_market_fact_constraints(db.connection())
    expected_market_checks = all(
        f"ck_{table_name}_market" in check_constraints.get(table_name, [])
        for tables in parent_tables.values()
        for table_name in tables
    )
    checks = {
        "twenty_seven_physical_tables_exist": not missing_tables and len(all_tables) == 27,
        "market_table_sets_pairwise_disjoint": sum(
            len(tables) for tables in market_tables.values()
        ) == len(all_tables),
        "market_table_prefixes": all(
            all(name.startswith(f"{market.lower()}_") for name in tables)
            for market, tables in market_tables.items()
        ),
        "required_physical_write_markets_cn_hk_us": set(physical_fact_write_markets())
        == {"CN", "HK", "US"},
        "read_rollout_defaults_include_cn_hk_us": all(
            set(markets) == {"CN", "HK", "US"}
            for markets in configured_markets.values()
        ),
        "parent_market_check_constraints_present": expected_market_checks,
        "parent_wrong_market_rows_zero": all(
            value == 0 for value in wrong_market_rows.values()
        ),
        "parent_symbol_market_fks_compliant": fact_constraints["status"] == "pass",
        "child_tables_reference_same_market_parent": child_contract_ok,
    }
    return {
        "audit_version": "market-table-isolation-contract-v2",
        "generated_at": app_now_iso(),
        "status": "pass" if all(checks.values()) else "failed",
        "markets": ["CN", "HK", "US"],
        "market_status": {"CN": "active", "HK": "storage_ready", "US": "active"},
        "table_contract": contract,
        "configured_markets": configured_markets,
        "missing_tables": missing_tables,
        "row_counts": dict(sorted(row_counts.items())),
        "wrong_market_rows": dict(sorted(wrong_market_rows.items())),
        "check_constraints": check_constraints,
        "child_foreign_keys": child_foreign_keys,
        "fact_constraints": fact_constraints,
        "checks": checks,
    }


def _digest_table_rows(db: Session, table, *, model_run_id: int) -> tuple[int, str]:
    columns = [getattr(table, column) for column in PARITY_COLUMNS]
    rows = [
        dict(row)
        for row in db.execute(
            select(*columns)
            .where(table.model_run_id == int(model_run_id))
            .order_by(table.symbol_id.asc(), table.trade_date.asc())
        ).mappings()
    ]
    serialized = [
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        for row in rows
    ]
    return len(rows), hashlib.sha256("\n".join(serialized).encode("utf-8")).hexdigest()


def _constraint_rejects_wrong_market(db: Session, table, *, wrong_market: str) -> bool:
    seed = db.execute(
        select(table.model_run_id, table.symbol_id).order_by(table.id.asc()).limit(1)
    ).first()
    if seed is None:
        return False
    rejected = False
    try:
        with db.begin_nested():
            db.execute(
                insert(table).values(
                    id=-1,
                    model_run_id=int(seed.model_run_id),
                    symbol_id=int(seed.symbol_id),
                    market=wrong_market,
                    trade_date=date(1900, 1, 1),
                    published_at=app_now(),
                )
            )
    except IntegrityError:
        rejected = True
    return rejected


def audit_market_physical_live_storage(db: Session, *, market: str = "CN") -> dict:
    normalized_market = str(market or "").strip().upper()
    target_table = physical_live_prediction_model(normalized_market)
    latest_run_id = db.scalar(select(func.max(target_table.model_run_id)))
    if latest_run_id is None:
        raise RuntimeError(
            f"{target_table.__tablename__} is empty; publish or backfill {normalized_market} first."
        )

    physical_count, physical_digest = _digest_table_rows(
        db,
        target_table,
        model_run_id=int(latest_run_id),
    )
    legacy_count, legacy_digest = _digest_table_rows(
        db,
        LivePrediction,
        model_run_id=int(latest_run_id),
    )
    wrong_market_rows = int(
        db.scalar(
            select(func.count(target_table.id)).where(
                target_table.market != normalized_market
            )
        )
        or 0
    )
    symbol_mismatch = int(
        db.scalar(
            select(func.count(target_table.id))
            .join(Symbol, Symbol.id == target_table.symbol_id)
            .where(Symbol.market != target_table.market)
        )
        or 0
    )
    model_run_mismatch = int(
        db.scalar(
            select(func.count(target_table.id))
            .join(ModelRun, ModelRun.id == target_table.model_run_id)
            .where(ModelRun.market != target_table.market)
        )
        or 0
    )
    inspector = inspect(db.get_bind())
    table_names = set(inspector.get_table_names())
    live_tables = {
        item: physical_live_prediction_model(item)
        for item in sorted(physical_fact_write_markets())
    }
    constraint_names = {
        table.__tablename__: sorted(
            str(item.get("name") or "")
            for item in inspector.get_check_constraints(table.__tablename__)
        )
        for table in live_tables.values()
    }
    indexes = {
        table.__tablename__: sorted(
            str(item.get("name") or "")
            for item in inspector.get_indexes(table.__tablename__)
        )
        for table in live_tables.values()
    }
    table_bytes = {
        table.__tablename__: int(
            db.scalar(
                text("SELECT pg_total_relation_size(CAST(:table_name AS regclass))"),
                {"table_name": table.__tablename__},
            )
            or 0
        )
        for table in live_tables.values()
    }
    wrong_market = next(
        item for item in sorted(physical_fact_write_markets()) if item != normalized_market
    )
    constraint_rejects_wrong_market = _constraint_rejects_wrong_market(
        db,
        target_table,
        wrong_market=wrong_market,
    )
    source_rows = PredictionRepository(db).list_latest_predictions_for_market(
        normalized_market, limit=3
    )
    source_layer = str(source_rows[0].get("source_layer") or "") if source_rows else ""
    exact_match = physical_count == legacy_count and physical_digest == legacy_digest
    fact_constraints = inspect_market_fact_constraints(db.connection())
    live_fact_tables = tuple(table.__tablename__ for table in live_tables.values())
    checks = {
        "physical_tables_exist": set(live_fact_tables).issubset(table_names),
        "legacy_exact_match": exact_match,
        "wrong_market_rows_zero": wrong_market_rows == 0,
        "symbol_market_mismatch_zero": symbol_mismatch == 0,
        "model_run_market_mismatch_zero": model_run_mismatch == 0,
        "constraint_rejects_wrong_market": constraint_rejects_wrong_market,
        "all_market_constraints_present": all(
            f"ck_{table_name}_market" in constraint_names[table_name]
            for table_name in live_fact_tables
        ),
        "repository_reads_market_physical_table": source_layer
        == target_table.__tablename__,
        "composite_symbol_market_fks_present": all(
            fact_constraints["tables"][name]["compliant"]
            for name in live_fact_tables
        ),
    }
    return {
        "audit_version": "market-physical-live-storage-v2",
        "generated_at": app_now_iso(),
        "status": "pass" if all(checks.values()) else "failed",
        "market": normalized_market,
        "model_run_id": int(latest_run_id),
        "physical_rows": physical_count,
        "legacy_rows": legacy_count,
        "physical_sha256": physical_digest,
        "legacy_sha256": legacy_digest,
        "exact_match": exact_match,
        "wrong_market_rows": wrong_market_rows,
        "symbol_market_mismatch_rows": symbol_mismatch,
        "model_run_market_mismatch_rows": model_run_mismatch,
        "repository_source_layer": source_layer,
        "constraints": constraint_names,
        "indexes": indexes,
        "table_bytes": table_bytes,
        "fact_constraints": {
            name: fact_constraints["tables"][name] for name in live_fact_tables
        },
        "checks": checks,
    }


def _hot_constraint_rejects_wrong_market(
    db: Session,
    table,
    *,
    wrong_market: str,
) -> bool:
    seed = db.execute(
        select(table.model_run_id, table.symbol_id).order_by(table.id.asc()).limit(1)
    ).first()
    if seed is None:
        return False
    try:
        with db.begin_nested():
            db.execute(
                insert(table).values(
                    id=-1,
                    model_run_id=int(seed.model_run_id),
                    symbol_id=int(seed.symbol_id),
                    market=wrong_market,
                    trade_date=date(1900, 1, 1),
                    created_at=app_now(),
                )
            )
    except IntegrityError:
        return True
    return False


def audit_market_physical_hot_storage(
    db: Session,
    *,
    market: str = "CN",
    model_run_id: int | None = None,
) -> dict:
    normalized_market = str(market or "").strip().upper()
    target_prediction, target_detail, target_explanation = physical_hot_prediction_models(
        normalized_market
    )
    if model_run_id is None:
        model_run_id = db.scalar(select(func.max(target_prediction.model_run_id)))
    if model_run_id is None:
        raise RuntimeError(
            f"{target_prediction.__tablename__} is empty; publish or backfill "
            f"{normalized_market} first."
        )

    source = _prepare_rows(
        _load_prediction_rows(db, int(model_run_id)),
        _load_detail_rows(db, int(model_run_id)),
        _load_explanation_rows(db, int(model_run_id)),
    )
    physical = MarketHotPredictionRepository(db)._load_rows(
        market=normalized_market,
        model_run_id=int(model_run_id),
    )
    source_counts = {
        "predictions": len(source[0]),
        "prediction_details": len(source[1]),
        "prediction_explanations": len(source[2]),
    }
    physical_counts = {
        "predictions": len(physical[0]),
        "prediction_details": len(physical[1]),
        "prediction_explanations": len(physical[2]),
    }
    source_digests = MarketHotPredictionRepository._digests(*source)
    physical_digests = MarketHotPredictionRepository._digests(*physical)
    exact_match = source_counts == physical_counts and source_digests == physical_digests

    wrong_market_rows = int(
        db.scalar(
            select(func.count(target_prediction.id)).where(
                target_prediction.market != normalized_market
            )
        )
        or 0
    )
    symbol_mismatch = int(
        db.scalar(
            select(func.count(target_prediction.id))
            .join(Symbol, Symbol.id == target_prediction.symbol_id)
            .where(Symbol.market != target_prediction.market)
        )
        or 0
    )
    model_run_mismatch = int(
        db.scalar(
            select(func.count(target_prediction.id))
            .join(ModelRun, ModelRun.id == target_prediction.model_run_id)
            .where(ModelRun.market != target_prediction.market)
        )
        or 0
    )
    detail_orphans = int(
        db.scalar(
            select(func.count(target_detail.id))
            .outerjoin(
                target_prediction,
                target_prediction.id == target_detail.prediction_id,
            )
            .where(target_prediction.id.is_(None))
        )
        or 0
    )
    explanation_orphans = int(
        db.scalar(
            select(func.count(target_explanation.id))
            .outerjoin(
                target_prediction,
                target_prediction.id == target_explanation.prediction_id,
            )
            .where(target_prediction.id.is_(None))
        )
        or 0
    )
    market_hot_tables = {
        item: physical_hot_prediction_models(item)
        for item in sorted(physical_fact_write_markets())
    }

    inspector = inspect(db.get_bind())
    all_tables = tuple(
        table for tables in market_hot_tables.values() for table in tables
    )
    table_names = set(inspector.get_table_names())
    constraints = {
        table.__tablename__: sorted(
            str(item.get("name") or "")
            for item in inspector.get_check_constraints(table.__tablename__)
        )
        for table, _, _ in market_hot_tables.values()
    }
    foreign_keys = {
        table.__tablename__: [
            {
                "name": str(item.get("name") or ""),
                "referred_table": str(item.get("referred_table") or ""),
                "ondelete": str((item.get("options") or {}).get("ondelete") or ""),
            }
            for item in inspector.get_foreign_keys(table.__tablename__)
        ]
        for _, detail_table, explanation_table in market_hot_tables.values()
        for table in (detail_table, explanation_table)
    }
    child_cascades = all(
        any(item["ondelete"].upper() == "CASCADE" for item in values)
        for values in foreign_keys.values()
    )
    indexes = {
        table.__tablename__: sorted(
            str(item.get("name") or "")
            for item in inspector.get_indexes(table.__tablename__)
        )
        for table in all_tables
    }
    repository_rows = PredictionRepository(db).list_predictions_for_run(
        int(model_run_id),
        market=normalized_market,
        limit=3,
    )
    source_layer = (
        str(repository_rows[0].get("source_layer") or "")
        if repository_rows
        else ""
    )
    fact_constraints = inspect_market_fact_constraints(db.connection())
    hot_fact_tables = tuple(
        tables[0].__tablename__ for tables in market_hot_tables.values()
    )
    wrong_market = next(
        item for item in sorted(physical_fact_write_markets()) if item != normalized_market
    )
    checks = {
        "physical_tables_exist": {table.__tablename__ for table in all_tables}.issubset(
            table_names
        ),
        "source_exact_match": exact_match,
        "wrong_market_rows_zero": wrong_market_rows == 0,
        "symbol_market_mismatch_zero": symbol_mismatch == 0,
        "model_run_market_mismatch_zero": model_run_mismatch == 0,
        "child_orphans_zero": detail_orphans == 0 and explanation_orphans == 0,
        "constraint_rejects_wrong_market": _hot_constraint_rejects_wrong_market(
            db,
            target_prediction,
            wrong_market=wrong_market,
        ),
        "all_market_constraints_present": all(
            f"ck_{table.__tablename__}_market" in constraints[table.__tablename__]
            for table, _, _ in market_hot_tables.values()
        ),
        "child_foreign_keys_cascade": child_cascades,
        "repository_reads_market_physical_table": source_layer
        == target_prediction.__tablename__,
        "composite_symbol_market_fks_present": all(
            fact_constraints["tables"][name]["compliant"]
            for name in hot_fact_tables
        ),
    }
    return {
        "audit_version": "market-physical-hot-storage-v2",
        "generated_at": app_now_iso(),
        "status": "pass" if all(checks.values()) else "failed",
        "market": normalized_market,
        "model_run_id": int(model_run_id),
        "source_counts": source_counts,
        "physical_counts": physical_counts,
        "source_digests": source_digests,
        "physical_digests": physical_digests,
        "exact_match": exact_match,
        "wrong_market_rows": wrong_market_rows,
        "symbol_market_mismatch_rows": symbol_mismatch,
        "model_run_market_mismatch_rows": model_run_mismatch,
        "detail_orphans": detail_orphans,
        "explanation_orphans": explanation_orphans,
        "repository_source_layer": source_layer,
        "constraints": constraints,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
        "fact_constraints": {
            name: fact_constraints["tables"][name] for name in hot_fact_tables
        },
        "checks": checks,
    }
