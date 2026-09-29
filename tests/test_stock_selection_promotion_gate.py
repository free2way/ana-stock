from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from app.services.stock_selection.promotion_gate import (
    SelectivePromotionGateConfig,
    assess_candidate_promotion,
    assess_selective_candidate_promotion,
    persist_promotion_gate_report,
)
from app.services.stock_selection.selective_policy import (
    SelectiveEvaluationReport,
    SelectiveMonthlyMetric,
)


def _report(cost: float, *, excess: float = 0.01) -> dict[str, object]:
    metrics = {
        "evaluated_date_count": 120,
        "mean_risk_adjusted_return": excess,
        "positive_date_rate": 0.6,
    }
    return {
        "market": "CN",
        "model_key": "equal_weight",
        "factor_set_key": "candidate",
        "horizon_days": 5,
        "top_n": 5,
        "round_trip_cost_bps": cost,
        "base_metrics": metrics,
        "ticker_exclusion_metrics": {"mean_risk_adjusted_return": excess},
        "date_exclusion_metrics": {"mean_risk_adjusted_return": excess},
        "monthly_metrics": [
            {"key": "2026-01", "metrics": {"mean_risk_adjusted_return": excess}},
            {"key": "2026-02", "metrics": {"mean_risk_adjusted_return": excess}},
        ],
        "point_in_time_gate": {
            "average_scaled_exposure": 0.7,
            "scaled_metrics": {"mean_risk_adjusted_return": excess},
        },
    }


def _selective_report(
    cost: float,
    *,
    excess: float = 0.01,
    coverage: float = 0.25,
    ci95_lower: float = 0.005,
) -> SelectiveEvaluationReport:
    return SelectiveEvaluationReport(
        schema_version="selective_stock_evaluation_v1",
        market="CN",
        model_key="selective-shadow",
        model_versions=("model-v1",),
        policy_version="policy-v1",
        horizon_days=5,
        max_selected_per_date=5,
        round_trip_cost_bps=cost,
        oos_date_count=120,
        active_date_count=30,
        abstention_date_count=90,
        coverage_rate=coverage,
        average_selected_count=0.5,
        mean_active_risk_adjusted_return=excess,
        mean_calendar_risk_adjusted_return=excess * coverage,
        positive_active_date_rate=0.70,
        positive_selected_rate=0.70,
        active_mean_ci95=(ci95_lower, excess + 0.005),
        extreme_five_dates_excluded_mean=excess,
        extreme_five_tickers_excluded_mean=excess,
        monthly_metrics=tuple(
            SelectiveMonthlyMetric(
                month=f"2026-{month:02d}",
                oos_date_count=20,
                active_date_count=5,
                mean_calendar_risk_adjusted_return=excess * coverage,
                mean_active_risk_adjusted_return=excess,
            )
            for month in range(1, 7)
        ),
        daily_metrics=(),
    )
class PromotionGateTests(unittest.TestCase):
    def test_negative_top_tail_is_rejected_even_with_complete_oos(self) -> None:
        report = assess_candidate_promotion(
            [_report(20.0), _report(40.0, excess=-0.001)],
            source_evidence_versions=["base", "stress"],
            baseline_comparison_passed=True,
        )
        self.assertEqual("REJECT", report.decision)
        failed = {item.key for item in report.checks if item.status == "FAIL"}
        self.assertIn("top_n_risk_adjusted_label_across_costs", failed)

    def test_missing_baseline_evidence_cannot_be_auto_promoted(self) -> None:
        report = assess_candidate_promotion(
            [_report(20.0), _report(40.0)],
            source_evidence_versions=["base", "stress"],
        )
        self.assertEqual("OBSERVE", report.decision)
        self.assertEqual("KEEP_CURRENT", report.champion_action)

    def test_base_cost_failure_rejects_without_running_stress_cost(self) -> None:
        report = assess_candidate_promotion(
            [_report(20.0, excess=-0.001)],
            source_evidence_versions=["base"],
        )
        self.assertEqual("REJECT", report.decision)
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("NOT_ENOUGH_EVIDENCE", statuses["stress_cost_evidence"])

    def test_passing_base_cost_waits_for_stress_evidence(self) -> None:
        report = assess_candidate_promotion(
            [_report(20.0)],
            source_evidence_versions=["base"],
            baseline_comparison_passed=True,
        )
        self.assertEqual("OBSERVE", report.decision)
        self.assertEqual("KEEP_CURRENT", report.champion_action)

    def test_all_gates_only_make_candidate_eligible_for_manual_review(self) -> None:
        report = assess_candidate_promotion(
            [_report(20.0), _report(40.0)],
            source_evidence_versions=["base", "stress"],
            baseline_comparison_passed=True,
        )
        self.assertEqual("ELIGIBLE_FOR_MANUAL_REVIEW", report.decision)
        self.assertEqual("KEEP_CURRENT", report.champion_action)

    def test_persistence_is_immutable_and_idempotent(self) -> None:
        report = assess_candidate_promotion(
            [_report(20.0), _report(40.0)],
            source_evidence_versions=["base", "stress"],
            baseline_comparison_passed=True,
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            first = persist_promotion_gate_report(report, root=root)
            second = persist_promotion_gate_report(report, root=root)
            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual("ELIGIBLE_FOR_MANUAL_REVIEW", manifest["decision"])

    def test_cost_or_candidate_mismatch_fails_closed(self) -> None:
        mismatched = _report(40.0)
        mismatched["top_n"] = 10
        with self.assertRaises(ValueError):
            assess_candidate_promotion(
                [_report(20.0), mismatched],
                source_evidence_versions=["base", "stress"],
            )
        with self.assertRaises(ValueError):
            assess_candidate_promotion(
                [_report(20.0), _report(30.0)],
                source_evidence_versions=["base", "stress"],
            )
        with self.assertRaises(ValueError):
            assess_candidate_promotion(
                [_report(40.0)],
                source_evidence_versions=["stress"],
            )

    def test_evidence_order_does_not_change_decision_artifact(self) -> None:
        forward = assess_candidate_promotion(
            [_report(20.0), _report(40.0)],
            source_evidence_versions=["base", "stress"],
        )
        reversed_order = assess_candidate_promotion(
            [_report(40.0), _report(20.0)],
            source_evidence_versions=["stress", "base"],
        )
        self.assertEqual(asdict(forward), asdict(reversed_order))

    def test_selective_gate_allows_only_manual_review_after_stress_pass(self) -> None:
        report = assess_selective_candidate_promotion(
            [_selective_report(20.0), _selective_report(40.0)],
            source_evidence_versions=["selective-base", "selective-stress"],
            baseline_comparison_passed=True,
        )
        self.assertEqual("ELIGIBLE_FOR_MANUAL_REVIEW", report.decision)
        self.assertEqual("KEEP_CURRENT", report.champion_action)

    def test_selective_gate_rejects_trivial_abstention_or_uncertain_mean(self) -> None:
        sparse = assess_selective_candidate_promotion(
            [_selective_report(20.0, coverage=0.01)],
            source_evidence_versions=["selective-base"],
        )
        self.assertEqual("REJECT", sparse.decision)
        uncertain = assess_selective_candidate_promotion(
            [_selective_report(20.0, ci95_lower=-0.001)],
            source_evidence_versions=["selective-base"],
            config=SelectivePromotionGateConfig(),
        )
        self.assertEqual("REJECT", uncertain.decision)


if __name__ == "__main__":
    unittest.main()
