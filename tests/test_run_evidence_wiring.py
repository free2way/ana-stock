"""Wiring tests for the run-level readiness / statistical evidence producers.

Gap 4 (``acceptance/signoff/acceptance-debt-registry-zh.md``): the producers
existed but nothing invoked them at run time, so the two gate checks stayed
``NOT_ENOUGH_EVIDENCE``.  These tests prove the wiring is real (the producers
are invoked and their output is persisted under audit keys), that the read side
can fetch it, and -- the red line -- that the gate decision is *unchanged*
because the gate's own config keys are never written.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.services.stock_selection.promotion_enforcement import (
    build_promotion_candidate,
)
from app.services.stock_selection.promotion_gate_v2 import evaluate_promotion_gate
from app.services.stock_selection.run_evidence import (
    DATA_READINESS_AUDIT_KEY,
    STATISTICAL_GATE_AUDIT_KEY,
    build_evidence_audit_config,
    produce_data_readiness_evidence,
    produce_statistical_gate_evidence,
    read_run_evidence_audit,
)


def _fake_readiness_result(*, passed: bool) -> SimpleNamespace:
    report = SimpleNamespace(
        market="CN",
        passed=passed,
        blockers=() if passed else ("historical_membership_not_verified",),
        scope="formal_full_market" if passed else "engineering_only",
    )
    evidence = SimpleNamespace(
        evidence_version="stock_selection_data_readiness_v1:CN:deadbeefdeadbeefdead",
        artifact_dir=Path("/tmp/readiness/CN_deadbeef"),
        reused_existing=True,
    )
    return SimpleNamespace(report=report, evidence=evidence, source_version="lake-v1")


class ProduceReadinessEvidenceTests(TestCase):
    def test_producer_invoked_and_enveloped(self) -> None:
        with patch(
            "app.services.stock_selection.production_research."
            "audit_market_research_readiness",
            return_value=_fake_readiness_result(passed=False),
        ) as producer:
            envelope = produce_data_readiness_evidence(market="CN")

        producer.assert_called_once()
        self.assertTrue(envelope["produced"])
        self.assertFalse(envelope["passed"])
        self.assertEqual(["historical_membership_not_verified"], envelope["blockers"])
        self.assertEqual("lake-v1", envelope["source_version"])
        self.assertIn("evidence_version", envelope)

    def test_producer_failure_is_recorded_not_faked(self) -> None:
        with patch(
            "app.services.stock_selection.production_research."
            "audit_market_research_readiness",
            side_effect=ValueError("no CN market lake symbols found"),
        ):
            envelope = produce_data_readiness_evidence(market="CN")

        self.assertFalse(envelope["produced"])
        self.assertIn("no CN market lake symbols found", envelope["missing_reason"])
        self.assertNotIn("passed", envelope)


class ProduceStatisticalEvidenceTests(TestCase):
    def test_no_robustness_evidence_records_reason(self) -> None:
        envelope = produce_statistical_gate_evidence(robustness_evidence_dirs=[])
        self.assertFalse(envelope["produced"])
        self.assertIn("robustness-evidence", envelope["missing_reason"])

    def test_sub_gate_report_is_enveloped(self) -> None:
        report = SimpleNamespace(
            decision="OBSERVE",
            checks=(
                SimpleNamespace(key="transparent_baseline_comparison", status="NOT_ENOUGH_EVIDENCE"),
                SimpleNamespace(key="positive_oos_date_rate", status="FAIL"),
            ),
            source_evidence_versions=("robustness:base",),
        )
        write = SimpleNamespace(
            evidence_version="stock_selection_promotion_gate_v1:CN:abc",
            artifact_dir=Path("/tmp/promotion_gates/CN_abc"),
            reused_existing=False,
        )
        with (
            patch(
                "app.services.stock_selection.promotion_gate.load_robustness_evidence",
                return_value=({"market": "CN"}, "robustness:base"),
            ),
            patch(
                "app.services.stock_selection.promotion_gate.assess_candidate_promotion",
                return_value=report,
            ),
            patch(
                "app.services.stock_selection.promotion_gate.persist_promotion_gate_report",
                return_value=write,
            ),
        ):
            envelope = produce_statistical_gate_evidence(
                robustness_evidence_dirs=[Path("/tmp/robustness/base")]
            )

        self.assertTrue(envelope["produced"])
        self.assertEqual("OBSERVE", envelope["decision"])
        self.assertEqual(["positive_oos_date_rate"], envelope["failed_checks"])
        self.assertEqual(["transparent_baseline_comparison"], envelope["missing_checks"])


class ReadSideAndRedLineTests(TestCase):
    def test_reader_returns_persisted_envelopes(self) -> None:
        config = build_evidence_audit_config(
            {"run_scope": "full_market"},
            data_readiness={"produced": True, "passed": False, "blockers": ["x"]},
            statistical_evidence={"produced": False, "missing_reason": "no evidence"},
        )
        run = SimpleNamespace(config_json=json.dumps(config))

        audit = read_run_evidence_audit(run)

        self.assertEqual(["x"], audit["data_readiness"]["blockers"])
        self.assertFalse(audit["statistical_evidence"]["produced"])

    def test_reader_tolerates_runs_without_wiring(self) -> None:
        audit = read_run_evidence_audit(SimpleNamespace(config_json="{}"))
        self.assertIsNone(audit["data_readiness"])
        self.assertIsNone(audit["statistical_evidence"])

    def test_audit_keys_do_not_change_the_gate_decision(self) -> None:
        # A run config carrying the produced evidence under audit keys only --
        # the gate must still see missing evidence (NOT_ENOUGH_EVIDENCE), exactly
        # as before the wiring, so the CN serving surface is untouched.
        config = build_evidence_audit_config(
            {"status": "success", "run_scope": "full_market"},
            data_readiness={"produced": True, "passed": False, "blockers": ["x"]},
            statistical_evidence={"produced": True, "decision": "REJECT"},
        )
        run = SimpleNamespace(
            id=398,
            name="cn_close_2026-10-09",
            market="CN",
            status="success",
            config_json=json.dumps(config),
        )

        candidate = build_promotion_candidate(run)
        self.assertIsNone(candidate.data_readiness)
        self.assertIsNone(candidate.statistical_gate)

        report = evaluate_promotion_gate(candidate, code_version={"commit": "x"})
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("NOT_ENOUGH_EVIDENCE", statuses["data_readiness"])
        self.assertEqual("NOT_ENOUGH_EVIDENCE", statuses["statistical_evidence"])

    def test_audit_keys_are_distinct_from_gate_keys(self) -> None:
        # Guardrail: the wiring must never accidentally write the gate's own keys.
        self.assertNotEqual("data_readiness", DATA_READINESS_AUDIT_KEY)
        self.assertNotEqual("statistical_gate", STATISTICAL_GATE_AUDIT_KEY)
