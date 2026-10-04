from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import polars as pl

from app.services.market_data_quality import (
    classify_ohlcv_row,
    partition_ohlcv_rows,
    quarantine_rows,
)


def _row(**overrides) -> dict:
    row = {
        "date": "2026-06-22",
        "symbol": "000001.SZ",
        "provider": "fixture",
        "source_reference": "fixture:000001.SZ:2026-06-22",
        "open": 10.0,
        "high": 10.5,
        "low": 9.8,
        "close": 10.2,
        "volume": 1000.0,
        "adj_close": 10.2,
        "dividend": None,
        "split_ratio": None,
    }
    row.update(overrides)
    return row


class OhlcvValidationTests(TestCase):
    def test_valid_row_passes(self) -> None:
        self.assertIsNone(classify_ohlcv_row(_row()))

    def test_reason_codes_are_specific(self) -> None:
        cases = {
            "missing_identity": _row(symbol=""),
            "missing_close": _row(close=None),
            "non_positive_price": _row(close=0.0),
            "non_positive_price_open": _row(open=-1.0),
            "missing_volume": _row(volume=None),
            "negative_volume": _row(volume=-5.0),
            "invalid_ohlc_high": _row(high=9.9),
            "invalid_ohlc_low": _row(low=10.3),
        }
        for expected, row in cases.items():
            reason = classify_ohlcv_row(row)
            if expected == "non_positive_price_open":
                self.assertEqual("non_positive_price", reason)
            else:
                self.assertEqual(expected, reason, msg=str(row))

    def test_partition_keeps_valid_and_tags_rejected(self) -> None:
        accepted, rejected = partition_ohlcv_rows([_row(), _row(close=0.0), _row(volume=-1.0)])
        self.assertEqual(1, len(accepted))
        self.assertEqual(2, len(rejected))
        self.assertEqual({"non_positive_price", "negative_volume"}, {row["rejection_reason"] for row in rejected})

    def test_quarantine_appends_jsonl_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            _, rejected = partition_ohlcv_rows([_row(close=0.0)])
            path = quarantine_rows(root=Path(temp_dir), market="CN", rows=rejected)
            self.assertIsNotNone(path)
            self.assertEqual(path.parent.name, "_quarantine")
            entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual("CN", entry["market"])
            self.assertEqual("non_positive_price", entry["rejection_reason"])
            self.assertTrue(entry["rejected_at"])


class LakeWriteQuarantineTests(TestCase):
    def test_invalid_rows_are_quarantined_and_valid_rows_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.services.market_lake.market_lake_root",
            return_value=Path(temp_dir),
        ), patch(
            "app.services.market_lake.latest_completed_market_date",
            return_value="2026-06-22",
        ):
            from app.services.market_lake import write_daily_ohlcv_parquet

            write_daily_ohlcv_parquet(
                market="CN",
                trade_date="2026-06-22",
                rows=[_row(), _row(symbol="000002.SZ", high=9.0, low=9.5)],
                merge_existing=False,
            )

            partition = Path(temp_dir) / "cn_daily" / "date=2026-06-22" / "part.parquet"
            frame = pl.read_parquet(partition)
            self.assertEqual(["000001.SZ"], frame["symbol"].to_list())

            quarantine_files = list((Path(temp_dir) / "_quarantine").glob("cn_rejected_*.jsonl"))
            self.assertEqual(1, len(quarantine_files))
            entries = [json.loads(line) for line in quarantine_files[0].read_text(encoding="utf-8").splitlines()]
            self.assertEqual(1, len(entries))
            self.assertEqual("000002.SZ", entries[0]["symbol"])
            self.assertEqual("invalid_ohlc_high", entries[0]["rejection_reason"])

    def test_all_invalid_rows_raise_and_write_no_partition(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app.services.market_lake.market_lake_root",
            return_value=Path(temp_dir),
        ), patch(
            "app.services.market_lake.latest_completed_market_date",
            return_value="2026-06-22",
        ):
            from app.services.market_lake import write_daily_ohlcv_parquet

            with self.assertRaises(ValueError):
                write_daily_ohlcv_parquet(
                    market="CN",
                    trade_date="2026-06-22",
                    rows=[_row(close=0.0)],
                    merge_existing=False,
                )
            self.assertFalse((Path(temp_dir) / "cn_daily" / "date=2026-06-22" / "part.parquet").exists())
            self.assertTrue(list((Path(temp_dir) / "_quarantine").glob("cn_rejected_*.jsonl")))
