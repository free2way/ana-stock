"""S-12: SAFE FX sync normalization, /100 conversion, staleness and fail-closed.

Real values asserted here are the SAFE 人民币汇率中间价 for 2026-09-30 published by
``ak.currency_boc_safe()`` (美元=673.51, 港元=85.842 per 100 units) -> 6.7351 and
0.85842 CNY per unit.  They are hard-coded so the tests stay offline.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tests.postgres_safety import ApplicationPostgresTestCase

from app.services.fx_rates import (
    FX_MAX_AGE_DAYS,
    fx_table_freshness,
    load_fx_rate_table,
    normalize_currency_aggregation,
    resolve_fx_rate,
)
from scripts.sync_fx_rates import (
    SOURCE_LABEL,
    FxSyncError,
    build_fx_table_payload,
    sync_fx_rates,
    table_content_hash,
)


class _Frame:
    """Minimal offline stand-in for the AKShare DataFrame."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.columns = list(rows[0].keys()) if rows else []
        self.empty = not rows

    def to_dict(self, orient: str = "records") -> list[dict]:
        assert orient == "records"
        return list(self._rows)


class _AmbiguousIndex:
    """pandas.Index raises on ``bool()``; guard against truthiness checks."""

    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def __iter__(self):
        return iter(self._values)

    def __bool__(self):
        raise ValueError("The truth value of an Index is ambiguous")


def _safe_frame() -> _Frame:
    return _Frame(
        [
            {"日期": "2026-09-28", "美元": 673.99, "港元": 85.923},
            {"日期": "2026-09-29", "美元": 674.11, "港元": 85.935},
            {"日期": "2026-09-30", "美元": 673.51, "港元": 85.842},
        ]
    )


def _resolved(table: dict) -> dict:
    """Mirror what ``load_fx_rate_table`` hands to the aggregation consumer."""
    return {**table, "available": True}


# An explicitly unconfigured operator table; ``table=None`` would read ambient DB state.
_NO_TABLE = {"available": False, "base": None, "as_of": None, "source": None, "rates": {}}


class SafeNormalizationTests(unittest.TestCase):
    def test_100_unit_quotes_are_divided_by_100(self) -> None:
        table, meta = build_fx_table_payload(_safe_frame(), as_of="2026-09-30")

        self.assertEqual("CNY", table["base"])
        self.assertEqual("2026-09-30", table["as_of"])
        self.assertEqual(SOURCE_LABEL, table["source"])
        # SAFE quotes 100 foreign units -> CNY; the consumer expects per 1 unit.
        self.assertAlmostEqual(6.7351, table["rates"]["USD"], places=6)
        self.assertAlmostEqual(0.85842, table["rates"]["HKD"], places=6)
        self.assertEqual({"USD", "HKD"}, set(table["rates"]))
        self.assertTrue(meta["exact_match"])
        self.assertEqual(673.51, meta["raw_100_unit"]["USD"])

    def test_existing_table_yields_single_base_total_with_fx_status_ok(self) -> None:
        table = _resolved(build_fx_table_payload(_safe_frame(), as_of="2026-09-30")[0])
        self.assertAlmostEqual(6.7351, resolve_fx_rate("USD", "CNY", table), places=6)

        rows = [
            {"market": "CN", "market_value": 1000.0},
            {"market": "US", "market_value": 1000.0},
            {"market": "HK", "market_value": 1000.0},
        ]
        summary = normalize_currency_aggregation(
            rows, value_key="market_value", table=table, reference_date="2026-10-03"
        )

        self.assertEqual("ok", summary["fx_status"])
        self.assertEqual("CNY", summary["base_currency"])
        self.assertFalse(summary["fx_unavailable"])
        # 1000 + 6735.10 + 858.42 == one blended CNY total (no raw mixing).
        self.assertAlmostEqual(8593.52, summary["total_base"], places=4)
        self.assertAlmostEqual(6735.10, rows[1]["market_value_base"], places=4)
        self.assertAlmostEqual(0.85842, rows[2]["fx_rate"], places=6)

    def test_missing_table_still_fails_closed(self) -> None:
        rows = [
            {"market": "CN", "market_value": 10000.0},
            {"market": "US", "market_value": 1000.0},
        ]
        summary = normalize_currency_aggregation(
            rows, value_key="market_value", table=_NO_TABLE, reference_date="2026-10-03"
        )

        self.assertEqual("partial", summary["fx_status"])
        self.assertIn("US", summary["fx_unavailable_markets"])
        self.assertAlmostEqual(10000.0, summary["total_base"])
        self.assertIsNone(rows[1]["market_value_base"])
        self.assertFalse(summary["fx_stale"])
        self.assertEqual([], summary["fx_warnings"])

    def test_as_of_selects_exact_or_latest_earlier_quote(self) -> None:
        exact, meta = build_fx_table_payload(_safe_frame(), as_of="2026-09-29")
        self.assertEqual("2026-09-29", exact["as_of"])
        self.assertAlmostEqual(6.7411, exact["rates"]["USD"], places=6)
        self.assertTrue(meta["exact_match"])

        # 2026-10-01 has no SAFE quote (holiday): fall back to the latest earlier row.
        fallback, meta = build_fx_table_payload(_safe_frame(), as_of="2026-10-01")
        self.assertEqual("2026-09-30", fallback["as_of"])
        self.assertFalse(meta["exact_match"])
        self.assertEqual("2026-10-01", meta["requested_as_of"])

        with self.assertRaises(FxSyncError):
            build_fx_table_payload(_safe_frame(), as_of="1990-01-01")

    def test_missing_currency_column_is_rejected(self) -> None:
        broken = _Frame([{"日期": "2026-09-30", "美元": 673.51}])
        with self.assertRaises(FxSyncError):
            build_fx_table_payload(broken)

    def test_pandas_style_index_columns_are_not_truth_tested(self) -> None:
        frame = _Frame([{"日期": "2026-09-30", "美元": 673.51, "港元": 85.842}])
        frame.columns = _AmbiguousIndex(["日期", "美元", "港元"])
        table, _meta = build_fx_table_payload(frame)
        self.assertAlmostEqual(6.7351, table["rates"]["USD"], places=6)

    def test_content_hash_is_order_insensitive(self) -> None:
        table = {"base": "CNY", "as_of": "2026-09-30", "source": SOURCE_LABEL, "rates": {"USD": 6.7351, "HKD": 0.85842}}
        reordered = {"rates": {"HKD": 0.85842, "USD": 6.7351}, "source": SOURCE_LABEL, "as_of": "2026-09-30", "base": "CNY"}
        self.assertEqual(table_content_hash(table), table_content_hash(reordered))


class FxStalenessTests(unittest.TestCase):
    def _table(self) -> dict:
        table, _meta = build_fx_table_payload(_safe_frame(), as_of="2026-09-30")
        return _resolved(table)

    def test_fresh_table_is_not_stale(self) -> None:
        summary = normalize_currency_aggregation(
            [{"market": "US", "market_value": 100.0}],
            value_key="market_value",
            table=self._table(),
            reference_date="2026-10-03",
        )
        self.assertFalse(summary["fx_stale"])
        self.assertEqual(3, summary["fx_age_days"])
        self.assertEqual([], summary["fx_warnings"])
        self.assertEqual("ok", summary["fx_status"])

    def test_stale_table_is_annotated_but_still_converts(self) -> None:
        reference = (date(2026, 9, 30) + timedelta(days=FX_MAX_AGE_DAYS + 16)).isoformat()
        rows = [
            {"market": "CN", "market_value": 1000.0},
            {"market": "US", "market_value": 100.0},
        ]
        summary = normalize_currency_aggregation(
            rows, value_key="market_value", table=self._table(), reference_date=reference
        )

        # Policy: annotate, never silently drop a valid rate.
        self.assertTrue(summary["fx_stale"])
        self.assertEqual(FX_MAX_AGE_DAYS + 16, summary["fx_age_days"])
        self.assertEqual("stale_fx_table", summary["fx_warnings"][0])
        self.assertEqual("ok", summary["fx_status"])
        self.assertAlmostEqual(1673.51, summary["total_base"], places=4)

    def test_max_age_threshold_is_configurable(self) -> None:
        freshness = fx_table_freshness(
            self._table(), reference_date="2026-10-10", max_age_days=5
        )
        self.assertTrue(freshness["stale"])
        self.assertEqual(10, freshness["age_days"])
        self.assertFalse(
            fx_table_freshness(self._table(), reference_date="2026-10-10", max_age_days=30)["stale"]
        )


class FxSyncStorageTests(ApplicationPostgresTestCase):
    def test_sync_writes_setting_and_versioned_artifact_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = sync_fx_rates(frame=_safe_frame(), as_of="2026-09-30", artifact_dir=Path(tmp))
            self.assertEqual("ok", first["status"])
            self.assertTrue(first["setting_written"])

            stored = load_fx_rate_table()
            self.assertTrue(stored["available"])
            self.assertEqual("CNY", stored["base"])
            self.assertEqual("2026-09-30", stored["as_of"])
            self.assertEqual(SOURCE_LABEL, stored["source"])
            self.assertAlmostEqual(6.7351, stored["rates"]["USD"], places=6)
            self.assertAlmostEqual(0.85842, stored["rates"]["HKD"], places=6)

            # The persisted operator table now blends currencies to one CNY total.
            rows = [
                {"market": "CN", "market_value": 1000.0},
                {"market": "US", "market_value": 1000.0},
            ]
            summary = normalize_currency_aggregation(
                rows, value_key="market_value", table=stored, reference_date="2026-10-03"
            )
            self.assertEqual("ok", summary["fx_status"])
            self.assertAlmostEqual(7735.10, summary["total_base"], places=4)

            snapshot = Path(first["artifact"]["snapshot_path"])
            history = Path(first["artifact"]["history_path"])
            self.assertTrue(snapshot.exists())
            self.assertTrue(history.exists())
            snapshot_bytes = snapshot.read_bytes()

            second = sync_fx_rates(frame=_safe_frame(), as_of="2026-09-30", artifact_dir=Path(tmp))
            self.assertEqual(
                first["artifact"]["content_sha256"], second["artifact"]["content_sha256"]
            )
            self.assertFalse(second["artifact"]["written"])
            self.assertEqual(snapshot_bytes, snapshot.read_bytes())

            index = json.loads(history.read_text(encoding="utf-8"))
            self.assertEqual(1, len(index["entries"]))
            self.assertEqual("2026-09-30", index["entries"][0]["as_of"])
            self.assertEqual(
                first["artifact"]["content_sha256"], index["entries"][0]["content_sha256"]
            )


if __name__ == "__main__":
    unittest.main()
