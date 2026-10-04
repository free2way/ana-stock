from __future__ import annotations

from unittest import TestCase

from app.services.backtesting.schemas import EngineConfig
from app.services.stock_selection.decision_ledger import (
    _market_decision_identity,
    validate_execution_effective_date,
)


class CNTPlusOneEntryPointTests(TestCase):
    """E-5: the four entries (protocol / engine / paper / ledger) reject T+0."""

    def test_protocol_entry_rejects_horizon_one(self) -> None:
        from app.services.stock_selection.protocol import ExecutableSelectionProtocol

        with self.assertRaisesRegex(ValueError, "T\\+1"):
            ExecutableSelectionProtocol(
                market="CN", horizon_days=1, top_n=5,
                entry_price_mode="next_session_open", exit_price_mode="holding_session_close",
            )

    def test_engine_entry_rejects_holding_days_one(self) -> None:
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            EngineConfig(market="CN", top_n=5, holding_days=1)
        # US has no T+1 restriction
        EngineConfig(market="US", top_n=5, holding_days=1)

    def test_paper_entry_inherits_protocol_and_engine_guards(self) -> None:
        # paper builds EngineConfig from protocol.horizon_days: a 1-day CN
        # horizon cannot exist, and a hand-built 1-day CN config is rejected.
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            EngineConfig(market="CN", top_n=3, holding_days=1, liquidate_at_end=False)
        config = EngineConfig(market="CN", top_n=3, holding_days=2)
        self.assertEqual(2, config.holding_days)

    def test_ledger_entry_rejects_same_session_effective_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            validate_execution_effective_date("CN", "2026-10-09", "2026-10-09")
        with self.assertRaisesRegex(ValueError, "T\\+1"):
            validate_execution_effective_date("US", "2026-10-09", "2026-10-08")
        validate_execution_effective_date("CN", "2026-10-09", "2026-10-12")
        validate_execution_effective_date("HK", "2026-10-09", "2026-10-09")  # not covered by rule

    def test_ledger_identity_derives_next_open(self) -> None:
        report = {"model_qualification": {"CN": {"protocol_id": "protocol:cn:v1"}}}
        details = {"input_market_date": "2026-10-09", "decision": "CANDIDATES", "candidates": [], "watch_candidates": []}
        decision_id, digest, protocol_id, effective = _market_decision_identity(
            {"report_date": "2026-10-09"}, "CN", details, report
        )
        self.assertEqual("2026-10-12", effective)  # 10-10/11 are weekend, 10-09 Friday
        self.assertTrue(decision_id.startswith("decision:CN:"))
        self.assertEqual(64, len(digest))
        self.assertEqual("protocol:cn:v1", protocol_id)
