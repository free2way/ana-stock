from __future__ import annotations

import hashlib
import json
import math
import statistics
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

import polars as pl
from sklearn.metrics import average_precision_score, roc_auc_score

from app.services.stock_selection.factor_pipeline import FactorDirection, _quantile
from app.services.stock_selection.factor_sets import research_factor_sets


@dataclass(frozen=True, slots=True)
class SeparabilityRow:
    sample_id: str
    ticker: str
    feature_date: date
    label_value: float
    features: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    factor_name: str
    direction: FactorDirection
    weight: float = 1.0
    winsor_lower: float = 0.025
    winsor_upper: float = 0.975


@dataclass(frozen=True, slots=True)
class SeparabilityScoreDefinition:
    key: str
    components: tuple[ScoreComponent, ...]


@dataclass(frozen=True, slots=True)
class HeadSeparabilityConfig:
    top_fraction: float = 0.01
    minimum_head_count: int = 5
    precision_n: int = 5
    chronological_block_count: int = 4
    minimum_cross_section_size: int = 100
    minimum_auc_mean: float = 0.55
    minimum_auc_positive_date_rate: float = 0.60
    minimum_precision_at_n: float = 0.05
    minimum_precision_lift: float = 5.0
    minimum_positive_blocks: int = 3
    zscore_clip: float = 3.0

    def __post_init__(self) -> None:
        if not 0 < self.top_fraction <= 0.5:
            raise ValueError("top_fraction must be in (0, 0.5]")
        if self.minimum_head_count <= 0 or self.precision_n <= 0:
            raise ValueError("head and precision counts must be positive")
        if self.chronological_block_count < 2:
            raise ValueError("chronological_block_count must be at least two")
        if self.minimum_cross_section_size < self.minimum_head_count * 2:
            raise ValueError("minimum_cross_section_size is too small")
        if not 0.5 <= self.minimum_auc_mean <= 1.0:
            raise ValueError("minimum_auc_mean must be in [0.5, 1]")
        if not 0.5 <= self.minimum_auc_positive_date_rate <= 1.0:
            raise ValueError("minimum_auc_positive_date_rate must be in [0.5, 1]")
        if not 0 <= self.minimum_precision_at_n <= 1.0:
            raise ValueError("minimum_precision_at_n must be in [0, 1]")
        if self.minimum_precision_lift < 1.0:
            raise ValueError("minimum_precision_lift must be at least one")
        if not 1 <= self.minimum_positive_blocks <= self.chronological_block_count:
            raise ValueError("minimum_positive_blocks is invalid")
        if self.zscore_clip <= 0:
            raise ValueError("zscore_clip must be positive")


@dataclass(frozen=True, slots=True)
class SeparabilityDailyMetric:
    feature_date: date
    score_key: str
    sample_count: int
    positive_head_count: int
    positive_label_rate: float
    head_auc: float
    positive_label_auc: float
    average_precision: float
    precision_at_n: float
    precision_lift_at_n: float


@dataclass(frozen=True, slots=True)
class SeparabilityBlockMetric:
    block_index: int
    start_date: date
    end_date: date
    date_count: int
    head_auc_mean: float
    precision_lift_mean: float
    passed: bool


@dataclass(frozen=True, slots=True)
class SeparabilityScoreSummary:
    score_key: str
    evaluated_date_count: int
    head_auc_mean: float
    head_auc_median: float
    head_auc_positive_date_rate: float
    positive_label_auc_mean: float
    average_precision_mean: float
    precision_at_n_mean: float
    precision_lift_at_n_mean: float
    positive_block_count: int
    block_metrics: tuple[SeparabilityBlockMetric, ...]
    passed: bool
    failed_checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HeadSeparabilityReport:
    schema_version: str
    market: str
    dataset_version: str
    horizon_days: int
    analysis_dates: tuple[date, ...]
    excluded_tail_date_count: int
    config: HeadSeparabilityConfig
    score_definitions: tuple[SeparabilityScoreDefinition, ...]
    summaries: tuple[SeparabilityScoreSummary, ...]
    daily_metrics: tuple[SeparabilityDailyMetric, ...]
    separable_score_keys: tuple[str, ...]
    verdict: str


@dataclass(frozen=True, slots=True)
class HeadSeparabilityEvidenceWriteResult:
    evidence_version: str
    artifact_dir: Path
    manifest_path: Path
    reused_existing: bool


def default_separability_score_definitions() -> tuple[SeparabilityScoreDefinition, ...]:
    factor_directions: dict[tuple[str, FactorDirection], None] = {}
    definitions: list[SeparabilityScoreDefinition] = []
    for factor_set in research_factor_sets().values():
        for spec in factor_set.specs:
            factor_directions[(spec.name, spec.direction)] = None
        definitions.append(
            SeparabilityScoreDefinition(
                key=f"factor_set:{factor_set.key}",
                components=tuple(
                    ScoreComponent(
                        spec.name,
                        spec.direction,
                        spec.weight,
                        spec.winsor_lower,
                        spec.winsor_upper,
                    )
                    for spec in factor_set.specs
                ),
            )
        )
    for factor_name, direction in sorted(
        factor_directions,
        key=lambda item: (item[0], item[1].value),
    ):
        definitions.append(
            SeparabilityScoreDefinition(
                key=f"factor:{factor_name}:{direction.value}",
                components=(ScoreComponent(factor_name, direction),),
            )
        )
    return tuple(sorted(definitions, key=lambda item: item.key))


def load_separability_rows(
    *,
    samples_path: Path,
    horizon_days: int,
    factor_names: Iterable[str],
    analysis_date_count: int,
    exclude_tail_date_count: int,
) -> tuple[tuple[SeparabilityRow, ...], tuple[date, ...]]:
    if analysis_date_count <= 0 or exclude_tail_date_count < 0:
        raise ValueError("analysis and exclusion date counts are invalid")
    base = pl.scan_parquet(samples_path).filter(
        (pl.col("horizon_days") == horizon_days) & pl.col("tradable")
    )
    date_strings = (
        base.select(pl.col("feature_date").cast(pl.String).alias("feature_date"))
        .unique()
        .sort("feature_date")
        .collect()["feature_date"]
        .to_list()
    )
    available = date_strings[:-exclude_tail_date_count] if exclude_tail_date_count else date_strings
    selected_date_strings = available[-analysis_date_count:]
    if len(selected_date_strings) < analysis_date_count:
        raise ValueError("dataset does not contain the requested training-only analysis window")
    names = tuple(sorted(set(factor_names)))
    frame = (
        base.filter(pl.col("feature_date").cast(pl.String).is_in(selected_date_strings))
        .select(
            "sample_id",
            "ticker",
            pl.col("feature_date").cast(pl.String).alias("feature_date"),
            "label_value",
            *[
                pl.col("features_json")
                .str.json_path_match(f"$.{name}")
                .cast(pl.Float64, strict=False)
                .alias(name)
                for name in names
            ],
        )
        .collect()
    )
    rows: list[SeparabilityRow] = []
    for item in frame.iter_rows(named=True):
        features = {
            name: float(item[name])
            for name in names
            if item[name] is not None and math.isfinite(float(item[name]))
        }
        label = float(item["label_value"])
        if not math.isfinite(label):
            continue
        rows.append(
            SeparabilityRow(
                sample_id=str(item["sample_id"]),
                ticker=str(item["ticker"]),
                feature_date=date.fromisoformat(str(item["feature_date"])),
                label_value=label,
                features=features,
            )
        )
    return tuple(rows), tuple(date.fromisoformat(value) for value in selected_date_strings)


def _definition_scores(
    group: list[SeparabilityRow],
    definition: SeparabilityScoreDefinition,
    *,
    zscore_clip: float,
) -> tuple[list[SeparabilityRow], list[float]]:
    rows = group
    if not rows:
        return [], []
    normalized_components: list[list[float]] = []
    for component in definition.components:
        direction = -1.0 if component.direction == FactorDirection.LOWER_BETTER else 1.0
        valid = {
            index: float(item.features[component.factor_name])
            for index, item in enumerate(rows)
            if component.factor_name in item.features
            and math.isfinite(float(item.features[component.factor_name]))
        }
        normalized = [0.0] * len(rows)
        if valid:
            sorted_values = sorted(valid.values())
            lower = _quantile(sorted_values, component.winsor_lower)
            upper = _quantile(sorted_values, component.winsor_upper)
            clipped = {
                index: max(lower, min(upper, value)) for index, value in valid.items()
            }
            median = statistics.median(clipped.values())
            scale = statistics.median(
                abs(value - median) for value in clipped.values()
            ) * 1.4826
            if scale <= 1e-12 and len(clipped) > 1:
                scale = statistics.pstdev(clipped.values())
            scale = scale if scale > 1e-12 else 1.0
            for index, value in clipped.items():
                normalized[index] = max(
                    -zscore_clip,
                    min(zscore_clip, ((value - median) / scale) * direction),
                )
        normalized_components.append(normalized)
    total_weight = sum(component.weight for component in definition.components)
    scores = [
        sum(
            values[index] * component.weight
            for values, component in zip(
                normalized_components, definition.components, strict=True
            )
        )
        / total_weight
        for index in range(len(rows))
    ]
    return rows, scores


def _daily_metric(
    feature_date: date,
    definition: SeparabilityScoreDefinition,
    group: list[SeparabilityRow],
    config: HeadSeparabilityConfig,
) -> SeparabilityDailyMetric | None:
    rows, scores = _definition_scores(
        group,
        definition,
        zscore_clip=config.zscore_clip,
    )
    if len(rows) < config.minimum_cross_section_size:
        return None
    positive_rows = sorted(
        (item for item in rows if item.label_value > 0.0),
        key=lambda item: (-item.label_value, item.ticker, item.sample_id),
    )
    head_count = min(
        len(positive_rows),
        max(config.minimum_head_count, math.ceil(len(rows) * config.top_fraction)),
    )
    if head_count <= 0 or head_count >= len(rows):
        return None
    positive_head_ids = {item.sample_id for item in positive_rows[:head_count]}
    head_target = [1 if item.sample_id in positive_head_ids else 0 for item in rows]
    positive_target = [1 if item.label_value > 0.0 else 0 for item in rows]
    if len(set(positive_target)) < 2:
        return None
    selected_count = min(config.precision_n, len(rows))
    selected_indices = sorted(
        range(len(rows)),
        key=lambda index: (-scores[index], rows[index].ticker, rows[index].sample_id),
    )[:selected_count]
    precision = sum(head_target[index] for index in selected_indices) / selected_count
    base_rate = head_count / len(rows)
    return SeparabilityDailyMetric(
        feature_date=feature_date,
        score_key=definition.key,
        sample_count=len(rows),
        positive_head_count=head_count,
        positive_label_rate=sum(positive_target) / len(positive_target),
        head_auc=float(roc_auc_score(head_target, scores)),
        positive_label_auc=float(roc_auc_score(positive_target, scores)),
        average_precision=float(average_precision_score(head_target, scores)),
        precision_at_n=precision,
        precision_lift_at_n=precision / base_rate,
    )


def analyze_head_separability(
    rows: Iterable[SeparabilityRow],
    *,
    market: str,
    dataset_version: str,
    horizon_days: int,
    analysis_dates: Iterable[date],
    excluded_tail_date_count: int,
    score_definitions: Iterable[SeparabilityScoreDefinition] | None = None,
    config: HeadSeparabilityConfig | None = None,
) -> HeadSeparabilityReport:
    settings = config or HeadSeparabilityConfig()
    definitions = tuple(score_definitions or default_separability_score_definitions())
    dates = tuple(sorted(set(analysis_dates)))
    if not dates or not definitions:
        raise ValueError("separability analysis requires dates and score definitions")
    grouped: dict[date, list[SeparabilityRow]] = defaultdict(list)
    allowed_dates = set(dates)
    for item in rows:
        if item.feature_date in allowed_dates:
            grouped[item.feature_date].append(item)
    daily_metrics: list[SeparabilityDailyMetric] = []
    by_score: dict[str, list[SeparabilityDailyMetric]] = defaultdict(list)
    for feature_date in dates:
        group = grouped.get(feature_date, [])
        for definition in definitions:
            metric = _daily_metric(feature_date, definition, group, settings)
            if metric is not None:
                daily_metrics.append(metric)
                by_score[definition.key].append(metric)

    summaries: list[SeparabilityScoreSummary] = []
    for definition in definitions:
        metrics = by_score.get(definition.key, [])
        if len(metrics) != len(dates):
            continue
        block_metrics: list[SeparabilityBlockMetric] = []
        for block_index in range(settings.chronological_block_count):
            start = math.floor(block_index * len(metrics) / settings.chronological_block_count)
            end = math.floor((block_index + 1) * len(metrics) / settings.chronological_block_count)
            block = metrics[start:end]
            auc_mean = statistics.fmean(item.head_auc for item in block)
            lift_mean = statistics.fmean(item.precision_lift_at_n for item in block)
            block_metrics.append(
                SeparabilityBlockMetric(
                    block_index=block_index,
                    start_date=block[0].feature_date,
                    end_date=block[-1].feature_date,
                    date_count=len(block),
                    head_auc_mean=auc_mean,
                    precision_lift_mean=lift_mean,
                    passed=(
                        auc_mean >= settings.minimum_auc_mean
                        and lift_mean >= settings.minimum_precision_lift
                    ),
                )
            )
        auc_values = [item.head_auc for item in metrics]
        precision_values = [item.precision_at_n for item in metrics]
        lift_values = [item.precision_lift_at_n for item in metrics]
        positive_block_count = sum(item.passed for item in block_metrics)
        checks = {
            "head_auc_mean": statistics.fmean(auc_values) >= settings.minimum_auc_mean,
            "head_auc_positive_date_rate": (
                sum(value > 0.5 for value in auc_values) / len(auc_values)
                >= settings.minimum_auc_positive_date_rate
            ),
            "precision_at_n": (
                statistics.fmean(precision_values) >= settings.minimum_precision_at_n
            ),
            "precision_lift": (
                statistics.fmean(lift_values) >= settings.minimum_precision_lift
            ),
            "chronological_blocks": positive_block_count >= settings.minimum_positive_blocks,
        }
        summaries.append(
            SeparabilityScoreSummary(
                score_key=definition.key,
                evaluated_date_count=len(metrics),
                head_auc_mean=statistics.fmean(auc_values),
                head_auc_median=statistics.median(auc_values),
                head_auc_positive_date_rate=sum(value > 0.5 for value in auc_values) / len(auc_values),
                positive_label_auc_mean=statistics.fmean(
                    item.positive_label_auc for item in metrics
                ),
                average_precision_mean=statistics.fmean(
                    item.average_precision for item in metrics
                ),
                precision_at_n_mean=statistics.fmean(precision_values),
                precision_lift_at_n_mean=statistics.fmean(lift_values),
                positive_block_count=positive_block_count,
                block_metrics=tuple(block_metrics),
                passed=all(checks.values()),
                failed_checks=tuple(key for key, passed in checks.items() if not passed),
            )
        )
    summaries.sort(key=lambda item: (-item.head_auc_mean, item.score_key))
    separable = tuple(sorted(item.score_key for item in summaries if item.passed))
    return HeadSeparabilityReport(
        schema_version="stock_selection_head_separability_v1",
        market=str(market).upper(),
        dataset_version=dataset_version,
        horizon_days=horizon_days,
        analysis_dates=dates,
        excluded_tail_date_count=excluded_tail_date_count,
        config=settings,
        score_definitions=definitions,
        summaries=tuple(summaries),
        daily_metrics=tuple(daily_metrics),
        separable_score_keys=separable,
        verdict="PASS" if separable else "FAIL",
    )


def persist_head_separability_report(
    report: HeadSeparabilityReport,
    *,
    root: Path,
) -> HeadSeparabilityEvidenceWriteResult:
    payload = asdict(report)
    canonical = json.dumps(payload, default=_json_default, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    evidence_version = (
        f"stock_selection_head_separability_v1:{report.market}:{report.horizon_days}d:"
        f"{digest[:20]}"
    )
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / evidence_version.replace(":", "_")
    with tempfile.TemporaryDirectory(prefix=".head-separability-", dir=root) as name:
        temporary_dir = Path(name)
        report_path = temporary_dir / "head_separability.json"
        report_path.write_text(
            json.dumps(payload, default=_json_default, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": "stock_selection_head_separability_manifest_v1",
            "evidence_version": evidence_version,
            "report_file": report_path.name,
            "report_sha256": report_sha256,
            "market": report.market,
            "dataset_version": report.dataset_version,
            "horizon_days": report.horizon_days,
            "verdict": report.verdict,
        }
        (temporary_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target_dir.exists():
            existing = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("report_sha256") != report_sha256:
                raise RuntimeError(f"refusing to overwrite separability evidence: {target_dir}")
            return HeadSeparabilityEvidenceWriteResult(
                evidence_version=evidence_version,
                artifact_dir=target_dir,
                manifest_path=target_dir / "manifest.json",
                reused_existing=True,
            )
        temporary_dir.replace(target_dir)
    return HeadSeparabilityEvidenceWriteResult(
        evidence_version=evidence_version,
        artifact_dir=target_dir,
        manifest_path=target_dir / "manifest.json",
        reused_existing=False,
    )


def _json_default(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, FactorDirection):
        return value.value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
