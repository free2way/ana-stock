from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest import TestCase

from app.services.cn_adjustment_factors import (
    derive_factor_actions,
    load_factor_rows,
    normalize_factor_rows,
    summarize_factor_rows,
    write_factor_series,
)


class CnAdjustmentFactorTests(TestCase):
    def test_normalize_dedupes_and_drops_invalid(self) -> None:
        rows = [
            {"symbol": "000001.sz", "trade_date": "2026-01-05", "adj_factor": 10.0},
            {"symbol": "000001.SZ", "trade_date": "2026-01-05", "adj_factor": 10.5},
            {"symbol": "000002.SZ", "trade_date": "2026-01-05", "adj_factor": 0.0},
            {"symbol": "", "trade_date": "2026-01-05", "adj_factor": 1.0},
            {"symbol": "000003.SZ", "trade_date": "bad-date", "adj_factor": 1.0},
        ]
        normalized = normalize_factor_rows(rows)
        self.assertEqual(1, len(normalized))
        self.assertEqual(10.5, normalized[0]["adj_factor"])

    def test_factor_jump_becomes_action_and_flat_segments_do_not(self) -> None:
        rows = [
            {"symbol": "AAA.SZ", "trade_date": "2026-01-02", "adj_factor": 10.0},
            {"symbol": "AAA.SZ", "trade_date": "2026-01-05", "adj_factor": 10.0},
            {"symbol": "AAA.SZ", "trade_date": "2026-01-06", "adj_factor": 20.0},  # 2:1 split
            {"symbol": "AAA.SZ", "trade_date": "2026-01-07", "adj_factor": 20.0},
            {"symbol": "AAA.SZ", "trade_date": "2026-01-08", "adj_factor": 21.0},  # small dividend
        ]
        records = derive_factor_actions(rows)
        self.assertEqual(2, len(records))
        split, dividend = records
        self.assertEqual(date(2026, 1, 6), split.effective_date)
        self.assertAlmostEqual(2.0, split.factor)
        self.assertEqual("adjustment_factor", split.action_type)
        self.assertEqual(date(2026, 1, 8), dividend.effective_date)
        self.assertAlmostEqual(1.05, dividend.factor)

    def test_factor_series_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                {"symbol": "AAA.SZ", "trade_date": "2026-01-02", "adj_factor": 10.0, "source": "tushare"},
                {"symbol": "AAA.SZ", "trade_date": "2026-01-02", "adj_factor": 10.2, "source": "tushare"},
            ]
            write_factor_series(rows, root=root)
            loaded = load_factor_rows(root=root)
            self.assertEqual(1, len(loaded))
            self.assertAlmostEqual(10.2, loaded[0]["adj_factor"])
            summary = summarize_factor_rows(loaded)
            self.assertEqual(1, summary["rows"])
            self.assertEqual("2026-01-02", summary["first_date"])

    def test_symbols_are_independent(self) -> None:
        rows = [
            {"symbol": "AAA.SZ", "trade_date": "2026-01-02", "adj_factor": 10.0},
            {"symbol": "BBB.SZ", "trade_date": "2026-01-02", "adj_factor": 5.0},
            {"symbol": "AAA.SZ", "trade_date": "2026-01-05", "adj_factor": 20.0},
        ]
        records = derive_factor_actions(rows)
        self.assertEqual(["AAA.SZ"], [record.symbol for record in records])
