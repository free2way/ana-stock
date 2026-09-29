from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.services.prediction_read_rollback import _rows_digest, run_prediction_read_rollback_drill


class PredictionReadRollbackTests(unittest.TestCase):
    def test_digest_preserves_candidate_order_and_ignores_presentation_fields(self) -> None:
        first = [
            {
                "model_run_id": 1,
                "trade_date": "2026-08-22",
                "ticker": "000001.SZ",
                "score": 0.8,
                "rank_value": 1.0,
                "name": "A",
            },
            {
                "model_run_id": 1,
                "trade_date": "2026-08-22",
                "ticker": "000002.SZ",
                "score": 0.7,
                "rank_value": 2.0,
            },
        ]
        renamed = [{**row, "name": "changed"} for row in first]
        reversed_rows = list(reversed(first))

        self.assertEqual(_rows_digest(first), _rows_digest(renamed))
        self.assertNotEqual(_rows_digest(first), _rows_digest(reversed_rows))

    def test_us_rollback_drill_is_market_isolated_after_hold_is_removed(self) -> None:
        rows = [{
            "model_run_id": 7,
            "trade_date": "2026-09-14",
            "ticker": "AAPL",
            "score": 0.8,
            "rank_value": 1.0,
            "source_layer": "postgresql_hot",
        }]
        observed: list[tuple[str, bool]] = []

        class RepositoryStub:
            def __init__(self, _db, *, cold_reads_enabled):
                self.cold_reads_enabled = cold_reads_enabled

            def list_latest_predictions_for_market(self, market, *, limit):
                observed.append((market, self.cold_reads_enabled))
                return rows

        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        with patch(
            "app.services.prediction_read_rollback.PredictionRepository",
            RepositoryStub,
        ):
            result = run_prediction_read_rollback_drill(db, market="US", limit=50)

        self.assertEqual("pass", result["status"])
        self.assertEqual("US", result["market"])
        self.assertEqual([("US", True), ("US", False)], observed)

    def test_rollback_drill_rejects_unconfigured_market(self) -> None:
        with self.assertRaisesRegex(ValueError, "CN or US"):
            run_prediction_read_rollback_drill(MagicMock(), market="HK")


if __name__ == "__main__":
    unittest.main()
