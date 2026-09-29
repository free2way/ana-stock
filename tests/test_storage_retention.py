from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.services.storage_retention import (
    RETENTION_APPLY_TOKEN,
    _bounded_retention_ids,
    _split_successful_run_candidates,
    clean_model_history,
    user_authorized_retention_override_token,
    validate_retention_acceptance_gate,
    validate_retention_foundation_gate,
    validate_user_authorized_retention_override_gate,
)


class StorageRetentionPolicyTests(unittest.TestCase):
    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_only_stale_verified_runs_are_purgeable(self) -> None:
        rows = [
            {"id": 9, "market": "CN", "artifact_status": None},
            {"id": 8, "market": "CN", "artifact_status": "verified"},
            {"id": 7, "market": "CN", "artifact_status": "writing"},
            {"id": 6, "market": "CN", "artifact_status": "verified"},
            {"id": 5, "market": "US", "artifact_status": "verified"},
            {"id": 4, "market": "US", "artifact_status": "verified"},
            {"id": 3, "market": "US", "artifact_status": None},
        ]

        purgeable, blocked = _split_successful_run_candidates(rows, keep_runs_per_market=2)

        self.assertEqual(purgeable, [6])
        self.assertEqual(blocked, [7, 3])

    def test_keep_count_is_applied_per_market(self) -> None:
        rows = [
            {"id": 4, "market": "CN", "artifact_status": "verified"},
            {"id": 3, "market": "CN", "artifact_status": "verified"},
            {"id": 2, "market": "US", "artifact_status": "verified"},
            {"id": 1, "market": "US", "artifact_status": "verified"},
        ]

        purgeable, blocked = _split_successful_run_candidates(rows, keep_runs_per_market=1)

        self.assertEqual(purgeable, [3, 1])
        self.assertEqual(blocked, [])

    def test_retention_batch_is_bounded_deduplicated_and_oldest_first(self) -> None:
        self.assertEqual(
            [2, 3, 5],
            _bounded_retention_ids([9, 5, 3, 2, 5], limit=3),
        )

    def test_full_candidate_row_scan_is_opt_in(self) -> None:
        db = MagicMock()
        db.execute.return_value.mappings.return_value.all.side_effect = [[], []]
        db.execute.return_value.scalars.return_value.all.return_value = []
        with (
            patch(
                "app.services.storage_retention.select_workspace_snapshot_retention",
                return_value={
                    "candidate_delete_ids": [],
                    "policy": {},
                },
            ),
            patch(
                "app.services.storage_retention._related_row_counts",
                return_value={"predictions": 0},
            ) as row_counts,
        ):
            result = clean_model_history(db, apply=False)

        self.assertFalse(result["candidate_row_count_scan_enabled"])
        self.assertNotIn("candidate_row_counts", result)
        row_counts.assert_called_once_with(db, [])

    def test_apply_is_rejected_before_database_access_without_acceptance_evidence(self) -> None:
        db = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "explicit destructive approval token"):
            clean_model_history(db, apply=True)

        db.execute.assert_not_called()

    def test_user_authorized_override_requires_its_explicit_token_before_queries(self) -> None:
        db = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "explicit destructive token"):
            clean_model_history(db, apply=True, user_authorized_override=True)

        db.execute.assert_not_called()

    def test_user_authorized_override_keeps_backup_and_manifest_safeguards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "quant.dump"
            backup.write_bytes(b"recoverable backup")
            backup_receipt = root / "backup.json"
            manifest = root / "manifest.json"
            self._write_json(
                backup_receipt,
                {
                    "status": "pass",
                    "generated_at": "2026-09-10T19:00:00+08:00",
                    "database_mutated": False,
                    "backup_path": str(backup),
                    "backup_sha256": self._digest(backup),
                },
            )
            self._write_json(
                manifest,
                {
                    "files": {
                        backup_receipt.name: {"sha256": self._digest(backup_receipt)}
                    }
                },
            )

            with patch("app.services.storage_retention._same_device", return_value=False):
                result = validate_user_authorized_retention_override_gate(
                    markets=["CN"],
                    approval_token=user_authorized_retention_override_token("CN"),
                    evidence_manifest_path=manifest,
                    backup_receipt_path=backup_receipt,
                )

        self.assertEqual("pass", result["status"])
        self.assertEqual("CN", result["market"])
        self.assertIn("physical_only_cutover", result["bypassed_acceptance_checks"])
        self.assertIn("verified_cold_artifact_required", result["preserved_safeguards"])

    def test_prevalidated_override_must_match_target_market(self) -> None:
        db = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "does not match"):
            clean_model_history(
                db,
                markets=["CN"],
                apply=True,
                user_authorized_override=True,
                _prevalidated_acceptance_gate={
                    "status": "pass",
                    "gate": "user_authorized_retention_override_v1",
                    "market": "US",
                },
            )

        db.execute.assert_not_called()

    def test_cli_missing_evidence_writes_blocked_receipt_before_database_connection(self) -> None:
        from scripts.apply_storage_retention_batch import main

        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "blocked.json"
            with (
                patch(
                    "scripts.apply_storage_retention_batch.SessionLocal"
                ) as session_local,
                patch.object(
                    sys,
                    "argv",
                    [
                        "apply_storage_retention_batch.py",
                        "--apply",
                        "--receipt",
                        str(receipt),
                    ],
                ),
                self.assertRaises(SystemExit) as exit_context,
            ):
                main()

            self.assertEqual(1, exit_context.exception.code)
            session_local.assert_not_called()
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual("blocked", payload["status"])
            self.assertEqual("apply", payload["action"])
            self.assertFalse(payload["database_connected"])
            self.assertFalse(payload["database_mutated"])

    def test_apply_is_rejected_before_retention_queries_when_cutover_marker_is_inactive(self) -> None:
        db = MagicMock()
        with (
            patch(
                "app.services.storage_retention.validate_retention_acceptance_gate",
                return_value={"status": "pass"},
            ),
            patch(
                "app.services.storage_retention.cn_physical_only_cutover_active",
                return_value=False,
            ),
            self.assertRaisesRegex(RuntimeError, "cutover marker is not active"),
        ):
            clean_model_history(
                db,
                apply=True,
                approval_token=RETENTION_APPLY_TOKEN,
            )

        db.execute.assert_not_called()

    def test_acceptance_gate_requires_frozen_backup_restore_and_five_dual_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "quant.dump"
            backup.write_bytes(b"verified backup")
            backup_hash = self._digest(backup)
            backup_receipt = root / "backup.json"
            restore_receipt = root / "restore.json"
            dual_receipt = root / "dual.json"
            manifest_path = root / "manifest.json"
            self._write_json(
                backup_receipt,
                {
                    "status": "pass",
                    "generated_at": "2026-08-22T18:00:00+08:00",
                    "backup_path": str(backup),
                    "backup_sha256": backup_hash,
                },
            )
            self._write_json(
                restore_receipt,
                {
                    "status": "pass",
                    "generated_at": "2026-08-22T19:00:00+08:00",
                    "backup_sha256": backup_hash,
                    "critical_table_parity": {"exact_match": True},
                    "restore_database_dropped": True,
                    "source_database_mutated": False,
                },
            )
            self._write_json(
                dual_receipt,
                {
                    "audit_version": "prediction-dual-write-v2",
                    "market": "CN",
                    "status": "pass",
                    "generated_at": "2026-08-22T17:00:00+08:00",
                    "required_runs": 5,
                    "passed_runs": [1, 2, 3, 4, 5],
                    "sequence_runs": [1, 2, 3, 4, 5],
                    "sequence_trade_dates": [
                        "2026-08-17",
                        "2026-08-18",
                        "2026-08-19",
                        "2026-08-20",
                        "2026-08-21",
                    ],
                    "consecutive_trade_dates": True,
                    "failed_runs": [],
                    "pending_runs": [],
                    "remaining_runs": 0,
                    "runs": [
                        {
                            "model_run_id": run_id,
                            "publication_runtime": {"status": "pass"},
                        }
                        for run_id in (1, 2, 3, 4, 5)
                    ],
                },
            )
            self._write_json(
                manifest_path,
                {
                    "files": {
                        path.name: {"sha256": self._digest(path)}
                        for path in (backup_receipt, restore_receipt, dual_receipt)
                    }
                },
            )

            with patch("app.services.storage_retention._same_device", return_value=False):
                result = validate_retention_foundation_gate(
                    markets=["CN"],
                    approval_token=RETENTION_APPLY_TOKEN,
                    evidence_manifest_path=manifest_path,
                    backup_receipt_path=backup_receipt,
                    restore_receipt_path=restore_receipt,
                    dual_write_receipt_path=dual_receipt,
                )

            self.assertEqual("pass", result["status"])
            self.assertEqual([1, 2, 3, 4, 5], result["dual_write_run_ids"])

            self._write_json(
                backup_receipt,
                {
                    "status": "pass",
                    "generated_at": "2026-08-22T16:00:00+08:00",
                    "backup_path": str(backup),
                    "backup_sha256": backup_hash,
                },
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][backup_receipt.name]["sha256"] = self._digest(
                backup_receipt
            )
            self._write_json(manifest_path, manifest)
            with (
                patch("app.services.storage_retention._same_device", return_value=False),
                self.assertRaisesRegex(
                    RuntimeError,
                    "backup must be generated after the final production dual-write",
                ),
            ):
                validate_retention_foundation_gate(
                    markets=["CN"],
                    approval_token=RETENTION_APPLY_TOKEN,
                    evidence_manifest_path=manifest_path,
                    backup_receipt_path=backup_receipt,
                    restore_receipt_path=restore_receipt,
                    dual_write_receipt_path=dual_receipt,
                )

    def test_acceptance_gate_rejects_duplicate_trade_dates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "quant.dump"
            backup.write_bytes(b"verified backup")
            backup_hash = self._digest(backup)
            backup_receipt = root / "backup.json"
            restore_receipt = root / "restore.json"
            dual_receipt = root / "dual.json"
            manifest_path = root / "manifest.json"
            self._write_json(
                backup_receipt,
                {"status": "pass", "backup_path": str(backup), "backup_sha256": backup_hash},
            )
            self._write_json(
                restore_receipt,
                {
                    "status": "pass",
                    "backup_sha256": backup_hash,
                    "critical_table_parity": {"exact_match": True},
                    "restore_database_dropped": True,
                    "source_database_mutated": False,
                },
            )
            self._write_json(
                dual_receipt,
                {
                    "audit_version": "prediction-dual-write-v2",
                    "market": "CN",
                    "status": "pass",
                    "required_runs": 5,
                    "passed_runs": [1, 2, 3, 4, 5],
                    "sequence_runs": [1, 2, 3, 4, 5],
                    "sequence_trade_dates": [
                        "2026-08-18",
                        "2026-08-19",
                        "2026-08-20",
                        "2026-08-21",
                        "2026-08-21",
                    ],
                    "consecutive_trade_dates": True,
                    "failed_runs": [],
                    "pending_runs": [],
                    "remaining_runs": 0,
                    "runs": [
                        {
                            "model_run_id": run_id,
                            "publication_runtime": {"status": "pass"},
                        }
                        for run_id in (1, 2, 3, 4, 5)
                    ],
                },
            )
            self._write_json(
                manifest_path,
                {
                    "files": {
                        path.name: {"sha256": self._digest(path)}
                        for path in (backup_receipt, restore_receipt, dual_receipt)
                    }
                },
            )

            with (
                patch("app.services.storage_retention._same_device", return_value=False),
                self.assertRaisesRegex(RuntimeError, "Five verified production"),
            ):
                validate_retention_foundation_gate(
                    markets=["CN"],
                    approval_token=RETENTION_APPLY_TOKEN,
                    evidence_manifest_path=manifest_path,
                    backup_receipt_path=backup_receipt,
                    restore_receipt_path=restore_receipt,
                    dual_write_receipt_path=dual_receipt,
                )

    def test_acceptance_gate_rejects_receipt_changed_after_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / "receipt.json"
            self._write_json(receipt, {"status": "pass"})
            manifest = root / "manifest.json"
            self._write_json(manifest, {"files": {receipt.name: {"sha256": self._digest(receipt)}}})
            self._write_json(receipt, {"status": "tampered"})

            with self.assertRaisesRegex(RuntimeError, "hash mismatch"):
                validate_retention_acceptance_gate(
                    markets=["CN"],
                    approval_token=RETENTION_APPLY_TOKEN,
                    evidence_manifest_path=manifest,
                    backup_receipt_path=receipt,
                    restore_receipt_path=receipt,
                    dual_write_receipt_path=receipt,
                    cutover_receipt_path=None,
                    physical_only_receipt_path=None,
                )

    def test_retention_gate_requires_post_cutover_physical_only_canary_and_new_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "quant.dump"
            backup.write_bytes(b"verified post-cutover backup")
            backup_hash = self._digest(backup)
            receipts = {
                name: root / f"{name}.json"
                for name in ("backup", "restore", "dual", "cutover", "physical")
            }
            self._write_json(
                receipts["backup"],
                {
                    "status": "pass",
                    "generated_at": "2026-08-23T12:00:00+08:00",
                    "backup_path": str(backup),
                    "backup_sha256": backup_hash,
                },
            )
            self._write_json(
                receipts["restore"],
                {
                    "status": "pass",
                    "generated_at": "2026-08-23T13:00:00+08:00",
                    "backup_sha256": backup_hash,
                    "critical_table_parity": {"exact_match": True},
                    "restore_database_dropped": True,
                    "source_database_mutated": False,
                },
            )
            self._write_json(
                receipts["dual"],
                {
                    "audit_version": "prediction-dual-write-v2",
                    "market": "CN",
                    "status": "pass",
                    "generated_at": "2026-08-22T17:00:00+08:00",
                    "required_runs": 5,
                    "passed_runs": [5, 4, 3, 2, 1],
                    "sequence_runs": [5, 4, 3, 2, 1],
                    "sequence_trade_dates": [
                        "2026-08-17",
                        "2026-08-18",
                        "2026-08-19",
                        "2026-08-20",
                        "2026-08-21",
                    ],
                    "consecutive_trade_dates": True,
                    "failed_runs": [],
                    "pending_runs": [],
                    "remaining_runs": 0,
                    "runs": [
                        {
                            "model_run_id": run_id,
                            "publication_runtime": {"status": "pass"},
                        }
                        for run_id in (5, 4, 3, 2, 1)
                    ],
                },
            )
            self._write_json(
                receipts["cutover"],
                {
                    "status": "success",
                    "action": "activate",
                    "database_connected": True,
                    "database_mutated": True,
                    "activation": {
                        "status": "success",
                        "action": "activated",
                        "marker": {
                            "cutover_version": "market-physical-storage-cutover-v2",
                            "status": "active",
                            "market": "CN",
                            "activated_at": "2026-08-23T10:00:00+08:00",
                        },
                    },
                },
            )
            physical_run = {
                "status": "pass",
                "model_run_id": 6,
                "market": "CN",
                "hot_source_layer": "physical_market_tables",
                "legacy_hot_dual_write": False,
                "legacy_hot_comparisons": None,
                "publication_runtime": {"status": "pass"},
                "storage_contract": {"legacy_hot_dual_write": False},
                "predictions": {
                    "status": "pass",
                    "compact_rows": {"exact_match": True},
                },
                "prediction_details": {"exact_match": True},
                "prediction_explanations": {
                    "status": "pass",
                    "selected_rows": {"exact_match": True},
                },
            }
            self._write_json(
                receipts["physical"],
                {
                    "audit_version": "prediction-dual-write-v2",
                    "status": "pass",
                    "generated_at": "2026-08-23T11:00:00+08:00",
                    "required_runs": 1,
                    "passed_runs": [6],
                    "failed_runs": [],
                    "pending_runs": [],
                    "remaining_runs": 0,
                    "runs": [physical_run],
                },
            )
            manifest_path = root / "manifest.json"
            self._write_json(
                manifest_path,
                {
                    "files": {
                        path.name: {"sha256": self._digest(path)}
                        for path in receipts.values()
                    }
                },
            )

            with patch("app.services.storage_retention._same_device", return_value=False):
                result = validate_retention_acceptance_gate(
                    markets=["CN"],
                    approval_token=RETENTION_APPLY_TOKEN,
                    evidence_manifest_path=manifest_path,
                    backup_receipt_path=receipts["backup"],
                    restore_receipt_path=receipts["restore"],
                    dual_write_receipt_path=receipts["dual"],
                    cutover_receipt_path=receipts["cutover"],
                    physical_only_receipt_path=receipts["physical"],
                )
            self.assertEqual("pass", result["status"])
            self.assertEqual(6, result["physical_only_run_id"])

            physical = json.loads(receipts["physical"].read_text(encoding="utf-8"))
            physical["runs"][0]["legacy_hot_dual_write"] = True
            self._write_json(receipts["physical"], physical)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"][receipts["physical"].name]["sha256"] = self._digest(
                receipts["physical"]
            )
            self._write_json(manifest_path, manifest)
            with (
                patch("app.services.storage_retention._same_device", return_value=False),
                self.assertRaisesRegex(RuntimeError, "physical-only production canary"),
            ):
                validate_retention_acceptance_gate(
                    markets=["CN"],
                    approval_token=RETENTION_APPLY_TOKEN,
                    evidence_manifest_path=manifest_path,
                    backup_receipt_path=receipts["backup"],
                    restore_receipt_path=receipts["restore"],
                    dual_write_receipt_path=receipts["dual"],
                    cutover_receipt_path=receipts["cutover"],
                    physical_only_receipt_path=receipts["physical"],
                )


if __name__ == "__main__":
    unittest.main()
