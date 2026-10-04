from __future__ import annotations

import tempfile
from datetime import date

import polars as pl
from pathlib import Path
from unittest import TestCase

from app.services.backtesting.market_rules import cn_price_limit_pct
from app.services.corporate_actions import (
    CorporateActionRecord,
    explains_close_jump,
    load_actions,
    normalize_action,
    write_actions,
)


class CorporateActionValidationTests(TestCase):
    def test_split_and_dividend_normalize(self) -> None:
        split = normalize_action(
            {
                "symbol": "000001.sz",
                "action_type": "split",
                "effective_date": "2026-07-30",
                "factor": "2.0",
                "source": "tushare",
            },
            market="cn",
        )
        self.assertEqual("CN", split.market)
        self.assertEqual("000001.SZ", split.symbol)
        self.assertEqual(date(2026, 7, 30), split.effective_date)
        self.assertEqual(2.0, split.factor)
        self.assertTrue(split.revision_id)

        dividend = normalize_action(
            {
                "symbol": "600000.SS",
                "action_type": "cash_dividend",
                "effective_date": "2026-06-20",
                "cash_amount": "0.5",
                "announced_date": "2026-06-01",
            },
            market="CN",
        )
        self.assertEqual(0.5, dividend.cash_amount)
        self.assertEqual(date(2026, 6, 1), dividend.announced_date)

    def test_invalid_rows_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported action_type"):
            normalize_action(
                {"symbol": "AAA", "action_type": "bonus", "effective_date": "2026-01-05"}, market="US"
            )
        with self.assertRaisesRegex(ValueError, "split requires factor"):
            normalize_action(
                {"symbol": "AAA", "action_type": "split", "effective_date": "2026-01-05"}, market="US"
            )
        with self.assertRaisesRegex(ValueError, "cash_amount"):
            normalize_action(
                {"symbol": "AAA", "action_type": "cash_dividend", "effective_date": "2026-01-05"},
                market="US",
            )
        with self.assertRaisesRegex(ValueError, "effective_date"):
            normalize_action(
                {"symbol": "AAA", "action_type": "split", "effective_date": "not-a-date", "factor": 2},
                market="US",
            )
        with self.assertRaisesRegex(ValueError, "cannot follow"):
            normalize_action(
                {
                    "symbol": "AAA",
                    "action_type": "split",
                    "effective_date": "2026-01-05",
                    "announced_date": "2026-02-05",
                    "factor": 2,
                },
                market="US",
            )


class CorporateActionStoreTests(TestCase):
    def test_roundtrip_dedupe_and_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = CorporateActionRecord(
                market="US", symbol="AAA", action_type="split",
                effective_date=date(2026, 1, 5), factor=2.0, source="polygon",
                ingested_at="2026-01-06T00:00:00+00:00",
            )
            write_actions("US", [first], root=root)
            # Later ingestion replaces the same natural key.
            corrected = CorporateActionRecord(
                market="US", symbol="AAA", action_type="split",
                effective_date=date(2026, 1, 5), factor=2.0, source="polygon+alpaca",
                ingested_at="2026-01-07T00:00:00+00:00",
            )
            other = CorporateActionRecord(
                market="US", symbol="BBB", action_type="cash_dividend",
                effective_date=date(2026, 3, 2), cash_amount=0.25,
            )
            write_actions("US", [corrected, other], root=root)

            all_records = load_actions("US", root=root)
            self.assertEqual(2, len(all_records))
            self.assertEqual("polygon+alpaca", next(r for r in all_records if r.symbol == "AAA").source)
            windowed = load_actions("US", start_date="2026-02-01", end_date="2026-12-31", root=root)
            self.assertEqual(["BBB"], [record.symbol for record in windowed])
            filtered = load_actions("US", symbols=["aaa"], root=root)
            self.assertEqual(["AAA"], [record.symbol for record in filtered])

    def test_market_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = CorporateActionRecord(
                market="US", symbol="AAA", action_type="split",
                effective_date=date(2026, 1, 5), factor=2.0,
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                write_actions("CN", [record], root=Path(tmp))


class CloseJumpExplanationTests(TestCase):
    def test_two_for_one_split_explains_halving(self) -> None:
        action = CorporateActionRecord(
            market="US", symbol="AAA", action_type="split",
            effective_date=date(2026, 1, 5), factor=2.0,
        )
        self.assertTrue(
            explains_close_jump(previous_close=100.0, close=50.5, actions=[action], symbol="AAA")
        )
        self.assertFalse(
            explains_close_jump(previous_close=100.0, close=71.0, actions=[action], symbol="AAA")
        )

    def test_cash_dividend_step_is_explained(self) -> None:
        action = CorporateActionRecord(
            market="CN", symbol="600000.SS", action_type="cash_dividend",
            effective_date=date(2026, 6, 20), cash_amount=0.5,
        )
        self.assertTrue(
            explains_close_jump(previous_close=10.0, close=9.51, actions=[action], symbol="600000.SS")
        )
        self.assertFalse(
            explains_close_jump(previous_close=10.0, close=8.0, actions=[action], symbol="600000.SS")
        )

    def test_unrelated_actions_do_not_explain(self) -> None:
        action = CorporateActionRecord(
            market="US", symbol="BBB", action_type="split",
            effective_date=date(2026, 1, 5), factor=2.0,
        )
        self.assertFalse(
            explains_close_jump(previous_close=100.0, close=50.0, actions=[action], symbol="AAA")
        )

    def test_zero_or_negative_prices_never_match(self) -> None:
        action = CorporateActionRecord(
            market="US", symbol="AAA", action_type="split",
            effective_date=date(2026, 1, 5), factor=2.0,
        )
        self.assertFalse(
            explains_close_jump(previous_close=0.0, close=50.0, actions=[action], symbol="AAA")
        )

    def test_cash_and_stock_dividend_combine(self) -> None:
        cash = CorporateActionRecord(
            market="CN", symbol="603629.SS", action_type="cash_dividend",
            effective_date=date(2026, 7, 7), cash_amount=0.37,
        )
        stock = CorporateActionRecord(
            market="CN", symbol="603629.SS", action_type="stock_dividend",
            effective_date=date(2026, 7, 7), factor=1.4,
        )
        # (172.90 - 0.37) / 1.4 = 123.236; -10% limit = 110.91
        self.assertTrue(
            explains_close_jump(previous_close=172.90, close=111.07, actions=[cash, stock], symbol="603629.SS")
        )
        self.assertFalse(
            explains_close_jump(previous_close=172.90, close=100.0, actions=[cash, stock], symbol="603629.SS")
        )

    def test_close_exactly_at_rounded_limit_is_explained(self) -> None:
        action = CorporateActionRecord(
            market="CN", symbol="601208.SS", action_type="cash_dividend",
            effective_date=date(2026, 7, 28), cash_amount=0.05,
        )
        # Reference 43.27; half-up limit-down 38.94 equals the actual close.
        self.assertTrue(
            explains_close_jump(previous_close=43.32, close=38.94, actions=[action], symbol="601208.SS")
        )

    def test_growth_board_302_prefix_has_20pct_band(self) -> None:
        self.assertAlmostEqual(0.20, cn_price_limit_pct("302132.SZ"))
        self.assertAlmostEqual(0.20, cn_price_limit_pct("301550.SZ"))
        self.assertAlmostEqual(0.30, cn_price_limit_pct("920000.BJ"))

    def test_official_reference_rounding_margin_is_tolerated(self) -> None:
        # 002975 2026-05-11: 10转3 (factor 1.3), official ex-reference 95.85
        # versus the factor-derived 95.73; the close sits on the official limit.
        action = CorporateActionRecord(
            market="CN", symbol="002975.SZ", action_type="stock_dividend",
            effective_date=date(2026, 5, 11), factor=1.3,
        )
        self.assertTrue(
            explains_close_jump(previous_close=124.45, close=105.44, actions=[action], symbol="002975.SZ")
        )


class ActionRevisionAuditTests(TestCase):
    """A4: append-only revisions with reproducible history."""

    def _action(self, *, cash: float, reference: str, ingested: str, announced: date | None = None) -> CorporateActionRecord:
        return CorporateActionRecord(
            market="CN", symbol="600000.SS", action_type="cash_dividend",
            effective_date=date(2026, 6, 20), cash_amount=cash,
            source="akshare_fhps", source_reference=reference,
            announced_date=announced, ingested_at=ingested,
        )

    def _write(self, tmp: str, records: list[CorporateActionRecord]):
        from pathlib import Path as _Path

        from app.services.corporate_actions import write_actions as _write_actions

        return _write_actions("CN", records, root=_Path(tmp))

    def test_revisions_are_kept_and_latest_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, [self._action(cash=0.5, reference="akshare:p1", ingested="2026-06-10T00:00:00+00:00")])
            self._write(tmp, [self._action(cash=0.52, reference="akshare:p1:corrected", ingested="2026-06-12T00:00:00+00:00")])
            frame = pl.read_parquet(Path(tmp) / "cn_actions.parquet")
            self.assertEqual(2, frame.height)  # both revisions stored
            latest = load_actions("CN", root=Path(tmp))
            self.assertEqual(1, len(latest))
            self.assertAlmostEqual(0.52, latest[0].cash_amount)
            # Historical view: what a run on 06-11 would have seen.
            historical = load_actions("CN", as_of="2026-06-11T00:00:00+00:00", root=Path(tmp))
            self.assertEqual(1, len(historical))
            self.assertAlmostEqual(0.5, historical[0].cash_amount)

    def test_as_of_and_latest_use_absolute_time_not_string_order(self) -> None:
        """Mixed UTC offsets must be compared as instants (review finding)."""
        with tempfile.TemporaryDirectory() as tmp:
            # 10:00+08:00 == 02:00Z (earlier instant, lexicographically larger)
            self._write(tmp, [self._action(cash=0.5, reference="akshare:p1", ingested="2026-06-11T10:00:00+08:00")])
            # 05:00Z (later instant, lexicographically smaller)
            self._write(tmp, [self._action(cash=0.9, reference="akshare:p1:later", ingested="2026-06-11T05:00:00+00:00")])
            latest = load_actions("CN", root=Path(tmp))
            self.assertEqual(1, len(latest))
            self.assertAlmostEqual(0.9, latest[0].cash_amount)  # 05:00Z wins on instants
            before = load_actions("CN", as_of="2026-06-11T02:00:00+00:00", root=Path(tmp))
            self.assertEqual(1, len(before))
            self.assertAlmostEqual(0.5, before[0].cash_amount)  # 02:00Z sees only the +08:00 row

    def test_identical_content_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            action = self._action(cash=0.5, reference="akshare:p1", ingested="2026-06-10T00:00:00+00:00")
            self._write(tmp, [action])
            self._write(tmp, [action])
            frame = pl.read_parquet(Path(tmp) / "cn_actions.parquet")
            self.assertEqual(1, frame.height)

    def test_revision_id_covers_announced_date_currency_reference(self) -> None:
        base = self._action(cash=0.5, reference="akshare:p1", ingested="2026-06-10T00:00:00+00:00")
        announced = self._action(
            cash=0.5, reference="akshare:p1", ingested="2026-06-10T00:00:00+00:00", announced=date(2026, 6, 1)
        )
        reference_changed = CorporateActionRecord(
            market="CN", symbol="600000.SS", action_type="cash_dividend",
            effective_date=date(2026, 6, 20), cash_amount=0.5, currency="CNY",
            source="akshare_fhps", source_reference="akshare:p2", ingested_at="2026-06-10T00:00:00+00:00",
        )
        self.assertNotEqual(base.compute_revision_id(), announced.compute_revision_id())
        self.assertNotEqual(base.compute_revision_id(), reference_changed.compute_revision_id())
