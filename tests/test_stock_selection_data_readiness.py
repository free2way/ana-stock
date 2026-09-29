from __future__ import annotations

from unittest import TestCase
from pathlib import Path
from tempfile import TemporaryDirectory

from app.services.stock_selection.data_readiness import (
    DataReadinessConfig,
    assess_data_readiness,
    persist_data_readiness_report,
)


class StockSelectionDataReadinessTests(TestCase):
    def test_formal_scope_passes_only_when_coverage_gates_pass(self) -> None:
        metrics = {
            f"S{index:03d}": {
                "history_days": 252,
                "duplicate_conflict_days": 0,
            }
            for index in range(10)
        }
        report = assess_data_readiness(
            metrics,
            security_types={ticker: "equity" for ticker in metrics},
            metadata_present={ticker: True for ticker in metrics},
            industries={ticker: "TECH" for ticker in metrics},
            config=DataReadinessConfig(
                market="US",
                minimum_eligible_symbols=10,
                minimum_history_coverage=1.0,
                minimum_industry_coverage=1.0,
            ),
        )

        self.assertTrue(report.passed)
        self.assertEqual("formal_full_market", report.scope)
        self.assertEqual(10, report.history_counts[252])

    def test_missing_history_industry_and_conflicts_are_explicit_blockers(self) -> None:
        metrics = {
            "A": {"history_days": 252, "duplicate_conflict_days": 1},
            "B": {"history_days": 120, "duplicate_conflict_days": 0},
            "ETF": {"history_days": 252, "duplicate_conflict_days": 0},
        }
        report = assess_data_readiness(
            metrics,
            security_types={"A": "equity", "B": "equity", "ETF": "etf"},
            metadata_present={"A": True, "B": True},
            industries={"A": None, "B": "FINANCE"},
            config=DataReadinessConfig(
                market="CN",
                minimum_eligible_symbols=2,
                minimum_history_coverage=0.8,
                minimum_industry_coverage=0.8,
                maximum_duplicate_conflict_rate=0.1,
            ),
        )

        self.assertFalse(report.passed)
        self.assertEqual("engineering_only", report.scope)
        self.assertIn("insufficient_history_eligible_symbols", report.blockers)
        self.assertIn("insufficient_history_coverage", report.blockers)
        self.assertNotIn("insufficient_metadata_coverage", report.blockers)
        self.assertIn("insufficient_industry_coverage", report.blockers)
        self.assertIn("duplicate_price_conflicts", report.blockers)

        with TemporaryDirectory() as temporary_name:
            first = persist_data_readiness_report(
                report,
                source_version="lake-fixture-v1",
                root=Path(temporary_name),
            )
            second = persist_data_readiness_report(
                report,
                source_version="lake-fixture-v1",
                root=Path(temporary_name),
            )
            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)
            self.assertEqual(first.evidence_version, second.evidence_version)

    def test_protocol_specific_history_threshold_is_reported(self) -> None:
        metrics = {
            "A": {"history_days": 305, "duplicate_conflict_days": 0},
            "B": {"history_days": 252, "duplicate_conflict_days": 0},
        }
        report = assess_data_readiness(
            metrics,
            security_types={"A": "equity", "B": "equity"},
            metadata_present={"A": True, "B": True},
            industries={"A": "TECH", "B": "TECH"},
            config=DataReadinessConfig(
                market="CN",
                required_history_sessions=305,
                minimum_eligible_symbols=1,
                minimum_history_coverage=0.5,
            ),
        )

        self.assertTrue(report.passed)
        self.assertEqual(1, report.history_counts[305])

    def test_formal_historical_universe_contract_fails_closed(self) -> None:
        metrics = {
            f"S{index:03d}": {
                "history_days": 252,
                "duplicate_conflict_days": 0,
            }
            for index in range(10)
        }
        report = assess_data_readiness(
            metrics,
            security_types={ticker: "equity" for ticker in metrics},
            metadata_present={ticker: True for ticker in metrics},
            industries={ticker: "TECH" for ticker in metrics},
            config=DataReadinessConfig(
                market="CN",
                minimum_eligible_symbols=10,
                minimum_history_coverage=1.0,
                minimum_metadata_coverage=1.0,
                minimum_industry_coverage=1.0,
                require_historical_universe_contract=True,
            ),
        )

        self.assertFalse(report.passed)
        self.assertEqual("engineering_only", report.scope)
        self.assertIn("historical_security_master_not_verified", report.blockers)
        self.assertIn("historical_membership_not_verified", report.blockers)
        self.assertIn("delisting_history_not_verified", report.blockers)
        self.assertIn("historical_industry_not_verified", report.blockers)
        self.assertIn("universe_revision_history_not_verified", report.blockers)

    def test_historical_universe_contract_requires_every_evidence_dimension(self) -> None:
        metrics = {
            f"S{index:03d}": {
                "history_days": 252,
                "duplicate_conflict_days": 0,
            }
            for index in range(10)
        }
        common = {
            "metrics": metrics,
            "security_types": {ticker: "equity" for ticker in metrics},
            "metadata_present": {ticker: True for ticker in metrics},
            "industries": {ticker: "TECH" for ticker in metrics},
            "config": DataReadinessConfig(
                market="US",
                minimum_eligible_symbols=10,
                minimum_history_coverage=1.0,
                minimum_metadata_coverage=1.0,
                minimum_industry_coverage=1.0,
                require_historical_universe_contract=True,
            ),
        }
        incomplete = assess_data_readiness(
            **common,
            historical_universe_contract={
                "historical_security_master_verified": True,
                "historical_membership_verified": True,
                "delisting_history_verified": True,
                "historical_industry_verified": True,
            },
        )
        complete = assess_data_readiness(
            **common,
            historical_universe_contract={
                "historical_security_master_verified": True,
                "historical_membership_verified": True,
                "delisting_history_verified": True,
                "historical_industry_verified": True,
                "universe_revision_history_verified": True,
            },
        )

        self.assertFalse(incomplete.passed)
        self.assertEqual(("universe_revision_history_not_verified",), incomplete.blockers)
        self.assertTrue(complete.passed)
        self.assertEqual("formal_full_market", complete.scope)
