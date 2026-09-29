import unittest

from app.services.market_snapshot_backfill import snapshot_semantic_summary


class MarketSnapshotBackfillTests(unittest.TestCase):
    def test_semantic_summary_is_order_independent(self):
        first = {
            "fundamental_snapshots": [
                {
                    "symbol_id": 1,
                    "report_date": "2026-06-30",
                    "source": "test",
                },
                {
                    "symbol_id": 2,
                    "report_date": "2026-06-30",
                    "source": "test",
                },
            ],
            "point_in_time_features": [],
            "technical_snapshots": [],
        }
        second = {**first, "fundamental_snapshots": list(reversed(first["fundamental_snapshots"]))}
        self.assertEqual(
            snapshot_semantic_summary(first),
            snapshot_semantic_summary(second),
        )


if __name__ == "__main__":
    unittest.main()
