from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from sklearn.linear_model import Ridge

from app.services.stock_selection.factor_pipeline import (
    FactorScore,
    MissingFactorPolicy,
    resolve_model_missing_policy,
)


@dataclass(frozen=True, slots=True)
class BaselinePrediction:
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    model_version: str


MISSING_INDICATOR_SUFFIX = "__missing"


def _finite_or_nan(raw_value: object) -> float:
    """Coerce a factor cell to a float, mapping unknown/non-finite to ``NaN``."""

    if raw_value is None:
        return math.nan
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def _legacy_feature_row(score: FactorScore, factor_names: tuple[str, ...]) -> list[float]:
    """Legacy ``NEUTRAL_ZERO`` row: an absent cell enters as ``0.0``."""

    return [float(score.factor_values.get(name, 0.0)) for name in factor_names]


def _training_medians(
    rows: list[FactorScore], factor_names: tuple[str, ...]
) -> tuple[float, ...]:
    """Training-window median per factor, computed only from present cells.

    The median is fit on the training rows handed to ``fit_ridge_factor_baseline``
    (already purged/point-in-time safe) and stored on the model, so scoring uses
    the training-window statistic rather than any inference-time value.
    """

    medians: list[float] = []
    for name in factor_names:
        present = [
            value
            for value in (
                _finite_or_nan(item.factor_values.get(name)) for item in rows
            )
            if math.isfinite(value)
        ]
        medians.append(float(statistics.median(present)) if present else 0.0)
    return tuple(medians)


def _exclude_feature_row(
    score: FactorScore,
    factor_names: tuple[str, ...],
    medians: tuple[float, ...],
) -> list[float]:
    """``EXCLUDE`` design row: median-impute unknown cells + explicit indicators.

    An unknown cell never becomes a fabricated cross-sectional ``0.0``; it is
    replaced by the training-window median and flagged with a trailing ``1.0``
    indicator, so the linear model can separate "unknown" from "real zero".
    """

    row: list[float] = []
    indicators: list[float] = []
    for index, name in enumerate(factor_names):
        value = _finite_or_nan(score.factor_values.get(name))
        if math.isfinite(value):
            row.append(value)
            indicators.append(0.0)
        else:
            row.append(medians[index] if index < len(medians) else 0.0)
            indicators.append(1.0)
    return row + indicators


def indicator_feature_names(factor_names: tuple[str, ...]) -> tuple[str, ...]:
    """Names of the trailing missing-indicator columns for an ``EXCLUDE`` model."""

    return tuple(f"{name}{MISSING_INDICATOR_SUFFIX}" for name in factor_names)


@dataclass(frozen=True, slots=True)
class RidgeFactorModel:
    factor_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    intercept: float
    alpha: float
    horizon_days: int
    training_sample_count: int
    training_end_date: date
    training_effective_sample_count: float
    sampling_config_version: str
    model_version: str
    # Explicit record of the cross-sectional missing contract the design matrix
    # was fit under; defaults to the legacy neutral-zero contract.
    missing_policy: str = MissingFactorPolicy.NEUTRAL_ZERO.value
    # Training-window medians used to impute unknown cells under ``EXCLUDE``;
    # empty for the legacy ``NEUTRAL_ZERO`` contract.
    feature_medians: tuple[float, ...] = ()

    def predict(self, scores: Iterable[FactorScore]) -> tuple[BaselinePrediction, ...]:
        rows = list(scores)
        if any(item.horizon_days != self.horizon_days for item in rows):
            raise ValueError("prediction scores must match model horizon")
        if not rows:
            return ()
        policy = MissingFactorPolicy(self.missing_policy)
        if resolve_model_missing_policy(rows) != policy:
            raise ValueError(
                "prediction scores do not share the model's fitted missing-factor policy"
            )
        grouped: dict[date, list[tuple[FactorScore, float]]] = defaultdict(list)
        for item in rows:
            if policy == MissingFactorPolicy.EXCLUDE:
                features = _exclude_feature_row(item, self.factor_names, self.feature_medians)
            else:
                features = _legacy_feature_row(item, self.factor_names)
            raw_score = self.intercept + sum(
                coefficient * value
                for coefficient, value in zip(self.coefficients, features, strict=True)
            )
            grouped[item.feature_date].append((item, raw_score))
        predictions: list[BaselinePrediction] = []
        for feature_date in sorted(grouped):
            group = sorted(grouped[feature_date], key=lambda item: (item[0].ticker, item[0].sample_id))
            raw_values = [item[1] for item in group]
            ranks = _ranks(raw_values)
            predictions.extend(
                BaselinePrediction(
                    sample_id=item.sample_id,
                    ticker=item.ticker,
                    feature_date=item.feature_date,
                    horizon_days=item.horizon_days,
                    raw_score=raw_score,
                    cross_sectional_rank=rank,
                    model_version=self.model_version,
                )
                for (item, raw_score), rank in zip(group, ranks, strict=True)
            )
        return tuple(predictions)


def equal_weight_predictions(
    scores: Iterable[FactorScore],
    *,
    model_version: str = "factor_equal_weight_v1",
) -> tuple[BaselinePrediction, ...]:
    return tuple(
        BaselinePrediction(
            sample_id=item.sample_id,
            ticker=item.ticker,
            feature_date=item.feature_date,
            horizon_days=item.horizon_days,
            raw_score=item.composite_score,
            cross_sectional_rank=item.cross_sectional_rank,
            model_version=model_version,
        )
        for item in scores
    )


def _ranks(values: list[float]) -> list[float]:
    if not values:
        return []
    if len(values) == 1:
        return [0.5]
    positions: dict[float, list[int]] = defaultdict(list)
    for position, value in enumerate(sorted(values)):
        positions[value].append(position)
    by_value = {
        value: (sum(items) / len(items)) / (len(values) - 1)
        for value, items in positions.items()
    }
    return [by_value[value] for value in values]


def _rank_targets(scores: list[FactorScore]) -> Mapping[str, float]:
    grouped: dict[date, list[FactorScore]] = defaultdict(list)
    for item in scores:
        grouped[item.feature_date].append(item)
    result: dict[str, float] = {}
    for feature_date in sorted(grouped):
        group = sorted(grouped[feature_date], key=lambda item: (item.ticker, item.sample_id))
        labels = [item.label_value for item in group]
        if any(value is None for value in labels):
            raise ValueError("ridge training requires a matured label for every sample")
        ranks = _ranks([float(value) for value in labels if value is not None])
        result.update({item.sample_id: rank for item, rank in zip(group, ranks, strict=True)})
    return result


def fit_ridge_factor_baseline(
    scores: Iterable[FactorScore],
    *,
    factor_names: tuple[str, ...],
    horizon_days: int,
    prediction_date: date,
    alpha: float = 10.0,
    sample_weight_by_id: Mapping[str, float] | None = None,
    sampling_config_version: str = "legacy_uniform_row_weight_v1",
) -> RidgeFactorModel:
    rows = [item for item in scores if item.horizon_days == horizon_days]
    if len(rows) < 2:
        raise ValueError("ridge baseline requires at least two training samples")
    if not factor_names or len(set(factor_names)) != len(factor_names):
        raise ValueError("factor_names must be non-empty and unique")
    if alpha < 0:
        raise ValueError("ridge alpha must not be negative")
    if not str(sampling_config_version or "").strip():
        raise ValueError("sampling_config_version must not be empty")
    unlabeled = [
        item.sample_id
        for item in rows
        if item.label_available_date is None or item.label_value is None
    ]
    if unlabeled:
        raise ValueError(
            "ridge training requires matured labels: " + ", ".join(unlabeled[:5])
        )
    unavailable = [
        item.sample_id
        for item in rows
        if item.label_available_date is not None and item.label_available_date >= prediction_date
    ]
    if unavailable:
        raise ValueError(
            "ridge training received labels unavailable at prediction_date: "
            + ", ".join(unavailable[:5])
        )
    target_by_id = _rank_targets(rows)
    missing_policy = resolve_model_missing_policy(rows)
    if missing_policy == MissingFactorPolicy.EXCLUDE:
        # Unknown cells are median-imputed from the training window and flagged
        # with an explicit missing-indicator column; sample count is unchanged.
        feature_medians = _training_medians(rows, factor_names)
        x_train = [
            _exclude_feature_row(item, factor_names, feature_medians) for item in rows
        ]
    else:
        feature_medians = ()
        x_train = [_legacy_feature_row(item, factor_names) for item in rows]
    y_train = [target_by_id[item.sample_id] for item in rows]
    if sample_weight_by_id is None:
        sample_weights = [1.0] * len(rows)
    else:
        expected = {item.sample_id for item in rows}
        if set(sample_weight_by_id) != expected:
            raise ValueError("ridge sample weights must match training sample ids exactly")
        sample_weights = [float(sample_weight_by_id[item.sample_id]) for item in rows]
        if any(not math.isfinite(value) or value <= 0 for value in sample_weights):
            raise ValueError("ridge sample weights must be finite and positive")
    estimator = Ridge(alpha=alpha, fit_intercept=True)
    estimator.fit(x_train, y_train, sample_weight=sample_weights)
    coefficients = tuple(float(value) for value in estimator.coef_)
    intercept = float(estimator.intercept_)
    training_end_date = max(item.feature_date for item in rows)
    version_payload: dict[str, object] = {
        "family": "factor_ridge_v1",
        "factor_names": factor_names,
        "coefficients": coefficients,
        "intercept": intercept,
        "alpha": alpha,
        "horizon_days": horizon_days,
        "training_sample_count": len(rows),
        "training_end_date": training_end_date.isoformat(),
        "sampling_config_version": sampling_config_version,
        "sample_weights": [
            [item.sample_id, weight]
            for item, weight in sorted(
                zip(rows, sample_weights, strict=True), key=lambda value: value[0].sample_id,
            )
        ],
    }
    if missing_policy != MissingFactorPolicy.NEUTRAL_ZERO:
        # Only the EXCLUDE contract changes the design matrix; the legacy digest
        # is kept byte-identical so unchanged price/P1 panels are not re-versioned.
        version_payload["missing_policy"] = missing_policy.value
        version_payload["feature_medians"] = list(feature_medians)
    digest = hashlib.sha256(
        json.dumps(version_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return RidgeFactorModel(
        factor_names=factor_names,
        coefficients=coefficients,
        intercept=intercept,
        alpha=alpha,
        horizon_days=horizon_days,
        training_sample_count=len(rows),
        training_end_date=training_end_date,
        training_effective_sample_count=(
            sum(sample_weights) ** 2 / sum(value * value for value in sample_weights)
        ),
        sampling_config_version=sampling_config_version,
        model_version=f"factor_ridge_v1:{horizon_days}d:{digest}",
        missing_policy=missing_policy.value,
        feature_medians=feature_medians,
    )
