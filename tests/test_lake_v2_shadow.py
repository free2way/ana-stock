from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import polars as pl

from app.services.market_lake import write_daily_ohlcv_parquet


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
    }
    row.update(overrides)
    return row


class LakeV2ShadowTests(TestCase):
    def _write(self, tmp: str, rows: list[dict], *, merge_existing: bool = True, allow_legacy_defaults: bool = False):
        with patch("app.services.market_lake.market_lake_root", return_value=Path(tmp)), patch(
            "app.services.market_lake.latest_completed_market_date",
            return_value="2026-06-22",
        ):
            return write_daily_ohlcv_parquet(
                market="CN",
                trade_date="2026-06-22",
                rows=rows,
                merge_existing=merge_existing,
                allow_legacy_defaults=allow_legacy_defaults,
            )

    def test_shadow_write_carries_full_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                [
                    _row(
                        provider="tushare",
                        provider_symbol="000001.SZ",
                        source_reference="tushare:pro.daily",
                        source_batch_id="batch-1",
                    )
                ],
            )
            shadow = Path(tmp) / "_lake_v2" / "cn_daily" / "date=2026-06-22" / "part.parquet"
            self.assertTrue(shadow.exists())
            frame = pl.read_parquet(shadow)
            record = frame.row(0, named=True)
            self.assertEqual("CN", record["market"])
            self.assertEqual("tushare", record["provider"])
            self.assertEqual("000001.SZ", record["provider_symbol"])
            self.assertEqual("raw", record["price_basis"])
            self.assertEqual("share", record["volume_unit"])
            self.assertEqual("CNY", record["currency"])
            self.assertEqual("tushare:pro.daily", record["source_reference"])
            self.assertEqual("batch-1", record["source_batch_id"])
            self.assertTrue(record["ingested_at"])
            self.assertEqual(16, len(record["revision_id"]))
            provenance_columns = [
                "market", "provider", "provider_symbol", "price_basis", "volume_unit",
                "currency", "source_reference", "source_batch_id", "ingested_at", "revision_id",
            ]
            for name in provenance_columns:
                self.assertIsNotNone(record[name], msg=name)
                self.assertNotEqual("", record[name], msg=name)

    def test_missing_provenance_fails_closed_unless_legacy_mode(self) -> None:
        """A3: placeholder provenance is never written silently."""

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "provider is required"):
                self._write(tmp, [{**_row(), "provider": "", "source_reference": ""}])
            # The explicit legacy path keeps the old placeholder behaviour.
            self._write(tmp, [{**_row(), "provider": "", "source_reference": ""}], allow_legacy_defaults=True)
            frame = pl.read_parquet(Path(tmp) / "_lake_v2" / "cn_daily" / "date=2026-06-22" / "part.parquet")
            record = frame.row(0, named=True)
            self.assertEqual("unknown", record["provider"])
            self.assertEqual("000001.SZ", record["provider_symbol"])
            self.assertEqual("CNY", record["currency"])
            self.assertEqual("unspecified", record["source_batch_id"])
            self.assertEqual("unspecified", record["source_reference"])

    def test_basis_conflict_within_batch_blocks_both_stores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "price_basis conflict"):
                self._write(
                    tmp,
                    [
                        _row(symbol="000001.SZ", price_basis="raw"),
                        _row(symbol="000001.SZ", close=10.4, price_basis="qfq"),
                    ],
                )
            self.assertFalse((Path(tmp) / "cn_daily" / "date=2026-06-22" / "part.parquet").exists())
            self.assertFalse((Path(tmp) / "_lake_v2" / "cn_daily" / "date=2026-06-22" / "part.parquet").exists())

    def test_provenance_is_validated_even_when_shadow_is_disabled(self) -> None:
        """The kill switch must not bypass the fail-closed provenance check."""
        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.services.market_lake._lake_v2_shadow_enabled",
            return_value=False,
        ):
            with self.assertRaisesRegex(ValueError, "concrete provider is required"):
                self._write(tmp, [_row(provider="", source_reference="")])

    def test_auto_provider_selector_is_rejected_as_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "concrete provider is required"):
                self._write(tmp, [_row(provider="auto", source_reference="auto:cn:daily")])

    def test_basis_change_against_existing_shadow_partition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [_row(price_basis="raw")])
            with self.assertRaisesRegex(ValueError, "price_basis conflict"):
                self._write(tmp, [_row(close=10.3, price_basis="qfq")])

    def test_shadow_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "app.services.market_lake._lake_v2_shadow_enabled",
            return_value=False,
        ):
            self._write(tmp, [_row()])
            self.assertFalse((Path(tmp) / "_lake_v2").exists())
            self.assertTrue((Path(tmp) / "cn_daily" / "date=2026-06-22" / "part.parquet").exists())
