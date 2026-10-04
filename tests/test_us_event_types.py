from __future__ import annotations

from datetime import date
from unittest import TestCase

from app.services.alpaca_client import normalize_corporate_action_rows
from app.services.corporate_actions import CorporateActionRecord, explains_close_jump


class USEventTypeTests(TestCase):
    """US event types beyond split/dividend: merger completions and spinoffs."""

    def test_merger_and_spinoff_are_normalized(self) -> None:
        rows = normalize_corporate_action_rows(
            [
                {
                    "id": "m1", "ca_type": "merger", "ca_sub_type": "merger_completion",
                    "target_symbol": "alex", "effective_date": "2026-03-12", "cash": "20.85",
                    "old_rate": "1", "new_rate": "0",
                },
                {
                    "id": "s1", "ca_type": "spinoff", "initiating_symbol": "VSNT",
                    "target_symbol": "CMCSA", "effective_date": "2025-12-03", "ex_date": "2026-01-05",
                    "new_rate": "0.07904",
                },
            ]
        )
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual("merger", by_symbol["ALEX"]["action_type"])
        self.assertAlmostEqual(20.85, by_symbol["ALEX"]["cash_amount"])
        self.assertEqual("2026-03-12", by_symbol["ALEX"]["effective_date"])
        self.assertEqual("spinoff", by_symbol["CMCSA"]["action_type"])
        self.assertEqual("2026-01-05", by_symbol["CMCSA"]["effective_date"])

    def test_merger_completion_explains_final_price_step(self) -> None:
        action = CorporateActionRecord(
            market="US", symbol="ALEX", action_type="merger",
            effective_date=date(2026, 3, 12), cash_amount=20.85,
        )
        self.assertTrue(
            explains_close_jump(previous_close=19.5, close=20.9, actions=[action], symbol="ALEX", band=0.5)
        )
        # other symbols are unaffected
        self.assertFalse(
            explains_close_jump(previous_close=19.5, close=20.9, actions=[action], symbol="OTHER", band=0.5)
        )

    def test_spinoff_explains_parent_repricing(self) -> None:
        action = CorporateActionRecord(
            market="US", symbol="CMCSA", action_type="spinoff",
            effective_date=date(2026, 1, 5),
        )
        self.assertTrue(
            explains_close_jump(previous_close=30.0, close=26.0, actions=[action], symbol="CMCSA", band=0.5)
        )

    def test_unknown_action_types_still_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported action_type"):
            CorporateActionRecord(
                market="US", symbol="AAA", action_type="dividend_reinvest",
                effective_date=date(2026, 1, 5),
            )
