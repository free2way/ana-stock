from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from app.services.price_basis_contract import (
    DECISION_REJECT,
    ENTRY_INFERENCE,
    AdjustedViewProbe,
    PriceBasisRequirements,
    decide_price_basis,
)
from app.services.stock_selection.data_readiness import (
    DataReadinessConfig,
    assess_data_readiness,
)
from app.services.stock_selection.promotion_gate import (
    PromotionGateCheck,
    PromotionGateReport,
)
from app.services.stock_selection.promotion_gate_v2 import (
    DECISION_ELIGIBLE,
    DECISION_REJECT_V2,
    PromotionCandidate,
    PromotionGateV2Config,
    PromotionNotAuthorized,
    assert_promotable,
    classify_run_scope,
    evaluate_promotion_gate,
    persist_promotion_approval_record,
)

_FIXED_AT = "2026-10-03T00:00:00+00:00"
_CODE_VERSION = {"commit": "deadbeef", "worktree_dirty": True, "resolved_from": "test"}


def _passing_statistical_gate() -> PromotionGateReport:
    return PromotionGateReport(
        schema_version="stock_selection_promotion_gate_v1",
        market="CN",
        model_key="candidate-model",
        factor_set_key="candidate",
        horizon_days=5,
        top_n=5,
        source_evidence_versions=("base", "stress"),
        decision=DECISION_ELIGIBLE,
        champion_action="KEEP_CURRENT",
        checks=(
            PromotionGateCheck(
                key="top_n_risk_adjusted_label_across_costs",
                status="PASS",
                observed=0.01,
                threshold=">0",
                detail="ok",
            ),
        ),
    )


def _candidate(**overrides: object) -> PromotionCandidate:
    payload: dict[str, object] = {
        "market": "CN",
        "model_key": "candidate-model",
        "run_id": "42",
        "status": "success",
        "scope": "formal_full_market",
        "training_sample_count": 50_000,
        "oos_evaluation": {
            "evaluated_date_count": 200,
            "mean_risk_adjusted_return": 0.012,
            "positive_date_rate": 0.6,
        },
        "purge": {"purge_sessions": 5, "embargo_sessions": 0},
        "price_basis": {
            "decision": "allow_adjusted",
            "fallback_policy": "none",
            "authorized_by": None,
            "version_match": None,
            "view_state": "present",
            "reasons": [],
            "label_price_basis": "adjusted_view",
        },
        "corporate_actions": {
            "unmodeled_corporate_actions": [],
            "unmodeled_opt_in": False,
        },
        "data_readiness": {"blockers": [], "scope": "formal_full_market"},
        "statistical_gate": _passing_statistical_gate(),
    }
    payload.update(overrides)
    return PromotionCandidate(**payload)  # type: ignore[arg-type]


def _evaluate(candidate: PromotionCandidate):
    return evaluate_promotion_gate(
        candidate,
        config=PromotionGateV2Config(),
        code_version=_CODE_VERSION,
        decided_at=_FIXED_AT,
    )


class PromotionGateV2Tests(unittest.TestCase):
    def test_shipped_threshold_defaults_are_trainer_aligned(self) -> None:
        # The class-level defaults (env-independent) must match what the
        # production trainer can emit; the settings-resolved dataclass default
        # follows the environment and an explicit argument still wins.
        from app.core.config import Settings, get_settings

        self.assertEqual(
            40, Settings.model_fields["promotion_gate_minimum_oos_dates"].default
        )
        self.assertEqual(
            1000,
            Settings.model_fields["promotion_gate_minimum_training_samples"].default,
        )
        self.assertEqual(
            get_settings().promotion_gate_minimum_oos_dates,
            PromotionGateV2Config().minimum_oos_dates,
        )
        self.assertEqual(
            get_settings().promotion_gate_minimum_training_samples,
            PromotionGateV2Config().minimum_training_samples,
        )
        self.assertEqual(
            120, PromotionGateV2Config(minimum_oos_dates=120).minimum_oos_dates
        )

    def test_oos_threshold_records_configured_minimum_without_window_cap(self) -> None:
        # Legacy run with no `window_capable_dates`: the configured minimum
        # applies unchanged. An undeclared window is never silently relaxed.
        report = evaluate_promotion_gate(
            _candidate(
                oos_evaluation={
                    "evaluated_date_count": 100,
                    "mean_risk_adjusted_return": 0.01,
                }
            ),
            config=PromotionGateV2Config(minimum_oos_dates=120),
            code_version=_CODE_VERSION,
        )
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("FAIL", statuses["oos_evaluation"])
        self.assertEqual(120, report.evaluation["oos_threshold_effective"])
        self.assertEqual(
            "configured_minimum", report.evaluation["oos_threshold_source"]
        )
        self.assertIsNone(report.evaluation["oos_window_capable_dates"])

    def test_oos_threshold_capped_by_declared_window_capability(self) -> None:
        # A run that declares its window ceiling is judged against the smaller
        # of the configured minimum and that ceiling, and the audit fields say
        # exactly which bar applied.
        report = evaluate_promotion_gate(
            _candidate(
                oos_evaluation={
                    "evaluated_date_count": 54,
                    "mean_risk_adjusted_return": 0.01,
                    "window_capable_dates": 54,
                }
            ),
            config=PromotionGateV2Config(minimum_oos_dates=120),
            code_version=_CODE_VERSION,
        )
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("PASS", statuses["oos_evaluation"])
        self.assertEqual(120, report.evaluation["oos_threshold_configured"])
        self.assertEqual(54, report.evaluation["oos_threshold_effective"])
        self.assertEqual(
            "window_capable_dates", report.evaluation["oos_threshold_source"]
        )
        self.assertEqual(54, report.evaluation["oos_window_capable_dates"])

    def test_rejected_price_basis_blocks_promotion(self) -> None:
        rejected = decide_price_basis(
            ENTRY_INFERENCE,
            AdjustedViewProbe(market="CN", state="absent"),
            PriceBasisRequirements(entry_point=ENTRY_INFERENCE, allow_raw_fallback=False),
        )
        self.assertEqual(DECISION_REJECT, rejected.decision)
        report = _evaluate(_candidate(price_basis=rejected))
        self.assertFalse(report.promotable)
        self.assertEqual(DECISION_REJECT_V2, report.decision)
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("FAIL", statuses["price_basis_contract"])

    def test_unauthorized_raw_fallback_blocks_promotion(self) -> None:
        unauthorized = {
            "decision": "allow_raw_with_authorization",
            "fallback_policy": "explicit_raw_fallback",
            "authorized_by": None,
            "version_match": None,
            "view_state": "absent",
            "reasons": [],
        }
        report = _evaluate(_candidate(price_basis=unauthorized))
        self.assertFalse(report.promotable)
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("FAIL", statuses["price_basis_contract"])

    def test_version_mismatch_blocks_promotion(self) -> None:
        mismatched = {
            "decision": "allow_adjusted",
            "fallback_policy": "none",
            "authorized_by": None,
            "version_match": False,
            "view_state": "present",
            "reasons": [],
        }
        report = _evaluate(_candidate(price_basis=mismatched))
        self.assertFalse(report.promotable)
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("FAIL", statuses["price_basis_contract"])

    def test_unmodeled_corporate_actions_without_opt_in_blocks_promotion(self) -> None:
        events = [
            {"symbol": "600000.SH", "action_type": "merger", "effective_date": "2026-02-03"}
        ]
        report = _evaluate(
            _candidate(
                corporate_actions={
                    "unmodeled_corporate_actions": events,
                    "unmodeled_opt_in": False,
                }
            )
        )
        self.assertFalse(report.promotable)
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("FAIL", statuses["corporate_action_coverage"])

    def test_unmodeled_corporate_actions_with_opt_in_passes(self) -> None:
        events = [
            {"symbol": "600000.SH", "action_type": "merger", "effective_date": "2026-02-03"}
        ]
        report = _evaluate(
            _candidate(
                corporate_actions={
                    "unmodeled_corporate_actions": events,
                    "unmodeled_opt_in": True,
                }
            )
        )
        self.assertTrue(report.promotable)
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("PASS", statuses["corporate_action_coverage"])

    def test_point_in_time_readiness_blockers_block_promotion(self) -> None:
        readiness = assess_data_readiness(
            {"600000.SH": {"history_days": 300, "duplicate_conflict_days": 0}},
            security_types={"600000.SH": "equity"},
            metadata_present={"600000.SH": True},
            industries={"600000.SH": "Industrials"},
            config=DataReadinessConfig(
                market="CN",
                minimum_eligible_symbols=1,
                minimum_history_coverage=0.0,
                minimum_metadata_coverage=0.0,
                minimum_industry_coverage=0.0,
                maximum_duplicate_conflict_rate=1.0,
                require_historical_universe_contract=True,
            ),
            historical_universe_contract={
                "historical_industry_verified": True,
                "delisting_history_verified": True,
                "historical_security_master_verified": False,
                "historical_membership_verified": False,
                "universe_revision_history_verified": False,
            },
        )
        self.assertEqual(
            (
                "historical_security_master_not_verified",
                "historical_membership_not_verified",
                "universe_revision_history_not_verified",
            ),
            readiness.blockers,
        )
        self.assertTrue(readiness.historical_industry_verified)
        self.assertTrue(readiness.delisting_history_verified)
        report = _evaluate(_candidate(data_readiness=readiness))
        self.assertFalse(report.promotable)
        statuses = {item.key: item.status for item in report.checks}
        self.assertEqual("FAIL", statuses["data_readiness"])

    def test_all_checks_passing_is_promotable_with_complete_traceability(self) -> None:
        report = _evaluate(_candidate())
        self.assertTrue(report.promotable)
        self.assertEqual(DECISION_ELIGIBLE, report.decision)
        self.assertEqual((), report.non_promotable_reasons)
        self.assertEqual(
            [], [item.key for item in report.checks if item.status != "PASS"]
        )
        fields = report.as_dict()
        for key in (
            "schema_version",
            "market",
            "run_id",
            "data_version",
            "code_version",
            "params",
            "evaluation",
            "decision",
            "decided_by",
            "decided_at",
            "reasons",
        ):
            self.assertIn(key, fields)
        self.assertEqual(_CODE_VERSION, fields["code_version"])
        self.assertEqual(_FIXED_AT, fields["decided_at"])
        self.assertEqual(200, fields["evaluation"]["oos_evaluation"]["evaluated_date_count"])
        self.assertEqual(
            "ELIGIBLE_FOR_MANUAL_REVIEW",
            fields["evaluation"]["statistical_gate_decision"],
        )
        self.assertIn(":" + report.market + ":42:", report.evidence_version)

    def test_research_scope_run_is_marked_non_promotable(self) -> None:
        report = _evaluate(_candidate(scope="engineering_pilot_not_for_model_selection"))
        self.assertFalse(report.promotable)
        self.assertEqual(DECISION_REJECT_V2, report.decision)
        self.assertTrue(
            any("research_scope" in reason for reason in report.non_promotable_reasons)
        )
        status_fields = report.promotion_status_fields()
        self.assertFalse(status_fields["promotable"])
        self.assertTrue(status_fields["non_promotable_reasons"])
        with self.assertRaises(PromotionNotAuthorized):
            assert_promotable(report)

    def test_explicit_tickers_selection_mode_is_non_promotable(self) -> None:
        report = _evaluate(
            _candidate(scope=None, scope_hints={"selection_mode": "explicit_tickers"})
        )
        self.assertFalse(report.promotable)
        self.assertTrue(
            any("explicit_tickers" in reason for reason in report.non_promotable_reasons)
        )

    def test_classify_run_scope_only_blocks_explicit_research_markers(self) -> None:
        self.assertEqual((True, ""), classify_run_scope("formal_full_market"))
        self.assertEqual((True, ""), classify_run_scope(None))
        self.assertFalse(classify_run_scope("engineering_full_market_survivor_biased")[0])
        self.assertFalse(classify_run_scope("latest_liquidity_pilot_not_point_in_time")[0])

    def test_from_model_run_reads_persisted_config_fields(self) -> None:
        model_run = SimpleNamespace(
            id=7,
            name="cn-lgbm",
            market="CN",
            status="success",
            model_type="lightgbm_multifactor",
            universe="full_dataset",
            train_start="2024-01-01",
            train_end="2025-06-01",
            test_start="2025-06-02",
            test_end="2025-12-01",
            config_json=json.dumps(
                {
                    "model_type": "lightgbm",
                    "purge_gap_days": 5,
                    "embargo_sessions": 0,
                    "evaluation_protocol": "walk_forward_purged_v2",
                    "sample_count": 12345,
                    "selection_mode": "explicit_tickers",
                    "unmodeled_corporate_actions": [],
                    "unmodeled_opt_in": False,
                    "prediction_price_basis_contract": {"decision": "allow_adjusted"},
                }
            ),
        )
        candidate = PromotionCandidate.from_model_run(
            model_run,
            data_readiness={"blockers": []},
            oos_evaluation={"evaluated_date_count": 150, "mean_risk_adjusted_return": 0.01},
            statistical_gate={"decision": DECISION_ELIGIBLE},
        )
        self.assertEqual("12345", str(candidate.training_sample_count))
        self.assertEqual(5, candidate.purge["purge_sessions"])
        self.assertEqual("explicit_tickers", candidate.scope_hints["selection_mode"])
        report = _evaluate(candidate)
        self.assertFalse(report.promotable)

    def test_approval_record_persistence_is_idempotent_and_non_overwriting(self) -> None:
        report = _evaluate(_candidate())
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            first = persist_promotion_approval_record(report, root=root)
            second = persist_promotion_approval_record(report, root=root)
            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)
            self.assertEqual(first.evidence_version, second.evidence_version)
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(DECISION_ELIGIBLE, manifest["decision"])
            self.assertTrue(manifest["promotable"])
            self.assertEqual(_CODE_VERSION, manifest["code_version"])
            record = json.loads(first.record_path.read_text(encoding="utf-8"))
            expected = json.loads(json.dumps(asdict(report), default=str))
            self.assertEqual(expected, record)
            # Tamper the manifest and confirm a re-persist refuses to overwrite.
            manifest["record_sha256"] = "0" * 64
            first.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(RuntimeError):
                persist_promotion_approval_record(report, root=root)

    def test_content_digest_changes_with_decision_evidence(self) -> None:
        passing = _evaluate(_candidate())
        failing = _evaluate(_candidate(training_sample_count=10))
        self.assertNotEqual(passing.content_digest, failing.content_digest)
        self.assertNotEqual(passing.evidence_version, failing.evidence_version)


if __name__ == "__main__":
    unittest.main()
