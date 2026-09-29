from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.services.stock_selection.screener_regime import build_screener_regime_diagnostics
from app.services.screener_snapshots import refresh_precomputed_screener_snapshots


_DEFAULT = object()


class ScreenerRegimeDiagnosticsTests(TestCase):
    def snapshot(self, **changes):
        return {
            "market": "CN",
            "snapshot_date": "2026-09-18",
            "generated_at": "2026-09-18T18:00:00+08:00",
            "risk_regime": "risk_on",
            "buy_gate": "ALLOW",
            "max_position_scale": 1.0,
            **changes,
        }

    def rows(self):
        return [
            {"ticker": f"60000{i}.SS", "tradability_status": "READY", "is_tradable": True}
            for i in range(6)
        ] + [
            {"ticker": "430001.BJ", "tradability_status": "READY", "is_tradable": True},
            {"ticker": "000001.SZ", "tradability_status": "BLOCKED", "is_tradable": False},
        ]

    def assess(self, rows=None, snapshot=_DEFAULT, **changes):
        return build_screener_regime_diagnostics(
            self.rows() if rows is None else rows,
            market="CN",
            input_market_date="2026-09-18",
            decision_cutoff_at="2026-09-18T19:00:00+08:00",
            regime_snapshot=self.snapshot() if snapshot is _DEFAULT else snapshot,
            **changes,
        )

    def test_risk_on_keeps_observations_but_caps_research_shortlist(self):
        rows = self.rows()
        original = deepcopy(rows)
        result = self.assess(rows=rows)
        self.assertEqual("READY", result["status"])
        self.assertEqual(7, result["observation_count"])
        self.assertEqual(1, result["excluded_bj_count"])
        self.assertEqual(6, result["tradability_ready_count"])
        self.assertEqual(5, result["regime_shortlist_count"])
        self.assertEqual(0, result["formal_candidate_count"])
        self.assertEqual("research_observation_not_trade_authorization", result["candidate_semantics"])
        self.assertEqual(original, rows)

    def test_crash_missing_or_stale_snapshot_has_zero_shortlist(self):
        for snapshot in (None, self.snapshot(risk_regime="crash", buy_gate="BLOCK", max_position_scale=0.0),
                         self.snapshot(snapshot_date="2026-09-17")):
            result = self.assess(snapshot=snapshot)
            self.assertEqual("BLOCKED", result["status"])
            self.assertEqual(0, result["regime_shortlist_count"])
            self.assertEqual(7, result["observation_count"])

    def test_us_is_explicitly_not_enabled_and_never_claims_candidates(self):
        result = build_screener_regime_diagnostics(
            [{"ticker": "AAPL"}], market="US", input_market_date="2026-09-18",
            decision_cutoff_at="2026-09-18T18:00:00-04:00", regime_snapshot=None,
        )
        self.assertEqual("NOT_ENABLED", result["status"])
        self.assertEqual(0, result["formal_candidate_count"])
        self.assertEqual(1, result["observation_count"])

    def test_precomputed_snapshot_persists_same_regime_diagnostics(self):
        params = {"market": "CN", "universe": "full_market", "model_template": "technical_momentum", "limit": 5000}
        rows = self.rows()
        repo = MagicMock()
        repo.get_latest_snapshot.return_value = {"payload": self.snapshot()}
        repo.create_snapshot.return_value = SimpleNamespace(id=7)
        session = MagicMock()
        session.__enter__.return_value = MagicMock()
        with patch("app.services.screener_snapshots.build_lake_precompute_screener_params", return_value=[params]), \
             patch("app.services.screener_snapshots._screen_with_lake_preferred", return_value=rows), \
             patch("app.services.screener_snapshots._snapshot_input_meta", return_value={
                 "market": "CN", "input_as_of_date": "2026-09-18", "required_as_of_date": "2026-09-18",
             }), \
             patch("app.services.screener_snapshots.app_now_iso", return_value="2026-09-18T19:00:00+08:00"), \
             patch("app.services.screener_snapshots.SessionLocal", return_value=session), \
             patch("app.services.screener_snapshots.WorkspaceSnapshotRepository", return_value=repo):
            result = refresh_precomputed_screener_snapshots(MagicMock(), lake_only=True)
        self.assertEqual("success", result["status"])
        payload = repo.create_snapshot.call_args.kwargs["payload"]
        self.assertEqual("READY", payload["regime_diagnostics"]["status"])
        self.assertEqual(5, payload["regime_diagnostics"]["regime_shortlist_count"])
        self.assertEqual(0, payload["regime_diagnostics"]["formal_candidate_count"])
