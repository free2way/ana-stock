import json
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import polars as pl

from app.services.hithink_finance_client import HithinkFinanceClient
from app.services.hithink_market_data import import_hithink_market_dump
from app.services.openbb_client import HistoricalPriceRequest
from app.services.providers.fundamental import HithinkFinanceFundamentalProvider, resolve_fundamental_provider
from app.services.providers.price import HithinkFinancePriceProvider, resolve_price_provider


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeDownloadResponse:
    def __init__(self, payload: bytes, *, content_length: int | None = None):
        self._stream = BytesIO(payload)
        self.headers = {
            "Content-Length": str(len(payload) if content_length is None else content_length)
        }

    def read(self, size: int = -1):
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _settings():
    return SimpleNamespace(
        hithink_finance_api_key="fixture-secret",
        hithink_finance_base_url="https://example.invalid",
        hithink_finance_timeout_seconds=1.0,
        hithink_finance_max_retries=0,
    )


class HithinkFinanceTests(unittest.TestCase):
    def test_market_dump_excludes_bse_from_automatic_cn_price_lake(self):
        frame = pl.DataFrame({
            "thscode": ["600000.SH", "920001.BJ"],
            "date_ms": [1789084800000, 1789084800000],
            "open_price": [10.0, 20.0],
            "high_price": [10.5, 20.5],
            "low_price": [9.5, 19.5],
            "close_price": [10.2, 20.2],
            "volume": [1000.0, 2000.0],
        })
        with TemporaryDirectory() as directory, patch(
            "app.services.hithink_market_data.get_settings",
            return_value=SimpleNamespace(raw_data_dir=Path(directory)),
        ), patch.object(HithinkFinanceClient, "is_configured", return_value=True), patch.object(
            HithinkFinanceClient, "download_market_dump", return_value={"bytes": 100},
        ), patch("app.services.hithink_market_data.pl.read_parquet", return_value=frame), patch(
            "app.services.hithink_market_data.write_ohlcv_rows_to_lake", return_value=[]
        ) as write_lake:
            result = import_hithink_market_dump()

        self.assertEqual("success", result["status"])
        self.assertEqual(1, result["excluded_unsupported_rows"])
        self.assertEqual(1, result["rows_written"])
        self.assertEqual(["600000.SS"], [row["symbol"] for row in write_lake.call_args.kwargs["rows"]])

    def test_invalid_dump_does_not_replace_last_valid_file(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "daily-k-10d-latest.parquet"
            original = b"PAR1previous-validPAR1"
            destination.write_bytes(original)
            with patch(
                "app.services.hithink_finance_client.get_settings",
                return_value=_settings(),
            ), patch.object(
                HithinkFinanceClient,
                "get_market_dump_download_url",
                return_value=("https://example.invalid/dump", None),
            ), patch(
                "app.services.hithink_finance_client.urlopen",
                return_value=_FakeDownloadResponse(b"truncated"),
            ):
                client = HithinkFinanceClient()
                with self.assertRaises(RuntimeError):
                    client.download_market_dump(
                        kind="daily-k-10d",
                        destination=destination,
                    )

            self.assertEqual(original, destination.read_bytes())

    def test_valid_dump_is_atomically_published_after_validation(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "daily-k-10d-latest.parquet"
            payload = b"PAR1new-valid-payloadPAR1"
            with patch(
                "app.services.hithink_finance_client.get_settings",
                return_value=_settings(),
            ), patch.object(
                HithinkFinanceClient,
                "get_market_dump_download_url",
                return_value=("https://example.invalid/dump", None),
            ), patch(
                "app.services.hithink_finance_client.urlopen",
                return_value=_FakeDownloadResponse(payload),
            ):
                result = HithinkFinanceClient().download_market_dump(
                    kind="daily-k-10d",
                    destination=destination,
                )

            self.assertEqual(payload, destination.read_bytes())
            self.assertEqual(len(payload), result["bytes"])

    def test_symbol_mapping_and_provider_resolution(self):
        self.assertEqual("600519.SH", HithinkFinanceClient.to_thscode("600519.SS"))
        self.assertEqual("000001.SZ", HithinkFinanceClient.to_thscode("000001.SZ"))
        self.assertEqual("920001.BJ", HithinkFinanceClient.to_thscode("920001.BJ"))
        self.assertEqual("600519.SS", HithinkFinanceClient.to_internal_ticker("600519.SH"))
        self.assertIsInstance(resolve_price_provider("hithink_finance", market="CN"), HithinkFinancePriceProvider)
        self.assertIsInstance(
            resolve_fundamental_provider("hithink_finance", market="CN"),
            HithinkFinanceFundamentalProvider,
        )

    def test_historical_prices_are_mapped_to_canonical_raw_lake_rows(self):
        payload = {
            "code": 0,
            "message": "success",
            "request_id": "req-fixture",
            "data": {
                "timestamp": 1787241600000,
                "item": [
                    {
                        "date_ms": 1787241600000,
                        "open_price": 1291.5,
                        "high_price": 1291.5,
                        "low_price": 1272.01,
                        "close_price": 1272.83,
                        "volume": 3347231,
                        "turnover": 4.28e9,
                    }
                ],
            },
        }
        with patch("app.services.hithink_finance_client.get_settings", return_value=_settings()), patch(
            "app.services.hithink_finance_client.urlopen", return_value=_FakeResponse(payload)
        ):
            provider = HithinkFinancePriceProvider()
            rows = provider.fetch_historical_prices(
                HistoricalPriceRequest(
                    ticker="600519.SS",
                    start_date="2026-08-21",
                    end_date="2026-08-21",
                    provider="hithink_finance",
                )
            )

        self.assertEqual(1, len(rows))
        self.assertEqual("600519.SS", rows[0]["symbol"])
        self.assertEqual("2026-08-21", rows[0]["date"])
        self.assertEqual(1272.83, rows[0]["close"])
        self.assertEqual(rows[0]["close"], rows[0]["adj_close"])
        self.assertEqual("hithink_finance", provider.last_source_used)

    def test_api_business_error_does_not_expose_credential(self):
        payload = {"code": 2003, "message": "permission denied", "request_id": "req-denied", "data": None}
        with patch("app.services.hithink_finance_client.get_settings", return_value=_settings()), patch(
            "app.services.hithink_finance_client.urlopen", return_value=_FakeResponse(payload)
        ):
            client = HithinkFinanceClient()
            with self.assertRaises(RuntimeError) as caught:
                client.fetch_valuation_snapshots(["600519.SS"])
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertIn("permission denied", str(caught.exception))

    def test_fundamental_snapshot_keeps_current_valuation_time_separate(self):
        class FakeClient:
            last_request_id = "req-fundamental"

            def is_configured(self):
                return True

            def to_internal_ticker(self, value):
                return HithinkFinanceClient.to_internal_ticker(value)

            def fetch_valuation_snapshots(self, _tickers):
                return ([{"thscode": "600519.SH", "name": "贵州茅台", "pe_ttm": "20.5"}], 1787396124000)

            def fetch_financial_statements(self, _ticker, *, statement, period, limit):
                if statement == "income":
                    return ([{
                        "fiscal_year": 2026,
                        "fiscal_period": "H1",
                        "period_end_ms": 1782748800000,
                        "report_date_ms": 1786377600000,
                        "operating_income": 100,
                        "parent_holder_net_profit": 50,
                    }], None)
                return ([{
                    "period_end_ms": 1782748800000,
                    "report_date_ms": 1786377600000,
                    "assets_total": 1000,
                    "total_debt": 200,
                }], None)

            def report_code(self, _row):
                return "2026-2"

            def fetch_financial_indicators(self, _ticker, *, report):
                self.report = report
                return ({
                    "calculate_operating_income_yoy_growth_ratio": 8.4,
                    "calculate_parent_holder_net_profit_yoy_growth_ratio": 12.5,
                    "assets_debt_ratio": 20.0,
                }, {"report": report})

            _to_float = staticmethod(HithinkFinanceClient._to_float)
            _optional_int = staticmethod(HithinkFinanceClient._optional_int)

        provider = HithinkFinanceFundamentalProvider.__new__(HithinkFinanceFundamentalProvider)
        provider.last_source_used = provider.name
        provider.last_error = None
        provider.last_diagnostics = {}
        provider.client = FakeClient()
        rows = provider.fetch_snapshots(["600519.SS"], metadata={"600519.SS": {"exchange": "SSE"}})

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("2026-06-30", row["report_date"])
        self.assertEqual(20.5, row["pe_ttm"])
        self.assertEqual(8.4, row["revenue_yoy"])
        self.assertEqual(12.5, row["net_profit_yoy"])
        self.assertEqual(20.0, row["debt_to_assets"])
        self.assertNotEqual(
            row["feature_times"]["pe_ttm"]["event_time"],
            row["feature_times"]["revenue_yoy"]["event_time"],
        )
        ingested_time = datetime.fromisoformat(row["ingested_time"])
        self.assertGreater(
            ingested_time,
            datetime.fromisoformat(row["available_time"]),
        )
        self.assertEqual(
            row["ingested_time"],
            row["feature_times"]["revenue_yoy"]["ingested_time"],
        )
        self.assertGreater(
            ingested_time,
            datetime.fromisoformat(row["feature_times"]["pe_ttm"]["available_time"]),
        )
        self.assertFalse(row["revision_history_preserved"])

    def test_fundamental_provider_emits_historical_periods_without_backfilling_current_valuation(self):
        class FakeClient:
            last_request_id = "req-history"

            def is_configured(self):
                return True

            def to_internal_ticker(self, value):
                return HithinkFinanceClient.to_internal_ticker(value)

            def fetch_valuation_snapshots(self, _tickers):
                return ([{"thscode": "600519.SH", "name": "贵州茅台", "pe_ttm": "20.5"}], 1787396124000)

            def fetch_financial_statements(self, _ticker, *, statement, period, limit):
                self.requested_limit = limit
                if statement == "income":
                    return ([
                        {
                            "fiscal_year": 2026,
                            "fiscal_period": "Q2",
                            "period_end_ms": 1782748800000,
                            "report_date_ms": 1786377600000,
                            "operating_income": 120,
                            "parent_holder_net_profit": 60,
                        },
                        {
                            "fiscal_year": 2025,
                            "fiscal_period": "Q2",
                            "period_end_ms": 1751212800000,
                            "report_date_ms": 1753891200000,
                            "operating_income": 100,
                            "parent_holder_net_profit": 50,
                        },
                    ], None)
                return ([
                    {
                        "period_end_ms": 1782748800000,
                        "report_date_ms": 1786464000000,
                        "assets_total": 1000,
                        "total_debt": 200,
                    },
                    {
                        "period_end_ms": 1751212800000,
                        "report_date_ms": 1753977600000,
                        "assets_total": 800,
                        "total_debt": 200,
                    },
                ], None)

            def report_code(self, row):
                return f"{row.get('fiscal_year')}-2" if row else None

            def fetch_financial_indicators(self, _ticker, *, report):
                return ({}, {"report": report})

            _to_float = staticmethod(HithinkFinanceClient._to_float)
            _optional_int = staticmethod(HithinkFinanceClient._optional_int)

        provider = HithinkFinanceFundamentalProvider.__new__(HithinkFinanceFundamentalProvider)
        provider.last_source_used = provider.name
        provider.last_error = None
        provider.last_diagnostics = {}
        provider.client = FakeClient()

        rows = provider.fetch_snapshots(["600519.SS"])

        self.assertEqual(2, len(rows))
        self.assertEqual(20, provider.client.requested_limit)
        self.assertEqual(20.5, rows[0]["pe_ttm"])
        self.assertIsNone(rows[1]["pe_ttm"])
        self.assertAlmostEqual(20.0, rows[0]["revenue_yoy"])
        self.assertAlmostEqual(20.0, rows[0]["net_profit_yoy"])
        self.assertAlmostEqual(25.0, rows[1]["debt_to_assets"])
        self.assertNotIn("pe_ttm", rows[1]["feature_times"])
        self.assertEqual(1, provider.last_diagnostics["historical_periods_returned"])

    def test_historical_period_does_not_reuse_a_different_balance_period(self):
        provider = HithinkFinanceFundamentalProvider.__new__(HithinkFinanceFundamentalProvider)
        matched = provider._matching_balance(
            {"period_end_ms": 100},
            [{"period_end_ms": 200, "assets_total": 999}],
        )
        self.assertEqual({}, matched)


if __name__ == "__main__":
    unittest.main()
