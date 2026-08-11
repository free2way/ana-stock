import os
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import SessionLocal, configure_database, init_db
from app.models.base import Base
from app.models.tables import ModelEvaluation, ModelEvaluationMetric, ModelRun, Prediction, Symbol, WorkspaceSnapshot
from app.services.model_evaluation import _history_outcome, evaluate_model_runs, latest_model_activation_statuses, list_latest_model_evaluations, summarize_evaluation_samples
from app.services.model_challenger import challenger_race_readiness


TEST_DATABASE_URL = "postgresql+psycopg://quant:quant!123@127.0.0.1:5432/quant_test"


class ModelEvaluationTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["PQW_DATABASE_URL"] = os.getenv("PQW_TEST_DATABASE_URL", TEST_DATABASE_URL)
        get_settings.cache_clear()
        configure_database()
        init_db()

    def setUp(self) -> None:
        with SessionLocal() as db:
            for table in reversed(Base.metadata.sorted_tables):
                db.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))
            db.commit()

    def test_summary_is_net_of_cost_and_reports_drawdown(self) -> None:
        result = summarize_evaluation_samples(
            [
                {"gross_return_pct": 2.0, "drawdown_pct": -1.0},
                {"gross_return_pct": -1.0, "drawdown_pct": -3.0},
            ],
            horizon_days=5,
            round_trip_cost_bps=20,
        )
        self.assertEqual(2, result["sample_count"])
        self.assertAlmostEqual(0.3, result["avg_return"], places=6)
        self.assertAlmostEqual(-3.0, result["max_drawdown"], places=6)
        self.assertAlmostEqual(50.0, result["hit_rate"], places=6)

    def test_reverse_split_like_price_jump_is_excluded(self) -> None:
        outcome = _history_outcome(
            [
                {"date": "2026-07-01", "close": 0.05, "low": 0.04},
                {"date": "2026-07-02", "close": 4.0, "low": 3.8},
            ],
            trade_date="2026-07-01",
            horizon_days=1,
        )
        self.assertEqual("suspected_corporate_action_discontinuity", outcome["excluded_reason"])

    def test_persists_market_state_slice_from_matching_prediction_date(self) -> None:
        with SessionLocal() as db:
            symbol = Symbol(ticker="000001.SZ", name="Ping An", market="CN", created_at="2026-07-01T00:00:00+00:00", updated_at="2026-07-01T00:00:00+00:00")
            run = ModelRun(
                name="cn-oos",
                model_type="lightgbm_multifactor",
                market="CN",
                universe="full_market",
                train_start="2025-01-01",
                # New runs score in a purged walk-forward loop. `train_end`
                # records the rolling run envelope and must not turn the
                # already-forward scores into in-sample observations.
                train_end="2026-07-31",
                test_start="2026-07-01",
                test_end=None,
                config_json='{"input_market_date":"2026-07-01","evaluation_protocol":"walk_forward_purged_v1","oos_start_date":"2026-07-01","purge_gap_days":5,"universe_version":"full_market:all"}',
                artifact_path=None,
                status="success",
                created_at="2026-07-01T00:00:00+00:00",
                finished_at="2026-07-01T00:01:00+00:00",
            )
            db.add_all([symbol, run])
            db.flush()
            db.add(Prediction(model_run_id=run.id, symbol_id=symbol.id, trade_date="2026-07-01", score=0.9, rank_value=1, created_at="2026-07-01T00:00:00+00:00"))
            db.add(WorkspaceSnapshot(
                snapshot_type="market_regime_snapshot:CN",
                snapshot_date="2026-07-01",
                payload_json='{"regime":"risk_on","risk_regime":"risk_on","buy_gate":"ALLOW"}',
                source_job_id=None,
                created_at="2026-07-01T00:00:00+00:00",
            ))
            db.commit()
            history = [
                {"date": "2026-07-01", "close": 100.0, "low": 99.0},
                {"date": "2026-07-02", "close": 102.0, "low": 101.0},
                {"date": "2026-07-03", "close": 105.0, "low": 100.0},
            ]
            with patch("app.services.model_evaluation.load_lake_price_history", return_value=history):
                result = evaluate_model_runs(
                    db,
                    markets=["CN"],
                    model_run_id=run.id,
                    recent_trade_dates=1,
                    top_n=1,
                    horizons=(1, 2),
                    round_trip_cost_bps=20.0,
                )
            evaluation = db.scalar(select(ModelEvaluation))
            metrics = list(db.scalars(select(ModelEvaluationMetric).order_by(ModelEvaluationMetric.horizon_days, ModelEvaluationMetric.metric_scope)).all())
            api_rows = list_latest_model_evaluations(db, market="CN", limit=1)

        self.assertEqual("success", result["status"])
        self.assertEqual("success", evaluation.status)
        self.assertTrue(bool(evaluation.is_out_of_sample))
        self.assertEqual(1, evaluation.oos_sample_count)
        self.assertEqual(1, evaluation.oos_coverage_days)
        self.assertEqual(5, evaluation.purge_gap_days)
        self.assertEqual("observation_insufficient_oos", evaluation.activation_status)
        self.assertEqual("2026-07-01", evaluation.input_as_of_date)
        state_metrics = [row for row in metrics if row.metric_scope.startswith("market_state:")]
        self.assertEqual(2, len(state_metrics))
        self.assertTrue(all(row.market_regime == "risk_on" for row in state_metrics))
        overall_2d = next(row for row in metrics if row.horizon_days == 2 and row.metric_scope == "overall")
        self.assertAlmostEqual(4.8, overall_2d.avg_return, places=6)
        self.assertEqual("risk_on", api_rows[0]["market_state_metrics"][0]["market_regime"])
        self.assertEqual("observation_insufficient_oos", api_rows[0]["activation_status"])
        self.assertEqual(
            "observation_insufficient_oos",
            latest_model_activation_statuses(db, model_run_ids=[run.id])[run.id],
        )
        readiness = challenger_race_readiness(db, markets=["CN"])
        self.assertEqual("waiting_for_oos", readiness["status"])
        self.assertEqual("strict_oos_evidence_insufficient", readiness["markets"]["CN"]["reason"])

    def test_full_market_template_governance_attaches_observation_status_before_risk_filter(self) -> None:
        from app.services.screener import ScreenerService

        service = ScreenerService()
        rows = [{"ticker": "000001.SZ", "market": "CN", "model_activation_status": "unverified"}]
        with patch.object(
            service,
            "_load_model_context_map",
            return_value={"000001.SZ": {"model_run_id": 77, "activation_status": "observation_insufficient_oos"}},
        ), patch.object(service, "_apply_trade_readiness", side_effect=lambda value: value):
            result = service.apply_candidate_governance(rows)

        self.assertEqual(77, result[0]["model_run_id"])
        self.assertEqual("observation_insufficient_oos", result[0]["model_activation_status"])
