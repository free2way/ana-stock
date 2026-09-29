import unittest

from app.services.tradability_filter import evaluate_candidate_tradability


class RiskGuardrailTradabilityTests(unittest.TestCase):
    def _breakout_candidate(self) -> dict:
        return {
            "ticker": "000001.SZ",
            "market": "CN",
            "score": 0.92,
            "model_percentile": 92.0,
            "signal_label": "BUY",
            "signal_strength": 88,
            "entry_style": "breakout",
            "latest_close": 10.0,
            "momentum_5": 8.0,
        }

    def test_market_block_gate_blocks_otherwise_strong_candidate(self) -> None:
        decision = evaluate_candidate_tradability(
            self._breakout_candidate(),
            market_snapshot={"market": "CN", "regime": "defensive", "buy_gate": "BLOCK", "breadth_pct": 27.3},
        )

        self.assertFalse(decision.is_tradable)
        self.assertEqual("BLOCKED", decision.tradability_status)
        self.assertEqual("market_buy_gate_blocked", decision.block_reason)
        self.assertIn("market-buy-gate-blocked", decision.risk_flags)

    def test_market_review_gate_defers_breakout_candidate(self) -> None:
        decision = evaluate_candidate_tradability(
            {**self._breakout_candidate(), "market": "US", "ticker": "AAPL"},
            market_snapshot={"market": "US", "regime": "watchful", "buy_gate": "REVIEW", "breadth_pct": 59.3},
        )

        self.assertTrue(decision.is_tradable)
        self.assertEqual("DEFER", decision.tradability_status)
        self.assertIn("market-risk-review", decision.risk_flags)

    def test_observation_only_model_cannot_produce_normal_ready_signal(self) -> None:
        decision = evaluate_candidate_tradability(
            {**self._breakout_candidate(), "model_activation_status": "observation_insufficient_oos"},
            market_snapshot={"market": "CN", "regime": "risk_on", "buy_gate": "ALLOW", "breadth_pct": 65.0},
        )

        self.assertTrue(decision.is_tradable)
        self.assertEqual("DEFER", decision.tradability_status)
        self.assertIn("model-observation-only", decision.risk_flags)

    def test_rank_margin_uses_same_date_percentile_for_conviction(self) -> None:
        decision = evaluate_candidate_tradability({
            **self._breakout_candidate(),
            "score": 0.043,
            "model_percentile": 96.0,
        })

        self.assertEqual("READY", decision.tradability_status)
        self.assertNotIn("low-conviction", decision.risk_flags)
        self.assertEqual(0.043, decision.diagnostics["raw_model_score"])
        self.assertEqual(0.96, decision.diagnostics["conviction_score"])
        self.assertEqual("same_date_percentile", decision.diagnostics["conviction_source"])

    def test_explicit_zero_score_is_not_treated_as_missing(self) -> None:
        decision = evaluate_candidate_tradability({
            **self._breakout_candidate(),
            "score": 0.0,
            "model_percentile": 80.0,
        })

        self.assertNotIn("missing-model-score", decision.risk_flags)
        self.assertEqual(0.0, decision.diagnostics["raw_model_score"])

    def test_recalculation_removes_stale_derived_conviction_flag(self) -> None:
        decision = evaluate_candidate_tradability({
            **self._breakout_candidate(),
            "score": 0.043,
            "model_percentile": 96.0,
            "risk_flags": ["low-conviction", "external-liquidity-warning"],
        })

        self.assertNotIn("low-conviction", decision.risk_flags)
        self.assertIn("external-liquidity-warning", decision.risk_flags)


if __name__ == "__main__":
    unittest.main()
