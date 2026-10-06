from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable, Mapping

from lightgbm import LGBMClassifier

from app.services.stock_selection.factor_pipeline import (
    FactorScore,
    MissingFactorPolicy,
    _percentile_ranks,
    model_feature_row,
    resolve_model_missing_policy,
)


@dataclass(frozen=True, slots=True)
class TopTailConfig:
    feature_names: tuple[str, ...]
    horizon_days: int
    target_top_n: int = 5
    min_group_size: int = 20
    n_estimators: int = 80
    learning_rate: float = 0.03
    num_leaves: int = 7
    min_child_samples: int = 50
    subsample: float = 0.9
    colsample_bytree: float = 0.9
    reg_alpha: float = 0.2
    reg_lambda: float = 2.0
    random_seed: int = 42

    def __post_init__(self) -> None:
        if not self.feature_names or len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("feature_names must be non-empty and unique")
        if self.horizon_days <= 0 or self.target_top_n <= 0:
            raise ValueError("horizon_days and target_top_n must be positive")
        if self.min_group_size < self.target_top_n:
            raise ValueError("min_group_size must be at least target_top_n")
        if self.n_estimators <= 0 or self.learning_rate <= 0 or self.num_leaves < 2:
            raise ValueError("top-tail tree parameters must be positive")
        if self.min_child_samples <= 0:
            raise ValueError("min_child_samples must be positive")
        if not 0 < self.subsample <= 1 or not 0 < self.colsample_bytree <= 1:
            raise ValueError("sampling fractions must be in (0, 1]")
        if self.reg_alpha < 0 or self.reg_lambda < 0:
            raise ValueError("regularization values must not be negative")


@dataclass(frozen=True, slots=True)
class TopTailTrainingAudit:
    prediction_date: date
    training_sample_count: int
    training_group_dates: tuple[date, ...]
    training_group_sizes: tuple[int, ...]
    skipped_groups: tuple[tuple[date, str], ...]
    positive_label_count: int
    negative_label_count: int
    feature_importance_gain: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class TopTailPrediction:
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    model_version: str


@dataclass(frozen=True, slots=True)
class TopTailClassifierModel:
    config: TopTailConfig
    estimator: LGBMClassifier
    audit: TopTailTrainingAudit
    model_version: str
    # Explicit record of the cross-sectional missing contract the matrix was fit
    # under; defaults to the legacy neutral-zero contract for direct constructors.
    missing_policy: str = MissingFactorPolicy.NEUTRAL_ZERO.value

    def predict(self, scores: Iterable[FactorScore]) -> tuple[TopTailPrediction, ...]:
        rows = list(scores)
        if any(item.horizon_days != self.config.horizon_days for item in rows):
            raise ValueError("prediction scores must match top-tail model horizon")
        if not rows:
            return ()
        if resolve_model_missing_policy(rows) != MissingFactorPolicy(self.missing_policy):
            raise ValueError(
                "prediction scores do not share the model's fitted missing-factor policy"
            )
        _require_unique_sample_ids(rows)
        grouped: dict[date, list[FactorScore]] = defaultdict(list)
        for item in rows:
            grouped[item.feature_date].append(item)
        predictions: list[TopTailPrediction] = []
        for feature_date in sorted(grouped):
            group = sorted(grouped[feature_date], key=lambda item: (item.ticker, item.sample_id))
            matrix = [_feature_row(item, self.config.feature_names) for item in group]
            raw_scores = [float(value) for value in self.estimator.booster_.predict(matrix)]
            ranks = _percentile_ranks(raw_scores)
            predictions.extend(
                TopTailPrediction(
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
    # ``EXCLUDE`` panels carry NaN for genuinely unknown cells (LightGBM splits
    # on them natively); the legacy ``NEUTRAL_ZERO`` panel still zero-fills.
    return model_feature_row(score, feature_names)


def _require_unique_sample_ids(scores: list[FactorScore]) -> None:
    sample_ids = [item.sample_id for item in scores]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("sample_id must be unique within top-tail input")


def fit_top_tail_classifier(
    scores: Iterable[FactorScore],
    *,
    config: TopTailConfig,
    prediction_date: date,
) -> TopTailClassifierModel:
    rows = [item for item in scores if item.horizon_days == config.horizon_days]
    if not rows:
        raise ValueError("top-tail training received no samples for configured horizon")
    _require_unique_sample_ids(rows)
    unlabeled = [
        item.sample_id
        for item in rows
        if item.label_available_date is None or item.label_value is None
    ]
    if unlabeled:
        raise ValueError("top-tail training requires matured labels: " + ", ".join(unlabeled[:5]))
    unavailable = [
        item.sample_id
        for item in rows
        if item.label_available_date is not None and item.label_available_date >= prediction_date
    ]
    if unavailable:
        raise ValueError(
            "top-tail training received labels unavailable at prediction_date: "
            + ", ".join(unavailable[:5])
        )

    grouped: dict[date, list[FactorScore]] = defaultdict(list)
    for item in rows:
        grouped[item.feature_date].append(item)
    training_rows: list[FactorScore] = []
    targets: list[int] = []
    sample_weights: list[float] = []
    group_dates: list[date] = []
    group_sizes: list[int] = []
    skipped: list[tuple[date, str]] = []
    for feature_date in sorted(grouped):
        group = sorted(grouped[feature_date], key=lambda item: (item.ticker, item.sample_id))
        if len(group) < config.min_group_size:
            skipped.append((feature_date, "below_min_group_size"))
            continue
        labels = [float(item.label_value) for item in group if item.label_value is not None]
        if len(labels) != len(group) or any(not math.isfinite(value) for value in labels):
            raise ValueError("top-tail training labels must be finite")
        positive_ids = {
            item.sample_id
            for item in sorted(
                (item for item in group if float(item.label_value or 0.0) > 0.0),
                key=lambda item: (-float(item.label_value or 0.0), item.ticker, item.sample_id),
            )[: config.target_top_n]
        }
        positive_count = len(positive_ids)
        negative_count = len(group) - positive_count
        if negative_count == 0:
            skipped.append((feature_date, "no_negative_class"))
            continue
        training_rows.extend(group)
        group_targets = [1 if item.sample_id in positive_ids else 0 for item in group]
        targets.extend(group_targets)
        if positive_count:
            sample_weights.extend(
                0.5 / positive_count if target else 0.5 / negative_count
                for target in group_targets
            )
        else:
            sample_weights.extend(1.0 / negative_count for _ in group)
        group_dates.append(feature_date)
        group_sizes.append(len(group))

    positive_label_count = sum(targets)
    if not training_rows or positive_label_count == 0:
        raise ValueError("top-tail training has no positive head labels")
    missing_policy = resolve_model_missing_policy(training_rows)
    matrix = [_feature_row(item, config.feature_names) for item in training_rows]
    estimator = LGBMClassifier(
        objective="binary",
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
        targets,
        sample_weight=sample_weights,
        feature_name=list(config.feature_names),
    )
    booster = estimator.booster_
    if missing_policy == MissingFactorPolicy.NEUTRAL_ZERO:
        # Byte-identical to the pre-EXCLUDE payload so unchanged price/P1 panels
        # are not re-versioned.
        version_payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    else:
        version_payload = json.dumps(
            {"config": asdict(config), "missing_policy": missing_policy.value},
            sort_keys=True,
            separators=(",", ":"),
        )
    version_payload += booster.model_to_string()
    model_digest = hashlib.sha256(version_payload.encode("utf-8")).hexdigest()[:16]
    importance = booster.feature_importance(importance_type="gain")
    audit = TopTailTrainingAudit(
        prediction_date=prediction_date,
        training_sample_count=len(training_rows),
        training_group_dates=tuple(group_dates),
        training_group_sizes=tuple(group_sizes),
        skipped_groups=tuple(skipped),
        positive_label_count=positive_label_count,
        negative_label_count=len(targets) - positive_label_count,
        feature_importance_gain={
            name: float(value)
            for name, value in zip(config.feature_names, importance, strict=True)
        },
    )
    return TopTailClassifierModel(
        config=config,
        estimator=estimator,
        audit=audit,
        model_version=f"lightgbm_top_tail_v1:{config.horizon_days}d:{model_digest}",
        missing_policy=missing_policy.value,
    )
