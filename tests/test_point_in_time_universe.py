from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from app.services.stock_selection.universe import (
    SecurityMetadata,
    UniverseRuleConfig,
    build_point_in_time_universe,
)


def _row(ticker: str, trade_date: date, *, close: float = 10.0, volume: float = 1_000.0) -> dict:
    return {
        "symbol": ticker,
        "date": trade_date.isoformat(),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
    }


class PointInTimeUniverseTests(TestCase):
    def setUp(self) -> None:
        start = date(2026, 7, 1)
        self.dates = [start + timedelta(days=index) for index in range(5)]
        self.rules = UniverseRuleConfig(
            market="US",
            min_price=3.0,
            min_adv20=5_000.0,
            min_avg_volume20=500.0,
            min_history_sessions=2,
            adv_lookback_sessions=2,
        )

    def test_preserves_not_yet_listed_and_delisted_names_in_history(self) -> None:
        rows = [_row("AAA", item) for item in self.dates]
        rows += [_row("NEW", item) for item in self.dates[2:]]
        rows += [_row("OLD", item) for item in self.dates[:3]]
        rows += [_row("ETF", item) for item in self.dates]
        metadata = {
            "AAA": SecurityMetadata(ticker="AAA", listing_date=self.dates[0]),
            "NEW": SecurityMetadata(ticker="NEW", listing_date=self.dates[2]),
            "OLD": SecurityMetadata(
                ticker="OLD",
                listing_date=self.dates[0],
                delisting_date=self.dates[3],
            ),
            "ETF": SecurityMetadata(ticker="ETF", security_type="etf", listing_date=self.dates[0]),
        }

        result = build_point_in_time_universe(
            rows,
            trading_dates=self.dates,
            metadata=metadata,
            rules=self.rules,
            source_version="fixture-v1",
        )
        snapshots = result.by_key()

        self.assertTrue(snapshots[("AAA", self.dates[1])].included)
        self.assertIn("not_yet_listed", snapshots[("NEW", self.dates[1])].exclusion_reason_codes)
        self.assertTrue(snapshots[("NEW", self.dates[3])].included)
        self.assertIn("delisted", snapshots[("OLD", self.dates[3])].exclusion_reason_codes)
        self.assertIn("unsupported_security_type", snapshots[("ETF", self.dates[4])].exclusion_reason_codes)
        self.assertEqual(20, len(result.snapshots))
        self.assertEqual("fixture-v1", result.manifest()["source_version"])

    def test_future_volume_cannot_change_past_adv_or_membership(self) -> None:
        base_rows = [_row("AAA", item, volume=1_000.0) for item in self.dates]
        changed_rows = [dict(item) for item in base_rows]
        changed_rows[-1]["volume"] = 1_000_000_000.0
        metadata = {"AAA": SecurityMetadata(ticker="AAA", listing_date=self.dates[0])}

        base = build_point_in_time_universe(
            base_rows,
            trading_dates=self.dates,
            metadata=metadata,
            rules=self.rules,
            source_version="fixture-v1",
        ).by_key()
        changed = build_point_in_time_universe(
            changed_rows,
            trading_dates=self.dates,
            metadata=metadata,
            rules=self.rules,
            source_version="fixture-v1",
        ).by_key()

        past_key = ("AAA", self.dates[2])
        self.assertEqual(base[past_key].adv20, changed[past_key].adv20)
        self.assertEqual(base[past_key].included, changed[past_key].included)

    def test_rule_change_creates_new_universe_version(self) -> None:
        rows = [_row("AAA", item) for item in self.dates]
        metadata = {"AAA": SecurityMetadata(ticker="AAA", listing_date=self.dates[0])}
        first = build_point_in_time_universe(
            rows,
            trading_dates=self.dates,
            metadata=metadata,
            rules=self.rules,
            source_version="fixture-v1",
        )
        stricter = UniverseRuleConfig(
            market="US",
            min_price=20.0,
            min_adv20=5_000.0,
            min_avg_volume20=500.0,
            min_history_sessions=2,
            adv_lookback_sessions=2,
        )
        second = build_point_in_time_universe(
            rows,
            trading_dates=self.dates,
            metadata=metadata,
            rules=stricter,
            source_version="fixture-v1",
        )
        self.assertNotEqual(first.universe_version, second.universe_version)
        self.assertGreater(second.exclusion_counts.get("low_price", 0), 0)
