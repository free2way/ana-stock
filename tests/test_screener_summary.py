from __future__ import annotations

import copy
import unittest

from app.services.stock_selection.screener_summary import summarize_screener_rows


class ScreenerSummaryTests(unittest.TestCase):
    def test_summarizes_risk_and_trade_status_without_mutation(self) -> None:
        rows = [
            {
                "ticker": "A",
                "model_execution_tags": ["gap-risk", "earnings-soon"],
                "tradability_status": "READY",
                "readiness_bucket": "HIGH",
                "trade_readiness_score": 80,
            },
            {
                "ticker": "B",
                "model_execution_tags": ["gap-risk"],
                "tradability_status": "DO_NOT_CHASE",
                "readiness_bucket": "LOW",
                "trade_readiness_score": 40,
            },
            {"ticker": "C", "tradability_status": "BLOCKED"},
        ]
        original = copy.deepcopy(rows)

        summary = summarize_screener_rows(rows)

        self.assertEqual(rows, original)
        self.assertEqual(summary["tagged_names"], 2)
        self.assertEqual(summary["risk_top_tags"][0], ("gap-risk", 2))
        self.assertEqual(summary["status_counts"]["ready"], 1)
        self.assertEqual(summary["status_counts"]["do_not_chase"], 1)
        self.assertEqual(summary["status_counts"]["blocked"], 1)
        self.assertEqual(summary["status_counts"]["low_readiness"], 1)


if __name__ == "__main__":
    unittest.main()
