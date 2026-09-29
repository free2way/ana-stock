from __future__ import annotations

import hashlib
import json

from sqlalchemy import desc, func, inspect, select, text
from sqlalchemy.orm import Session

from app.models.tables import (
    ModelRun,
    Prediction,
    PredictionDetail,
)
from app.services.market_storage_routing import physical_live_prediction_model
from app.services.repository import (
    LivePredictionRepository,
    PRODUCTION_SIGNAL_MODEL_TYPES,
)


PARITY_COLUMNS = (
    "symbol_id",
    "trade_date",
    "score",
    "rank_value",
    *LivePredictionRepository.DETAIL_FIELDS,
)


def _canonical_digest(rows: list[dict]) -> str:
    normalized = [
        {column: row.get(column) for column in PARITY_COLUMNS}
        for row in rows
    ]
    serialized = sorted(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        for row in normalized
    )
    return hashlib.sha256("\n".join(serialized).encode("utf-8")).hexdigest()


def _load_source_rows(db: Session, *, model_run_id: int, trade_date: str) -> list[dict]:
    detail_columns = [
        getattr(PredictionDetail, field)
        for field in LivePredictionRepository.DETAIL_FIELDS
    ]
    rows = db.execute(
        select(
            Prediction.symbol_id,
            Prediction.trade_date,
            Prediction.score,
            Prediction.rank_value,
            *detail_columns,
        )
        .outerjoin(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
        .where(
            Prediction.model_run_id == int(model_run_id),
            Prediction.trade_date == str(trade_date),
        )
        .order_by(desc(Prediction.score), Prediction.symbol_id.asc())
    ).mappings()
    return [dict(row) for row in rows]


def _load_live_rows(db: Session, *, market: str, model_run_id: int) -> list[dict]:
    table = physical_live_prediction_model(market)
    columns = [getattr(table, column) for column in PARITY_COLUMNS]
    rows = db.execute(
        select(*columns)
        .where(table.model_run_id == int(model_run_id))
        .order_by(desc(table.score), table.symbol_id.asc())
    ).mappings()
    return [dict(row) for row in rows]


def backfill_latest_live_predictions(
    db: Session,
    *,
    market: str = "CN",
    apply: bool = False,
) -> dict:
    normalized_market = str(market or "").strip().upper()
    if normalized_market not in {"CN", "US"}:
        raise ValueError(f"Unsupported market: {market!r}")

    latest_run_id = db.scalar(
        select(ModelRun.id)
        .where(
            ModelRun.status == "success",
            ModelRun.market == normalized_market,
            ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
            select(Prediction.id)
            .where(Prediction.model_run_id == ModelRun.id)
            .limit(1)
            .exists(),
        )
        .order_by(ModelRun.id.desc())
        .limit(1)
    )
    if latest_run_id is None:
        raise ValueError(
            f"No successful production run with predictions for {normalized_market}."
        )
    latest_trade_date = db.scalar(
        select(func.max(Prediction.trade_date)).where(
            Prediction.model_run_id == int(latest_run_id)
        )
    )
    if latest_trade_date is None:
        raise ValueError(f"Model run {latest_run_id} does not contain predictions.")

    source_rows = _load_source_rows(
        db,
        model_run_id=int(latest_run_id),
        trade_date=str(latest_trade_date),
    )
    source_digest = _canonical_digest(source_rows)
    result = {
        "status": "dry_run" if not apply else "pending",
        "market": normalized_market,
        "model_run_id": int(latest_run_id),
        "trade_date": str(latest_trade_date),
        "source_rows": len(source_rows),
        "source_sha256": source_digest,
        "applied": bool(apply),
    }
    if not apply:
        return result

    prediction_rows = [
        {
            "symbol_id": row["symbol_id"],
            "trade_date": row["trade_date"],
            "score": row["score"],
            "rank_value": row["rank_value"],
        }
        for row in source_rows
    ]
    detail_rows = [dict(row) for row in source_rows]
    live_repository = LivePredictionRepository(db)
    published_rows = live_repository.publish_for_model_run(
        model_run_id=int(latest_run_id),
        market=normalized_market,
        prediction_rows=prediction_rows,
        detail_rows=detail_rows,
    )
    physical_table = physical_live_prediction_model(normalized_market)
    live_rows = _load_live_rows(
        db,
        market=normalized_market,
        model_run_id=int(latest_run_id),
    )
    live_digest = _canonical_digest(live_rows)
    parity_passed = (
        published_rows == len(source_rows)
        and len(live_rows) == len(source_rows)
        and live_digest == source_digest
    )

    db.execute(text(f"ANALYZE {physical_table.__tablename__}"))
    db.commit()
    inspector = inspect(db.get_bind())
    column_types = {
        str(row["column_name"]): str(row["data_type"])
        for row in db.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table_name "
                "AND column_name IN ('trade_date', 'published_at')"
            ),
            {"table_name": physical_table.__tablename__},
        ).mappings()
    }
    index_names = sorted(
        str(index["name"])
        for index in inspector.get_indexes(physical_table.__tablename__)
    )
    explain_result = db.execute(
        text(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
            f"SELECT symbol_id, trade_date, score FROM {physical_table.__tablename__} "
            "WHERE market = :market AND model_run_id = :model_run_id "
            "ORDER BY score DESC LIMIT 50"
        ),
        {"market": normalized_market, "model_run_id": int(latest_run_id)},
    ).scalar_one()
    result.update(
        {
            "status": "pass" if parity_passed else "failed",
            "published_rows": int(published_rows),
            "publish_action": live_repository.last_publish_action,
            "publish_actions": live_repository.last_publish_actions,
            "physical_table": physical_table.__tablename__,
            "live_rows": len(live_rows),
            "live_sha256": live_digest,
            "exact_match": parity_passed,
            "column_types": column_types,
            "indexes": index_names,
            "explain_analyze": explain_result,
        }
    )
    if not parity_passed:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, default=str))
    return result
