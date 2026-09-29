from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import func, inspect, select, text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.tables import ModelRun, Prediction  # noqa: E402
from app.services.market_hot_predictions import (  # noqa: E402
    MarketHotPredictionRepository,
    _prepare_rows,
)
from app.services.market_storage_routing import physical_hot_prediction_models  # noqa: E402
from app.services.prediction_archive_migration import (  # noqa: E402
    _load_detail_rows,
    _load_explanation_rows,
    _load_prediction_rows,
)
from app.services.repository import PRODUCTION_SIGNAL_MODEL_TYPES  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill compact prediction outputs into physical market tables."
    )
    parser.add_argument("--market", choices=("CN", "US"), default="CN")
    parser.add_argument("--model-run-id", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    init_db()
    with SessionLocal() as db:
        model_run_id = args.model_run_id
        if model_run_id is None:
            model_run_id = db.scalar(
                select(ModelRun.id)
                .where(
                    ModelRun.status == "success",
                    ModelRun.market == args.market,
                    ModelRun.model_type.in_(PRODUCTION_SIGNAL_MODEL_TYPES),
                    select(Prediction.id)
                    .where(Prediction.model_run_id == ModelRun.id)
                    .limit(1)
                    .exists(),
                )
                .order_by(ModelRun.id.desc())
                .limit(1)
            )
        if model_run_id is None:
            raise RuntimeError(f"No compact production predictions found for {args.market}.")
        prediction_rows = _load_prediction_rows(db, int(model_run_id))
        detail_rows = _load_detail_rows(db, int(model_run_id))
        explanation_rows = _load_explanation_rows(db, int(model_run_id))
        prepared = _prepare_rows(prediction_rows, detail_rows, explanation_rows)
        source_counts = {
            "predictions": len(prepared[0]),
            "prediction_details": len(prepared[1]),
            "prediction_explanations": len(prepared[2]),
        }
        source_digests = MarketHotPredictionRepository._digests(*prepared)
        if not args.apply:
            result = {
                "status": "dry_run",
                "market": args.market,
                "model_run_id": int(model_run_id),
                "source_counts": source_counts,
                "source_digests": source_digests,
                "applied": False,
            }
        else:
            result = MarketHotPredictionRepository(db).publish_for_model_run(
                model_run_id=int(model_run_id),
                market=args.market,
                prediction_rows=prediction_rows,
                detail_rows=detail_rows,
                explanation_rows=explanation_rows,
            )
            prediction_table, detail_table, explanation_table = physical_hot_prediction_models(
                args.market
            )
            for table in (prediction_table, detail_table, explanation_table):
                db.execute(text(f"ANALYZE {table.__tablename__}"))
            db.commit()
            inspector = inspect(db.get_bind())
            result.update(
                {
                    "source_counts": source_counts,
                    "source_digests": source_digests,
                    "column_types": {
                        str(row["column_name"]): str(row["data_type"])
                        for row in db.execute(
                            text(
                                "SELECT column_name, data_type "
                                "FROM information_schema.columns "
                                "WHERE table_schema = current_schema() "
                                "AND table_name = :table_name "
                                "AND column_name IN ('trade_date', 'created_at')"
                            ),
                            {"table_name": prediction_table.__tablename__},
                        ).mappings()
                    },
                    "indexes": {
                        table.__tablename__: sorted(
                            str(item.get("name") or "")
                            for item in inspector.get_indexes(table.__tablename__)
                        )
                        for table in (prediction_table, detail_table, explanation_table)
                    },
                    "wrong_market_rows": int(
                        db.scalar(
                            select(func.count(prediction_table.id)).where(
                                prediction_table.market != args.market
                            )
                        )
                        or 0
                    ),
                    "applied": True,
                }
            )
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.receipt is not None:
        receipt_path = args.receipt.resolve()
        if receipt_path.exists():
            raise FileExistsError(f"Receipt already exists: {receipt_path}")
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = receipt_path.with_name(
            f".{receipt_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary_path.write_text(serialized + "\n", encoding="utf-8")
            os.replace(temporary_path, receipt_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    print(serialized)


if __name__ == "__main__":
    main()
