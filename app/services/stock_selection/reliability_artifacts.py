"""Production producers/loaders for the reliability-weighted confluence layer.

``selective_calibration`` already ships the pure producers
(:func:`build_rolling_oos_reliability`, :func:`build_probability_calibration_artifact`),
row injection (:func:`attach_oos_reliability_metadata`) and atomic
artifact persistence.  This module is the *call site* layer that was missing:

1. :func:`collect_matured_rows` reads matured, point-in-time model evaluation
   outcomes (already OOS-gated and cost-netted by
   ``evaluate_model_runs(require_execution_reconciliation=True)``) and joins them
   to the persisted prediction scores.
2. :func:`refresh_stock_selection_reliability_artifacts` writes the rolling OOS
   reliability metadata and probability-calibration artifacts to the agreed
   artifact directories.  The schedulers call it right after structured
   evaluation; failures there only warn and never block the main flow.
3. :func:`load_latest_reliability_metadata` /
   :func:`load_latest_calibration_artifact` /
   :func:`attach_reliability_metadata` let the screener/fusion call sites load
   the newest artifact and inject it, keeping legacy behaviour (equal weights,
   ``expected_hit_probability=None``) whenever an artifact is missing.

Key mapping
-----------
The fusion layer keys rows by ``MODEL_TEMPLATES`` template key, while model runs
are identified by ``ModelRun``/``model_version``.  :func:`resolve_model_template_key`
derives the template key from the run's ``model_type``/config.  When the run does
not map unambiguously to a template the model version itself becomes the
artifact key (logged + recorded in a sidecar audit file) so nothing is silently
mis-attributed.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import ModelEvaluation, ModelRun, Prediction, PredictionArtifact, Symbol
from app.services.prediction_artifacts import read_prediction_artifact_rows
from app.services.stock_selection.factor_baseline import BaselinePrediction
from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.selective_calibration import (
    OOS_METRIC_HIT_RATE,
    CalibrationArtifact,
    OosReliabilityMetadata,
    attach_oos_reliability_metadata,
    build_probability_calibration_artifact,
    build_rolling_oos_reliability,
    load_calibration_artifact,
    load_oos_reliability_metadata,
    write_calibration_artifact,
    write_oos_reliability_metadata,
)

logger = logging.getLogger(__name__)

RELIABILITY_ARTIFACT_DIRNAME = "reliability"
CALIBRATION_ARTIFACT_DIRNAME = "calibration"
OOS_RELIABILITY_ARTIFACT_FILENAME = "oos_reliability_latest.json"
PROBABILITY_CALIBRATION_ARTIFACT_FILENAME = "probability_calibration_latest.json"
MODEL_KEY_RESOLUTION_FILENAME = "model_key_resolution_latest.json"

# Reconciled evaluation outcome rows carry a real cost-net return; nothing is
# subtracted again, so the recorded flat bps stays zero and the definition is
# still "net of cost".
RECONCILED_LABEL_COST_BPS = 0.0

DEFAULT_LOOKBACK_DATES = 40
DEFAULT_MINIMUM_OBSERVATIONS = 30
DEFAULT_CALIBRATION_BINS = 5
# How many of the newest success runs may be scanned per market while looking
# for the newest one that actually carries a ModelEvaluation.  Ablation /
# experiment runs are written as ``success`` without an evaluation and would
# otherwise shadow the scheduled run.
DEFAULT_RUN_SCAN_LIMIT = 50

# Score-join source layers, ordered from lowest to highest precedence.  When the
# same (ticker, trade_date) key exists in more than one layer the higher layer
# wins; the winning layer is recorded in the audit payload.
SCORE_JOIN_LAYERS = ("cold_artifact", "legacy_table", "physical_table")
DEFAULT_CALIBRATION_FIT_SCORE_FIELD = "raw_score"
# The screener/fusion rows expose the model score under this field name
# (``_CALIBRATION_SCORE_FALLBACK_FIELDS`` in ``multi_model_confluence``).
DEFAULT_CALIBRATION_SCORE_FIELD = "model_score"

# model_type family -> ``MODEL_TEMPLATES`` keys that serve those model signals.
# Only the LightGBM momentum run feeds a screener template today; rule-based and
# fundamental templates have no model version and therefore no OOS reliability.
MODEL_TEMPLATE_MODEL_TYPES: dict[str, tuple[str, ...]] = {
    "lightgbm": ("lightgbm_top_picks",),
}


def _model_family(model_type: object) -> str:
    text = str(model_type or "").strip().lower()
    if text.endswith("_multifactor"):
        text = text[: -len("_multifactor")]
    return text


def _run_config(run: ModelRun | object) -> dict:
    raw = getattr(run, "config_json", None)
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_model_template_key(
    *,
    model_type: object,
    market: object = None,
    config: Mapping[str, object] | None = None,
) -> tuple[str | None, str]:
    """Resolve the ``MODEL_TEMPLATES`` key a model run feeds, if unambiguous.

    Returns ``(template_key_or_None, note)``.  ``None`` means "fall back to the
    model version as the artifact key" and ``note`` records why so the decision
    is auditable.
    """

    config = dict(config or {})
    family = _model_family(config.get("model_type") or model_type)
    signal_type = str(config.get("signal_type") or "momentum").strip().lower()
    market_code = str(market or "").strip().upper()
    if family not in MODEL_TEMPLATE_MODEL_TYPES:
        return None, f"unmapped_model_type:{family or 'unknown'}"
    if signal_type not in {"", "momentum"}:
        return None, f"unmapped_signal_type:{signal_type}"
    # Import lazily: ``app.services.screener`` imports the stock_selection
    # package, so a module-level import here would create a cycle.
    from app.services.screener import MODEL_TEMPLATES

    candidates = [
        key
        for key in MODEL_TEMPLATE_MODEL_TYPES[family]
        if key in MODEL_TEMPLATES
        and (not market_code or str(MODEL_TEMPLATES[key].get("market") or "ALL") in {"ALL", market_code})
    ]
    if len(candidates) == 1:
        return candidates[0], f"resolved:{family}->{candidates[0]}"
    if not candidates:
        return None, f"no_template_for:{family}:{market_code or 'ALL'}"
    return None, "ambiguous_template:" + ",".join(sorted(candidates))


def run_model_version(run: ModelRun | object) -> str:
    """Stable, collision-free version key recorded in the artifact."""

    run_id = getattr(run, "id", None)
    name = str(getattr(run, "name", "") or "").strip()
    if run_id is not None and name:
        return f"model_run:{int(run_id)}:{name}"
    if run_id is not None:
        return f"model_run:{int(run_id)}"
    return name or "model_run:unknown"


def resolve_run_horizon(run: ModelRun | object, *, default: int = 5) -> int:
    config = _run_config(run)
    try:
        horizon = int(config.get("prediction_horizon_days"))
    except (TypeError, ValueError):
        return int(default)
    return horizon if horizon >= 1 else int(default)


@dataclass(frozen=True, slots=True)
class MaturedReliabilityRow:
    """One matured, out-of-sample (prediction, label) pair from an evaluation."""

    model_version: str
    ticker: str
    feature_date: date
    label_available_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    net_return: float

    def sample_id(self) -> str:
        return f"{self.feature_date.isoformat()}:{self.model_version}:{self.ticker}"


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _iso_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def matured_rows_from_candidate_outcomes(
    outcomes: Sequence[Mapping[str, object]],
    *,
    model_version: str,
    prediction_index: Mapping[tuple[str, str], tuple[float, float]],
    horizon_days: int | None = None,
) -> list[MaturedReliabilityRow]:
    """Keep only CLOSED, strict-OOS, fully-specified outcomes with a matched score.

    ``prediction_index`` maps ``(ticker, trade_date)`` to
    ``(raw_score, cross_sectional_rank)``.  Anything incomplete is dropped, never
    guessed, so the rolling window stays point-in-time valid.
    """

    rows: list[MaturedReliabilityRow] = []
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        if str(outcome.get("status") or "").upper() != "CLOSED":
            continue
        if not bool(outcome.get("is_out_of_sample")):
            continue
        horizon = outcome.get("horizon_days")
        try:
            horizon_int = int(horizon)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if horizon_days is not None and horizon_int != int(horizon_days):
            continue
        ticker = str(outcome.get("ticker") or "").strip().upper()
        trade_date = str(outcome.get("trade_date") or "").strip()
        feature_date = _iso_date(trade_date)
        label_available_date = _iso_date(outcome.get("label_available_date") or outcome.get("exit_date"))
        net_return = _number(outcome.get("net_return"))
        if not ticker or feature_date is None or label_available_date is None:
            continue
        if net_return is None or label_available_date < feature_date:
            continue
        matched = prediction_index.get((ticker, trade_date))
        if matched is None:
            continue
        raw_score, cross_sectional_rank = matched
        rows.append(
            MaturedReliabilityRow(
                model_version=str(model_version),
                ticker=ticker,
                feature_date=feature_date,
                label_available_date=label_available_date,
                horizon_days=horizon_int,
                raw_score=float(raw_score),
                cross_sectional_rank=float(cross_sectional_rank),
                net_return=float(net_return),
            )
        )
    return rows


def producer_inputs(
    rows: Sequence[MaturedReliabilityRow],
) -> tuple[list[BaselinePrediction], list[FactorScore]]:
    """Adapt matured rows into the producer's prediction/label contracts."""

    predictions: list[BaselinePrediction] = []
    labels: list[FactorScore] = []
    for row in rows:
        sample_id = row.sample_id()
        predictions.append(
            BaselinePrediction(
                sample_id=sample_id,
                ticker=row.ticker,
                feature_date=row.feature_date,
                horizon_days=row.horizon_days,
                raw_score=row.raw_score,
                cross_sectional_rank=row.cross_sectional_rank,
                model_version=row.model_version,
            )
        )
        labels.append(
            FactorScore(
                sample_id=sample_id,
                ticker=row.ticker,
                feature_date=row.feature_date,
                label_available_date=row.label_available_date,
                horizon_days=row.horizon_days,
                factor_values={},
                missing_factors=(),
                composite_score=row.raw_score,
                cross_sectional_rank=row.cross_sectional_rank,
                # ``net_return`` is already cost-netted by the reconciliation
                # ledger, so the producers must not subtract a cost again.
                label_value=row.net_return,
            )
        )
    return predictions, labels


def _trade_date_key(value: object) -> str:
    """Normalize legacy TEXT / physical DATE / artifact STRING trade dates."""

    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()[:10]


def _index_row(
    index: dict[tuple[str, str], tuple[float, float]],
    origin: dict[tuple[str, str], str],
    *,
    ticker: object,
    trade_date: object,
    score: object,
    rank_value: object,
    layer: str,
) -> bool:
    """Insert one scored row into the merge index, tagging its source layer."""

    ticker_code = str(ticker or "").strip().upper()
    trade_date_code = _trade_date_key(trade_date)
    numeric_score = _number(score)
    if not ticker_code or not trade_date_code or numeric_score is None:
        return False
    numeric_rank = _number(rank_value)
    key = (ticker_code, trade_date_code)
    index[key] = (
        numeric_score,
        numeric_rank if numeric_rank is not None else numeric_score,
    )
    origin[key] = layer
    return True


def _legacy_prediction_index(
    db: Session, *, model_run_id: int, market: str, trade_dates: Sequence[str]
) -> dict[tuple[str, str], tuple[float, float]]:
    rows = db.execute(
        select(Symbol.ticker, Prediction.trade_date, Prediction.score, Prediction.rank_value)
        .join(Symbol, Symbol.id == Prediction.symbol_id)
        .where(
            Prediction.model_run_id == int(model_run_id),
            Symbol.market == market,
            Prediction.trade_date.in_(sorted(set(trade_dates))),
        )
    ).all()
    index: dict[tuple[str, str], tuple[float, float]] = {}
    origin: dict[tuple[str, str], str] = {}
    for ticker, trade_date, score, rank_value in rows:
        _index_row(
            index,
            origin,
            ticker=ticker,
            trade_date=trade_date,
            score=score,
            rank_value=rank_value,
            layer="legacy_table",
        )
    return index


def _physical_prediction_index(
    db: Session, *, model_run_id: int, market: str, trade_dates: Sequence[str]
) -> tuple[dict[tuple[str, str], tuple[float, float]], str | None]:
    """Read the market-specific physical prediction table (``us_predictions`` ...).

    Returns ``(index, table_name)``.  ``table_name`` is ``None`` when the market
    has no physical table (or the caller is not a real SQLAlchemy session, as in
    lightweight unit-test doubles).
    """

    if not isinstance(db, Session):
        return {}, None
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US", "HK"}:
        return {}, None
    # Import lazily so importing this module stays cheap and cycle-free.
    from app.services.market_storage_routing import physical_hot_prediction_models

    prediction_table, _, _ = physical_hot_prediction_models(market_code)
    date_values = sorted({parsed for parsed in (_iso_date(item) for item in trade_dates) if parsed})
    if not date_values:
        return {}, prediction_table.__tablename__
    rows = db.execute(
        select(
            Symbol.ticker,
            prediction_table.trade_date,
            prediction_table.score,
            prediction_table.rank_value,
        )
        .join(Symbol, Symbol.id == prediction_table.symbol_id)
        .where(
            prediction_table.model_run_id == int(model_run_id),
            prediction_table.market == market_code,
            Symbol.market == market_code,
            prediction_table.trade_date.in_(date_values),
        )
    ).all()
    index: dict[tuple[str, str], tuple[float, float]] = {}
    origin: dict[tuple[str, str], str] = {}
    for ticker, trade_date, score, rank_value in rows:
        _index_row(
            index,
            origin,
            ticker=ticker,
            trade_date=trade_date,
            score=score,
            rank_value=rank_value,
            layer="physical_table",
        )
    return index, prediction_table.__tablename__


def _cold_prediction_index(
    db: Session, *, model_run_id: int, market: str, trade_dates: Sequence[str]
) -> dict[tuple[str, str], tuple[float, float]]:
    """Read a run's verified Parquet prediction artifact (cold storage)."""

    if not isinstance(db, Session):
        return {}
    artifact = db.scalar(
        select(PredictionArtifact).where(
            PredictionArtifact.model_run_id == int(model_run_id),
            PredictionArtifact.status == "verified",
        )
    )
    if artifact is None:
        return {}
    normalized_dates = sorted({_trade_date_key(item) for item in trade_dates if _trade_date_key(item)})
    try:
        rows = read_prediction_artifact_rows(
            artifact.artifact_path,
            trade_dates=normalized_dates,
            include_details=False,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError, json.JSONDecodeError):
        return {}
    if not rows:
        return {}
    market_code = str(market or "").strip().upper()
    symbol_ids = sorted({int(row["symbol_id"]) for row in rows if row.get("symbol_id") is not None})
    symbol_rows = list(db.scalars(select(Symbol).where(Symbol.id.in_(symbol_ids))).all())
    symbols = {int(symbol.id): symbol for symbol in symbol_rows}
    index: dict[tuple[str, str], tuple[float, float]] = {}
    origin: dict[tuple[str, str], str] = {}
    for row in rows:
        symbol = symbols.get(int(row["symbol_id"]))
        if symbol is None:
            continue
        if market_code and str(symbol.market or "").strip().upper() != market_code:
            continue
        _index_row(
            index,
            origin,
            ticker=symbol.ticker,
            trade_date=row.get("trade_date"),
            score=row.get("score"),
            rank_value=row.get("rank_value"),
            layer="cold_artifact",
        )
    return index


def _prediction_index(
    db: Session, *, model_run_id: int, market: str, trade_dates: Sequence[str]
) -> tuple[dict[tuple[str, str], tuple[float, float]], dict[str, object]]:
    """Join evaluation outcomes to persisted scores across all storage layers.

    Layers are merged lowest-to-highest precedence
    (``cold_artifact < legacy_table < physical_table``); a key present in more
    than one layer keeps the highest-precedence score.  Returns the merged index
    plus an auditable source summary.
    """

    if not trade_dates:
        return {}, {
            "physical_table": None,
            "physical_rows": 0,
            "legacy_rows": 0,
            "cold_artifact_rows": 0,
            "merged_score_keys": 0,
            "origin_counts": {},
            "priority": ">".join(reversed(SCORE_JOIN_LAYERS)),
        }
    merged: dict[tuple[str, str], tuple[float, float]] = {}
    origin: dict[tuple[str, str], str] = {}
    layer_rows: dict[str, int] = {}
    layers = {
        "cold_artifact": _cold_prediction_index(
            db, model_run_id=model_run_id, market=market, trade_dates=trade_dates
        ),
        "legacy_table": _legacy_prediction_index(
            db, model_run_id=model_run_id, market=market, trade_dates=trade_dates
        ),
    }
    physical_index, physical_table = _physical_prediction_index(
        db, model_run_id=model_run_id, market=market, trade_dates=trade_dates
    )
    layers["physical_table"] = physical_index
    for layer in SCORE_JOIN_LAYERS:  # ascending precedence
        layer_index = layers[layer]
        layer_rows[layer] = len(layer_index)
        for key, value in layer_index.items():
            merged[key] = value
            origin[key] = layer
    origin_counts: dict[str, int] = {}
    for layer in origin.values():
        origin_counts[layer] = origin_counts.get(layer, 0) + 1
    audit: dict[str, object] = {
        "physical_table": physical_table,
        "physical_rows": layer_rows.get("physical_table", 0),
        "legacy_rows": layer_rows.get("legacy_table", 0),
        "cold_artifact_rows": layer_rows.get("cold_artifact", 0),
        "merged_score_keys": len(merged),
        "origin_counts": origin_counts,
        "priority": ">".join(reversed(SCORE_JOIN_LAYERS)),
    }
    return merged, audit


def _latest_evaluation(db: Session, *, model_run_id: int, market: str) -> ModelEvaluation | None:
    return db.scalar(
        select(ModelEvaluation)
        .where(
            ModelEvaluation.model_run_id == int(model_run_id),
            ModelEvaluation.market == market,
            ModelEvaluation.status.in_(("success", "partial")),
        )
        .order_by(ModelEvaluation.id.desc())
        .limit(1)
    )


@dataclass(frozen=True, slots=True)
class _SelectedRun:
    run: ModelRun
    evaluation: ModelEvaluation


def _select_runs_for_market(
    db: Session,
    *,
    market: str,
    recent_runs: int,
    run_ids: Sequence[int] | None,
    run_scan_limit: int,
) -> tuple[list[_SelectedRun], dict[str, object]]:
    """Pick the newest success run per market that actually has an evaluation.

    A success run written by an ablation/experiment script has no
    ``ModelEvaluation``; selecting it would silently shadow the scheduled run
    whose evaluation carries the matured outcomes.  Evaluated runs are selected
    directly with an ``EXISTS`` filter (so no amount of unevaluated runs can push
    the scheduled run out of a scan window); the unevaluated runs newer than the
    newest selected run are recorded as ``skipped`` for audit.  An explicit
    ``run_ids`` list overrides the scan for schedulers that must pin exact runs.
    """

    explicit_ids = list(dict.fromkeys(int(item) for item in (run_ids or [])))
    audit: dict[str, object] = {
        "market": market,
        "selection_basis": "explicit_run_ids" if explicit_ids else "newest_evaluated_success_run",
        "recent_runs": max(1, int(recent_runs)),
        "scan_limit": max(1, int(run_scan_limit)),
        "requested_run_ids": explicit_ids,
        "selected": [],
        "skipped": [],
        "scanned_run_count": 0,
    }

    def _has_evaluation():
        return (
            select(ModelEvaluation.id)
            .where(
                ModelEvaluation.model_run_id == ModelRun.id,
                ModelEvaluation.market == market,
                ModelEvaluation.status.in_(("success", "partial")),
            )
            .exists()
        )

    if explicit_ids:
        candidates = list(
            db.scalars(
                select(ModelRun)
                .where(ModelRun.id.in_(explicit_ids), ModelRun.market.in_([market, "MIXED"]))
                .order_by(ModelRun.id.desc())
            ).all()
        )
        found_ids = {int(getattr(run, "id")) for run in candidates}
        for missing in sorted(set(explicit_ids) - found_ids, reverse=True):
            audit["skipped"].append({"run_id": missing, "reason": "run_not_found_for_market"})
    else:
        candidates = list(
            db.scalars(
                select(ModelRun)
                .where(
                    ModelRun.status == "success",
                    ModelRun.market.in_([market, "MIXED"]),
                    _has_evaluation(),
                )
                .order_by(ModelRun.id.desc())
                .limit(max(1, int(recent_runs)))
            ).all()
        )
        newest_selected_id = int(getattr(candidates[0], "id")) if candidates else None
        skip_stmt = (
            select(ModelRun)
            .where(
                ModelRun.status == "success",
                ModelRun.market.in_([market, "MIXED"]),
                ~_has_evaluation(),
            )
            .order_by(ModelRun.id.desc())
            .limit(int(audit["scan_limit"]))
        )
        if newest_selected_id is not None:
            skip_stmt = skip_stmt.where(ModelRun.id > newest_selected_id)
        for run in db.scalars(skip_stmt).all():
            run_id = int(getattr(run, "id"))
            audit["skipped"].append(
                {
                    "run_id": run_id,
                    "reason": "no_evaluation",
                    "run_status": str(getattr(run, "status") or ""),
                    "run_name": str(getattr(run, "name") or ""),
                }
            )
            logger.warning(
                "rolling OOS reliability: skipping run %s for market %s because it "
                "has no ModelEvaluation; an unevaluated success run must not shadow "
                "the newest evaluated run",
                run_id,
                market,
            )

    selected: list[_SelectedRun] = []
    max_select = len(candidates) if explicit_ids else max(1, int(recent_runs))
    for run in candidates:
        audit["scanned_run_count"] = int(audit["scanned_run_count"]) + 1
        run_id = int(getattr(run, "id"))
        evaluation = _latest_evaluation(db, model_run_id=run_id, market=market)
        if evaluation is None:
            audit["skipped"].append(
                {
                    "run_id": run_id,
                    "reason": "no_evaluation",
                    "run_status": str(getattr(run, "status") or ""),
                    "run_name": str(getattr(run, "name") or ""),
                }
            )
            logger.warning(
                "rolling OOS reliability: skipping run %s for market %s because it "
                "has no ModelEvaluation",
                run_id,
                market,
            )
            continue
        selected.append(_SelectedRun(run=run, evaluation=evaluation))
        audit["selected"].append(
            {
                "run_id": run_id,
                "evaluation_id": int(getattr(evaluation, "id")),
                "evaluation_status": str(getattr(evaluation, "status") or ""),
                "run_status": str(getattr(run, "status") or ""),
                "run_name": str(getattr(run, "name") or ""),
            }
        )
        if len(selected) >= max_select:
            break
    audit["scanned_run_count"] = int(audit["scanned_run_count"]) + len(audit["skipped"])
    return selected, audit


def collect_matured_rows(
    db: Session,
    *,
    markets: Sequence[str],
    recent_runs: int = 1,
    horizon_days: int | None = None,
    run_ids: Sequence[int] | None = None,
    run_scan_limit: int = DEFAULT_RUN_SCAN_LIMIT,
) -> tuple[list[MaturedReliabilityRow], dict[str, object]]:
    """Read the newest *evaluated* per-market runs and join their scores.

    Returns ``(rows, key_map)`` where ``key_map`` carries the auditable
    ``model_version -> template key`` resolution, the per-run score-source
    summary (which storage layer supplied each score) and the run-selection
    audit (selected ``run_id``/``evaluation_id`` plus skipped runs and reasons).

    ``run_ids`` pins exact runs for schedulers that need deterministic input;
    otherwise the newest success run *with* a ``ModelEvaluation`` wins, so an
    unevaluated ablation run can no longer shadow the scheduled run.
    """

    target_markets = [str(item).upper() for item in markets if str(item).strip().upper() in {"CN", "US"}]
    target_markets = list(dict.fromkeys(target_markets))
    collected: list[MaturedReliabilityRow] = []
    model_key_by_version: dict[str, str] = {}
    notes: dict[str, str] = {}
    score_sources: dict[str, object] = {}
    run_selection: dict[str, object] = {}
    for market in target_markets:
        selected_runs, selection_audit = _select_runs_for_market(
            db,
            market=market,
            recent_runs=recent_runs,
            run_ids=run_ids,
            run_scan_limit=run_scan_limit,
        )
        run_selection[market] = selection_audit
        for item in selected_runs:
            run = item.run
            evaluation = item.evaluation
            run_id = int(getattr(run, "id"))
            version = run_model_version(run)
            template_key, note = resolve_model_template_key(
                model_type=run.model_type, market=market, config=_run_config(run)
            )
            model_key_by_version[version] = template_key or version
            notes[version] = note
            if template_key is None:
                logger.warning(
                    "rolling OOS reliability: model run %s does not map to a "
                    "MODEL_TEMPLATES key (%s); using the model version as the "
                    "artifact key",
                    version,
                    note,
                )
            try:
                summary = json.loads(evaluation.summary_json or "{}")
            except (TypeError, ValueError):
                summary = {}
            outcomes = summary.get("candidate_outcomes") or []
            if not isinstance(outcomes, Sequence) or isinstance(outcomes, (str, bytes)):
                continue
            trade_dates = [str(item.get("trade_date") or "") for item in outcomes if isinstance(item, Mapping)]
            resolved_horizon = horizon_days if horizon_days is not None else resolve_run_horizon(run)
            prediction_index, source_audit = _prediction_index(
                db,
                model_run_id=run_id,
                market=market,
                trade_dates=trade_dates,
            )
            score_sources[version] = {
                "run_id": run_id,
                "evaluation_id": int(getattr(evaluation, "id")),
                **source_audit,
            }
            collected.extend(
                matured_rows_from_candidate_outcomes(
                    outcomes,
                    model_version=version,
                    prediction_index=prediction_index,
                    horizon_days=resolved_horizon,
                )
            )
    return collected, {
        "model_key_by_version": model_key_by_version,
        "notes": notes,
        "score_sources": score_sources,
        "run_selection": run_selection,
    }


def _select_single_horizon(
    rows: Sequence[MaturedReliabilityRow],
) -> tuple[list[MaturedReliabilityRow], int | None, str | None]:
    """The producers require exactly one horizon; keep the most populated one."""

    counts: dict[int, int] = {}
    for row in rows:
        counts[row.horizon_days] = counts.get(row.horizon_days, 0) + 1
    if not counts:
        return list(rows), None, None
    primary = max(counts, key=lambda horizon: (counts[horizon], -horizon))
    if len(counts) > 1:
        note = "mixed_horizons_kept:" + str(primary)
        logger.warning(
            "rolling OOS reliability: dropping non-primary horizons %s in favour of %sd",
            sorted(horizon for horizon in counts if horizon != primary),
            primary,
        )
        return [row for row in rows if row.horizon_days == primary], primary, note
    return list(rows), primary, None


def artifact_root(root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else Path(get_settings().artifacts_dir)
    return base / "stock_selection_research"


def reliability_artifact_path(root: Path | str | None = None) -> Path:
    return artifact_root(root) / RELIABILITY_ARTIFACT_DIRNAME / OOS_RELIABILITY_ARTIFACT_FILENAME


def calibration_artifact_path(root: Path | str | None = None) -> Path:
    return artifact_root(root) / CALIBRATION_ARTIFACT_DIRNAME / PROBABILITY_CALIBRATION_ARTIFACT_FILENAME


def model_key_resolution_path(root: Path | str | None = None) -> Path:
    return artifact_root(root) / RELIABILITY_ARTIFACT_DIRNAME / MODEL_KEY_RESOLUTION_FILENAME


def _atomic_write_json(payload: Mapping[str, object], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)


def persist_reliability_artifacts(
    matured_rows: Sequence[MaturedReliabilityRow],
    *,
    as_of_date: date,
    key_map: Mapping[str, str] | None = None,
    notes: Mapping[str, str] | None = None,
    score_sources: Mapping[str, object] | None = None,
    run_selection: Mapping[str, object] | None = None,
    calibration_model_key: str | None = None,
    lookback_dates: int = DEFAULT_LOOKBACK_DATES,
    minimum_observations: int = DEFAULT_MINIMUM_OBSERVATIONS,
    bin_count: int = DEFAULT_CALIBRATION_BINS,
    metric: str = OOS_METRIC_HIT_RATE,
    root: Path | str | None = None,
) -> dict[str, object]:
    """Build + atomically persist both artifacts from matured evaluation rows.

    Under-sampled models simply produce no entry / ``None`` artifact, so the
    fusion layer keeps its equal-weight and null-probability fallbacks.  The
    ``score_sources`` / ``run_selection`` audits are recorded in the sidecar so
    the score-join layer and the chosen evaluated run are traceable.
    """

    resolved_key_map = dict(key_map or {})
    resolved_notes = dict(notes or {})
    rows, primary_horizon, horizon_note = _select_single_horizon(matured_rows)
    if horizon_note:
        resolved_notes = {**resolved_notes, "__horizon__": horizon_note}
    predictions, labels = producer_inputs(rows)

    reliability = build_rolling_oos_reliability(
        predictions,
        labels,
        as_of_date=as_of_date,
        model_key_by_version=resolved_key_map,
        lookback_dates=lookback_dates,
        minimum_observations=minimum_observations,
        horizon_days=primary_horizon,
        metric=metric,
        score_field="raw_score",
        round_trip_cost_bps=RECONCILED_LABEL_COST_BPS,
        net_of_cost=True,
    )
    resolution_payload = {
        "schema_version": "stock_selection_model_key_resolution_v1",
        "as_of_date": as_of_date.isoformat(),
        "model_key_by_version": resolved_key_map,
        "notes": resolved_notes,
        "label_basis": "reconciled_net_return",
        "horizon_days": primary_horizon,
        "sample_count": len(rows),
        "score_sources": dict(score_sources or {}),
        "run_selection": dict(run_selection or {}),
    }
    _atomic_write_json(resolution_payload, model_key_resolution_path(root))

    written: dict[str, object] = {"artifact_status": "ready"}
    if reliability:
        reliability_payload = write_oos_reliability_metadata(
            reliability, reliability_artifact_path(root)
        )
        written["reliability_status"] = "written"
        written["reliability_models"] = sorted(reliability)
        written["reliability_path"] = str(reliability_artifact_path(root))
        written["reliability_sha256"] = reliability_payload.get("artifact_sha256")
    else:
        written["reliability_status"] = "insufficient_samples"
        written["reliability_models"] = []

    calibration: CalibrationArtifact | None = None
    if calibration_model_key:
        calibration = build_probability_calibration_artifact(
            predictions,
            labels,
            as_of_date=as_of_date,
            method="bins",
            score_field=DEFAULT_CALIBRATION_SCORE_FIELD,
            fit_score_field=DEFAULT_CALIBRATION_FIT_SCORE_FIELD,
            bin_count=bin_count,
            minimum_observations=minimum_observations,
            prior_strength=0.0,
            lookback_dates=lookback_dates,
            horizon_days=primary_horizon,
            round_trip_cost_bps=RECONCILED_LABEL_COST_BPS,
            net_of_cost=True,
            model_key=calibration_model_key,
            model_key_by_version=resolved_key_map,
        )
    if calibration is not None:
        calibration_payload = write_calibration_artifact(calibration, calibration_artifact_path(root))
        written["calibration_status"] = "written"
        written["calibration_version"] = calibration.version
        written["calibration_path"] = str(calibration_artifact_path(root))
        written["calibration_sha256"] = calibration_payload.get("artifact_sha256")
    else:
        written["calibration_status"] = "insufficient_samples"
    return written


@dataclass(frozen=True, slots=True)
class _CachedArtifact:
    mtime_ns: int
    value: object


_ARTIFACT_CACHE: dict[tuple[str, str], _CachedArtifact] = {}


def _read_cached(path: Path, *, loader) -> object:
    try:
        stat = path.stat()
    except OSError:
        return None
    cache_key = (str(path), loader.__name__)
    cached = _ARTIFACT_CACHE.get(cache_key)
    if cached is not None and cached.mtime_ns == stat.st_mtime_ns:
        return cached.value
    try:
        value = loader(path)
    except Exception as exc:  # noqa: BLE001 - a bad artifact must not break serving
        logger.warning("ignoring unreadable stock-selection artifact %s: %s", path, exc)
        value = None
    _ARTIFACT_CACHE[cache_key] = _CachedArtifact(mtime_ns=stat.st_mtime_ns, value=value)
    return value


def load_latest_reliability_metadata(
    root: Path | str | None = None,
) -> dict[str, OosReliabilityMetadata]:
    """Load the newest reliability metadata; ``{}`` when missing or unreadable."""

    loaded = _read_cached(reliability_artifact_path(root), loader=load_oos_reliability_metadata)
    if isinstance(loaded, Mapping):
        return dict(loaded)
    return {}


def load_latest_calibration_artifact(root: Path | str | None = None) -> CalibrationArtifact | None:
    """Load the newest calibration artifact; ``None`` when missing or unreadable."""

    loaded = _read_cached(calibration_artifact_path(root), loader=load_calibration_artifact)
    return loaded if isinstance(loaded, CalibrationArtifact) else None


def attach_reliability_metadata(
    template_rows: Mapping[str, Sequence[dict]],
    metadata: Mapping[str, OosReliabilityMetadata] | None = None,
    *,
    root: Path | str | None = None,
) -> dict[str, list[dict]]:
    """Inject reliability row metadata, loading the latest artifact by default.

    A missing artifact leaves the rows untouched so the fusion layer keeps its
    equal-weight fallback.
    """

    resolved = dict(metadata) if metadata is not None else load_latest_reliability_metadata(root)
    return attach_oos_reliability_metadata(template_rows, resolved)


def inject_calibration_defaults(
    params: Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> dict:
    """Fill ``probability_calibration`` from the newest artifact when absent.

    An explicitly supplied spec always wins; a missing artifact is a no-op so the
    legacy ``expected_hit_probability=None`` behaviour is preserved.
    """

    resolved = dict(params)
    if resolved.get("probability_calibration") is not None:
        return resolved
    artifact = load_latest_calibration_artifact(root)
    if artifact is None:
        return resolved
    resolved["probability_calibration"] = artifact.calibration_params()
    return resolved


def refresh_stock_selection_reliability_artifacts(
    db: Session,
    *,
    markets: Sequence[str],
    recent_runs: int = 1,
    horizon_days: int | None = None,
    run_ids: Sequence[int] | None = None,
    run_scan_limit: int = DEFAULT_RUN_SCAN_LIMIT,
    as_of_date: date | None = None,
    lookback_dates: int = DEFAULT_LOOKBACK_DATES,
    minimum_observations: int = DEFAULT_MINIMUM_OBSERVATIONS,
    bin_count: int = DEFAULT_CALIBRATION_BINS,
    root: Path | str | None = None,
) -> dict[str, object]:
    """Collect matured evaluation rows and persist the reliability artifacts.

    Raises on unexpected failure; the scheduler call sites deliberately catch and
    log so a producer error never blocks the primary evaluation flow.
    """

    effective_as_of = as_of_date or date.today()
    rows, key_map = collect_matured_rows(
        db,
        markets=markets,
        recent_runs=recent_runs,
        horizon_days=horizon_days,
        run_ids=run_ids,
        run_scan_limit=run_scan_limit,
    )
    if not rows:
        return {
            "status": "skipped",
            "reason": "no_matured_outcomes",
            "markets": list(markets),
            # Keep the selection/score-source audit even on an empty result so the
            # chosen run and any shadowing runs stay diagnosable.
            "run_selection": dict(key_map.get("run_selection") or {}),
            "score_sources": dict(key_map.get("score_sources") or {}),
        }
    resolved_map = dict(key_map.get("model_key_by_version") or {})
    notes = dict(key_map.get("notes") or {})
    calibration_model_key = next(
        (
            template_key
            for version, template_key in resolved_map.items()
            if template_key and template_key != version and template_key in _model_template_keys()
        ),
        None,
    )
    persisted = persist_reliability_artifacts(
        rows,
        as_of_date=effective_as_of,
        key_map=resolved_map,
        notes=notes,
        score_sources=key_map.get("score_sources") or {},
        run_selection=key_map.get("run_selection") or {},
        calibration_model_key=calibration_model_key,
        lookback_dates=lookback_dates,
        minimum_observations=minimum_observations,
        bin_count=bin_count,
        root=root,
    )
    return {"status": "success", "markets": list(markets), **persisted}


def _model_template_keys() -> set[str]:
    from app.services.screener import MODEL_TEMPLATES

    return set(MODEL_TEMPLATES)
