import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import polars as pl

from app.services.market_lake import write_daily_ohlcv_parquet, write_ohlcv_rows_to_lake


def _row(symbol: str, close: float, trade_date: str = "2026-06-22") -> dict:
    return {
        "date": trade_date,
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 100,
        "adj_close": close,
        "dividend": 0,
        "split_ratio": 1,
    }


class MarketLakeWriteTests(TestCase):
    def test_partial_write_preserves_existing_daily_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.services.market_lake.market_lake_root",
            return_value=Path(temp_dir),
        ):
            path = write_daily_ohlcv_parquet(
                market="CN",
                trade_date="2026-06-22",
                rows=[_row("000001.SZ", 10), _row("000002.SZ", 20)],
                merge_existing=False,
            )

            write_ohlcv_rows_to_lake(
                market="CN",
                rows=[_row("000001.SZ", 11)],
            )

            rows = pl.read_parquet(path).sort("symbol").to_dicts()
            self.assertEqual(["000001.SZ", "000002.SZ"], [row["symbol"] for row in rows])
            self.assertEqual(11, rows[0]["close"])
            self.assertEqual(20, rows[1]["close"])

    def test_partial_write_fails_closed_when_existing_partition_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.services.market_lake.market_lake_root",
            return_value=Path(temp_dir),
        ):
            path = Path(temp_dir) / "cn_daily" / "date=2026-06-22" / "part.parquet"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"not-a-parquet-file")

            with self.assertRaises(Exception):
                write_ohlcv_rows_to_lake(market="CN", rows=[_row("000001.SZ", 11)])

            self.assertEqual(b"not-a-parquet-file", path.read_bytes())

    def test_explicit_full_write_can_replace_partition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.services.market_lake.market_lake_root",
            return_value=Path(temp_dir),
        ):
            write_daily_ohlcv_parquet(
                market="CN",
                trade_date="2026-06-22",
                rows=[_row("000001.SZ", 10), _row("000002.SZ", 20)],
                merge_existing=False,
            )
            path = write_daily_ohlcv_parquet(
                market="CN",
                trade_date="2026-06-22",
                rows=[_row("000001.SZ", 12)],
                merge_existing=False,
            )

            rows = pl.read_parquet(path).to_dicts()
            self.assertEqual(["000001.SZ"], [row["symbol"] for row in rows])
            self.assertEqual(12, rows[0]["close"])

    def test_future_market_rows_are_rejected_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.services.market_lake.market_lake_root",
            return_value=Path(temp_dir),
        ), patch(
            "app.services.market_lake.latest_completed_market_date",
            return_value="2026-06-22",
        ):
            with self.assertRaisesRegex(ValueError, "future"):
                write_ohlcv_rows_to_lake(
                    market="US",
                    rows=[_row("AAPL", 200, trade_date="2026-06-23")],
                )
            self.assertFalse(list(Path(temp_dir).rglob("*.parquet")))
