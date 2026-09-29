from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.trainer import SignalTrainer


class TrainerPointInTimeProtocolTests(TestCase):
    def test_row_cap_keeps_complete_newest_market_date_groups(self) -> None:
        samples = [
            {"trade_date": "2026-01-06", "ticker": "B"},
            {"trade_date": "2026-01-05", "ticker": "A"},
            {"trade_date": "2026-01-06", "ticker": "A"},
            {"trade_date": "2026-01-05", "ticker": "B"},
        ]
        selected = SignalTrainer._complete_date_training_window(samples, max_rows=3)
        self.assertEqual(["A", "B"], [row["ticker"] for row in selected])
        self.assertEqual({"2026-01-06"}, {row["trade_date"] for row in selected})

    def test_legacy_composite_samples_wait_for_full_declared_horizon(self) -> None:
        start = date(2026, 7, 1)
        rows: list[dict] = []
        for index in range(12):
            close = 100.0 + index
            rows.append(
                {
                    "symbol": "TEST",
                    "date": (start + timedelta(days=index)).isoformat(),
                    "open": close - 0.5,
                    "high": close + 1.0,
                    "low": close - 1.0,
                    "close": close,
                    "volume": 1_000_000 + index * 1_000,
                }
            )

        samples = SignalTrainer()._build_lightgbm_samples(
            rows=rows,
            lookback_days=3,
            horizon_days=5,
            symbol_feature_context={"TEST": {}},
        )
        labeled = [sample for sample in samples if sample["target"] is not None]
        unlabeled = [sample for sample in samples if sample["target"] is None]

        self.assertTrue(labeled)
        self.assertTrue(unlabeled)
        for sample in labeled:
            feature_index = next(index for index, row in enumerate(rows) if row["date"] == sample["trade_date"])
            self.assertEqual(rows[feature_index + 1]["date"], sample["label_start_date"])
            self.assertEqual(rows[feature_index + 5]["date"], sample["label_end_date"])
            self.assertEqual(sample["label_end_date"], sample["label_available_date"])

        # The most recent rows remain prediction-only until the complete label
        # horizon is available; they must never enter the training pool early.
        self.assertTrue(all(sample["label_available_date"] is None for sample in unlabeled[-5:]))
