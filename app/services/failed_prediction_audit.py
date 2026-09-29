from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.tables import (
    LivePrediction,
    ModelChartSignal,
    ModelRun,
    Prediction,
    PredictionDetail,
    PredictionExplanation,
    PredictionTradePlan,
)
from app.services.market_storage_routing import (
    physical_hot_prediction_models,
    physical_live_prediction_model,
    physical_model_chart_signal_model,
    physical_prediction_trade_plan_model,
)
from app.services.time_utils import app_now_iso


def _counts_by_run(db: Session, statement) -> dict[int, int]:
    return {
        int(run_id): int(row_count or 0)
        for run_id, row_count in db.execute(statement).all()
    }


def audit_failed_prediction_rows(db: Session, *, market: str = "CN") -> dict:
    """Prove failed runs cannot retain legacy, physical-hot, or live rows."""

    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError(f"Failed-run prediction audit does not support {market!r}.")
    failed_run_ids = [
        int(item)
        for item in db.scalars(
            select(ModelRun.id)
            .where(ModelRun.market == market_code, ModelRun.status == "failed")
            .order_by(ModelRun.id.asc())
        ).all()
    ]
    if not failed_run_ids:
        return {
            "audit_version": "failed-prediction-rows-v1",
            "generated_at": app_now_iso(),
            "status": "pass",
            "market": market_code,
            "failed_run_count": 0,
            "failed_run_ids": [],
            "runs": [],
            "totals": {},
            "database_mutated": False,
        }

    physical_prediction, physical_detail, physical_explanation = (
        physical_hot_prediction_models(market_code)
    )
    physical_live = physical_live_prediction_model(market_code)
    physical_chart = physical_model_chart_signal_model(market_code)
    physical_trade_plan = physical_prediction_trade_plan_model(market_code)
    market_prefix = market_code.lower()
    counts = {
        "legacy_predictions": _counts_by_run(
            db,
            select(Prediction.model_run_id, func.count(Prediction.id))
            .where(Prediction.model_run_id.in_(failed_run_ids))
            .group_by(Prediction.model_run_id),
        ),
        "legacy_prediction_details": _counts_by_run(
            db,
            select(Prediction.model_run_id, func.count(PredictionDetail.id))
            .join(PredictionDetail, PredictionDetail.prediction_id == Prediction.id)
            .where(Prediction.model_run_id.in_(failed_run_ids))
            .group_by(Prediction.model_run_id),
        ),
        "legacy_prediction_explanations": _counts_by_run(
            db,
            select(Prediction.model_run_id, func.count(PredictionExplanation.id))
            .join(
                PredictionExplanation,
                PredictionExplanation.prediction_id == Prediction.id,
            )
            .where(Prediction.model_run_id.in_(failed_run_ids))
            .group_by(Prediction.model_run_id),
        ),
        "legacy_live_predictions": _counts_by_run(
            db,
            select(LivePrediction.model_run_id, func.count(LivePrediction.id))
            .where(LivePrediction.model_run_id.in_(failed_run_ids))
            .group_by(LivePrediction.model_run_id),
        ),
        "legacy_model_chart_signals": _counts_by_run(
            db,
            select(ModelChartSignal.model_run_id, func.count(ModelChartSignal.id))
            .where(ModelChartSignal.model_run_id.in_(failed_run_ids))
            .group_by(ModelChartSignal.model_run_id),
        ),
        "legacy_prediction_trade_plans": _counts_by_run(
            db,
            select(Prediction.model_run_id, func.count(PredictionTradePlan.id))
            .join(
                PredictionTradePlan,
                PredictionTradePlan.prediction_id == Prediction.id,
            )
            .where(Prediction.model_run_id.in_(failed_run_ids))
            .group_by(Prediction.model_run_id),
        ),
        f"{market_prefix}_live_predictions": _counts_by_run(
            db,
            select(physical_live.model_run_id, func.count(physical_live.id))
            .where(physical_live.model_run_id.in_(failed_run_ids))
            .group_by(physical_live.model_run_id),
        ),
        f"{market_prefix}_predictions": _counts_by_run(
            db,
            select(physical_prediction.model_run_id, func.count(physical_prediction.id))
            .where(physical_prediction.model_run_id.in_(failed_run_ids))
            .group_by(physical_prediction.model_run_id),
        ),
        f"{market_prefix}_model_chart_signals": _counts_by_run(
            db,
            select(
                physical_chart.model_run_id,
                func.count(physical_chart.id),
            )
            .where(physical_chart.model_run_id.in_(failed_run_ids))
            .group_by(physical_chart.model_run_id),
        ),
        f"{market_prefix}_prediction_details": _counts_by_run(
            db,
            select(physical_prediction.model_run_id, func.count(physical_detail.id))
            .join(
                physical_detail,
                physical_detail.prediction_id == physical_prediction.id,
            )
            .where(physical_prediction.model_run_id.in_(failed_run_ids))
            .group_by(physical_prediction.model_run_id),
        ),
        f"{market_prefix}_prediction_explanations": _counts_by_run(
            db,
            select(
                physical_prediction.model_run_id,
                func.count(physical_explanation.id),
            )
            .join(
                physical_explanation,
                physical_explanation.prediction_id == physical_prediction.id,
            )
            .where(physical_prediction.model_run_id.in_(failed_run_ids))
            .group_by(physical_prediction.model_run_id),
        ),
        f"{market_prefix}_prediction_trade_plans": _counts_by_run(
            db,
            select(
                physical_prediction.model_run_id,
                func.count(physical_trade_plan.id),
            )
            .join(
                physical_trade_plan,
                physical_trade_plan.prediction_id == physical_prediction.id,
            )
            .where(physical_prediction.model_run_id.in_(failed_run_ids))
            .group_by(physical_prediction.model_run_id),
        ),
    }
    runs = []
    for run_id in failed_run_ids:
        table_counts = {
            name: int(values.get(run_id, 0)) for name, values in counts.items()
        }
        runs.append(
            {
                "model_run_id": run_id,
                "status": "pass" if not any(table_counts.values()) else "fail",
                "table_counts": table_counts,
            }
        )
    totals = {
        name: sum(int(values.get(run_id, 0)) for run_id in failed_run_ids)
        for name, values in counts.items()
    }
    return {
        "audit_version": "failed-prediction-rows-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if not any(totals.values()) else "fail",
        "market": market_code,
        "failed_run_count": len(failed_run_ids),
        "failed_run_ids": failed_run_ids,
        "runs": runs,
        "totals": totals,
        "database_mutated": False,
    }
