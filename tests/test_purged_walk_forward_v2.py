from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest import TestCase

from app.services.model_evaluation import _strict_oos_status
from app.services.stock_selection.schemas import LabeledSample
from app.services.stock_selection.walk_forward import PointInTimeTrainingPool, audit_training_samples


def _sample(
    identifier: str,
    trading_dates: list[date],
    *,
    feature_index: int,
    available_index: int,
    horizon_days: int = 3,
) -> LabeledSample:
    return LabeledSample(
        sample_id=identifier,
        market="CN",
        ticker=identifier,
        feature_date=trading_dates[feature_index],
        label_start_date=trading_dates[feature_index + 1],
        label_end_date=trading_dates[available_index],
        label_available_date=trading_dates[available_index],
        horizon_days=horizon_days,
        label_value=0.05,
        features={"momentum": 0.1},
        dataset_version="fixture-v1",
    )


class PurgedWalkForwardV2Tests(TestCase):
    def setUp(self) -> None:
        start = date(2026, 7, 1)
        self.trading_dates = [start + timedelta(days=index) for index in range(10)]

    def _pool(self, samples: list[LabeledSample]) -> PointInTimeTrainingPool[LabeledSample]:
        return PointInTimeTrainingPool(
            samples,
            trading_dates=self.trading_dates,
            feature_date=lambda item: item.feature_date,
            label_end_date=lambda item: item.label_end_date,
            label_available_date=lambda item: item.label_available_date,
            sample_id=lambda item: item.sample_id,
            purge_sessions=3,
            embargo_sessions=0,
        )

    def test_releases_label_strictly_after_availability_and_purge(self) -> None:
        first = _sample("first", self.trading_dates, feature_index=0, available_index=3)
        second = _sample("second", self.trading_dates, feature_index=1, available_index=4)
        pool = self._pool([first, second])

        on_day_3 = pool.advance(self.trading_dates[3])
        on_day_4 = pool.advance(self.trading_dates[4])
        on_day_5 = pool.advance(self.trading_dates[5])

        self.assertEqual((), on_day_3)
        self.assertEqual((first,), on_day_4)
        self.assertEqual((first, second), on_day_5)

    def test_future_extreme_sample_cannot_change_current_training_pool(self) -> None:
        mature = _sample("mature", self.trading_dates, feature_index=0, available_index=3)
        future_extreme = _sample("future-extreme", self.trading_dates, feature_index=4, available_index=7)
        pool = self._pool([mature, future_extreme])

        current = pool.advance(self.trading_dates[5])
        self.assertEqual((mature,), current)
        self.assertNotIn(future_extreme, current)

    def test_audit_reports_unavailable_and_purge_violations(self) -> None:
        valid = _sample("valid", self.trading_dates, feature_index=0, available_index=3)
        leaking = _sample("leaking", self.trading_dates, feature_index=2, available_index=5)
        audit = audit_training_samples(
            [valid, leaking],
            prediction_date=self.trading_dates[5],
            trading_dates=self.trading_dates,
            purge_sessions=3,
        )
        reasons = {violation.reason for violation in audit.violations if violation.sample_id == "leaking"}
        self.assertFalse(audit.passed)
        self.assertIn("label_not_available", reasons)
        self.assertIn("label_window_not_ended", reasons)
        self.assertIn("purge_or_embargo_violation", reasons)

    def test_model_evaluation_recognizes_v2_protocol(self) -> None:
        run = SimpleNamespace(
            config_json=(
                '{"evaluation_protocol":"walk_forward_purged_v2",'
                '"oos_start_date":"2026-07-01","purge_gap_days":5}'
            ),
            test_start=None,
            train_end="2026-06-30",
        )
        is_oos, protocol, purge = _strict_oos_status(run, trade_date="2026-07-02")
        self.assertTrue(is_oos)
        self.assertEqual("walk_forward_purged_v2", protocol)
        self.assertEqual(5, purge)
