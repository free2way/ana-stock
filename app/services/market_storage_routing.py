from __future__ import annotations

import json
from typing import TypeAlias

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import (
    AppSetting,
    CNFundamentalSnapshot,
    CNLivePrediction,
    CNModelChartSignal,
    CNPointInTimeFeature,
    CNPrediction,
    CNPredictionDetail,
    CNPredictionExplanation,
    CNPredictionTradePlan,
    CNTechnicalSnapshot,
    HKFundamentalSnapshot,
    HKLivePrediction,
    HKModelChartSignal,
    HKPointInTimeFeature,
    HKPrediction,
    HKPredictionDetail,
    HKPredictionExplanation,
    HKPredictionTradePlan,
    HKTechnicalSnapshot,
    USFundamentalSnapshot,
    USLivePrediction,
    USModelChartSignal,
    USPointInTimeFeature,
    USPrediction,
    USPredictionDetail,
    USPredictionExplanation,
    USPredictionTradePlan,
    USTechnicalSnapshot,
)


CN_PHYSICAL_CUTOVER_SETTING_KEY = "cn_physical_storage_cutover"
CN_PHYSICAL_CUTOVER_VERSION = "cn-physical-storage-cutover-v1"
MARKET_PHYSICAL_CUTOVER_VERSION = "market-physical-storage-cutover-v2"
REQUIRED_PHYSICAL_FACT_MARKETS = frozenset({"CN", "HK", "US"})


PhysicalLivePredictionModel: TypeAlias = (
    type[CNLivePrediction] | type[HKLivePrediction] | type[USLivePrediction]
)

PHYSICAL_LIVE_PREDICTION_TABLES: dict[str, PhysicalLivePredictionModel] = {
    "CN": CNLivePrediction,
    "HK": HKLivePrediction,
    "US": USLivePrediction,
}
PHYSICAL_HOT_PREDICTION_TABLES = {
    "CN": (CNPrediction, CNPredictionDetail, CNPredictionExplanation),
    "HK": (HKPrediction, HKPredictionDetail, HKPredictionExplanation),
    "US": (USPrediction, USPredictionDetail, USPredictionExplanation),
}
PHYSICAL_SNAPSHOT_TABLES = {
    "CN": (CNFundamentalSnapshot, CNPointInTimeFeature, CNTechnicalSnapshot),
    "HK": (HKFundamentalSnapshot, HKPointInTimeFeature, HKTechnicalSnapshot),
    "US": (USFundamentalSnapshot, USPointInTimeFeature, USTechnicalSnapshot),
}
PHYSICAL_MODEL_CHART_SIGNAL_TABLES = {
    "CN": CNModelChartSignal,
    "HK": HKModelChartSignal,
    "US": USModelChartSignal,
}
PHYSICAL_PREDICTION_TRADE_PLAN_TABLES = {
    "CN": CNPredictionTradePlan,
    "HK": HKPredictionTradePlan,
    "US": USPredictionTradePlan,
}


def normalize_fact_market(market: str | None) -> str:
    normalized = str(market or "").strip().upper()
    if normalized not in PHYSICAL_LIVE_PREDICTION_TABLES:
        raise ValueError(
            f"Market facts require an explicit CN, HK, or US market; received {market!r}."
        )
    return normalized


def enabled_physical_markets(raw: str | None) -> set[str]:
    values = {
        str(item).strip().upper()
        for item in str(raw or "").split(",")
        if str(item).strip()
    }
    invalid = values - set(PHYSICAL_LIVE_PREDICTION_TABLES)
    if invalid:
        raise ValueError(f"Unsupported physical-market configuration: {sorted(invalid)}")
    return values


def physical_fact_write_markets() -> frozenset[str]:
    """Markets whose facts must always have a market-specific physical target.

    Runtime rollout settings may control compatibility reads, but they cannot
    disable the primary CN/HK/US physical write required by the storage contract.
    """

    return REQUIRED_PHYSICAL_FACT_MARKETS


def physical_live_prediction_model(market: str | None) -> PhysicalLivePredictionModel:
    return PHYSICAL_LIVE_PREDICTION_TABLES[normalize_fact_market(market)]


def physical_hot_prediction_models(market: str | None):
    return PHYSICAL_HOT_PREDICTION_TABLES[normalize_fact_market(market)]


def physical_snapshot_models(market: str | None):
    return PHYSICAL_SNAPSHOT_TABLES[normalize_fact_market(market)]


def physical_model_chart_signal_model(market: str | None):
    return PHYSICAL_MODEL_CHART_SIGNAL_TABLES[normalize_fact_market(market)]


def physical_prediction_trade_plan_model(market: str | None):
    return PHYSICAL_PREDICTION_TRADE_PLAN_TABLES[normalize_fact_market(market)]


def physical_cutover_setting_key(market: str | None) -> str:
    return f"{normalize_fact_market(market).lower()}_physical_storage_cutover"


def market_physical_cutover_marker(
    db: Session,
    market: str | None,
) -> dict | None:
    """Load a structurally valid market-scoped cutover marker."""
    if not hasattr(db, "scalar"):
        return None
    normalized = normalize_fact_market(market)
    raw = db.scalar(
        select(AppSetting.value).where(
            AppSetting.key == physical_cutover_setting_key(normalized)
        )
    )
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    accepted_versions = {MARKET_PHYSICAL_CUTOVER_VERSION}
    if normalized == "CN":
        accepted_versions.add(CN_PHYSICAL_CUTOVER_VERSION)
    if payload.get("cutover_version") not in accepted_versions:
        return None
    if payload.get("market") != normalized:
        return None
    return payload


def cn_physical_cutover_marker(db: Session) -> dict | None:
    """Backward-compatible CN marker accessor."""

    return market_physical_cutover_marker(db, "CN")


def cn_physical_only_cutover_active(db: Session) -> bool:
    """Return true only for a gate-produced active CN cutover marker."""

    payload = cn_physical_cutover_marker(db)
    return bool(payload and payload.get("status") == "active")


def physical_only_cutover_active(db: Session, market: str | None) -> bool:
    """Return the market-scoped physical-only state.

    Cutover markers remain market-scoped.  A CN marker must never silently
    change HK or US compatibility behavior.
    """

    normalized = normalize_fact_market(market)
    if normalized == "CN":
        return cn_physical_only_cutover_active(db)
    payload = market_physical_cutover_marker(db, normalized)
    return bool(payload and payload.get("status") == "active")


def legacy_mirror_write_enabled(
    db: Session,
    *,
    market: str | None,
    configured: bool,
) -> bool:
    """Legacy shared tables are an optional migration mirror, never primary."""

    return bool(configured) and not physical_only_cutover_active(db, market)


def physical_fact_table_contract() -> dict[str, dict[str, str]]:
    """Expose the authoritative CN/HK/US physical table mapping for audits."""

    result: dict[str, dict[str, str]] = {}
    for market in sorted(REQUIRED_PHYSICAL_FACT_MARKETS):
        live = physical_live_prediction_model(market)
        hot = physical_hot_prediction_models(market)
        snapshots = physical_snapshot_models(market)
        chart_signals = physical_model_chart_signal_model(market)
        trade_plans = physical_prediction_trade_plan_model(market)
        result[market] = {
            "live_predictions": live.__tablename__,
            "predictions": hot[0].__tablename__,
            "prediction_details": hot[1].__tablename__,
            "prediction_explanations": hot[2].__tablename__,
            "fundamental_snapshots": snapshots[0].__tablename__,
            "point_in_time_features": snapshots[1].__tablename__,
            "technical_snapshots": snapshots[2].__tablename__,
            "model_chart_signals": chart_signals.__tablename__,
            "prediction_trade_plans": trade_plans.__tablename__,
        }
    return result
