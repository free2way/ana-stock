import unittest

from app.services.stock_selection.hithink_execution_evidence import (
    build_gap_rows,
    compare_action_sources,
    compare_price_sources,
    normalize_action_items,
    normalize_price_items,
)


class HithinkExecutionEvidenceTests(unittest.TestCase):
    def test_matching_price_and_action_sources_still_leave_suspension_and_bounds_blocked(self):
        prices = normalize_price_items(ticker="000001.SZ", items=[{
            "date_ms": 1790179200000, "open_price": 10, "high_price": 11,
            "low_price": 9, "close_price": 10.5, "volume": 100, "turnover": 1000,
        }])
        actions = normalize_action_items(ticker="000001.SZ", items=[{
            "ex_date_ms": 1790179200000, "dividend_per_share": 0.1, "per_share_bonus": 0,
        }])
        price_check = compare_price_sources(api_rows=prices, dump_rows=prices)
        action_check = compare_action_sources(api_rows=actions, dump_rows=actions)
        result = build_gap_rows(
            tickers=["000001.SZ"], trading_dates=["2026-09-24"],
            price_checks={"000001.SZ": price_check}, action_checks={"000001.SZ": action_check},
            actions_by_ticker={"000001.SZ": actions},
            pool_memberships={("000001.SZ", "2026-09-24"): ["limit_up"]},
            source_references={"raw_price": "prices", "limit_pools": "pools", "corporate_actions": "actions"},
        )
        self.assertFalse(result["evidence_complete"])
        self.assertEqual("POOL_OBSERVATION_ONLY", result["rows"][0]["facts"]["trading_limit"]["status"])
        self.assertEqual("MISSING", result["rows"][0]["facts"]["suspension"]["status"])
        self.assertEqual("action", result["rows"][0]["facts"]["corporate_action"]["state"])

    def test_observable_ohlcv_policy_keeps_optional_gaps_but_allows_sample(self):
        price_checks = {"000001.SZ": {"status": "PASS"}}
        action_checks = {"000001.SZ": {"status": "PASS"}}
        result = build_gap_rows(
            tickers=["000001.SZ"], trading_dates=["2026-09-29"],
            price_checks=price_checks, action_checks=action_checks,
            actions_by_ticker={"000001.SZ": {}}, pool_memberships={},
            source_references={"raw_price": "price", "corporate_actions": "action"},
            required_components=("raw_price", "corporate_action"),
        )
        self.assertTrue(result["evidence_complete"])
        self.assertEqual("EVIDENCE_COMPLETE", result["rows"][0]["status"])
        self.assertEqual(["suspension", "trading_limit"], result["rows"][0]["optional_evidence_gaps"])
        self.assertEqual("none", result["rows"][0]["facts"]["corporate_action"]["state"])

    def test_source_conflicts_are_not_silently_accepted(self):
        base = {"2026-09-24": {"ticker": "600519.SH", "date": "2026-09-24",
                "open_price": 10.0, "high_price": 11.0, "low_price": 9.0,
                "close_price": 10.0, "volume": 100.0, "turnover": 1000.0}}
        changed = {"2026-09-24": {**base["2026-09-24"], "close_price": 10.1}}
        self.assertEqual("BLOCKED", compare_price_sources(api_rows=base, dump_rows=changed)["status"])

        api = {"2026-09-24": [{"ticker": "600519.SH", "ex_date": "2026-09-24",
                                "dividend_per_share": 1.0, "per_share_bonus": 0.0}]}
        self.assertEqual("BLOCKED", compare_action_sources(api_rows=api, dump_rows={})["status"])


if __name__ == "__main__":
    unittest.main()
