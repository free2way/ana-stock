from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

import polars as pl

from app.services.adjusted_view import (
    AdjustmentError,
    adjustment_version,
    build_adjusted_series,
    raw_series_digest,
)
from app.services.adjusted_view_builder import (
    rebuild_adjusted_view_if_stale,
    us_adjusted_raw_glob,
)
from app.services.corporate_actions import CorporateActionRecord


def _bar(day: str, close: float, *, open_price: float | None = None) -> dict:
    price = open_price if open_price is not None else close
    return {
        "date": day,
        "symbol": "AAA",
        "open": price,
        "high": max(price, close),
        "low": min(price, close),
        "close": close,
        "volume": 1000.0,
    }


def _split(day: str, factor: float) -> CorporateActionRecord:
    return CorporateActionRecord(
        market="US", symbol="AAA", action_type="split",
        effective_date=date.fromisoformat(day), factor=factor,
    )


class AdjustedViewTests(TestCase):
    def test_qfq_split_makes_the_series_continuous(self) -> None:
        bars = [_bar("2026-01-02", 100.0), _bar("2026-01-05", 102.0), _bar("2026-01-06", 51.0), _bar("2026-01-07", 52.0)]
        adjusted, stats = build_adjusted_series(bars, [_split("2026-01-06", 2.0)], method="qfq")
        self.assertEqual(1, stats.events_applied)
        closes = {item.trade_date.isoformat(): round(item.close, 6) for item in adjusted}
        self.assertEqual(50.0, closes["2026-01-02"])
        self.assertEqual(51.0, closes["2026-01-05"])
        self.assertEqual(51.0, closes["2026-01-06"])
        self.assertEqual(52.0, closes["2026-01-07"])
        # The latest session must never be rescaled in a qfq view.
        self.assertEqual(1.0, adjusted[-1].multiplier)

    def test_hfq_split_scales_post_split_prices_up(self) -> None:
        bars = [_bar("2026-01-05", 100.0), _bar("2026-01-06", 51.0)]
        adjusted, _ = build_adjusted_series(bars, [_split("2026-01-06", 2.0)], method="hfq")
        closes = [round(item.close, 6) for item in adjusted]
        self.assertEqual([100.0, 102.0], closes)

    def test_cash_dividend_uses_previous_close(self) -> None:
        bars = [_bar("2026-06-19", 10.0), _bar("2026-06-22", 9.5)]
        action = CorporateActionRecord(
            market="CN", symbol="AAA", action_type="cash_dividend",
            effective_date=date(2026, 6, 22), cash_amount=0.5,
        )
        adjusted, stats = build_adjusted_series(bars, [action], method="qfq")
        self.assertEqual(1, stats.events_applied)
        self.assertEqual(9.5, round(adjusted[0].close, 6))
        self.assertEqual(9.5, round(adjusted[1].close, 6))

    def test_event_without_trading_date_is_skipped_and_counted(self) -> None:
        bars = [_bar("2026-01-05", 100.0), _bar("2026-01-06", 100.0)]
        adjusted, stats = build_adjusted_series(bars, [_split("2026-01-07", 2.0)], method="qfq")
        self.assertEqual(1, stats.events_skipped_no_trade_date)
        self.assertEqual(0, stats.events_applied)
        self.assertEqual([100.0, 100.0], [item.close for item in adjusted])

    def test_unusable_dividend_without_previous_close_is_skipped(self) -> None:
        bars = [_bar("2026-06-22", 9.5), _bar("2026-06-23", 9.6)]
        action = CorporateActionRecord(
            market="CN", symbol="AAA", action_type="cash_dividend",
            effective_date=date(2026, 6, 22), cash_amount=0.5,
        )
        _, stats = build_adjusted_series(bars, [action], method="qfq")
        self.assertEqual(1, stats.events_skipped_unusable)

    def test_version_pins_method_raw_digest_and_actions(self) -> None:
        bars = [_bar("2026-01-05", 100.0), _bar("2026-01-06", 51.0)]
        action = _split("2026-01-06", 2.0)
        digest = raw_series_digest(bars)
        first = adjustment_version(method="qfq", raw_digest=digest, actions=[action])
        second = adjustment_version(method="qfq", raw_digest=digest, actions=[action])
        self.assertEqual(first, second)
        changed = adjustment_version(method="qfq", raw_digest=digest, actions=[_split("2026-01-06", 3.0)])
        self.assertNotEqual(first, changed)
        self.assertNotEqual(
            first,
            adjustment_version(method="hfq", raw_digest=digest, actions=[action]),
        )

    def test_invalid_method_and_unsorted_bars_are_rejected(self) -> None:
        bars = [_bar("2026-01-05", 100.0)]
        with self.assertRaisesRegex(AdjustmentError, "method"):
            build_adjusted_series(bars, [], method="mystery")
        with self.assertRaisesRegex(AdjustmentError, "ascending"):
            build_adjusted_series([bars[0], _bar("2026-01-04", 99.0)], [], method="qfq")


class AdjustedViewRebuildTests(TestCase):
    """``rebuild_adjusted_view_if_stale`` gates the expensive rebuild on the
    persisted view's newest date."""

    def _write_view(self, lake_root: Path, dates: list[str]) -> None:
        target = lake_root / "_adjusted_v2" / "us" / "method=qfq"
        target.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"date": dates, "symbol": ["AAA"] * len(dates)}).write_parquet(
            target / "adjusted.parquet"
        )

    def test_skips_rebuild_when_view_covers_required_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lake_root = Path(tmp)
            self._write_view(lake_root, ["2026-10-07", "2026-10-08"])
            with patch(
                "app.services.adjusted_view_builder.build_market_view"
            ) as build, patch(
                "app.services.adjusted_view_builder.write_view"
            ) as write:
                result = rebuild_adjusted_view_if_stale(
                    "US",
                    raw_glob="ignored/*.parquet",
                    required_upper_bound="2026-10-08",
                    lake_root=lake_root,
                )
        self.assertEqual("skipped", result["status"])
        self.assertEqual("2026-10-08", result["latest_date"])
        build.assert_not_called()
        write.assert_not_called()

    def test_rebuilds_when_view_lags_required_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lake_root = Path(tmp)
            self._write_view(lake_root, ["2026-10-07"])
            with patch(
                "app.services.adjusted_view_builder.build_market_view",
                return_value=([], {"symbols": 3, "rows": 30}),
            ) as build, patch(
                "app.services.adjusted_view_builder.write_view",
                return_value={"parquet": "p", "parquet_sha256": "sha", "generated_at": "t"},
            ) as write:
                result = rebuild_adjusted_view_if_stale(
                    "US",
                    raw_glob="x/*.parquet",
                    required_upper_bound="2026-10-08",
                    lake_root=lake_root,
                )
        self.assertEqual("rebuilt", result["status"])
        self.assertEqual("2026-10-07", result["previous_latest_date"])
        self.assertEqual(3, result["symbols"])
        build.assert_called_once_with("US", method="qfq", raw_glob="x/*.parquet")
        write.assert_called_once()

    def test_rebuilds_when_view_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lake_root = Path(tmp)
            with patch(
                "app.services.adjusted_view_builder.build_market_view",
                return_value=([], {"symbols": 1, "rows": 1}),
            ) as build, patch(
                "app.services.adjusted_view_builder.write_view",
                return_value={"parquet": "p", "parquet_sha256": "sha", "generated_at": "t"},
            ):
                result = rebuild_adjusted_view_if_stale(
                    "US",
                    raw_glob="x/*.parquet",
                    required_upper_bound="2026-10-08",
                    lake_root=lake_root,
                )
        self.assertEqual("rebuilt", result["status"])
        self.assertIsNone(result["previous_latest_date"])
        build.assert_called_once()

    def test_us_raw_glob_points_at_single_basis_namespace(self) -> None:
        glob = us_adjusted_raw_glob(Path("/lake"))
        self.assertEqual("/lake/_us_alpaca/raw/*.parquet", glob)
