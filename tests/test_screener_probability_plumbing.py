"""Reliability weights + calibrated probability must survive the screener pipeline.

The fusion layer (``multi_model_confluence``) already knew how to weight models
and abstain on uncalibrated probabilities, but the request whitelist dropped
``min_hit_probability`` / ``probability_calibration`` / ``model_reliability_weights``
and the snapshot compaction stripped the fused fields. These tests pin the
plumbing so the feature is actually reachable end to end.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from app.api.routes import screener as screener_route
from app.services import screener_snapshots
from app.services.screener import ScreenerService
from app.services.stock_selection import screener_query
from tests.artifact_isolation import IsolatedArtifactsTestCase

HIGH = "lightgbm_top_picks"
MID = "technical_momentum"
LOW = "next_tesla_swing"

CALIBRATION_SPEC = {
    "method": "isotonic",
    "source": "plumbing_unit_test_v1",
    "samples": [[0.1, 0], [0.5, 0], [0.9, 1], [0.95, 1]],
}


def _row(ticker: str, *, score: float, action: str = "pullback") -> dict:
    return {
        "ticker": ticker,
        "market": "CN",
        "snapshot_score": score,
        "trend_score": score,
        "action_label": action,
        "model_execution_tags": [],
        "tradability_status": "READY",
        "trade_readiness_score": 80.0,
    }


class ParamWhitelistTests(unittest.TestCase):
    def _base(self, **overrides: object) -> dict:
        params = {
            "model_template": HIGH,
            "universe": "full_market",
            "market": "CN",
            "lang": "zh",
        }
        params.update(overrides)
        return params

    def test_json_specs_are_parsed_into_dicts(self) -> None:
        normalized = screener_query.normalize_screen_params(
            self._base(
                model_reliability_weights=json.dumps({HIGH: 0.9, LOW: 0.1}),
                probability_calibration=json.dumps(CALIBRATION_SPEC),
                min_hit_probability="0.6",
            )
        )

        self.assertEqual({HIGH: 0.9, LOW: 0.1}, normalized["model_reliability_weights"])
        self.assertEqual(CALIBRATION_SPEC, normalized["probability_calibration"])
        self.assertAlmostEqual(0.6, normalized["min_hit_probability"])

    def test_mapping_inputs_pass_through(self) -> None:
        normalized = screener_query.normalize_screen_params(
            self._base(model_reliability_weights={MID: 1.0}, probability_calibration=dict(CALIBRATION_SPEC))
        )

        self.assertEqual({MID: 1.0}, normalized["model_reliability_weights"])
        self.assertEqual(CALIBRATION_SPEC, normalized["probability_calibration"])

    def test_defaults_are_none(self) -> None:
        normalized = screener_query.normalize_screen_params(self._base())

        self.assertIsNone(normalized["min_hit_probability"])
        self.assertIsNone(normalized["probability_calibration"])
        self.assertIsNone(normalized["model_reliability_weights"])

    def test_malformed_json_fails_fast(self) -> None:
        for field in ("probability_calibration", "model_reliability_weights"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    screener_query.normalize_screen_params(self._base(**{field: "{not json"}))

    def test_non_object_json_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "probability_calibration"):
            screener_query.normalize_screen_params(self._base(probability_calibration="[1, 2, 3]"))

    def test_non_numeric_probability_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_hit_probability"):
            screener_query.normalize_screen_params(self._base(min_hit_probability="high"))

    def test_out_of_range_probability_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "min_hit_probability"):
            screener_query.normalize_screen_params(self._base(min_hit_probability=1.5))


class RouteWhitelistTests(unittest.TestCase):
    def test_current_params_carries_opt_in_specs(self) -> None:
        params = screener_route._current_params(
            lang="zh",
            model_template=HIGH,
            universe="full_market",
            market="CN",
            min_trend_score=60,
            action_filter="ALL",
            min_volume_ratio=0.0,
            min_listing_days=365,
            pe_min=0.0,
            pe_max=30.0,
            min_roe_avg_3y=12.0,
            min_net_profit_yoy=20.0,
            min_revenue_yoy=0.0,
            max_debt_to_assets=100.0,
            min_dividend_yield=0.0,
            exclude_bottom_market_cap_pct=10.0,
            recent_snapshot_runs=0,
            min_snapshot_hits=0,
            model_signal_filter="ALL",
            min_model_signal_strength=0.0,
            execution_tag_filter="ALL",
            exclude_execution_tag_filter="ALL",
            sort_by="weighted_score",
            sort_order="desc",
            min_hit_probability=0.5,
            probability_calibration=json.dumps(CALIBRATION_SPEC),
            model_reliability_weights=json.dumps({HIGH: 0.9}),
        )

        self.assertEqual(0.5, params["min_hit_probability"])
        self.assertEqual(json.dumps(CALIBRATION_SPEC), params["probability_calibration"])
        self.assertEqual(json.dumps({HIGH: 0.9}), params["model_reliability_weights"])

    def test_omitted_opt_in_specs_are_absent(self) -> None:
        params = screener_route._current_params(
            lang="en",
            model_template=HIGH,
            universe="full_market",
            market="CN",
            min_trend_score=60,
            action_filter="ALL",
            min_volume_ratio=0.0,
            min_listing_days=365,
            pe_min=0.0,
            pe_max=30.0,
            min_roe_avg_3y=12.0,
            min_net_profit_yoy=20.0,
            min_revenue_yoy=0.0,
            max_debt_to_assets=100.0,
            min_dividend_yield=0.0,
            exclude_bottom_market_cap_pct=10.0,
            recent_snapshot_runs=0,
            min_snapshot_hits=0,
            model_signal_filter="ALL",
            min_model_signal_strength=0.0,
            execution_tag_filter="ALL",
            exclude_execution_tag_filter="ALL",
            sort_by="default",
            sort_order="desc",
        )

        self.assertNotIn("min_hit_probability", params)
        self.assertNotIn("probability_calibration", params)
        self.assertNotIn("model_reliability_weights", params)


class AggregationPlumbingTests(IsolatedArtifactsTestCase, unittest.TestCase):
    """Once whitelisted, the three params must actually change the fusion."""

    def setUp(self) -> None:
        super().setUp()
        self.template_rows = {
            HIGH: [_row("ALPHA", score=0.05), _row("OMEGA", score=0.95)],
            LOW: [_row("ALPHA", score=0.05), _row("OMEGA", score=0.95)],
        }

    def _run(self, **overrides: object) -> tuple[list[dict], dict]:
        params = {
            "model_template": HIGH,
            "universe": "full_market",
            "market": "CN",
            "multi_model_templates": [HIGH, LOW],
            "min_multi_model_hits": 2,
            "confluence_action_filter": "ALL",
            "lang": "zh",
        }
        params.update(overrides)
        normalized = screener_query.normalize_screen_params(params)

        def screen_rows_loader(_service, local_params):
            key = str(local_params.get("model_template") or "")
            return [dict(row) for row in self.template_rows.get(key, [])], True

        rows, _ready, meta = screener_query.run_multi_screen(
            MagicMock(),
            normalized,
            snapshot_loader=lambda _params: None,
            screen_rows_loader=screen_rows_loader,
        )
        return rows, meta

    def test_provided_weights_reach_the_fusion(self) -> None:
        rows, meta = self._run(
            model_reliability_weights=json.dumps({HIGH: 0.9, LOW: 0.1}),
        )

        self.assertEqual("provided_model_reliability_weights", meta["weight_source"])
        self.assertEqual({HIGH: 0.9, LOW: 0.1}, meta["model_weights"])
        by_ticker = {row["ticker"]: row for row in rows}
        self.assertAlmostEqual(0.9, by_ticker["ALPHA"]["model_weights"][HIGH])
        self.assertAlmostEqual(0.1, by_ticker["ALPHA"]["model_weights"][LOW])

    def test_calibration_and_gate_produce_an_abstention_subset(self) -> None:
        ungated, ungated_meta = self._run(
            probability_calibration=json.dumps(CALIBRATION_SPEC),
        )
        gated, gated_meta = self._run(
            probability_calibration=json.dumps(CALIBRATION_SPEC),
            min_hit_probability=0.5,
        )

        self.assertEqual("calibrated", ungated_meta["calibration_status"])
        ungated_tickers = {row["ticker"] for row in ungated}
        gated_tickers = {row["ticker"] for row in gated}
        self.assertEqual({"ALPHA", "OMEGA"}, ungated_tickers)
        self.assertTrue(gated_tickers.issubset(ungated_tickers))
        self.assertEqual({"OMEGA"}, gated_tickers)
        self.assertTrue(gated_meta["probability_gate"]["enabled"])
        for row in gated:
            self.assertIsNotNone(row["expected_hit_probability"])
            self.assertEqual("calibrated", row["calibration_status"])

    def test_uncalibrated_rows_abstain_when_gate_enabled(self) -> None:
        rows, meta = self._run(min_hit_probability=0.5)

        self.assertEqual([], rows)
        self.assertEqual(2, meta["probability_gate"]["excluded_uncalibrated"])


class WeightedSortTests(IsolatedArtifactsTestCase, unittest.TestCase):
    def test_weighted_score_order_is_deterministic_and_input_order_independent(self) -> None:
        template_rows = {
            HIGH: [_row("AAA", score=60.0)],
            MID: [_row("AAA", score=60.0), _row("BBB", score=60.0)],
            LOW: [_row("BBB", score=60.0)],
        }
        params = screener_query.normalize_screen_params(
            {
                "model_template": HIGH,
                "universe": "full_market",
                "market": "CN",
                "multi_model_templates": [HIGH, MID, LOW],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "ALL",
                "sort_by": "weighted_score",
                "sort_order": "desc",
                "model_reliability_weights": json.dumps({HIGH: 0.9, MID: 0.5, LOW: 0.1}),
                "lang": "zh",
            }
        )

        def loader(reverse: bool):
            def _inner(_service, local_params):
                key = str(local_params.get("model_template") or "")
                rows = [dict(row) for row in template_rows.get(key, [])]
                if reverse:
                    rows = list(reversed(rows))
                return rows, True

            return _inner

        forward, _ready, _meta = screener_query.run_multi_screen(
            MagicMock(), dict(params), snapshot_loader=lambda _p: None, screen_rows_loader=loader(False)
        )
        backward, _ready, _meta = screener_query.run_multi_screen(
            MagicMock(), dict(params), snapshot_loader=lambda _p: None, screen_rows_loader=loader(True)
        )

        self.assertEqual(["AAA", "BBB"], [row["ticker"] for row in forward])
        self.assertEqual(["AAA", "BBB"], [row["ticker"] for row in backward])
        by_ticker = {row["ticker"]: row for row in forward}
        self.assertGreater(by_ticker["AAA"]["weighted_score"], by_ticker["BBB"]["weighted_score"])


class SnapshotRoundTripTests(IsolatedArtifactsTestCase, unittest.TestCase):
    REQUIRED_FIELDS = (
        "weighted_score",
        "weighted_score_normalized",
        "expected_hit_probability",
        "calibration_status",
        "score_source",
        "model_weights",
    )

    def test_whitelist_contains_the_fusion_fields(self) -> None:
        for field in self.REQUIRED_FIELDS:
            self.assertIn(field, screener_snapshots.SNAPSHOT_ROW_FIELDS)

    def test_fusion_fields_survive_compaction_and_read_back(self) -> None:
        row = {
            "ticker": "600000.SS",
            "weighted_score": 1.4,
            "weighted_score_normalized": 0.7,
            "expected_hit_probability": 0.62,
            "calibration_status": "calibrated",
            "weight_source": "provided_model_reliability_weights",
            "model_weights": {HIGH: 0.9, LOW: 0.5},
            "score_source": "lightgbm_prediction_v1:percentile_0_100",
            "unrelated": "dropped",
        }
        compacted = screener_snapshots._compact_snapshot_rows([row], limit=1)[0]

        self.assertNotIn("unrelated", compacted)
        self.assertEqual(1.4, compacted["weighted_score"])
        self.assertEqual(0.7, compacted["weighted_score_normalized"])
        self.assertEqual(0.62, compacted["expected_hit_probability"])
        self.assertEqual("calibrated", compacted["calibration_status"])
        self.assertEqual("provided_model_reliability_weights", compacted["weight_source"])
        self.assertEqual({HIGH: 0.9, LOW: 0.5}, compacted["model_weights"])
        self.assertEqual("lightgbm_prediction_v1:percentile_0_100", compacted["score_source"])

    def test_multi_model_precompute_builder_persists_fusion_fields(self) -> None:
        template_rows = {
            HIGH: [_row("ALPHA", score=0.05), _row("OMEGA", score=0.95)],
            LOW: [_row("ALPHA", score=0.05), _row("OMEGA", score=0.95)],
        }
        params = {
            "model_template": HIGH,
            "multi_model_templates": [HIGH, LOW],
            "min_multi_model_hits": 2,
            "confluence_action_filter": "ALL",
            "market": "CN",
            "universe": "full_market",
            "sort_by": "weighted_score",
            "sort_order": "desc",
            "probability_calibration": CALIBRATION_SPEC,
            "model_reliability_weights": {HIGH: 0.9, LOW: 0.1},
            "lang": "zh",
        }

        def loader(local_params):
            key = str(local_params.get("model_template") or "")
            rows = [dict(row) for row in template_rows.get(key, [])]
            return rows or None

        with patch.object(screener_snapshots, "load_exact_screener_snapshot_rows", side_effect=loader):
            rows, meta = screener_snapshots._build_multi_screen_rows_from_snapshots(params)

        self.assertEqual("provided_model_reliability_weights", meta["weight_source"])
        self.assertEqual("calibrated", meta["calibration_status"])
        persisted = screener_snapshots._compact_snapshot_rows(rows, limit=10)
        by_ticker = {row["ticker"]: row for row in persisted}
        self.assertEqual({"ALPHA", "OMEGA"}, set(by_ticker))
        for row in persisted:
            self.assertIsNotNone(row["weighted_score"])
            self.assertIsNotNone(row["weighted_score_normalized"])
            self.assertIsNotNone(row["expected_hit_probability"])
            self.assertEqual("calibrated", row["calibration_status"])
            self.assertEqual({HIGH: 0.9, LOW: 0.1}, row["model_weights"])

    def test_compacted_rows_remain_sortable_by_weighted_score(self) -> None:
        rows = [
            {**{key: 0.1 for key in self.REQUIRED_FIELDS}, "ticker": "LOW", "weighted_score": 0.2, "trend_score": 10},
            {**{key: 0.1 for key in self.REQUIRED_FIELDS}, "ticker": "HIGH", "weighted_score": 0.9, "trend_score": 10},
        ]
        compacted = screener_snapshots._compact_snapshot_rows(rows, limit=10)
        params = screener_query.normalize_screen_params(
            {"model_template": HIGH, "universe": "full_market", "market": "CN", "sort_by": "weighted_score"}
        )
        params.update({
            "min_trend_score": 0,
            "min_listing_days": 0,
            "min_volume_ratio": 0.0,
            "exclude_bottom_market_cap_pct": 0.0,
            "recent_snapshot_runs": 0,
            "min_snapshot_hits": 0,
        })
        # A DB-free service instance: sort/filter helpers are pure row transforms.
        service = ScreenerService.__new__(ScreenerService)
        ranked = screener_query._rank_precomputed_rows(service, compacted, params)

        self.assertEqual(["HIGH", "LOW"], [row["ticker"] for row in ranked])


class DisplayTests(unittest.TestCase):
    def test_model_cell_blank_when_uncalibrated_and_shown_when_calibrated(self) -> None:
        base_row = {
            "model_summary": "LightGBM",
            "model_score": 0.5,
        }
        uncalibrated = screener_route._model_cell({**base_row, "expected_hit_probability": None}, "en")
        self.assertNotIn("Hit probability", uncalibrated)

        calibrated = screener_route._model_cell({**base_row, "expected_hit_probability": 0.62}, "en")
        self.assertIn("Hit probability 62.0%", calibrated)


if __name__ == "__main__":
    unittest.main()
