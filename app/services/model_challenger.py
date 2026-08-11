"""Gate XGBoost/CatBoost challenger runs on strict OOS evidence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import ModelEvaluation, ModelRun
from app.services.model_evaluation import STRICT_OOS_MIN_COVERAGE_DAYS, STRICT_OOS_MIN_SAMPLES


CHALLENGER_MODEL_TYPES = ("xgboost", "catboost")


def challenger_race_readiness(db: Session, *, markets: list[str] | None = None) -> dict:
    """Assess whether each market has earned an expensive challenger race.

    The baseline must have a successful strict-OOS evaluation for both sample
    count and coverage. A new model is never trained merely because a calendar
    date passed or because an in-sample score looked attractive.
    """
    target_markets = [str(item).upper() for item in (markets or ["CN", "US"]) if str(item).upper() in {"CN", "US"}]
    target_markets = list(dict.fromkeys(target_markets)) or ["CN", "US"]
    market_rows: dict[str, dict] = {}
    for market in target_markets:
        row = db.execute(
            select(ModelEvaluation, ModelRun)
            .join(ModelRun, ModelRun.id == ModelEvaluation.model_run_id)
            .where(ModelEvaluation.market == market)
            .where(ModelRun.model_type == "lightgbm_multifactor")
            .where(ModelEvaluation.status == "success")
            .order_by(ModelEvaluation.id.desc())
            .limit(1)
        ).first()
        if row is None:
            market_rows[market] = {
                "market": market,
                "ready": False,
                "reason": "missing_strict_oos_baseline_evaluation",
                "oos_sample_count": 0,
                "oos_coverage_days": 0,
            }
            continue
        evaluation, run = row
        sample_count = int(evaluation.oos_sample_count or 0)
        coverage_days = int(evaluation.oos_coverage_days or 0)
        ready = sample_count >= STRICT_OOS_MIN_SAMPLES and coverage_days >= STRICT_OOS_MIN_COVERAGE_DAYS
        missing_samples = max(0, STRICT_OOS_MIN_SAMPLES - sample_count)
        missing_days = max(0, STRICT_OOS_MIN_COVERAGE_DAYS - coverage_days)
        market_rows[market] = {
            "market": market,
            "ready": ready,
            "reason": "ready" if ready else "strict_oos_evidence_insufficient",
            "baseline_model_run_id": run.id,
            "baseline_evaluation_id": evaluation.id,
            "oos_sample_count": sample_count,
            "oos_coverage_days": coverage_days,
            "missing_oos_samples": missing_samples,
            "missing_oos_days": missing_days,
        }
    return {
        "markets": market_rows,
        "required_oos_samples": STRICT_OOS_MIN_SAMPLES,
        "required_oos_coverage_days": STRICT_OOS_MIN_COVERAGE_DAYS,
        "ready_markets": [market for market, row in market_rows.items() if row["ready"]],
        "waiting_markets": [market for market, row in market_rows.items() if not row["ready"]],
        "status": "ready" if market_rows and all(row["ready"] for row in market_rows.values()) else "waiting_for_oos",
    }
