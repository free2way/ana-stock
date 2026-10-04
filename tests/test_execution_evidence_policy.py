import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from scripts.apply_execution_evidence_policy import apply_policy


class ExecutionEvidencePolicyTests(TestCase):
    def source(self, price="PASS", action="PASS"):
        return {
            "schema": "hithink_cn_execution_fact_gap_v1",
            "scope": {"tickers": ["000001.SZ"], "trading_dates": ["2026-09-29"]},
            "price_crosscheck": {"000001.SZ": {"status": price}},
            "corporate_action_crosscheck": {"000001.SZ": {"status": action}},
        }

    def apply(self, source):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "source.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            return apply_policy(source, source_path=path)

    def test_optional_facts_are_visible_but_non_blocking(self):
        result = self.apply(self.source())
        self.assertEqual("READY", result["small_scope_status"])
        self.assertTrue(result["training_authorized_by_policy_receipt"])
        self.assertEqual(2, len(result["rows"][0]["optional_evidence_gaps"]))
        self.assertFalse(result["old_results_rewritten"])

    def test_required_price_or_action_conflict_still_blocks(self):
        for source in (self.source(price="BLOCKED"), self.source(action="BLOCKED")):
            with self.subTest(source=source):
                result = self.apply(source)
                self.assertEqual("BLOCKED", result["small_scope_status"])
                self.assertFalse(result["training_authorized_by_policy_receipt"])
