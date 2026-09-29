from __future__ import annotations

from datetime import date, datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from app.services.stock_selection.feature_availability import (
    FUNDAMENTAL_FEATURE_NAMES,
    PointInTimeFeatureRecord,
)
from app.services.stock_selection.point_in_time_features import (
    PointInTimeFeatureJoinConfig,
    build_point_in_time_feature_join,
    merge_point_in_time_features,
)


TZ = ZoneInfo("Asia/Shanghai")


def _record(
    ticker: str,
    feature_name: str,
    value: float,
    *,
    event_time: datetime,
    available_time: datetime,
    revision: str = "v1",
) -> PointInTimeFeatureRecord:
    return PointInTimeFeatureRecord(
        record_id=f"{ticker}:{feature_name}:{revision}",
        market="CN",
        ticker=ticker,
        feature_name=feature_name,
        value=value,
        event_time=event_time,
        available_time=available_time,
        ingested_time=available_time,
        source="fixture",
        revision_id=revision,
    )


def _complete_records(
    ticker: str,
    *,
    event_time: datetime,
    available_time: datetime,
) -> list[PointInTimeFeatureRecord]:
    values = {
        "pe_ttm": 20.0,
        "dividend_yield": 0.02,
        "market_cap": 10_000_000_000.0,
        "roe_avg_3y": 0.12,
        "net_profit_yoy": 0.15,
        "revenue_yoy": 0.10,
        "debt_to_assets": 0.40,
    }
    return [
        _record(
            ticker,
            feature_name,
            value,
            event_time=event_time,
            available_time=available_time,
        )
        for feature_name, value in values.items()
    ]


class PointInTimeFeatureJoinTests(unittest.TestCase):
    def test_future_revision_is_excluded_and_derived_values_are_safe(self) -> None:
        feature_date = date(2026, 8, 21)
        available = datetime(2026, 8, 21, 15, 0, tzinfo=TZ)
        records = [
            *_complete_records("AAA", event_time=available, available_time=available),
            *_complete_records("BBB", event_time=available, available_time=available),
            _record(
                "AAA",
                "pe_ttm",
                5.0,
                event_time=available,
                available_time=datetime(2026, 8, 21, 17, 0, tzinfo=TZ),
                revision="future",
            ),
        ]
        result = build_point_in_time_feature_join(
            records,
            universe_by_date={feature_date: ("AAA", "BBB")},
            source_version="pit-fixture-v1",
            config=PointInTimeFeatureJoinConfig(market="CN"),
        )
        self.assertEqual((feature_date,), result.enabled_dates)
        self.assertEqual(20.0, result.features_by_key[("AAA", feature_date)]["pe_ttm"])
        self.assertAlmostEqual(
            0.05,
            result.features_by_key[("AAA", feature_date)]["positive_earnings_yield"],
        )
        self.assertAlmostEqual(
            10.0,
            result.features_by_key[("AAA", feature_date)]["market_cap_log"],
        )

    def test_stale_daily_values_disable_the_entire_group(self) -> None:
        collected_date = date(2026, 8, 1)
        evaluation_date = date(2026, 8, 10)
        available = datetime(2026, 8, 1, 15, 0, tzinfo=TZ)
        records = _complete_records("AAA", event_time=available, available_time=available)
        result = build_point_in_time_feature_join(
            records,
            universe_by_date={evaluation_date: ("AAA",)},
            source_version="pit-fixture-v1",
            config=PointInTimeFeatureJoinConfig(market="CN"),
        )
        self.assertEqual((), result.enabled_dates)
        self.assertEqual((evaluation_date,), result.disabled_dates)
        self.assertEqual({}, result.features_by_key)
        self.assertEqual(
            0.0,
            result.date_coverage[0].feature_coverage["pe_ttm"],
        )

    def test_low_cross_section_coverage_does_not_leak_partial_group(self) -> None:
        feature_date = date(2026, 8, 21)
        available = datetime(2026, 8, 21, 15, 0, tzinfo=TZ)
        result = build_point_in_time_feature_join(
            _complete_records("AAA", event_time=available, available_time=available),
            universe_by_date={feature_date: ("AAA", "BBB")},
            source_version="pit-fixture-v1",
            config=PointInTimeFeatureJoinConfig(
                market="CN",
                minimum_cross_section_coverage=0.60,
            ),
        )
        self.assertFalse(result.date_coverage[0].enabled)
        merged = merge_point_in_time_features(
            {
                ("AAA", feature_date): {"momentum_5d": 0.1},
                ("BBB", feature_date): {"momentum_5d": 0.2},
            },
            result,
        )
        self.assertEqual({"momentum_5d": 0.1}, merged[("AAA", feature_date)])
        self.assertEqual({"momentum_5d": 0.2}, merged[("BBB", feature_date)])

    def test_financial_values_can_persist_while_daily_valuation_is_refreshed(self) -> None:
        first_date = date(2026, 8, 1)
        second_date = date(2026, 8, 8)
        first_available = datetime(2026, 8, 1, 15, 0, tzinfo=TZ)
        refreshed = datetime(2026, 8, 8, 15, 0, tzinfo=TZ)
        records = _complete_records(
            "AAA",
            event_time=first_available,
            available_time=first_available,
        )
        for feature_name, value in (
            ("pe_ttm", 18.0),
            ("dividend_yield", 0.021),
            ("market_cap", 11_000_000_000.0),
        ):
            records.append(
                _record(
                    "AAA",
                    feature_name,
                    value,
                    event_time=refreshed,
                    available_time=refreshed,
                    revision="daily-v2",
                )
            )
        result = build_point_in_time_feature_join(
            records,
            universe_by_date={second_date: ("AAA",)},
            source_version="pit-fixture-v2",
            config=PointInTimeFeatureJoinConfig(market="CN"),
        )
        values = result.features_by_key[("AAA", second_date)]
        self.assertEqual(18.0, values["pe_ttm"])
        self.assertEqual(0.12, values["roe_avg_3y"])
        self.assertEqual(set(FUNDAMENTAL_FEATURE_NAMES), set(values) - {"positive_earnings_yield", "market_cap_log"})

    def test_explicit_live_cutoff_attributes_after_close_collection_without_backdating(self) -> None:
        feature_date = date(2026, 8, 21)
        collected = datetime(2026, 8, 21, 18, 30, tzinfo=TZ)
        records = _complete_records("AAA", event_time=collected, available_time=collected)

        historical = build_point_in_time_feature_join(
            records,
            universe_by_date={feature_date: ("AAA",)},
            source_version="pit-fixture-live-v1",
            config=PointInTimeFeatureJoinConfig(market="CN"),
        )
        live = build_point_in_time_feature_join(
            records,
            universe_by_date={feature_date: ("AAA",)},
            source_version="pit-fixture-live-v1",
            config=PointInTimeFeatureJoinConfig(market="CN"),
            cutoff_by_date={
                feature_date: datetime(2026, 8, 22, 10, 0, tzinfo=TZ),
            },
        )

        self.assertEqual((feature_date,), historical.disabled_dates)
        self.assertEqual((feature_date,), live.enabled_dates)
        self.assertEqual(20.0, live.features_by_key[("AAA", feature_date)]["pe_ttm"])

    def test_explicit_cutoff_must_be_timezone_aware(self) -> None:
        feature_date = date(2026, 8, 21)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            build_point_in_time_feature_join(
                [],
                universe_by_date={feature_date: ("AAA",)},
                source_version="pit-fixture-v1",
                config=PointInTimeFeatureJoinConfig(market="CN"),
                cutoff_by_date={feature_date: datetime(2026, 8, 21, 18, 0)},
            )


if __name__ == "__main__":
    unittest.main()
