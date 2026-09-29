from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

from app.services.stock_selection.factor_pipeline import FactorScore, _percentile_ranks


@dataclass(frozen=True, slots=True)
class FactorDiagnosticConfig:
    minimum_cross_section_size: int = 20
    minimum_date_count: int = 20
    useful_abs_ic_threshold: float = 0.02
    positive_date_rate_threshold: float = 0.55
    redundancy_abs_correlation: float = 0.80
    target_component: str | None = None

    def __post_init__(self) -> None:
        if self.minimum_cross_section_size < 2 or self.minimum_date_count < 2:
            raise ValueError("factor diagnostic minimum counts must be at least two")
        if self.useful_abs_ic_threshold < 0:
            raise ValueError("useful_abs_ic_threshold must not be negative")
        if not 0.5 <= self.positive_date_rate_threshold <= 1.0:
            raise ValueError("positive_date_rate_threshold must be in [0.5, 1]")
        if not 0 < self.redundancy_abs_correlation <= 1.0:
            raise ValueError("redundancy_abs_correlation must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class FactorDailyIC:
    feature_date: date
    factor_name: str
    sample_count: int
    rank_ic: float | None


@dataclass(frozen=True, slots=True)
class FactorSummary:
    factor_name: str
    eligible_date_count: int
    ic_observation_count: int
    sample_coverage: float
    rank_ic_mean: float | None
    rank_ic_median: float | None
    rank_ic_std: float | None
    rank_ic_ir: float | None
    positive_date_rate: float | None
    recommendation: str


@dataclass(frozen=True, slots=True)
class FactorCorrelation:
    left_factor: str
    right_factor: str
    mean_rank_correlation: float
    observation_date_count: int


@dataclass(frozen=True, slots=True)
class FactorDiagnosticReport:
    horizon_days: int
    target_name: str
    evaluated_dates: tuple[date, ...]
    sample_count: int
    factor_summaries: Mapping[str, FactorSummary]
    high_correlation_pairs: tuple[FactorCorrelation, ...]
    daily_ics: tuple[FactorDailyIC, ...]


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    denominator = math.sqrt(sum((value - left_mean) ** 2 for value in left)) * math.sqrt(
        sum((value - right_mean) ** 2 for value in right)
    )
    return numerator / denominator if denominator > 1e-15 else None


def _rank_correlation(left: list[float], right: list[float]) -> float | None:
    return _pearson(_percentile_ranks(left), _percentile_ranks(right))


def _target_value(score: FactorScore, target_component: str | None) -> float | None:
    value = (
        score.label_components.get(target_component)
        if target_component
        else score.label_value
    )
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _recommendation(
    *,
    ic_values: list[float],
    config: FactorDiagnosticConfig,
) -> str:
    if len(ic_values) < config.minimum_date_count:
        return "insufficient_evidence"
    mean_ic = statistics.fmean(ic_values)
    positive_rate = sum(value > 0 for value in ic_values) / len(ic_values)
    negative_rate = sum(value < 0 for value in ic_values) / len(ic_values)
    if (
        mean_ic >= config.useful_abs_ic_threshold
        and positive_rate >= config.positive_date_rate_threshold
    ):
        return "keep"
    if (
        mean_ic <= -config.useful_abs_ic_threshold
        and negative_rate >= config.positive_date_rate_threshold
    ):
        return "review_direction"
    return "drop_or_observe"


def diagnose_factors(
    scores: Iterable[FactorScore],
    *,
    config: FactorDiagnosticConfig | None = None,
) -> FactorDiagnosticReport:
    settings = config or FactorDiagnosticConfig()
    rows = list(scores)
    if not rows:
        raise ValueError("factor diagnostics require at least one score")
    horizons = {item.horizon_days for item in rows}
    if len(horizons) != 1:
        raise ValueError("factor diagnostics require exactly one horizon")
    factor_names = sorted({name for item in rows for name in item.factor_values})
    if not factor_names:
        raise ValueError("factor diagnostics found no factor values")
    grouped: dict[date, list[FactorScore]] = defaultdict(list)
    for item in rows:
        grouped[item.feature_date].append(item)

    daily_ics: list[FactorDailyIC] = []
    ic_by_factor: dict[str, list[float]] = defaultdict(list)
    available_by_factor: dict[str, int] = defaultdict(int)
    eligible_samples_by_factor: dict[str, int] = defaultdict(int)
    pair_values: dict[tuple[str, str], list[float]] = defaultdict(list)
    evaluated_dates: list[date] = []
    for feature_date in sorted(grouped):
        group = grouped[feature_date]
        eligible = [item for item in group if _target_value(item, settings.target_component) is not None]
        if len(eligible) < settings.minimum_cross_section_size:
            continue
        evaluated_dates.append(feature_date)
        for factor_name in factor_names:
            factor_rows = [
                item for item in eligible if factor_name not in item.missing_factors
            ]
            eligible_samples_by_factor[factor_name] += len(eligible)
            available_by_factor[factor_name] += len(factor_rows)
            ic = None
            if len(factor_rows) >= settings.minimum_cross_section_size:
                values = [float(item.factor_values[factor_name]) for item in factor_rows]
                factor_targets = [float(_target_value(item, settings.target_component)) for item in factor_rows]
                ic = _rank_correlation(values, factor_targets)
                if ic is not None:
                    ic_by_factor[factor_name].append(ic)
            daily_ics.append(
                FactorDailyIC(
                    feature_date=feature_date,
                    factor_name=factor_name,
                    sample_count=len(factor_rows),
                    rank_ic=ic,
                )
            )
        for left_index, left_name in enumerate(factor_names):
            for right_name in factor_names[left_index + 1 :]:
                pair_rows = [
                    item
                    for item in eligible
                    if left_name not in item.missing_factors
                    and right_name not in item.missing_factors
                ]
                if len(pair_rows) < settings.minimum_cross_section_size:
                    continue
                correlation = _rank_correlation(
                    [float(item.factor_values[left_name]) for item in pair_rows],
                    [float(item.factor_values[right_name]) for item in pair_rows],
                )
                if correlation is not None:
                    pair_values[(left_name, right_name)].append(correlation)

    summaries: dict[str, FactorSummary] = {}
    for factor_name in factor_names:
        values = ic_by_factor.get(factor_name, [])
        mean_ic = statistics.fmean(values) if values else None
        std = statistics.pstdev(values) if len(values) > 1 else (0.0 if values else None)
        denominator = eligible_samples_by_factor.get(factor_name, 0)
        summaries[factor_name] = FactorSummary(
            factor_name=factor_name,
            eligible_date_count=len(evaluated_dates),
            ic_observation_count=len(values),
            sample_coverage=(available_by_factor.get(factor_name, 0) / denominator) if denominator else 0.0,
            rank_ic_mean=mean_ic,
            rank_ic_median=statistics.median(values) if values else None,
            rank_ic_std=std,
            rank_ic_ir=(mean_ic / std) if mean_ic is not None and std not in {None, 0.0} else None,
            positive_date_rate=(sum(value > 0 for value in values) / len(values)) if values else None,
            recommendation=_recommendation(ic_values=values, config=settings),
        )
    high_correlations = [
        FactorCorrelation(
            left_factor=left,
            right_factor=right,
            mean_rank_correlation=statistics.fmean(values),
            observation_date_count=len(values),
        )
        for (left, right), values in pair_values.items()
        if values and abs(statistics.fmean(values)) >= settings.redundancy_abs_correlation
    ]
    high_correlations.sort(
        key=lambda item: (-abs(item.mean_rank_correlation), item.left_factor, item.right_factor)
    )
    return FactorDiagnosticReport(
        horizon_days=next(iter(horizons)),
        target_name=settings.target_component or "label_value",
        evaluated_dates=tuple(evaluated_dates),
        sample_count=len(rows),
        factor_summaries=summaries,
        high_correlation_pairs=tuple(high_correlations),
        daily_ics=tuple(daily_ics),
    )
