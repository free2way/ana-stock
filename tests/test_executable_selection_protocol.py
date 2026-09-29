from dataclasses import replace
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from app.services.stock_selection.labels import PriceBar, build_executable_label
from app.services.stock_selection.protocol import (
    ExecutableSelectionProtocol,
    persist_protocol,
    protocol_change_classification,
)


class ExecutableSelectionProtocolTests(TestCase):
    def test_semantic_changes_invalidate_protocol_but_approval_does_not(self):
        original = ExecutableSelectionProtocol(market="CN", feature_set_version="basic_v1")
        approved = replace(original, approved=True, approved_by="owner", approved_at="2026-09-13T12:00:00+08:00")
        self.assertEqual(original.protocol_id, approved.protocol_id)
        with self.assertRaises(PermissionError):
            original.require_approved()
        approved.require_approved()
        for changed in (
            replace(original, horizon_days=6),
            replace(original, commission_bps_one_way=9),
            replace(original, feature_set_version="basic_v2"),
            replace(original, minimum_expected_net_return=0.01),
            replace(original, max_position_weight=0.09),
        ):
            self.assertNotEqual(original.protocol_id, changed.protocol_id)
            self.assertTrue(protocol_change_classification(original, changed)["invalidates_prior_effectiveness_evidence"])

    def test_protocol_artifact_can_be_verified(self):
        protocol = ExecutableSelectionProtocol(market="US", universe_version="historical_v1")
        with TemporaryDirectory() as root:
            receipt = persist_protocol(protocol, artifact_root=Path(root))
            self.assertEqual(protocol.protocol_id, receipt["protocol_id"])
            self.assertTrue(receipt["artifact"])

    def test_fixed_horizon_net_profit_golden_loss_not_intraperiod_peak(self):
        bars = [
            PriceBar(date(2026, 1, 5), 100, 100, 100, 100),
            PriceBar(date(2026, 1, 6), 100, 110, 100, 105),
            PriceBar(date(2026, 1, 7), 96, 100, 96, 96),
        ]
        result = build_executable_label(
            bars, signal_index=0, horizon_days=2, round_trip_cost_bps=40,
            drawdown_penalty=0, market_return=0, industry_return=0,
        )
        self.assertAlmostEqual(-0.044, result.net_return, places=10)
        self.assertFalse(result.net_return > 0)
