import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.services.stock_selection.p0_diagnostics import (
    build_p0_pipeline_diagnostics,
    load_latest_research_regime_coverage,
)


class P0PipelineDiagnosticsTests(TestCase):
    def policy(self, digest="a" * 64):
        return {"policy_version": "regime_policy_research_v1", "source_snapshot_sha256": digest, "buy_gate": "ALLOW"}

    def screener(self, *, trade_date="2026-09-18", digest="a" * 64):
        return {
            "input": {"input_as_of_date": trade_date},
            "candidate_stats": {"returned_count": 5000, "persisted_count": 5000},
            "regime_diagnostics": {
                "status": "READY", "observation_count": 5000, "tradability_ready_count": 120,
                "regime_shortlist_count": 5, "formal_candidate_count": 0,
                "regime_policy": self.policy(digest),
            },
        }

    def report(self, *, trade_date="2026-09-18", digest="a" * 64):
        return {
            "input_market_dates": {"CN": trade_date},
            "market_recommendations": [],
            "market_watch_recommendations": [{"ticker": "600000.SS"}],
            "market_recommendations_meta": {
                "status": "observation_ready", "blocked_candidate_count": 5,
                "regime_policy": self.policy(digest),
            },
        }

    def test_complete_diagnostics_reconcile_stage_counts_and_identity(self):
        result = build_p0_pipeline_diagnostics(
            market="CN", screener_payload=self.screener(),
            research_coverage={"status": "COMPLETE", "evidence_complete": True, "requested_date_count": 20},
            publication_report=self.report(),
        )
        self.assertEqual("COMPLETE", result["status"])
        self.assertEqual(5000, result["stages"]["screener"]["observation_count"])
        self.assertEqual(5, result["stages"]["screener"]["regime_shortlist_count"])
        self.assertEqual(0, result["stages"]["publication"]["candidate_count"])
        self.assertEqual(1, result["stages"]["publication"]["watch_count"])
        self.assertEqual([], result["blockers"])

    def test_missing_stage_is_partial_and_never_invented_as_zero(self):
        result = build_p0_pipeline_diagnostics(
            market="CN", screener_payload=self.screener(), research_coverage=None, publication_report=None,
        )
        self.assertEqual("PARTIAL", result["status"])
        self.assertEqual("NOT_AVAILABLE", result["stages"]["research"]["status"])
        self.assertEqual("MISSING", result["stages"]["publication"]["status"])
        self.assertIn("missing_research_regime_coverage", result["blockers"])

    def test_date_or_policy_identity_mismatch_blocks(self):
        for report in (self.report(trade_date="2026-09-17"), self.report(digest="b" * 64)):
            result = build_p0_pipeline_diagnostics(
                market="CN", screener_payload=self.screener(),
                research_coverage={"status": "COMPLETE", "evidence_complete": True},
                publication_report=report,
            )
            self.assertEqual("BLOCKED", result["status"])

    def test_market_validation_and_us_without_watch_pool(self):
        with self.assertRaises(ValueError):
            build_p0_pipeline_diagnostics(market="HK", screener_payload=None, research_coverage=None, publication_report=None)
        result = build_p0_pipeline_diagnostics(
            market="US", screener_payload=None, research_coverage=None,
            publication_report={"input_market_dates": {"US": "2026-09-18"}, "us_model_recommendations": []},
        )
        self.assertEqual(0, result["stages"]["publication"]["watch_count"])

    def test_same_date_research_policy_identity_mismatch_blocks(self):
        result = build_p0_pipeline_diagnostics(
            market="CN", screener_payload=self.screener(), publication_report=self.report(),
            research_coverage={
                "status": "COMPLETE", "evidence_complete": True,
                "latest_policy_date": "2026-09-18", "latest_policy": self.policy("b" * 64),
            },
        )
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("screener_research_regime_identity_mismatch", result["blockers"])


class P0ResearchEvidenceLoaderTests(TestCase):
    def _write_evidence(self, root: Path, *, name: str, market="CN", complete=True, legacy=False):
        directory = root / name
        directory.mkdir()
        files = {
            "reports.json": {},
            "fold_audits.json": [],
        }
        if not legacy:
            files.update({
                "regime_policy.json": {
                    "mode": "historical_required",
                    "coverage": {
                        "status": "COMPLETE" if complete else "BLOCKED_MISSING_EVIDENCE",
                        "requested_date_count": 2,
                        "blocked_date_count": 0 if complete else 1,
                    },
                    "policies": {
                        "2026-09-17": self._policy("a" * 64),
                        "2026-09-18": self._policy("b" * 64),
                    },
                },
                "regime_candidates.json": {
                    "ridge": {"2026-09-17": ["one"], "2026-09-18": ["two", "three"]},
                },
            })
        hashes = {}
        for filename, payload in files.items():
            path = directory / filename
            path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            hashes[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
        bundle = hashlib.sha256(json.dumps(
            hashes, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        manifest = {
            "schema_version": "stock_selection_research_evidence_v1",
            "market": market,
            "horizon_days": 5,
            "run_scope": "challenger_research_only",
            "common_evaluated_dates": ["2026-09-17", "2026-09-18"],
            "evaluation_sample_count": 20,
            "evidence_version": f"fixture:{name}",
            "file_sha256": hashes,
            "bundle_sha256": bundle,
        }
        if not legacy:
            manifest.update({
                "regime_policy_mode": files["regime_policy.json"]["mode"],
                "regime_coverage": files["regime_policy.json"]["coverage"],
                "regime_policy_by_date": files["regime_policy.json"]["policies"],
                "regime_candidate_sample_ids": files["regime_candidates.json"],
            })
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return directory

    @staticmethod
    def _policy(digest):
        return {
            "policy_version": "regime_policy_research_v1",
            "source_snapshot_sha256": digest,
            "buy_gate": "ALLOW",
        }

    def test_loads_and_verifies_latest_complete_regime_evidence(self):
        with TemporaryDirectory() as temporary_name:
            directory = self._write_evidence(Path(temporary_name), name="complete")
            result = load_latest_research_regime_coverage(
                artifact_root=Path(temporary_name), market="CN",
            )
        self.assertEqual("COMPLETE", result["status"])
        self.assertTrue(result["evidence_complete"])
        self.assertEqual("VERIFIED", result["integrity_status"])
        self.assertEqual(2, result["evaluated_date_count"])
        self.assertEqual(3, result["candidate_counts_by_model"]["ridge"])
        self.assertEqual("fixture:complete", result["evidence_version"])
        self.assertTrue((directory / "manifest.json").name)

    def test_newest_legacy_or_corrupt_evidence_is_not_hidden_by_fallback(self):
        with TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            old = self._write_evidence(root, name="old")
            newest = self._write_evidence(root, name="newest", legacy=True)
            os.utime(old / "manifest.json", ns=(1, 1))
            os.utime(newest / "manifest.json", ns=(2, 2))
            result = load_latest_research_regime_coverage(artifact_root=root, market="CN")
            self.assertEqual("LEGACY_NO_REGIME_EVIDENCE", result["status"])
            (newest / "reports.json").write_text("tampered", encoding="utf-8")
            result = load_latest_research_regime_coverage(artifact_root=root, market="CN")
            self.assertEqual("CORRUPT_EVIDENCE", result["status"])
            self.assertEqual("checksum_mismatch:reports.json", result["reason"])

    def test_manifest_and_regime_files_must_have_the_same_identity(self):
        with TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            directory = self._write_evidence(root, name="identity")
            manifest_path = directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["regime_coverage"]["blocked_date_count"] = 99
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = load_latest_research_regime_coverage(artifact_root=root, market="CN")
        self.assertEqual("CORRUPT_EVIDENCE", result["status"])
        self.assertEqual("manifest_regime_identity_mismatch", result["reason"])

    def test_missing_market_evidence_is_unknown_not_zero(self):
        with TemporaryDirectory() as temporary_name:
            self._write_evidence(Path(temporary_name), name="cn", market="CN")
            self.assertIsNone(load_latest_research_regime_coverage(
                artifact_root=Path(temporary_name), market="US",
            ))
