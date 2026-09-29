import unittest

import numpy as np

from app.services.market_hot_predictions import (
    INSERT_BIND_PARAMETER_BUDGET,
    MarketHotPredictionRepository,
    _chunks,
    _prepare_rows,
)


class MarketHotPredictionTests(unittest.TestCase):
    def test_chunks_respect_postgresql_bind_parameter_budget(self):
        detail_row = {f"column_{index}": index for index in range(20)}
        chunks = _chunks([detail_row] * 5_548)

        self.assertEqual([3_000, 2_548], [len(chunk) for chunk in chunks])
        self.assertTrue(
            all(
                len(chunk) * len(detail_row) <= INSERT_BIND_PARAMETER_BUDGET
                for chunk in chunks
            )
        )

    def test_chunks_keep_fast_path_for_narrow_prediction_payloads(self):
        prediction_row = {f"column_{index}": index for index in range(7)}

        self.assertEqual(
            [5_000, 548],
            [len(chunk) for chunk in _chunks([prediction_row] * 5_548)],
        )

    def test_prepare_rows_rejects_children_without_hot_parent(self):
        predictions, details, explanations = _prepare_rows(
            [{"symbol_id": 1, "trade_date": "2026-08-21", "score": 0.8}],
            [
                {"symbol_id": 1, "trade_date": "2026-08-21", "confidence": 0.9},
                {"symbol_id": 2, "trade_date": "2026-08-21", "confidence": 0.9},
            ],
            [
                {
                    "symbol_id": 2,
                    "trade_date": "2026-08-21",
                    "feature_name": "momentum",
                }
            ],
        )

        self.assertEqual(1, len(predictions))
        self.assertEqual(1, len(details))
        self.assertEqual([], explanations)

    def test_prepare_rows_deduplicates_predictions_and_explanations(self):
        predictions, _, explanations = _prepare_rows(
            [
                {"symbol_id": 1, "trade_date": "2026-08-21", "score": 0.7},
                {"symbol_id": 1, "trade_date": "2026-08-21", "score": 0.9},
            ],
            [],
            [
                {
                    "symbol_id": 1,
                    "trade_date": "2026-08-21",
                    "feature_name": "momentum",
                    "contribution": 0.1,
                },
                {
                    "symbol_id": 1,
                    "trade_date": "2026-08-21",
                    "feature_name": "momentum",
                    "contribution": 0.2,
                },
            ],
        )

        self.assertEqual(0.9, predictions[0]["score"])
        self.assertEqual(1, len(explanations))
        self.assertEqual(0.2, explanations[0]["contribution"])

    def test_prepare_rows_normalizes_numpy_scalars_before_semantic_hashing(self):
        prepared = _prepare_rows(
            [
                {
                    "symbol_id": np.int64(1),
                    "trade_date": "2026-08-21",
                    "score": np.float32(0.125),
                    "rank_value": np.float64(1.0),
                }
            ],
            [
                {
                    "symbol_id": np.int64(1),
                    "trade_date": "2026-08-21",
                    "confidence": np.float32(45.5),
                    "target_horizon_days": np.int64(5),
                    "signal_label": np.str_("watch"),
                }
            ],
            [
                {
                    "symbol_id": np.int64(1),
                    "trade_date": "2026-08-21",
                    "feature_name": np.str_("momentum"),
                    "feature_value": np.float32(1.25),
                    "contribution": np.float32(0.5),
                    "display_order": np.int64(1),
                }
            ],
        )
        database_round_trip = (
            [
                {
                    "symbol_id": 1,
                    "trade_date": "2026-08-21",
                    "score": 0.125,
                    "rank_value": 1.0,
                }
            ],
            [
                {
                    "symbol_id": 1,
                    "trade_date": "2026-08-21",
                    **{
                        column: None
                        for column in (
                            "bullish_prob",
                            "bearish_prob",
                            "expected_return_5d",
                            "expected_return_20d",
                            "expected_drawdown_20d",
                            "model_reward_risk_ratio",
                            "risk_score",
                            "universe_size",
                            "percentile",
                            "regime_label",
                            "conviction_bucket",
                            "position_size_hint",
                            "entry_style",
                            "signal_strength",
                            "summary_text",
                        )
                    },
                    "confidence": 45.5,
                    "target_horizon_days": 5,
                    "signal_label": "watch",
                }
            ],
            [
                {
                    "symbol_id": 1,
                    "trade_date": "2026-08-21",
                    "feature_name": "momentum",
                    "feature_value": 1.25,
                    "contribution": 0.5,
                    "direction": None,
                    "display_order": 1,
                }
            ],
        )

        self.assertEqual(
            MarketHotPredictionRepository._digests(*database_round_trip),
            MarketHotPredictionRepository._digests(*prepared),
        )


if __name__ == "__main__":
    unittest.main()
