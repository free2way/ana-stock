"""Trainer-side promotion evidence: real small fit -> gate pure function.

Runs one genuine (small) walk-forward fit with the real sample/label builder and
asserts the trainer persists the structured evidence ``promotion_gate_v2`` reads
(``training_sample_count``, ``oos_evaluation``, purge/embargo, price basis) and
that, once the remaining (non-trainer) evidence is attached, the gate flips from
``OBSERVE`` to ``ELIGIBLE``.
"""

from __future__ import annotations

import json
import math
from contextlib import ExitStack
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.services.stock_selection.promotion_gate_v2 import (
    DECISION_ELIGIBLE,
    DECISION_OBSERVE,
    PromotionCandidate,
    PromotionGateV2Config,
    evaluate_promotion_gate,
)
from app.services.stock_selection.promotion_enforcement import (
    assess_run_for_serving,
)
from app.services.trainer import SignalTrainer

_GATE_CONFIG = PromotionGateV2Config(
    minimum_training_samples=1000,
    minimum_oos_dates=30,
    minimum_purge_sessions=1,
)
_CODE_VERSION = {"commit": "deadbeef", "worktree_dirty": True, "resolved_from": "test"}

# Non-trainer evidence, supplied the way promotion tooling / serving does.
_NON_TRAINER_EVIDENCE = {
    "data_readiness": {"blockers": []},
    "corporate_actions": {"unmodeled_corporate_actions": [], "unmodeled_opt_in": False},
    "statistical_gate": {"decision": "ELIGIBLE_FOR_MANUAL_REVIEW"},
}


def _fixture_rows(symbols: list[str], sessions: int) -> tuple[list[dict], list[str]]:
    """Deterministic, gently rising prices so every matured label is positive."""

    start = date(2025, 1, 1)
    dates = [(start + timedelta(days=index)).isoformat() for index in range(sessions)]
    rows: list[dict] = []
    for symbol in symbols:
        for index, trade_date in enumerate(dates):
            close = 100.0 * (1.0 + 0.002 * index + 0.002 * math.sin(index / 5.0))
            rows.append(
                {
                    "symbol": symbol,
                    "date": trade_date,
                    "open": close,
                    "high": close * 1.004,
                    "low": close * 0.996,
                    "close": close,
                    "volume": 1_000_000 + index * 1_000,
                }
            )
    return rows, dates


class TrainerPromotionEvidenceTests(TestCase):
    def _run_real_training(self) -> tuple[dict, dict, dict]:
        symbols = [f"6000{index:02d}.SH" for index in range(15)]
        sessions = 340
        rows, dates = _fixture_rows(symbols, sessions)
        trainer = SignalTrainer()
        configs: list[dict] = []

        def _record_merge_config(_run_id: int, payload: dict) -> None:
            configs.append(dict(payload))

        model = MagicMock()
        model.feature_importances_ = [1.0] * len(
            trainer._feature_names(lookback_days=3)
        )
        # The corporate-action audit is a filesystem read in production; stub it
        # here so this test only asserts the *wiring* (config + artifact
        # manifest) and cannot be broken by a leaked PQW_DATA_DIR from another
        # module. The rule itself is covered by
        # tests/test_corporate_action_coverage.py.
        coverage_audit = {
            "audited": True,
            "unmodeled_corporate_actions": [],
            "unmodeled_opt_in": False,
            "loaded": 0,
            "modeled": 0,
            "unmodeled": 0,
            "window_start": dates[-61],
            "window_end": dates[-1],
            "symbol_count": len(symbols),
        }
        repositories = (
            "SymbolRepository",
            "ModelRunRepository",
            "PredictionWriteRepository",
            "PredictionDetailRepository",
            "PredictionExplanationRepository",
        )
        with ExitStack() as stack:
            stack.enter_context(patch("app.services.trainer.SessionLocal"))
            mocks = {
                name: stack.enter_context(patch(f"app.services.trainer.{name}")).return_value
                for name in repositories
            }
            mocks["SymbolRepository"].list_symbols.return_value = [
                SimpleNamespace(ticker=symbol, id=index + 1)
                for index, symbol in enumerate(symbols)
            ]
            repo = mocks["ModelRunRepository"]
            repo.create_run.return_value = SimpleNamespace(id=9876)
            repo.merge_config.side_effect = _record_merge_config
            stack.enter_context(
                patch(
                    "app.services.trainer.assess_corporate_action_coverage",
                    return_value=dict(coverage_audit),
                )
            )
            stack.enter_context(
                patch(
                    "app.services.trainer.get_latest_lake_trade_date",
                    return_value=dates[-1],
                )
            )
            stack.enter_context(
                patch(
                    "app.services.trainer.lgb",
                    SimpleNamespace(LGBMRegressor=MagicMock(return_value=model)),
                )
            )
            for name, value in {
                "_load_symbol_feature_context": {},
                "_load_oos_score_calibration": ([], {}),
                "_build_score_calibration": [],
                "_build_detail_row": {},
                "_build_lightgbm_explanations": [],
            }.items():
                stack.enter_context(patch.object(trainer, name, return_value=value))
            stack.enter_context(
                patch.object(
                    trainer,
                    "_predict_scores",
                    side_effect=lambda _model, values: [1.0] * len(values),
                )
            )
            persist = stack.enter_context(
                patch.object(trainer, "_persist_model_outputs", return_value=9876)
            )
            run_id = trainer._train_lightgbm(
                run_name="fixture",
                signal_type="momentum",
                lookback_days=3,
                normalized_tickers=set(symbols),
                market="CN",
                universe="fixture",
                rows=rows,
            )

        self.assertEqual(9876, run_id)
        create_config = dict(repo.create_run.call_args.kwargs["config"])
        merged: dict = {}
        for payload in configs:
            merged.update(payload)
        artifact_meta = dict(persist.call_args.kwargs["model_metadata"])
        return create_config, merged, artifact_meta

    def test_trainer_persists_gate_readable_evidence(self) -> None:
        create_config, merged, artifact_meta = self._run_real_training()

        # Pre-existing protocol evidence is reused, not rebuilt.
        self.assertGreaterEqual(int(create_config["purge_gap_days"]), 1)
        # The embargo now defaults to one horizon (purge already covers the
        # label window; the embargo adds an equal forward gap).
        self.assertEqual(
            create_config["purge_gap_days"], create_config["embargo_sessions"]
        )
        self.assertGreater(create_config["embargo_sessions"], 0)
        self.assertEqual("walk_forward_purged_v2", create_config["evaluation_protocol"])
        self.assertIsInstance(create_config["prediction_price_basis_contract"], dict)
        # Training-side robustness contract persisted for audit.
        self.assertTrue(create_config["universe_filter_stats"]["enabled"])
        self.assertTrue(create_config["label_winsorize"]["enabled"])
        self.assertEqual(0.025, create_config["label_winsorize"]["lower_quantile"])
        self.assertEqual("huber", create_config["objective"]["objective"])
        self.assertEqual(0.12, create_config["drawdown_penalty"])
        self.assertTrue(create_config["feature_transform"]["enabled"])
        self.assertEqual(
            "cross_sectional_winsor_mad_zscore",
            create_config["feature_transform"]["method"],
        )
        # The run config and the artifact manifest must carry the identical
        # execution cost tuple so the default executable profile's execution
        # contract can be re-derived downstream.
        self.assertIn("execution_cost_bps", create_config)
        self.assertEqual(
            create_config["execution_cost_bps"], artifact_meta["execution_cost_bps"]
        )
        self.assertEqual(
            {"commission_bps_one_way", "slippage_bps_one_way"},
            set(create_config["execution_cost_bps"]),
        )

        self.assertGreater(int(merged["training_sample_count"]), 1000)
        evaluation = merged["oos_evaluation"]
        self.assertEqual("walk_forward_oos_evaluation_v1", evaluation["schema_version"])
        self.assertEqual("trainer_walk_forward_predictions", evaluation["source"])
        self.assertGreater(evaluation["evaluated_date_count"], 0)
        # 340 sessions - 60-session prediction window - 6-session label horizon.
        self.assertEqual(54, evaluation["evaluated_date_count"])
        # The window can physically mature at most `window - horizon` dates,
        # declared so the gate never demands more than the trainer can produce.
        self.assertEqual(54, evaluation["window_capable_dates"])
        self.assertGreater(evaluation["evaluated_sample_count"], 0)
        # P1-3: the candidate list is frozen before outcomes are read, and the
        # audit counts the names the metric could not use instead of backfilling.
        self.assertEqual(5, evaluation["top_n"])
        self.assertEqual(
            5 * evaluation["candidate_date_count"],
            evaluation["frozen_candidate_count"],
        )
        # 60 prediction dates, 54 physically matured (window - horizon).
        self.assertEqual(60, evaluation["candidate_date_count"])
        self.assertEqual(54, evaluation["evaluated_date_count"])
        # The six un-matured tail dates are counted as immature, not backfilled.
        self.assertEqual(30, evaluation["missing_label_count"])
        self.assertEqual(30, evaluation["immature_label_count"])
        self.assertEqual(0, evaluation["missing_outcome_count"])
        self.assertEqual(
            {"label_window_not_available": 30},
            evaluation["missing_label_reasons"],
        )
        self.assertEqual(
            evaluation["missing_label_count"],
            evaluation["immature_label_count"] + evaluation["missing_outcome_count"],
        )
        self.assertGreater(evaluation["mean_risk_adjusted_return"], 0.0)
        self.assertGreater(evaluation["mean_net_return"], 0.0)
        self.assertEqual(1.0, evaluation["positive_date_rate"])
        self.assertEqual(6, evaluation["horizon_days"])
        self.assertEqual("executable_net_return_v1", evaluation["label_profile"])
        self.assertLess(evaluation["date_min"], evaluation["date_max"])
        candidate_audit = merged["oos_candidate_audit"]
        self.assertEqual(
            evaluation["candidate_date_count"], len(candidate_audit)
        )
        self.assertEqual(
            5,
            evaluation["frozen_candidate_count"] // len(candidate_audit),
        )
        for row in candidate_audit:
            self.assertEqual(5, row["candidate_count"])
            self.assertEqual(
                row["missing_label_count"],
                row["immature_label_count"] + row["missing_outcome_count"],
            )
        # Exactly the un-matured tail dates report a gap.
        self.assertEqual(
            6, sum(1 for row in candidate_audit if row["missing_label_count"])
        )
        self.assertEqual(
            evaluation,
            artifact_meta["oos_evaluation"],
        )
        self.assertEqual(
            merged["training_sample_count"], artifact_meta["training_sample_count"]
        )
        # Corporate-action coverage: the trainer is the producer for its own
        # traded window (the shared rule lives in
        # `app.services.corporate_action_coverage`). The stubbed audit records
        # "0 unmodeled events", which must reach both the run config (what the
        # gate reads) and the artifact manifest.
        coverage = merged["corporate_action_coverage_audit"]
        self.assertTrue(coverage["audited"])
        self.assertEqual(0, coverage["unmodeled"])
        self.assertEqual(merged["unmodeled_corporate_actions"], coverage["unmodeled_corporate_actions"])
        self.assertEqual([], merged["unmodeled_corporate_actions"])
        self.assertIs(False, merged["unmodeled_opt_in"])
        self.assertEqual(coverage, artifact_meta["corporate_action_coverage_audit"])
        # The trainer cannot produce these, so it must record the omission
        # instead of dropping the question silently.
        self.assertIn("data_readiness_evidence_missing_reason", merged)
        self.assertNotIn("data_readiness", merged)
        self.assertNotIn("statistical_gate", merged)

    def test_gate_moves_from_observe_to_eligible_with_trainer_evidence(self) -> None:
        create_config, merged, _artifact_meta = self._run_real_training()

        def _model_run(config: dict) -> SimpleNamespace:
            return SimpleNamespace(
                id=4242,
                name="candidate-model",
                market="CN",
                status="success",
                model_type="lightgbm_multifactor",
                universe="full_dataset",
                train_start="2025-01-01",
                train_end=merged["oos_evaluation"]["date_min"],
                test_start=merged["oos_evaluation"]["date_min"],
                test_end=merged["oos_evaluation"]["date_max"],
                config_json=json.dumps(config),
            )

        # Before the trainer wrote OOS evidence the run is only OBSERVE.
        legacy_config = {
            **create_config,
            "training_window_audits": merged["training_window_audits"],
        }
        legacy_candidate = PromotionCandidate.from_model_run(
            _model_run(legacy_config), **_NON_TRAINER_EVIDENCE
        )
        legacy_report = evaluate_promotion_gate(
            legacy_candidate, config=_GATE_CONFIG, code_version=_CODE_VERSION
        )
        self.assertEqual(DECISION_OBSERVE, legacy_report.decision)
        legacy_statuses = {item.key: item.status for item in legacy_report.checks}
        self.assertEqual("NOT_ENOUGH_EVIDENCE", legacy_statuses["oos_evaluation"])

        # With the trainer-persisted evidence every check the trainer owns
        # passes, and the full candidate (non-trainer evidence attached) is
        # eligible for manual review.
        full_config = {**create_config, **merged}
        full_candidate = PromotionCandidate.from_model_run(
            _model_run(full_config), **_NON_TRAINER_EVIDENCE
        )
        full_report = evaluate_promotion_gate(
            full_candidate, config=_GATE_CONFIG, code_version=_CODE_VERSION
        )
        statuses = {item.key: item.status for item in full_report.checks}
        for key in (
            "run_status_completed",
            "run_scope_promotable",
            "price_basis_contract",
            "training_sample_size",
            "oos_evaluation",
            "purge_embargo_audit",
        ):
            self.assertEqual("PASS", statuses[key], key)
        self.assertEqual(DECISION_ELIGIBLE, full_report.decision)
        self.assertTrue(full_report.promotable)
        self.assertEqual(
            merged["oos_evaluation"]["evaluated_date_count"],
            full_candidate.oos_evaluation["evaluated_date_count"],
        )

        # Regression: previously the freshly written evidence was a FAIL against
        # the gate's stock minimum_oos_dates=120 (the 60-session prediction
        # window matures at most 54 dates) and would have been blocked under the
        # default fail-closed enforcement. With the default threshold aligned to
        # what the trainer can produce, the same evidence passes and the report
        # records which threshold it used and why.
        default_report = evaluate_promotion_gate(
            full_candidate, config=PromotionGateV2Config(), code_version=_CODE_VERSION
        )
        default_statuses = {item.key: item.status for item in default_report.checks}
        self.assertEqual("PASS", default_statuses["oos_evaluation"])
        self.assertEqual(DECISION_ELIGIBLE, default_report.decision)
        self.assertEqual(
            40, default_report.evaluation["oos_threshold_configured"]
        )
        self.assertEqual(40, default_report.evaluation["oos_threshold_effective"])
        self.assertEqual(
            "configured_minimum", default_report.evaluation["oos_threshold_source"]
        )
        self.assertEqual(
            54, default_report.evaluation["oos_window_capable_dates"]
        )

        # If an operator raises the configured minimum above the window's
        # physical ceiling, the threshold is lowered to the declared capable
        # dates -- and the audit fields say so, instead of silently relaxing it.
        capped_report = evaluate_promotion_gate(
            full_candidate,
            config=PromotionGateV2Config(minimum_oos_dates=120),
            code_version=_CODE_VERSION,
        )
        capped_statuses = {item.key: item.status for item in capped_report.checks}
        self.assertEqual("PASS", capped_statuses["oos_evaluation"])
        self.assertEqual(54, capped_report.evaluation["oos_threshold_effective"])
        self.assertEqual(
            "window_capable_dates", capped_report.evaluation["oos_threshold_source"]
        )

        # The trainer alone still cannot satisfy the readiness / statistical
        # checks honestly, but the corporate-action coverage audit it now
        # produces for its own traded window does pass.
        trainer_only = evaluate_promotion_gate(
            PromotionCandidate.from_model_run(_model_run(full_config)),
            config=_GATE_CONFIG,
            code_version=_CODE_VERSION,
        )
        trainer_only_statuses = {item.key: item.status for item in trainer_only.checks}
        self.assertEqual("PASS", trainer_only_statuses["corporate_action_coverage"])
        self.assertEqual(DECISION_OBSERVE, trainer_only.decision)
        for key in ("data_readiness", "statistical_evidence"):
            self.assertEqual("NOT_ENOUGH_EVIDENCE", trainer_only_statuses[key], key)

    def test_default_serving_gate_no_longer_blocks_trainer_artifact(self) -> None:
        """Real serving path: the trainer's own evidence serves under defaults.

        Regression for the mis-interception: before the threshold was aligned,
        the trainer's ~54 matured OOS dates failed ``minimum_oos_dates=120``,
        making the gate ``REJECT`` and ``assess_run_for_serving`` withhold the
        freshly trained artifact under the default fail-closed enforcement.
        """

        create_config, merged, _artifact_meta = self._run_real_training()
        config = {
            **create_config,
            **merged,
            "data_readiness": _NON_TRAINER_EVIDENCE["data_readiness"],
            "statistical_gate": _NON_TRAINER_EVIDENCE["statistical_gate"],
            **_NON_TRAINER_EVIDENCE["corporate_actions"],
        }
        run = SimpleNamespace(
            id=4242,
            name="candidate-model",
            market="CN",
            status="success",
            model_type="lightgbm_multifactor",
            universe="full_dataset",
            config_json=json.dumps(config),
        )
        # No explicit config: the shipped settings defaults must be enough.
        decision = assess_run_for_serving(run, enforce=True, log_warning=False)
        statuses = {item.key: item.status for item in decision.report.checks}
        self.assertEqual("PASS", statuses["oos_evaluation"])
        self.assertTrue(decision.report.promotable)
        self.assertFalse(decision.blocked)
        self.assertFalse(decision.marked_non_promotable)


if __name__ == "__main__":
    import unittest

    unittest.main()
