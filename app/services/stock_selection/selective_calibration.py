from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable, Mapping, Protocol

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
