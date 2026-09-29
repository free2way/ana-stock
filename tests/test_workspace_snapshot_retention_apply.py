from __future__ import annotations

import unittest

from scripts.apply_workspace_snapshot_retention_batch import _candidate_digest, _plan


class WorkspaceSnapshotRetentionApplyTests(unittest.TestCase):
    def test_plan_is_bounded_oldest_first_and_counts_payload(self) -> None:
        rows = [
            {
                "id": 4,
                "snapshot_type": "pipeline",
                "snapshot_date": "2026-09-09",
                "payload_bytes": 40,
            },
            {
                "id": 3,
                "snapshot_type": "pipeline",
                "snapshot_date": "2026-09-09",
                "payload_bytes": 30,
            },
            {
                "id": 2,
                "snapshot_type": "pipeline",
                "snapshot_date": "2026-09-09",
                "payload_bytes": 20,
            },
            {
                "id": 1,
                "snapshot_type": "pipeline",
                "snapshot_date": "2026-09-09",
                "payload_bytes": 10,
            },
        ]

        result = _plan(rows, minimum_latest_per_type=1, max_rows=2)

        self.assertEqual([1, 2], result["batch_ids"])
        self.assertEqual(2, result["batch_rows"])
        self.assertEqual(30, result["batch_payload_bytes"])
        self.assertEqual(3, result["candidate_delete_rows"])
        self.assertEqual(60, result["candidate_payload_bytes"])

    def test_empty_digest_is_stable(self) -> None:
        self.assertEqual(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            _candidate_digest([]),
        )


if __name__ == "__main__":
    unittest.main()
