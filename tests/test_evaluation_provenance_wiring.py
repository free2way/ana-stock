"""End-to-end evaluation wiring for the provenance loader + contract derivation.

Runs on the disposable Postgres test database only.  The price loader is the
sole patched seam; the evaluator, contract resolution, replay and persistence
are the real code paths.
"""
import json
from unittest.mock import patch

from sqlalchemy import select

from tests.postgres_safety import ApplicationPostgresTestCase
from app.core.db import SessionLocal
from app.models.tables import ModelEvaluation, ModelEvaluationMetric, ModelRun, Prediction, Symbol
from app.services.market_calendar import next_market_open_date
from app.services.model_evaluation import evaluate_model_runs


def _sessions(market: str, start: str, count: int) -> list[str]:
    days = [start]
    for _ in range(count - 1):
        days.append(next_market_open_date(market, days[-1], include_self=False))
    return days


def _provenance_history(market: str, signal_date: str, horizon: int = 5) -> list[dict]:
    days = _sessions(market, signal_date, horizon + 2)
    return [
        {
            "date": day,
            "symbol": "000001.SZ" if market == "CN" else "AAPL",
            "open": 100.0,
            "high": 110.0,
            "low": 99.0,
            "close": 105.0,
            "volume": 100000.0,
            "price_basis": "raw",
            "execution_source_reference": f"lake_v2:{market}:{day}:fixture",
            "corporate_action_status": "none",
            "corporate_action_evidence": [],
        }
        for day in days
    ]


class EvaluationProvenanceWiringTests(ApplicationPostgresTestCase):
    def _seed(self, config: dict, trade_date: str = "2026-07-01") -> int:
        with SessionLocal() as db:
            symbol = Symbol(
                ticker="000001.SZ",
                name="Fixture",
                market="CN",
                created_at="2026-07-01T00:00:00+00:00",
                updated_at="2026-07-01T00:00:00+00:00",
            )
            run = ModelRun(
                name="provenance-fixture",
                model_type="lightgbm_multifactor",
                market="CN",
                universe="fixture",
                status="success",
                train_start="2025-01-01",
                train_end="2026-06-01",
                test_start=trade_date,
                config_json=json.dumps(config),
                created_at="2026-07-01T00:00:00+00:00",
            )
            db.add_all([symbol, run])
            db.flush()
            db.add(
                Prediction(
                    model_run_id=run.id,
                    symbol_id=symbol.id,
                    trade_date=trade_date,
                    score=0.5,
                    rank_value=1,
                    created_at="2026-07-01T00:00:00+00:00",
                )
            )
            db.commit()
            return int(run.id)

    def test_executable_profile_derives_contract_and_closes_real_replay(self) -> None:
        run_id = self._seed(
            {
                "target_profile": "confirmed_next_open_fixed_exit_fill_cost_v2",
                "evaluation_protocol": "walk_forward_purged_v2",
                "oos_start_date": "2026-07-01",
                "purge_gap_days": 5,
                "execution_contract": None,
                "execution_cost_bps": {
                    "commission_bps_one_way": 2.5,
                    "slippage_bps_one_way": 15.0,
                },
            }
        )
        with patch(
            "app.services.model_evaluation.load_lake_price_history_with_provenance",
            side_effect=lambda *, market, ticker, limit=320: _provenance_history(market, "2026-07-01"),
        ):
            with SessionLocal() as db:
                evaluate_model_runs(
                    db, markets=["CN"], model_run_id=run_id, recent_trade_dates=1,
                    top_n=1, horizons=(5,), require_execution_reconciliation=True,
                )
        with SessionLocal() as db:
            evaluation = db.scalar(select(ModelEvaluation).where(ModelEvaluation.model_run_id == run_id))
            summary = json.loads(evaluation.summary_json)
        self.assertEqual("success", evaluation.status)
        self.assertTrue(summary["execution_verified"])
        self.assertEqual(0, summary["outcome_counts"].get("UNVERIFIED", 0))
        self.assertEqual(1, summary["outcome_counts"].get("CLOSED", 0))
        ledger = summary["candidate_outcomes"]
        self.assertEqual(1, len(ledger))
        self.assertIsNone(ledger[0]["reason"])
        # The contract is the label's own cost口径, re-derived not fabricated.
        self.assertEqual(2.5, summary["cost_contract"]["cost"]["commission_bps_one_way"])
        # No `prediction_horizon_days` on this run: the caller's set must pass
        # through unchanged (non-regression for the run-horizon union).
        self.assertEqual([5], summary["horizons_used"])
        self.assertIsNone(summary["run_prediction_horizon_days"])

    def test_run_prediction_horizon_is_union_into_evaluation(self) -> None:
        """A run scoring a 6-day forward return must be evaluated at horizon 6.

        The reliability producer filters CLOSED candidate outcomes by the run's
        ``prediction_horizon_days``; before this the scheduled default set
        ``(1, 3, 5, 10, 20)`` never contained 6, so the producer saw zero matured
        rows and silently wrote no artifact.
        """
        run_id = self._seed(
            {
                "prediction_horizon_days": 6,
                "target_profile": "confirmed_next_open_fixed_exit_fill_cost_v2",
                "evaluation_protocol": "walk_forward_purged_v2",
                "oos_start_date": "2026-07-01",
                "purge_gap_days": 5,
                "execution_contract": None,
                "execution_cost_bps": {
                    "commission_bps_one_way": 2.5,
                    "slippage_bps_one_way": 15.0,
                },
            }
        )
        # 23 sessions so every default horizon (max 20) plus the run horizon (6)
        # has enough forward bars to close.
        with patch(
            "app.services.model_evaluation.load_lake_price_history_with_provenance",
            side_effect=lambda *, market, ticker, limit=320: _provenance_history(market, "2026-07-01", horizon=21),
        ):
            with SessionLocal() as db:
                evaluate_model_runs(
                    db, markets=["CN"], model_run_id=run_id, recent_trade_dates=1,
                    top_n=1, require_execution_reconciliation=True,
                )
        with SessionLocal() as db:
            evaluation = db.scalar(select(ModelEvaluation).where(ModelEvaluation.model_run_id == run_id))
            summary = json.loads(evaluation.summary_json)
            metric_horizons = sorted(
                db.scalars(
                    select(ModelEvaluationMetric.horizon_days).where(
                        ModelEvaluationMetric.model_evaluation_id == evaluation.id,
                        ModelEvaluationMetric.metric_scope == "overall",
                    )
                ).all()
            )
        self.assertEqual([1, 3, 5, 6, 10, 20], summary["horizons_used"])
        self.assertEqual(6, summary["run_prediction_horizon_days"])
        closed_horizons = sorted(
            {
                int(row["horizon_days"])
                for row in summary["candidate_outcomes"]
                if str(row.get("status") or "").upper() == "CLOSED"
            }
        )
        self.assertIn(6, closed_horizons)
        # CN replay rejects a 1-day horizon by protocol; every other horizon
        # (including the run's own 6) still closes.
        self.assertEqual([3, 5, 6, 10, 20], closed_horizons)
        self.assertIn(6, metric_horizons)

    def test_legacy_profile_does_not_invent_a_contract(self) -> None:
        run_id = self._seed(
            {
                "target_profile": "short_horizon_composite_v1",
                "evaluation_protocol": "walk_forward_purged_v2",
                "oos_start_date": "2026-07-01",
                "purge_gap_days": 5,
                "execution_contract": None,
                "execution_cost_bps": {
                    "commission_bps_one_way": 2.5,
                    "slippage_bps_one_way": 15.0,
                },
            }
        )
        with patch(
            "app.services.model_evaluation.load_lake_price_history_with_provenance",
            return_value=_provenance_history("CN", "2026-07-01"),
        ):
            with SessionLocal() as db:
                evaluate_model_runs(
                    db, markets=["CN"], model_run_id=run_id, recent_trade_dates=1,
                    top_n=1, horizons=(5,), require_execution_reconciliation=True,
                )
        with SessionLocal() as db:
            evaluation = db.scalar(select(ModelEvaluation).where(ModelEvaluation.model_run_id == run_id))
            summary = json.loads(evaluation.summary_json)
        self.assertEqual("partial", evaluation.status)
        self.assertFalse(summary["execution_verified"])
        self.assertEqual(0, summary["outcome_counts"].get("CLOSED", 0))
