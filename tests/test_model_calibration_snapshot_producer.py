"""The ``model_calibration_snapshot`` producer and its read-side contract.

Snapshots are partitioned by market: the producer writes one row per requested
market, each payload carrying a singular ``market`` key, and the trainer reads
only the partition stamped with the market it is training. That removes the old
combined ``markets`` fallback where one row's 5d/20d keys could come from a
different period/market mix. Pre-partition rows (a ``markets`` list only) stay
readable through an explicit, auditable ``legacy_merged`` path.
"""

from unittest import TestCase
from unittest.mock import patch

from tests.postgres_safety import ApplicationPostgresTestCase

SNAPSHOT_TYPE = "model_calibration_snapshot"


def _calibration_payload(
    *, market: str = "CN", sample_count: int = 120, five: float = 2.5, twenty: float = 6.5
) -> dict:
    return {
        "markets": [market],
        "run_count": 3,
        "sample_count": sample_count,
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
                    "next_5d_close_return_avg": five,
                    "next_20d_close_return_avg": twenty,
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
    def _save(self, *, payloads: dict[str, dict] | None = None, markets=None) -> dict:
        """Save via a producer whose per-market evaluation is stubbed.

        ``payloads`` maps market -> evaluation payload; a missing market yields
        an empty (bucket-less) evaluation so a partition is simply not written.
        """
        from app.services.template_evaluation import save_model_calibration_snapshot

        if payloads is None:
            payloads = {"CN": _calibration_payload(market="CN"), "US": _calibration_payload(market="US")}

        def _evaluation(*, market, recent_runs=8, top_n=40, allow_compute=True):
            return payloads.get(market, {"markets": [market], "latest_trade_date": None, "score_calibration_buckets": []})

        with patch(
            "app.services.template_evaluation.build_lightgbm_prediction_evaluation",
            side_effect=_evaluation,
        ):
            if markets is None:
                return save_model_calibration_snapshot(source_job_id=None)
            return save_model_calibration_snapshot(markets=markets, source_job_id=None)

    def _insert_legacy_snapshot(self, markets: list[str]) -> dict:
        """Backdate a combined pre-partition snapshot (no singular ``market``)."""
        from app.core.db import SessionLocal
        from app.services.repository import WorkspaceSnapshotRepository

        payload = {
            "markets": markets,
            "run_count": 2,
            "sample_count": 90,
            "latest_trade_date": "2026-09-15",
            "score_calibration_buckets": [
                {
                    "score_low": -0.1,
                    "score_high": 0.1,
                    "score_mid": 0.0,
                    "sample_count": 40,
                    "metrics": {"next_5d_close_return_avg": 7.0, "next_20d_close_return_avg": 8.0},
                }
            ],
        }
        with SessionLocal() as db:
            row = WorkspaceSnapshotRepository(db).create_snapshot(
                snapshot_type=SNAPSHOT_TYPE,
                snapshot_date="2026-09-15",
                payload=payload,
            )
        return {"id": row.id, "snapshot_date": row.snapshot_date}

    def test_producer_persists_one_partitioned_snapshot_per_market(self) -> None:
        result = self._save()
        self.assertEqual("success", result["status"])
        self.assertEqual(["CN", "US"], result["written_markets"])
        self.assertEqual("2026-09-30", result["snapshot_date"])
        self.assertEqual(2, result["bucket_count"])
        self.assertEqual({"CN", "US"}, set(result["per_market"]))

        from app.core.db import SessionLocal
        from app.services.repository import WorkspaceSnapshotRepository

        with SessionLocal() as db:
            repo = WorkspaceSnapshotRepository(db)
            cn = repo.get_latest_snapshot_for_market(SNAPSHOT_TYPE, "CN")
            us = repo.get_latest_snapshot_for_market(SNAPSHOT_TYPE, "US")
        self.assertIsNotNone(cn)
        self.assertIsNotNone(us)
        self.assertNotEqual(cn["id"], us["id"])
        self.assertEqual("CN", cn["payload"]["market"])
        self.assertEqual(["CN"], cn["payload"]["markets"])
        self.assertEqual("US", us["payload"]["market"])

    def test_reader_resolves_5d_20d_from_its_own_partition(self) -> None:
        from app.services.trainer import SignalTrainer

        self._save(
            payloads={
                "CN": _calibration_payload(market="CN", five=2.5, twenty=6.5),
                "US": _calibration_payload(market="US", five=22.2, twenty=26.6),
            }
        )

        cn_buckets, cn_meta = SignalTrainer()._load_oos_score_calibration(market="CN")
        us_buckets, us_meta = SignalTrainer()._load_oos_score_calibration(market="US")
        self.assertEqual("model_calibration_snapshot", cn_meta["source"])
        self.assertEqual("market_partition", cn_meta["match"])
        self.assertFalse(cn_meta["legacy_merged"])
        self.assertEqual("2026-09-30", cn_meta["snapshot_date"])
        self.assertNotEqual(cn_meta["snapshot_id"], us_meta["snapshot_id"])

        cn_metrics, cn_sources = SignalTrainer()._resolve_detail_estimate_metrics(
            score=0.0, primary_buckets=cn_buckets, fallback_buckets=_TRAIN_WINDOW_FALLBACK
        )
        us_metrics, _ = SignalTrainer()._resolve_detail_estimate_metrics(
            score=0.0, primary_buckets=us_buckets, fallback_buckets=_TRAIN_WINDOW_FALLBACK
        )
        assert cn_metrics is not None and us_metrics is not None
        # The CN reader must never surface the US partition's numbers.
        self.assertEqual(2.5, cn_metrics["next_5d_close_return_avg"])
        self.assertEqual(6.5, cn_metrics["next_20d_close_return_avg"])
        self.assertEqual(22.2, us_metrics["next_5d_close_return_avg"])
        self.assertEqual("model_calibration_snapshot", cn_sources["next_5d_close_return_avg"])

    def test_market_without_a_partition_is_uncalibrated(self) -> None:
        from app.services.trainer import SignalTrainer

        self._save(markets=["US"], payloads={"US": _calibration_payload(market="US")})
        _buckets, meta = SignalTrainer()._load_oos_score_calibration(market="CN")
        self.assertEqual("uncalibrated", meta["source"])

    def test_legacy_merged_snapshot_remains_readable_and_flagged(self) -> None:
        from app.services.trainer import SignalTrainer

        legacy = self._insert_legacy_snapshot(["CN", "US"])
        buckets, meta = SignalTrainer()._load_oos_score_calibration(market="CN")
        self.assertEqual("model_calibration_snapshot", meta["source"])
        self.assertEqual("legacy_merged", meta["match"])
        self.assertTrue(meta["legacy_merged"])
        self.assertEqual(legacy["id"], meta["snapshot_id"])
        self.assertTrue(buckets)
        _us_buckets, us_meta = SignalTrainer()._load_oos_score_calibration(market="US")
        self.assertTrue(us_meta["legacy_merged"])

    def test_legacy_snapshot_rejects_a_market_it_never_covered(self) -> None:
        from app.services.trainer import SignalTrainer

        self._insert_legacy_snapshot(["CN"])
        _buckets, meta = SignalTrainer()._load_oos_score_calibration(market="US")
        self.assertEqual("market_mismatch", meta["source"])
        self.assertTrue(meta["legacy_merged"])
        self.assertEqual(["CN"], meta["payload_markets"])

    def test_same_day_rerun_reuses_the_partition_instead_of_duplicating(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import WorkspaceSnapshotRepository

        first = self._save(markets=["CN"], payloads={"CN": _calibration_payload(market="CN")})
        self.assertEqual("success", first["status"])
        second = self._save(markets=["CN"], payloads={"CN": _calibration_payload(market="CN")})
        self.assertEqual("exists", second["status"])
        self.assertEqual(["CN"], second["reused_markets"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])

        with SessionLocal() as db:
            rows = WorkspaceSnapshotRepository(db).list_snapshots(SNAPSHOT_TYPE, limit=50)
        cn_rows = [row for row in rows if str((row["payload"] or {}).get("market")) == "CN"]
        self.assertEqual(1, len(cn_rows))

    def test_empty_evaluation_does_not_clobber_existing_partition(self) -> None:
        from app.services.trainer import SignalTrainer

        good = self._save(markets=["CN"], payloads={"CN": _calibration_payload(market="CN")})
        empty = self._save(markets=["CN"], payloads={})
        self.assertEqual("empty", empty["status"])
        self.assertIsNone(empty["snapshot_id"])

        oos_buckets, meta = SignalTrainer()._load_oos_score_calibration(market="CN")
        self.assertEqual("model_calibration_snapshot", meta["source"])
        self.assertEqual(good["snapshot_id"], meta["snapshot_id"])
        self.assertTrue(oos_buckets)


class ModelCalibrationSnapshotRouteTests(TestCase):
    def test_job_endpoint_is_registered(self) -> None:
        from app.api.main import app

        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/jobs/model-calibration-snapshot", paths)
