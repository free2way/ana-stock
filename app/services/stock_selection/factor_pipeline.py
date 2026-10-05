from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Iterable, Mapping

from app.services.stock_selection.schemas import LabeledSample


class FactorDirection(StrEnum):
    HIGHER_BETTER = "higher_better"
    LOWER_BETTER = "lower_better"


class MissingFactorPolicy(StrEnum):
    """How an unknown (``None``/non-finite) factor cell joins the composite score.

    ``NEUTRAL_ZERO`` is the legacy, explicitly-audited contract: a missing factor
    is scored as the neutral ``0.0`` z-score and the sample's ``missing_factors``
    records it. ``EXCLUDE`` omits the factor from ``factor_values`` and
    renormalizes the composite over the present factors, so an unknown value can
    never masquerade as a real zero. Sparse, forward-only families such as the
    bounded ``sentiment_v1`` window must use ``EXCLUDE`` because their
    out-of-coverage cells are legitimately unknown rather than zero.
    """

    NEUTRAL_ZERO = "neutral_zero"
    EXCLUDE = "exclude"


@dataclass(frozen=True, slots=True)
class FactorSpec:
    name: str
    direction: FactorDirection = FactorDirection.HIGHER_BETTER
    weight: float = 1.0
    winsor_lower: float = 0.025
    winsor_upper: float = 0.975

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("factor name must not be empty")
        if self.weight < 0:
            raise ValueError("factor weight must not be negative")
        if not 0.0 <= self.winsor_lower < self.winsor_upper <= 1.0:
            raise ValueError("winsor bounds must satisfy 0 <= lower < upper <= 1")


@dataclass(frozen=True, slots=True)
class FactorObservation:
    """Point-in-time factor row, optionally carrying a matured research label."""

    observation_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    features: Mapping[str, float]
    label_available_date: date | None = None
    label_value: float | None = None
    label_components: Mapping[str, float] = field(default_factory=dict)
    eligible: bool = True

    def __post_init__(self) -> None:
        if not str(self.observation_id or "").strip():
            raise ValueError("observation_id must not be empty")
        if not str(self.ticker or "").strip():
            raise ValueError("ticker must not be empty")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if (self.label_available_date is None) != (self.label_value is None):
            raise ValueError("label_available_date and label_value must be provided together")
        if self.label_components and self.label_value is None:
            raise ValueError("unlabeled observations cannot carry label components")

    @classmethod
    def from_labeled_sample(cls, sample: LabeledSample) -> FactorObservation:
        return cls(
            observation_id=sample.sample_id,
            ticker=sample.ticker,
            feature_date=sample.feature_date,
            horizon_days=sample.horizon_days,
            features=sample.features,
            label_available_date=sample.label_available_date,
            label_value=sample.label_value,
            label_components=sample.label_components,
            eligible=sample.tradable,
        )


@dataclass(frozen=True, slots=True)
class FactorScore:
    sample_id: str
    ticker: str
    feature_date: date
    label_available_date: date | None
    horizon_days: int
    factor_values: Mapping[str, float]
    missing_factors: tuple[str, ...]
    composite_score: float
    cross_sectional_rank: float
    label_value: float | None
    label_components: Mapping[str, float] = field(default_factory=dict)
    # Explicit record of how missing factors were treated; defaults to the legacy
    # neutral-zero contract so existing direct constructors stay valid.
    missing_policy: str = MissingFactorPolicy.NEUTRAL_ZERO.value


def _quantile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("cannot calculate quantile of empty values")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return sorted_values[lower_index] * (1.0 - fraction) + sorted_values[upper_index] * fraction


def _percentile_ranks(values: list[float]) -> list[float]:
    if not values:
        return []
    if len(values) == 1:
        return [0.5]
    positions: dict[float, list[int]] = defaultdict(list)
    for position, value in enumerate(sorted(values)):
        positions[value].append(position)
    rank_by_value = {
        value: (sum(value_positions) / len(value_positions)) / (len(values) - 1)
        for value, value_positions in positions.items()
    }
    return [rank_by_value[value] for value in values]


class CrossSectionalFactorPipeline:
    """Fit-free same-date transforms that cannot borrow another date's data."""

    def __init__(
        self,
        specs: Iterable[FactorSpec],
        *,
        zscore_clip: float = 3.0,
        missing_policy: MissingFactorPolicy | str = MissingFactorPolicy.NEUTRAL_ZERO,
    ) -> None:
        self.specs = tuple(specs)
        if not self.specs:
            raise ValueError("at least one factor spec is required")
        names = [item.name for item in self.specs]
        if len(set(names)) != len(names):
            raise ValueError("factor names must be unique")
        if zscore_clip <= 0:
            raise ValueError("zscore_clip must be positive")
        if sum(item.weight for item in self.specs) <= 0:
            raise ValueError("at least one factor must have positive weight")
        try:
            self.missing_policy = MissingFactorPolicy(missing_policy)
        except ValueError as exc:
            raise ValueError(f"unsupported missing_policy: {missing_policy!r}") from exc
        self.zscore_clip = zscore_clip

    def transform(
        self,
        samples: Iterable[LabeledSample | FactorObservation],
        *,
        eligible_only: bool = True,
    ) -> tuple[FactorScore, ...]:
        grouped: dict[tuple[date, int], list[FactorObservation]] = defaultdict(list)
        for sample in samples:
            observation = (
                FactorObservation.from_labeled_sample(sample)
                if isinstance(sample, LabeledSample)
                else sample
            )
            if eligible_only and not observation.eligible:
                continue
            grouped[(observation.feature_date, observation.horizon_days)].append(observation)

        output: list[FactorScore] = []
        total_weight = sum(item.weight for item in self.specs)
        weight_by_name = {item.name: item.weight for item in self.specs}
        exclude_missing = self.missing_policy == MissingFactorPolicy.EXCLUDE
        for group_key in sorted(grouped):
            group = sorted(grouped[group_key], key=lambda item: (item.ticker, item.observation_id))
            normalized_by_factor: dict[str, dict[str, float]] = {}
            missing_by_sample: dict[str, list[str]] = defaultdict(list)
            for spec in self.specs:
                valid: dict[str, float] = {}
                for sample in group:
                    raw_value = sample.features.get(spec.name)
                    try:
                        value = float(raw_value) if raw_value is not None else math.nan
                    except (TypeError, ValueError):
                        value = math.nan
                    if math.isfinite(value):
                        valid[sample.observation_id] = value
                    else:
                        missing_by_sample[sample.observation_id].append(spec.name)
                normalized: dict[str, float] = {}
                if valid:
                    sorted_values = sorted(valid.values())
                    lower = _quantile(sorted_values, spec.winsor_lower)
                    upper = _quantile(sorted_values, spec.winsor_upper)
                    clipped = {key: max(lower, min(upper, value)) for key, value in valid.items()}
                    median = statistics.median(clipped.values())
                    absolute_deviations = [abs(value - median) for value in clipped.values()]
                    scale = statistics.median(absolute_deviations) * 1.4826
                    if scale <= 1e-12 and len(clipped) > 1:
                        scale = statistics.pstdev(clipped.values())
                    scale = scale if scale > 1e-12 else 1.0
                    direction = -1.0 if spec.direction == FactorDirection.LOWER_BETTER else 1.0
                    normalized = {
                        key: max(-self.zscore_clip, min(self.zscore_clip, ((value - median) / scale) * direction))
                        for key, value in clipped.items()
                    }
                normalized_by_factor[spec.name] = normalized

            composites: list[float] = []
            factor_rows: list[dict[str, float]] = []
            for sample in group:
                present = {
                    spec.name: normalized_by_factor[spec.name][sample.observation_id]
                    for spec in self.specs
                    if sample.observation_id in normalized_by_factor[spec.name]
                }
                if exclude_missing:
                    # Unknown cells are omitted entirely; renormalize over the
                    # factors that are actually present so a missing value never
                    # enters the composite as a functional zero.
                    values = dict(present)
                    present_weight = sum(weight_by_name[name] for name in present)
                    composite = (
                        sum(present[name] * weight_by_name[name] for name in present)
                        / present_weight
                        if present_weight > 0
                        else 0.0
                    )
                else:
                    values = {
                        spec.name: present.get(spec.name, 0.0) for spec in self.specs
                    }
                    composite = (
                        sum(values[spec.name] * spec.weight for spec in self.specs)
                        / total_weight
                    )
                factor_rows.append(values)
                composites.append(composite)
            ranks = _percentile_ranks(composites)
            output.extend(
                FactorScore(
                    sample_id=sample.observation_id,
                    ticker=sample.ticker,
                    feature_date=sample.feature_date,
                    label_available_date=sample.label_available_date,
                    horizon_days=sample.horizon_days,
                    factor_values=factor_values,
                    missing_factors=tuple(missing_by_sample.get(sample.observation_id, [])),
                    composite_score=composite,
                    cross_sectional_rank=rank,
                    label_value=sample.label_value,
                    label_components=sample.label_components,
                    missing_policy=self.missing_policy.value,
                )
                for sample, factor_values, composite, rank in zip(
                    group,
                    factor_rows,
                    composites,
                    ranks,
                    strict=True,
                )
            )
        return tuple(output)
