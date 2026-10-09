"""The ``model_calibration_snapshot`` producer and its read-side contract.

Connecting the producer is what removes the cross-period (5d from an old OOS
snapshot, 20d from the current train window) split in the published detail
estimates: once a fresh snapshot carrying both ``next_5d_close_return_avg`` and
``next_20d_*`` keys is persisted, ``SignalTrainer._load_oos_score_calibration``
resolves both horizons from that single snapshot.
"""

from unittest import TestCase
from unittest.mock import patch

from tests.postgres_safety import ApplicationPostgresTestCase


def _calibration_payload() -> dict:
    return {
        "markets": ["CN", "US"],
        "run_count": 3,
        "sample_count": 120,
        "latest_trade_date": "2026-09-30",
        "score_calibration_buckets": [
            {
                "score_low": -0.1,
                "score_high": 0.1,
                "score_mid": 0.0,
                "sample_count": 60,
                "source": "out_of_sample_predictions",
                "metrics": {
                    "next_3d_max_return_avg": 1.0,
                    "next_5d_close_return_avg": 2.5,
                    "next_20d_close_return_avg": 6.5,
                    "next_20d_max_drawdown_avg": -9.5,
                },
            }
        ],
    }


_TRAIN_WINDOW_FALLBACK = [
    {
        "score_low": -0.2,
        "score_high": 0.2,
        "score_mid": 0.0,
        "sample_count": 60,
        "metrics": {
            "next_5d_close_return_avg": -4.5,
            "next_20d_close_return_avg": -6.25,
            "next_20d_max_drawdown_avg": -18.3,
        },
    }
]


class ModelCalibrationSnapshotProducerTests(ApplicationPostgresTestCase):
    def _save(self, payload: dict, *, markets=None) -> dict:
        from app.services.template_evaluation import save_model_calibration_snapshot

        with patch(
            "app.services.template_evaluation.build_lightgbm_prediction_evaluation",
            return_value=payload,
        ):
            if markets is None:
                return save_model_calibration_snapshot(source_job_id=None)
            return save_model_calibration_snapshot(markets=markets, source_job_id=None)

    def test_producer_persists_snapshot_under_expected_type_and_date(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import WorkspaceSnapshotRepository

        result = self._save(_calibration_payload())
        self.assertEqual("success", result["status"])
        self.assertEqual("2026-09-30", result["snapshot_date"])
        self.assertEqual(1, result["bucket_count"])

        with SessionLocal() as db:
            snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(
                "model_calibration_snapshot"
            )
        self.assertIsNotNone(snapshot)
        self.assertEqual("model_calibration_snapshot", snapshot["snapshot_type"])
        self.assertEqual("2026-09-30", snapshot["snapshot_date"])
        self.assertEqual(result["snapshot_id"], snapshot["id"])
        metrics = snapshot["payload"]["score_calibration_buckets"][0]["metrics"]
        self.assertEqual(2.5, metrics["next_5d_close_return_avg"])
        self.assertEqual(6.5, metrics["next_20d_close_return_avg"])

    def test_reader_hits_fresh_snapshot_and_resolves_5d_20d_from_one_source(self) -> None:
        from app.services.trainer import SignalTrainer

        self._save(_calibration_payload())

        trainer = SignalTrainer()
        oos_buckets, meta = trainer._load_oos_score_calibration(market="CN")
        self.assertEqual("model_calibration_snapshot", meta["source"])
        self.assertEqual("2026-09-30", meta["snapshot_date"])
        self.assertTrue(oos_buckets)

        metrics, sources = trainer._resolve_detail_estimate_metrics(
            score=0.0,
            primary_buckets=oos_buckets,
            fallback_buckets=_TRAIN_WINDOW_FALLBACK,
        )
        assert metrics is not None
        # The fallback carries different values for the *same* keys; if the
        # snapshot were only partially used (the old cross-period bug) one of
        # these would come from the train window instead.
        self.assertEqual(2.5, metrics["next_5d_close_return_avg"])
        self.assertEqual(6.5, metrics["next_20d_close_return_avg"])
        self.assertEqual(-9.5, metrics["next_20d_max_drawdown_avg"])
        self.assertEqual("model_calibration_snapshot", sources["next_5d_close_return_avg"])
        self.assertEqual("model_calibration_snapshot", sources["next_20d_close_return_avg"])

    def test_us_market_reader_hits_combined_snapshot(self) -> None:
        from app.services.trainer import SignalTrainer

        self._save(_calibration_payload())
        oos_buckets, meta = SignalTrainer()._load_oos_score_calibration(market="US")
        self.assertEqual("model_calibration_snapshot", meta["source"])
        self.assertTrue(oos_buckets)

    def test_subset_snapshot_is_rejected_for_other_market(self) -> None:
        from app.services.trainer import SignalTrainer

        payload = _calibration_payload()
        payload["markets"] = ["CN"]
        self._save(payload, markets=["CN"])
        _buckets, meta = SignalTrainer()._load_oos_score_calibration(market="US")
        self.assertEqual("market_mismatch", meta["source"])

    def test_empty_evaluation_does_not_clobber_existing_snapshot(self) -> None:
        from app.services.trainer import SignalTrainer

        good = self._save(_calibration_payload())
        empty = {
            "markets": ["CN", "US"],
            "run_count": 0,
            "sample_count": 0,
            "latest_trade_date": None,
            "score_calibration_buckets": [],
        }
        result = self._save(empty)
        self.assertEqual("empty", result["status"])
        self.assertIsNone(result["snapshot_id"])

        oos_buckets, meta = SignalTrainer()._load_oos_score_calibration(market="CN")
        self.assertEqual("model_calibration_snapshot", meta["source"])
        self.assertEqual(good["snapshot_id"], meta["snapshot_id"])
        self.assertTrue(oos_buckets)


class ModelCalibrationSnapshotRouteTests(TestCase):
    def test_job_endpoint_is_registered(self) -> None:
        from app.api.main import app

        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/jobs/model-calibration-snapshot", paths)
