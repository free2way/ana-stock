from copy import deepcopy
from datetime import date
from unittest import TestCase

from app.services.stock_selection.cn_execution_coverage import assess_cn_execution_coverage


class CNExecutionCoverageTests(TestCase):
    def setUp(self):
        self.days = [date.fromisoformat(day) for day in ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")]
        self.rows = [dict(symbol="000001.SZ", date=day.isoformat(), open=10.0, high=10.2,
            low=9.8, close=10.0, volume=1000, adj_close=10.0, dividend=None, split_ratio=None)
            for day in self.days]

    def assess(self, rows=None):
        return assess_cn_execution_coverage(self.rows if rows is None else rows,
            trading_dates=self.days, tickers=["000001.SZ"], horizon_days=3, source_reference="fixture")

    def enriched(self):
        return [{**row, "price_basis": "raw", "execution_source_reference": "verified-fixture",
                 "corporate_action_status": "none", "upper_limit": 11.0, "lower_limit": 9.0,
                 "suspended": False} for row in self.rows]

    def test_ohlcv_does_not_silently_certify_execution(self):
        result = self.assess()
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual(1, result["status_counts"]["unknown"])
        self.assertEqual(3, result["status_counts"]["immature"])
        self.assertEqual(0, result["eligible_coverage_ratio"])
        self.assertIsNone(result["training_evidence"])
        self.assertIn("corporate_action_state_unknown", result["reason_counts"])

    def test_explicit_complete_facts_produce_price_bound_research_evidence(self):
        result = self.assess(self.enriched())
        self.assertEqual(1, result["status_counts"]["eligible"])
        self.assertEqual(result["price_sha256"], result["training_evidence"]["price_sha256"])
        self.assertIsNone(result["training_evidence"]["records"][0]["reason"])
        self.assertEqual("NOT_RUN", result["model_backtest_status"])

    def test_zero_volume_is_a_known_reject_even_with_missing_metadata(self):
        rows = deepcopy(self.rows)
        rows[1]["volume"] = 0
        result = self.assess(rows)
        self.assertEqual(1, result["status_counts"]["blocked"])
        self.assertFalse(result["decisions"][0]["entry_allowed"])
        self.assertIn("entry_suspended_or_no_volume", result["reason_counts"])

    def test_limit_up_blocks_entry_using_supplied_limit_not_current_name(self):
        rows = self.enriched()
        rows[1].update(open=11.0, close=11.0, high=11.0, low=11.0)
        result = self.assess(rows)
        self.assertFalse(result["decisions"][0]["entry_allowed"])
        self.assertIn("entry_at_upper_limit", result["reason_counts"])

    def test_limit_down_blocks_exit_and_does_not_count_as_profit(self):
        rows = self.enriched()
        rows[-1].update(open=9.0, close=9.0, high=9.0, low=9.0)
        result = self.assess(rows)
        self.assertFalse(result["decisions"][0]["exit_allowed"])
        self.assertEqual(0, result["eligible_coverage_ratio"])

    def test_corporate_action_requires_replay_instead_of_fixed_label(self):
        rows = self.enriched()
        rows[2]["corporate_action_status"] = "action"
        result = self.assess(rows)
        self.assertIn("corporate_action_requires_account_replay", result["reason_counts"])
        self.assertEqual(1, result["status_counts"]["blocked"])

    def test_missing_middle_quote_does_not_shorten_holding_period(self):
        result = self.assess(self.rows[:2] + self.rows[3:])
        self.assertEqual(1, result["matured_decision_count"])
        self.assertEqual(1, result["reason_counts"]["missing_price_path"])

    def test_zero_dividend_split_values_do_not_prove_no_actions(self):
        rows = [{**row, "dividend": 0, "split_ratio": 1} for row in self.rows]
        self.assertIn("corporate_action_state_unknown", self.assess(rows)["reason_counts"])

    def test_bj_and_noncanonical_market_are_rejected(self):
        for ticker in ("430001.BJ", "AAPL", "600000.SH"):
            with self.assertRaises(ValueError):
                assess_cn_execution_coverage(self.rows, trading_dates=self.days, tickers=[ticker],
                    horizon_days=3, source_reference="fixture")

    def test_invalid_suspension_boolean_is_unknown(self):
        rows = self.enriched()
        rows[1]["suspended"] = "false"
        result = self.assess(rows)
        self.assertEqual(1, result["status_counts"]["unknown"])

    def test_missing_provenance_blocks_raw_evidence_export(self):
        rows = self.enriched()
        rows[1]["execution_source_reference"] = ""
        self.assertIsNone(self.assess(rows)["training_evidence"])
