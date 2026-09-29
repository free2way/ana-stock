from __future__ import annotations

import unittest

from app.services.stock_selection.forward_shadow_evaluation import (
    CNForwardShadowEvaluationConfig,
    build_cn_forward_shadow_evaluation,
)


def _snapshot(*, feature_date: str, effective_date: str, tickers: tuple[str, ...]) -> dict:
    return {
        "feature_date": feature_date,
        "effective_trade_date": effective_date,
        "horizon_days": 5,
        "top_observations": [
            {
                "ticker": ticker,
                "cross_sectional_rank": 1.0 - index / max(1, len(tickers)),
            }
            for index, ticker in enumerate(tickers)
        ],
    }


def _prices(ticker: str, values: tuple[tuple[str, float, float], ...]) -> list[dict]:
    return [
        {"symbol": ticker, "date": trade_date, "open": open_price, "close": close_price}
        for trade_date, open_price, close_price in values
    ]


class CNForwardShadowEvaluationTests(unittest.TestCase):
    def test_evaluates_only_matured_snapshots_with_cost(self) -> None:
        snapshots = [
            _snapshot(
                feature_date="2026-08-21",
                effective_date="2026-08-24",
                tickers=("AAA", "BBB"),
            ),
            _snapshot(
                feature_date="2026-08-28",
                effective_date="2026-08-31",
                tickers=("AAA",),
            ),
        ]
        rows = [
            *_prices(
                "AAA",
                (
                    ("2026-08-24", 10.0, 10.1),
                    ("2026-08-25", 10.1, 10.2),
                    ("2026-08-26", 10.2, 10.3),
                    ("2026-08-27", 10.3, 10.4),
                    ("2026-08-28", 10.4, 11.0),
                ),
            ),
            *_prices(
                "BBB",
                (
                    ("2026-08-24", 20.0, 20.0),
                    ("2026-08-25", 20.0, 20.0),
                    ("2026-08-26", 20.0, 20.0),
                    ("2026-08-27", 20.0, 20.0),
                    ("2026-08-28", 20.0, 19.0),
                ),
            ),
        ]

        result = build_cn_forward_shadow_evaluation(
            snapshots,
            rows,
            as_of_date="2026-08-28",
            config=CNForwardShadowEvaluationConfig(top_ns=(1, 2)),
        )

        self.assertEqual(1, result["evaluated_date_count"])
        self.assertEqual(1, result["pending_date_count"])
        self.assertAlmostEqual(0.096, result["aggregate_top_n"]["1"]["mean_daily_net_return"])
        self.assertAlmostEqual(0.021, result["aggregate_top_n"]["2"]["mean_daily_net_return"])
        self.assertEqual(0.5, result["aggregate_top_n"]["2"]["stock_signal_win_rate"])
        self.assertEqual(1.0, result["aggregate_top_n"]["2"]["positive_date_rate"])
        self.assertEqual("BLOCKED", result["promotion_status"])

    def test_excludes_reverse_split_like_path(self) -> None:
        snapshot = _snapshot(
            feature_date="2026-08-21",
            effective_date="2026-08-24",
            tickers=("JUMP",),
        )
        rows = _prices(
            "JUMP",
            (
                ("2026-08-24", 1.0, 1.0),
                ("2026-08-25", 1.0, 10.0),
                ("2026-08-26", 10.0, 10.0),
                ("2026-08-27", 10.0, 10.0),
                ("2026-08-28", 10.0, 10.0),
            ),
        )

        result = build_cn_forward_shadow_evaluation(
            [snapshot],
            rows,
            as_of_date="2026-08-28",
        )

        self.assertEqual(0, result["evaluated_date_count"])
        self.assertEqual(1, result["excluded_date_count"])
        self.assertEqual("no_complete_tradable_outcomes", result["excluded_dates"][0]["reason"])

    def test_first_snapshot_for_effective_date_is_immutable(self) -> None:
        first = _snapshot(
            feature_date="2026-08-21",
            effective_date="2026-08-24",
            tickers=("AAA",),
        )
        duplicate = _snapshot(
            feature_date="2026-08-21",
            effective_date="2026-08-24",
            tickers=("BBB",),
        )
        rows = _prices(
            "AAA",
            (
                ("2026-08-24", 10.0, 10.0),
                ("2026-08-25", 10.0, 10.0),
                ("2026-08-26", 10.0, 10.0),
                ("2026-08-27", 10.0, 10.0),
                ("2026-08-28", 10.0, 10.0),
            ),
        )

        result = build_cn_forward_shadow_evaluation(
            [first, duplicate],
            rows,
            as_of_date="2026-08-28",
            config=CNForwardShadowEvaluationConfig(top_ns=(1,)),
        )

        self.assertEqual(1, result["source_snapshot_count"])
        self.assertEqual(1, result["evaluated_date_count"])

    def test_missing_or_invalid_first_member_is_never_replaced(self):
        for first in ({"ticker": "MISSING"}, {}, {"ticker": "BBB"}):
            snapshot = _snapshot(feature_date="2026-08-21", effective_date="2026-08-24", tickers=())
            snapshot["top_observations"] = [first, {"ticker": "BBB"}]
            rows = _prices("BBB", (("2026-08-24", 10, 10), ("2026-08-28", 10, 11)))
            result = build_cn_forward_shadow_evaluation(
                [snapshot], rows, as_of_date="2026-08-28",
                config=CNForwardShadowEvaluationConfig(top_ns=(1, 2), minimum_confirmation_dates=1),
            )
            self.assertIsNone(result["aggregate_top_n"]["2"]["stock_signal_win_rate"])
            self.assertEqual("BLOCKED", result["promotion_status"])
            self.assertIn("incomplete_frozen_batches", result["promotion_blockers"])
            if first.get("ticker") != "BBB":
                self.assertIsNone(result["daily_metrics"][0]["top_n"]["1"]["mean_net_return"])
                self.assertEqual(first.get("ticker", ""), result["daily_metrics"][0]["top_n"]["1"]["members"][0]["ticker"])

    def test_delayed_effective_date_does_not_shorten_horizon(self):
        snapshot = _snapshot(feature_date="2026-08-20", effective_date="2026-08-24", tickers=("AAA",))
        result = build_cn_forward_shadow_evaluation([snapshot], [], as_of_date="2026-08-27")
        self.assertEqual(1, result["pending_date_count"])
        self.assertEqual("2026-08-28", result["pending_dates"][0]["exit_trade_date"])


if __name__ == "__main__":
    unittest.main()
