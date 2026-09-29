from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.storage_architecture_readiness import (
    audit_registered_hard_gate_evidence,
    post_cutover_backup_restore_passed,
    summarize_multi_market_storage_architecture_readiness,
    summarize_storage_architecture_readiness,
    verify_acceptance_manifest,
    verify_us_independent_signoff,
)


class StorageArchitectureReadinessTests(unittest.TestCase):
    @staticmethod
    def _market_state(**overrides):
        values = {
            "dual_write": {
                "audit_version": "prediction-dual-write-v2",
                "status": "pass",
                "required_runs": 5,
                "passed_runs": [5, 4, 3, 2, 1],
                "remaining_runs": 0,
                "consecutive_trade_dates": True,
                "sequence_trade_dates": [
                    "2026-08-17",
                    "2026-08-18",
                    "2026-08-19",
                    "2026-08-20",
                    "2026-08-21",
                ],
                "runs": [
                    {
                        "model_run_id": run_id,
                        "publication_runtime": {"status": "pass"},
                    }
                    for run_id in (5, 4, 3, 2, 1)
                ],
            },
            "retention_preview": {
                "status": "success",
                "blocked_success_runs_without_verified_artifact": 0,
            },
            "failed_predictions": {"status": "pass"},
            "scheduler_enabled": True,
            "cutover_active": True,
            "physical_only_canary_passed": True,
            "physical_only_generated_at": "2026-08-22T18:00:00+08:00",
        }
        values.update(overrides)
        return values

    def _multi_summary(self, **overrides):
        values = {
            "manifest": {"status": "pass"},
            "capacity": {
                "status": "pass",
                "required_intervals": 5,
                "sample_count": 6,
                "sample_dates": [],
                "average_daily_growth_bytes": 1,
                "limit_bytes": 20 * 1024 * 1024,
                "acceptance_scope": "post_cutover_under_size",
                "not_before_date": "2026-08-23",
                "maximum_database_bytes": 5 * 1024 * 1024 * 1024,
            },
            "isolation": {"status": "pass"},
            "market_states": {
                "CN": self._market_state(),
                "US": self._market_state(),
            },
            "frozen_evidence": {"status": "pass"},
            "hot_prediction_rows_by_market": {"CN": 1_000_000, "US": 500_000},
            "post_cutover_backup_restore": {"passed": True},
            "database_bytes": 4 * 1024 * 1024 * 1024,
            "us_signoff": {"status": "pass", "passed": True},
        }
        values.update(overrides)
        return summarize_multi_market_storage_architecture_readiness(**values)

    def _summary(self, **overrides):
        values = {
            "manifest": {"status": "pass"},
            "dual_write": {
                "audit_version": "prediction-dual-write-v2",
                "status": "pass",
                "required_runs": 5,
                "passed_runs": [5, 4, 3, 2, 1],
                "remaining_runs": 0,
                "consecutive_trade_dates": True,
                "sequence_trade_dates": [
                    "2026-08-17",
                    "2026-08-18",
                    "2026-08-19",
                    "2026-08-20",
                    "2026-08-21",
                ],
                "runs": [
                    {
                        "model_run_id": run_id,
                        "publication_runtime": {"status": "pass"},
                    }
                    for run_id in (5, 4, 3, 2, 1)
                ],
            },
            "capacity": {
                "status": "pass",
                "required_intervals": 5,
                "sample_count": 6,
                "sample_dates": [],
                "average_daily_growth_bytes": 1,
                "limit_bytes": 20 * 1024 * 1024,
                "acceptance_scope": "post_cutover_under_size",
                "not_before_date": "2026-08-23",
                "maximum_database_bytes": 5 * 1024 * 1024 * 1024,
            },
            "isolation": {"status": "pass"},
            "retention_preview": {
                "status": "success",
                "blocked_success_runs_without_verified_artifact": 0,
            },
            "frozen_evidence": {"status": "pass"},
            "failed_predictions": {"status": "pass"},
            "hot_prediction_rows": 1_000_000,
            "cutover_active": True,
            "physical_only_canary_passed": True,
            "physical_only_generated_at": "2026-08-22T18:00:00+08:00",
            "post_cutover_backup_restore": {"passed": True},
            "database_bytes": 4 * 1024 * 1024 * 1024,
            "us_signoff": {"status": "pass", "passed": True},
            "us_hold": False,
        }
        values.update(overrides)
        return summarize_storage_architecture_readiness(**values)

    def test_complete_requires_every_hard_gate(self) -> None:
        result = self._summary()

        self.assertEqual("pass", result["status"])
        self.assertEqual("complete", result["stage"])
        self.assertTrue(result["overall_acceptance_ready"])

    def test_v3_requires_both_markets_and_reports_separate_progress(self) -> None:
        us_state = self._market_state(
            dual_write={
                "audit_version": "prediction-dual-write-v2",
                "status": "fail",
                "required_runs": 5,
                "passed_runs": [313, 309],
                "remaining_runs": 3,
                "consecutive_trade_dates": True,
                "sequence_trade_dates": ["2026-09-02", "2026-09-03"],
            },
            cutover_active=False,
            physical_only_canary_passed=False,
        )
        result = self._multi_summary(
            market_states={"CN": self._market_state(), "US": us_state}
        )

        self.assertEqual("storage-architecture-readiness-v3", result["readiness_version"])
        self.assertTrue(result["deletion_gate_ready_by_market"]["CN"])
        self.assertFalse(result["deletion_gate_ready_by_market"]["US"])
        self.assertEqual(2, result["markets"]["US"]["dual_write_progress"]["passed_runs"])
        self.assertEqual(3, result["markets"]["US"]["dual_write_progress"]["remaining_runs"])
        self.assertEqual("collecting_pre_cutover_evidence", result["markets"]["US"]["stage"])
        self.assertEqual(
            "continue_daily_dual_write_observation",
            result["markets"]["US"]["next_action"],
        )
        self.assertFalse(result["overall_acceptance_ready"])

    def test_v3_does_not_call_active_us_hold_when_signoff_is_pending(self) -> None:
        result = self._multi_summary(
            us_signoff={"status": "missing", "passed": False}
        )

        self.assertEqual("active", result["us_operational_status"])
        self.assertEqual("pending", result["us_signoff_status"])
        self.assertEqual(
            "awaiting_independent_production_signoff",
            result["markets"]["US"]["stage"],
        )
        self.assertNotIn("hold", str(result).lower())
        self.assertFalse(result["overall_acceptance_ready"])

    def test_v3_disabled_scheduler_fails_closed_for_only_that_market(self) -> None:
        result = self._multi_summary(
            market_states={
                "CN": self._market_state(),
                "US": self._market_state(scheduler_enabled=False),
            }
        )

        self.assertTrue(result["deletion_gate_ready_by_market"]["CN"])
        self.assertFalse(result["deletion_gate_ready_by_market"]["US"])
        self.assertEqual("disabled", result["markets"]["US"]["operational_status"])
        self.assertEqual(
            "operational_scheduler_disabled", result["markets"]["US"]["stage"]
        )

    def test_current_phase_does_not_confuse_partial_dual_write_with_readiness(self) -> None:
        result = self._summary(
            dual_write={
                "audit_version": "prediction-dual-write-v2",
                "status": "pending",
                "required_runs": 5,
                "passed_runs": [293],
                "remaining_runs": 4,
                "consecutive_trade_dates": False,
                "sequence_trade_dates": ["2026-08-21"],
            },
            capacity={
                "status": "collecting",
                "required_intervals": 5,
                "sample_count": 2,
                "sample_dates": ["2026-08-22", "2026-08-23"],
            },
            cutover_active=False,
            physical_only_canary_passed=False,
            post_cutover_backup_restore={"passed": False},
            database_bytes=25 * 1024 * 1024 * 1024,
            us_hold=True,
        )

        self.assertEqual("in_progress", result["status"])
        self.assertEqual("collecting_pre_cutover_evidence", result["stage"])
        self.assertFalse(result["cn_deletion_gate_ready"])
        self.assertEqual(1, result["dual_write_progress"]["passed_runs"])
        self.assertEqual(4, result["dual_write_progress"]["remaining_runs"])

    def test_final_readiness_requires_all_registered_and_live_hard_gates(self) -> None:
        missing_evidence = self._summary(frozen_evidence={"status": "failed"})
        failed_rows = self._summary(failed_predictions={"status": "fail"})
        oversized_hot_store = self._summary(hot_prediction_rows=3_000_001)

        self.assertFalse(missing_evidence["overall_acceptance_ready"])
        self.assertIn("registered_hard_gate_evidence", missing_evidence["blockers"])
        self.assertFalse(failed_rows["cn_final_acceptance_ready"])
        self.assertIn("cn_failed_runs_online_rows_zero", failed_rows["blockers"])
        self.assertFalse(oversized_hot_store["cn_final_acceptance_ready"])
        self.assertIn(
            "physical_hot_prediction_rows_at_most_3m",
            oversized_hot_store["blockers"],
        )

    def test_generic_or_pre_cutover_capacity_window_cannot_pass_final_gate(self) -> None:
        result = self._summary(
            capacity={
                "status": "pass",
                "required_intervals": 5,
                "sample_count": 6,
                "sample_dates": [
                    "2026-08-18",
                    "2026-08-19",
                    "2026-08-20",
                    "2026-08-21",
                    "2026-08-22",
                    "2026-08-23",
                ],
                "average_daily_growth_bytes": -4_000_000_000,
                "limit_bytes": 20 * 1024 * 1024,
                "acceptance_scope": "pre_cutover_baseline",
            }
        )

        self.assertFalse(result["cn_final_acceptance_ready"])
        self.assertIn("five_day_growth_at_most_20_mib", result["blockers"])

    def test_empty_registered_evidence_profile_fails_closed(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"files": {}}), encoding="utf-8")

            result = audit_registered_hard_gate_evidence(manifest)

        self.assertEqual("failed", result["status"])
        self.assertEqual(8, len(result["blockers"]))

    def test_us_cannot_pass_by_only_clearing_hold_flag(self) -> None:
        result = self._summary(
            us_hold=False,
            us_signoff={"status": "missing", "passed": False},
        )

        self.assertFalse(result["overall_acceptance_ready"])
        self.assertIn("us_independent_production_signoff", result["blockers"])

    def test_us_signoff_requires_registered_semantic_receipt(self) -> None:
        import hashlib
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "us-signoff.json"
            receipt.write_text(
                json.dumps(
                    {
                        "signoff_version": "us-market-storage-signoff-v1",
                        "status": "pass",
                        "market": "US",
                        "checks": {"physical_write": True, "restore": True},
                        "cold_recovery_run_ids": [1, 2, 3],
                        "production_run_ids": [4],
                        "wrong_market_rows": 0,
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": {}}), encoding="utf-8")
            unregistered = verify_us_independent_signoff(
                evidence_manifest_path=manifest,
                receipt_path=receipt,
            )
            manifest.write_text(
                json.dumps(
                    {
                        "files": {
                            receipt.name: {
                                "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            registered = verify_us_independent_signoff(
                evidence_manifest_path=manifest,
                receipt_path=receipt,
            )

        self.assertFalse(unregistered["passed"])
        self.assertTrue(registered["passed"])

    def test_missing_us_signoff_is_pending_not_operational_hold(self) -> None:
        result = verify_us_independent_signoff(
            evidence_manifest_path="unused-when-receipt-is-missing.json",
            receipt_path=None,
        )

        self.assertEqual("pending", result["status"])
        self.assertFalse(result["passed"])

    def test_manifest_verifier_rejects_changed_registered_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.json"
            evidence.write_text("original", encoding="utf-8")
            import hashlib
            import json

            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "files": {
                            evidence.name: {
                                "sha256": hashlib.sha256(
                                    evidence.read_bytes()
                                ).hexdigest()
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            evidence.write_text("changed", encoding="utf-8")

            result = verify_acceptance_manifest(manifest)

        self.assertEqual("failed", result["status"])
        self.assertEqual(["evidence.json"], result["hash_mismatches"])

    def test_post_cutover_backup_requires_registered_receipts_and_backup_hash(self) -> None:
        import hashlib
        import json

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_file = root / "quant.dump"
            backup_file.write_bytes(b"backup")
            backup_sha = hashlib.sha256(backup_file.read_bytes()).hexdigest()
            backup_receipt = root / "backup.json"
            restore_receipt = root / "restore.json"
            backup_receipt.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "generated_at": "2026-08-22T19:00:00+08:00",
                        "backup_path": str(backup_file),
                        "backup_sha256": backup_sha,
                    }
                ),
                encoding="utf-8",
            )
            restore_receipt.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "generated_at": "2026-08-22T20:00:00+08:00",
                        "backup_sha256": backup_sha,
                        "critical_table_parity": {"exact_match": True},
                        "restore_database_dropped": True,
                        "source_database_mutated": False,
                    }
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": {}}), encoding="utf-8")

            unregistered = post_cutover_backup_restore_passed(
                evidence_manifest_path=manifest,
                backup_receipt_path=backup_receipt,
                restore_receipt_path=restore_receipt,
                physical_only_generated_at="2026-08-22T18:00:00+08:00",
            )
            manifest.write_text(
                json.dumps(
                    {
                        "files": {
                            path.name: {
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                            }
                            for path in (backup_receipt, restore_receipt)
                        }
                    }
                ),
                encoding="utf-8",
            )
            registered = post_cutover_backup_restore_passed(
                evidence_manifest_path=manifest,
                backup_receipt_path=backup_receipt,
                restore_receipt_path=restore_receipt,
                physical_only_generated_at="2026-08-22T18:00:00+08:00",
            )

        self.assertFalse(unregistered["passed"])
        self.assertFalse(unregistered["receipts_registered"])
        self.assertTrue(registered["passed"])
        self.assertTrue(registered["backup_file_verified"])


if __name__ == "__main__":
    unittest.main()
