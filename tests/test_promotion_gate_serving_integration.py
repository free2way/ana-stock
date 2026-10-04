"""Promotion-gate enforcement at the two remaining serving surfaces.

Covers, for both the screener model-ranking champion selection and the
publication guard, the four required scenarios:

1. a ``REJECT`` run is skipped (screener) / refused (publication);
2. a fully passing run is served / published normally;
3. an ``OBSERVE`` run is honoured according to
   ``PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE`` (both states);
4. a report that cannot be tied to a source run does not borrow another run's
   gate report: it is marked ``promotion_evidence_provenance=unresolved`` (a
   non-promotable / research-only label) with a WARNING.
5. a declared run whose report embeds conflicting evidence is treated the same
   way, instead of the conflicting fields being overwritten.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.tables import ModelRun
from app.services import screener as screener_service
from app.services.ai_daily_report import load_ai_daily_report, save_ai_daily_report
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.publication_guard import (
    prepare_report_for_publication,
)

import app.services.stock_selection.promotion_enforcement as promotion_enforcement


def _passing_config(run_id: int = 1) -> dict:
    return {
        "sample_count": 50_000,
        "purge_gap_days": 5,
        "embargo_sessions": 0,
        "evaluation_protocol": "walk_forward_purged_v2",
        "prediction_price_basis_contract": {
            "decision": "allow_adjusted",
            "fallback_policy": "none",
            "authorized_by": None,
            "version_match": None,
            "view_state": "present",
            "reasons": [],
        },
        "unmodeled_corporate_actions": [],
        "unmodeled_opt_in": False,
        "oos_evaluation": {
            "evaluated_date_count": 200,
            "mean_risk_adjusted_return": 0.012,
        },
        "data_readiness": {"blockers": []},
        "statistical_gate": {"decision": "ELIGIBLE_FOR_MANUAL_REVIEW"},
    }


_REJECT_CONFIG = {"selection_mode": "explicit_tickers"}
_OBSERVE_CONFIG: dict = {}


def _run(config: dict, *, run_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        name=f"run-{run_id}",
        market="CN",
        universe="full_market",
        model_type="lightgbm_multifactor",
        status="success",
        config_json=json.dumps(config),
    )


class _NoQuerySession:
    """Context manager without ``query`` so the OOS evaluation read is skipped."""

    def __enter__(self) -> "_NoQuerySession":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _settings(
    *, enforce: bool = True, require_complete: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        promotion_gate_enforce=enforce,
        promotion_gate_require_complete_evidence=require_complete,
    )


class ScreenerChampionPromotionTests(unittest.TestCase):
    def _screen(self, runs, *, require_complete: bool):
        prediction_repo = MagicMock()

        def _list_predictions_for_run(run_id, **_kwargs):
            return [
                {
                    "ticker": "AAA",
                    "score": 0.8,
                    "model_run_id": int(run_id),
                    "name": "AAA",
                    "market": "US",
                }
            ]

        prediction_repo.return_value.list_predictions_for_run.side_effect = (
            _list_predictions_for_run
        )

        def _decision(candidate: dict) -> dict:
            return {
                "ticker": candidate["ticker"],
                "score": candidate["score"],
                "signal_strength": 80,
                "signal_label": "Buy",
                "percentile": 0.9,
                "name": candidate["ticker"],
                "market": "US",
                "entry_style": "Breakout",
                "model_run_id": candidate["model_run_id"],
            }

        prediction_repo.return_value._build_signal_decision.side_effect = _decision
        run_repo = MagicMock()
        run_repo.return_value.list_successful_runs.return_value = list(runs)

        with (
            patch.object(screener_service, "SessionLocal", lambda: _NoQuerySession()),
            patch.object(screener_service, "ModelRunRepository", run_repo),
            patch.object(screener_service, "PredictionRepository", prediction_repo),
            patch.object(screener_service, "load_latest_closes", lambda _t: {}),
            patch.object(
                promotion_enforcement,
                "get_settings",
                lambda: _settings(require_complete=require_complete),
            ),
        ):
            service = screener_service.ScreenerService()
            with patch.object(service, "_load_universe", return_value=["AAA"]):
                rows = service._screen_model_ranking(
                    universe="full_market",
                    market="US",
                    min_trend_score=0,
                    action_filter="ALL",
                )
        selected = (
            prediction_repo.return_value.list_predictions_for_run.call_args
        )
        selected_run_id = (
            int(selected.args[0]) if selected is not None else None
        )
        return rows, selected_run_id

    def test_reject_run_is_skipped_for_older_promotable_run(self) -> None:
        newest = _run(_REJECT_CONFIG, run_id=2)
        older = _run(_passing_config(), run_id=1)
        rows, selected_run_id = self._screen([newest, older], require_complete=False)
        self.assertEqual(1, selected_run_id)
        self.assertTrue(rows)
        self.assertTrue(rows[0]["promotable"])
        self.assertIsNone(rows[0]["promotion_label"])

    def test_all_passing_run_is_served(self) -> None:
        newest = _run(_passing_config(), run_id=2)
        older = _run(_passing_config(), run_id=1)
        rows, selected_run_id = self._screen([newest, older], require_complete=False)
        self.assertEqual(2, selected_run_id)
        self.assertTrue(rows[0]["promotable"])
        self.assertFalse(rows[0]["research_only"])

    def test_observe_run_served_and_labelled_when_flag_off(self) -> None:
        newest = _run(_OBSERVE_CONFIG, run_id=2)
        older = _run(_passing_config(), run_id=1)
        with self.assertLogs("app.services.screener", level="WARNING") as captured:
            rows, selected_run_id = self._screen(
                [newest, older], require_complete=False
            )
        self.assertEqual(2, selected_run_id)
        self.assertFalse(rows[0]["promotable"])
        self.assertTrue(rows[0]["research_only"])
        self.assertEqual("非晋级/研究口径", rows[0]["promotion_label"])
        self.assertFalse(rows[0]["promotion_blocked_from_serving"])
        self.assertTrue(
            any("non-promotable model run 2" in line for line in captured.output)
        )

    def test_observe_run_is_skipped_when_flag_on(self) -> None:
        newest = _run(_OBSERVE_CONFIG, run_id=2)
        older = _run(_passing_config(), run_id=1)
        rows, selected_run_id = self._screen([newest, older], require_complete=True)
        self.assertEqual(1, selected_run_id)
        self.assertTrue(rows[0]["promotable"])

    def test_only_withheld_runs_yield_no_rows(self) -> None:
        rows, selected_run_id = self._screen(
            [_run(_REJECT_CONFIG, run_id=2)], require_complete=False
        )
        self.assertEqual([], rows)
        self.assertIsNone(selected_run_id)


def _report() -> dict:
    return {
        "report_date": "2026-09-11",
        "saved_at": "2026-09-11T20:00:00+08:00",
        "input_market_dates": {"CN": "2026-09-11"},
        "market_recommendations": [
            {"ticker": "600000.SS", "symbol_status": "ready", "model_score": 1}
        ],
        "market_recommendations_meta": {"status": "ready", "market_health": "graded"},
        "model_qualification": {
            "CN": {
                "status": "QUALIFIED",
                "protocol_approved": True,
                "protocol_id": "test-approved-protocol",
                "model_artifact_sha256": "a" * 64,
            }
        },
    }


def _regime() -> dict:
    return {
        "CN": {
            "market": "CN",
            "snapshot_date": "2026-09-11",
            "generated_at": "2026-09-11T18:00:00+08:00",
            "risk_regime": "risk_on",
            "buy_gate": "ALLOW",
            "max_position_scale": 1.0,
        }
    }


def _approved() -> dict:
    return {
        "CN": {
            **_report()["model_qualification"]["CN"],
            "approved_by": "test-owner",
            "approved_at": "2026-09-11T12:00:00+08:00",
        }
    }


class PublicationGuardPromotionTests(unittest.TestCase):
    def _publish(self, **kwargs):
        return prepare_report_for_publication(
            _report(),
            approved_qualifications=_approved(),
            trusted_regime_snapshots=_regime(),
            **kwargs,
        )

    def _publish_with_gate(
        self, *, enforce: bool, require_complete: bool, **kwargs
    ):
        with patch.object(
            promotion_enforcement,
            "get_settings",
            lambda: _settings(enforce=enforce, require_complete=require_complete),
        ):
            return self._publish(**kwargs)

    def test_reject_decision_refuses_publication(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._publish(promotion_decision="REJECT", model_run_id=42)
        self.assertIn("REJECT", str(ctx.exception))
        self.assertIn("42", str(ctx.exception))

    def test_reject_decision_publishes_labelled_when_enforcement_disabled(self) -> None:
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ) as captured:
            guarded = self._publish_with_gate(
                enforce=False,
                require_complete=False,
                promotion_decision="REJECT",
                model_run_id=42,
            )
        self.assertEqual(1, len(guarded["market_recommendations"]))
        status = guarded["promotion_status"]
        self.assertEqual("REJECT", status["promotion_decision"])
        self.assertFalse(status["promotable"])
        self.assertEqual("非晋级/研究口径", status["promotion_label"])
        self.assertEqual("Not promoted / research-only", status["promotion_label_en"])
        self.assertTrue(status["research_only"])
        self.assertFalse(status["promotion_blocked_from_serving"])
        self.assertFalse(status["promotion_enforced"])
        self.assertFalse(status["promotion_require_complete_evidence"])
        self.assertEqual(["promotion_gate_decision:REJECT"], status["non_promotable_reasons"])
        self.assertTrue(
            any(
                "labelled 非晋级/研究口径 but still published "
                "(PQW_PROMOTION_GATE_ENFORCE=false)" in message
                for message in captured.output
            )
        )

    def test_passing_decision_publishes_normally(self) -> None:
        guarded = self._publish(
            promotion_decision="ELIGIBLE_FOR_MANUAL_REVIEW", model_run_id=7
        )
        self.assertEqual(1, len(guarded["market_recommendations"]))
        self.assertNotIn("promotion_status", guarded)

    def test_observe_refused_when_complete_evidence_required(self) -> None:
        with patch.object(
            promotion_enforcement,
            "promotion_require_complete_evidence_enabled",
            return_value=True,
        ):
            with self.assertRaises(ValueError) as ctx:
                self._publish(promotion_decision="OBSERVE", model_run_id=8)
        self.assertIn("OBSERVE", str(ctx.exception))

    def test_observe_publishes_when_complete_evidence_not_required(self) -> None:
        with patch.object(
            promotion_enforcement,
            "promotion_require_complete_evidence_enabled",
            return_value=False,
        ):
            guarded = self._publish(promotion_decision="OBSERVE", model_run_id=8)
        self.assertEqual(1, len(guarded["market_recommendations"]))
        self.assertNotIn("promotion_status", guarded)

    def test_observe_publishes_labelled_when_enforcement_disabled(self) -> None:
        # Both require-complete-evidence states publish (labelled) once the
        # enforcement switch is off: interception is always subordinate to it.
        for require_complete in (False, True):
            with self.subTest(require_complete=require_complete):
                with self.assertLogs(
                    "app.services.stock_selection.publication_guard",
                    level="WARNING",
                ):
                    guarded = self._publish_with_gate(
                        enforce=False,
                        require_complete=require_complete,
                        promotion_decision="OBSERVE",
                        model_run_id=8,
                    )
                self.assertEqual(1, len(guarded["market_recommendations"]))
                status = guarded["promotion_status"]
                self.assertEqual("OBSERVE", status["promotion_decision"])
                self.assertEqual("非晋级/研究口径", status["promotion_label"])
                self.assertTrue(status["research_only"])
                self.assertEqual(
                    require_complete, status["promotion_require_complete_evidence"]
                )

    def test_observe_refused_only_when_enforcement_on(self) -> None:
        # require_complete_evidence=True + enforce=True refuses; the same
        # require_complete_evidence=True + enforce=False does not (above).
        with self.assertRaises(ValueError):
            self._publish_with_gate(
                enforce=True,
                require_complete=True,
                promotion_decision="OBSERVE",
                model_run_id=8,
            )

    def test_missing_evidence_keeps_behaviour_and_warns(self) -> None:
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ) as captured:
            guarded = self._publish()
        self.assertEqual(1, len(guarded["market_recommendations"]))
        self.assertNotIn("promotion_status", guarded)
        self.assertTrue(
            any(
                "without promotion evidence" in message
                for message in captured.output
            )
        )

    def test_run_id_without_decision_is_missing_evidence(self) -> None:
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ):
            guarded = self._publish(model_run_id=99)
        self.assertEqual(1, len(guarded["market_recommendations"]))
        self.assertNotIn("promotion_status", guarded)


class PublicationGuardPromotionReportTests(unittest.TestCase):
    """Full ``PromotionGateV2Report`` evidence, aligned with serving fields."""

    def _serving(self, config: dict, *, run_id: int, enforce: bool, require_complete: bool):
        return promotion_enforcement.assess_run_for_serving(
            _run(config, run_id=run_id),
            enforce=enforce,
            require_complete_evidence=require_complete,
        )

    def _publish(self, report=None, **kwargs):
        promotion_report = kwargs.get("promotion_report")
        if promotion_report is not None and "model_run_id" not in kwargs:
            # A supplied gate report is only honoured when the report declares
            # the same source run; tests exercise the confirmed path by default.
            kwargs["model_run_id"] = getattr(promotion_report, "run_id", None) or (
                promotion_report.get("run_id")
                if isinstance(promotion_report, dict)
                else None
            )
        return prepare_report_for_publication(
            _report() if report is None else report,
            approved_qualifications=_approved(),
            trusted_regime_snapshots=_regime(),
            **kwargs,
        )

    def _publish_with_gate(
        self, *, enforce: bool, require_complete: bool, **kwargs
    ):
        with patch.object(
            promotion_enforcement,
            "get_settings",
            lambda: _settings(enforce=enforce, require_complete=require_complete),
        ):
            return self._publish(**kwargs)

    def test_unconfirmed_report_marks_provenance_unresolved(self) -> None:
        # A gate report supplied without a matching declared run must never be
        # attributed to the report: mark unresolved instead of attaching it.
        serving = self._serving(
            _REJECT_CONFIG, run_id=42, enforce=False, require_complete=False
        )
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ):
            guarded = self._publish_with_gate(
                enforce=False,
                require_complete=False,
                model_run_id=None,
                promotion_report=serving.report,
            )
        status = guarded["promotion_status"]
        self.assertEqual("unresolved", status["promotion_evidence_provenance"])
        self.assertNotIn("promotion_evidence_version", status)
        self.assertNotIn("promotion_schema_version", status)
        self.assertNotIn("content_digest", status)
        self.assertEqual("非晋级/研究口径", status["promotion_label"])
        self.assertTrue(status["research_only"])
        self.assertNotEqual(
            serving.report.evidence_version, status.get("promotion_evidence_version")
        )

    def test_embedded_evidence_mismatch_marks_provenance_unresolved(self) -> None:
        # Declared run id matches, but the report already embeds evidence from a
        # different run: the mismatch is treated as unresolved, not overwritten.
        serving = self._serving(
            _REJECT_CONFIG, run_id=42, enforce=False, require_complete=False
        )
        stale = _report()
        stale["promotion_status"] = {
            "promotion_evidence_version": (
                "stock_selection_promotion_approval_v2:CN:999:deadbeefdeadbeefdead"
            )
        }
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ) as captured:
            guarded = self._publish_with_gate(
                enforce=False,
                require_complete=False,
                report=stale,
                model_run_id=42,
                promotion_report=serving.report,
            )
        status = guarded["promotion_status"]
        self.assertEqual("unresolved", status["promotion_evidence_provenance"])
        self.assertNotIn("promotion_evidence_version", status)
        self.assertTrue(any("unresolved" in line for line in captured.output))

    def test_full_report_populates_real_schema_evidence_and_reasons(self) -> None:
        serving = self._serving(
            _REJECT_CONFIG, run_id=42, enforce=False, require_complete=False
        )
        report = serving.report
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ):
            guarded = self._publish_with_gate(
                enforce=False, require_complete=False, promotion_report=report
            )
        status = guarded["promotion_status"]
        # Real gate fields, not the synthesised minimal-contract values.
        self.assertEqual("stock_selection_promotion_approval_v2", status["promotion_schema_version"])
        self.assertEqual(report.evidence_version, status["promotion_evidence_version"])
        self.assertEqual(list(report.non_promotable_reasons), status["non_promotable_reasons"])
        self.assertNotIn(
            "promotion_gate_decision:REJECT", status["non_promotable_reasons"]
        )
        self.assertEqual(report.content_digest, status["content_digest"])
        self.assertEqual("REJECT", status["promotion_decision"])
        self.assertFalse(status["promotable"])
        # Serving-aligned audit / marking fields.
        serving_fields = serving.status_fields()
        for key, value in serving_fields.items():
            self.assertEqual(value, status[key], f"mismatch on {key}")

    def test_mapping_report_derives_evidence_version(self) -> None:
        serving = self._serving(
            _REJECT_CONFIG, run_id=42, enforce=False, require_complete=False
        )
        report = serving.report
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ):
            guarded = self._publish_with_gate(
                enforce=False,
                require_complete=False,
                promotion_report=report.as_dict(),
            )
        status = guarded["promotion_status"]
        # as_dict() omits the derived evidence_version; the guard recomputes it
        # with the gate's own formula.
        self.assertNotIn("promotion_evidence_version", report.as_dict())
        self.assertEqual(report.evidence_version, status["promotion_evidence_version"])
        self.assertEqual(report.content_digest, status["content_digest"])

    def test_promotable_report_publishes_without_promotion_status(self) -> None:
        serving = self._serving(
            _passing_config(), run_id=7, enforce=True, require_complete=False
        )
        self.assertTrue(serving.promotable)
        guarded = self._publish_with_gate(
            enforce=True, require_complete=False, promotion_report=serving.report
        )
        self.assertEqual(1, len(guarded["market_recommendations"]))
        self.assertNotIn("promotion_status", guarded)

    def test_reject_report_refuses_publication_when_enforced(self) -> None:
        serving = self._serving(
            _REJECT_CONFIG, run_id=42, enforce=True, require_complete=False
        )
        with self.assertRaises(ValueError) as ctx:
            self._publish_with_gate(
                enforce=True, require_complete=False, promotion_report=serving.report
            )
        self.assertIn("REJECT", str(ctx.exception))
        self.assertIn("42", str(ctx.exception))

    def test_observe_report_matrix_matches_string_contract(self) -> None:
        serving = self._serving(
            _OBSERVE_CONFIG, run_id=8, enforce=True, require_complete=False
        )
        # require_complete=False + enforce=True: labelled, not withheld.
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ):
            guarded = self._publish_with_gate(
                enforce=True, require_complete=False, promotion_report=serving.report
            )
        status = guarded["promotion_status"]
        self.assertEqual("OBSERVE", status["promotion_decision"])
        self.assertEqual(
            list(serving.report.non_promotable_reasons), status["non_promotable_reasons"]
        )
        # require_complete=True + enforce=True: refused like REJECT.
        with self.assertRaises(ValueError):
            self._publish_with_gate(
                enforce=True, require_complete=True, promotion_report=serving.report
            )
        # enforce=False: never refused, always labelled.
        with self.assertLogs(
            "app.services.stock_selection.publication_guard", level="WARNING"
        ):
            labelled = self._publish_with_gate(
                enforce=False, require_complete=True, promotion_report=serving.report
            )
        self.assertFalse(labelled["promotion_status"]["promotion_enforced"])

    def test_full_report_wins_over_string_evidence(self) -> None:
        # A stale/misleading string verdict must not override the real report.
        serving = self._serving(
            _REJECT_CONFIG, run_id=42, enforce=True, require_complete=False
        )
        with self.assertRaises(ValueError):
            self._publish_with_gate(
                enforce=True,
                require_complete=False,
                promotion_decision="ELIGIBLE_FOR_MANUAL_REVIEW",
                promotion_report=serving.report,
            )


class DailyReportPromotionWiringTests(unittest.TestCase):
    """``save_ai_daily_report`` resolves the CN serving run's real gate report."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.temp = TemporaryDirectory()
        self.store = JsonPayloadArtifactStore(Path(self.temp.name))

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def _seed_run(self, db: Session, config: dict) -> int:
        run = ModelRun(
            name="cn-serving-run",
            model_type="lightgbm_multifactor",
            market="CN",
            universe="full_market",
            status="success",
            config_json=json.dumps(config),
            created_at="2026-09-10T00:00:00+00:00",
        )
        db.add(run)
        db.commit()
        return int(run.id)

    def test_save_attaches_real_gate_fields_from_declared_run(self) -> None:
        with Session(self.engine) as db:
            run_id = self._seed_run(db, _OBSERVE_CONFIG)
            report = _report()
            report["model_run_id"] = run_id
            with patch(
                "app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore",
                return_value=self.store,
            ):
                save_ai_daily_report(report, db=db)
            stored = load_ai_daily_report(db=db)
        status = stored.get("promotion_status")
        self.assertIsNotNone(status)
        self.assertEqual("stock_selection_promotion_approval_v2", status["promotion_schema_version"])
        self.assertTrue(status["promotion_evidence_version"].startswith("stock_selection_promotion_approval_v2:"))
        self.assertEqual("OBSERVE", status["promotion_decision"])
        self.assertFalse(status["promotable"])
        self.assertTrue(status["research_only"])
        self.assertEqual("非晋级/研究口径", status["promotion_label"])
        # Real gate reasons, not the synthesised minimal-contract value.
        self.assertNotIn(
            "promotion_gate_decision:OBSERVE", status["non_promotable_reasons"]
        )
        self.assertTrue(status["non_promotable_reasons"])
        self.assertTrue(status["content_digest"])
        # The evidence is the run the report itself declares.
        self.assertIn(f":{run_id}:", status["promotion_evidence_version"])

    def test_missing_run_id_marks_provenance_unresolved_without_borrowing(self) -> None:
        # A servable OBSERVE run is present, but the report does not name it:
        # its evidence must never be attributed to this report.
        with Session(self.engine) as db:
            self._seed_run(db, _OBSERVE_CONFIG)
            with self.assertLogs(
                "app.services.stock_selection.publication_guard", level="WARNING"
            ) as captured, patch(
                "app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore",
                return_value=self.store,
            ):
                save_ai_daily_report(_report(), db=db)
            stored = load_ai_daily_report(db=db)
        status = stored.get("promotion_status")
        self.assertIsNotNone(status)
        self.assertEqual("unresolved", status["promotion_evidence_provenance"])
        # No run-specific evidence may be borrowed from the seeded run.
        self.assertNotIn("promotion_evidence_version", status)
        self.assertNotIn("promotion_schema_version", status)
        self.assertNotIn("content_digest", status)
        self.assertNotIn("promotion_decision", status)
        self.assertFalse(status["promotable"])
        self.assertTrue(status["research_only"])
        self.assertEqual("非晋级/研究口径", status["promotion_label"])
        self.assertTrue(any("unresolved" in line for line in captured.output))

    def test_declared_run_with_inconsistent_embedded_evidence_is_unresolved(self) -> None:
        with Session(self.engine) as db:
            run_id = self._seed_run(db, _OBSERVE_CONFIG)
            report = _report()
            report["model_run_id"] = run_id
            report["promotion_status"] = {
                "promotion_evidence_version": (
                    "stock_selection_promotion_approval_v2:CN:999:deadbeefdeadbeefdead"
                )
            }
            with self.assertLogs(
                "app.services.stock_selection.publication_guard", level="WARNING"
            ) as captured, patch(
                "app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore",
                return_value=self.store,
            ):
                save_ai_daily_report(report, db=db)
            stored = load_ai_daily_report(db=db)
        status = stored["promotion_status"]
        self.assertEqual("unresolved", status["promotion_evidence_provenance"])
        self.assertNotIn("promotion_evidence_version", status)
        self.assertTrue(any("unresolved" in line for line in captured.output))

    def test_resolver_uses_declared_run_not_newest_servable(self) -> None:
        # The report names the REJECT run: its evidence must be used as-is, not
        # silently swapped for an older ELIGIBLE run (no misattribution).
        from app.services.ai_daily_report import _resolve_cn_serving_promotion_report

        with Session(self.engine) as db:
            self._seed_run(db, _passing_config())
            newer = ModelRun(
                name="cn-reject-run",
                model_type="lightgbm_multifactor",
                market="CN",
                universe="full_market",
                status="success",
                config_json=json.dumps(_REJECT_CONFIG),
                created_at="2026-09-11T00:00:00+00:00",
            )
            db.add(newer)
            db.commit()
            resolved = _resolve_cn_serving_promotion_report(db, int(newer.id))
        self.assertIsNotNone(resolved)
        self.assertEqual(str(newer.id), resolved.run_id)
        self.assertFalse(resolved.promotable)

    def test_declared_reject_run_is_refused_or_labelled_with_own_evidence(self) -> None:
        with Session(self.engine) as db:
            run_id = self._seed_run(db, _REJECT_CONFIG)
            report = _report()
            report["model_run_id"] = run_id
            with patch(
                "app.services.stock_selection.decision_ledger.JsonPayloadArtifactStore",
                return_value=self.store,
            ):
                with patch.object(
                    promotion_enforcement,
                    "get_settings",
                    lambda: _settings(enforce=True, require_complete=False),
                ):
                    with self.assertRaises(ValueError) as ctx:
                        save_ai_daily_report(report, db=db)
                self.assertIn(str(run_id), str(ctx.exception))
                with self.assertLogs(
                    "app.services.stock_selection.publication_guard", level="WARNING"
                ), patch.object(
                    promotion_enforcement,
                    "get_settings",
                    lambda: _settings(enforce=False, require_complete=False),
                ):
                    save_ai_daily_report(report, db=db)
            stored = load_ai_daily_report(db=db)
        status = stored["promotion_status"]
        self.assertEqual("REJECT", status["promotion_decision"])
        self.assertIn(f":{run_id}:", status["promotion_evidence_version"])


if __name__ == "__main__":
    unittest.main()
