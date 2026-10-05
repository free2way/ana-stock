from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.api.routes import screener as screener_route
from app.services import screener_snapshots
from app.services.stock_selection.multi_model_confluence import aggregate_multi_model_rows
from tests.artifact_isolation import IsolatedArtifactsTestCase


class MultiModelConfluenceDecouplingTests(IsolatedArtifactsTestCase, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.template_keys = ["lightgbm_top_picks", "technical_momentum"]
        self.template_rows = {
            "lightgbm_top_picks": [
                {
                    "ticker": "600000.SS",
                    "market": "CN",
                    "snapshot_score": 60.0,
                    "trend_score": 65.0,
                    "action_label": "pullback",
                    "selection_reason": "model rank",
                    "model_execution_tags": [],
                    "tradability_status": "READY",
                    "trade_readiness_score": 80.0,
                }
            ],
            "technical_momentum": [
                {
                    "ticker": "600000.SS",
                    "market": "CN",
                    "snapshot_score": 88.0,
                    "trend_score": 86.0,
                    "action_label": "breakout",
                    "selection_reason": "momentum",
                    "model_execution_tags": [],
                    "tradability_status": "READY",
                    "trade_readiness_score": 84.0,
                }
            ],
        }
        self.params = {
            "model_template": self.template_keys[0],
            "multi_model_templates": self.template_keys,
            "min_multi_model_hits": 2,
            "confluence_action_filter": "ALL",
            "strategy_profile": "",
            "market": "CN",
            "universe": "full_market",
            "sort_by": "confluence_rank",
            "sort_order": "desc",
            "limit": 500,
            "lang": "zh",
        }

    def test_online_fallback_and_precompute_use_identical_aggregation(self) -> None:
        def route_rows(_service, local_params):
            template_key = str(local_params.get("model_template") or "")
            return [dict(row) for row in self.template_rows.get(template_key, [])], True

        def snapshot_rows(local_params):
            template_key = str(local_params.get("model_template") or "")
            return [dict(row) for row in self.template_rows.get(template_key, [])]

        with (
            patch.object(screener_route, "_load_screener_snapshot", return_value=None),
            patch.object(screener_route, "_load_screen_rows_from_snapshot", side_effect=route_rows),
            patch.object(screener_snapshots, "load_exact_screener_snapshot_rows", side_effect=snapshot_rows),
        ):
            online_rows, online_ready, online_meta = screener_route._run_multi_screen(
                MagicMock(),
                dict(self.params),
            )
            precomputed_rows, precomputed_meta = screener_snapshots._build_multi_screen_rows_from_snapshots(
                dict(self.params)
            )

        self.assertTrue(online_ready)
        self.assertEqual(precomputed_meta, online_meta)
        self.assertEqual(precomputed_rows, online_rows)
        self.assertEqual(["600000.SS"], [row["ticker"] for row in online_rows])
        self.assertEqual(2, online_rows[0]["model_hit_count"])

    def test_both_entry_points_reference_the_same_domain_aggregator(self) -> None:
        self.assertIs(aggregate_multi_model_rows, screener_route.aggregate_multi_model_rows)
        self.assertIs(aggregate_multi_model_rows, screener_snapshots.aggregate_multi_model_rows)

    def test_risk_downgrade_prevents_false_breakout_confluence(self) -> None:
        rows = {
            "lightgbm_top_picks": [
                {
                    "ticker": "RISKY",
                    "action_label": "breakout",
                    "snapshot_score": 80,
                    "risk_flags": ["do-not-chase"],
                }
            ],
            "technical_momentum": [
                {
                    "ticker": "RISKY",
                    "action_label": "breakout",
                    "snapshot_score": 70,
                    "risk_flags": [],
                }
            ],
        }
        params = {
            **self.params,
            "confluence_action_filter": "breakout_confirmation",
        }

        filtered, _meta = aggregate_multi_model_rows(
            rows,
            template_keys=self.template_keys,
            params=params,
        )
        unfiltered, _meta = aggregate_multi_model_rows(
            rows,
            template_keys=self.template_keys,
            params={**params, "confluence_action_filter": "ALL"},
        )

        self.assertEqual([], filtered)
        self.assertEqual(["RISKY"], [row["ticker"] for row in unfiltered])
        self.assertEqual(1, unfiltered[0]["matched_action_bucket_hits"]["breakout_confirmation"])
        self.assertEqual(1, unfiltered[0]["matched_action_bucket_hits"]["watchlist"])

    def test_aggregation_does_not_mutate_source_rows_and_reports_missing_templates(self) -> None:
        source = {
            "lightgbm_top_picks": [dict(self.template_rows["lightgbm_top_picks"][0])],
        }
        original = {
            key: [dict(row) for row in rows]
            for key, rows in source.items()
        }

        rows, meta = aggregate_multi_model_rows(
            source,
            template_keys=self.template_keys,
            params=self.params,
        )

        self.assertEqual([], rows)
        self.assertEqual(original, source)
        self.assertEqual(["lightgbm_top_picks"], meta["available_templates"])
        self.assertEqual(["technical_momentum"], meta["missing_templates"])

    def test_structured_confluence_fields_survive_snapshot_compaction(self) -> None:
        rows, _meta = aggregate_multi_model_rows(
            self.template_rows,
            template_keys=self.template_keys,
            params=self.params,
        )

        compacted = screener_snapshots._compact_snapshot_rows(rows, limit=10)

        self.assertEqual(["pullback", "breakout"], compacted[0]["matched_action_labels"])
        self.assertEqual(74.0, compacted[0]["confluence_score_mean"])


if __name__ == "__main__":
    unittest.main()
