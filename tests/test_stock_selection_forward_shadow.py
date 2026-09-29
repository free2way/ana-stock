from __future__ import annotations

from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

from app.services.stock_selection.feature_availability import PointInTimeFeatureRecord
from app.services.stock_selection.forward_shadow import (
    CNForwardShadowConfig,
    _effective_trade_date,
    build_cn_forward_shadow_snapshot,
)


TZ = ZoneInfo("Asia/Shanghai")


def _records(ticker: str, values: dict[str, float], available: datetime) -> list[PointInTimeFeatureRecord]:
    return [
        PointInTimeFeatureRecord(
            record_id=f"{ticker}:{feature_name}:v1",
            market="CN",
            ticker=ticker,
            feature_name=feature_name,
            value=value,
            event_time=available,
            available_time=available,
            ingested_time=available,
            source="fixture",
            revision_id="v1",
        )
        for feature_name, value in values.items()
    ]


class CNForwardShadowTests(unittest.TestCase):
    def test_effective_session_uses_same_open_only_before_the_market_cutoff(self) -> None:
        self.assertEqual(
            "2026-08-24",
            _effective_trade_date(datetime(2026, 8, 24, 8, 0, tzinfo=TZ)),
        )
        self.assertEqual(
            "2026-08-25",
            _effective_trade_date(datetime(2026, 8, 24, 18, 0, tzinfo=TZ)),
        )

    def test_builds_next_session_observations_but_always_abstains(self) -> None:
        feature_date = date(2026, 8, 21)
        available = datetime(2026, 8, 21, 18, 30, tzinfo=TZ)
        base = {
            "pe_ttm": 20.0,
            "dividend_yield": 0.02,
            "market_cap": 10_000_000_000.0,
            "roe_avg_3y": 0.12,
            "net_profit_yoy": 0.15,
            "revenue_yoy": 0.10,
            "debt_to_assets": 0.40,
        }
        stronger = {
            **base,
            "pe_ttm": 10.0,
            "dividend_yield": 0.04,
            "roe_avg_3y": 0.22,
            "net_profit_yoy": 0.30,
            "revenue_yoy": 0.20,
            "debt_to_assets": 0.20,
        }
        result = build_cn_forward_shadow_snapshot(
            [*_records("AAA", stronger, available), *_records("BBB", base, available)],
            universe=("AAA", "BBB"),
            feature_date=feature_date,
            decision_cutoff=datetime(2026, 8, 22, 10, 0, tzinfo=TZ),
            source_version="pit-forward-fixture-v1",
            revision_history_preserved=False,
            config=CNForwardShadowConfig(top_observation_count=2),
        )

        self.assertEqual("success", result.payload["status"])
        self.assertEqual("2026-08-24", result.payload["effective_trade_date"])
        self.assertEqual("PASS", result.payload["as_of_gate"])
        self.assertEqual("ABSTAIN", result.payload["shadow_decision"])
        self.assertEqual("uncalibrated_confirmation_window", result.payload["abstention_reason"])
        self.assertIn("revision_history_not_preserved", result.payload["promotion_blockers"])
        self.assertEqual("AAA", result.payload["top_observations"][0]["ticker"])
        self.assertEqual(2, len(result.score_rows))

    def test_closed_data_gate_persists_an_abstention_receipt(self) -> None:
        feature_date = date(2026, 8, 21)
        result = build_cn_forward_shadow_snapshot(
            [],
            universe=("AAA", "BBB"),
            feature_date=feature_date,
            decision_cutoff=datetime(2026, 8, 22, 10, 0, tzinfo=TZ),
            source_version="pit-forward-empty-v1",
            revision_history_preserved=False,
        )

        self.assertEqual("collecting", result.payload["status"])
        self.assertEqual("COLLECTING", result.payload["as_of_gate"])
        self.assertEqual("point_in_time_data_gate_closed", result.payload["abstention_reason"])
        self.assertEqual((), result.score_rows)


if __name__ == "__main__":
    unittest.main()
