from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.services.failed_prediction_audit import _counts_by_run


class FailedPredictionAuditTests(unittest.TestCase):
    def test_counts_by_run_normalizes_database_values(self) -> None:
        db = MagicMock()
        db.execute.return_value.all.return_value = [(7, 0), (8, 3)]

        self.assertEqual({7: 0, 8: 3}, _counts_by_run(db, object()))


if __name__ == "__main__":
    unittest.main()
