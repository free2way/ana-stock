from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.storage_architecture_benchmark import measure_query, percentile


class StorageArchitectureBenchmarkTests(unittest.TestCase):
    def test_percentile_interpolates_and_validates_input(self) -> None:
        self.assertEqual(percentile([4.0], 95), 4.0)
        self.assertEqual(percentile([1.0, 2.0, 3.0], 50), 2.0)
        self.assertAlmostEqual(percentile([0.0, 10.0], 95), 9.5)
        with self.assertRaises(ValueError):
            percentile([], 95)

    @patch(
        "app.services.storage_architecture_benchmark.time.perf_counter",
        side_effect=[1.0, 1.01, 2.0, 2.02],
    )
    def test_measure_query_reports_rows_and_threshold(self, _perf_counter) -> None:
        result = measure_query(lambda: [1, 2], iterations=2, warmups=1, threshold_ms=25)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["row_count_min"], 2)
        self.assertEqual(result["row_count_max"], 2)
        self.assertEqual(result["samples_ms"], [10.0, 20.0])
        self.assertEqual(result["p95_ms"], 19.5)


if __name__ == "__main__":
    unittest.main()
