from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable, Mapping

from app.services.stock_selection.factor_pipeline import FactorScore, _percentile_ranks


@dataclass(frozen=True, slots=True)
class TwoStageConfig:
    horizon_days: int
    risk_factor_name: str
    target_top_n: int = 5
    candidate_fractions: tuple[float, ...] = (0.10, 0.20, 0.40, 1.0)
    validation_dates: int = 20
    minimum_validation_dates: int = 10
    confidence_penalty: float = 1.0

    def __post_init__(self) -> None:
        if self.horizon_days <= 0 or self.target_top_n <= 0:
            raise ValueError("horizon_days and target_top_n must be positive")
        if not str(self.risk_factor_name or "").strip():
            raise ValueError("risk_factor_name must not be empty")
        if (
            not self.candidate_fractions
            or any(not 0 < value <= 1 for value in self.candidate_fractions)
            or len(set(self.candidate_fractions)) != len(self.candidate_fractions)
        ):
            raise ValueError("candidate_fractions must be unique values in (0, 1]")
        if 1.0 not in self.candidate_fractions:
            raise ValueError("candidate_fractions must include the unfiltered 1.0 control")
        if self.validation_dates < self.minimum_validation_dates or self.minimum_validation_dates < 2:
            raise ValueError("validation date requirements are invalid")
        if self.confidence_penalty < 0:
            raise ValueError("confidence_penalty must not be negative")


@dataclass(frozen=True, slots=True)
class TwoStageFractionMetric:
    candidate_fraction: float
    evaluated_date_count: int
    mean_top_n_label: float
    positive_date_rate: float
    standard_error: float
    selection_objective: float


@dataclass(frozen=True, slots=True)
class TwoStageTrainingAudit:
    prediction_date: date
    training_sample_count: int
    validation_dates: tuple[date, ...]
    selected_candidate_fraction: float
    fraction_metrics: tuple[TwoStageFractionMetric, ...]


@dataclass(frozen=True, slots=True)
class TwoStagePrediction:
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    model_version: str


@dataclass(frozen=True, slots=True)
class TwoStageSelectorModel:
    config: TwoStageConfig
    selected_candidate_fraction: float
    audit: TwoStageTrainingAudit
    model_version: str

    def predict(self, scores: Iterable[FactorScore]) -> tuple[TwoStagePrediction, ...]:
        rows = list(scores)
        if any(item.horizon_days != self.config.horizon_days for item in rows):
            raise ValueError("prediction scores must match two-stage model horizon")
        grouped: dict[date, list[FactorScore]] = defaultdict(list)
        for item in rows:
            grouped[item.feature_date].append(item)
        predictions: list[TwoStagePrediction] = []
        for feature_date in sorted(grouped):
            group = sorted(grouped[feature_date], key=lambda item: (item.ticker, item.sample_id))
            selected_ids = _candidate_ids(
                group,
                risk_factor_name=self.config.risk_factor_name,
                candidate_fraction=self.selected_candidate_fraction,
                minimum_count=self.config.target_top_n,
            )
            candidate_scores = [item.composite_score for item in group if item.sample_id in selected_ids]
            floor = min(candidate_scores) - 1.0
            raw_scores = [
                item.composite_score
                if item.sample_id in selected_ids
                else floor + (_risk_value(item, self.config.risk_factor_name) * 1e-6)
                for item in group
            ]
            ranks = _percentile_ranks(raw_scores)
            predictions.extend(
                TwoStagePrediction(
                    sample_id=item.sample_id,
                    ticker=item.ticker,
                    feature_date=item.feature_date,
                    horizon_days=item.horizon_days,
                    raw_score=raw_score,
                    cross_sectional_rank=rank,
                    model_version=self.model_version,
                )
                for item, raw_score, rank in zip(group, raw_scores, ranks, strict=True)
            )
        return tuple(predictions)


def _risk_value(score: FactorScore, factor_name: str) -> float:
    try:
        value = float(score.factor_values[factor_name])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"risk factor {factor_name!r} is unavailable for {score.sample_id}") from exc
    if not math.isfinite(value):
        raise ValueError(f"risk factor {factor_name!r} is not finite for {score.sample_id}")
    return value


def _candidate_ids(
    group: list[FactorScore],
    *,
    risk_factor_name: str,
    candidate_fraction: float,
    minimum_count: int,
) -> set[str]:
    candidate_count = min(
        len(group),
        max(minimum_count, math.ceil(len(group) * candidate_fraction)),
    )
    ordered = sorted(
        group,
        key=lambda item: (
            -_risk_value(item, risk_factor_name),
            item.ticker,
            item.sample_id,
        ),
    )
    return {item.sample_id for item in ordered[:candidate_count]}


def _fraction_metric(
    groups: Mapping[date, list[FactorScore]],
    *,
    config: TwoStageConfig,
    candidate_fraction: float,
) -> TwoStageFractionMetric:
    daily_labels: list[float] = []
    for feature_date in sorted(groups):
        group = groups[feature_date]
        selected_ids = _candidate_ids(
            group,
            risk_factor_name=config.risk_factor_name,
            candidate_fraction=candidate_fraction,
            minimum_count=config.target_top_n,
        )
        selected = sorted(
            (item for item in group if item.sample_id in selected_ids),
            key=lambda item: (-item.composite_score, item.ticker, item.sample_id),
        )[: config.target_top_n]
        daily_labels.append(statistics.fmean(float(item.label_value) for item in selected))
    mean_label = statistics.fmean(daily_labels)
    standard_error = (
        statistics.pstdev(daily_labels) / math.sqrt(len(daily_labels))
        if len(daily_labels) > 1
        else 0.0
    )
    return TwoStageFractionMetric(
        candidate_fraction=candidate_fraction,
        evaluated_date_count=len(daily_labels),
        mean_top_n_label=mean_label,
        positive_date_rate=sum(value > 0 for value in daily_labels) / len(daily_labels),
        standard_error=standard_error,
        selection_objective=mean_label - (config.confidence_penalty * standard_error),
    )


def fit_two_stage_selector(
    scores: Iterable[FactorScore],
    *,
    config: TwoStageConfig,
    prediction_date: date,
) -> TwoStageSelectorModel:
    rows = [item for item in scores if item.horizon_days == config.horizon_days]
    if not rows:
        raise ValueError("two-stage training received no samples for configured horizon")
    sample_ids = [item.sample_id for item in rows]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("sample_id must be unique within two-stage input")
    unavailable = [
        item.sample_id
        for item in rows
        if item.label_available_date is None
        or item.label_value is None
        or item.label_available_date >= prediction_date
    ]
    if unavailable:
        raise ValueError(
            "two-stage training requires labels available before prediction_date: "
            + ", ".join(unavailable[:5])
        )
    if any(not math.isfinite(float(item.label_value)) for item in rows):
        raise ValueError("two-stage training labels must be finite")
    grouped: dict[date, list[FactorScore]] = defaultdict(list)
    for item in rows:
        _risk_value(item, config.risk_factor_name)
        grouped[item.feature_date].append(item)
    validation_dates = tuple(sorted(grouped)[-config.validation_dates :])
    if len(validation_dates) < config.minimum_validation_dates:
        raise ValueError("two-stage training has insufficient internal validation dates")
    validation_groups = {feature_date: grouped[feature_date] for feature_date in validation_dates}
    metrics = tuple(
        _fraction_metric(
            validation_groups,
            config=config,
            candidate_fraction=fraction,
        )
        for fraction in sorted(config.candidate_fractions)
    )
    # Prefer the less restrictive filter when validation objectives tie.
    selected_metric = max(
        metrics,
        key=lambda item: (item.selection_objective, item.candidate_fraction),
    )
    version_payload = {
        "config": asdict(config),
        "prediction_date": prediction_date.isoformat(),
        "training_end_date": max(item.feature_date for item in rows).isoformat(),
        "selected_candidate_fraction": selected_metric.candidate_fraction,
        "fraction_metrics": [asdict(item) for item in metrics],
    }
    digest = hashlib.sha256(
        json.dumps(version_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    audit = TwoStageTrainingAudit(
        prediction_date=prediction_date,
        training_sample_count=len(rows),
        validation_dates=validation_dates,
        selected_candidate_fraction=selected_metric.candidate_fraction,
        fraction_metrics=metrics,
    )
    return TwoStageSelectorModel(
        config=config,
        selected_candidate_fraction=selected_metric.candidate_fraction,
        audit=audit,
        model_version=f"two_stage_risk_filter_v1:{config.horizon_days}d:{digest}",
    )
