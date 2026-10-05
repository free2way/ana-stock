from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.services.stock_selection.selective_policy import SelectiveEvaluationReport


@dataclass(frozen=True, slots=True)
class PromotionGateConfig:
    minimum_oos_dates: int = 120
    base_cost_bps: float = 20.0
    stress_cost_bps: float = 40.0
    minimum_positive_date_rate: float = 0.5
    minimum_positive_month_ratio: float = 0.5
    minimum_scaled_exposure: float = 0.25


@dataclass(frozen=True, slots=True)
class SelectivePromotionGateConfig:
    minimum_oos_dates: int = 120
    minimum_active_dates: int = 30
    base_cost_bps: float = 20.0
    stress_cost_bps: float = 40.0
    minimum_coverage_rate: float = 0.05
    maximum_coverage_rate: float = 0.60
    minimum_positive_active_date_rate: float = 0.55
    minimum_positive_month_ratio: float = 0.60

    def __post_init__(self) -> None:
        if self.minimum_oos_dates <= 0 or self.minimum_active_dates <= 0:
            raise ValueError("selective promotion date requirements must be positive")
        if self.base_cost_bps < 0 or self.stress_cost_bps <= self.base_cost_bps:
            raise ValueError("stress_cost_bps must be greater than non-negative base_cost_bps")
        if not 0.0 <= self.minimum_coverage_rate <= self.maximum_coverage_rate <= 1.0:
            raise ValueError("selective promotion coverage range is invalid")
        for name, value in (
            ("minimum_positive_active_date_rate", self.minimum_positive_active_date_rate),
            ("minimum_positive_month_ratio", self.minimum_positive_month_ratio),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class PromotionGateCheck:
    key: str
    status: str
    observed: float | bool | None
    threshold: str
    detail: str


@dataclass(frozen=True, slots=True)
class PromotionGateReport:
    schema_version: str
    market: str
    model_key: str
    factor_set_key: str
    horizon_days: int
    top_n: int
    source_evidence_versions: tuple[str, ...]
    decision: str
    champion_action: str
    checks: tuple[PromotionGateCheck, ...]


@dataclass(frozen=True, slots=True)
class PromotionGateEvidenceWriteResult:
    evidence_version: str
    artifact_dir: Path
    manifest_path: Path
    reused_existing: bool


def load_robustness_evidence(artifact_dir: Path) -> tuple[dict[str, Any], str]:
    manifest_path = artifact_dir / "manifest.json"
    report_path = artifact_dir / "robustness.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_bytes = report_path.read_bytes()
    actual_sha256 = hashlib.sha256(report_bytes).hexdigest()
    if manifest.get("report_sha256") != actual_sha256:
        raise ValueError(f"robustness evidence checksum mismatch: {artifact_dir}")
    report = json.loads(report_bytes)
    return report, str(manifest["evidence_version"])


def _metric(report: Mapping[str, Any], section: str, key: str) -> float:
    return float(report[section][key])


def _check(
    key: str,
    passed: bool,
    observed: float | bool,
    threshold: str,
    detail: str,
) -> PromotionGateCheck:
    return PromotionGateCheck(
        key=key,
        status="PASS" if passed else "FAIL",
        observed=observed,
        threshold=threshold,
        detail=detail,
    )


def assess_candidate_promotion(
    reports: Sequence[Mapping[str, Any]],
    *,
    source_evidence_versions: Sequence[str],
    baseline_comparison_passed: bool | None = None,
    config: PromotionGateConfig | None = None,
) -> PromotionGateReport:
    resolved = config or PromotionGateConfig()
    if len(reports) != len(source_evidence_versions) or len(reports) not in {1, 2}:
        raise ValueError("promotion assessment requires one or two aligned evidence reports")

    identity_keys = ("market", "model_key", "factor_set_key", "horizon_days", "top_n")
    identity = tuple(reports[0][key] for key in identity_keys)
    if any(tuple(report[key] for key in identity_keys) != identity for report in reports[1:]):
        raise ValueError("promotion inputs must describe the same candidate")
    evidence_by_cost = {
        float(report["round_trip_cost_bps"]): (report, evidence_version)
        for report, evidence_version in zip(reports, source_evidence_versions, strict=True)
    }
    expected_costs = {resolved.base_cost_bps, resolved.stress_cost_bps}
    supplied_costs = set(evidence_by_cost)
    if (
        len(evidence_by_cost) != len(reports)
        or resolved.base_cost_bps not in supplied_costs
        or not supplied_costs <= expected_costs
        or (len(reports) == 2 and supplied_costs != expected_costs)
    ):
        raise ValueError(
            f"promotion inputs must contain base cost {resolved.base_cost_bps} and optionally "
            f"stress cost {resolved.stress_cost_bps}"
        )
    ordered_costs = sorted(supplied_costs)
    ordered = tuple(evidence_by_cost[cost][0] for cost in ordered_costs)
    ordered_evidence_versions = tuple(evidence_by_cost[cost][1] for cost in ordered_costs)

    oos_dates = min(
        int(report["base_metrics"]["evaluated_date_count"]) for report in ordered
    )
    base_risk_adjusted_label = min(
        _metric(report, "base_metrics", "mean_risk_adjusted_return") for report in ordered
    )
    positive_rate = min(
        _metric(report, "base_metrics", "positive_date_rate") for report in ordered
    )
    ticker_exclusion_label = min(
        _metric(report, "ticker_exclusion_metrics", "mean_risk_adjusted_return")
        for report in ordered
    )
    date_exclusion_label = min(
        _metric(report, "date_exclusion_metrics", "mean_risk_adjusted_return")
        for report in ordered
    )
    monthly_positive_ratios = []
    for report in ordered:
        monthly = report["monthly_metrics"]
        if not monthly:
            raise ValueError("promotion evidence must contain monthly metrics")
        monthly_positive_ratios.append(
            sum(float(item["metrics"]["mean_risk_adjusted_return"]) > 0 for item in monthly)
            / len(monthly)
        )
    monthly_positive_ratio = min(monthly_positive_ratios)
    scaled_risk_adjusted_label = min(
        float(report["point_in_time_gate"]["scaled_metrics"]["mean_risk_adjusted_return"])
        for report in ordered
    )
    scaled_exposure = min(
        float(report["point_in_time_gate"]["average_scaled_exposure"])
        for report in ordered
    )

    checks = [
        _check(
            "minimum_oos_dates",
            oos_dates >= resolved.minimum_oos_dates,
            float(oos_dates),
            f">={resolved.minimum_oos_dates}",
            "Both base and stress evidence must cover the production-review window.",
        ),
        _check(
            "top_n_risk_adjusted_label_across_costs",
            base_risk_adjusted_label > 0,
            base_risk_adjusted_label,
            "risk-adjusted label >0 at every required cost",
            "The Top-N industry-excess label after path-drawdown penalty is the selection-quality hard gate; Rank IC cannot replace it.",
        ),
        _check(
            "positive_oos_date_rate",
            positive_rate >= resolved.minimum_positive_date_rate,
            positive_rate,
            f">={resolved.minimum_positive_date_rate}",
            "The worst required-cost scenario must be positive on at least half of OOS dates.",
        ),
        _check(
            "ticker_exclusion_stability",
            ticker_exclusion_label > 0,
            ticker_exclusion_label,
            "risk-adjusted label >0 at every required cost",
            "The Top-N risk-adjusted label must remain positive after removing the five largest ticker contributors.",
        ),
        _check(
            "date_exclusion_stability",
            date_exclusion_label > 0,
            date_exclusion_label,
            "risk-adjusted label >0 at every required cost",
            "The Top-N risk-adjusted label must remain positive after removing the five most extreme dates.",
        ),
        _check(
            "monthly_direction_stability",
            monthly_positive_ratio >= resolved.minimum_positive_month_ratio,
            monthly_positive_ratio,
            f">={resolved.minimum_positive_month_ratio}",
            "A majority of calendar-month slices must have a positive Top-N risk-adjusted label.",
        ),
        _check(
            "point_in_time_scaled_gate",
            scaled_risk_adjusted_label > 0
            and scaled_exposure >= resolved.minimum_scaled_exposure,
            scaled_risk_adjusted_label,
            f"risk-adjusted label>0 and exposure>={resolved.minimum_scaled_exposure}",
            "The ex-ante breadth-scaled strategy must improve robustness without becoming a trivial cash strategy.",
        ),
    ]
    if resolved.stress_cost_bps not in supplied_costs:
        checks.append(
            PromotionGateCheck(
                key="stress_cost_evidence",
                status="NOT_ENOUGH_EVIDENCE",
                observed=None,
                threshold=f"requires {resolved.stress_cost_bps} bps evidence",
                detail="Base-cost failure may reject early; passing base evidence cannot advance without stress evidence.",
            )
        )
    if baseline_comparison_passed is None:
        checks.append(
            PromotionGateCheck(
                key="transparent_baseline_comparison",
                status="NOT_ENOUGH_EVIDENCE",
                observed=None,
                threshold="must beat both frozen transparent baselines",
                detail="No baseline comparison decision was supplied.",
            )
        )
    else:
        checks.append(
            _check(
                "transparent_baseline_comparison",
                baseline_comparison_passed,
                baseline_comparison_passed,
                "must beat both frozen transparent baselines",
                "Baseline comparison is externally supplied and preserved in the decision artifact.",
            )
        )

    if any(item.status == "FAIL" for item in checks):
        decision = "REJECT"
    elif any(item.status == "NOT_ENOUGH_EVIDENCE" for item in checks):
        decision = "OBSERVE"
    else:
        decision = "ELIGIBLE_FOR_MANUAL_REVIEW"
    return PromotionGateReport(
        schema_version="stock_selection_promotion_gate_v1",
        market=str(identity[0]),
        model_key=str(identity[1]),
        factor_set_key=str(identity[2]),
        horizon_days=int(identity[3]),
        top_n=int(identity[4]),
        source_evidence_versions=ordered_evidence_versions,
        decision=decision,
        champion_action="KEEP_CURRENT",
        checks=tuple(checks),
    )


def assess_selective_candidate_promotion(
    reports: Sequence[SelectiveEvaluationReport],
    *,
    source_evidence_versions: Sequence[str],
    baseline_comparison_passed: bool | None = None,
    config: SelectivePromotionGateConfig | None = None,
) -> PromotionGateReport:
    """Assess a cash-capable selector without rewarding trivial abstention.

    Selection quality is measured on active dates, while minimum activity,
    calendar return and a bounded coverage range prevent a candidate from
    passing simply by making almost no decisions.
    """

    resolved = config or SelectivePromotionGateConfig()
    if len(reports) != len(source_evidence_versions) or len(reports) not in {1, 2}:
        raise ValueError("selective promotion requires one or two aligned evidence reports")
    identity = (
        reports[0].market,
        reports[0].model_key,
        reports[0].policy_version,
        reports[0].horizon_days,
        reports[0].max_selected_per_date,
    )
    if any(
        (
            report.market,
            report.model_key,
            report.policy_version,
            report.horizon_days,
            report.max_selected_per_date,
        )
        != identity
        for report in reports[1:]
    ):
        raise ValueError("selective promotion inputs must describe the same candidate")
    evidence_by_cost = {
        float(report.round_trip_cost_bps): (report, evidence_version)
        for report, evidence_version in zip(reports, source_evidence_versions, strict=True)
    }
    expected_costs = {resolved.base_cost_bps, resolved.stress_cost_bps}
    supplied_costs = set(evidence_by_cost)
    if (
        len(evidence_by_cost) != len(reports)
        or resolved.base_cost_bps not in supplied_costs
        or not supplied_costs <= expected_costs
        or (len(reports) == 2 and supplied_costs != expected_costs)
    ):
        raise ValueError(
            f"selective promotion inputs must contain base cost {resolved.base_cost_bps} "
            f"and optionally stress cost {resolved.stress_cost_bps}"
        )
    ordered_costs = sorted(supplied_costs)
    ordered = tuple(evidence_by_cost[cost][0] for cost in ordered_costs)
    ordered_versions = tuple(evidence_by_cost[cost][1] for cost in ordered_costs)

    oos_dates = min(report.oos_date_count for report in ordered)
    active_dates = min(report.active_date_count for report in ordered)
    coverage_min = min(report.coverage_rate for report in ordered)
    coverage_max = max(report.coverage_rate for report in ordered)
    active_mean = min(report.mean_active_risk_adjusted_return for report in ordered)
    calendar_mean = min(report.mean_calendar_risk_adjusted_return for report in ordered)
    ci95_lower = min(report.active_mean_ci95[0] for report in ordered)
    positive_rate = min(report.positive_active_date_rate for report in ordered)
    monthly_positive_ratio = min(
        sum(item.mean_calendar_risk_adjusted_return > 0 for item in report.monthly_metrics)
        / len(report.monthly_metrics)
        for report in ordered
    )
    date_exclusion_values = [report.extreme_five_dates_excluded_mean for report in ordered]
    ticker_exclusion_values = [report.extreme_five_tickers_excluded_mean for report in ordered]

    checks = [
        _check(
            "minimum_oos_dates",
            oos_dates >= resolved.minimum_oos_dates,
            float(oos_dates),
            f">={resolved.minimum_oos_dates}",
            "The frozen selective policy must cover the full confirmation window.",
        ),
        _check(
            "minimum_active_dates",
            active_dates >= resolved.minimum_active_dates,
            float(active_dates),
            f">={resolved.minimum_active_dates}",
            "A sparse policy still needs enough independent active dates for review.",
        ),
        _check(
            "bounded_coverage_rate",
            coverage_min >= resolved.minimum_coverage_rate
            and coverage_max <= resolved.maximum_coverage_rate,
            coverage_min,
            f"{resolved.minimum_coverage_rate}<=coverage<={resolved.maximum_coverage_rate}",
            "Coverage is bounded so forced daily picks and nearly permanent cash both fail.",
        ),
        _check(
            "positive_active_risk_adjusted_return",
            active_mean > 0,
            active_mean,
            ">0 at every required cost",
            "Selected dates must have positive industry excess after costs and drawdown penalty.",
        ),
        _check(
            "positive_calendar_risk_adjusted_return",
            calendar_mean > 0,
            calendar_mean,
            ">0 with abstention dates treated as cash",
            "The deployable calendar result must remain positive after cash days.",
        ),
        _check(
            "active_mean_ci95_lower_bound",
            ci95_lower > 0,
            ci95_lower,
            ">0 at every required cost",
            "The day-clustered 95% lower bound of the active-date mean must stay positive; "
            "the iid interval is diagnostic only and may not be used to clear this gate.",
        ),
        _check(
            "positive_active_date_rate",
            positive_rate >= resolved.minimum_positive_active_date_rate,
            positive_rate,
            f">={resolved.minimum_positive_active_date_rate}",
            "More than half of active OOS dates must be positive.",
        ),
        _check(
            "monthly_direction_stability",
            monthly_positive_ratio >= resolved.minimum_positive_month_ratio,
            monthly_positive_ratio,
            f">={resolved.minimum_positive_month_ratio}",
            "Positive calendar contribution must persist across months.",
        ),
    ]
    for key, values, detail in (
        (
            "extreme_date_exclusion_stability",
            date_exclusion_values,
            "Performance must remain positive after removing the five most extreme active dates.",
        ),
        (
            "ticker_exclusion_stability",
            ticker_exclusion_values,
            "Performance must remain positive after removing the five largest ticker contributors.",
        ),
    ):
        observed = min((value for value in values if value is not None), default=None)
        checks.append(
            PromotionGateCheck(
                key=key,
                status=(
                    "PASS"
                    if observed is not None
                    and all(value is not None and value > 0 for value in values)
                    else "FAIL"
                ),
                observed=observed,
                threshold=">0 at every required cost",
                detail=detail,
            )
        )
    if resolved.stress_cost_bps not in supplied_costs:
        checks.append(
            PromotionGateCheck(
                key="stress_cost_evidence",
                status="NOT_ENOUGH_EVIDENCE",
                observed=None,
                threshold=f"requires {resolved.stress_cost_bps} bps evidence",
                detail="Passing base-cost evidence cannot advance without frozen stress-cost replay.",
            )
        )
    if baseline_comparison_passed is None:
        checks.append(
            PromotionGateCheck(
                key="transparent_baseline_comparison",
                status="NOT_ENOUGH_EVIDENCE",
                observed=None,
                threshold="must beat frozen transparent and market baselines",
                detail="No paired baseline comparison decision was supplied.",
            )
        )
    else:
        checks.append(
            _check(
                "transparent_baseline_comparison",
                baseline_comparison_passed,
                baseline_comparison_passed,
                "must beat frozen transparent and market baselines",
                "The externally supplied paired comparison is preserved in the decision artifact.",
            )
        )

    if any(item.status == "FAIL" for item in checks):
        decision = "REJECT"
    elif any(item.status == "NOT_ENOUGH_EVIDENCE" for item in checks):
        decision = "OBSERVE"
    else:
        decision = "ELIGIBLE_FOR_MANUAL_REVIEW"
    return PromotionGateReport(
        schema_version="stock_selection_selective_promotion_gate_v1",
        market=identity[0],
        model_key=identity[1],
        factor_set_key=f"selective:{identity[2]}",
        horizon_days=identity[3],
        top_n=identity[4],
        source_evidence_versions=ordered_versions,
        decision=decision,
        champion_action="KEEP_CURRENT",
        checks=tuple(checks),
    )


def persist_promotion_gate_report(
    report: PromotionGateReport,
    *,
    root: Path,
    dataset_hash: str | None = None,
    registry_path: Path | None = None,
) -> PromotionGateEvidenceWriteResult:
    payload = asdict(report)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    evidence_version = (
        f"stock_selection_promotion_gate_v1:{report.market}:{report.horizon_days}d:"
        f"{report.factor_set_key}:{digest[:20]}"
    )
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / evidence_version.replace(":", "_")
    with tempfile.TemporaryDirectory(prefix=".promotion-gate-", dir=root) as temporary_name:
        temporary_dir = Path(temporary_name)
        report_path = temporary_dir / "promotion_gate.json"
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        registry_stats = None
        if dataset_hash:
            from app.services.stock_selection.experiment_registry import attempt_stats, record_attempt

            record_attempt(
                model_key=report.model_key,
                dataset_hash=dataset_hash,
                protocol_id=f"{report.factor_set_key}:{report.horizon_days}d",
                verdict=report.decision,
                metrics={"top_n": report.top_n, "champion_action": report.champion_action},
                rejected=report.decision.upper() not in {"PASS", "PROMOTE"},
                path=registry_path,
            )
            registry_stats = attempt_stats(model_key=report.model_key, path=registry_path)
        manifest = {
            "schema_version": "stock_selection_promotion_gate_manifest_v1",
            "evidence_version": evidence_version,
            "report_file": report_path.name,
            "report_sha256": report_sha256,
            "decision": report.decision,
            "champion_action": report.champion_action,
            "source_evidence_versions": list(report.source_evidence_versions),
            "experiment_registry": registry_stats,
        }
        (temporary_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target_dir.exists():
            existing = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("report_sha256") != report_sha256:
                raise RuntimeError(f"refusing to overwrite promotion evidence: {target_dir}")
            return PromotionGateEvidenceWriteResult(
                evidence_version=evidence_version,
                artifact_dir=target_dir,
                manifest_path=target_dir / "manifest.json",
                reused_existing=True,
            )
        temporary_dir.replace(target_dir)
    return PromotionGateEvidenceWriteResult(
        evidence_version=evidence_version,
        artifact_dir=target_dir,
        manifest_path=target_dir / "manifest.json",
        reused_existing=False,
    )
