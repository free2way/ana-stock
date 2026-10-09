"""Call-site acceptance for the reliability-weighted confluence producers.

``test_oos_reliability_producers`` proved the producers/injection/persistence
primitives work in isolation.  These tests prove the previously missing
*production call sites* now exist:

1. the schedulers persist rolling OOS reliability + calibration artifacts right
   after structured evaluation, and a producer failure only warns;
2. the screener/fusion call sites load the newest artifact, inject row metadata
   and fall back to the artifact's ``probability_calibration`` when the request
   omits it (explicit parameters still win);
3. a missing artifact leaves the legacy equal-weight / null-probability
   behaviour untouched.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import tables  # noqa: F401  (register metadata)
from app.models.base import Base
from app.models.tables import ModelEvaluation, ModelRun, Prediction, Symbol
from app.services.model_signal_summary import resolve_probability_calibrator
from app.services.stock_selection.multi_model_confluence import aggregate_multi_model_rows
from app.services.stock_selection.reliability_artifacts import (
    MaturedReliabilityRow,
    attach_reliability_metadata,
    collect_matured_rows,
    inject_calibration_defaults,
    load_latest_calibration_artifact,
    load_latest_reliability_metadata,
    matured_rows_from_candidate_outcomes,
    persist_reliability_artifacts,
    resolve_model_template_key,
    run_model_version,
)

HIGH = "lightgbm_top_picks"
LOW = "next_tesla_swing"
HIGH_VERSION = "model_run:1:us_close_2026-04-20"
LOW_VERSION = "model_run:2:cn_close_2026-04-20"
SCORES = (0.1, 0.3, 0.5, 0.7, 0.9)
HORIZON = 5
FEATURE_START = date(2026, 3, 2)
TRADING_DATES = 8
AS_OF = FEATURE_START + timedelta(days=TRADING_DATES + 30)


def _matured_rows(version: str, *, positive_scores: tuple[float, ...]) -> list[MaturedReliabilityRow]:
    rows: list[MaturedReliabilityRow] = []
    for day_index in range(TRADING_DATES):
        feature_date = FEATURE_START + timedelta(days=day_index)
        for ticker_index, score in enumerate(SCORES):
            rows.append(
                MaturedReliabilityRow(
                    model_version=version,
                    ticker=f"T{ticker_index}",
                    feature_date=feature_date,
                    label_available_date=feature_date + timedelta(days=HORIZON),
                    horizon_days=HORIZON,
                    raw_score=score,
                    cross_sectional_rank=score,
                    net_return=0.02 if score in positive_scores else -0.01,
                )
            )
    return rows


def _combined_rows() -> list[MaturedReliabilityRow]:
    return [
        *_matured_rows(HIGH_VERSION, positive_scores=(0.5, 0.7, 0.9)),
        *_matured_rows(LOW_VERSION, positive_scores=(0.1,)),
    ]


def _key_map() -> dict[str, str]:
    return {HIGH_VERSION: HIGH, LOW_VERSION: LOW}


def _row(ticker: str, *, model_score: float, action: str = "pullback") -> dict:
    return {
        "ticker": ticker,
        "market": "CN",
        "snapshot_score": model_score * 100.0,
        "trend_score": model_score * 100.0,
        "model_score": model_score,
        "action_label": action,
        "model_execution_tags": [],
        "tradability_status": "READY",
        "trade_readiness_score": 80.0,
    }


def _template_rows() -> dict[str, list[dict]]:
    return {
        HIGH: [_row("ALPHA", model_score=0.1), _row("OMEGA", model_score=0.9)],
        LOW: [_row("ALPHA", model_score=0.1), _row("OMEGA", model_score=0.9)],
    }


def _params(**overrides) -> dict:
    params = {
        "multi_model_templates": [HIGH, LOW],
        "min_multi_model_hits": 2,
        "confluence_action_filter": "ALL",
        "strategy_profile": "",
        "market": "CN",
        "universe": "full_market",
        "sort_by": "confluence_rank",
        "sort_order": "desc",
        "limit": 500,
        "lang": "zh",
    }
    params.update(overrides)
    return params


class KeyResolutionTests(unittest.TestCase):
    def test_lightgbm_run_resolves_to_its_template(self) -> None:
        key, note = resolve_model_template_key(
            model_type="lightgbm_multifactor",
            market="US",
            config={"model_type": "lightgbm", "signal_type": "momentum"},
        )
        self.assertEqual(HIGH, key)
        self.assertEqual("resolved:lightgbm->lightgbm_top_picks", note)

    def test_unmapped_model_type_falls_back_with_audit_note(self) -> None:
        key, note = resolve_model_template_key(
            model_type="ridge_multifactor", market="CN", config={"model_type": "ridge"}
        )
        self.assertIsNone(key)
        self.assertIn("unmapped_model_type:ridge", note)

    def test_non_momentum_signal_is_not_mapped(self) -> None:
        key, note = resolve_model_template_key(
            model_type="lightgbm_multifactor",
            market="CN",
            config={"model_type": "lightgbm", "signal_type": "mean_reversion"},
        )
        self.assertIsNone(key)
        self.assertIn("unmapped_signal_type", note)

    def test_run_model_version_is_stable_and_collision_free(self) -> None:
        run = SimpleNamespace(id=7, name="cn_close_2026-04-20")
        self.assertEqual("model_run:7:cn_close_2026-04-20", run_model_version(run))


class ArtifactWiringTests(unittest.TestCase):
    def test_persist_load_inject_fusion_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            written = persist_reliability_artifacts(
                _combined_rows(),
                as_of_date=AS_OF,
                key_map=_key_map(),
                calibration_model_key=HIGH,
                market="CN",
                root=directory,
            )
            self.assertEqual("written", written["reliability_status"])
            self.assertEqual("written", written["calibration_status"])

            metadata = load_latest_reliability_metadata(directory)
            self.assertEqual({HIGH, LOW}, set(metadata))
            self.assertGreater(metadata[HIGH].oos_hit_rate, metadata[LOW].oos_hit_rate)

            artifact = load_latest_calibration_artifact(directory)
            assert artifact is not None
            calibrator, status = resolve_probability_calibrator(
                {"probability_calibration": artifact.calibration_params()}
            )
            self.assertEqual("calibrated", status)
            assert calibrator is not None

            enriched = attach_reliability_metadata(_template_rows(), metadata={})
            # Explicit empty metadata is a no-op; the loader path is what injects.
            self.assertNotIn("model_oos_hit_rate", enriched[HIGH][0])

            enriched = attach_reliability_metadata(
                _template_rows(), root=directory
            )
            self.assertIn("model_oos_hit_rate", enriched[HIGH][0])

            fusion_params = inject_calibration_defaults(_params(), root=directory)
            self.assertIn("probability_calibration", fusion_params)
            out, meta = aggregate_multi_model_rows(
                enriched,
                template_keys=[HIGH, LOW],
                params=fusion_params,
            )

            self.assertTrue(meta["weight_source"].startswith("row_metadata:"))
            self.assertNotEqual("equal_weight_fallback", meta["weight_source"])
            self.assertEqual("calibrated", meta["calibration_status"])
            by_ticker = {row["ticker"]: row for row in out}
            self.assertGreater(
                by_ticker["OMEGA"]["model_weight_lightgbm_top_picks"],
                by_ticker["OMEGA"]["model_weight_next_tesla_swing"],
            )
            self.assertIsNotNone(by_ticker["OMEGA"]["expected_hit_probability"])
            self.assertIsNotNone(by_ticker["ALPHA"]["expected_hit_probability"])
            self.assertGreater(
                by_ticker["OMEGA"]["expected_hit_probability"],
                by_ticker["ALPHA"]["expected_hit_probability"],
            )

    def test_explicit_probability_calibration_wins_over_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            persist_reliability_artifacts(
                _combined_rows(),
                as_of_date=AS_OF,
                key_map=_key_map(),
                calibration_model_key=HIGH,
                market="CN",
                root=directory,
            )
            explicit = {"method": "bins", "bins": []}
            fused = inject_calibration_defaults(
                _params(probability_calibration=explicit), root=directory
            )
            self.assertIs(explicit, fused["probability_calibration"])

    def test_missing_artifact_keeps_legacy_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual({}, load_latest_reliability_metadata(directory))
            self.assertIsNone(load_latest_calibration_artifact(directory))

            enriched = attach_reliability_metadata(_template_rows(), root=directory)
            self.assertNotIn("model_oos_hit_rate", enriched[HIGH][0])

            fusion_params = inject_calibration_defaults(_params(), root=directory)
            self.assertIsNone(fusion_params.get("probability_calibration"))

            out, meta = aggregate_multi_model_rows(
                enriched,
                template_keys=[HIGH, LOW],
                params=fusion_params,
            )
            self.assertEqual("equal_weight_fallback", meta["weight_source"])
            self.assertTrue(meta["calibration_status"].startswith("unavailable"))
            for row in out:
                self.assertIsNone(row["expected_hit_probability"])

    def test_thin_sample_writes_no_reliability_or_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            written = persist_reliability_artifacts(
                _combined_rows(),
                as_of_date=AS_OF,
                key_map=_key_map(),
                calibration_model_key=HIGH,
                minimum_observations=10_000,
                root=directory,
            )
            self.assertEqual("insufficient_samples", written["reliability_status"])
            self.assertEqual("insufficient_samples", written["calibration_status"])
            self.assertEqual({}, load_latest_reliability_metadata(directory))
            self.assertIsNone(load_latest_calibration_artifact(directory))


class MaturedRowExtractionTests(unittest.TestCase):
    def _candidate(self, **overrides) -> dict:
        base = {
            "ticker": "ALPHA",
            "trade_date": "2026-03-02",
            "horizon_days": HORIZON,
            "status": "CLOSED",
            "is_out_of_sample": True,
            "net_return": 0.031,
            "label_available_date": "2026-03-09",
        }
        base.update(overrides)
        return base

    def _index(self) -> dict[tuple[str, str], tuple[float, float]]:
        return {("ALPHA", "2026-03-02"): (0.42, 0.8)}

    def test_keeps_only_closed_oos_matured_rows_with_scores(self) -> None:
        index = self._index()
        kept = matured_rows_from_candidate_outcomes(
            [
                self._candidate(),
                self._candidate(status="PENDING"),
                self._candidate(is_out_of_sample=False),
                self._candidate(horizon_days=10),
                self._candidate(ticker="MISSING"),
            ],
            model_version=HIGH_VERSION,
            prediction_index=index,
            horizon_days=HORIZON,
        )
        self.assertEqual(1, len(kept))
        row = kept[0]
        self.assertEqual(HIGH_VERSION, row.model_version)
        self.assertEqual("ALPHA", row.ticker)
        self.assertEqual(date(2026, 3, 2), row.feature_date)
        self.assertEqual(date(2026, 3, 9), row.label_available_date)
        self.assertAlmostEqual(0.42, row.raw_score)
        self.assertAlmostEqual(0.031, row.net_return)

    def test_label_available_date_falls_back_to_exit_date(self) -> None:
        index = self._index()
        kept = matured_rows_from_candidate_outcomes(
            [self._candidate(label_available_date=None, exit_date="2026-03-09")],
            model_version=HIGH_VERSION,
            prediction_index=index,
            horizon_days=HORIZON,
        )
        self.assertEqual(1, len(kept))
        self.assertEqual(date(2026, 3, 9), kept[0].label_available_date)


def _seed_db(db: Session) -> None:
    db.add_all(
        [
            Symbol(
                ticker="T0",
                name="T0",
                market="US",
                is_active=1,
                created_at="2026-04-20T00:00:00+00:00",
                updated_at="2026-04-20T00:00:00+00:00",
            ),
            Symbol(
                ticker="T1",
                name="T1",
                market="US",
                is_active=1,
                created_at="2026-04-20T00:00:00+00:00",
                updated_at="2026-04-20T00:00:00+00:00",
            ),
        ]
    )
    db.flush()
    symbols = {row.ticker: row for row in db.query(Symbol).all()}
    run = ModelRun(
        name="us_close_2026-04-20",
        model_type="lightgbm_multifactor",
        market="US",
        universe="full_market_us_lake",
        status="success",
        created_at="2026-04-20T00:00:00+00:00",
        config_json=json.dumps(
            {"model_type": "lightgbm", "signal_type": "momentum", "prediction_horizon_days": HORIZON}
        ),
    )
    db.add(run)
    db.flush()
    outcomes: list[dict] = []
    for day_index in range(TRADING_DATES):
        feature_date = FEATURE_START + timedelta(days=day_index)
        for ticker in ("T0", "T1"):
            score = 0.9 if ticker == "T1" else 0.1
            db.add(
                Prediction(
                    model_run_id=run.id,
                    symbol_id=symbols[ticker].id,
                    trade_date=feature_date.isoformat(),
                    score=score,
                    rank_value=score,
                    created_at="2026-04-20T00:00:00+00:00",
                )
            )
            outcomes.append(
                {
                    "ticker": ticker,
                    "trade_date": feature_date.isoformat(),
                    "horizon_days": HORIZON,
                    "status": "CLOSED",
                    "is_out_of_sample": True,
                    "net_return": 0.02 if score > 0.5 else -0.01,
                    "label_available_date": (feature_date + timedelta(days=HORIZON)).isoformat(),
                }
            )
    db.add(
        ModelEvaluation(
            model_run_id=run.id,
            market="US",
            evaluation_type="prediction_forward_return",
            is_out_of_sample=1,
            sample_count=len(outcomes),
            status="success",
            round_trip_cost_bps=50.0,
            summary_json=json.dumps({"candidate_outcomes": outcomes}),
            created_at="2026-04-20T00:00:00+00:00",
            finished_at="2026-04-20T00:00:00+00:00",
        )
    )
    db.commit()


class DatabaseCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            _seed_db(db)

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_collects_matured_oos_rows_and_writes_artifacts(self) -> None:
        with Session(self.engine) as db, tempfile.TemporaryDirectory() as directory:
            rows, key_map = collect_matured_rows(db, markets=["US"], recent_runs=1)
            self.assertEqual(TRADING_DATES * 2, len(rows))
            self.assertEqual(1, len(key_map["model_key_by_version"]))
            self.assertEqual(HIGH, next(iter(key_map["model_key_by_version"].values())))

            # 16 rows is below the default reliability floor, so a targeted
            # minimum lets the same fixtures prove the write path end to end.
            written = persist_reliability_artifacts(
                rows,
                as_of_date=AS_OF,
                key_map=key_map["model_key_by_version"],
                notes=key_map["notes"],
                calibration_model_key=HIGH,
                minimum_observations=8,
                root=directory,
            )
            self.assertEqual("written", written["reliability_status"])
            self.assertEqual("written", written["calibration_status"])
            metadata = load_latest_reliability_metadata(directory)
            self.assertEqual({HIGH}, set(metadata))
            # T1 scores are the winners: its row metadata must not be equal-weight.
            enriched = attach_reliability_metadata(_template_rows(), root=directory)
            self.assertIn("model_oos_hit_rate", enriched[HIGH][0])

    def test_refresh_orchestrator_writes_artifacts_and_reports_success(self) -> None:
        from app.services.stock_selection.reliability_artifacts import (
            refresh_stock_selection_reliability_artifacts,
        )

        with Session(self.engine) as db, tempfile.TemporaryDirectory() as directory:
            result = refresh_stock_selection_reliability_artifacts(
                db,
                markets=["US"],
                recent_runs=1,
                minimum_observations=8,
                as_of_date=AS_OF,
                root=directory,
            )
        self.assertEqual("success", result["status"])
        self.assertEqual("written", result["reliability_status"])
        self.assertEqual([HIGH], result["reliability_models"])


class SchedulerIsolationTests(unittest.TestCase):
    def test_us_reliability_refresh_failure_only_warns(self) -> None:
        from app.services.us_market_scheduler import USMarketSchedulerService

        service = USMarketSchedulerService()
        db = MagicMock()
        with patch(
            "app.services.stock_selection.reliability_artifacts.refresh_stock_selection_reliability_artifacts",
            side_effect=RuntimeError("producer exploded"),
        ):
            result = service._refresh_reliability_artifacts(db, markets=["US"])
        self.assertEqual("failed", result["status"])
        self.assertIn("producer exploded", result["error"])

    def test_cn_reliability_refresh_failure_only_warns(self) -> None:
        from app.services.cn_market_scheduler import CNMarketSchedulerService

        service = CNMarketSchedulerService()
        db = MagicMock()
        with patch(
            "app.services.stock_selection.reliability_artifacts.refresh_stock_selection_reliability_artifacts",
            side_effect=RuntimeError("producer exploded"),
        ):
            result = service._refresh_reliability_artifacts(db, markets=["CN"])
        self.assertEqual("failed", result["status"])
        self.assertIn("producer exploded", result["error"])

    def test_us_structured_evaluation_completes_when_refresh_raises(self) -> None:
        from app.services.us_market_scheduler import USMarketSchedulerService

        service = USMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        job_repo = MagicMock()
        job_repo.has_running_job.return_value = False
        job_repo.create_job.return_value = SimpleNamespace(id=901)

        with patch(
            "app.services.us_market_scheduler.SessionLocal", return_value=context
        ), patch(
            "app.services.us_market_scheduler.DataJobRepository", return_value=job_repo
        ), patch(
            "app.services.us_market_scheduler.evaluate_model_runs",
            return_value={"status": "success", "message": "evaluated"},
        ), patch(
            "app.services.stock_selection.reliability_artifacts.refresh_stock_selection_reliability_artifacts",
            side_effect=RuntimeError("producer exploded"),
        ):
            service._run_structured_evaluation(source_job_id=42)

        completion = job_repo.complete_job.call_args.kwargs
        self.assertEqual("success", completion["status"])
        self.assertEqual("failed", completion["result"]["reliability_artifacts"]["status"])

    def test_cn_structured_evaluation_completes_when_refresh_raises(self) -> None:
        from app.services.cn_market_scheduler import CNMarketSchedulerService

        service = CNMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        job_repo = MagicMock()
        acceptance = {"status": "pending", "required_runs": 5, "passed_runs": [301]}

        with patch.object(service, "_create_stage_job", return_value=902), patch(
            "app.services.cn_market_scheduler.SessionLocal", return_value=context
        ), patch(
            "app.services.cn_market_scheduler.DataJobRepository", return_value=job_repo
        ), patch(
            "app.services.cn_market_scheduler.evaluate_model_runs",
            return_value={"status": "success", "message": "evaluated"},
        ), patch(
            "app.services.cn_market_scheduler.audit_recent_compact_dual_writes",
            return_value=acceptance,
        ), patch(
            "app.services.stock_selection.reliability_artifacts.refresh_stock_selection_reliability_artifacts",
            side_effect=RuntimeError("producer exploded"),
        ):
            service._run_structured_evaluation(source_job_id=42)

        completion = job_repo.complete_job.call_args.kwargs
        self.assertEqual("success", completion["status"])
        self.assertEqual("failed", completion["result"]["reliability_artifacts"]["status"])


class CalibrationScopeIsolationTests(unittest.TestCase):
    """A request may only ever see the calibration fit that applies to it."""

    def _persist(
        self,
        root: str,
        *,
        market: str,
        positive: tuple[float, ...] = (0.5, 0.7, 0.9),
        as_of: date = AS_OF,
        minimum_observations: int = 8,
    ) -> dict:
        return persist_reliability_artifacts(
            _matured_rows(HIGH_VERSION, positive_scores=positive),
            as_of_date=as_of,
            key_map={HIGH_VERSION: HIGH},
            calibration_model_key=HIGH,
            market=market,
            minimum_observations=minimum_observations,
            root=root,
        )

    def test_market_request_only_sees_its_own_fit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cn = self._persist(directory, market="CN", positive=(0.5, 0.7, 0.9))
            us = self._persist(directory, market="US", positive=(0.1,))
            self.assertEqual("written", cn["calibration_status"])
            self.assertEqual("written", us["calibration_status"])
            self.assertNotEqual(cn["calibration_version"], us["calibration_version"])
            self.assertNotEqual(cn["calibration_path"], us["calibration_path"])

            cn_params = inject_calibration_defaults(
                {"market": "CN", "multi_model_templates": [HIGH]}, root=directory
            )
            us_params = inject_calibration_defaults(
                {"market": "US", "multi_model_templates": [HIGH]}, root=directory
            )

            self.assertEqual("applied", cn_params["probability_calibration_status"])
            self.assertEqual("applied", us_params["probability_calibration_status"])
            self.assertEqual(
                cn["calibration_version"], cn_params["probability_calibration"]["source"]
            )
            self.assertEqual(
                us["calibration_version"], us_params["probability_calibration"]["source"]
            )
            self.assertEqual("CN", cn_params["probability_calibration"]["market"])
            self.assertEqual("US", us_params["probability_calibration"]["market"])

    def test_future_artifact_is_never_applied_to_an_earlier_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            written = self._persist(directory, market="US", as_of=date(2026, 5, 1))
            self.assertEqual("written", written["calibration_status"])

            early = inject_calibration_defaults(
                {"market": "US", "multi_model_templates": [HIGH], "as_of_date": "2026-03-10"},
                root=directory,
            )
            self.assertEqual("uncalibrated:future_artifact",
                             early["probability_calibration_status"])
            self.assertIsNone(early.get("probability_calibration"))

            applicable = inject_calibration_defaults(
                {"market": "US", "multi_model_templates": [HIGH], "as_of_date": "2026-06-01"},
                root=directory,
            )
            self.assertEqual("applied", applicable["probability_calibration_status"])

    def test_insufficient_samples_invalidates_the_previous_fit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            written = self._persist(directory, market="CN")
            self.assertEqual("written", written["calibration_status"])
            applied = inject_calibration_defaults(
                {"market": "CN", "multi_model_templates": [HIGH]}, root=directory
            )
            self.assertEqual("applied", applied["probability_calibration_status"])

            thin = self._persist(directory, market="CN", minimum_observations=10_000)
            self.assertEqual("insufficient_samples", thin["calibration_status"])

            self.assertIsNone(load_latest_calibration_artifact(directory))
            after = inject_calibration_defaults(
                {"market": "CN", "multi_model_templates": [HIGH]}, root=directory
            )
            self.assertEqual(
                "uncalibrated:invalidated:insufficient_samples",
                after["probability_calibration_status"],
            )
            self.assertIsNone(after.get("probability_calibration"))

    def test_horizon_score_field_and_model_mismatches_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._persist(directory, market="CN")

            horizon = inject_calibration_defaults(
                {"market": "CN", "multi_model_templates": [HIGH]},
                root=directory,
                horizon_days=HORIZON + 5,
            )
            self.assertEqual("uncalibrated:horizon_mismatch",
                             horizon["probability_calibration_status"])

            score = inject_calibration_defaults(
                {"market": "CN", "multi_model_templates": [HIGH]},
                root=directory,
                score_field="composite_score",
            )
            self.assertEqual("uncalibrated:score_field_mismatch",
                             score["probability_calibration_status"])

            model = inject_calibration_defaults(
                {"market": "CN", "multi_model_templates": [LOW]}, root=directory
            )
            self.assertEqual("uncalibrated:model_mismatch",
                             model["probability_calibration_status"])

            hit = inject_calibration_defaults(
                {"market": "CN", "multi_model_templates": [HIGH]},
                root=directory,
                horizon_days=HORIZON,
                score_field="model_score",
            )
            self.assertEqual("applied", hit["probability_calibration_status"])

    def test_no_artifact_reports_explicit_uncalibrated_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            params = inject_calibration_defaults(
                {"market": "CN", "multi_model_templates": [HIGH]}, root=directory
            )
            self.assertEqual("uncalibrated:no_artifact",
                             params["probability_calibration_status"])
            self.assertIsNone(params.get("probability_calibration"))


if __name__ == "__main__":
    unittest.main()
