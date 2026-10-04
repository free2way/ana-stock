from __future__ import annotations

from copy import deepcopy
import unittest

from app.api.routes import screener as screener_route
from app.services.stock_selection.selection_policy import (
    apply_quality_confluence_profile,
    focus_pool_trade_candidates,
    kronos_sort_value,
    lightgbm_confluence_fit_score,
    normalize_action_filter,
    rerank_with_lightgbm_tactical_signal,
)


class ScreenerSelectionPolicyTests(unittest.TestCase):
    def test_route_uses_the_domain_policy_instead_of_reimplementing_it(self) -> None:
        self.assertIs(screener_route._normalize_action_filter, normalize_action_filter)
        self.assertIs(screener_route._lightgbm_confluence_fit_score, lightgbm_confluence_fit_score)
        self.assertIs(screener_route._rerank_with_lightgbm_tactical_signal, rerank_with_lightgbm_tactical_signal)
        self.assertIs(screener_route._apply_quality_confluence_profile, apply_quality_confluence_profile)
        self.assertIs(screener_route._kronos_sort_value, kronos_sort_value)
        self.assertIs(screener_route._focus_pool_trade_candidates, focus_pool_trade_candidates)

    def test_lightgbm_confluence_scoring_normalizes_filters(self) -> None:
        pullback = {"lightgbm_tactical_action": "pullback"}
        breakout = {"lightgbm_tactical_action": "breakout"}
        watch = {"lightgbm_tactical_action": "watch"}

        self.assertEqual("buy_the_dip", normalize_action_filter(" Buy The Dip "))
        self.assertEqual(3, lightgbm_confluence_fit_score(pullback, confluence_action_filter="Buy The Dip"))
        self.assertEqual(1, lightgbm_confluence_fit_score(breakout, confluence_action_filter="buy_the_dip"))
        self.assertEqual(1, lightgbm_confluence_fit_score(watch, confluence_action_filter="bullish_entry"))
        self.assertEqual(1, lightgbm_confluence_fit_score(watch, confluence_action_filter="ALL"))

    def test_tactical_rerank_keeps_the_existing_business_order(self) -> None:
        rows = [
        {
            "ticker": "WATCH",
            "lightgbm_tactical_action": "watch",
            "model_hit_count": 9,
            "confluence_alignment_count": 9,
            "snapshot_score": 99.0,
            "trend_score": 99.0,
        },
        {
            "ticker": "PULLBACK",
            "lightgbm_tactical_action": "pullback",
            "model_hit_count": 2,
            "confluence_alignment_count": 2,
            "snapshot_score": 70.0,
            "trend_score": 70.0,
        },
        {
            "ticker": "BREAKOUT",
            "lightgbm_tactical_action": "breakout",
            "model_hit_count": 4,
            "confluence_alignment_count": 4,
            "snapshot_score": 80.0,
            "trend_score": 80.0,
        },
        ]

        ranked = rerank_with_lightgbm_tactical_signal(rows, confluence_action_filter="buy_the_dip")

        self.assertEqual(["PULLBACK", "BREAKOUT", "WATCH"], [row["ticker"] for row in ranked])
        self.assertEqual("WATCH", rows[0]["ticker"])

    def test_quality_profile_returns_only_execution_ready_confluence_rows(self) -> None:
        rows = [
        {
            "ticker": "PASS",
            "tradability_status": "READY",
            "model_hit_count": 2,
            "trade_readiness_score": 72.0,
            "risk_flags": [],
        },
        {
            "ticker": "RISK",
            "tradability_status": "READY",
            "model_hit_count": 3,
            "trade_readiness_score": 91.0,
            "risk_flags": ["do-not-chase"],
        },
        {
            "ticker": "NOT_READY",
            "tradability_status": "REVIEW",
            "model_hit_count": 4,
            "trade_readiness_score": 95.0,
            "risk_flags": [],
        },
        ]
        original = deepcopy(rows)

        approved = apply_quality_confluence_profile(rows, profile="quality_confluence_v1")

        self.assertEqual(["PASS"], [row["ticker"] for row in approved])
        self.assertEqual("A", approved[0]["strategy_tier"])
        self.assertEqual("双模型共振、市场状态与可成交性均通过", approved[0]["strategy_gate_reason"])
        self.assertNotIn("strategy_tier", rows[1])
        self.assertNotIn("strategy_tier", rows[2])
        self.assertEqual(original[1:], rows[1:])

    def test_unknown_quality_profile_is_a_noop(self) -> None:
        rows = [{"ticker": "ANY"}]
        self.assertIs(rows, apply_quality_confluence_profile(rows, profile=""))

    def test_kronos_sort_value_preserves_ready_and_support_priority(self) -> None:
        supported = {
            "kronos_validation": {
                "kronos_status": "READY",
                "kronos_decision": "支持",
                "kronos_score": 12.5,
            }
        }
        rejected = {
            "kronos_validation": {
                "kronos_status": "READY",
                "kronos_decision": "不支持",
                "kronos_score": 99.0,
            }
        }
        self.assertEqual(1112.5, kronos_sort_value(supported))
        self.assertEqual(199.0, kronos_sort_value(rejected))
        self.assertEqual(-1.0, kronos_sort_value({}))

    def test_focus_pool_policy_excludes_only_hard_blocks_and_low_readiness(self) -> None:
        rows = [
            {"ticker": "READY", "tradability_status": "READY", "trade_readiness_score": 75},
            {"ticker": "BLOCKED", "tradability_status": "BLOCKED", "trade_readiness_score": 95},
            {"ticker": "CHASE", "tradability_status": "DO_NOT_CHASE", "trade_readiness_score": 95},
            {"ticker": "LOW", "tradability_status": "READY", "readiness_bucket": "LOW", "trade_readiness_score": 80},
            {"ticker": "THIN", "tradability_status": "READY", "trade_readiness_score": 59.9},
            {"ticker": "UNKNOWN", "trade_readiness_score": "bad"},
        ]

        candidates, skipped = focus_pool_trade_candidates(rows)

        self.assertEqual(["READY", "UNKNOWN"], [row["ticker"] for row in candidates])
        self.assertEqual(4, skipped)


if __name__ == "__main__":
    unittest.main()
