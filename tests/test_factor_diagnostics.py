from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.services.stock_selection.factor_diagnostics import (
    FactorDiagnosticConfig,
    diagnose_factors,
)
from app.services.stock_selection.factor_pipeline import FactorScore
from app.services.stock_selection.research_artifacts import persist_factor_diagnostic_evidence


class FactorDiagnosticTests(TestCase):
    def _scores(self) -> list[FactorScore]:
        start = date(2026, 1, 2)
        rows: list[FactorScore] = []
        for day_index in range(20):
            feature_date = start + timedelta(days=day_index)
            for ticker_index in range(25):
                target = ticker_index / 100.0 + day_index / 10_000.0
                missing = ("sparse",) if ticker_index >= 10 else ()
                rows.append(
                    FactorScore(
                        sample_id=f"{feature_date.isoformat()}:S{ticker_index:02d}",
                        ticker=f"S{ticker_index:02d}",
                        feature_date=feature_date,
                        label_available_date=feature_date + timedelta(days=3),
                        horizon_days=3,
                        factor_values={
                            "good": target,
                            "duplicate_good": target * 2.0,
                            "bad": -target,
                            "noise": float((ticker_index * 7 + day_index * 3) % 25),
                            "sparse": target if ticker_index < 10 else 0.0,
                        },
                        missing_factors=missing,
                        composite_score=target,
                        cross_sectional_rank=ticker_index / 24.0,
                        label_value=target,
                        label_components={"net_return": target + 0.01},
                    )
                )
        return rows

    def test_reports_factor_direction_stability_coverage_and_redundancy(self) -> None:
        report = diagnose_factors(
            self._scores(),
            config=FactorDiagnosticConfig(
                minimum_cross_section_size=20,
                minimum_date_count=20,
            ),
        )

        self.assertAlmostEqual(1.0, report.factor_summaries["good"].rank_ic_mean or 0.0)
        self.assertEqual("keep", report.factor_summaries["good"].recommendation)
        self.assertAlmostEqual(-1.0, report.factor_summaries["bad"].rank_ic_mean or 0.0)
        self.assertEqual("review_direction", report.factor_summaries["bad"].recommendation)
        self.assertAlmostEqual(0.4, report.factor_summaries["sparse"].sample_coverage)
        self.assertEqual("insufficient_evidence", report.factor_summaries["sparse"].recommendation)
        pairs = {
            (item.left_factor, item.right_factor): item.mean_rank_correlation
            for item in report.high_correlation_pairs
        }
        self.assertAlmostEqual(1.0, pairs[("duplicate_good", "good")])

    def test_can_diagnose_an_explicit_label_component(self) -> None:
        report = diagnose_factors(
            self._scores(),
            config=FactorDiagnosticConfig(
                minimum_cross_section_size=20,
                minimum_date_count=20,
                target_component="net_return",
            ),
        )

        self.assertEqual("net_return", report.target_name)
        self.assertEqual("keep", report.factor_summaries["good"].recommendation)

    def test_mixed_horizons_fail_closed(self) -> None:
        scores = self._scores()
        first = scores[0]
        scores.append(
            FactorScore(
                sample_id="other-horizon",
                ticker="OTHER",
                feature_date=first.feature_date,
                label_available_date=first.label_available_date,
                horizon_days=5,
                factor_values=first.factor_values,
                missing_factors=(),
                composite_score=first.composite_score,
                cross_sectional_rank=first.cross_sectional_rank,
                label_value=first.label_value,
            )
        )
        with self.assertRaisesRegex(ValueError, "exactly one horizon"):
            diagnose_factors(scores)

    def test_diagnostic_evidence_is_immutable_and_idempotent(self) -> None:
        report = diagnose_factors(
            self._scores(),
            config=FactorDiagnosticConfig(
                minimum_cross_section_size=20,
                minimum_date_count=20,
                target_component="net_return",
            ),
        )
        with TemporaryDirectory() as temporary_name:
            kwargs = {
                "root": Path(temporary_name),
                "market": "US",
                "source_version": "lake-v1",
                "universe_version": "universe-v1",
                "dataset_version": "dataset-v2",
                "run_scope": "engineering_subset_not_for_model_selection",
            }
            first = persist_factor_diagnostic_evidence(report, **kwargs)
            second = persist_factor_diagnostic_evidence(report, **kwargs)

            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)
            self.assertEqual(first.evidence_version, second.evidence_version)
