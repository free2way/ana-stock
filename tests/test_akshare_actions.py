from __future__ import annotations

from unittest import TestCase

from app.services.akshare_actions import app_ticker_from_code, normalize_fhps_rows


def _row(**overrides) -> dict:
    row = {
        "代码": "000990",
        "除权除息日": "2026-07-30",
        "股权登记日": "2026-07-29",
        "预案公告日": "2026-06-20",
        "方案进度": "实施分配",
        "送转股份-送转总比例": 4.0,
        "现金分红-现金分红比例": 1.5,
    }
    row.update(overrides)
    return row


class AkshareActionsTests(TestCase):
    def test_ticker_suffix_mapping(self) -> None:
        self.assertEqual("600000.SS", app_ticker_from_code("600000"))
        self.assertEqual("688001.SS", app_ticker_from_code("688001"))
        self.assertEqual("000001.SZ", app_ticker_from_code("000001"))
        self.assertEqual("300750.SZ", app_ticker_from_code("300750"))
        self.assertEqual("830799.BJ", app_ticker_from_code("830799"))
        self.assertEqual("", app_ticker_from_code("bad"))

    def test_per_ten_conversion_and_factor_inputs(self) -> None:
        rows = normalize_fhps_rows([_row()], period="20251231")
        self.assertEqual(1, len(rows))
        record = rows[0]
        self.assertEqual("000990.SZ", record["symbol"])
        self.assertAlmostEqual(0.4, record["stk_div"])  # 10 送 4 -> 0.4/share
        self.assertAlmostEqual(0.15, record["cash_div_tax"])
        self.assertEqual("2026-07-30", record["ex_date"])
        self.assertEqual("akshare:fhps:20251231:000990", record["source_reference"])

    def test_unimplemented_or_empty_rows_are_dropped(self) -> None:
        rows = normalize_fhps_rows(
            [
                _row(方案进度="董事会预案"),
                _row(除权除息日=None),
                _row(**{"送转股份-送转总比例": 0.0, "现金分红-现金分红比例": 0.0}),
                _row(代码="830799", 除权除息日="2026-08-01"),
            ],
            period="20251231",
        )
        self.assertEqual(["830799.BJ"], [record["symbol"] for record in rows])
        self.assertAlmostEqual(0.4, rows[0]["stk_div"])

    def test_nan_and_dash_values_are_ignored(self) -> None:
        rows = normalize_fhps_rows(
            [_row(**{"送转股份-送转总比例": float("nan"), "现金分红-现金分红比例": "--"})],
            period="20251231",
        )
        self.assertEqual([], rows)
