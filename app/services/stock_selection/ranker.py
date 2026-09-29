from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable, Mapping

from lightgbm import LGBMRanker

from app.services.stock_selection.factor_pipeline import FactorScore, _percentile_ranks


@dataclass(frozen=True, slots=True)
class RankerConfig:
    feature_names: tuple[str, ...]
    horizon_days: int
    relevance_grades: int = 5
    eval_at: tuple[int, ...] = (5, 10, 20)
    min_group_size: int = 5
    n_estimators: int = 200
    learning_rate: float = 0.03
    num_leaves: int = 15
    min_child_samples: int = 20
    subsample: float = 0.9
    colsample_bytree: float = 0.9
    reg_alpha: float = 0.1
    reg_lambda: float = 1.0
    random_seed: int = 42

    def __post_init__(self) -> None:
        if not self.feature_names or len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("feature_names must be non-empty and unique")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.relevance_grades < 2:
            raise ValueError("relevance_grades must be at least two")
        if self.min_group_size < 2:
            raise ValueError("min_group_size must be at least two")
        if not self.eval_at or any(value <= 0 for value in self.eval_at):
            raise ValueError("eval_at values must be positive")
        if self.n_estimators <= 0 or self.learning_rate <= 0 or self.num_leaves < 2:
            raise ValueError("ranker tree parameters must be positive")
        if self.min_child_samples <= 0:
            raise ValueError("min_child_samples must be positive")
        if not 0 < self.subsample <= 1 or not 0 < self.colsample_bytree <= 1:
            raise ValueError("sampling fractions must be in (0, 1]")
        if self.reg_alpha < 0 or self.reg_lambda < 0:
            raise ValueError("regularization values must not be negative")


@dataclass(frozen=True, slots=True)
class RankerTrainingAudit:
    prediction_date: date
    training_sample_count: int
    training_group_dates: tuple[date, ...]
    training_group_sizes: tuple[int, ...]
    skipped_groups: tuple[tuple[date, str], ...]
    relevance_grade_counts: Mapping[int, int]
    feature_importance_gain: Mapping[str, float]
    training_effective_sample_count: float
    sampling_config_version: str

    def __post_init__(self) -> None:
        if self.training_sample_count != sum(self.training_group_sizes):
            raise ValueError("training group sizes do not sum to training sample count")
        if len(self.training_group_dates) != len(self.training_group_sizes):
            raise ValueError("training group dates and sizes must align")
        if sum(self.relevance_grade_counts.values()) != self.training_sample_count:
            raise ValueError("relevance grade counts do not sum to training sample count")
        if not 0 < self.training_effective_sample_count <= self.training_sample_count + 1e-9:
            raise ValueError("invalid effective training sample count")
        if not str(self.sampling_config_version or "").strip():
            raise ValueError("sampling_config_version must not be empty")


@dataclass(frozen=True, slots=True)
class RankerPrediction:
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    model_version: str


@dataclass(frozen=True, slots=True)
class CrossSectionalRankerModel:
    config: RankerConfig
    estimator: LGBMRanker
    audit: RankerTrainingAudit
    model_version: str

    def predict(self, scores: Iterable[FactorScore]) -> tuple[RankerPrediction, ...]:
        rows = list(scores)
        wrong_horizon = [item.sample_id for item in rows if item.horizon_days != self.config.horizon_days]
        if wrong_horizon:
            raise ValueError(
                "prediction scores must match model horizon: " + ", ".join(wrong_horizon[:5])
            )
        if not rows:
            return ()
        _require_unique_sample_ids(rows)
        grouped: dict[date, list[FactorScore]] = defaultdict(list)
        for item in rows:
            grouped[item.feature_date].append(item)

        predictions: list[RankerPrediction] = []
        for feature_date in sorted(grouped):
            group = sorted(grouped[feature_date], key=lambda item: (item.ticker, item.sample_id))
            matrix = [_feature_row(item, self.config.feature_names) for item in group]
            raw_scores = [float(value) for value in self.estimator.booster_.predict(matrix)]
            ranks = _percentile_ranks(raw_scores)
            predictions.extend(
                RankerPrediction(
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


def _feature_row(score: FactorScore, feature_names: tuple[str, ...]) -> list[float]:
    row: list[float] = []
    for name in feature_names:
        raw_value = score.factor_values.get(name, 0.0)
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"factor {name!r} is not numeric for {score.sample_id}") from exc
        if not math.isfinite(value):
            raise ValueError(f"factor {name!r} is not finite for {score.sample_id}")
        row.append(value)
    return row


def _require_unique_sample_ids(scores: list[FactorScore]) -> None:
    sample_ids = [item.sample_id for item in scores]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("sample_id must be unique within ranker input")


def _relevance_labels(group: list[FactorScore], relevance_grades: int) -> list[int]:
    values = [float(item.label_value) for item in group if item.label_value is not None]
    if len(values) != len(group):
        raise ValueError("ranker training requires a matured label for every sample")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("ranker training labels must be finite")
    positions: dict[float, list[int]] = defaultdict(list)
    for position, value in enumerate(sorted(values)):
        positions[value].append(position)
    result: list[int] = []
    for value in values:
        average_position = sum(positions[value]) / len(positions[value])
        grade = min(relevance_grades - 1, int((average_position * relevance_grades) / len(values)))
        result.append(grade)
    return result


def fit_cross_sectional_ranker(
    scores: Iterable[FactorScore],
    *,
    config: RankerConfig,
    prediction_date: date,
    sample_weight_by_id: Mapping[str, float] | None = None,
    sampling_config_version: str = "legacy_uniform_row_weight_v1",
) -> CrossSectionalRankerModel:
    rows = [item for item in scores if item.horizon_days == config.horizon_days]
    if not rows:
        raise ValueError("ranker training received no samples for configured horizon")
    _require_unique_sample_ids(rows)
    if not str(sampling_config_version or "").strip():
        raise ValueError("sampling_config_version must not be empty")
    if sample_weight_by_id is None:
        all_sample_weights = {item.sample_id: 1.0 for item in rows}
    else:
        expected = {item.sample_id for item in rows}
        if set(sample_weight_by_id) != expected:
            raise ValueError("ranker sample weights must match training sample ids exactly")
        all_sample_weights = {
            sample_id: float(value) for sample_id, value in sample_weight_by_id.items()
        }
        if any(not math.isfinite(value) or value <= 0 for value in all_sample_weights.values()):
            raise ValueError("ranker sample weights must be finite and positive")

    unlabeled = [
        item.sample_id
        for item in rows
        if item.label_available_date is None or item.label_value is None
    ]
    if unlabeled:
        raise ValueError("ranker training requires matured labels: " + ", ".join(unlabeled[:5]))
    unavailable = [
        item.sample_id
        for item in rows
        if item.label_available_date is not None and item.label_available_date >= prediction_date
    ]
    if unavailable:
        raise ValueError(
            "ranker training received labels unavailable at prediction_date: "
            + ", ".join(unavailable[:5])
        )

    grouped: dict[date, list[FactorScore]] = defaultdict(list)
    for item in rows:
        grouped[item.feature_date].append(item)

    training_rows: list[FactorScore] = []
    relevance: list[int] = []
    group_dates: list[date] = []
    group_sizes: list[int] = []
    skipped: list[tuple[date, str]] = []
    for feature_date in sorted(grouped):
        group = sorted(grouped[feature_date], key=lambda item: (item.ticker, item.sample_id))
        if len(group) < config.min_group_size:
            skipped.append((feature_date, "below_min_group_size"))
            continue
        labels = [float(item.label_value) for item in group if item.label_value is not None]
        if max(labels) == min(labels):
            skipped.append((feature_date, "constant_label"))
            continue
        training_rows.extend(group)
        relevance.extend(_relevance_labels(group, config.relevance_grades))
        group_dates.append(feature_date)
        group_sizes.append(len(group))

    if not training_rows:
        raise ValueError("ranker training has no usable date groups")

    matrix = [_feature_row(item, config.feature_names) for item in training_rows]
    training_weights = [all_sample_weights[item.sample_id] for item in training_rows]
    estimator = LGBMRanker(
        objective="lambdarank",
        metric="ndcg",
        n_estimators=config.n_estimators,
        learning_rate=config.learning_rate,
        num_leaves=config.num_leaves,
        min_child_samples=config.min_child_samples,
        subsample=config.subsample,
        subsample_freq=1,
        colsample_bytree=config.colsample_bytree,
        reg_alpha=config.reg_alpha,
        reg_lambda=config.reg_lambda,
        random_state=config.random_seed,
        n_jobs=1,
        deterministic=True,
        force_col_wise=True,
        verbosity=-1,
    )
    estimator.fit(
        matrix,
        relevance,
        group=group_sizes,
        eval_at=config.eval_at,
        feature_name=list(config.feature_names),
        sample_weight=training_weights,
    )
    booster = estimator.booster_
    version_payload = json.dumps(
        {
            "config": asdict(config),
            "sampling_config_version": sampling_config_version,
            "sample_weights": [
                [item.sample_id, weight]
                for item, weight in sorted(
                    zip(training_rows, training_weights, strict=True),
                    key=lambda value: value[0].sample_id,
                )
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + booster.model_to_string()
    model_digest = hashlib.sha256(version_payload.encode("utf-8")).hexdigest()[:16]
    importance = booster.feature_importance(importance_type="gain")
    relevance_grade_counts = {
        grade: relevance.count(grade)
        for grade in range(config.relevance_grades)
    }
    audit = RankerTrainingAudit(
        prediction_date=prediction_date,
        training_sample_count=len(training_rows),
        training_group_dates=tuple(group_dates),
        training_group_sizes=tuple(group_sizes),
        skipped_groups=tuple(skipped),
        relevance_grade_counts=relevance_grade_counts,
        feature_importance_gain={
            name: float(value)
            for name, value in zip(config.feature_names, importance, strict=True)
        },
        training_effective_sample_count=(
            sum(training_weights) ** 2 / sum(value * value for value in training_weights)
        ),
        sampling_config_version=sampling_config_version,
    )
    return CrossSectionalRankerModel(
        config=config,
        estimator=estimator,
        audit=audit,
        model_version=f"lightgbm_lambdarank_v1:{config.horizon_days}d:{model_digest}",
    )
