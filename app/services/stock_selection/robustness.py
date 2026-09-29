from __future__ import annotations

import hashlib
import json
import math
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Mapping

import polars as pl


@dataclass(frozen=True, slots=True)
class RobustnessRow:
    sample_id: str
    ticker: str
    feature_date: date
    horizon_days: int
    raw_score: float
    label_value: float
    label_components: Mapping[str, float]
    features: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class CohortMetrics:
    evaluated_date_count: int
    mean_risk_adjusted_return: float
    median_risk_adjusted_return: float
    positive_date_rate: float
    mean_gross_return: float
    mean_net_return: float
    mean_market_return: float


@dataclass(frozen=True, slots=True)
class SliceMetrics:
    key: str
    metrics: CohortMetrics


@dataclass(frozen=True, slots=True)
class EquityPoint:
    feature_date: date
    sleeve_index: int
    cohort_net_return: float
    aggregate_equity: float


@dataclass(frozen=True, slots=True)
class PortfolioMetrics:
    sleeve_count: int
    observation_count: int
    total_return: float
    annualized_return: float
    annualized_volatility: float
    annualized_sharpe: float | None
    max_drawdown: float
    positive_update_rate: float


@dataclass(frozen=True, slots=True)
class PointInTimeGateMetrics:
    factor_name: str
    threshold_mode: str
    fixed_threshold: float | None
    trailing_lookback_sessions: int | None
    mean_threshold: float
    gate_on_date_count: int
    gate_off_date_count: int
    gate_on_ratio: float
    mean_breadth: float
    mean_risk_adjusted_return_when_on: float
    mean_risk_adjusted_return_when_off: float
    average_scaled_exposure: float
    gated_metrics: CohortMetrics
    portfolio_metrics: PortfolioMetrics
    equity_curve: tuple[EquityPoint, ...]
    scaled_metrics: CohortMetrics
    scaled_portfolio_metrics: PortfolioMetrics
    scaled_equity_curve: tuple[EquityPoint, ...]


@dataclass(frozen=True, slots=True)
class RobustnessReport:
    schema_version: str
    market: str
    model_key: str
    factor_set_key: str
    dataset_version: str
    horizon_days: int
    top_n: int
    round_trip_cost_bps: float
    base_metrics: CohortMetrics
    excluded_tickers: tuple[str, ...]
    ticker_exclusion_metrics: CohortMetrics
    excluded_dates: tuple[date, ...]
    date_exclusion_metrics: CohortMetrics
    monthly_metrics: tuple[SliceMetrics, ...]
    market_state_metrics: tuple[SliceMetrics, ...]
    portfolio_metrics: PortfolioMetrics
    equity_curve: tuple[EquityPoint, ...]
    point_in_time_gate: PointInTimeGateMetrics


@dataclass(frozen=True, slots=True)
class RobustnessEvidenceWriteResult:
    evidence_version: str
    artifact_dir: Path
    manifest_path: Path
    reused_existing: bool


@dataclass(frozen=True, slots=True)
class _DailyCohort:
    feature_date: date
    risk_adjusted_return: float
    gross_return: float
    net_return: float
    market_return: float


def load_robustness_rows(
    *,
    predictions_path: Path,
    samples_path: Path,
    model_key: str,
) -> tuple[RobustnessRow, ...]:
    predictions = pl.scan_parquet(predictions_path).filter(pl.col("model_key") == model_key)
    samples = pl.scan_parquet(samples_path).select(
        "sample_id",
        "label_value",
        "label_components_json",
        "features_json",
        "dataset_version",
    )
    joined = predictions.join(samples, on="sample_id", how="inner").collect()
    if joined.height == 0:
        raise ValueError("robustness input join is empty")
    if joined.height != predictions.select(pl.len()).collect().item():
        raise ValueError("robustness samples are missing prediction labels")
    rows: list[RobustnessRow] = []
    for item in joined.iter_rows(named=True):
        components = json.loads(str(item["label_components_json"]))
        features = json.loads(str(item["features_json"]))
        rows.append(
            RobustnessRow(
                sample_id=str(item["sample_id"]),
                ticker=str(item["ticker"]),
                feature_date=date.fromisoformat(str(item["feature_date"])),
                horizon_days=int(item["horizon_days"]),
                raw_score=float(item["raw_score"]),
                label_value=float(item["label_value"]),
                label_components={key: float(value) for key, value in components.items()},
                features={key: float(value) for key, value in features.items()},
            )
        )
    return tuple(rows)


def load_market_breadth_history(
    *,
    samples_path: Path,
    horizon_days: int,
    factor_name: str = "price_vs_ma20",
) -> dict[date, float]:
    frame = (
        pl.scan_parquet(samples_path)
        .filter((pl.col("horizon_days") == horizon_days) & pl.col("tradable"))
        .with_columns(
            pl.col("features_json")
            .str.json_path_match(f"$.{factor_name}")
            .cast(pl.Float64, strict=False)
            .alias("gate_factor")
        )
        .filter(pl.col("gate_factor").is_finite())
        .group_by("feature_date")
        .agg((pl.col("gate_factor") > 0).mean().alias("breadth"))
        .sort("feature_date")
        .collect()
    )
    if frame.height == 0:
        raise ValueError(f"market breadth history is empty for {factor_name}")
    return {
        date.fromisoformat(str(item["feature_date"])): float(item["breadth"])
        for item in frame.iter_rows(named=True)
    }


def _select_top_n(
    rows_by_date: Mapping[date, list[RobustnessRow]],
    *,
    top_n: int,
    excluded_tickers: set[str] | None = None,
) -> dict[date, list[RobustnessRow]]:
    excluded = excluded_tickers or set()
    selected: dict[date, list[RobustnessRow]] = {}
    for feature_date, rows in sorted(rows_by_date.items()):
        eligible = [item for item in rows if item.ticker not in excluded]
        if len(eligible) < top_n:
            raise ValueError(f"fewer than top_n eligible rows on {feature_date.isoformat()}")
        selected[feature_date] = sorted(
            eligible,
            key=lambda item: (-item.raw_score, item.ticker, item.sample_id),
        )[:top_n]
    return selected


def _daily_cohorts(selected_by_date: Mapping[date, list[RobustnessRow]]) -> tuple[_DailyCohort, ...]:
    output: list[_DailyCohort] = []
    for feature_date, rows in sorted(selected_by_date.items()):
        components = {
            name: statistics.fmean(float(item.label_components[name]) for item in rows)
            for name in ("gross_return", "net_return", "market_excess_return")
        }
        output.append(
            _DailyCohort(
                feature_date=feature_date,
                risk_adjusted_return=statistics.fmean(item.label_value for item in rows),
                gross_return=components["gross_return"],
                net_return=components["net_return"],
                market_return=components["net_return"] - components["market_excess_return"],
            )
        )
    return tuple(output)


def _cohort_metrics(daily: Iterable[_DailyCohort]) -> CohortMetrics:
    rows = list(daily)
    if not rows:
        raise ValueError("cohort metrics require at least one date")
    risk_values = [item.risk_adjusted_return for item in rows]
    return CohortMetrics(
        evaluated_date_count=len(rows),
        mean_risk_adjusted_return=statistics.fmean(risk_values),
        median_risk_adjusted_return=statistics.median(risk_values),
        positive_date_rate=sum(value > 0 for value in risk_values) / len(risk_values),
        mean_gross_return=statistics.fmean(item.gross_return for item in rows),
        mean_net_return=statistics.fmean(item.net_return for item in rows),
        mean_market_return=statistics.fmean(item.market_return for item in rows),
    )


def _slice_metrics(
    daily: Iterable[_DailyCohort],
    *,
    key_for: Callable[[_DailyCohort], str],
) -> tuple[SliceMetrics, ...]:
    grouped: dict[str, list[_DailyCohort]] = defaultdict(list)
    for item in daily:
        grouped[str(key_for(item))].append(item)
    return tuple(
        SliceMetrics(key=key, metrics=_cohort_metrics(grouped[key]))
        for key in sorted(grouped)
    )


def _equity_metrics(
    daily: Iterable[_DailyCohort],
    *,
    horizon_days: int,
) -> tuple[PortfolioMetrics, tuple[EquityPoint, ...]]:
    rows = sorted(daily, key=lambda item: item.feature_date)
    sleeves = [1.0] * horizon_days
    curve: list[EquityPoint] = []
    aggregate_returns: list[float] = []
    previous_equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for index, item in enumerate(rows):
        sleeve_index = index % horizon_days
        sleeves[sleeve_index] *= max(0.0, 1.0 + item.net_return)
        equity = statistics.fmean(sleeves)
        aggregate_return = (equity / previous_equity) - 1.0 if previous_equity > 0 else -1.0
        aggregate_returns.append(aggregate_return)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, (equity / peak) - 1.0)
        curve.append(
            EquityPoint(
                feature_date=item.feature_date,
                sleeve_index=sleeve_index,
                cohort_net_return=item.net_return,
                aggregate_equity=equity,
            )
        )
        previous_equity = equity
    total_return = curve[-1].aggregate_equity - 1.0
    periods = len(aggregate_returns)
    annualized_return = (
        (1.0 + total_return) ** (252.0 / periods) - 1.0
        if periods and total_return > -1.0
        else -1.0
    )
    volatility = statistics.pstdev(aggregate_returns) * math.sqrt(252.0)
    mean_return = statistics.fmean(aggregate_returns)
    sharpe = (
        mean_return / statistics.pstdev(aggregate_returns) * math.sqrt(252.0)
        if statistics.pstdev(aggregate_returns) > 1e-15
        else None
    )
    metrics = PortfolioMetrics(
        sleeve_count=horizon_days,
        observation_count=periods,
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_volatility=volatility,
        annualized_sharpe=sharpe,
        max_drawdown=max_drawdown,
        positive_update_rate=sum(value > 0 for value in aggregate_returns) / periods,
    )
    return metrics, tuple(curve)


def _point_in_time_gate_metrics(
    rows_by_date: Mapping[date, list[RobustnessRow]],
    base_daily: tuple[_DailyCohort, ...],
    *,
    horizon_days: int,
    factor_name: str = "price_vs_ma20",
    threshold: float = 0.50,
    minimum_cross_section_size: int = 20,
    breadth_history: Mapping[date, float] | None = None,
    trailing_lookback_sessions: int = 120,
    minimum_trailing_sessions: int = 60,
) -> PointInTimeGateMetrics:
    breadth_by_date: dict[date, float] = {}
    for feature_date, rows in sorted(rows_by_date.items()):
        values = [
            float(item.features[factor_name])
            for item in rows
            if factor_name in item.features and math.isfinite(float(item.features[factor_name]))
        ]
        if len(values) < minimum_cross_section_size:
            raise ValueError(
                f"point-in-time gate has insufficient {factor_name} coverage on "
                f"{feature_date.isoformat()}"
            )
        breadth_by_date[feature_date] = sum(value > 0 for value in values) / len(values)
    thresholds_by_date: dict[date, float] = {}
    threshold_mode = "fixed"
    if breadth_history is None:
        thresholds_by_date = {feature_date: threshold for feature_date in breadth_by_date}
    else:
        threshold_mode = "trailing_median"
        ordered_history = sorted(breadth_history.items())
        for feature_date in breadth_by_date:
            trailing = [
                value
                for history_date, value in ordered_history
                if history_date < feature_date
            ][-trailing_lookback_sessions:]
            if len(trailing) < minimum_trailing_sessions:
                raise ValueError(
                    "point-in-time gate has insufficient trailing breadth history on "
                    + feature_date.isoformat()
                )
            thresholds_by_date[feature_date] = statistics.median(trailing)
    gate_on = {
        feature_date: breadth >= thresholds_by_date[feature_date]
        for feature_date, breadth in breadth_by_date.items()
    }
    on_rows = [item for item in base_daily if gate_on[item.feature_date]]
    off_rows = [item for item in base_daily if not gate_on[item.feature_date]]
    if not on_rows or not off_rows:
        raise ValueError("point-in-time gate must contain both on and off dates")
    gated_daily = tuple(
        item
        if gate_on[item.feature_date]
        else _DailyCohort(
            feature_date=item.feature_date,
            risk_adjusted_return=0.0,
            gross_return=0.0,
            net_return=0.0,
            market_return=item.market_return,
        )
        for item in base_daily
    )
    exposure_by_date = {
        feature_date: max(
            0.0,
            min(1.0, breadth_by_date[feature_date] / thresholds_by_date[feature_date]),
        )
        if thresholds_by_date[feature_date] > 1e-12
        else 0.0
        for feature_date in breadth_by_date
    }
    scaled_daily = tuple(
        _DailyCohort(
            feature_date=item.feature_date,
            risk_adjusted_return=(
                item.risk_adjusted_return * exposure_by_date[item.feature_date]
            ),
            gross_return=item.gross_return * exposure_by_date[item.feature_date],
            net_return=item.net_return * exposure_by_date[item.feature_date],
            market_return=item.market_return,
        )
        for item in base_daily
    )
    portfolio_metrics, curve = _equity_metrics(gated_daily, horizon_days=horizon_days)
    scaled_portfolio_metrics, scaled_curve = _equity_metrics(
        scaled_daily,
        horizon_days=horizon_days,
    )
    return PointInTimeGateMetrics(
        factor_name=factor_name,
        threshold_mode=threshold_mode,
        fixed_threshold=threshold if threshold_mode == "fixed" else None,
        trailing_lookback_sessions=(
            trailing_lookback_sessions if threshold_mode == "trailing_median" else None
        ),
        mean_threshold=statistics.fmean(thresholds_by_date.values()),
        gate_on_date_count=len(on_rows),
        gate_off_date_count=len(off_rows),
        gate_on_ratio=len(on_rows) / len(base_daily),
        mean_breadth=statistics.fmean(breadth_by_date.values()),
        mean_risk_adjusted_return_when_on=statistics.fmean(
            item.risk_adjusted_return for item in on_rows
        ),
        mean_risk_adjusted_return_when_off=statistics.fmean(
            item.risk_adjusted_return for item in off_rows
        ),
        average_scaled_exposure=statistics.fmean(exposure_by_date.values()),
        gated_metrics=_cohort_metrics(gated_daily),
        portfolio_metrics=portfolio_metrics,
        equity_curve=curve,
        scaled_metrics=_cohort_metrics(scaled_daily),
        scaled_portfolio_metrics=scaled_portfolio_metrics,
        scaled_equity_curve=scaled_curve,
    )


def analyze_top_n_robustness(
    rows: Iterable[RobustnessRow],
    *,
    model_key: str,
    factor_set_key: str,
    dataset_version: str,
    top_n: int,
    round_trip_cost_bps: float,
    market: str = "CN",
    extreme_ticker_count: int = 5,
    extreme_date_count: int = 5,
    gate_minimum_cross_section_size: int = 20,
    gate_breadth_history: Mapping[date, float] | None = None,
) -> RobustnessReport:
    items = list(rows)
    if not items:
        raise ValueError("robustness analysis requires rows")
    if (
        top_n <= 0
        or extreme_ticker_count <= 0
        or extreme_date_count <= 0
        or gate_minimum_cross_section_size <= 0
    ):
        raise ValueError("robustness counts must be positive")
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    horizons = {item.horizon_days for item in items}
    if len(horizons) != 1:
        raise ValueError("robustness analysis requires exactly one horizon")
    horizon_days = next(iter(horizons))
    rows_by_date: dict[date, list[RobustnessRow]] = defaultdict(list)
    for item in items:
        rows_by_date[item.feature_date].append(item)
    selected = _select_top_n(rows_by_date, top_n=top_n)
    base_daily = _daily_cohorts(selected)

    expected_cost = round_trip_cost_bps / 10_000.0
    observed_costs = [item.gross_return - item.net_return for item in base_daily]
    if any(abs(value - expected_cost) > 1e-10 for value in observed_costs):
        raise ValueError("configured round-trip cost does not match sample label components")

    contributions: Counter[str] = Counter()
    for rows_for_date in selected.values():
        for item in rows_for_date:
            contributions[item.ticker] += item.label_value / top_n
    excluded_tickers = tuple(
        ticker
        for ticker, _ in sorted(
            contributions.items(),
            key=lambda item: (-abs(item[1]), item[0]),
        )[:extreme_ticker_count]
    )
    ticker_exclusion_daily = _daily_cohorts(
        _select_top_n(rows_by_date, top_n=top_n, excluded_tickers=set(excluded_tickers))
    )
    excluded_dates = tuple(
        item.feature_date
        for item in sorted(
            base_daily,
            key=lambda item: (-abs(item.risk_adjusted_return), item.feature_date),
        )[:extreme_date_count]
    )
    date_exclusion_daily = [item for item in base_daily if item.feature_date not in set(excluded_dates)]
    monthly = _slice_metrics(
        base_daily,
        key_for=lambda item: item.feature_date.strftime("%Y-%m"),
    )
    market_states = _slice_metrics(
        base_daily,
        key_for=lambda item: "market_up" if item.market_return >= 0 else "market_down",
    )
    portfolio_metrics, curve = _equity_metrics(base_daily, horizon_days=horizon_days)
    point_in_time_gate = _point_in_time_gate_metrics(
        rows_by_date,
        base_daily,
        horizon_days=horizon_days,
        minimum_cross_section_size=gate_minimum_cross_section_size,
        breadth_history=gate_breadth_history,
    )
    return RobustnessReport(
        schema_version="stock_selection_robustness_v1",
        market=market_code,
        model_key=model_key,
        factor_set_key=factor_set_key,
        dataset_version=dataset_version,
        horizon_days=horizon_days,
        top_n=top_n,
        round_trip_cost_bps=round_trip_cost_bps,
        base_metrics=_cohort_metrics(base_daily),
        excluded_tickers=excluded_tickers,
        ticker_exclusion_metrics=_cohort_metrics(ticker_exclusion_daily),
        excluded_dates=excluded_dates,
        date_exclusion_metrics=_cohort_metrics(date_exclusion_daily),
        monthly_metrics=monthly,
        market_state_metrics=market_states,
        portfolio_metrics=portfolio_metrics,
        equity_curve=curve,
        point_in_time_gate=point_in_time_gate,
    )


def persist_robustness_report(
    report: RobustnessReport,
    *,
    root: Path,
) -> RobustnessEvidenceWriteResult:
    payload = asdict(report)
    canonical = json.dumps(payload, default=_json_default, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    evidence_version = (
        f"stock_selection_robustness_v1:{report.market}:{report.horizon_days}d:"
        f"{report.factor_set_key}:{digest[:20]}"
    )
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / evidence_version.replace(":", "_")
    with tempfile.TemporaryDirectory(prefix=".robustness-", dir=root) as temporary_name:
        temporary_dir = Path(temporary_name)
        report_path = temporary_dir / "robustness.json"
        report_path.write_text(
            json.dumps(payload, default=_json_default, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": "stock_selection_robustness_manifest_v1",
            "evidence_version": evidence_version,
            "report_file": "robustness.json",
            "report_sha256": report_sha256,
            "dataset_version": report.dataset_version,
            "market": report.market,
            "factor_set_key": report.factor_set_key,
            "model_key": report.model_key,
            "horizon_days": report.horizon_days,
            "top_n": report.top_n,
            "round_trip_cost_bps": report.round_trip_cost_bps,
        }
        (temporary_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target_dir.exists():
            manifest_path = target_dir / "manifest.json"
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("report_sha256") != report_sha256:
                raise RuntimeError(f"refusing to overwrite immutable robustness evidence: {target_dir}")
            return RobustnessEvidenceWriteResult(
                evidence_version=evidence_version,
                artifact_dir=target_dir,
                manifest_path=manifest_path,
                reused_existing=True,
            )
        temporary_dir.replace(target_dir)
    return RobustnessEvidenceWriteResult(
        evidence_version=evidence_version,
        artifact_dir=target_dir,
        manifest_path=target_dir / "manifest.json",
        reused_existing=False,
    )


def _json_default(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
