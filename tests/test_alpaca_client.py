from __future__ import annotations

from unittest import TestCase

from app.services.alpaca_client import AlpacaClient, normalize_corporate_action_rows


class _FakeClient(AlpacaClient):
    def __init__(self, responses):
        super().__init__(api_key="k", api_secret="s")
        self._responses = list(responses)
        self.urls: list[str] = []

    def _get(self, url, *, attempts=4):
        self.urls.append(url)
        return self._responses.pop(0)


class AlpacaClientTests(TestCase):
    def test_bars_are_paginated_and_mapped(self) -> None:
        client = _FakeClient(
            [
                {
                    "bars": [
                        {"t": "2026-10-01T04:00:00Z", "o": 10.0, "h": 10.5, "l": 9.9, "c": 10.2, "v": 1000},
                        {"t": "2026-10-02T04:00:00Z", "o": 10.2, "h": 10.8, "l": 10.1, "c": 10.7, "v": 1200},
                    ],
                    "next_page_token": "tok",
                },
                {"bars": [{"t": "2026-10-05T04:00:00Z", "o": 10.7, "h": 11.0, "l": 10.6, "c": 10.9, "v": 900}], "next_page_token": None},
            ]
        )
        rows = client.fetch_daily_bars("aaa", start="2026-10-01", end="2026-10-05")
        self.assertEqual(["2026-10-01", "2026-10-02", "2026-10-05"], [row["date"] for row in rows])
        self.assertEqual(2, len(client.urls))
        self.assertIn("page_token=tok", client.urls[1])
        self.assertIn("adjustment=raw", client.urls[0])
        self.assertIn("AAA", client.urls[0])

    def test_corporate_actions_are_paginated(self) -> None:
        client = _FakeClient(
            [
                {"announcements": [{"id": "1", "ca_type": "dividend", "target_symbol": "AAA", "ex_date": "2026-09-01", "cash": "0.5"}], "next_page_token": "next"},
                {"announcements": [{"id": "2", "ca_type": "split", "target_symbol": "BBB", "ex_date": "2026-09-02", "old_rate": "1", "new_rate": "4"}], "next_page_token": None},
            ]
        )
        records = client.list_corporate_actions(start="2026-08-01", end="2026-09-30")
        self.assertEqual(2, len(records))
        self.assertIn("since=2026-08-01", client.urls[0])
        self.assertIn("ca_types=split%2Cdividend", client.urls[0])

    def test_multi_bars_dedupe_overlapping_pages(self) -> None:
        client = _FakeClient(
            [
                {
                    "bars": {
                        "AAA": [
                            {"t": "2026-10-01T04:00:00Z", "o": 10.0, "h": 10.5, "l": 9.9, "c": 10.2, "v": 1000},
                            {"t": "2026-10-02T04:00:00Z", "o": 10.2, "h": 10.8, "l": 10.1, "c": 10.7, "v": 1200},
                        ],
                        "BBB": [{"t": "2026-10-01T04:00:00Z", "o": 5.0, "h": 5.1, "l": 4.9, "c": 5.05, "v": 500}],
                    },
                    "next_page_token": "tok",
                },
                {
                    "bars": {
                        "AAA": [
                            {"t": "2026-10-02T04:00:00Z", "o": 10.2, "h": 10.8, "l": 10.1, "c": 10.7, "v": 1200},
                            {"t": "2026-10-05T04:00:00Z", "o": 10.7, "h": 11.0, "l": 10.6, "c": 10.9, "v": 900},
                        ]
                    },
                    "next_page_token": None,
                },
            ]
        )
        out = client.fetch_daily_bars_multi(["AAA", "BBB"], start="2026-10-01", end="2026-10-05")
        self.assertEqual(["2026-10-01", "2026-10-02", "2026-10-05"], [row["date"] for row in out["AAA"]])
        self.assertEqual(["2026-10-01"], [row["date"] for row in out["BBB"]])

    def test_unconfigured_client_fails_fast(self) -> None:
        client = AlpacaClient(api_key=None, api_secret=None)
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            client.get_account()

    def test_action_normalization_covers_splits_and_dividends(self) -> None:
        rows = normalize_corporate_action_rows(
            [
                {"id": "s1", "ca_type": "split", "target_symbol": "aaa", "ex_date": "2026-09-01", "old_rate": "1", "new_rate": "2"},
                {"id": "s2", "ca_type": "split", "target_symbol": "BBB", "ex_date": "2026-09-02", "old_rate": "7", "new_rate": "1"},
                {"id": "d1", "ca_type": "dividend", "target_symbol": "CCC", "ex_date": "2026-09-03", "cash": "0.25"},
                {"id": "d2", "ca_type": "dividend", "target_symbol": "DDD", "ex_date": "2026-09-04", "cash": "0"},
                {"id": "x", "ca_type": "split", "target_symbol": "EEE", "ex_date": "bad-date", "old_rate": "1", "new_rate": "2"},
            ]
        )
        by_symbol = {row["symbol"]: row for row in rows}
        self.assertAlmostEqual(2.0, by_symbol["AAA"]["factor"])
        self.assertAlmostEqual(1.0 / 7.0, by_symbol["BBB"]["factor"])
        self.assertAlmostEqual(0.25, by_symbol["CCC"]["cash_amount"])
        self.assertNotIn("DDD", by_symbol)
        self.assertNotIn("EEE", by_symbol)
        self.assertTrue(all(row["source"] == "alpaca" for row in rows))
