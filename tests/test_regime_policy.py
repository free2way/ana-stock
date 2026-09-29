from copy import deepcopy
from unittest import TestCase

from app.services.stock_selection.regime_policy import CEILINGS, evaluate_regime_policy, prepare_regime_research_selection


class RegimePolicyTests(TestCase):
    def snapshot(self, **changes):
        return {"market": "CN", "snapshot_date": "2026-09-18", "generated_at": "2026-09-18T18:00:00+08:00",
                "risk_regime": "risk_on", "buy_gate": "ALLOW", "max_position_scale": 1.0, **changes}

    def assess(self, snapshot=None, **changes):
        args = {"market": "CN", "expected_market_date": "2026-09-18",
                "decision_cutoff_at": "2026-09-18T19:00:00+08:00", **changes}
        return evaluate_regime_policy(self.snapshot() if snapshot is None else snapshot, **args)

    def test_all_eight_states_and_existing_ceilings(self):
        for regime, ceiling in CEILINGS.items():
            result = self.assess(self.snapshot(risk_regime=regime))
            self.assertEqual(ceiling, result["max_position_scale"])
            self.assertEqual(0 if ceiling == 0 else 5, result["max_new_candidates"])
            self.assertEqual("BLOCK" if not ceiling else "ALLOW" if regime == "risk_on" else "REVIEW", result["buy_gate"])

    def test_source_block_cannot_be_relaxed_by_risk_on(self):
        result = self.assess(self.snapshot(buy_gate="BLOCK"))
        self.assertEqual("BLOCK", result["buy_gate"])
        self.assertEqual(0, result["max_position_scale"])

    def test_review_and_tighter_budget_are_preserved(self):
        result = self.assess(self.snapshot(buy_gate="REVIEW", max_position_scale=0.15), max_new_candidates=2)
        self.assertEqual("REVIEW", result["buy_gate"])
        self.assertEqual(0.15, result["max_position_scale"])
        self.assertEqual(2, result["max_new_candidates"])

    def test_stale_future_and_cross_market_snapshots_block(self):
        for changes in ({"snapshot_date": "2026-09-17"}, {"snapshot_date": "2026-09-21"}, {"market": "US"}):
            self.assertEqual("BLOCK", self.assess(self.snapshot(**changes))["buy_gate"])

    def test_missing_and_invalid_state_gate_or_budget_block(self):
        for value in ({}, self.snapshot(risk_regime="defensive"), self.snapshot(risk_regime=[]),
                      self.snapshot(buy_gate="allow"), self.snapshot(max_position_scale=None),
                      self.snapshot(max_position_scale=True), self.snapshot(max_position_scale=float("nan")),
                      self.snapshot(max_position_scale=1.1), self.snapshot(max_position_scale=-1),
                      self.snapshot(max_position_scale=10**1000)):
            self.assertEqual("BLOCK", self.assess(value)["buy_gate"])

    def test_missing_snapshot_is_explicitly_blocked(self):
        result = evaluate_regime_policy(None, market="CN", expected_market_date="2026-09-18",
                                       decision_cutoff_at="2026-09-18T19:00:00+08:00")
        self.assertEqual("BLOCK", result["buy_gate"])

    def test_future_naive_or_missing_generation_time_blocks(self):
        for timestamp in (None, "2026-09-18T18:00:00", "bad", "2026-09-18T19:00:01+08:00", "2026-09-17T18:00:00+08:00"):
            self.assertEqual("BLOCK", self.assess(self.snapshot(generated_at=timestamp))["buy_gate"])

    def test_cutoff_timezone_and_historical_asof_are_not_host_today(self):
        result = self.assess(self.snapshot(generated_at="2026-09-18T10:00:00Z"), decision_cutoff_at="2026-09-18T11:00:00Z")
        self.assertEqual("ALLOW", result["buy_gate"])
        self.assertEqual("BLOCK", self.assess(decision_cutoff_at="2026-09-17T19:00:00+08:00")["buy_gate"])

    def test_invalid_caps_and_closed_dates_fail_closed(self):
        for cap in (True, -1, 6, 5.0, None):
            self.assertEqual("BLOCK", self.assess(max_new_candidates=cap)["buy_gate"])
        self.assertEqual("BLOCK", self.assess(expected_market_date="2026-09-19")["buy_gate"])

    def test_us_has_separate_market_date_and_budget(self):
        result = self.assess(self.snapshot(market="US", generated_at="2026-09-18T17:00:00-04:00"), market="US",
                             decision_cutoff_at="2026-09-18T18:00:00-04:00")
        self.assertEqual("ALLOW", result["buy_gate"])
        self.assertTrue(result["research_only"])
        self.assertFalse(result["protocol_approved"])

    def test_digest_deterministic_and_input_not_modified(self):
        value = self.snapshot()
        original = deepcopy(value)
        first = self.assess(value)
        second = self.assess(dict(reversed(list(value.items()))))
        self.assertEqual(first, second)
        self.assertEqual(original, value)
        self.assertEqual(64, len(first["source_snapshot_sha256"]))

    def test_zero_budget_blocks_and_multiplier_is_not_account_allocation(self):
        self.assertEqual("BLOCK", self.assess(max_new_candidates=0)["buy_gate"])
        self.assertEqual("BLOCK", self.assess(self.snapshot(max_position_scale=0))["buy_gate"])
        self.assertEqual("multiplier_of_protocol_exposure_cap_not_account_weight", self.assess()["budget_semantics"])

    def select(self, rows, **changes):
        return prepare_regime_research_selection(rows, self.snapshot(**changes), market="CN",
            expected_market_date="2026-09-18", decision_cutoff_at="2026-09-18T19:00:00+08:00")

    def test_research_selection_preserves_order_and_top5_without_refilling(self):
        rows = [{"ticker": f"60000{i}.SS", "rank": 6-i} for i in range(6, 0, -1)]
        original = deepcopy(rows)
        result = self.select(rows)
        self.assertEqual(rows[:5], result["research_candidates"])
        self.assertEqual(rows, result["watch_candidates"])
        self.assertEqual(1, len(result["excluded_candidates"]))
        result["research_candidates"][0]["rank"] = -1
        self.assertEqual(original, rows)
        self.assertEqual(2, self.select(rows[:2])["selected_count"])

    def test_research_crash_and_stale_keep_denominators_but_zero_candidates(self):
        rows = [{"ticker": "600000.SS"}, {"ticker": "000001.SZ"}]
        for changes in ({"risk_regime": "crash"}, {"risk_regime": "rebound_failed"}, {"snapshot_date": "2026-09-17"}):
            result = self.select(rows, **changes)
            self.assertEqual([], result["research_candidates"])
            self.assertEqual(2, result["input_count"])
            self.assertEqual(2, len(result["excluded_candidates"]))
            self.assertEqual(rows, result["watch_candidates"])

    def test_research_selection_rejects_wrong_market_duplicate_and_bj(self):
        for tickers in (["AAPL"], ["430001.BJ"], ["600000.SH"], ["600000.SS", "600000.SS"]):
            with self.assertRaises(ValueError):
                self.select([{"ticker": ticker} for ticker in tickers])
