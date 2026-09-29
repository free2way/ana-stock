from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.services.backtesting import DailyBar
from app.services.execution_costs import FillCostModel
from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.paper_simulation import run_frozen_decision_paper_simulation
from app.services.stock_selection.protocol import ExecutableSelectionProtocol


def _bar(day: str, price: float) -> DailyBar:
    return DailyBar("AAA", day, price, price, price, price, 100_000)


class PaperSimulationP0Tests(TestCase):
    def setUp(self):
        self.protocol = ExecutableSelectionProtocol(
            market="US", horizon_days=1, initial_cash=10_000,
            max_position_weight=1, max_sector_weight=1,
            max_gross_exposure=1, max_participation_rate=1,
            commission_bps_one_way=0, slippage_bps_one_way=0,
            approved=True, approved_by="owner", approved_at="2026-01-05T21:00:00+00:00",
        )
        self.payload = {
            "frozen_at": "2026-01-05T21:00:00+00:00",
            "markets": {"US": {"protocol_id": self.protocol.protocol_id,
                               "input_market_date": "2026-01-05",
                               "decision": "CANDIDATES",
                               "candidates": [{"ticker": "AAA", "model_score": 1}]}}}
        self.bars = [_bar("2026-01-05", 10), DailyBar("AAA", "2026-01-06", 10, 11, 10, 11, 100_000)]

    def test_replay_is_idempotent_and_matured_outcome_matches_account(self):
        with TemporaryDirectory() as root:
            store = JsonPayloadArtifactStore(Path(root))
            first = run_frozen_decision_paper_simulation(
                decision_id="decision:US:example", decision_payload=self.payload,
                protocol=self.protocol, bars=self.bars,
                calendar_sessions=["2026-01-05", "2026-01-06"], store=store,
            )
            second = run_frozen_decision_paper_simulation(
                decision_id="decision:US:example", decision_payload=self.payload,
                protocol=self.protocol, bars=self.bars,
                calendar_sessions=["2026-01-05", "2026-01-06"], store=store,
            )
            self.assertEqual(first["simulation_id"], second["simulation_id"])
            self.assertEqual("MATURED", first["summary"]["status"])
            self.assertEqual(11_000, first["summary"]["end_nav"])
            artifact = store.read(first["artifact"])
            self.assertEqual(2, len(artifact["fills"]))
            self.assertEqual("matured", artifact["outcomes"][0]["status"])
            self.assertEqual(FillCostModel(0, 0).model_hash, artifact["summary"]["cost_model"]["hash"])
            self.assertEqual(artifact["summary"]["cost_model"], first["summary"]["cost_model"])

    def test_abstain_stays_cash_and_late_cutoff_cannot_claim_next_open(self):
        with TemporaryDirectory() as root:
            store = JsonPayloadArtifactStore(Path(root))
            abstain = {**self.payload, "markets": {"US": {
                "protocol_id": self.protocol.protocol_id,
                "input_market_date": "2026-01-05", "decision": "ABSTAIN", "candidates": [],
            }}}
            result = run_frozen_decision_paper_simulation(
                decision_id="decision:US:abstain", decision_payload=abstain,
                protocol=self.protocol, bars=self.bars,
                calendar_sessions=["2026-01-05", "2026-01-06"], store=store,
            )
            self.assertEqual("ABSTAIN", result["summary"]["status"])
            self.assertEqual(10_000, result["summary"]["end_nav"])
            late = {**self.payload, "frozen_at": "2026-01-06T15:00:00+00:00"}
            result = run_frozen_decision_paper_simulation(
                decision_id="decision:US:late", decision_payload=late,
                protocol=self.protocol, bars=self.bars,
                calendar_sessions=["2026-01-05", "2026-01-06"], store=store,
            )
            self.assertEqual("LATE_CUTOFF", result["summary"]["status"])
            self.assertEqual(0, result["summary"]["signal_count"])

    def test_unapproved_or_mismatched_protocol_is_blocked(self):
        with TemporaryDirectory() as root:
            store = JsonPayloadArtifactStore(Path(root))
            with self.assertRaises(PermissionError):
                run_frozen_decision_paper_simulation(
                    decision_id="decision:US:example", decision_payload=self.payload,
                    protocol=replace(self.protocol, approved=False, approved_by=None, approved_at=None),
                    bars=self.bars, calendar_sessions=["2026-01-05", "2026-01-06"], store=store,
                )
            with self.assertRaisesRegex(ValueError, "does not match"):
                run_frozen_decision_paper_simulation(
                    decision_id="decision:US:example", decision_payload=self.payload,
                    protocol=replace(self.protocol, horizon_days=2),
                    bars=self.bars, calendar_sessions=["2026-01-05", "2026-01-06"], store=store,
                )
