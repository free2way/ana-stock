"""Key-aware detail-estimate source selection.

An out-of-sample ``model_calibration_snapshot`` produced before a horizon
landed only carries the keys it knew (the id=8997 CN snapshot has 1/3/5-day and
open-to-* keys but no ``next_20d_*`` keys). Publishing therefore resolves each
metric key independently: a key missing from the snapshot falls back to the
run's own matured train-window buckets for the same score band, and a key
present in neither stays ``None``. A key is never aliased from another horizon
and never silently zero-filled.
"""

from __future__ import annotations

from unittest import TestCase

from app.services.trainer import SignalTrainer


def _bucket(score_low: float, score_high: float, metrics: dict) -> dict:
    return {
        "score_low": score_low,
        "score_high": score_high,
        "score_mid": round((score_low + score_high) / 2, 6),
        "sample_count": 60,
        "metrics": dict(metrics),
    }


# Mirrors snapshot 8997: no 20-day keys at all.
_OOS_SNAPSHOT_METRICS = {
    "next_3d_max_return_avg": 3.6703,
    "next_5d_close_return_avg": -4.5,
    "next_5d_max_return_avg": 3.6703,
    "next_5d_max_drawdown_avg": -6.22,
    "next_open_to_high_avg": 2.1586,
    "tradable_next_day_rate": 100.0,
}
_TRAIN_WINDOW_METRICS = {
    "next_3d_max_return_avg": 1.0747,
    "next_5d_close_return_avg": 1.2,
    "next_20d_close_return_avg": -6.2522,
    "next_20d_max_drawdown_avg": -18.3011,
}


class DetailEstimateSourceTests(TestCase):
    def setUp(self) -> None:
        self.trainer = SignalTrainer()
        self.primary = [_bucket(-0.1, 0.1, _OOS_SNAPSHOT_METRICS)]
        self.fallback = [_bucket(-0.2, 0.2, _TRAIN_WINDOW_METRICS)]

    def test_missing_keys_fall_back_per_key(self) -> None:
        metrics, sources = self.trainer._resolve_detail_estimate_metrics(
            score=0.004,
            primary_buckets=self.primary,
            fallback_buckets=self.fallback,
        )
        assert metrics is not None
        # The snapshot carries 5d, so 5d is NOT taken from the fallback.
        self.assertEqual(-4.5, metrics["next_5d_close_return_avg"])
        self.assertEqual("model_calibration_snapshot", sources["next_5d_close_return_avg"])
        # The snapshot lacks 20d, so 20d comes from the train window.
        self.assertEqual(-6.2522, metrics["next_20d_close_return_avg"])
        self.assertEqual(-18.3011, metrics["next_20d_max_drawdown_avg"])
        self.assertEqual("train_window_calibration", sources["next_20d_close_return_avg"])
        self.assertEqual("train_window_calibration", sources["next_20d_max_drawdown_avg"])

    def test_detail_row_publishes_20d_alongside_5d(self) -> None:
        metrics, _sources = self.trainer._resolve_detail_estimate_metrics(
            score=0.004,
            primary_buckets=self.primary,
            fallback_buckets=self.fallback,
        )
        detail = self.trainer._build_detail_row(
            symbol_id=1,
            trade_date="2026-10-08",
            score=0.004,
            rank_value=1,
            universe_size=100,
            horizon_days=5,
            run_name="fixture",
            calibrated_metrics=metrics,
        )
        self.assertIsNotNone(detail["expected_return_5d"])
        self.assertIsNotNone(detail["expected_return_20d"])
        self.assertEqual(-6.2522, detail["expected_return_20d"])
        # Drawdown is published as a magnitude.
        self.assertEqual(18.3011, detail["expected_drawdown_20d"])

    def test_complete_primary_is_unchanged(self) -> None:
        complete = {
            "next_3d_max_return_avg": 2.0,
            "next_5d_close_return_avg": 2.5,
            "next_20d_close_return_avg": 3.5,
            "next_20d_max_drawdown_avg": -4.5,
        }
        metrics, sources = self.trainer._resolve_detail_estimate_metrics(
            score=0.004,
            primary_buckets=[_bucket(-0.1, 0.1, complete)],
            fallback_buckets=self.fallback,
        )
        assert metrics is not None
        for key, value in complete.items():
            self.assertEqual(value, metrics[key])
            self.assertEqual("model_calibration_snapshot", sources[key])

    def test_key_absent_from_both_sources_stays_null(self) -> None:
        # Neither source has 20d; a present 5d must not be reused as a 20d
        # estimate, and the field must not be silently zero-filled.
        metrics, sources = self.trainer._resolve_detail_estimate_metrics(
            score=0.004,
            primary_buckets=[_bucket(-0.1, 0.1, {"next_5d_close_return_avg": -4.5})],
            fallback_buckets=[_bucket(-0.2, 0.2, {"next_5d_close_return_avg": 1.2})],
        )
        assert metrics is not None
        self.assertIsNone(metrics["next_20d_close_return_avg"])
        self.assertIsNone(metrics["next_20d_max_drawdown_avg"])
        self.assertEqual("unavailable", sources["next_20d_close_return_avg"])
        self.assertEqual("unavailable", sources["next_20d_max_drawdown_avg"])

    def test_no_source_at_all_returns_none(self) -> None:
        metrics, sources = self.trainer._resolve_detail_estimate_metrics(
            score=0.004,
            primary_buckets=[],
            fallback_buckets=[],
        )
        self.assertIsNone(metrics)
        self.assertTrue(all(value == "unavailable" for value in sources.values()))


class SnapshotProducerContractTests(TestCase):
    """The out-of-sample snapshot producer must emit the 20-day keys.

    The id=8997 snapshot (2026-06-26) predates the 20-day horizon and carries
    none, which is why consumers need per-key fallback. This pins the producer
    contract so a freshly persisted snapshot does carry them.
    """

    def test_aggregate_emits_20d_keys_from_matured_history(self) -> None:
        from app.services.template_evaluation import aggregate_lightgbm_score_calibration

        records = [
            {
                "score": 0.01,
                "return_3d": 0.5,
                "return_5d": 1.0,
                "return_20d": 2.5,
                "drawdown_20d_pct": -7.5,
                "max_3d_high_return_pct": 1.5,
                "max_3d_drawdown_pct": -3.0,
            }
        ]
        buckets = aggregate_lightgbm_score_calibration(records)
        self.assertTrue(buckets)
        metrics = buckets[0]["metrics"]
        self.assertEqual(2.5, metrics["next_20d_close_return_avg"])
        self.assertEqual(-7.5, metrics["next_20d_max_drawdown_avg"])

    def test_aggregate_never_aliases_20d_from_shorter_horizon(self) -> None:
        from app.services.template_evaluation import aggregate_lightgbm_score_calibration

        records = [
            {
                "score": 0.01,
                "return_3d": 0.5,
                "return_5d": 1.0,
                "return_20d": None,
                "drawdown_20d_pct": None,
                "max_3d_high_return_pct": 1.5,
                "max_3d_drawdown_pct": -3.0,
            }
        ]
        metrics = aggregate_lightgbm_score_calibration(records)[0]["metrics"]
        self.assertIsNone(metrics["next_20d_close_return_avg"])
        self.assertIsNone(metrics["next_20d_max_drawdown_avg"])
        # The shorter horizons are still published honestly.
        self.assertEqual(1.0, metrics["next_5d_close_return_avg"])
