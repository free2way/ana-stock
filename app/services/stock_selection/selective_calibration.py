from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Protocol

from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.selective_policy import (
    SelectiveCandidate,
    SelectiveDateDecision,
    SelectiveEvaluationConfig,
    SelectiveEvaluationReport,
    SelectivePolicyConfig,
    evaluate_selective_decisions,
    select_candidates,
)


class PredictionLike(Protocol):
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    model_version: str


@dataclass(frozen=True, slots=True)
class SelectiveCalibrationConfig:
    bin_count: int = 10
    minimum_observations: int = 200
    prior_strength: float = 30.0
    uncertainty_z: float = 1.96
    score_field: str = "cross_sectional_rank"

    def __post_init__(self) -> None:
        if self.bin_count < 2:
            raise ValueError("bin_count must be at least two")
        if self.minimum_observations < self.bin_count:
            raise ValueError("minimum_observations must be at least bin_count")
        if self.prior_strength < 0 or self.uncertainty_z < 0:
            raise ValueError("prior_strength and uncertainty_z must not be negative")
        if self.score_field not in {"raw_score", "cross_sectional_rank"}:
            raise ValueError("score_field must be raw_score or cross_sectional_rank")


@dataclass(frozen=True, slots=True)
class SelectiveCalibrationBin:
    lower_score: float
    upper_score: float
    observation_count: int
    positive_count: int
    calibrated_positive_probability: float
    expected_risk_adjusted_return: float
    probability_uncertainty: float


@dataclass(frozen=True, slots=True)
class SelectiveCalibrator:
    config: SelectiveCalibrationConfig
    horizon_days: int
    training_end_date: date
    source_model_versions: tuple[str, ...]
    bins: tuple[SelectiveCalibrationBin, ...]
    calibration_version: str

    def calibrate(
        self,
        predictions: Iterable[PredictionLike],
        *,
        tradable_by_sample_id: Mapping[str, bool] | None = None,
        risk_tags_by_sample_id: Mapping[str, tuple[str, ...]] | None = None,
    ) -> tuple[SelectiveCandidate, ...]:
        rows = list(predictions)
        if any(item.horizon_days != self.horizon_days for item in rows):
            raise ValueError("calibration input predictions must match fitted horizon")
        sample_ids = [item.sample_id for item in rows]
        if len(sample_ids) != len(set(sample_ids)):
            raise ValueError("calibration input sample_id values must be unique")
        tradability = tradable_by_sample_id or {}
        risk_tags = risk_tags_by_sample_id or {}
        upper_bounds = [item.upper_score for item in self.bins]
        output: list[SelectiveCandidate] = []
        for item in rows:
            score = _prediction_score(item, self.config.score_field)
            bin_index = min(bisect.bisect_left(upper_bounds, score), len(self.bins) - 1)
            calibrated = self.bins[bin_index]
            output.append(
                SelectiveCandidate(
                    sample_id=item.sample_id,
                    ticker=item.ticker,
                    feature_date=item.feature_date,
                    horizon_days=item.horizon_days,
                    raw_score=float(item.raw_score),
                    cross_sectional_rank=float(item.cross_sectional_rank),
                    positive_probability=calibrated.calibrated_positive_probability,
                    expected_risk_adjusted_return=calibrated.expected_risk_adjusted_return,
                    uncertainty=calibrated.probability_uncertainty,
                    model_version=f"{item.model_version}+{self.calibration_version}",
                    tradable=bool(tradability.get(item.sample_id, True)),
                    risk_tags=tuple(risk_tags.get(item.sample_id, ())),
                )
            )
        return tuple(output)


@dataclass(frozen=True, slots=True)
class SelectiveWalkForwardDateAudit:
    prediction_date: date
    status: str
    calibration_observation_count: int
    calibration_start_date: date | None
    calibration_end_date: date | None
    candidate_count: int
    eligible_count: int
    selected_count: int
    abstention_reason: str | None
    maximum_calibrated_probability: float | None
    maximum_expected_risk_adjusted_return: float | None
    minimum_probability_uncertainty: float | None


@dataclass(frozen=True, slots=True)
class SelectiveWalkForwardResult:
    schema_version: str
    model_key: str
    horizon_days: int
    calibration_lookback_dates: int
    decisions: tuple[SelectiveDateDecision, ...]
    audits: tuple[SelectiveWalkForwardDateAudit, ...]
    evaluation: SelectiveEvaluationReport | None


def _prediction_score(prediction: PredictionLike, field: str) -> float:
    value = float(getattr(prediction, field))
    if not math.isfinite(value):
        raise ValueError(f"prediction {field} must be finite for {prediction.sample_id}")
    if field == "cross_sectional_rank" and not 0.0 <= value <= 1.0:
        raise ValueError(f"prediction cross_sectional_rank must be in [0, 1] for {prediction.sample_id}")
    return value


def fit_selective_calibrator(
    predictions: Iterable[PredictionLike],
    labeled_scores: Iterable[FactorScore],
    *,
    prediction_date: date,
    config: SelectiveCalibrationConfig | None = None,
) -> SelectiveCalibrator:
    """Fit score calibration only from labels observable before prediction_date."""

    resolved = config or SelectiveCalibrationConfig()
    prediction_rows = list(predictions)
    labels = list(labeled_scores)
    if len(prediction_rows) < resolved.minimum_observations:
        raise ValueError("selective calibration has insufficient observations")
    prediction_ids = [item.sample_id for item in prediction_rows]
    if len(prediction_ids) != len(set(prediction_ids)):
        raise ValueError("calibration prediction sample_id values must be unique")
    label_by_id = {item.sample_id: item for item in labels}
    if len(label_by_id) != len(labels):
        raise ValueError("calibration label sample_id values must be unique")

    joined: list[tuple[float, float, str, date]] = []
    horizons: set[int] = set()
    source_versions: set[str] = set()
    for prediction in prediction_rows:
        label = label_by_id.get(prediction.sample_id)
        if label is None:
            raise ValueError(f"calibration label is missing: {prediction.sample_id}")
        if (
            prediction.ticker != label.ticker
            or prediction.feature_date != label.feature_date
            or prediction.horizon_days != label.horizon_days
        ):
            raise ValueError(f"calibration prediction and label mismatch: {prediction.sample_id}")
        if (
            label.label_available_date is None
            or label.label_value is None
            or label.label_available_date >= prediction_date
        ):
            raise ValueError(
                "calibration requires labels available before prediction_date: "
                + prediction.sample_id
            )
        label_value = float(label.label_value)
        if not math.isfinite(label_value):
            raise ValueError(f"calibration label must be finite: {prediction.sample_id}")
        joined.append(
            (
                _prediction_score(prediction, resolved.score_field),
                label_value,
                prediction.sample_id,
                prediction.feature_date,
            )
        )
        horizons.add(prediction.horizon_days)
        source_versions.add(prediction.model_version)
    if len(horizons) != 1:
        raise ValueError("selective calibration requires exactly one horizon")

    joined.sort(key=lambda item: (item[0], item[2]))
    global_positive_rate = sum(label > 0 for _, label, _, _ in joined) / len(joined)
    global_mean = sum(label for _, label, _, _ in joined) / len(joined)
    # Quantile boundaries are deduplicated so equal scores can never be split
    # across bins and mapped to conflicting calibration statistics.
    upper_bounds = sorted(
        {
            joined[
                min(
                    len(joined) - 1,
                    math.ceil((bin_index + 1) * len(joined) / resolved.bin_count) - 1,
                )
            ][0]
            for bin_index in range(resolved.bin_count)
        }
    )
    grouped_bins: list[list[tuple[float, float, str, date]]] = [
        [] for _ in upper_bounds
    ]
    for item in joined:
        grouped_bins[bisect.bisect_left(upper_bounds, item[0])].append(item)

    bins: list[SelectiveCalibrationBin] = []
    for group in grouped_bins:
        positive_count = sum(label > 0 for _, label, _, _ in group)
        effective_count = len(group) + resolved.prior_strength
        probability = (
            positive_count + (resolved.prior_strength * global_positive_rate)
        ) / effective_count
        expected_return = (
            sum(label for _, label, _, _ in group)
            + (resolved.prior_strength * global_mean)
        ) / effective_count
        uncertainty = min(
            1.0,
            resolved.uncertainty_z
            * math.sqrt(max(probability * (1.0 - probability), 0.0) / effective_count),
        )
        bins.append(
            SelectiveCalibrationBin(
                lower_score=group[0][0],
                upper_score=group[-1][0],
                observation_count=len(group),
                positive_count=positive_count,
                calibrated_positive_probability=probability,
                expected_risk_adjusted_return=expected_return,
                probability_uncertainty=uncertainty,
            )
        )

    training_end_date = max(item[3] for item in joined)
    version_payload = {
        "config": asdict(resolved),
        "horizon_days": next(iter(horizons)),
        "training_end_date": training_end_date.isoformat(),
        "source_model_versions": sorted(source_versions),
        "bins": [asdict(item) for item in bins],
    }
    digest = hashlib.sha256(
        json.dumps(version_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return SelectiveCalibrator(
        config=resolved,
        horizon_days=next(iter(horizons)),
        training_end_date=training_end_date,
        source_model_versions=tuple(sorted(source_versions)),
        bins=tuple(bins),
        calibration_version=f"selective_calibration_v1:{digest}",
    )


def run_selective_walk_forward(
    predictions: Iterable[PredictionLike],
    labeled_scores: Iterable[FactorScore],
    *,
    evaluation_config: SelectiveEvaluationConfig,
    calibration_config: SelectiveCalibrationConfig | None = None,
    policy_config: SelectivePolicyConfig | None = None,
    calibration_lookback_dates: int = 60,
    market_gate_by_date: Mapping[date, bool] | None = None,
) -> SelectiveWalkForwardResult:
    """Replay calibration and abstention using only previously matured OOS rows."""

    if calibration_lookback_dates <= 0:
        raise ValueError("calibration_lookback_dates must be positive")
    resolved_calibration = calibration_config or SelectiveCalibrationConfig()
    resolved_policy = policy_config or SelectivePolicyConfig()
    rows = list(predictions)
    labels = list(labeled_scores)
    if not rows:
        raise ValueError("selective walk-forward requires predictions")
    sample_ids = [item.sample_id for item in rows]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("selective walk-forward prediction sample_id values must be unique")
    horizons = {item.horizon_days for item in rows}
    if len(horizons) != 1:
        raise ValueError("selective walk-forward requires exactly one horizon")

    label_by_id = {item.sample_id: item for item in labels}
    if len(label_by_id) != len(labels):
        raise ValueError("selective walk-forward label sample_id values must be unique")
    missing = [item.sample_id for item in rows if item.sample_id not in label_by_id]
    if missing:
        raise ValueError("selective walk-forward labels are missing: " + ", ".join(missing[:5]))

    by_date: dict[date, list[PredictionLike]] = defaultdict(list)
    for item in rows:
        by_date[item.feature_date].append(item)
    ordered_dates = sorted(by_date)
    decisions: list[SelectiveDateDecision] = []
    audits: list[SelectiveWalkForwardDateAudit] = []
    gates = market_gate_by_date or {}
    for prediction_date in ordered_dates:
        eligible_history_dates = [
            history_date
            for history_date in ordered_dates
            if history_date < prediction_date
            and all(
                label_by_id[item.sample_id].label_available_date is not None
                and label_by_id[item.sample_id].label_available_date < prediction_date
                for item in by_date[history_date]
            )
        ][-calibration_lookback_dates:]
        calibration_rows = [
            item for history_date in eligible_history_dates for item in by_date[history_date]
        ]
        if len(calibration_rows) < resolved_calibration.minimum_observations:
            audits.append(
                SelectiveWalkForwardDateAudit(
                    prediction_date=prediction_date,
                    status="skipped:insufficient_matured_oos_calibration",
                    calibration_observation_count=len(calibration_rows),
                    calibration_start_date=(eligible_history_dates[0] if eligible_history_dates else None),
                    calibration_end_date=(eligible_history_dates[-1] if eligible_history_dates else None),
                    candidate_count=len(by_date[prediction_date]),
                    eligible_count=0,
                    selected_count=0,
                    abstention_reason=None,
                    maximum_calibrated_probability=None,
                    maximum_expected_risk_adjusted_return=None,
                    minimum_probability_uncertainty=None,
                )
            )
            continue
        calibrator = fit_selective_calibrator(
            calibration_rows,
            labels,
            prediction_date=prediction_date,
            config=resolved_calibration,
        )
        calibrated = calibrator.calibrate(by_date[prediction_date])
        decision = select_candidates(
            calibrated,
            config=resolved_policy,
            market_gate_by_date={prediction_date: bool(gates.get(prediction_date, True))},
        )[0]
        decisions.append(decision)
        audits.append(
            SelectiveWalkForwardDateAudit(
                prediction_date=prediction_date,
                status="success",
                calibration_observation_count=len(calibration_rows),
                calibration_start_date=eligible_history_dates[0],
                calibration_end_date=eligible_history_dates[-1],
                candidate_count=len(calibrated),
                eligible_count=decision.eligible_count,
                selected_count=len(decision.selected),
                abstention_reason=decision.abstention_reason,
                maximum_calibrated_probability=max(
                    item.positive_probability for item in calibrated
                ),
                maximum_expected_risk_adjusted_return=max(
                    item.expected_risk_adjusted_return for item in calibrated
                ),
                minimum_probability_uncertainty=min(item.uncertainty for item in calibrated),
            )
        )

    evaluation: SelectiveEvaluationReport | None = None
    if decisions and any(item.selected for item in decisions):
        evaluation = evaluate_selective_decisions(
            decisions,
            labels,
            config=evaluation_config,
        )
    return SelectiveWalkForwardResult(
        schema_version="selective_stock_walk_forward_v1",
        model_key=evaluation_config.model_key,
        horizon_days=next(iter(horizons)),
        calibration_lookback_dates=calibration_lookback_dates,
        decisions=tuple(decisions),
        audits=tuple(audits),
        evaluation=evaluation,
    )


# ---------------------------------------------------------------------------
# Producers: turn matured OOS evaluation rows into the metadata and
# calibration specs the screener fusion layer consumes.
#
# Both producers are strictly point-in-time: an observation may only enter a
# window when its *feature date* and its *label availability date* are strictly
# earlier than the prediction date being annotated.  Nothing is emitted when a
# model has too few matured observations, so the fusion layer falls back to
# equal weights and reports an uncalibrated probability instead of inheriting a
# fabricated one.
# ---------------------------------------------------------------------------

OOS_RELIABILITY_SCHEMA_VERSION = "model_oos_reliability_v1"
PROBABILITY_CALIBRATION_ARTIFACT_SCHEMA_VERSION = "probability_calibration_artifact_v1"
OOS_METRIC_HIT_RATE = "oos_hit_rate"
OOS_METRIC_IC = "oos_ic"
OOS_DEFINITION_NET = "net_of_cost_positive_return"
OOS_DEFINITION_GROSS = "gross_positive_return"


def _net_label(label_value: float, *, round_trip_cost_bps: float, net_of_cost: bool) -> float:
    value = float(label_value)
    if not math.isfinite(value):
        raise ValueError("OOS label value must be finite")
    if not net_of_cost:
        return value
    cost_bps = float(round_trip_cost_bps)
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be finite and non-negative")
    return value - cost_bps / 10000.0


def _spearman_rank_ic(scores: Sequence[float], labels: Sequence[float]) -> float | None:
    """Rank correlation; ``None`` when a side has no rank variance."""

    count = len(scores)
    if count < 3 or count != len(labels):
        return None

    def _ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(count), key=lambda index: values[index])
        ranks = [0.0] * count
        position = 0
        while position < count:
            end = position
            while end + 1 < count and values[order[end + 1]] == values[order[position]]:
                end += 1
            average = (position + end) / 2.0 + 1.0
            for offset in range(position, end + 1):
                ranks[order[offset]] = average
            position = end + 1
        return ranks

    x_ranks = _ranks(scores)
    y_ranks = _ranks(labels)
    x_mean = sum(x_ranks) / count
    y_mean = sum(y_ranks) / count
    covariance = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_ranks, y_ranks, strict=True))
    x_variance = sum((x - x_mean) ** 2 for x in x_ranks)
    y_variance = sum((y - y_mean) ** 2 for y in y_ranks)
    denominator = math.sqrt(x_variance * y_variance)
    if denominator <= 0.0:
        return None
    return covariance / denominator


def _join_matured_rows(
    predictions: Iterable[PredictionLike],
    labeled_scores: Iterable[FactorScore],
    *,
    as_of_date: date,
    horizon_days: int | None,
) -> list[tuple[PredictionLike, FactorScore]]:
    """Join predictions to labels, keeping only rows matured before as_of_date."""

    label_by_id: dict[str, FactorScore] = {}
    for label in labeled_scores:
        if label.sample_id in label_by_id:
            raise ValueError("rolling OOS label sample_id values must be unique")
        label_by_id[label.sample_id] = label
    joined: list[tuple[PredictionLike, FactorScore]] = []
    for prediction in predictions:
        label = label_by_id.get(prediction.sample_id)
        if label is None:
            raise ValueError(f"rolling OOS metadata is missing a label: {prediction.sample_id}")
        if (
            prediction.ticker != label.ticker
            or prediction.feature_date != label.feature_date
            or prediction.horizon_days != label.horizon_days
        ):
            raise ValueError(f"rolling OOS prediction and label mismatch: {prediction.sample_id}")
        if horizon_days is not None and prediction.horizon_days != int(horizon_days):
            continue
        # PIT: the signal itself must precede the annotated prediction date.
        if prediction.feature_date >= as_of_date:
            continue
        # PIT: the outcome must already be observable; a label that matures on
        # or after as_of_date is future leakage and is dropped entirely.
        if label.label_available_date is None or label.label_value is None:
            continue
        if label.label_available_date >= as_of_date:
            continue
        joined.append((prediction, label))
    return joined


def _recent_window(
    rows: Sequence[tuple[PredictionLike, FactorScore]], lookback_dates: int
) -> list[tuple[PredictionLike, FactorScore]]:
    ordered_dates = sorted({row[0].feature_date for row in rows})
    if not ordered_dates:
        return []
    window_dates = set(ordered_dates[-lookback_dates:])
    return [row for row in rows if row[0].feature_date in window_dates]


@dataclass(frozen=True, slots=True)
class OosReliabilityMetadata:
    """Rolling, point-in-time per-model reliability for one prediction date.

    ``model_reliability`` is the value the fusion layer should weight by; it is
    the hit rate (or ``abs(ic)``) of the bounded rolling window below.  The
    window is always strictly earlier than ``as_of_date``.
    """

    model_key: str
    metric: str
    score_field: str
    horizon_days: int
    as_of_date: date
    window_start_date: date
    window_end_date: date
    sample_count: int
    positive_count: int
    oos_hit_rate: float | None
    oos_ic: float | None
    model_reliability: float
    definition: str
    round_trip_cost_bps: float
    lookback_dates: int
    version: str

    def row_metadata(self) -> dict[str, object]:
        """Row-level fields read by ``multi_model_confluence.resolve_model_weights``."""

        payload: dict[str, object] = {
            "model_reliability": round(self.model_reliability, 6),
            "model_oos_metric": self.metric,
            "model_oos_score_field": self.score_field,
            "model_oos_horizon_days": self.horizon_days,
            "model_oos_as_of_date": self.as_of_date.isoformat(),
            "model_oos_window_start": self.window_start_date.isoformat(),
            "model_oos_window_end": self.window_end_date.isoformat(),
            "model_oos_sample_count": self.sample_count,
            "model_oos_definition": self.definition,
            "model_oos_version": self.version,
        }
        if self.metric == OOS_METRIC_HIT_RATE and self.oos_hit_rate is not None:
            payload["model_oos_hit_rate"] = round(self.oos_hit_rate, 6)
        if self.metric == OOS_METRIC_IC and self.oos_ic is not None:
            payload["model_oos_ic"] = round(self.oos_ic, 6)
        # Non-selected statistics stay auditable but do not drive fusion weights.
        if self.oos_hit_rate is not None:
            payload["model_oos_hit_rate_diagnostic"] = round(self.oos_hit_rate, 6)
        if self.oos_ic is not None:
            payload["model_oos_ic_diagnostic"] = round(self.oos_ic, 6)
        return payload

    def artifact_payload(self) -> dict[str, object]:
        return {
            "schema_version": OOS_RELIABILITY_SCHEMA_VERSION,
            "model_key": self.model_key,
            "metric": self.metric,
            "score_field": self.score_field,
            "horizon_days": self.horizon_days,
            "as_of_date": self.as_of_date.isoformat(),
            "window_start_date": self.window_start_date.isoformat(),
            "window_end_date": self.window_end_date.isoformat(),
            "sample_count": self.sample_count,
            "positive_count": self.positive_count,
            "oos_hit_rate": self.oos_hit_rate,
            "oos_ic": self.oos_ic,
            "model_reliability": self.model_reliability,
            "definition": self.definition,
            "round_trip_cost_bps": self.round_trip_cost_bps,
            "lookback_dates": self.lookback_dates,
            "version": self.version,
        }


def build_rolling_oos_reliability(
    predictions: Iterable[PredictionLike],
    labeled_scores: Iterable[FactorScore],
    *,
    as_of_date: date,
    model_key_by_version: Mapping[str, str] | None = None,
    lookback_dates: int = 60,
    minimum_observations: int = 30,
    horizon_days: int | None = None,
    score_field: str = "raw_score",
    metric: str = OOS_METRIC_HIT_RATE,
    round_trip_cost_bps: float = 0.0,
    net_of_cost: bool = True,
) -> dict[str, OosReliabilityMetadata]:
    """Build per-model rolling OOS reliability, omitting under-sampled models.

    Only matured observations strictly earlier than ``as_of_date`` are used; the
    window is capped at ``lookback_dates`` distinct feature dates.  A model with
    fewer than ``minimum_observations`` matured rows produces **no** entry so the
    fusion layer keeps its equal-weight fallback rather than trusting a thin
    sample.
    """

    if lookback_dates <= 0:
        raise ValueError("lookback_dates must be positive")
    if minimum_observations < 1:
        raise ValueError("minimum_observations must be positive")
    if metric not in {OOS_METRIC_HIT_RATE, OOS_METRIC_IC}:
        raise ValueError("metric must be oos_hit_rate or oos_ic")
    if score_field not in {"raw_score", "cross_sectional_rank"}:
        raise ValueError("score_field must be raw_score or cross_sectional_rank")
    key_map = dict(model_key_by_version or {})
    rows = _join_matured_rows(
        predictions, labeled_scores, as_of_date=as_of_date, horizon_days=horizon_days
    )
    if not rows:
        return {}
    resolved_horizons = {prediction.horizon_days for prediction, _ in rows}
    if len(resolved_horizons) != 1:
        raise ValueError("rolling OOS reliability requires exactly one horizon")
    resolved_horizon = next(iter(resolved_horizons))

    by_model: dict[str, list[tuple[PredictionLike, FactorScore]]] = defaultdict(list)
    for prediction, label in rows:
        by_model[key_map.get(prediction.model_version, prediction.model_version)].append(
            (prediction, label)
        )

    definition = OOS_DEFINITION_NET if net_of_cost else OOS_DEFINITION_GROSS
    output: dict[str, OosReliabilityMetadata] = {}
    for model_key, model_rows in by_model.items():
        window = _recent_window(model_rows, lookback_dates)
        if len(window) < minimum_observations:
            continue
        scores = [_prediction_score(prediction, score_field) for prediction, _ in window]
        labels = [
            _net_label(label.label_value, round_trip_cost_bps=round_trip_cost_bps, net_of_cost=net_of_cost)
            for _, label in window
        ]
        positive_count = sum(1 for value in labels if value > 0)
        sample_count = len(window)
        hit_rate = positive_count / sample_count
        ic = _spearman_rank_ic(scores, labels)
        if metric == OOS_METRIC_IC and ic is None:
            continue
        model_reliability = hit_rate if metric == OOS_METRIC_HIT_RATE else min(1.0, abs(ic or 0.0))
        window_dates = sorted({prediction.feature_date for prediction, _ in window})
        if window_dates[-1] >= as_of_date:
            raise ValueError("rolling OOS window must end strictly before as_of_date")
        version_payload = {
            "model_key": model_key,
            "metric": metric,
            "score_field": score_field,
            "horizon_days": resolved_horizon,
            "as_of_date": as_of_date.isoformat(),
            "window_start_date": window_dates[0].isoformat(),
            "window_end_date": window_dates[-1].isoformat(),
            "sample_count": sample_count,
            "positive_count": positive_count,
            "definition": definition,
            "round_trip_cost_bps": float(round_trip_cost_bps) if net_of_cost else 0.0,
        }
        version = "rolling_oos_reliability_v1:" + hashlib.sha256(
            json.dumps(version_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        output[model_key] = OosReliabilityMetadata(
            model_key=model_key,
            metric=metric,
            score_field=score_field,
            horizon_days=resolved_horizon,
            as_of_date=as_of_date,
            window_start_date=window_dates[0],
            window_end_date=window_dates[-1],
            sample_count=sample_count,
            positive_count=positive_count,
            oos_hit_rate=hit_rate,
            oos_ic=ic,
            model_reliability=model_reliability,
            definition=definition,
            round_trip_cost_bps=float(round_trip_cost_bps) if net_of_cost else 0.0,
            lookback_dates=lookback_dates,
            version=version,
        )
    return output


def attach_oos_reliability_metadata(
    template_rows: Mapping[str, Sequence[dict]],
    metadata_by_model: Mapping[str, OosReliabilityMetadata],
) -> dict[str, list[dict]]:
    """Copy template rows, adding reliability fields to every covered model row.

    Models without metadata are copied untouched, so the fusion layer sees no
    ``model_oos_*`` field for them and keeps its equal-weight fallback.
    """

    enriched: dict[str, list[dict]] = {}
    for template_key, rows in template_rows.items():
        metadata = metadata_by_model.get(template_key)
        if metadata is None:
            enriched[template_key] = [dict(row) for row in rows]
            continue
        fields = metadata.row_metadata()
        enriched[template_key] = [{**dict(row), **fields} for row in rows]
    return enriched


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    """A point-in-time probability-calibration specification.

    ``bins`` mirrors :class:`SelectiveCalibrationBin` (``upper_score`` /
    ``calibrated_positive_probability``); ``calibration_params`` returns the
    mapping the screener passes as ``params["probability_calibration"]``.
    """

    method: str
    score_field: str
    fit_score_field: str
    horizon_days: int
    as_of_date: date
    window_start_date: date
    window_end_date: date
    sample_count: int
    positive_count: int
    source: str
    version: str
    definition: str
    round_trip_cost_bps: float
    lookback_dates: int
    bins: tuple[dict[str, object], ...]
    samples: tuple[tuple[float, float], ...]

    def calibration_params(self) -> dict[str, object]:
        params: dict[str, object] = {
            "method": self.method,
            "source": self.version,
            "score_field": self.score_field,
            "horizon_days": self.horizon_days,
            "as_of_date": self.as_of_date.isoformat(),
            "window_start_date": self.window_start_date.isoformat(),
            "window_end_date": self.window_end_date.isoformat(),
            "sample_count": self.sample_count,
            "positive_count": self.positive_count,
            "definition": self.definition,
        }
        if self.method == "bins":
            params["bins"] = [dict(bin_row) for bin_row in self.bins]
        else:
            params["samples"] = [list(sample) for sample in self.samples]
            params["min_samples"] = 2
        return params

    def artifact_payload(self) -> dict[str, object]:
        return {
            "schema_version": PROBABILITY_CALIBRATION_ARTIFACT_SCHEMA_VERSION,
            "method": self.method,
            "score_field": self.score_field,
            "fit_score_field": self.fit_score_field,
            "horizon_days": self.horizon_days,
            "as_of_date": self.as_of_date.isoformat(),
            "window_start_date": self.window_start_date.isoformat(),
            "window_end_date": self.window_end_date.isoformat(),
            "sample_count": self.sample_count,
            "positive_count": self.positive_count,
            "source": self.source,
            "version": self.version,
            "definition": self.definition,
            "round_trip_cost_bps": self.round_trip_cost_bps,
            "lookback_dates": self.lookback_dates,
            "bins": [dict(bin_row) for bin_row in self.bins],
            "samples": [list(sample) for sample in self.samples],
        }


def build_probability_calibration_artifact(
    predictions: Iterable[PredictionLike],
    labeled_scores: Iterable[FactorScore],
    *,
    as_of_date: date,
    method: str = "bins",
    score_field: str = "model_score",
    fit_score_field: str = "raw_score",
    bin_count: int = 10,
    minimum_observations: int = 30,
    prior_strength: float = 0.0,
    lookback_dates: int = 60,
    horizon_days: int | None = None,
    round_trip_cost_bps: float = 0.0,
    net_of_cost: bool = True,
    model_key: str | None = None,
    model_key_by_version: Mapping[str, str] | None = None,
) -> CalibrationArtifact | None:
    """Fit calibration bins/samples from matured OOS rows; ``None`` if too thin.

    Profitability is defined net of the round-trip cost: an observation is
    positive when ``label_value - cost`` is strictly greater than zero.  Only
    observations that matured strictly before ``as_of_date`` enter the window.
    """

    if method not in {"bins", "isotonic"}:
        raise ValueError("method must be bins or isotonic")
    if fit_score_field not in {"raw_score", "cross_sectional_rank"}:
        raise ValueError("fit_score_field must be raw_score or cross_sectional_rank")
    if lookback_dates <= 0:
        raise ValueError("lookback_dates must be positive")
    if minimum_observations < bin_count:
        raise ValueError("minimum_observations must be at least bin_count")
    key_map = dict(model_key_by_version or {})
    rows = _join_matured_rows(
        predictions, labeled_scores, as_of_date=as_of_date, horizon_days=horizon_days
    )
    if model_key is not None:
        rows = [
            (prediction, label)
            for prediction, label in rows
            if key_map.get(prediction.model_version, prediction.model_version) == model_key
        ]
    window = _recent_window(rows, lookback_dates)
    if len(window) < minimum_observations:
        return None
    horizons = {prediction.horizon_days for prediction, _ in window}
    if len(horizons) != 1:
        raise ValueError("probability calibration artifact requires exactly one horizon")
    resolved_horizon = next(iter(horizons))

    adjusted_labels = [
        replace(
            label,
            label_value=_net_label(
                label.label_value, round_trip_cost_bps=round_trip_cost_bps, net_of_cost=net_of_cost
            ),
        )
        for _, label in window
    ]
    config = SelectiveCalibrationConfig(
        bin_count=bin_count,
        minimum_observations=minimum_observations,
        prior_strength=prior_strength,
        score_field=fit_score_field,
    )
    calibrator = fit_selective_calibrator(
        [prediction for prediction, _ in window],
        adjusted_labels,
        prediction_date=as_of_date,
        config=config,
    )
    bins = tuple(asdict(bin_row) for bin_row in calibrator.bins)
    samples = tuple(
        sorted(
            (
                _prediction_score(prediction, fit_score_field),
                1.0 if label.label_value > 0 else 0.0,
            )
            for (prediction, _), label in zip(window, adjusted_labels, strict=True)
        )
    )
    positive_count = sum(1 for _, label in samples if label > 0)
    window_dates = sorted({prediction.feature_date for prediction, _ in window})
    if window_dates[-1] >= as_of_date:
        raise ValueError("calibration window must end strictly before as_of_date")
    definition = OOS_DEFINITION_NET if net_of_cost else OOS_DEFINITION_GROSS
    digest = hashlib.sha256(
        json.dumps(
            {
                "method": method,
                "score_field": score_field,
                "fit_score_field": fit_score_field,
                "horizon_days": resolved_horizon,
                "as_of_date": as_of_date.isoformat(),
                "window_start_date": window_dates[0].isoformat(),
                "window_end_date": window_dates[-1].isoformat(),
                "sample_count": len(window),
                "definition": definition,
                "bins": [dict(bin_row) for bin_row in bins],
                "samples": [list(sample) for sample in samples],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    version = f"selective_calibration_artifact_v1:{digest}"
    return CalibrationArtifact(
        method=method,
        score_field=score_field,
        fit_score_field=fit_score_field,
        horizon_days=resolved_horizon,
        as_of_date=as_of_date,
        window_start_date=window_dates[0],
        window_end_date=window_dates[-1],
        sample_count=len(window),
        positive_count=positive_count,
        source=calibrator.calibration_version,
        version=version,
        definition=definition,
        round_trip_cost_bps=float(round_trip_cost_bps) if net_of_cost else 0.0,
        lookback_dates=lookback_dates,
        bins=bins,
        samples=samples,
    )


def calibration_artifact_from_payload(payload: Mapping[str, object]) -> CalibrationArtifact:
    """Rebuild an artifact from its persisted payload (validates the schema)."""

    if not isinstance(payload, Mapping):
        raise TypeError("calibration artifact payload must be a mapping")
    if str(payload.get("schema_version") or "") != PROBABILITY_CALIBRATION_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("unsupported probability calibration artifact schema")
    method = str(payload.get("method") or "")
    if method not in {"bins", "isotonic"}:
        raise ValueError("calibration artifact method must be bins or isotonic")
    bins_payload = payload.get("bins") or []
    if not isinstance(bins_payload, Sequence) or isinstance(bins_payload, (str, bytes)):
        raise TypeError("calibration artifact bins must be a sequence")
    samples_payload = payload.get("samples") or []
    if not isinstance(samples_payload, Sequence) or isinstance(samples_payload, (str, bytes)):
        raise TypeError("calibration artifact samples must be a sequence")
    bins = tuple(
        {
            "lower_score": float(item["lower_score"]),
            "upper_score": float(item["upper_score"]),
            "observation_count": int(item["observation_count"]),
            "positive_count": int(item["positive_count"]),
            "calibrated_positive_probability": float(item["calibrated_positive_probability"]),
            "expected_risk_adjusted_return": float(item["expected_risk_adjusted_return"]),
            "probability_uncertainty": float(item["probability_uncertainty"]),
        }
        for item in bins_payload
    )
    samples = tuple((float(item[0]), float(item[1])) for item in samples_payload)
    if method == "bins" and not bins:
        raise ValueError("bins calibration artifact requires at least one bin")
    return CalibrationArtifact(
        method=method,
        score_field=str(payload.get("score_field") or "model_score"),
        fit_score_field=str(payload.get("fit_score_field") or "raw_score"),
        horizon_days=int(payload["horizon_days"]),
        as_of_date=date.fromisoformat(str(payload["as_of_date"])),
        window_start_date=date.fromisoformat(str(payload["window_start_date"])),
        window_end_date=date.fromisoformat(str(payload["window_end_date"])),
        sample_count=int(payload["sample_count"]),
        positive_count=int(payload.get("positive_count") or 0),
        source=str(payload.get("source") or ""),
        version=str(payload.get("version") or ""),
        definition=str(payload.get("definition") or OOS_DEFINITION_NET),
        round_trip_cost_bps=float(payload.get("round_trip_cost_bps") or 0.0),
        lookback_dates=int(payload.get("lookback_dates") or 0),
        bins=bins,
        samples=samples,
    )


def write_calibration_artifact(artifact: CalibrationArtifact, path: Path | str) -> dict[str, object]:
    """Persist a calibration artifact as an atomic JSON file; return its payload."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = artifact.artifact_payload()
    payload["artifact_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)
    return payload


def load_calibration_artifact(path: Path | str) -> CalibrationArtifact:
    """Load a persisted calibration artifact produced above."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return calibration_artifact_from_payload(payload)


def write_oos_reliability_metadata(
    metadata_by_model: Mapping[str, OosReliabilityMetadata], path: Path | str
) -> dict[str, object]:
    """Persist per-model reliability metadata as an atomic JSON artifact."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": OOS_RELIABILITY_SCHEMA_VERSION,
        "models": {
            key: value.artifact_payload() for key, value in sorted(metadata_by_model.items())
        },
    }
    payload["artifact_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    os.replace(temporary, target)
    return payload


def load_oos_reliability_metadata(
    path: Path | str,
) -> dict[str, OosReliabilityMetadata]:
    """Load per-model reliability metadata keyed by model/template key."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if str(payload.get("schema_version") or "") != OOS_RELIABILITY_SCHEMA_VERSION:
        raise ValueError("unsupported OOS reliability metadata schema")
    output: dict[str, OosReliabilityMetadata] = {}
    for model_key, item in dict(payload.get("models") or {}).items():
        output[str(model_key)] = OosReliabilityMetadata(
            model_key=str(item.get("model_key") or model_key),
            metric=str(item["metric"]),
            score_field=str(item.get("score_field") or "raw_score"),
            horizon_days=int(item["horizon_days"]),
            as_of_date=date.fromisoformat(str(item["as_of_date"])),
            window_start_date=date.fromisoformat(str(item["window_start_date"])),
            window_end_date=date.fromisoformat(str(item["window_end_date"])),
            sample_count=int(item["sample_count"]),
            positive_count=int(item.get("positive_count") or 0),
            oos_hit_rate=(None if item.get("oos_hit_rate") is None else float(item["oos_hit_rate"])),
            oos_ic=(None if item.get("oos_ic") is None else float(item["oos_ic"])),
            model_reliability=float(item["model_reliability"]),
            definition=str(item.get("definition") or OOS_DEFINITION_NET),
            round_trip_cost_bps=float(item.get("round_trip_cost_bps") or 0.0),
            lookback_dates=int(item.get("lookback_dates") or 0),
            version=str(item.get("version") or ""),
        )
    return output
