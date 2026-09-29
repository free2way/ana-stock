from tests.postgres_safety import ApplicationPostgresTestCase
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.tables import ModelEvaluation, ModelEvaluationMetric, ModelRun, Prediction, Symbol, WorkspaceSnapshot
from app.services.model_evaluation import (
    DEFAULT_HORIZONS,
    SCHEDULED_EVALUATION_TRADE_DATES,
    STRICT_OOS_MIN_COVERAGE_DAYS,
    _history_outcome,
    cost_sensitivity_ladder,
    evaluate_model_runs,
    latest_model_activation_statuses,
    list_latest_model_evaluations,
    summarize_evaluation_samples,
)
from app.services.model_challenger import challenger_race_readiness


class EvaluationWindowTests(TestCase):
    def test_scheduled_window_can_mature_the_challenger_gate_for_longest_horizon(self) -> None:
        matured_dates = SCHEDULED_EVALUATION_TRADE_DATES - max(DEFAULT_HORIZONS)

        self.assertGreaterEqual(matured_dates, STRICT_OOS_MIN_COVERAGE_DAYS)


class CostSensitivityLadderTests(TestCase):
    """P0 #3 acceptance: one measured path set, repriced at escalating costs."""

    SAMPLES = [
        {"gross_return_pct": 6.0, "drawdown_pct": -1.0},
        {"gross_return_pct": 1.0, "drawdown_pct": -2.0},
        {"gross_return_pct": 0.5, "drawdown_pct": -1.5},
        {"gross_return_pct": -3.0, "drawdown_pct": -5.0},
    ]

    def test_ladder_keeps_gross_fixed_and_decays_net_monotonically(self) -> None:
        rows = cost_sensitivity_ladder(self.SAMPLES, horizon_days=5, round_trip_cost_bps=50.0)
        self.assertEqual([20.0, 50.0, 80.0], [row["round_trip_cost_bps"] for row in rows])
        gross_values = [row["gross_avg_return"] for row in rows]
        net_values = [row["net_avg_return"] for row in rows]
        for value in gross_values[1:]:
            self.assertAlmostEqual(gross_values[0], value, places=9)
        self.assertTrue(all(earlier > later for earlier, later in zip(net_values, net_values[1:])))
        self.assertAlmostEqual(0.3, rows[0]["net_avg_return_delta_vs_base"], places=9)
        self.assertAlmostEqual(0.0, rows[1]["net_avg_return_delta_vs_base"], places=9)
        self.assertAlmostEqual(-0.3, rows[2]["net_avg_return_delta_vs_base"], places=9)

    def test_ladder_hit_rate_never_increases_with_cost(self) -> None:
        rows = cost_sensitivity_ladder(self.SAMPLES, horizon_days=5, round_trip_cost_bps=50.0)
        hit_rates = [row["hit_rate"] for row in rows]
        self.assertTrue(all(earlier >= later for earlier, later in zip(hit_rates, hit_rates[1:])))

    def test_ladder_rejects_invalid_cost_levels(self) -> None:
        with self.assertRaises(ValueError):
            cost_sensitivity_ladder(self.SAMPLES, horizon_days=1, round_trip_cost_bps=-1)
        with self.assertRaises(ValueError):
            cost_sensitivity_ladder(
                self.SAMPLES, horizon_days=1, round_trip_cost_bps=50.0, ladder_bps=(10.0, float("nan"))
            )

    def test_ladder_handles_empty_samples_without_base_delta(self) -> None:
        rows = cost_sensitivity_ladder([], horizon_days=5, round_trip_cost_bps=50.0)
        self.assertTrue(rows)
        self.assertTrue(
            all(row["hit_rate"] is None and row["net_avg_return_delta_vs_base"] is None for row in rows)
        )


class ModelEvaluationTests(ApplicationPostgresTestCase):


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
            # These two reads MUST stay inside the session context: reusing a
            # closed Session lazily checks out a fresh connection with an open
            # transaction that nothing ever rolls back, leaving an idle-in-
            # transaction connection that blocks the next setUp() TRUNCATE
            # until the 60s statement timeout fires.
            activation_by_run = latest_model_activation_statuses(db, model_run_ids=[run.id])
            readiness = challenger_race_readiness(db, markets=["CN"])

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
        # P0 closeout entry basis: the T+1 tradable price, carried onto the
        # adjusted close series.  This fixture has no `open` fields, so the
        # gap ratio falls back to the next close: entry 102 -> exit 105 gives
        # gross +2.941176...%, net of the 20bps round trip +2.741176...%.
        # The pre-closeout signal-close basis (5.0 gross) is no longer valid.
        self.assertAlmostEqual(2.7411764705882247, overall_2d.avg_return, places=6)
        self.assertEqual("risk_on", api_rows[0]["market_state_metrics"][0]["market_regime"])
        self.assertEqual("observation_insufficient_oos", api_rows[0]["activation_status"])
        self.assertEqual("observation_insufficient_oos", activation_by_run[run.id])
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
