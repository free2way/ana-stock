from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from sklearn.linear_model import Ridge

from app.services.stock_selection.factor_pipeline import FactorScore


@dataclass(frozen=True, slots=True)
class BaselinePrediction:
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    model_version: str


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

    def predict(self, scores: Iterable[FactorScore]) -> tuple[BaselinePrediction, ...]:
        rows = list(scores)
        if any(item.horizon_days != self.horizon_days for item in rows):
            raise ValueError("prediction scores must match model horizon")
        grouped: dict[date, list[tuple[FactorScore, float]]] = defaultdict(list)
        for item in rows:
            raw_score = self.intercept + sum(
                coefficient * float(item.factor_values.get(name, 0.0))
                for name, coefficient in zip(self.factor_names, self.coefficients, strict=True)
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
    x_train = [
        [float(item.factor_values.get(name, 0.0)) for name in factor_names]
        for item in rows
    ]
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
    version_payload = {
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
    )
