import unittest

from app.services.repository import LivePredictionRepository


class LivePredictionRepositoryTests(unittest.TestCase):
    def test_latest_rows_keeps_only_latest_cross_section(self):
        rows = [
            {"symbol_id": 1, "trade_date": "2026-08-20"},
            {"symbol_id": 1, "trade_date": "2026-08-21"},
            {"symbol_id": 2, "trade_date": "2026-08-21"},
        ]

        selected = LivePredictionRepository.latest_rows(rows)

        self.assertEqual([1, 2], [row["symbol_id"] for row in selected])
        self.assertEqual({"2026-08-21"}, {row["trade_date"] for row in selected})

    def test_latest_rows_ignores_rows_without_dates(self):
        self.assertEqual([], LivePredictionRepository.latest_rows([{"symbol_id": 1}]))


if __name__ == "__main__":
    unittest.main()
