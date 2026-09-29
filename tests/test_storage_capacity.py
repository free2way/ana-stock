import unittest
from datetime import date, timedelta

from app.services.storage_capacity import evaluate_capacity_growth


class StorageCapacityGrowthTests(unittest.TestCase):
    def _samples(self, growth_values: list[int]) -> list[dict]:
        value = 1_000_000_000
        current = date(2026, 8, 1)
        rows = [{"sample_date": current, "database_bytes": value}]
        for growth in growth_values:
            current += timedelta(days=1)
            value += growth
            rows.append({"sample_date": current, "database_bytes": value})
        return rows

    def test_five_consecutive_days_under_limit_pass(self):
        result = evaluate_capacity_growth(self._samples([10, 12, 8, 11, 9]))
        self.assertEqual("pass", result["status"])
        self.assertTrue(result["consecutive"])
        self.assertEqual(10, result["average_daily_growth_bytes"])

    def test_missing_calendar_day_remains_collecting(self):
        samples = self._samples([10, 10, 10, 10, 10])
        samples[-1]["sample_date"] += timedelta(days=1)
        result = evaluate_capacity_growth(samples)
        self.assertEqual("collecting", result["status"])
        self.assertFalse(result["consecutive"])

    def test_average_above_limit_fails(self):
        limit = 20 * 1024 * 1024
        result = evaluate_capacity_growth(
            self._samples([limit + 1] * 5),
            limit_bytes=limit,
        )
        self.assertEqual("failed", result["status"])

    def test_same_day_capture_does_not_count_twice(self):
        samples = self._samples([1, 1, 1, 1])
        samples.append(
            {
                "sample_date": samples[-1]["sample_date"],
                "database_bytes": samples[-1]["database_bytes"] + 1,
            }
        )
        result = evaluate_capacity_growth(samples)
        self.assertEqual("collecting", result["status"])
        self.assertEqual(5, result["sample_count"])

    def test_final_window_excludes_pre_cutover_and_oversized_samples(self):
        samples = self._samples([10, 10, 10, 10, 10, 10, 10])
        samples[1]["database_bytes"] = 9_000_000_000
        result = evaluate_capacity_growth(
            samples,
            not_before_date="2026-08-02",
            maximum_database_bytes=5_000_000_000,
        )

        self.assertEqual("pass", result["status"])
        self.assertEqual("2026-08-03", result["sample_dates"][0])
        self.assertEqual(1, result["excluded_before_window"])
        self.assertEqual(1, result["excluded_above_maximum"])


if __name__ == "__main__":
    unittest.main()
