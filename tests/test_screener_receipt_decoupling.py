from __future__ import annotations

import unittest

from app.api.presentation.screener_receipt import render_screener_run_receipt
from app.services.stock_selection.screener_receipt import build_screener_run_receipt


class ScreenerReceiptDecouplingTests(unittest.TestCase):
    def test_service_falls_back_to_base_snapshot_and_preserves_lineage(self) -> None:
        calls: list[dict] = []

        def load_snapshot(params: dict) -> dict | None:
            calls.append(params)
            if len(calls) == 1:
                return None
            return {
                "id": 41,
                "source_job_id": 99,
                "created_at": "2026-10-02T18:00:00+08:00",
                "payload": {
                    "candidate_stats": {"returned_count": 12, "persisted_count": 60, "limit": 500}
                },
            }

        receipt = build_screener_run_receipt(
            params={"model_template": "technical_momentum", "market": "CN"},
            result_count=12,
            snapshot_ready=True,
            multi_templates_active=[],
            multi_screen_meta={},
            snapshot_loader=load_snapshot,
        )

        self.assertEqual(2, len(calls))
        self.assertEqual("base_precompute", receipt["source_kind"])
        self.assertEqual(41, receipt["snapshot"]["id"])
        self.assertEqual(12, receipt["result_count"])
        self.assertEqual(60, receipt["candidate_stats"]["persisted_count"])

    def test_multi_model_receipt_does_not_use_single_model_fallback(self) -> None:
        calls: list[dict] = []

        def load_snapshot(params: dict) -> None:
            calls.append(params)
            return None

        receipt = build_screener_run_receipt(
            params={"model_template": "technical_momentum", "market": "CN"},
            result_count=0,
            snapshot_ready=False,
            multi_templates_active=["technical_momentum", "lightgbm_top_picks"],
            multi_screen_meta={"available_templates": ["technical_momentum"]},
            snapshot_loader=load_snapshot,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual("page_result", receipt["source_kind"])
        self.assertTrue(receipt["multi_model"])

    def test_presenter_escapes_snapshot_lineage_values(self) -> None:
        page = render_screener_run_receipt(
            {
                "normalized_params": {"model_template": "technical_momentum", "market": "CN"},
                "source_kind": "exact",
                "snapshot": {
                    "id": "<script>alert(1)</script>",
                    "source_job_id": "<img src=x onerror=alert(1)>",
                },
                "snapshot_payload": {},
                "snapshot_ready": True,
                "result_count": 3,
                "param_digest": "safe-digest",
                "available_templates": [],
                "missing_templates": [],
                "multi_model": False,
                "candidate_stats": {},
                "regime_diagnostics": {},
            },
            lang="en",
        )

        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;script&gt;alert", page)
        self.assertIn("Run Receipt", page)


if __name__ == "__main__":
    unittest.main()
