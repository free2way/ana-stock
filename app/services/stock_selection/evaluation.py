from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Protocol

from app.services.statistical_inference import significance_report
from app.services.stock_selection.factor_pipeline import FactorScore, _percentile_ranks


class PredictionLike(Protocol):
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    model_version: str


@dataclass(frozen=True, slots=True)
class CrossSectionalEvaluationConfig:
    model_key: str
    top_ns: tuple[int, ...] = (5, 10, 20)
    quantile_count: int = 5

    def __post_init__(self) -> None:
        if not str(self.model_key or "").strip():
            raise ValueError("model_key must not be empty")
        if not self.top_ns or any(value <= 0 for value in self.top_ns):
            raise ValueError("top_ns must contain positive values")
        if len(set(self.top_ns)) != len(self.top_ns):
            raise ValueError("top_ns must not contain duplicates")
        if self.quantile_count < 2:
            raise ValueError("quantile_count must be at least two")


@dataclass(frozen=True, slots=True)
class ContributionRecord:
    key: str
    mean_contribution: float
    observation_count: int


@dataclass(frozen=True, slots=True)
class TopNMetrics:
    top_n: int
    evaluated_date_count: int
    average_selected_count: float
    mean_label: float
    median_label: float
    positive_date_rate: float
    positive_oracle_precision_at_n: float
    average_positive_oracle_count: float
    average_one_way_turnover: float | None
    top_ticker_contributions: tuple[ContributionRecord, ...]
    top_date_contributions: tuple[ContributionRecord, ...]
    largest_five_abs_contribution_share: float
    max_repeat_frequency: float
    mean_label_components: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class DailyCrossSectionMetrics:
    feature_date: date
    sample_count: int
    rank_ic: float | None
    quantile_mean_labels: Mapping[int, float]
    top_n_mean_labels: Mapping[int, float]
    top_n_positive_oracle_precision: Mapping[int, float]
    top_n_mean_label_components: Mapping[int, Mapping[str, float]]


@dataclass(frozen=True, slots=True)
class CrossSectionalEvaluationReport:
    model_key: str
    model_versions: tuple[str, ...]
    horizon_days: int
    evaluated_dates: tuple[date, ...]
    sample_count: int
    rank_ic_observation_count: int
    rank_ic_mean: float | None
    rank_ic_median: float | None
    rank_ic_std: float | None
    rank_ic_ir: float | None
    rank_ic_ci95: tuple[float, float] | None
    quantile_mean_labels: Mapping[int, float]
    quantile_monotonicity: float | None
    top_minus_bottom_mean: float | None
    top_n_metrics: Mapping[int, TopNMetrics]
    daily_metrics: tuple[DailyCrossSectionMetrics, ...]
    overall_label_component_means: Mapping[str, float]
    label_component_coverage: Mapping[str, float]
    turnover_definition: str = "0.5 * sum(abs(equal_weight_t - equal_weight_t_minus_1))"
    significance: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _EvaluationRow:
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    model_version: str
    label_value: float
    label_components: Mapping[str, float]


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    denominator = left_scale * right_scale
    if denominator <= 1e-15:
        return None
    return numerator / denominator


def _rank_ic(rows: list[_EvaluationRow]) -> float | None:
    prediction_ranks = _percentile_ranks([item.raw_score for item in rows])
    label_ranks = _percentile_ranks([item.label_value for item in rows])
    return _pearson(prediction_ranks, label_ranks)


def _quantile_groups(
    rows: list[_EvaluationRow],
    quantile_count: int,
) -> dict[int, list[_EvaluationRow]]:
    ordered = sorted(rows, key=lambda item: (item.raw_score, item.ticker, item.sample_id))
    grouped: dict[int, list[_EvaluationRow]] = defaultdict(list)
    for index, row in enumerate(ordered):
        if len(ordered) < quantile_count and len(ordered) > 1:
            quantile = round(index * (quantile_count - 1) / (len(ordered) - 1)) + 1
        else:
            quantile = min(quantile_count, int(index * quantile_count / len(ordered)) + 1)
        grouped[quantile].append(row)
    return grouped


def _one_way_turnover(previous: list[_EvaluationRow], current: list[_EvaluationRow]) -> float:
    previous_weight = 1.0 / len(previous) if previous else 0.0
    current_weight = 1.0 / len(current) if current else 0.0
    tickers = {item.ticker for item in previous} | {item.ticker for item in current}
    previous_tickers = {item.ticker for item in previous}
    current_tickers = {item.ticker for item in current}
    absolute_weight_change = sum(
        abs(
            (current_weight if ticker in current_tickers else 0.0)
            - (previous_weight if ticker in previous_tickers else 0.0)
        )
        for ticker in tickers
    )
    return 0.5 * absolute_weight_change


def _mean_or_none(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _mean_components(rows: list[_EvaluationRow]) -> dict[str, float]:
    if not rows:
        return {}
    common_names = set(rows[0].label_components)
    for row in rows[1:]:
        common_names &= set(row.label_components)
    return {
        name: statistics.fmean(float(item.label_components[name]) for item in rows)
        for name in sorted(common_names)
    }


def _build_top_n_metrics(
    rows_by_date: Mapping[date, list[_EvaluationRow]],
    *,
    top_n: int,
) -> TopNMetrics:
    selected_by_date: dict[date, list[_EvaluationRow]] = {}
    daily_labels: list[float] = []
    ticker_contributions: Counter[str] = Counter()
    ticker_counts: Counter[str] = Counter()
    date_contributions: list[ContributionRecord] = []
    daily_component_values: dict[str, list[float]] = defaultdict(list)
    daily_precisions: list[float] = []
    daily_oracle_counts: list[int] = []
    for feature_date in sorted(rows_by_date):
        selected = sorted(
            rows_by_date[feature_date],
            key=lambda item: (-item.raw_score, item.ticker, item.sample_id),
        )[:top_n]
        selected_by_date[feature_date] = selected
        oracle = sorted(
            (item for item in rows_by_date[feature_date] if item.label_value > 0.0),
            key=lambda item: (-item.label_value, item.ticker, item.sample_id),
        )[:top_n]
        oracle_ids = {item.sample_id for item in oracle}
        daily_precisions.append(
            sum(item.sample_id in oracle_ids for item in selected) / len(selected)
        )
        daily_oracle_counts.append(len(oracle))
        daily_label = statistics.fmean(item.label_value for item in selected)
        daily_labels.append(daily_label)
        for name, value in _mean_components(selected).items():
            daily_component_values[name].append(value)
        date_contributions.append(
            ContributionRecord(
                key=feature_date.isoformat(),
                mean_contribution=daily_label,
                observation_count=len(selected),
            )
        )
        weight = 1.0 / len(selected)
        for item in selected:
            ticker_contributions[item.ticker] += item.label_value * weight
            ticker_counts[item.ticker] += 1

    dates = sorted(selected_by_date)
    turnovers = [
        _one_way_turnover(selected_by_date[previous], selected_by_date[current])
        for previous, current in zip(dates, dates[1:])
    ]
    date_count = len(dates)
    ticker_records = [
        ContributionRecord(
            key=ticker,
            mean_contribution=contribution / date_count,
            observation_count=ticker_counts[ticker],
        )
        for ticker, contribution in ticker_contributions.items()
    ]
    ticker_records.sort(key=lambda item: (-abs(item.mean_contribution), item.key))
    date_contributions.sort(key=lambda item: (-abs(item.mean_contribution), item.key))
    total_absolute = sum(abs(item.mean_contribution) for item in ticker_records)
    largest_five_share = (
        sum(abs(item.mean_contribution) for item in ticker_records[:5]) / total_absolute
        if total_absolute > 1e-15
        else 0.0
    )
    return TopNMetrics(
        top_n=top_n,
        evaluated_date_count=date_count,
        average_selected_count=statistics.fmean(len(items) for items in selected_by_date.values()),
        mean_label=statistics.fmean(daily_labels),
        median_label=statistics.median(daily_labels),
        positive_date_rate=sum(value > 0 for value in daily_labels) / len(daily_labels),
        positive_oracle_precision_at_n=statistics.fmean(daily_precisions),
        average_positive_oracle_count=statistics.fmean(daily_oracle_counts),
        average_one_way_turnover=_mean_or_none(turnovers),
        top_ticker_contributions=tuple(ticker_records[:5]),
        top_date_contributions=tuple(date_contributions[:5]),
        largest_five_abs_contribution_share=largest_five_share,
        max_repeat_frequency=(max(ticker_counts.values()) / date_count) if ticker_counts else 0.0,
        mean_label_components={
            name: statistics.fmean(values)
            for name, values in sorted(daily_component_values.items())
            if values
        },
    )


def evaluate_cross_sectional_predictions(
    predictions: Iterable[PredictionLike],
    labeled_scores: Iterable[FactorScore],
    *,
    config: CrossSectionalEvaluationConfig,
) -> CrossSectionalEvaluationReport:
    prediction_rows = list(predictions)
    labels = list(labeled_scores)
    if not prediction_rows:
        raise ValueError("evaluation requires at least one prediction")
    prediction_ids = [item.sample_id for item in prediction_rows]
    label_ids = [item.sample_id for item in labels]
    if len(prediction_ids) != len(set(prediction_ids)):
        raise ValueError("evaluation prediction sample_id values must be unique")
    if len(label_ids) != len(set(label_ids)):
        raise ValueError("evaluation label sample_id values must be unique")
    label_by_id = {item.sample_id: item for item in labels}
    missing = [sample_id for sample_id in prediction_ids if sample_id not in label_by_id]
    if missing:
        raise ValueError("evaluation labels are missing: " + ", ".join(missing[:5]))

    rows: list[_EvaluationRow] = []
    for prediction in prediction_rows:
        label = label_by_id[prediction.sample_id]
        if (
            prediction.ticker != label.ticker
            or prediction.feature_date != label.feature_date
            or prediction.horizon_days != label.horizon_days
        ):
            raise ValueError(f"prediction and label identity mismatch for {prediction.sample_id}")
        if label.label_value is None or not math.isfinite(float(label.label_value)):
            raise ValueError(f"evaluation label must be finite for {prediction.sample_id}")
        if not math.isfinite(float(prediction.raw_score)):
            raise ValueError(f"prediction score must be finite for {prediction.sample_id}")
        invalid_components = [
            name
            for name, value in label.label_components.items()
            if not math.isfinite(float(value))
        ]
        if invalid_components:
            raise ValueError(
                f"evaluation label components must be finite for {prediction.sample_id}: "
                + ", ".join(invalid_components[:5])
            )
        rows.append(
            _EvaluationRow(
                sample_id=prediction.sample_id,
                ticker=prediction.ticker,
                feature_date=prediction.feature_date,
                horizon_days=prediction.horizon_days,
                raw_score=float(prediction.raw_score),
                cross_sectional_rank=float(prediction.cross_sectional_rank),
                model_version=prediction.model_version,
                label_value=float(label.label_value),
                label_components=dict(label.label_components),
            )
        )
    horizons = {item.horizon_days for item in rows}
    if len(horizons) != 1:
        raise ValueError("evaluation requires exactly one horizon")

    rows_by_date: dict[date, list[_EvaluationRow]] = defaultdict(list)
    for row in rows:
        rows_by_date[row.feature_date].append(row)
    daily_metrics: list[DailyCrossSectionMetrics] = []
    daily_quantiles: dict[int, list[float]] = defaultdict(list)
    daily_ics: list[float] = []
    daily_spreads: list[float] = []
    for feature_date in sorted(rows_by_date):
        group = rows_by_date[feature_date]
        rank_ic = _rank_ic(group)
        if rank_ic is not None:
            daily_ics.append(rank_ic)
        quantile_groups = _quantile_groups(group, config.quantile_count)
        quantile_means = {
            quantile: statistics.fmean(item.label_value for item in items)
            for quantile, items in quantile_groups.items()
        }
        for quantile, value in quantile_means.items():
            daily_quantiles[quantile].append(value)
        if 1 in quantile_means and config.quantile_count in quantile_means:
            daily_spreads.append(quantile_means[config.quantile_count] - quantile_means[1])
        daily_metrics.append(
            DailyCrossSectionMetrics(
                feature_date=feature_date,
                sample_count=len(group),
                rank_ic=rank_ic,
                quantile_mean_labels=quantile_means,
                top_n_mean_labels={
                    top_n: statistics.fmean(
                        item.label_value
                        for item in sorted(
                            group,
                            key=lambda item: (-item.raw_score, item.ticker, item.sample_id),
                        )[:top_n]
                    )
                    for top_n in config.top_ns
                },
                top_n_positive_oracle_precision={
                    top_n: (
                        len(
                            {
                                item.sample_id
                                for item in sorted(
                                    group,
                                    key=lambda item: (-item.raw_score, item.ticker, item.sample_id),
                                )[:top_n]
                            }
                            & {
                                item.sample_id
                                for item in sorted(
                                    (item for item in group if item.label_value > 0.0),
                                    key=lambda item: (
                                        -item.label_value,
                                        item.ticker,
                                        item.sample_id,
                                    ),
                                )[:top_n]
                            }
                        )
                        / min(top_n, len(group))
                    )
                    for top_n in config.top_ns
                },
                top_n_mean_label_components={
                    top_n: _mean_components(
                        sorted(
                            group,
                            key=lambda item: (-item.raw_score, item.ticker, item.sample_id),
                        )[:top_n]
                    )
                    for top_n in config.top_ns
                },
            )
        )

    quantile_means = {
        quantile: statistics.fmean(daily_quantiles.get(quantile, []))
        for quantile in range(1, config.quantile_count + 1)
        if daily_quantiles.get(quantile)
    }
    adjacent = [
        quantile_means[index + 1] - quantile_means[index]
        for index in range(1, config.quantile_count)
        if index in quantile_means and index + 1 in quantile_means
    ]
    ic_mean = _mean_or_none(daily_ics)
    ic_std = statistics.pstdev(daily_ics) if len(daily_ics) > 1 else (0.0 if daily_ics else None)
    ic_ci = None
    if ic_mean is not None and ic_std is not None:
        half_width = 1.96 * ic_std / math.sqrt(len(daily_ics))
        ic_ci = (ic_mean - half_width, ic_mean + half_width)
    ic_significance = (
        significance_report(daily_ics, horizon_days=next(iter(horizons)))
        if len(daily_ics) >= 2
        else None
    )
    component_counts: Counter[str] = Counter(
        name for row in rows for name in row.label_components
    )
    return CrossSectionalEvaluationReport(
        model_key=config.model_key,
        model_versions=tuple(sorted({item.model_version for item in rows})),
        horizon_days=next(iter(horizons)),
        evaluated_dates=tuple(sorted(rows_by_date)),
        sample_count=len(rows),
        rank_ic_observation_count=len(daily_ics),
        rank_ic_mean=ic_mean,
        rank_ic_median=statistics.median(daily_ics) if daily_ics else None,
        rank_ic_std=ic_std,
        rank_ic_ir=(ic_mean / ic_std) if ic_mean is not None and ic_std not in {None, 0.0} else None,
        rank_ic_ci95=ic_ci,
        significance=ic_significance,
        quantile_mean_labels=quantile_means,
        quantile_monotonicity=(sum(value >= 0 for value in adjacent) / len(adjacent)) if adjacent else None,
        top_minus_bottom_mean=_mean_or_none(daily_spreads),
        top_n_metrics={
            top_n: _build_top_n_metrics(rows_by_date, top_n=top_n)
            for top_n in config.top_ns
        },
        daily_metrics=tuple(daily_metrics),
        overall_label_component_means={
            name: statistics.fmean(
                float(row.label_components[name])
                for row in rows
                if name in row.label_components
            )
            for name in sorted(component_counts)
        },
        label_component_coverage={
            name: count / len(rows)
            for name, count in sorted(component_counts.items())
        },
    )
