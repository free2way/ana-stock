from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest import TestCase

from app.services.cn_adjustment_factors import (
    derive_dividend_actions,
    load_dividend_rows,
    write_dividend_rows,
)


class CnDividendActionTests(TestCase):
    def test_stock_and_cash_components_become_separate_actions(self) -> None:
        rows = [
            {
                "symbol": "600000.SS",
                "end_date": "20251231",
                "ex_date": "2026-06-20",
                "cash_div_tax": 0.5,
                "stk_div": 0.3,
                "ann_date": "2026-06-01",
                "source": "tushare_dividend",
            }
        ]
        records = derive_dividend_actions(rows)
        self.assertEqual(2, len(records))
        cash, stock = sorted(records, key=lambda item: item.action_type)
        self.assertEqual("cash_dividend", cash.action_type)
        self.assertEqual(0.5, cash.cash_amount)
        self.assertEqual("stock_dividend", stock.action_type)
        self.assertAlmostEqual(1.3, stock.factor)
        self.assertEqual(date(2026, 6, 20), stock.effective_date)
        self.assertEqual(date(2026, 6, 1), stock.announced_date)
        self.assertTrue(all(record.source == "tushare_dividend" for record in records))

    def test_rows_without_ex_date_or_with_zero_amounts_are_ignored(self) -> None:
        rows = [
            {"symbol": "600000.SS", "ex_date": "", "cash_div_tax": 0.5, "stk_div": 0.0},
            {"symbol": "600001.SS", "ex_date": "2026-06-20", "cash_div_tax": 0.0, "stk_div": 0.0},
            {"symbol": "", "ex_date": "2026-06-20", "cash_div_tax": 0.5, "stk_div": 0.0},
        ]
        self.assertEqual([], derive_dividend_actions(rows))

    def test_dividend_rows_roundtrip_and_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {
                    "symbol": "600000.SS", "end_date": "20251231", "ex_date": "2026-06-20",
                    "cash_div_tax": 0.5, "stk_div": 0.0, "ann_date": "2026-06-01",
                }
            ]
            write_dividend_rows(rows, root=root)
            write_dividend_rows(rows, root=root)
            loaded = load_dividend_rows(root=root)
            self.assertEqual(1, len(loaded))
            self.assertAlmostEqual(0.5, loaded[0]["cash_div_tax"])
