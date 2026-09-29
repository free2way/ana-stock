from datetime import datetime
from zoneinfo import ZoneInfo
import unittest

from app.services.job_retention import select_job_retention


class JobRetentionTests(unittest.TestCase):
    def test_status_specific_retention_and_active_dependencies(self):
        rows = [
            {"id": 1, "status": "success", "finished_at": "2026-01-01T00:00:00+08:00"},
            {"id": 2, "status": "failed", "finished_at": "2026-01-01T00:00:00+08:00"},
            {"id": 3, "status": "running", "started_at": "2025-01-01T00:00:00+08:00"},
            {"id": 4, "status": "success", "finished_at": "2026-08-01T00:00:00+08:00"},
        ]

        result = select_job_retention(
            rows,
            protected_dependency_ids={2},
            now=datetime(2026, 8, 22, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual([1], result["candidate_delete_ids"])
        self.assertEqual("active_dependency", result["retained_reasons"][2])
        self.assertEqual("active", result["retained_reasons"][3])

    def test_failed_jobs_use_180_day_window(self):
        rows = [
            {"id": 1, "status": "failed", "finished_at": "2026-01-01T00:00:00+08:00"},
            {"id": 2, "status": "failed", "finished_at": "2026-04-01T00:00:00+08:00"},
        ]
        result = select_job_retention(
            rows,
            now=datetime(2026, 8, 22, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

        self.assertEqual([1], result["candidate_delete_ids"])


if __name__ == "__main__":
    unittest.main()
