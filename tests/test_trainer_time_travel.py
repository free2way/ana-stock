from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from unittest import TestCase

from app.services.trainer import SignalTrainer


def _price_rows() -> list[dict]:
    """30 sessions with a completed limit-up (index 9) and a signal-day limit-up (index 20)."""

    start = date(2026, 1, 5)
    rows: list[dict] = []
    for index in range(30):
        close = 10.0 + 0.1 * index
        rows.append(
            {
                "symbol": "TEST",
                "date": (start + timedelta(days=index)).isoformat(),
                "open": close - 0.02,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 1_000_000 + index * 1_000,
            }
        )

    def limit_up(index: int, factor: float) -> None:
        previous = rows[index - 1]["close"]
        rows[index]["open"] = previous
        rows[index]["close"] = previous * factor
        rows[index]["high"] = rows[index]["close"] + 0.1
        rows[index]["low"] = previous - 0.1

    # Completed historical limit-up: next session open->close = +2.0%.
    limit_up(9, 1.10)
    rows[10]["open"] = rows[9]["close"]
    rows[10]["close"] = rows[9]["close"] * 1.02
    rows[10]["high"] = rows[10]["close"] + 0.1
    rows[10]["low"] = rows[10]["open"] - 0.1

    # Signal-day limit-up (index 20): its next session belongs to the label window.
    limit_up(20, 1.10)
    rows[21]["open"] = rows[20]["close"]
    rows[21]["close"] = rows[20]["close"] * 1.03
    rows[21]["high"] = rows[21]["close"] + 0.1
    rows[21]["low"] = rows[21]["open"] - 0.1
    return rows


class TrainerTimeTravelTests(TestCase):
    def _feature(self, rows: list[dict], trade_date: str, name: str) -> float:
        samples = SignalTrainer()._build_lightgbm_samples(
            rows=rows,
            lookback_days=3,
            horizon_days=5,
            symbol_feature_context={"TEST": {"limit_band_pct": 10.0}},
            market="CN",
        )
        sample = next(item for item in samples if item["trade_date"] == trade_date)
        return float(sample["features"][name])

    def test_signal_day_limit_up_follow_through_never_reads_the_future(self) -> None:
        rows = _price_rows()
        signal_date = rows[20]["date"]

        # Only the completed limit-up (index 9) contributes: +2.0% * 5 = 0.1.
        self.assertAlmostEqual(
            self._feature(rows, signal_date, "limit_up_next_day_open_to_close_avg"), 0.1, places=9
        )
        # Count/recency intentionally include the signal day itself.
        self.assertAlmostEqual(self._feature(rows, signal_date, "limit_up_count_20d_norm"), 0.4, places=9)
        self.assertAlmostEqual(self._feature(rows, signal_date, "days_since_limit_up_norm"), 0.0, places=9)

        mutated = deepcopy(rows)
        mutated[21]["open"] = 5.0
        mutated[21]["close"] = 100.0
        mutated[21]["high"] = 100.0
        mutated[21]["low"] = 5.0
        # A future session must not move any signal-date feature.
        self.assertAlmostEqual(
            self._feature(mutated, signal_date, "limit_up_next_day_open_to_close_avg"), 0.1, places=9
        )

    def test_historical_limit_up_follow_through_still_uses_completed_sessions(self) -> None:
        rows = _price_rows()
        # At index 10 the completed probe 9 is already observable and must count.
        self.assertAlmostEqual(
            self._feature(rows, rows[10]["date"], "limit_up_next_day_open_to_close_avg"), 0.1, places=9
        )

    def _concept_feature(self, rows: list[dict], trade_date: str, name: str, concept_history: list[dict]) -> float:
        samples = SignalTrainer()._build_lightgbm_samples(
            rows=rows,
            lookback_days=3,
            horizon_days=5,
            symbol_feature_context={
                "TEST": {"limit_band_pct": 10.0, "concept_history": concept_history}
            },
            market="CN",
        )
        sample = next(item for item in samples if item["trade_date"] == trade_date)
        return float(sample["features"][name])

    def test_concept_features_use_only_snapshots_visible_on_the_signal_day(self) -> None:
        rows = _price_rows()
        signal_date = rows[20]["date"]
        visible = {"as_of_date": rows[5]["date"], "concept_count": 2.0, "max_strength": 40.0}
        future = {"as_of_date": rows[25]["date"], "concept_count": 8.0, "max_strength": 90.0}

        # Only the snapshot dated on/before the signal day may feed the features.
        self.assertAlmostEqual(
            self._concept_feature(rows, signal_date, "concept_count_norm", [visible, future]), 2.0 / 8.0, places=9
        )
        self.assertAlmostEqual(
            self._concept_feature(rows, signal_date, "concept_strength_norm", [visible, future]), 40.0 / 100.0, places=9
        )

        # Perturbing the future snapshot must not move any signal-day feature.
        perturbed = [visible, {**future, "concept_count": 1.0, "max_strength": 1.0}]
        self.assertAlmostEqual(
            self._concept_feature(rows, signal_date, "concept_count_norm", perturbed), 2.0 / 8.0, places=9
        )
        self.assertAlmostEqual(
            self._concept_feature(rows, signal_date, "concept_strength_norm", perturbed), 40.0 / 100.0, places=9
        )

    def test_future_fundamental_publication_does_not_change_signal_day_features(self) -> None:
        rows = _price_rows()
        signal_date = rows[20]["date"]
        visible = {
            "period_end": "2026-01-10",
            "available_time": f"{rows[5]['date']}T00:00:00+00:00",
            "revenue": 100.0,
        }
        future = {
            "period_end": "2026-02-10",
            "available_time": f"{rows[25]['date']}T00:00:00+00:00",
            "revenue": 999.0,
        }
        without_future = SignalTrainer()._build_lightgbm_samples(
            rows=rows,
            lookback_days=3,
            horizon_days=5,
            symbol_feature_context={"TEST": {"limit_band_pct": 10.0, "fundamental_history": [visible]}},
            market="CN",
        )
        with_future = SignalTrainer()._build_lightgbm_samples(
            rows=rows,
            lookback_days=3,
            horizon_days=5,
            symbol_feature_context={"TEST": {"limit_band_pct": 10.0, "fundamental_history": [visible, future]}},
            market="CN",
        )
        first = next(item for item in without_future if item["trade_date"] == signal_date)
        second = next(item for item in with_future if item["trade_date"] == signal_date)
        self.assertEqual(first["features"], second["features"])
