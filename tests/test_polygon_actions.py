from __future__ import annotations

import json
from io import BytesIO
from unittest import TestCase
from unittest.mock import patch

from app.services.polygon_actions import fetch_dividend_rows, fetch_split_rows


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class PolygonActionsTests(TestCase):
    def test_split_rows_are_mapped_with_factor(self) -> None:
        responses = [
            {
                "status": "OK",
                "results": [
                    {"id": "s1", "ticker": "aapl", "execution_date": "2026-01-05", "split_from": 1, "split_to": 2},
                    {"id": "s2", "ticker": "MSFT", "execution_date": "2026-02-10", "split_from": 0, "split_to": 3},
                ],
                "next_url": None,
            }
        ]
        with patch("app.services.polygon_actions._fetch_json", side_effect=responses):
            rows = fetch_split_rows("key", start="2026-01-01", end="2026-12-31")
        self.assertEqual(1, len(rows))
        self.assertEqual("AAPL", rows[0]["symbol"])
        self.assertEqual(2.0, rows[0]["factor"])
        self.assertEqual("2026-01-05", rows[0]["effective_date"])

    def test_dividend_rows_and_pagination(self) -> None:
        responses = [
            {
                "status": "OK",
                "results": [
                    {"id": "d1", "ticker": "AAPL", "ex_dividend_date": "2026-02-06", "cash_amount": 0.25, "currency": "USD"}
                ],
                "next_url": "https://api.polygon.io/v3/reference/dividends?cursor=abc",
            },
            {
                "status": "OK",
                "results": [
                    {"id": "d2", "ticker": "MSFT", "ex_dividend_date": "2026-02-19", "cash_amount": 0.83, "currency": "USD"}
                ],
                "next_url": None,
            },
        ]
        with patch("app.services.polygon_actions._fetch_json", side_effect=responses):
            rows = fetch_dividend_rows("key", start="2026-01-01")
        self.assertEqual(["AAPL", "MSFT"], [row["symbol"] for row in rows])
        self.assertEqual(0.25, rows[0]["cash_amount"])

    def test_error_status_raises(self) -> None:
        with patch(
            "app.services.polygon_actions._fetch_json",
            return_value={"status": "ERROR", "error": "not entitled"},
        ):
            with self.assertRaisesRegex(RuntimeError, "polygon error"):
                fetch_split_rows("key", start="2026-01-01")
