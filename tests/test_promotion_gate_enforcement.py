"""Phase 4: promotion gate (v2) enforcement on the serving / recommendation path.

Covers the four required scenarios:

1. a non-promotable run (research scope, or a point-in-time/data-readiness
   blocker) is not served as the champion and the payload carries the
   "非晋级/研究口径" marking;
2. a fully passing run serves normally;
3. with ``PQW_PROMOTION_GATE_ENFORCE=false`` the run is still labelled but not
   intercepted, and a WARNING is logged;
4. existing consumers keep their behaviour for evidence-less legacy runs;
5. ``OBSERVE`` (missing evidence) is served and labelled by default, but with
   ``PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE=true`` it is intercepted just
   like ``REJECT``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models import tables  # noqa: F401  (register metadata)
from app.models.tables import ModelRun, Prediction, Symbol
from app.services.repositories.predictions import PredictionRepository
from app.services.stock_selection.promotion_enforcement import (
    PROMOTION_LABEL_ZH,
    assess_run_for_serving,
    build_promotion_candidate,
    promotion_enforce_enabled,
    promotion_require_complete_evidence_enabled,
)


def _passing_config() -> dict:
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


def _run(config: dict | None, *, run_id: int = 1, status: str = "success"):
    return SimpleNamespace(
        id=run_id,
        name=f"run-{run_id}",
        market="CN",
        universe="full_market",
        model_type="lightgbm_multifactor",
        status=status,
        config_json=json.dumps(config if config is not None else {}),
    )


class AssessRunForServingTests(unittest.TestCase):
    def test_all_checks_passing_run_is_allowed(self) -> None:
        decision = assess_run_for_serving(_run(_passing_config()), enforce=True)
        self.assertTrue(decision.promotable)
        self.assertFalse(decision.blocked)
        self.assertFalse(decision.marked_non_promotable)
        fields = decision.status_fields()
        self.assertTrue(fields["promotable"])
        self.assertIsNone(fields["promotion_label"])
        self.assertFalse(fields["research_only"])

    def test_research_scope_run_is_blocked_and_marked(self) -> None:
        decision = assess_run_for_serving(
            _run({"selection_mode": "explicit_tickers"}), enforce=True
        )
        self.assertFalse(decision.promotable)
        self.assertTrue(decision.blocked)
        fields = decision.status_fields()
        self.assertEqual(PROMOTION_LABEL_ZH, fields["promotion_label"])
        self.assertTrue(fields["research_only"])
        self.assertEqual("REJECT", fields["promotion_decision"])
        self.assertTrue(fields["non_promotable_reasons"])

    def test_point_in_time_blocker_is_blocked(self) -> None:
        decision = assess_run_for_serving(
            _run({"data_readiness": {"blockers": ["historical_membership_not_verified"]}}),
            enforce=True,
        )
        self.assertFalse(decision.promotable)
        self.assertTrue(decision.blocked)
        self.assertTrue(
            any(
                "data_readiness" in reason
                for reason in decision.report.non_promotable_reasons
            )
        )

    def test_enforcement_disabled_marks_but_does_not_block_with_warning(self) -> None:
        with self.assertLogs(
            "app.services.stock_selection.promotion_enforcement", level="WARNING"
        ) as captured:
            decision = assess_run_for_serving(
                _run({"selection_mode": "explicit_tickers"}), enforce=False
            )
        self.assertFalse(decision.promotable)
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.marked_non_promotable)
        self.assertEqual(PROMOTION_LABEL_ZH, decision.status_fields()["promotion_label"])
        self.assertIn("PQW_PROMOTION_GATE_ENFORCE=false", decision.warning or "")
        self.assertTrue(
            any(
                "PQW_PROMOTION_GATE_ENFORCE=false" in message
                for message in captured.output
            )
        )

    def test_promotion_enforce_enabled_reads_settings(self) -> None:
        self.assertTrue(promotion_enforce_enabled(SimpleNamespace(promotion_gate_enforce=True)))
        self.assertFalse(promotion_enforce_enabled(SimpleNamespace(promotion_gate_enforce=False)))

    def test_require_complete_evidence_defaults_off(self) -> None:
        # Both the absent attribute and an explicit False stay observe-only.
        self.assertFalse(promotion_require_complete_evidence_enabled(SimpleNamespace()))
        self.assertFalse(
            promotion_require_complete_evidence_enabled(
                SimpleNamespace(promotion_gate_require_complete_evidence=False)
            )
        )
        self.assertTrue(
            promotion_require_complete_evidence_enabled(
                SimpleNamespace(promotion_gate_require_complete_evidence=True)
            )
        )

    def test_observe_run_is_served_and_labelled_by_default(self) -> None:
        # Legacy/evidence-less run → OBSERVE. Default switch off: marked, not
        # withheld, and the audit field reports the switch as off.
        decision = assess_run_for_serving(
            _run({}), enforce=True, require_complete_evidence=False
        )
        self.assertFalse(decision.promotable)
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.marked_non_promotable)
        self.assertEqual("OBSERVE", decision.report.decision)
        fields = decision.status_fields()
        self.assertFalse(fields["promotion_require_complete_evidence"])
        self.assertFalse(fields["promotion_blocked_from_serving"])
        self.assertEqual(PROMOTION_LABEL_ZH, fields["promotion_label"])

    def test_observe_run_is_blocked_when_complete_evidence_required(self) -> None:
        decision = assess_run_for_serving(
            _run({}), enforce=True, require_complete_evidence=True
        )
        self.assertFalse(decision.promotable)
        self.assertTrue(decision.blocked)
        self.assertEqual("OBSERVE", decision.report.decision)
        fields = decision.status_fields()
        self.assertTrue(fields["promotion_require_complete_evidence"])
        self.assertTrue(fields["promotion_blocked_from_serving"])
        self.assertEqual(PROMOTION_LABEL_ZH, fields["promotion_label"])
        self.assertIn("OBSERVE", decision.warning or "")

    def test_reject_still_blocked_when_complete_evidence_required(self) -> None:
        # Turning on the OBSERVE switch must not change how an explicit REJECT
        # is handled.
        decision = assess_run_for_serving(
            _run({"selection_mode": "explicit_tickers"}),
            enforce=True,
            require_complete_evidence=True,
        )
        self.assertEqual("REJECT", decision.report.decision)
        self.assertTrue(decision.blocked)

    def test_complete_evidence_switch_is_subordinate_to_enforcement(self) -> None:
        # With enforcement off nothing is withheld, even the OBSERVE switch on.
        decision = assess_run_for_serving(
            _run({}), enforce=False, require_complete_evidence=True
        )
        self.assertFalse(decision.blocked)
        fields = decision.status_fields()
        self.assertFalse(fields["promotion_enforced"])
        self.assertTrue(fields["promotion_require_complete_evidence"])
        self.assertFalse(fields["promotion_blocked_from_serving"])

    def test_candidate_reads_persisted_evidence(self) -> None:
        candidate = build_promotion_candidate(_run(_passing_config()))
        self.assertEqual(50_000, candidate.training_sample_count)
        self.assertEqual(5, candidate.purge["purge_sessions"])
        self.assertEqual({"blockers": []}, dict(candidate.data_readiness))


class PredictionRepositoryEnforcementTests(unittest.TestCase):
    """Integration: the serving-run selection must honour the gate."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            db.add(
                Symbol(
                    ticker="600000.SS",
                    name="浦发银行",
                    market="CN",
                    is_active=1,
                    created_at="2026-04-01T00:00:00+00:00",
                    updated_at="2026-04-01T00:00:00+00:00",
                )
            )
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _create_run(self, db: Session, name: str, config: dict, *, score: float) -> int:
        run = ModelRun(
            name=name,
            model_type="lightgbm_multifactor",
            market="CN",
            universe="full_market",
            config_json=json.dumps(config),
            status="success",
            created_at="2026-04-10T00:00:00+00:00",
        )
        db.add(run)
        db.flush()
        symbol = db.query(Symbol).filter(Symbol.ticker == "600000.SS").one()
        db.add(
            Prediction(
                model_run_id=run.id,
                symbol_id=symbol.id,
                trade_date="2026-04-10",
                score=score,
                rank_value=1,
                created_at="2026-04-10T00:00:00+00:00",
            )
        )
        db.commit()
        return int(run.id)

    def _list(self, db: Session) -> list[dict]:
        with patch.object(
            PredictionRepository, "_list_live_predictions_for_market", return_value=[]
        ):
            return PredictionRepository(db).list_latest_predictions_for_market("CN", limit=10)

    def test_research_run_is_skipped_in_favour_of_promotable_run(self) -> None:
        with Session(self.engine) as db:
            promotable_id = self._create_run(db, "clean", _passing_config(), score=0.7)
            research_id = self._create_run(
                db, "research", {"selection_mode": "explicit_tickers"}, score=0.99
            )
            rows = self._list(db)
        self.assertEqual([promotable_id], [row["model_run_id"] for row in rows])
        self.assertNotIn(research_id, [row["model_run_id"] for row in rows])
        self.assertTrue(rows[0]["promotable"])
        self.assertFalse(rows[0]["research_only"])

    def test_only_a_non_promotable_run_yields_no_recommendation(self) -> None:
        with Session(self.engine) as db:
            research_id = self._create_run(
                db, "research", {"selection_mode": "explicit_tickers"}, score=0.99
            )
            rows = self._list(db)
        self.assertEqual([], rows)
        # The artifact remains queryable through the research path.
        self.assertIsNotNone(research_id)

    def test_data_readiness_blocker_is_not_served(self) -> None:
        with Session(self.engine) as db:
            self._create_run(
                db,
                "pit-blocked",
                {
                    **_passing_config(),
                    "data_readiness": {"blockers": ["historical_membership_not_verified"]},
                },
                score=0.9,
            )
            rows = self._list(db)
        self.assertEqual([], rows)

    def test_enforcement_disabled_serves_but_labels_research_run(self) -> None:
        with Session(self.engine) as db:
            research_id = self._create_run(
                db, "research", {"selection_mode": "explicit_tickers"}, score=0.99
            )
            with patch(
                "app.services.stock_selection.promotion_enforcement.get_settings",
                return_value=SimpleNamespace(promotion_gate_enforce=False),
            ):
                rows = self._list(db)
        self.assertEqual([research_id], [row["model_run_id"] for row in rows])
        self.assertEqual(PROMOTION_LABEL_ZH, rows[0]["promotion_label"])
        self.assertTrue(rows[0]["research_only"])
        self.assertFalse(rows[0]["promotable"])
        self.assertFalse(rows[0]["promotion_blocked_from_serving"])

    def test_legacy_evidence_less_run_still_serves(self) -> None:
        """Regression: no promotion evidence must not disable legacy serving."""

        with Session(self.engine) as db:
            legacy_id = self._create_run(db, "legacy", {}, score=0.5)
            rows = self._list(db)
        self.assertEqual([legacy_id], [row["model_run_id"] for row in rows])
        # It is honestly marked as not promotable (OBSERVE) but is not withheld.
        self.assertFalse(rows[0]["promotable"])
        self.assertFalse(rows[0]["promotion_blocked_from_serving"])
        self.assertEqual(PROMOTION_LABEL_ZH, rows[0]["promotion_label"])

    def test_legacy_evidence_less_run_is_withheld_when_complete_evidence_required(self) -> None:
        """With the OBSERVE switch on, an evidence-less run is not a champion."""

        with Session(self.engine) as db:
            self._create_run(db, "legacy", {}, score=0.5)
            with patch(
                "app.services.stock_selection.promotion_enforcement.get_settings",
                return_value=SimpleNamespace(
                    promotion_gate_enforce=True,
                    promotion_gate_require_complete_evidence=True,
                ),
            ):
                rows = self._list(db)
        self.assertEqual([], rows)

    def test_run_scoped_read_marks_without_filtering(self) -> None:
        with Session(self.engine) as db:
            research_id = self._create_run(
                db, "research", {"selection_mode": "explicit_tickers"}, score=0.9
            )
            rows = PredictionRepository(db).list_predictions_for_run(research_id)
        # Research artifacts remain queryable, but are explicitly labelled.
        self.assertEqual(1, len(rows))
        self.assertEqual(PROMOTION_LABEL_ZH, rows[0]["promotion_label"])
        self.assertTrue(rows[0]["research_only"])
        self.assertFalse(rows[0]["promotable"])

    def test_run_scoped_read_of_promotable_run_has_no_label(self) -> None:
        with Session(self.engine) as db:
            clean_id = self._create_run(db, "clean", _passing_config(), score=0.9)
            rows = PredictionRepository(db).list_predictions_for_run(clean_id)
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["promotable"])
        self.assertIsNone(rows[0]["promotion_label"])
        self.assertFalse(rows[0]["research_only"])


if __name__ == "__main__":
    unittest.main()
