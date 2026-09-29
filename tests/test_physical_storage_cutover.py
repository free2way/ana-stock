from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.market_storage_routing import (
    CN_PHYSICAL_CUTOVER_SETTING_KEY,
    CN_PHYSICAL_CUTOVER_VERSION,
    cn_physical_only_cutover_active,
)
from app.services.physical_storage_cutover import (
    CUTOVER_APPLY_TOKEN,
    activate_cn_physical_only,
    validate_cn_physical_cutover_gate,
)


class PhysicalStorageCutoverTests(unittest.TestCase):
    def test_cutover_marker_requires_exact_active_contract(self) -> None:
        db = MagicMock()
        db.scalar.return_value = json.dumps(
            {
                "cutover_version": CN_PHYSICAL_CUTOVER_VERSION,
                "status": "active",
                "market": "CN",
            }
        )
        self.assertTrue(cn_physical_only_cutover_active(db))
        db.scalar.return_value = json.dumps(
            {
                "cutover_version": CN_PHYSICAL_CUTOVER_VERSION,
                "status": "rolled_back",
                "market": "CN",
            }
        )
        self.assertFalse(cn_physical_only_cutover_active(db))

    def test_gate_requires_latest_run_physical_audits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                name: root / f"{name}.json"
                for name in (
                    "manifest",
                    "backup",
                    "restore",
                    "dual",
                    "live",
                    "hot",
                    "snapshots",
                    "isolation",
                )
            }
            dual = {"market": "CN", "sequence_runs": [50, 49, 48, 47, 46]}
            live = {
                "status": "pass",
                "audit_version": "market-physical-live-storage-v2",
                "market": "CN",
                "model_run_id": 50,
                "us_rows": 0,
                "checks": {
                    name: True
                    for name in (
                        "cn_legacy_exact_match",
                        "cn_wrong_market_rows_zero",
                        "cn_symbol_market_mismatch_zero",
                        "repository_reads_cn_physical_table",
                        "composite_symbol_market_fks_present",
                    )
                },
            }
            hot = {
                "status": "pass",
                "audit_version": "market-physical-hot-storage-v2",
                "market": "CN",
                "model_run_id": 49,
                "us_counts": {},
                "checks": {},
            }
            snapshots = {
                "status": "pass",
                "audit_version": "market-physical-snapshots-v2",
                "market": "CN",
                "us_counts": {},
                "checks": {},
            }
            isolation = {
                "status": "pass",
                "table_contract": {
                    "CN": {f"fact_{index}": f"cn_fact_{index}" for index in range(9)},
                    "US": {f"fact_{index}": f"us_fact_{index}" for index in range(9)},
                },
                "checks": {
                    name: True
                    for name in (
                        "eighteen_physical_tables_exist",
                        "cn_us_table_sets_disjoint",
                        "cn_table_prefixes",
                        "us_table_prefixes",
                        "required_physical_write_markets_cn_us",
                        "parent_market_check_constraints_present",
                        "parent_wrong_market_rows_zero",
                        "parent_symbol_market_fks_compliant",
                        "child_tables_reference_same_market_parent",
                        "us_tables_empty_while_hold",
                    )
                },
            }
            for name, payload in (
                ("dual", dual),
                ("live", live),
                ("hot", hot),
                ("snapshots", snapshots),
                ("isolation", isolation),
                ("backup", {}),
                ("restore", {}),
            ):
                paths[name].write_text(json.dumps(payload), encoding="utf-8")
            import hashlib

            manifest = {
                "files": {
                    paths[name].name: {
                        "sha256": hashlib.sha256(paths[name].read_bytes()).hexdigest()
                    }
                    for name in ("live", "hot", "snapshots", "isolation")
                }
            }
            paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")
            with patch(
                "app.services.physical_storage_cutover.validate_retention_foundation_gate",
                return_value={
                    "dual_write_trade_dates": [
                        "2026-08-17",
                        "2026-08-18",
                        "2026-08-19",
                        "2026-08-20",
                        "2026-08-21",
                    ],
                    "backup_sha256": "fixture",
                },
            ):
                with self.assertRaisesRegex(RuntimeError, "hot audit does not cover"):
                    validate_cn_physical_cutover_gate(
                        approval_token=CUTOVER_APPLY_TOKEN,
                        evidence_manifest_path=paths["manifest"],
                        backup_receipt_path=paths["backup"],
                        restore_receipt_path=paths["restore"],
                        dual_write_receipt_path=paths["dual"],
                        live_audit_receipt_path=paths["live"],
                        hot_audit_receipt_path=paths["hot"],
                        snapshot_audit_receipt_path=paths["snapshots"],
                        isolation_audit_receipt_path=paths["isolation"],
                    )

    def test_gate_rejects_pre_expansion_isolation_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            isolation_path = root / "isolation.json"
            isolation_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "table_contract": {
                            "CN": {f"fact_{index}": f"cn_fact_{index}" for index in range(7)},
                            "US": {f"fact_{index}": f"us_fact_{index}" for index in range(7)},
                        },
                        "checks": {"fourteen_physical_tables_exist": True},
                    }
                ),
                encoding="utf-8",
            )
            import hashlib

            manifest = {
                "files": {
                    isolation_path.name: {
                        "sha256": hashlib.sha256(isolation_path.read_bytes()).hexdigest()
                    }
                }
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            placeholder = root / "placeholder.json"
            placeholder.write_text("{}", encoding="utf-8")

            with patch(
                "app.services.physical_storage_cutover.validate_retention_foundation_gate",
                return_value={
                    "dual_write_trade_dates": ["2026-08-21"] * 5,
                    "backup_sha256": "fixture",
                },
            ), patch(
                "app.services.physical_storage_cutover._registered_evidence",
                side_effect=lambda _manifest, *, label, path: (
                    json.loads(path.read_text(encoding="utf-8"))
                    if "twenty-seven-table" in label
                    else {
                        "status": "pass",
                        "audit_version": (
                            "market-physical-live-storage-v2"
                            if "physical live" in label
                            else "market-physical-hot-storage-v2"
                            if "physical hot" in label
                            else "market-physical-snapshots-v2"
                        ),
                        "market": "CN",
                        "model_run_id": 5,
                        "us_rows": 0,
                        "us_counts": {},
                        "checks": {
                            name: True
                            for name in (
                                "physical_tables_exist",
                                "legacy_exact_match",
                                "source_exact_match",
                                "wrong_market_rows_zero",
                                "symbol_market_mismatch_zero",
                                "model_run_market_mismatch_zero",
                                "child_orphans_zero",
                                "constraint_rejects_wrong_market",
                                "all_market_constraints_present",
                                "child_foreign_keys_cascade",
                                "repository_reads_market_physical_table",
                                "repositories_read_market_physical_tables",
                                "explain_is_market_local",
                                "composite_symbol_market_fks_present",
                            )
                        },
                    }
                ),
            ), patch(
                "app.services.physical_storage_cutover._load_json",
                side_effect=lambda path: (
                    {"files": {}}
                    if path.name == manifest_path.name
                    else {"market": "CN", "sequence_runs": [5, 4, 3, 2, 1]}
                    if path.name == placeholder.name
                    else json.loads(path.read_text(encoding="utf-8"))
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "wrong contract version"):
                    validate_cn_physical_cutover_gate(
                        approval_token=CUTOVER_APPLY_TOKEN,
                        evidence_manifest_path=manifest_path,
                        backup_receipt_path=placeholder,
                        restore_receipt_path=placeholder,
                        dual_write_receipt_path=placeholder,
                        live_audit_receipt_path=placeholder,
                        hot_audit_receipt_path=placeholder,
                        snapshot_audit_receipt_path=placeholder,
                        isolation_audit_receipt_path=isolation_path,
                    )

    def test_activation_persists_readable_marker(self) -> None:
        db = MagicMock()
        gate = {
            "status": "pass",
            "market": "CN",
            "latest_run_id": 50,
            "dual_write_run_ids": [50, 49, 48, 47, 46],
            "dual_write_trade_dates": [
                "2026-08-17",
                "2026-08-18",
                "2026-08-19",
                "2026-08-20",
                "2026-08-21",
            ],
            "backup_sha256": "fixture",
            "evidence_sha256": {},
        }
        with patch(
            "app.services.physical_storage_cutover.AppSettingRepository"
        ) as repository, patch(
            "app.services.physical_storage_cutover.cn_physical_only_cutover_active",
            return_value=True,
        ):
            result = activate_cn_physical_only(
                db,
                gate=gate,
                approval_token=CUTOVER_APPLY_TOKEN,
            )

        self.assertEqual("activated", result["action"])
        args = repository.return_value.set.call_args.args
        self.assertEqual(CN_PHYSICAL_CUTOVER_SETTING_KEY, args[0])
        marker = json.loads(args[1])
        self.assertEqual("market-physical-storage-cutover-v2", marker["cutover_version"])
        self.assertEqual("active", marker["status"])


if __name__ == "__main__":
    unittest.main()
