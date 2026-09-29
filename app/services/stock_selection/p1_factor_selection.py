"""Training-only factor screening and correlation clustering for P1 research."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
from typing import Iterable

from app.services.stock_selection.factor_diagnostics import (
    FactorDiagnosticConfig,
    FactorDiagnosticReport,
    diagnose_factors,
)
from app.services.stock_selection.factor_pipeline import FactorScore, FactorSpec


@dataclass(frozen=True, slots=True)
class P1FactorSelectionConfig:
    training_cutoff_date: date
    minimum_cross_section_size: int = 20
    minimum_date_count: int = 20
    useful_abs_ic_threshold: float = 0.02
    positive_date_rate_threshold: float = 0.55
    redundancy_abs_correlation: float = 0.80
    target_component: str | None = None
    schema_version: str = "p1_training_only_factor_selection_v1"


@dataclass(frozen=True, slots=True)
class P1FactorSelectionResult:
    selection_version: str
    training_cutoff_date: date
    input_factor_names: tuple[str, ...]
    selected_factor_names: tuple[str, ...]
    rejected_factor_reasons: dict[str, str]
    correlation_clusters: tuple[tuple[str, ...], ...]
    training_row_count: int
    evaluated_dates: tuple[date, ...]
    status: str
    diagnostic_report: FactorDiagnosticReport


def _connected_components(names: tuple[str, ...], edges: list[tuple[str, str]]) -> list[set[str]]:
    adjacency = {name: set() for name in names}
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    output = []
    remaining = set(names)
    while remaining:
        root = min(remaining)
        stack = [root]
        component = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(sorted(adjacency[current] - component, reverse=True))
        remaining -= component
        output.append(component)
    return output


def select_p1_factors_training_only(
    scores: Iterable[FactorScore],
    *,
    specs: Iterable[FactorSpec],
    config: P1FactorSelectionConfig,
) -> P1FactorSelectionResult:
    factor_specs = tuple(specs)
    factor_names = tuple(item.name for item in factor_specs)
    if not factor_names or len(factor_names) != len(set(factor_names)):
        raise ValueError("factor specs must be non-empty and unique")
    rows = [
        item
        for item in scores
        if item.feature_date < config.training_cutoff_date
        and item.label_available_date is not None
        and item.label_available_date < config.training_cutoff_date
    ]
    if not rows:
        raise ValueError("no label-mature training rows exist before training_cutoff_date")
    report = diagnose_factors(
        rows,
        config=FactorDiagnosticConfig(
            minimum_cross_section_size=config.minimum_cross_section_size,
            minimum_date_count=config.minimum_date_count,
            useful_abs_ic_threshold=config.useful_abs_ic_threshold,
            positive_date_rate_threshold=config.positive_date_rate_threshold,
            redundancy_abs_correlation=config.redundancy_abs_correlation,
            target_component=config.target_component,
        ),
    )
    if any(item >= config.training_cutoff_date for item in report.evaluated_dates):
        raise RuntimeError("factor selection diagnostics crossed the training cutoff")
    unknown = set(report.factor_summaries) - set(factor_names)
    if unknown:
        raise ValueError(f"scores contain factors outside the frozen candidate set: {sorted(unknown)}")
    high_correlation_edges = [
        (item.left_factor, item.right_factor)
        for item in report.high_correlation_pairs
        if item.left_factor in factor_names and item.right_factor in factor_names
    ]
    clusters = _connected_components(factor_names, high_correlation_edges)
    selected: list[str] = []
    rejected: dict[str, str] = {}
    for component in clusters:
        qualified = [
            name
            for name in component
            if report.factor_summaries.get(name)
            and report.factor_summaries[name].recommendation == "keep"
        ]
        if not qualified:
            for name in component:
                summary = report.factor_summaries.get(name)
                rejected[name] = summary.recommendation if summary else "missing_diagnostics"
            continue
        winner = min(
            qualified,
            key=lambda name: (
                -abs(report.factor_summaries[name].rank_ic_mean or 0.0),
                -report.factor_summaries[name].sample_coverage,
                name,
            ),
        )
        selected.append(winner)
        for name in component:
            if name != winner:
                rejected[name] = (
                    f"redundant_with:{winner}"
                    if name in qualified
                    else report.factor_summaries.get(name).recommendation
                    if report.factor_summaries.get(name)
                    else "missing_diagnostics"
                )
    selected_names = tuple(sorted(selected))
    cluster_payload = tuple(tuple(sorted(item)) for item in clusters)
    identity = {
        "schema_version": config.schema_version,
        "config": asdict(config),
        "input_factor_names": factor_names,
        "selected_factor_names": selected_names,
        "rejected_factor_reasons": rejected,
        "correlation_clusters": cluster_payload,
        "evaluated_dates": report.evaluated_dates,
        "training_sample_ids": sorted(item.sample_id for item in rows),
    }
    encoded = json.dumps(identity, default=str, sort_keys=True, separators=(",", ":"))
    version = f"{config.schema_version}:{hashlib.sha256(encoded.encode()).hexdigest()[:20]}"
    return P1FactorSelectionResult(
        selection_version=version,
        training_cutoff_date=config.training_cutoff_date,
        input_factor_names=factor_names,
        selected_factor_names=selected_names,
        rejected_factor_reasons=dict(sorted(rejected.items())),
        correlation_clusters=cluster_payload,
        training_row_count=len(rows),
        evaluated_dates=report.evaluated_dates,
        status="READY_RESEARCH_ONLY" if selected_names else "NO_QUALIFIED_FACTORS",
        diagnostic_report=report,
    )


__all__ = [
    "P1FactorSelectionConfig",
    "P1FactorSelectionResult",
    "select_p1_factors_training_only",
]
