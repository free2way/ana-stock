from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from typing import Iterable, Mapping

from app.services.statistical_inference import block_bootstrap_mean_ci
from app.services.stock_selection.factor_pipeline import FactorScore


@dataclass(frozen=True, slots=True)
class SelectivePolicyConfig:
    """Frozen shadow-policy thresholds for a selector that may hold cash."""

    max_selected_per_date: int = 5
    minimum_positive_probability: float = 0.60
    minimum_expected_risk_adjusted_return: float = 0.003
    minimum_cross_sectional_rank: float = 0.80
    maximum_uncertainty: float = 0.20
    blocked_risk_tags: tuple[str, ...] = (
        "corporate_action",
        "limit_up",
        "stale_data",
        "suspended",
    )

    def __post_init__(self) -> None:
        if self.max_selected_per_date <= 0:
            raise ValueError("max_selected_per_date must be positive")
        for name, value in (
            ("minimum_positive_probability", self.minimum_positive_probability),
            ("minimum_cross_sectional_rank", self.minimum_cross_sectional_rank),
            ("maximum_uncertainty", self.maximum_uncertainty),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if not math.isfinite(self.minimum_expected_risk_adjusted_return):
            raise ValueError("minimum_expected_risk_adjusted_return must be finite")
        normalized = tuple(str(item).strip().lower() for item in self.blocked_risk_tags)
        if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("blocked_risk_tags must contain unique non-empty values")

    def version(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"selective_stock_policy_v1:{digest}"


@dataclass(frozen=True, slots=True)
class SelectiveCandidate:
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    cross_sectional_rank: float
    positive_probability: float
    expected_risk_adjusted_return: float
    uncertainty: float
    model_version: str
    tradable: bool = True
    risk_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("sample_id", self.sample_id),
            ("ticker", self.ticker),
            ("model_version", self.model_version),
        ):
            if not str(value or "").strip():
                raise ValueError(f"{name} must not be empty")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        for name, value in (
            ("raw_score", self.raw_score),
            ("expected_risk_adjusted_return", self.expected_risk_adjusted_return),
        ):
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        for name, value in (
            ("cross_sectional_rank", self.cross_sectional_rank),
            ("positive_probability", self.positive_probability),
            ("uncertainty", self.uncertainty),
        ):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be finite and in [0, 1]")


@dataclass(frozen=True, slots=True)
class SelectiveCandidateDecision:
    sample_id: str
    ticker: str
    selected: bool
    selected_rank: int | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SelectiveDateDecision:
    feature_date: date
    horizon_days: int
    policy_version: str
    max_selected_per_date: int
    candidate_count: int
    eligible_count: int
    selected: tuple[SelectiveCandidate, ...]
    candidate_decisions: tuple[SelectiveCandidateDecision, ...]
    abstained: bool
    abstention_reason: str | None
    rejection_counts: Mapping[str, int]


def _rejection_reasons(
    candidate: SelectiveCandidate,
    *,
    config: SelectivePolicyConfig,
    market_gate_open: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not market_gate_open:
        reasons.append("market_gate_closed")
    if not candidate.tradable:
        reasons.append("not_tradable")
    if candidate.positive_probability < config.minimum_positive_probability:
        reasons.append("below_probability_threshold")
    if candidate.expected_risk_adjusted_return < config.minimum_expected_risk_adjusted_return:
        reasons.append("below_expected_return_threshold")
    if candidate.cross_sectional_rank < config.minimum_cross_sectional_rank:
        reasons.append("below_rank_threshold")
    if candidate.uncertainty > config.maximum_uncertainty:
        reasons.append("above_uncertainty_threshold")
    blocked = {str(item).strip().lower() for item in config.blocked_risk_tags} & {
        str(item).strip().lower() for item in candidate.risk_tags
    }
    if blocked:
        reasons.append("blocked_risk_tag")
    return tuple(reasons)


def _abstention_reason(rejection_counts: Counter[str]) -> str:
    priority = (
        "market_gate_closed",
        "not_tradable",
        "blocked_risk_tag",
        "below_expected_return_threshold",
        "below_probability_threshold",
        "above_uncertainty_threshold",
        "below_rank_threshold",
    )
    return next((item for item in priority if rejection_counts[item]), "no_eligible_candidates")


def select_candidates(
    candidates: Iterable[SelectiveCandidate],
    *,
    config: SelectivePolicyConfig | None = None,
    market_gate_by_date: Mapping[date, bool] | None = None,
) -> tuple[SelectiveDateDecision, ...]:
    """Apply an auditable threshold policy and explicitly allow zero selections.

    Inputs contain predictions and ex-ante risk state only. Matured labels are
    deliberately absent, so this function is safe to use during historical
    point-in-time replay as well as live shadow inference.
    """

    resolved = config or SelectivePolicyConfig()
    rows = list(candidates)
    if not rows:
        raise ValueError("selective policy requires at least one candidate")
    sample_ids = [item.sample_id for item in rows]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("selective candidate sample_id values must be unique")
    horizons = {item.horizon_days for item in rows}
    if len(horizons) != 1:
        raise ValueError("selective candidates must use exactly one horizon")

    gates = market_gate_by_date or {}
    grouped: dict[date, list[SelectiveCandidate]] = defaultdict(list)
    for item in rows:
        grouped[item.feature_date].append(item)

    output: list[SelectiveDateDecision] = []
    for feature_date in sorted(grouped):
        group = sorted(grouped[feature_date], key=lambda item: (item.ticker, item.sample_id))
        gate_open = bool(gates.get(feature_date, True))
        reasons_by_id = {
            item.sample_id: _rejection_reasons(
                item,
                config=resolved,
                market_gate_open=gate_open,
            )
            for item in group
        }
        eligible = [item for item in group if not reasons_by_id[item.sample_id]]
        eligible.sort(
            key=lambda item: (
                -item.expected_risk_adjusted_return,
                -item.positive_probability,
                item.uncertainty,
                -item.raw_score,
                item.ticker,
                item.sample_id,
            )
        )
        selected = tuple(eligible[: resolved.max_selected_per_date])
        selected_rank = {item.sample_id: index + 1 for index, item in enumerate(selected)}
        decisions: list[SelectiveCandidateDecision] = []
        rejection_counts: Counter[str] = Counter()
        for item in group:
            reasons = reasons_by_id[item.sample_id]
            if not reasons and item.sample_id not in selected_rank:
                reasons = ("outside_daily_capacity",)
            rejection_counts.update(reasons)
            decisions.append(
                SelectiveCandidateDecision(
                    sample_id=item.sample_id,
                    ticker=item.ticker,
                    selected=item.sample_id in selected_rank,
                    selected_rank=selected_rank.get(item.sample_id),
                    reason_codes=reasons,
                )
            )
        output.append(
            SelectiveDateDecision(
                feature_date=feature_date,
                horizon_days=group[0].horizon_days,
                policy_version=resolved.version(),
                max_selected_per_date=resolved.max_selected_per_date,
                candidate_count=len(group),
                eligible_count=len(eligible),
                selected=selected,
                candidate_decisions=tuple(decisions),
                abstained=not selected,
                abstention_reason=(
                    _abstention_reason(rejection_counts) if not selected else None
                ),
                rejection_counts=dict(sorted(rejection_counts.items())),
            )
        )
    return tuple(output)


@dataclass(frozen=True, slots=True)
class SelectiveEvaluationConfig:
    market: str
    model_key: str
    round_trip_cost_bps: float

    def __post_init__(self) -> None:
        if not str(self.market or "").strip():
            raise ValueError("market must not be empty")
        if not str(self.model_key or "").strip():
            raise ValueError("model_key must not be empty")
        if self.round_trip_cost_bps < 0 or not math.isfinite(self.round_trip_cost_bps):
            raise ValueError("round_trip_cost_bps must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class SelectiveDailyMetric:
    feature_date: date
    candidate_count: int
    selected_count: int
    mean_risk_adjusted_return: float
    positive_selected_rate: float | None


@dataclass(frozen=True, slots=True)
class SelectiveMonthlyMetric:
    month: str
    oos_date_count: int
    active_date_count: int
    mean_calendar_risk_adjusted_return: float
    mean_active_risk_adjusted_return: float | None


@dataclass(frozen=True, slots=True)
class SelectiveEvaluationReport:
    schema_version: str
    market: str
    model_key: str
    model_versions: tuple[str, ...]
    policy_version: str
    horizon_days: int
    max_selected_per_date: int
    round_trip_cost_bps: float
    oos_date_count: int
    active_date_count: int
    abstention_date_count: int
    coverage_rate: float
    average_selected_count: float
    mean_active_risk_adjusted_return: float
    mean_calendar_risk_adjusted_return: float
    positive_active_date_rate: float
    positive_selected_rate: float
    active_mean_ci95: tuple[float, float]
    extreme_five_dates_excluded_mean: float | None
    extreme_five_tickers_excluded_mean: float | None
    monthly_metrics: tuple[SelectiveMonthlyMetric, ...]
    daily_metrics: tuple[SelectiveDailyMetric, ...]
    active_mean_ci95_iid: tuple[float, float] | None = None
    active_mean_ci_cluster_method: str = "day_order_moving_block_bootstrap_block=horizon"


def _mean_ci95(values: list[float]) -> tuple[float, float]:
    """Legacy iid normal interval; kept as the diagnostic companion."""
    mean = statistics.fmean(values)
    if len(values) == 1:
        return (mean, mean)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    radius = 1.96 * standard_error
    return (mean - radius, mean + radius)


def _clustered_mean_ci95(values: list[float], *, horizon_days: int) -> tuple[float, float] | None:
    """Day-ordered moving-block bootstrap interval for the active-date mean.

    ``active_values`` already holds one value per active date in ascending date
    order, so a moving-block resample over consecutive entries is exactly a
    day-clustered bootstrap.  Block length = evaluation horizon, matching the
    overlapping-return dependence window used elsewhere in the repo.
    """
    if len(values) < 2:
        return None
    return block_bootstrap_mean_ci(values, block_length=max(1, int(horizon_days)))


def evaluate_selective_decisions(
    decisions: Iterable[SelectiveDateDecision],
    labeled_scores: Iterable[FactorScore],
    *,
    config: SelectiveEvaluationConfig,
) -> SelectiveEvaluationReport:
    """Evaluate a selective policy with abstention days treated as cash (zero)."""

    daily_decisions = list(decisions)
    labels = list(labeled_scores)
    if not daily_decisions:
        raise ValueError("selective evaluation requires at least one date decision")
    if len({item.feature_date for item in daily_decisions}) != len(daily_decisions):
        raise ValueError("selective date decisions must have unique feature dates")
    if len({item.policy_version for item in daily_decisions}) != 1:
        raise ValueError("selective date decisions must use one policy version")
    if len({item.horizon_days for item in daily_decisions}) != 1:
        raise ValueError("selective date decisions must use one horizon")
    if len({item.max_selected_per_date for item in daily_decisions}) != 1:
        raise ValueError("selective date decisions must use one daily capacity")
    label_by_id = {item.sample_id: item for item in labels}
    if len(label_by_id) != len(labels):
        raise ValueError("selective evaluation label sample_id values must be unique")

    daily_metrics: list[SelectiveDailyMetric] = []
    active_values: list[float] = []
    selected_values: list[float] = []
    selected_rows_by_date: dict[date, list[tuple[SelectiveCandidate, float]]] = {}
    model_versions: set[str] = set()
    for decision in sorted(daily_decisions, key=lambda item: item.feature_date):
        rows: list[tuple[SelectiveCandidate, float]] = []
        for candidate in decision.selected:
            label = label_by_id.get(candidate.sample_id)
            if label is None:
                raise ValueError(f"selected label is missing: {candidate.sample_id}")
            if (
                label.ticker != candidate.ticker
                or label.feature_date != candidate.feature_date
                or label.horizon_days != candidate.horizon_days
            ):
                raise ValueError(f"selected candidate and label mismatch: {candidate.sample_id}")
            if label.label_value is None or not math.isfinite(float(label.label_value)):
                raise ValueError(f"selected label must be finite: {candidate.sample_id}")
            value = float(label.label_value)
            rows.append((candidate, value))
            selected_values.append(value)
            model_versions.add(candidate.model_version)
        selected_rows_by_date[decision.feature_date] = rows
        daily_value = statistics.fmean(value for _, value in rows) if rows else 0.0
        if rows:
            active_values.append(daily_value)
        daily_metrics.append(
            SelectiveDailyMetric(
                feature_date=decision.feature_date,
                candidate_count=decision.candidate_count,
                selected_count=len(rows),
                mean_risk_adjusted_return=daily_value,
                positive_selected_rate=(
                    sum(value > 0 for _, value in rows) / len(rows) if rows else None
                ),
            )
        )
    if not active_values:
        raise ValueError("selective evaluation has no active selections")

    monthly_groups: dict[str, list[SelectiveDailyMetric]] = defaultdict(list)
    for item in daily_metrics:
        monthly_groups[item.feature_date.strftime("%Y-%m")].append(item)
    monthly_metrics = tuple(
        SelectiveMonthlyMetric(
            month=month,
            oos_date_count=len(rows),
            active_date_count=sum(item.selected_count > 0 for item in rows),
            mean_calendar_risk_adjusted_return=statistics.fmean(
                item.mean_risk_adjusted_return for item in rows
            ),
            mean_active_risk_adjusted_return=(
                statistics.fmean(
                    item.mean_risk_adjusted_return for item in rows if item.selected_count > 0
                )
                if any(item.selected_count > 0 for item in rows)
                else None
            ),
        )
        for month, rows in sorted(monthly_groups.items())
    )

    extreme_dates = {
        item.feature_date
        for item in sorted(
            (item for item in daily_metrics if item.selected_count > 0),
            key=lambda item: (-abs(item.mean_risk_adjusted_return), item.feature_date),
        )[:5]
    }
    date_excluded = [
        item.mean_risk_adjusted_return
        for item in daily_metrics
        if item.selected_count > 0 and item.feature_date not in extreme_dates
    ]

    ticker_contributions: Counter[str] = Counter()
    for rows in selected_rows_by_date.values():
        if not rows:
            continue
        weight = 1.0 / len(rows)
        for candidate, value in rows:
            ticker_contributions[candidate.ticker] += value * weight
    extreme_tickers = {
        ticker
        for ticker, _ in sorted(
            ticker_contributions.items(),
            key=lambda item: (-abs(item[1]), item[0]),
        )[:5]
    }
    ticker_excluded_daily: list[float] = []
    for feature_date in sorted(selected_rows_by_date):
        remaining = [
            value
            for candidate, value in selected_rows_by_date[feature_date]
            if candidate.ticker not in extreme_tickers
        ]
        if remaining:
            ticker_excluded_daily.append(statistics.fmean(remaining))

    oos_dates = len(daily_metrics)
    active_dates = len(active_values)
    horizon_days = daily_decisions[0].horizon_days
    iid_ci = _mean_ci95(active_values)
    clustered_ci = _clustered_mean_ci95(active_values, horizon_days=horizon_days)
    # Promotion thresholds consume `active_mean_ci95`.  It now carries the
    # day-clustered lower bound whenever the bootstrap is defined; the iid
    # interval is preserved separately as a diagnostic.
    return SelectiveEvaluationReport(
        schema_version="selective_stock_evaluation_v1",
        market=config.market,
        model_key=config.model_key,
        model_versions=tuple(sorted(model_versions)),
        policy_version=daily_decisions[0].policy_version,
        horizon_days=horizon_days,
        max_selected_per_date=daily_decisions[0].max_selected_per_date,
        round_trip_cost_bps=config.round_trip_cost_bps,
        oos_date_count=oos_dates,
        active_date_count=active_dates,
        abstention_date_count=oos_dates - active_dates,
        coverage_rate=active_dates / oos_dates,
        average_selected_count=statistics.fmean(item.selected_count for item in daily_metrics),
        mean_active_risk_adjusted_return=statistics.fmean(active_values),
        mean_calendar_risk_adjusted_return=statistics.fmean(
            item.mean_risk_adjusted_return for item in daily_metrics
        ),
        positive_active_date_rate=sum(value > 0 for value in active_values) / active_dates,
        positive_selected_rate=sum(value > 0 for value in selected_values) / len(selected_values),
        active_mean_ci95=clustered_ci if clustered_ci is not None else iid_ci,
        extreme_five_dates_excluded_mean=(
            statistics.fmean(date_excluded) if date_excluded else None
        ),
        extreme_five_tickers_excluded_mean=(
            statistics.fmean(ticker_excluded_daily) if ticker_excluded_daily else None
        ),
        monthly_metrics=monthly_metrics,
        daily_metrics=tuple(daily_metrics),
        active_mean_ci95_iid=iid_ci,
        active_mean_ci_cluster_method=(
            f"day_order_moving_block_bootstrap_block={max(1, int(horizon_days))}"
            if clustered_ci is not None
            else "iid_normal_fallback_insufficient_active_dates"
        ),
    )


@dataclass(frozen=True, slots=True)
class CoveragePrecisionPoint:
    """One point on the abstention curve: higher threshold -> lower coverage."""

    threshold: float
    selected_count: int
    total_count: int
    coverage: float
    precision: float | None


def coverage_precision_curve(
    rows: Iterable[Mapping],
    *,
    thresholds: Iterable[float],
    probability_key: str = "expected_hit_probability",
    outcome_key: str | None = None,
) -> tuple[CoveragePrecisionPoint, ...]:
    """Report the coverage/precision trade-off of a probability gate.

    Coverage is the share of candidates that clear ``threshold``; it is
    non-increasing as the threshold rises.  Precision, when matured outcomes
    are joined in, is the realized positive rate of the retained subset -- the
    "trade出手率 for命中率" contract.
    """
    resolved_rows = list(rows)
    total = len(resolved_rows)
    resolved_thresholds = sorted({float(item) for item in thresholds})
    points: list[CoveragePrecisionPoint] = []
    for threshold in resolved_thresholds:
        if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
            raise ValueError("coverage-precision thresholds must be finite and in [0, 1]")
        selected = [
            row
            for row in resolved_rows
            if row.get(probability_key) is not None and float(row[probability_key]) >= threshold
        ]
        precision: float | None = None
        if outcome_key is not None and selected:
            outcomes = [
                float(row[outcome_key])
                for row in selected
                if row.get(outcome_key) is not None
            ]
            if outcomes:
                precision = sum(1 for value in outcomes if value > 0) / len(outcomes)
        points.append(
            CoveragePrecisionPoint(
                threshold=threshold,
                selected_count=len(selected),
                total_count=total,
                coverage=(len(selected) / total) if total else 0.0,
                precision=precision,
            )
        )
    return tuple(points)
