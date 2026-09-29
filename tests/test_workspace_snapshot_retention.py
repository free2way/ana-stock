from datetime import date
import unittest

from app.services.workspace_snapshot_retention import (
    select_workspace_snapshot_retention,
)


class WorkspaceSnapshotRetentionTests(unittest.TestCase):
    def test_calendar_tiers_keep_latest_per_bucket(self):
        rows = [
            {"id": 10, "snapshot_type": "pipeline", "snapshot_date": "2026-08-21"},
            {"id": 9, "snapshot_type": "pipeline", "snapshot_date": "2026-08-21"},
            {"id": 8, "snapshot_type": "pipeline", "snapshot_date": "2026-07-01"},
            {"id": 7, "snapshot_type": "pipeline", "snapshot_date": "2026-07-02"},
            {"id": 6, "snapshot_type": "pipeline", "snapshot_date": "2025-12-01"},
            {"id": 5, "snapshot_type": "pipeline", "snapshot_date": "2025-12-20"},
        ]

        result = select_workspace_snapshot_retention(
            rows,
            today=date(2026, 8, 22),
        )

        self.assertIn(10, result["keep_ids"])
        self.assertIn(8, result["keep_ids"])
        self.assertIn(6, result["keep_ids"])
        self.assertIn(9, result["candidate_delete_ids"])
        self.assertIn(5, result["candidate_delete_ids"])

    def test_invalid_dates_and_latest_floor_are_protected(self):
        rows = [
            {"id": 3, "snapshot_type": "audit", "snapshot_date": "bad"},
            {"id": 2, "snapshot_type": "audit", "snapshot_date": "2026-08-20"},
            {"id": 1, "snapshot_type": "audit", "snapshot_date": "2026-08-20"},
        ]

        result = select_workspace_snapshot_retention(
            rows,
            today=date(2026, 8, 22),
            minimum_latest_per_type=2,
        )

        self.assertIn(3, result["keep_ids"])
        self.assertIn(2, result["keep_ids"])
        self.assertIn(1, result["candidate_delete_ids"])


if __name__ == "__main__":
    unittest.main()
