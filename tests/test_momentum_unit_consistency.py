"""S-4: momentum is percent at every delivery boundary, with an explicit unit."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.insight_engine import InsightEngine
from app.services.market_lake import screen_lake_momentum
from app.services.workspace_snapshots import _market_heatmap_fallback_label


def _history() -> list[dict]:
    rows = []
    closes = [100.0 for _ in range(30)]
    closes[-1] = 105.0
    for index, close in enumerate(closes):
        rows.append(
            {
                "date": f"2026-08-{index + 1:02d}",
                "open": close - 0.2,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 1_000_000 + index * 1_000,
            }
        )
    return rows


class LakeMomentumUnitTests(unittest.TestCase):
    def test_lake_momentum_is_emitted_as_percent_with_unit_field(self) -> None:
        columns = [
            "date",
            "symbol",
            "close",
            "volume",
            "dollar_volume",
            "momentum_5",
            "momentum_20",
            "volume_ratio",
            "ma20",
            "ma60",
            "pullback_depth_pct",
            "distance_to_breakout_pct",
        ]
        rows = [
            ("2026-10-02", "AAA", 10.0, 1_000_000, 10_000_000.0, 0.03, 0.12, 1.2, 9.5, 9.0, 2.0, -1.0),
        ]
        with (
            patch("app.services.market_lake._recent_parquet_files", return_value=["dummy.parquet"]),
            patch("app.services.market_lake._duckdb_fetchall", return_value=(rows, columns)),
        ):
            result = screen_lake_momentum(market="US", limit=5)

        self.assertEqual(1, len(result))
        row = result[0]
        self.assertEqual("percent", row["momentum_units"])
        # 0.03 fraction -> 3.0 percent; a consumer that renders "%" is now correct.
        self.assertAlmostEqual(3.0, row["momentum_5"])
        self.assertAlmostEqual(12.0, row["momentum_20"])
        # The old defect produced sub-1 values for ordinary moves; percent must
        # be able to exceed 1.0 for a 3% move.
        self.assertGreater(abs(row["momentum_5"]), 1.0)

    def test_heatmap_label_thresholds_use_percent_units(self) -> None:
        # momentum_5 == 3.0 is a 3% move: under the old fraction thresholds it
        # looked like 300% and was mislabelled "短线加速".
        label = _market_heatmap_fallback_label(
            {"market": "CN", "momentum_5": 3.0, "momentum_20": 5.0, "volume_ratio": 1.0},
            "technical_momentum",
        )
        self.assertEqual("技术动量", label)

        label_us = _market_heatmap_fallback_label(
            {"market": "US", "momentum_5": 3.0, "momentum_20": 50.0, "volume_ratio": 1.0},
            "technical_momentum",
        )
        self.assertEqual("高弹性趋势", label_us)


class InsightMomentumUnitTests(unittest.TestCase):
    def test_insight_declares_percent_and_scales_by_100(self) -> None:
        engine = InsightEngine()
        history = _history()
        engine.symbol_data.get_history = lambda ticker, limit=180: history  # type: ignore[method-assign]

        insight = engine.get_insight("600000.SH", lang="en")

        self.assertIsNotNone(insight)
        assert insight is not None
        self.assertEqual("percent", insight["momentum_units"])
        # Last close 105 vs close 5 sessions back 100 -> +5%.
        self.assertAlmostEqual(5.0, insight["momentum_5"], places=2)


if __name__ == "__main__":
    unittest.main()
