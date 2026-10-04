from unittest import TestCase

from app.services.stock_selection.frozen_panel_comparison import compare_frozen_panel_models


class FrozenPanelComparisonTests(TestCase):
    def sample(self, ticker, day, value, *, labeled=True):
        return {"symbol": ticker, "trade_date": day,
                "features": {"quality": value, "risk": 1.0 - value},
                "target": (value - 0.5 if labeled else None),
                "label_available_date": ("2026-01-20" if labeled else None)}

    def test_every_model_scores_the_identical_holdout_panel(self):
        train = [self.sample(f"S{ticker:02}", f"2026-01-{day:02}", ticker / 10)
                 for day in range(1, 13) for ticker in range(10)]
        test = [self.sample(f"S{ticker:02}", f"2026-02-{day:02}", ticker / 10,
                            labeled=not (day == 2 and ticker == 9))
                for day in range(1, 3) for ticker in range(10)]
        result = compare_frozen_panel_models(train=train, test=test,
            feature_names=["quality", "risk"], lower_better={"risk"}, top_n=5)
        self.assertEqual(20, result["panel_candidate_count"])
        self.assertEqual({"equal_weight", "ridge", "lambdarank", "lightgbm"},
                         set(result["models"]))
        self.assertTrue(all(report["selected_count"] == 10 for report in result["models"].values()))
        self.assertTrue(all(report["closed_count"] + report["unverified_count"] == 10
                            for report in result["models"].values()))
