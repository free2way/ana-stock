"""Shared price-basis contract: cross-entry decision consistency (A1 follow-up).

The three entry points (train / inference / backtest) must agree on the same
adjusted view: a missing, corrupt, or under-covered view may not produce a
product that silently claims an adjusted basis, and only an explicit opt-in may
fall back to raw. These tests pin the shared decision table and each entry's
wiring to it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from app.services.price_basis_contract import (
    DECISION_ALLOW_ADJUSTED,
    DECISION_ALLOW_RAW_WITH_AUTHORIZATION,
    DECISION_REJECT,
    ENTRY_BACKTEST,
    ENTRY_INFERENCE,
    ENTRY_TRAIN,
    REASON_ADJUSTED_VIEW_ABSENT,
    REASON_INCOMPLETE_ADJUSTED_COVERAGE,
    REASON_UNREADABLE_VIEW,
    REASON_VIEW_VERSION_MISMATCH,
    AdjustedViewProbe,
    PriceBasisRequirements,
    build_contract,
    decide_price_basis,
    probe_adjusted_view,
)
from app.services.backtesting.runner import EventDrivenBacktestRunner
from app.services.model_output_importer import _external_price_basis_contract
from app.services.trainer import EXECUTABLE_LABEL_PROFILE, SignalTrainer
from tests.test_runner_corporate_actions import _run_backtest

ALL_ENTRIES = (ENTRY_TRAIN, ENTRY_INFERENCE, ENTRY_BACKTEST)


def _probe(state: str, **kwargs) -> AdjustedViewProbe:
    base = {"market": "CN", "state": state}
    base.update(kwargs)
    return AdjustedViewProbe(**base)


def _requirements(entry: str, **kwargs) -> PriceBasisRequirements:
    base = {"entry_point": entry}
    base.update(kwargs)
    return PriceBasisRequirements(**base)


class SharedDecisionConsistencyTests(TestCase):
    def _decisions(self, probe: AdjustedViewProbe, **kwargs) -> dict:
        return {
            entry: decide_price_basis(entry, probe, _requirements(entry, **kwargs))
            for entry in ALL_ENTRIES
        }

    def test_corrupt_view_is_rejected_by_every_entry(self) -> None:
        probe = _probe("unreadable", path="/lake/_adjusted_v2/cn/adjusted.parquet", error="duckdb.IOException")
        decisions = self._decisions(probe)
        for entry, decision in decisions.items():
            with self.subTest(entry=entry):
                self.assertEqual(DECISION_REJECT, decision.decision)
                self.assertEqual((REASON_UNREADABLE_VIEW,), decision.reasons)
                self.assertIn("unreadable_view", decision.error_message(market="CN"))
        self.assertFalse(any(item.used_adjusted_basis for item in decisions.values()))

    def test_absent_view_is_rejected_by_every_entry_without_authorization(self) -> None:
        probe = _probe("absent", path="/lake/_adjusted_v2/cn/adjusted.parquet")
        for entry, decision in self._decisions(probe).items():
            with self.subTest(entry=entry):
                self.assertEqual(DECISION_REJECT, decision.decision)
                self.assertEqual((REASON_ADJUSTED_VIEW_ABSENT,), decision.reasons)
                self.assertEqual("fail_closed", decision.fallback_policy)

    def test_absent_view_authorization_is_consistent_and_audited(self) -> None:
        probe = _probe("absent", path="/lake/_adjusted_v2/cn/adjusted.parquet")
        for entry, decision in self._decisions(
            probe,
            allow_raw_fallback=True,
            adjusted_count=0,
            raw_fallback_count=1745,
            require_full_coverage=True,
        ).items():
            with self.subTest(entry=entry):
                self.assertEqual(DECISION_ALLOW_RAW_WITH_AUTHORIZATION, decision.decision)
                self.assertEqual("explicit_raw_fallback", decision.fallback_policy)
                self.assertEqual(
                    "PQW_TRAINER_ALLOW_RAW_FALLBACK", decision.authorized_by
                )
                self.assertEqual("mixed:0.00000000", decision.label_price_basis)
                self.assertFalse(decision.used_adjusted_basis)
                audit = decision.audit_fields()
                self.assertTrue(audit["authorized_by"])
                self.assertEqual("absent", audit["view_state"])

    def test_incomplete_coverage_is_rejected_by_every_entry(self) -> None:
        probe = _probe("present", view_sha256="a" * 64)
        for entry, decision in self._decisions(
            probe,
            adjusted_count=8,
            raw_fallback_count=1,
            dropped_missing_adjusted_count=1,
            require_full_coverage=True,
        ).items():
            with self.subTest(entry=entry):
                self.assertEqual(DECISION_REJECT, decision.decision)
                self.assertEqual((REASON_INCOMPLETE_ADJUSTED_COVERAGE,), decision.reasons)
                self.assertAlmostEqual(0.8, decision.coverage_share)
                self.assertEqual("mixed:0.80000000", decision.label_price_basis)

    def test_version_mismatch_is_rejected_and_never_adjusted(self) -> None:
        probe = _probe("present", view_sha256="b" * 64)
        for entry, decision in self._decisions(
            probe,
            expected_view_sha256="a" * 64,
        ).items():
            with self.subTest(entry=entry):
                self.assertEqual(DECISION_REJECT, decision.decision)
                self.assertEqual((REASON_VIEW_VERSION_MISMATCH,), decision.reasons)
                self.assertIs(False, decision.version_match)
                self.assertFalse(decision.used_adjusted_basis)

    def test_full_coverage_present_view_is_allowed_everywhere(self) -> None:
        probe = _probe("present", view_sha256="a" * 64)
        for entry, decision in self._decisions(
            probe,
            adjusted_count=10,
            raw_fallback_count=0,
            dropped_missing_adjusted_count=0,
        ).items():
            with self.subTest(entry=entry):
                self.assertEqual(DECISION_ALLOW_ADJUSTED, decision.decision)
                self.assertEqual("adjusted_view", decision.label_price_basis)
                self.assertTrue(decision.used_adjusted_basis)

    def test_not_applicable_market_is_raw_and_never_adjusted(self) -> None:
        probe = _probe("absent", market="HK")
        for entry, decision in self._decisions(
            probe,
            requires_adjusted_prices=True,
            adjusted_count=3,
        ).items():
            with self.subTest(entry=entry):
                self.assertFalse(decision.applicable)
                self.assertEqual(DECISION_ALLOW_RAW_WITH_AUTHORIZATION, decision.decision)
                self.assertEqual("raw", decision.label_price_basis)
                self.assertFalse(decision.used_adjusted_basis)

    def test_unknown_entry_point_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decide_price_basis("research", _probe("present"))


class ProbeCoverageTests(TestCase):
    def test_probe_reports_absent_when_store_missing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            probe = probe_adjusted_view("CN", root=Path(tmp), symbols={"AAA"})
        self.assertEqual("absent", probe.state)
        self.assertIsNone(probe.view_sha256)

    def test_probe_reports_unreadable_with_path_and_reason(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "adjusted.parquet"
            target.write_bytes(b"not a parquet")
            with patch(
                "app.services.adjusted_view_store.adjusted_view_path",
                return_value=target,
            ):
                probe = probe_adjusted_view("CN", root=Path(tmp))
        self.assertEqual("unreadable", probe.state)
        self.assertIn("adjusted.parquet", probe.error or "")
        self.assertIsNotNone(probe.path)

    def test_probe_row_coverage_counts_missing_symbols(self) -> None:
        rows = [
            {"symbol": "AAA", "date": "2026-01-02"},
            {"symbol": "BBB", "date": "2026-01-02"},
        ]
        view = {"AAA": {"2026-01-02": {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}}}
        with (
            patch(
                "app.services.adjusted_view_store.load_adjusted_bars",
                return_value=view,
            ),
            patch(
                "app.services.adjusted_view_store.adjusted_view_path",
                return_value=Path("/tmp/adjusted.parquet"),
            ),
        ):
            probe = probe_adjusted_view("CN", rows=rows, symbols={"AAA", "BBB"})
        self.assertEqual("present", probe.state)
        self.assertEqual(2, probe.requested_rows)
        self.assertEqual(1, probe.served_rows)
        self.assertEqual(1, probe.missing_rows)
        self.assertEqual(("BBB",), probe.missing_symbols)
        self.assertAlmostEqual(0.5, probe.coverage_share)

    def test_build_contract_returns_decision_and_probe(self) -> None:
        with patch(
            "app.services.price_basis_contract.probe_adjusted_view",
            return_value=_probe("absent"),
        ):
            decision, probe = build_contract(ENTRY_INFERENCE, "CN", allow_raw_fallback=True)
        self.assertEqual("absent", probe.state)
        self.assertEqual(DECISION_ALLOW_RAW_WITH_AUTHORIZATION, decision.decision)


class TrainerInferenceContractTests(TestCase):
    def _trainer_with(self, *, state: str, allow_raw: bool) -> SignalTrainer:
        trainer = SignalTrainer()
        trainer.settings = trainer.settings.model_copy(
            update={
                "trainer_allow_raw_fallback": allow_raw,
                "optin_reason": "fixture: inference contract test",
                "optin_operator": "fixture_operator",
            }
        )
        trainer._adjusted_basis_expected = True
        trainer._adjusted_view_state = state
        trainer._adjusted_view_market = "CN"
        trainer._label_price_stats = {
            "adjusted_count": 0,
            "raw_fallback_count": 100,
            "dropped_missing_adjusted_count": 0,
        }
        return trainer

    def test_inference_product_contract_is_recorded_for_authorized_absent_view(self) -> None:
        trainer = self._trainer_with(state="absent", allow_raw=True)
        trainer._label_price_basis_contract(label_profile=EXECUTABLE_LABEL_PROFILE)
        contract = trainer._prediction_price_basis_contract()
        assert contract is not None
        self.assertEqual("inference", contract["entry_point"])
        self.assertEqual("absent", contract["view_state"])
        self.assertEqual("explicit_raw_fallback", contract["fallback_policy"])
        self.assertEqual(
            "PQW_TRAINER_ALLOW_RAW_FALLBACK", contract["authorized_by"]
        )
        # The prediction product never claims an adjusted basis for a raw run.
        self.assertNotEqual("adjusted_view", contract["label_price_basis"])

    def test_inference_contract_is_none_before_the_gate_runs(self) -> None:
        self.assertIsNone(SignalTrainer()._prediction_price_basis_contract())


class BacktestEntryPointTests(TestCase):
    def test_success_manifest_records_price_basis_contract(self) -> None:
        captured = _run_backtest(records=[])
        self.assertEqual("success", captured["status"])
        contract = captured["config"]["price_basis_contract"]
        self.assertEqual("backtest", contract["entry_point"])
        # The real US view exists in this repo; the manifest must cite it, and
        # the decision must be an allow (not a reject) for a clean run.
        self.assertIn(contract["view_state"], {"present", "absent"})
        self.assertNotEqual("reject", contract["decision"])
        self.assertEqual(contract, captured["summary"]["price_basis_contract"])

    def test_corrupt_view_refuses_and_records_failed_contract(self) -> None:
        corrupt = AdjustedViewProbe(
            market="US",
            state="unreadable",
            path="/lake/_adjusted_v2/us/adjusted.parquet",
            error="duckdb.IOException: not a parquet file",
        )
        with patch(
            "app.services.backtesting.runner.probe_adjusted_view", return_value=corrupt
        ):
            captured = _run_backtest(records=[], expect_error=True)
        self.assertEqual("failed", captured["status"])
        self.assertIn("unreadable_view", str(captured["error"]))
        self.assertEqual("unreadable", captured["summary"]["price_basis_contract"]["view_state"])
        self.assertEqual("reject", captured["summary"]["price_basis_contract"]["decision"])

    def test_version_mismatch_refuses_the_run(self) -> None:
        present = AdjustedViewProbe(
            market="US", state="present", view_sha256="b" * 64
        )
        with (
            patch(
                "app.services.backtesting.runner.probe_adjusted_view",
                return_value=present,
            ),
            patch.object(
                EventDrivenBacktestRunner,
                "_model_run_price_basis_binding",
                return_value={"adjusted_view_sha256": "a" * 64},
            ),
        ):
            captured = _run_backtest(records=[], expect_error=True)
        self.assertEqual("failed", captured["status"])
        self.assertIn("view_version_mismatch", str(captured["error"]))
        contract = captured["summary"]["price_basis_contract"]
        self.assertIs(False, contract["version_match"])
        self.assertFalse(contract["label_price_basis"] == "adjusted_view")

    def test_model_declared_full_coverage_gates_current_coverage(self) -> None:
        partial = AdjustedViewProbe(
            market="US",
            state="present",
            view_sha256="a" * 64,
            requested_rows=2,
            served_rows=1,
            missing_rows=1,
            missing_symbols=("AAA",),
            coverage_share=0.5,
        )
        with (
            patch(
                "app.services.backtesting.runner.probe_adjusted_view",
                return_value=partial,
            ),
            patch.object(
                EventDrivenBacktestRunner,
                "_model_run_price_basis_binding",
                return_value={
                    "adjusted_view_sha256": "a" * 64,
                    "require_full_adjusted_coverage": True,
                },
            ),
        ):
            captured = _run_backtest(records=[], expect_error=True)
        self.assertEqual("failed", captured["status"])
        self.assertIn("incomplete_adjusted_coverage", str(captured["error"]))
        contract = captured["summary"]["price_basis_contract"]
        self.assertAlmostEqual(0.5, contract["coverage_share"])
        self.assertEqual(["AAA"], contract["missing_symbols"])


class ExternalInferenceContractTests(TestCase):
    def test_unreadable_view_refuses_external_import(self) -> None:
        corrupt = AdjustedViewProbe(
            market="CN", state="unreadable", error="boom", path="/x/adjusted.parquet"
        )
        with patch(
            "app.services.model_output_importer.probe_adjusted_view",
            return_value=corrupt,
        ):
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                _external_price_basis_contract("CN", artifact_path=None)

    def test_declared_version_mismatch_refuses_external_import(self) -> None:
        present = AdjustedViewProbe(
            market="CN", state="present", view_sha256="b" * 64
        )
        manifest = SimpleNamespace()
        with (
            patch(
                "app.services.model_output_importer.probe_adjusted_view",
                return_value=present,
            ),
            patch(
                "app.services.model_output_importer._read_artifact_price_basis_binding",
                return_value="a" * 64,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "different price basis"):
                _external_price_basis_contract("CN", artifact_path=str(manifest))

    def test_external_artifact_is_never_labelled_adjusted(self) -> None:
        present = AdjustedViewProbe(
            market="CN", state="present", view_sha256="a" * 64
        )
        with (
            patch(
                "app.services.model_output_importer.probe_adjusted_view",
                return_value=present,
            ),
            patch(
                "app.services.model_output_importer._read_artifact_price_basis_binding",
                return_value=None,
            ),
        ):
            contract = _external_price_basis_contract("CN", artifact_path=None)
        self.assertEqual("raw", contract["label_price_basis"])
        self.assertFalse(contract["applicable"])
        self.assertEqual("external_artifact", contract["fallback_policy"])
